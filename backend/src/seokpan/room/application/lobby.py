"""Lobby and Room use cases shared by HTTP and later WebSocket transports."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, replace
from typing import Protocol
from uuid import UUID, uuid4
from weakref import WeakValueDictionary

from seokpan.identity.application import (
    CreateSession,
    ParticipantSessionPort,
    SessionActorType,
    SessionRecord,
    SessionTransitionUnavailable,
)
from seokpan.room.application.realtime import NullRealtimeEventAdapter, RealtimeEventPort
from seokpan.room.application.runtime import (
    ChangeRoomIdentity,
    ChangeRoomTeam,
    ChangeRoomVoteSeconds,
    ConnectRoomParticipant,
    CreateRoomRuntime,
    DisconnectRoomParticipant,
    ExpireRoomDisconnect,
    JoinRoomRuntime,
    KickRoomParticipant,
    LeaveRoomRuntime,
    RoomMutationResult,
    RoomPasswordPort,
    RoomRuntimePort,
    RoomRuntimeSnapshot,
    SetRoomReady,
    StartRoomGame,
)
from seokpan.room.domain import (
    ActorType,
    RoomConfig,
    RoomRuleViolation,
    RoomStatus,
    RoomVisibility,
    Team,
)
from seokpan.vote.application import VoteRuntimePort, VoteRuntimeSnapshot
from seokpan.vote.domain import ParticipantRole as VoteParticipantRole
from seokpan.vote.domain import TurnStatus

_LOGGER = logging.getLogger(__name__)


class LobbyRoomRuntimePort(RoomRuntimePort, Protocol):
    async def list_rooms(self) -> tuple[RoomRuntimeSnapshot, ...]: ...


class RoomParticipationResolver(Protocol):
    async def by_session(self, session_digest: str) -> RoomParticipation | None: ...

    async def by_participant(self, participant_id: str) -> RoomParticipation | None: ...

    async def by_transition_source(self, previous: SessionRecord) -> RoomParticipation | None: ...

    async def identity_transition_applied(
        self,
        previous: SessionRecord,
        replacement: CreateSession,
        participant_id: str,
    ) -> bool | None: ...


@dataclass(frozen=True, slots=True)
class RoomParticipation:
    session_digest: str
    room_id: str
    participant_id: str
    actor_type: SessionActorType
    actor_id: str
    connection_generation: int | None = None
    connected: bool = True


@dataclass(slots=True, weakref_slot=True)
class ParticipationWatch:
    """Process-local invalidation for readers; not shared Provider state."""

    unchanged: bool = True


class RoomApplicationService(ParticipantSessionPort):
    """Coordinate Session participation with provider-neutral Room state."""

    def __init__(
        self,
        runtime: LobbyRoomRuntimePort,
        passwords: RoomPasswordPort,
        events: RealtimeEventPort | None = None,
        votes: VoteRuntimePort | None = None,
        participation_resolver: RoomParticipationResolver | None = None,
    ) -> None:
        self._runtime = runtime
        self._passwords = passwords
        self._events = events or NullRealtimeEventAdapter()
        self._votes = votes
        self._participation_resolver = participation_resolver
        self._by_session: dict[str, RoomParticipation] = {}
        self._by_participant: dict[str, RoomParticipation] = {}
        self._participation_watches: WeakValueDictionary[str, ParticipationWatch] = (
            WeakValueDictionary()
        )
        self._results: dict[tuple[str, str, str], tuple[str, RoomMutationResult]] = {}

    async def list_rooms(self) -> tuple[RoomRuntimeSnapshot, ...]:
        return tuple(
            room
            for room in await self._runtime.list_rooms()
            if room.status is not RoomStatus.CLOSED
        )

    async def get(self, room_id: str) -> RoomRuntimeSnapshot | None:
        return await self._runtime.get(room_id)

    def participation(self, session_digest: str) -> RoomParticipation | None:
        return self._by_session.get(session_digest)

    def watch_participation(self, session_digest: str) -> ParticipationWatch:
        watch = self._participation_watches.get(session_digest)
        if watch is None:
            watch = ParticipationWatch()
            self._participation_watches[session_digest] = watch
        return watch

    def _invalidate_participation_watch(self, session_digest: str) -> None:
        watch = self._participation_watches.pop(session_digest, None)
        if watch is not None:
            watch.unchanged = False

    def current_room(self, session_digest: str) -> tuple[str, str] | None:
        participation = self.participation(session_digest)
        if participation is None:
            return None
        return participation.room_id, participation.participant_id

    def participant_identity(self, participant_id: str) -> RoomParticipation | None:
        return self._by_participant.get(participant_id)

    async def resolve_participation(self, session_digest: str) -> RoomParticipation | None:
        if self._participation_resolver is not None:
            return await self._participation_resolver.by_session(session_digest)
        return self.participation(session_digest)

    async def resolve_participant_identity(self, participant_id: str) -> RoomParticipation | None:
        if self._participation_resolver is not None:
            return await self._participation_resolver.by_participant(participant_id)
        return self.participant_identity(participant_id)

    async def resolve_current_room(self, session_digest: str) -> tuple[str, str] | None:
        participation = await self.resolve_participation(session_digest)
        if participation is None:
            return None
        return participation.room_id, participation.participant_id

    async def create_room(
        self,
        *,
        session: SessionRecord,
        request_id: str,
        config: RoomConfig,
        password: str | None,
    ) -> RoomMutationResult:
        key = (session.session_digest, "create", request_id)
        fingerprint = _fingerprint(config, password)
        replay = self._replay(key, fingerprint)
        if replay is not None:
            return replay
        if session.actor_type is not SessionActorType.MEMBER:
            raise RoomRuleViolation("MEMBER_REQUIRED_TO_CREATE_ROOM")
        await self._require_not_participating(session.session_digest)
        room_id, participant_id = self._ids_for(session.session_digest, "create", request_id)
        encoded_password = None
        if config.visibility is RoomVisibility.PRIVATE:
            if password is None or not 4 <= len(password) <= 20:
                raise RoomRuleViolation("INVALID_ROOM_PASSWORD")
            encoded_password = await self._passwords.encode(password)
        elif password is not None:
            raise RoomRuleViolation("INVALID_ROOM_PASSWORD")
        result = await self._runtime.create(
            CreateRoomRuntime(
                room_id=room_id,
                request_id=request_id,
                config=config,
                owner_id=participant_id,
                owner_session_digest=session.session_digest,
                encoded_password=encoded_password,
            )
        )
        self._bind(session, room_id, participant_id)
        self._results[key] = (fingerprint, result)
        await self._lobby_changed("ROOM_CREATED", room_id)
        return result

    async def join_room(
        self,
        *,
        session: SessionRecord,
        room_id: str,
        request_id: str,
        expected_state_version: int,
        password: str | None,
    ) -> RoomMutationResult:
        key = (session.session_digest, f"join:{room_id}", request_id)
        fingerprint = _fingerprint(room_id, expected_state_version, password)
        replay = self._replay(key, fingerprint)
        if replay is not None:
            return replay
        await self._require_not_participating(session.session_digest)
        encoded_password = await self._runtime.get_private_access_hash(room_id)
        if encoded_password is None:
            if password is not None:
                raise RoomRuleViolation("INVALID_ROOM_PASSWORD")
            verified = False
        else:
            if password is None or not await self._passwords.verify(encoded_password, password):
                raise RoomRuleViolation("ROOM_PASSWORD_INVALID")
            verified = True
        _unused_room_id, participant_id = self._ids_for(
            session.session_digest, f"join:{room_id}", request_id
        )
        result = await self._runtime.join(
            JoinRoomRuntime(
                room_id=room_id,
                request_id=request_id,
                participant_id=participant_id,
                actor_type=_room_actor_type(session.actor_type),
                session_digest=session.session_digest,
                expected_state_version=expected_state_version,
                private_access_verified=verified,
            )
        )
        self._bind(session, room_id, participant_id)
        self._results[key] = (fingerprint, result)
        await self._room_changed(
            "room.participant_joined",
            result,
            room_id=room_id,
            payload={"participant_id": participant_id},
        )
        await self._lobby_changed("PARTICIPANT_JOINED", room_id)
        return result

    async def change_team(
        self,
        *,
        session: SessionRecord,
        request_id: str,
        expected_state_version: int,
        team: Team,
    ) -> RoomMutationResult:
        participation = await self._require_participation(session.session_digest)
        result = await self._runtime.change_team(
            ChangeRoomTeam(
                room_id=participation.room_id,
                request_id=request_id,
                participant_id=participation.participant_id,
                team=team,
                expected_state_version=expected_state_version,
            )
        )
        await self._room_changed(
            "room.team_changed",
            result,
            room_id=participation.room_id,
            payload={"participant_id": participation.participant_id, "team": team.value},
        )
        return result

    async def set_ready(
        self,
        *,
        session: SessionRecord,
        request_id: str,
        expected_state_version: int,
        ready: bool,
    ) -> RoomMutationResult:
        participation = await self._require_participation(session.session_digest)
        result = await self._runtime.set_ready(
            SetRoomReady(
                room_id=participation.room_id,
                request_id=request_id,
                participant_id=participation.participant_id,
                ready=ready,
                expected_state_version=expected_state_version,
            )
        )
        await self._room_changed(
            "room.ready_changed",
            result,
            room_id=participation.room_id,
            payload={"participant_id": participation.participant_id, "ready": ready},
        )
        return result

    async def change_vote_seconds(
        self,
        *,
        session: SessionRecord,
        request_id: str,
        expected_state_version: int,
        vote_seconds: int,
    ) -> RoomMutationResult:
        participation = await self._require_participation(session.session_digest)
        result = await self._runtime.change_vote_seconds(
            ChangeRoomVoteSeconds(
                room_id=participation.room_id,
                request_id=request_id,
                actor_id=participation.participant_id,
                vote_seconds=vote_seconds,
                expected_state_version=expected_state_version,
            )
        )
        if (
            result.replayed
            or result.snapshot is None
            or result.snapshot.state_version == expected_state_version
        ):
            return result
        await self._room_changed(
            "room.settings_changed",
            result,
            room_id=participation.room_id,
            payload={"vote_seconds": vote_seconds},
        )
        await self._lobby_changed(
            "ROOM_SETTINGS_CHANGED",
            participation.room_id,
            event_key=(
                f"room.settings_changed:{participation.room_id}:{result.snapshot.state_version}"
            ),
        )
        return result

    async def start_game(
        self,
        *,
        session: SessionRecord,
        request_id: str,
        game_id: str,
        expected_state_version: int,
        notify_realtime: bool = True,
    ) -> RoomMutationResult:
        participation = await self._require_participation(session.session_digest)
        result = await self._runtime.start_game(
            StartRoomGame(
                room_id=participation.room_id,
                request_id=request_id,
                actor_id=participation.participant_id,
                game_id=game_id,
                expected_state_version=expected_state_version,
            )
        )
        if notify_realtime:
            await self._room_changed(
                "snapshot.required",
                result,
                room_id=participation.room_id,
                payload={"reason": "GAME_STARTED"},
                game_id=game_id,
            )
            await self._lobby_changed("GAME_STARTED", participation.room_id)
        return result

    async def kick_participant(
        self,
        *,
        session: SessionRecord,
        target_id: str,
        request_id: str,
        expected_state_version: int,
    ) -> RoomMutationResult:
        actor = await self._require_participation(session.session_digest)
        result = await self._runtime.kick(
            KickRoomParticipant(
                room_id=actor.room_id,
                request_id=request_id,
                actor_id=actor.participant_id,
                target_id=target_id,
                expected_state_version=expected_state_version,
            )
        )
        if result.replayed:
            return result
        # Resolve the binding after the mutation: Guest login may have rotated
        # its session while the provider was processing this command.
        target = self._by_participant.get(target_id)
        if target is not None and target.room_id == actor.room_id:
            self._unbind(target)
        await self._room_changed(
            "room.participant_left",
            result,
            room_id=actor.room_id,
            payload={"participant_id": target_id, "reason": "KICKED"},
        )
        await self._lobby_changed("PARTICIPANT_LEFT", actor.room_id)
        return result

    async def leave_room(
        self,
        *,
        session: SessionRecord,
        request_id: str,
        expected_state_version: int,
    ) -> RoomMutationResult:
        key = (session.session_digest, "leave", request_id)
        fingerprint = _fingerprint(expected_state_version)
        replay = self._replay(key, fingerprint)
        if replay is not None:
            return replay
        participation = await self._require_participation(session.session_digest)
        active_vote_turn = await self._active_vote_turn(participation.room_id)
        result = await self._runtime.leave(
            LeaveRoomRuntime(
                room_id=participation.room_id,
                request_id=request_id,
                participant_id=participation.participant_id,
                expected_state_version=expected_state_version,
                active_vote_turn=active_vote_turn,
            )
        )
        if result.room_closed:
            self._unbind_room(participation.room_id)
            await self._closed(participation.room_id)
        else:
            self._unbind(participation)
            await self._room_changed(
                "room.participant_left",
                result,
                room_id=participation.room_id,
                payload={"participant_id": participation.participant_id},
            )
            await self._owner_changed(result, participation.room_id)
        await self._lobby_changed("PARTICIPANT_LEFT", participation.room_id)
        if result.vote_removed:
            await self._vote_tally_changed(
                participation.room_id,
                f"vote-leave:{participation.participant_id}:{request_id}",
            )
        self._results[key] = (fingerprint, result)
        return result

    async def connect(
        self,
        *,
        session: SessionRecord,
        room_id: str,
    ) -> RoomMutationResult:
        participation = await self._require_participation(session.session_digest)
        if participation.room_id != room_id:
            raise RoomRuleViolation("SESSION_NOT_IN_ROOM")
        result: RoomMutationResult | None = None
        for attempt in range(3):
            snapshot = await self._runtime.get(room_id)
            if snapshot is None:
                self._unbind(participation)
                raise RoomRuleViolation("ROOM_NOT_FOUND")
            try:
                result = await self._runtime.connect(
                    ConnectRoomParticipant(
                        room_id=room_id,
                        request_id=str(uuid4()),
                        participant_id=participation.participant_id,
                        session_digest=session.session_digest,
                        expected_state_version=snapshot.state_version,
                    )
                )
                break
            except RoomRuleViolation as error:
                if error.code != "STATE_VERSION_CONFLICT" or attempt == 2:
                    raise
        assert result is not None
        if result.connection_generation is None:
            raise RoomRuleViolation("CONNECTION_GENERATION_MISSING")
        self._update_local_connection(
            participation,
            generation=result.connection_generation,
            connected=True,
        )
        await self._room_changed(
            "snapshot.required",
            result,
            room_id=room_id,
            payload={"reason": "PARTICIPANT_CONNECTED"},
        )
        return result

    async def disconnect(
        self,
        *,
        session: SessionRecord,
        room_id: str,
        connection_generation: int,
        active_vote_turn: int | None = None,
    ) -> RoomMutationResult:
        participation = await self._require_participation(session.session_digest)
        if participation.room_id != room_id:
            raise RoomRuleViolation("SESSION_NOT_IN_ROOM")
        return await self.disconnect_participant(
            room_id=room_id,
            participant_id=participation.participant_id,
            connection_generation=connection_generation,
            active_vote_turn=active_vote_turn,
        )

    async def disconnect_participant(
        self,
        *,
        room_id: str,
        participant_id: str,
        connection_generation: int,
        active_vote_turn: int | None = None,
    ) -> RoomMutationResult:
        result: RoomMutationResult | None = None
        for attempt in range(3):
            snapshot = await self._runtime.get(room_id)
            if snapshot is None:
                participation = self._by_participant.get(participant_id)
                if participation is not None:
                    self._unbind(participation)
                raise RoomRuleViolation("ROOM_NOT_FOUND")
            try:
                result = await self._runtime.disconnect(
                    DisconnectRoomParticipant(
                        room_id=room_id,
                        request_id=str(uuid4()),
                        participant_id=participant_id,
                        connection_generation=connection_generation,
                        expected_state_version=snapshot.state_version,
                        active_vote_turn=active_vote_turn,
                    )
                )
                break
            except RoomRuleViolation as error:
                if error.code != "STATE_VERSION_CONFLICT" or attempt == 2:
                    raise
        assert result is not None
        if result.stale_connection or result.replayed:
            return result
        local_participation = self._by_participant.get(participant_id)
        if local_participation is not None:
            self._update_local_connection(
                local_participation,
                generation=connection_generation,
                connected=False,
            )
        if result.room_closed:
            self._unbind_room(room_id)
            await self._closed(room_id)
            await self._lobby_changed("ROOM_CLOSED", room_id)
            return result
        await self._owner_changed(result, room_id)
        await self._room_changed(
            "snapshot.required",
            result,
            room_id=room_id,
            payload={"reason": "PARTICIPANT_DISCONNECTED"},
        )
        if result.vote_removed:
            await self._vote_tally_changed(
                room_id,
                f"vote-disconnect:{participant_id}:{connection_generation}",
            )
        return result

    async def expire_disconnect(
        self,
        *,
        room_id: str,
        participant_id: str,
        connection_generation: int,
        active_vote_turn: int | None = None,
    ) -> RoomMutationResult:
        result: RoomMutationResult | None = None
        for attempt in range(3):
            snapshot = await self._runtime.get(room_id)
            if snapshot is None:
                raise RoomRuleViolation("ROOM_NOT_FOUND")
            try:
                result = await self._runtime.expire_disconnect(
                    ExpireRoomDisconnect(
                        room_id=room_id,
                        request_id=str(uuid4()),
                        participant_id=participant_id,
                        connection_generation=connection_generation,
                        expected_state_version=snapshot.state_version,
                        active_vote_turn=active_vote_turn,
                    )
                )
                break
            except RoomRuleViolation as error:
                if error.code != "STATE_VERSION_CONFLICT" or attempt == 2:
                    raise
        assert result is not None
        if result.stale_connection or result.replayed:
            return result
        participation = self._by_participant.get(participant_id)
        if result.room_closed:
            self._unbind_room(room_id)
            await self._closed(room_id)
        else:
            if participation is not None:
                self._unbind(participation)
            await self._room_changed(
                "room.participant_left",
                result,
                room_id=room_id,
                payload={"participant_id": participant_id},
            )
            await self._owner_changed(result, room_id)
        await self._lobby_changed("DISCONNECT_EXPIRED", room_id)
        if result.vote_removed:
            await self._vote_tally_changed(
                room_id,
                f"vote-expire:{participant_id}:{connection_generation}",
            )
        return result

    async def change_identity(
        self,
        previous: SessionRecord,
        replacement: CreateSession,
    ) -> None:
        participation = self.participation(previous.session_digest)
        if participation is None and self._participation_resolver is not None:
            participation = await self._participation_resolver.by_transition_source(previous)
        if participation is None:
            return
        if replacement.actor_type is not SessionActorType.MEMBER:
            raise RoomRuleViolation("ACTIVE_ROOM_IDENTITY_CHANGE_NOT_ALLOWED")
        if (
            previous.actor_type is SessionActorType.MEMBER
            and replacement.actor_type is SessionActorType.MEMBER
            and previous.actor_id != replacement.actor_id
        ):
            raise RoomRuleViolation("ACTIVE_ROOM_MEMBER_CHANGE_NOT_ALLOWED")
        snapshot = await self._runtime.get(participation.room_id)
        if snapshot is None:
            self._unbind(participation)
            return
        if (
            self._participation_resolver is None
            and self._by_participant.get(participation.participant_id) != participation
        ):
            # Participation can end during a session rotation. Login may still
            # succeed, but must not restore the removed Room binding.
            if self._by_participant.get(participation.participant_id) is None:
                return
            raise SessionTransitionUnavailable
        if previous.actor_type is SessionActorType.GUEST:
            try:
                await self._runtime.change_identity(
                    ChangeRoomIdentity(
                        room_id=participation.room_id,
                        request_id=str(uuid4()),
                        participant_id=participation.participant_id,
                        actor_type=ActorType.MEMBER,
                        session_digest=replacement.session_digest,
                        expected_state_version=snapshot.state_version,
                    )
                )
            except Exception as error:
                if self._participation_resolver is not None:
                    try:
                        applied = await self._participation_resolver.identity_transition_applied(
                            previous,
                            replacement,
                            participation.participant_id,
                        )
                    except Exception as confirmation_error:
                        raise SessionTransitionUnavailable from confirmation_error
                    if applied is True:
                        pass
                    elif applied is None:
                        raise SessionTransitionUnavailable from error
                    else:
                        raise
                elif not isinstance(error, RoomRuleViolation):
                    raise
                current = await self._runtime.get(participation.room_id)
                if self._participation_resolver is None:
                    if current is None or not any(
                        item.participant_id == participation.participant_id
                        for item in current.participants
                    ):
                        return
                    raise
        if (
            self._participation_resolver is None
            and self._by_participant.get(participation.participant_id) != participation
        ):
            if self._by_participant.get(participation.participant_id) is None:
                return
            raise SessionTransitionUnavailable
        updated = RoomParticipation(
            session_digest=replacement.session_digest,
            room_id=participation.room_id,
            participant_id=participation.participant_id,
            actor_type=replacement.actor_type,
            actor_id=replacement.actor_id,
            connection_generation=participation.connection_generation,
            connected=participation.connected,
        )
        self._unbind(participation)
        self._invalidate_participation_watch(updated.session_digest)
        self._by_session[updated.session_digest] = updated
        self._by_participant[updated.participant_id] = updated
        try:
            refreshed = await self._runtime.get(updated.room_id)
            if refreshed is not None:
                await self._room_changed(
                    "snapshot.required",
                    RoomMutationResult(snapshot=refreshed),
                    room_id=updated.room_id,
                    payload={"reason": "PARTICIPANT_IDENTITY_CHANGED"},
                )
        except Exception:
            # The Session and Room authorities already agree. Realtime is
            # advisory here; clients recover through the shared snapshot.
            _LOGGER.error("Room identity notification could not be confirmed")

    async def leave(self, current: SessionRecord) -> None:
        participation = await self.resolve_participation(current.session_digest)
        if participation is None:
            return
        snapshot = await self._runtime.get(participation.room_id)
        if snapshot is None:
            self._unbind(participation)
            return
        try:
            await self.leave_room(
                session=current,
                request_id=str(uuid4()),
                expected_state_version=snapshot.state_version,
            )
        except RoomRuleViolation as error:
            raise SessionTransitionUnavailable from error

    def _bind(self, session: SessionRecord, room_id: str, participant_id: str) -> None:
        self._invalidate_participation_watch(session.session_digest)
        participation = RoomParticipation(
            session_digest=session.session_digest,
            room_id=room_id,
            participant_id=participant_id,
            actor_type=session.actor_type,
            actor_id=session.actor_id,
        )
        self._by_session[session.session_digest] = participation
        self._by_participant[participant_id] = participation

    def _update_local_connection(
        self,
        participation: RoomParticipation,
        *,
        generation: int,
        connected: bool,
    ) -> None:
        if self._participation_resolver is not None:
            return
        updated = replace(
            participation,
            connection_generation=generation,
            connected=connected,
        )
        if self._by_session.get(participation.session_digest) == participation:
            self._by_session[participation.session_digest] = updated
        if self._by_participant.get(participation.participant_id) == participation:
            self._by_participant[participation.participant_id] = updated

    def _unbind(self, participation: RoomParticipation) -> None:
        if self._by_session.get(participation.session_digest) == participation:
            self._invalidate_participation_watch(participation.session_digest)
            self._by_session.pop(participation.session_digest, None)
        if self._by_participant.get(participation.participant_id) == participation:
            self._by_participant.pop(participation.participant_id, None)

    def _unbind_room(self, room_id: str) -> None:
        for participation in tuple(self._by_participant.values()):
            if participation.room_id == room_id:
                self._unbind(participation)

    async def _require_not_participating(self, session_digest: str) -> None:
        if await self.resolve_participation(session_digest) is not None:
            raise RoomRuleViolation("SESSION_ALREADY_IN_ROOM")

    async def _require_participation(self, session_digest: str) -> RoomParticipation:
        participation = await self.resolve_participation(session_digest)
        if participation is None:
            raise RoomRuleViolation("SESSION_NOT_IN_ROOM")
        return participation

    def _ids_for(self, session_digest: str, operation: str, request_id: str) -> tuple[str, str]:
        seed = f"{session_digest}\n{operation}\n{request_id}"
        return _stable_uuid4(seed + "\nroom"), _stable_uuid4(seed + "\nparticipant")

    def _replay(
        self,
        key: tuple[str, str, str],
        fingerprint: str,
    ) -> RoomMutationResult | None:
        cached = self._results.get(key)
        if cached is None:
            return None
        cached_fingerprint, result = cached
        if cached_fingerprint != fingerprint:
            raise RoomRuleViolation("REQUEST_ID_CONFLICT")
        return RoomMutationResult(
            snapshot=result.snapshot,
            replayed=True,
            connection_generation=result.connection_generation,
            disconnect_expires_at_ms=result.disconnect_expires_at_ms,
            stale_connection=result.stale_connection,
            vote_removed=result.vote_removed,
            departure=result.departure,
            start_roster=result.start_roster,
            operation_at_ms=result.operation_at_ms,
        )

    async def _room_changed(
        self,
        event_type: str,
        result: RoomMutationResult,
        *,
        room_id: str,
        payload: dict[str, object],
        game_id: str | None = None,
    ) -> None:
        if result.replayed or result.stale_connection or result.snapshot is None:
            return
        event_payload = dict(payload)
        event_payload["room_state_version"] = result.snapshot.state_version
        game = await self._game_runtime(room_id)
        if game is not None:
            event_payload["game_state_version"] = game.state_version
        try:
            await self._events.room_changed(
                event_type=event_type,
                room_id=room_id,
                event_key=(
                    f"{event_type}:{result.snapshot.state_version}:"
                    f"{None if game is None else game.state_version}"
                ),
                payload=event_payload,
                game_id=game_id,
            )
        except Exception:
            _LOGGER.exception(
                "Room realtime event delivery failed: event_type=%s room_id=%s",
                event_type,
                room_id,
            )
            return

    async def _owner_changed(self, result: RoomMutationResult, room_id: str) -> None:
        departure = result.departure
        if (
            departure is None
            or departure.previous_owner_id == departure.new_owner_id
            or result.snapshot is None
        ):
            return
        await self._room_changed(
            "room.owner_changed",
            result,
            room_id=room_id,
            payload={
                "previous_owner_id": departure.previous_owner_id,
                "owner_id": departure.new_owner_id,
                "ready_reset": True,
            },
        )

    async def _closed(self, room_id: str) -> None:
        try:
            await self._events.room_changed(
                event_type="room.closed",
                room_id=room_id,
                payload={"action": "RETURN_TO_LOBBY"},
            )
        except Exception:
            _LOGGER.exception("Room close event delivery failed: room_id=%s", room_id)
            return

    async def _lobby_changed(
        self,
        reason: str,
        room_id: str,
        *,
        event_key: str | None = None,
    ) -> None:
        try:
            await self._events.lobby_rooms_changed(
                {"reason": reason, "room_id": room_id},
                event_key=event_key,
            )
        except Exception:
            _LOGGER.exception(
                "Lobby event delivery failed: reason=%s room_id=%s",
                reason,
                room_id,
            )
            return

    async def _active_vote_turn(self, room_id: str) -> int | None:
        game = await self._game_runtime(room_id)
        if game is None or game.turn_status is not TurnStatus.VOTING:
            return None
        return game.turn_no

    async def _game_runtime(self, room_id: str) -> VoteRuntimeSnapshot | None:
        if self._votes is None:
            return None
        return await self._votes.get(room_id)

    async def _vote_tally_changed(self, room_id: str, event_key: str) -> None:
        game = await self._game_runtime(room_id)
        if game is None:
            return
        valid_voter_count = sum(
            item.connected
            and item.role is VoteParticipantRole.PLAYER
            and item.team is game.current_team
            for item in game.participants
        )
        try:
            await self._events.room_changed(
                event_type="vote.tally_changed",
                event_key=event_key,
                room_id=room_id,
                game_id=game.game_id,
                turn_no=game.turn_no,
                payload={
                    "game_state_version": game.state_version,
                    "tally": [
                        {"coordinate": item.coordinate.canonical, "count": item.count}
                        for item in game.tally
                    ],
                    "valid_voter_count": valid_voter_count,
                },
            )
        except Exception:
            _LOGGER.exception(
                "Vote tally realtime event delivery failed: room_id=%s game_id=%s",
                room_id,
                game.game_id,
            )


def _room_actor_type(actor_type: SessionActorType) -> ActorType:
    return ActorType(actor_type.value)


def _fingerprint(*values: object) -> str:
    return hashlib.sha256(repr(values).encode(), usedforsecurity=False).hexdigest()


def _stable_uuid4(seed: str) -> str:
    """Derive a canonical UUIDv4-shaped id from an idempotency seed."""

    value = bytearray(hashlib.sha256(seed.encode("utf-8"), usedforsecurity=False).digest()[:16])
    value[6] = (value[6] & 0x0F) | 0x40
    value[8] = (value[8] & 0x3F) | 0x80
    return str(UUID(bytes=bytes(value)))
