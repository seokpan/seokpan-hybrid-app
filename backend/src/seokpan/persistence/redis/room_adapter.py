"""redis.asyncio-backed Room runtime adapter."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from redis.exceptions import RedisError

from seokpan.persistence.redis.common import (
    LuaScriptRunner,
    RedisClient,
    RedisKeyspace,
    RedisProviderError,
    VersionedJsonCodec,
)
from seokpan.persistence.redis.room_scripts import (
    ROOM_INVALIDATION_ACK,
    ROOM_MUTATION,
    ROOM_PRIVATE_HASH_READ,
    ROOM_READ,
)
from seokpan.persistence.redis.start_capture_script import (
    ROOM_START_CAPTURE,
    start_intent_key,
    start_phase_key,
)
from seokpan.room.application.runtime import (
    ROOM_CLOSED_TOMBSTONE_TTL_MS,
    ROOM_DISCONNECT_LEASE_MS,
    ROOM_REQUEST_DEDUPE_TTL_MS,
    ROOM_RUNTIME_SCHEMA_VERSION,
    ChangeRoomIdentity,
    ChangeRoomTeam,
    ChangeRoomVoteSeconds,
    CompleteRoomGame,
    ConnectRoomParticipant,
    CreateRoomRuntime,
    DisconnectRoomParticipant,
    DueRoomDisconnect,
    ExpireRoomDisconnect,
    JoinRoomRuntime,
    KickRoomParticipant,
    LeaveRoomRuntime,
    PendingGameInvalidation,
    RoomMutationResult,
    RoomRuntimeParticipant,
    RoomRuntimeSnapshot,
    RoomSessionBinding,
    SetRoomReady,
    StartRoomGame,
    validate_room_id,
)
from seokpan.room.application.start_capture import (
    CaptureRoomGameStart,
    validate_intent_lookup,
)
from seokpan.room.application.start_intent import RoomGameStartIntent
from seokpan.room.domain import (
    ActorType,
    DepartureResult,
    GameTermination,
    ParticipantRole,
    RoomConfig,
    RoomRuleViolation,
    RoomStatus,
    RoomVisibility,
    RosterEntry,
    StartRoster,
    Team,
)


class RedisRoomRuntimeAdapter:
    def __init__(self, client: RedisClient) -> None:
        self._client = client
        self._scripts = LuaScriptRunner(client)

    async def list_rooms(self) -> tuple[RoomRuntimeSnapshot, ...]:
        pattern = f"{RedisKeyspace.room_meta('*')}"
        prefix = "stone:v1:room:{"
        suffix = "}:meta"
        room_ids: set[str] = set()
        try:
            async for raw_id in self._client.scan_iter(match=pattern, count=100):
                try:
                    key = raw_id.decode("utf-8") if isinstance(raw_id, bytes) else raw_id
                except UnicodeDecodeError:
                    raise RedisProviderError("REDIS_RESPONSE_INVALID") from None
                if (
                    not isinstance(key, str)
                    or not key.startswith(prefix)
                    or not key.endswith(suffix)
                ):
                    raise RedisProviderError("REDIS_RESPONSE_INVALID")
                room_id = key[len(prefix) : -len(suffix)]
                try:
                    validate_room_id(room_id)
                except RoomRuleViolation:
                    raise RedisProviderError("REDIS_RESPONSE_INVALID") from None
                room_ids.add(room_id)
        except RedisError as error:
            raise RedisProviderError() from error
        rooms: list[RoomRuntimeSnapshot | None] = []
        for room_id in sorted(room_ids):
            rooms.append(await self.get(room_id))
        return tuple(room for room in rooms if room is not None)

    async def find_by_session(self, session_digest: str) -> RoomSessionBinding | None:
        return await self._find_binding(session_digest=session_digest)

    async def find_by_participant(self, participant_id: str) -> RoomSessionBinding | None:
        return await self._find_binding(participant_id=participant_id)

    async def due_disconnects(self, *, now_ms: int, limit: int) -> tuple[DueRoomDisconnect, ...]:
        if limit < 1:
            raise ValueError("INVALID_DUE_DISCONNECT_LIMIT")
        due: list[DueRoomDisconnect] = []
        pattern = RedisKeyspace.room_connections("*")
        prefix, suffix = "stone:v1:room:{", "}:connections"
        try:
            async for raw_key in self._client.scan_iter(match=pattern, count=100):
                key = raw_key.decode("utf-8") if isinstance(raw_key, bytes) else raw_key
                if (
                    not isinstance(key, str)
                    or not key.startswith(prefix)
                    or not key.endswith(suffix)
                ):
                    raise RedisProviderError("REDIS_RESPONSE_INVALID")
                room_id = key[len(prefix) : -len(suffix)]
                validate_room_id(room_id)
                for raw_participant, raw_connection in (await self._client.hgetall(key)).items():
                    participant = (
                        raw_participant.decode("utf-8")
                        if isinstance(raw_participant, bytes)
                        else raw_participant
                    )
                    if not isinstance(participant, str):
                        raise RedisProviderError("REDIS_RESPONSE_INVALID")
                    connection = VersionedJsonCodec.decode(raw_connection)
                    connected = _boolean(connection, "connected")
                    expires = _optional_integer(connection, "disconnect_expires_at_ms")
                    generation = _integer(connection, "generation")
                    if not connected and expires is not None and expires <= now_ms:
                        due.append(DueRoomDisconnect(room_id, participant, generation, expires))
        except RedisError as error:
            raise RedisProviderError() from error
        except (UnicodeDecodeError, RoomRuleViolation) as error:
            raise RedisProviderError("REDIS_RESPONSE_INVALID") from error
        return tuple(
            sorted(
                due,
                key=lambda item: (item.expires_at_ms, item.room_id, item.participant_id),
            )[:limit]
        )

    async def _find_binding(
        self,
        *,
        session_digest: str | None = None,
        participant_id: str | None = None,
    ) -> RoomSessionBinding | None:
        matches: list[RoomSessionBinding] = []
        pattern = RedisKeyspace.room_connections("*")
        prefix, suffix = "stone:v1:room:{", "}:connections"
        try:
            async for raw_key in self._client.scan_iter(match=pattern, count=100):
                key = raw_key.decode("utf-8") if isinstance(raw_key, bytes) else raw_key
                if (
                    not isinstance(key, str)
                    or not key.startswith(prefix)
                    or not key.endswith(suffix)
                ):
                    raise RedisProviderError("REDIS_RESPONSE_INVALID")
                room_id = key[len(prefix) : -len(suffix)]
                validate_room_id(room_id)
                connections = await self._client.hgetall(key)
                for raw_participant, raw_connection in connections.items():
                    found_participant = (
                        raw_participant.decode("utf-8")
                        if isinstance(raw_participant, bytes)
                        else raw_participant
                    )
                    if not isinstance(found_participant, str):
                        raise RedisProviderError("REDIS_RESPONSE_INVALID")
                    connection = VersionedJsonCodec.decode(raw_connection)
                    found_session = _string(connection, "session_digest")
                    if (session_digest is not None and found_session != session_digest) or (
                        participant_id is not None and found_participant != participant_id
                    ):
                        continue
                    snapshot = await self.get(room_id)
                    if snapshot is None:
                        continue
                    participant = next(
                        (
                            item
                            for item in snapshot.participants
                            if item.participant_id == found_participant
                        ),
                        None,
                    )
                    if participant is None:
                        raise RedisProviderError("REDIS_RESPONSE_INVALID")
                    matches.append(
                        RoomSessionBinding(
                            room_id,
                            found_participant,
                            found_session,
                            participant.actor_type,
                            _integer(connection, "generation"),
                            _boolean(connection, "connected"),
                        )
                    )
        except (RedisError, UnicodeDecodeError, RoomRuleViolation) as error:
            raise RedisProviderError("REDIS_RESPONSE_INVALID") from error
        if len(matches) > 1:
            raise RedisProviderError("ROOM_PARTICIPATION_AMBIGUOUS")
        return None if not matches else matches[0]

    async def create(self, command: CreateRoomRuntime) -> RoomMutationResult:
        return await self._mutate(
            command.room_id,
            command.request_id,
            "create",
            {
                "schema_version": ROOM_RUNTIME_SCHEMA_VERSION,
                "name": command.config.name,
                "visibility": command.config.visibility.value,
                "encoded_password": command.encoded_password or "",
                "max_participants": command.config.max_participants,
                "minimum_ready": command.config.minimum_ready,
                "vote_seconds": command.config.vote_seconds,
                "owner_id": command.owner_id,
                "session_digest": command.owner_session_digest,
            },
        )

    async def get(self, room_id: str) -> RoomRuntimeSnapshot | None:
        validate_room_id(room_id)
        result = await self._scripts.execute(
            ROOM_READ,
            keys=self._read_keys(room_id),
            args=(room_id, ROOM_RUNTIME_SCHEMA_VERSION),
        )
        decoded = self._result(result)
        self._raise_rejection(decoded)
        return self._optional_snapshot(decoded.get("snapshot"))

    async def pending_game_invalidations(
        self,
        *,
        limit: int,
    ) -> tuple[PendingGameInvalidation, ...]:
        if limit < 1:
            raise ValueError("INVALID_GAME_INVALIDATION_LIMIT")
        pattern = RedisKeyspace.room_closed("*")
        prefix = "stone:v1:room:{"
        suffix = "}:closed"
        pending: list[PendingGameInvalidation] = []
        try:
            async for raw_key in self._client.scan_iter(match=pattern, count=100):
                key = raw_key.decode("utf-8") if isinstance(raw_key, bytes) else raw_key
                if (
                    not isinstance(key, str)
                    or not key.startswith(prefix)
                    or not key.endswith(suffix)
                ):
                    raise RedisProviderError("REDIS_RESPONSE_INVALID")
                raw = await self._client.get(key)
                if raw is None:
                    continue
                value = VersionedJsonCodec.decode(raw)
                if value.get("invalidation_pending") is not True:
                    continue
                room_id = value.get("room_id")
                game_id = value.get("terminated_game_id")
                closed_at_ms = value.get("closed_at_ms")
                if (
                    not isinstance(room_id, str)
                    or not isinstance(game_id, str)
                    or type(closed_at_ms) is not int
                ):
                    raise RedisProviderError("REDIS_RESPONSE_INVALID")
                validate_room_id(room_id)
                pending.append(PendingGameInvalidation(room_id, game_id, closed_at_ms))
                if len(pending) >= limit:
                    break
        except RedisProviderError:
            raise
        except (RedisError, UnicodeDecodeError) as error:
            raise RedisProviderError() from error
        return tuple(sorted(pending, key=lambda item: (item.closed_at_ms, item.room_id)))

    async def complete_game_invalidation(self, room_id: str, game_id: str) -> None:
        validate_room_id(room_id)
        result = await self._scripts.execute(
            ROOM_INVALIDATION_ACK,
            keys=(RedisKeyspace.room_closed(room_id),),
            args=(game_id, ROOM_REQUEST_DEDUPE_TTL_MS),
        )
        decoded = self._result(result)
        self._raise_rejection(decoded)

    async def get_private_access_hash(self, room_id: str) -> str | None:
        validate_room_id(room_id)
        result = await self._scripts.execute(
            ROOM_PRIVATE_HASH_READ,
            keys=(RedisKeyspace.room_meta(room_id),),
            args=(),
        )
        decoded = self._result(result)
        self._raise_rejection(decoded)
        value = decoded.get("encoded_password")
        if value is None:
            return None
        if not isinstance(value, str) or not value.startswith("$argon2id$"):
            raise RedisProviderError("REDIS_RESPONSE_INVALID")
        return value

    async def join(self, command: JoinRoomRuntime) -> RoomMutationResult:
        return await self._mutate(
            command.room_id,
            command.request_id,
            "join",
            {
                "participant_id": command.participant_id,
                "actor_type": command.actor_type.value,
                "session_digest": command.session_digest,
                "expected_state_version": command.expected_state_version,
                "private_access_verified": command.private_access_verified,
            },
        )

    async def change_team(self, command: ChangeRoomTeam) -> RoomMutationResult:
        return await self._mutate(
            command.room_id,
            command.request_id,
            "change_team",
            {
                "participant_id": command.participant_id,
                "team": command.team.value,
                "expected_state_version": command.expected_state_version,
            },
        )

    async def change_identity(self, command: ChangeRoomIdentity) -> RoomMutationResult:
        return await self._mutate(
            command.room_id,
            command.request_id,
            "change_identity",
            {
                "participant_id": command.participant_id,
                "actor_type": command.actor_type.value,
                "session_digest": command.session_digest,
                "expected_state_version": command.expected_state_version,
            },
        )

    async def set_ready(self, command: SetRoomReady) -> RoomMutationResult:
        return await self._mutate(
            command.room_id,
            command.request_id,
            "set_ready",
            {
                "participant_id": command.participant_id,
                "ready": command.ready,
                "expected_state_version": command.expected_state_version,
            },
        )

    async def change_vote_seconds(self, command: ChangeRoomVoteSeconds) -> RoomMutationResult:
        return await self._mutate(
            command.room_id,
            command.request_id,
            "change_vote_seconds",
            {
                "actor_id": command.actor_id,
                "vote_seconds": command.vote_seconds,
                "expected_state_version": command.expected_state_version,
            },
        )

    async def get_start_intent(self, room_id: str, game_id: str) -> RoomGameStartIntent | None:
        validate_intent_lookup(room_id, game_id)
        try:
            raw = await self._client.get(start_intent_key(room_id, game_id))
        except RedisError as error:
            raise RedisProviderError() from error
        if raw is None:
            return None
        try:
            text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            intent = RoomGameStartIntent.from_json(text)
        except ValueError as error:
            raise RedisProviderError("START_INTENT_INVALID") from error
        if intent.room_id != room_id or intent.game_id != game_id:
            raise RedisProviderError("START_INTENT_INVALID")
        return intent

    async def start_game(self, command: StartRoomGame) -> RoomMutationResult:
        if isinstance(command, CaptureRoomGameStart):
            result = await self._scripts.execute(
                ROOM_START_CAPTURE,
                keys=(
                    *self._read_keys(command.room_id),
                    RedisKeyspace.room_requests(command.room_id),
                    RedisKeyspace.room_request_expiries(command.room_id),
                    RedisKeyspace.room_closed(command.room_id),
                    start_intent_key(command.room_id, command.game_id),
                    start_phase_key(command.room_id, command.game_id),
                ),
                args=(
                    command.room_id,
                    command.request_id,
                    command.game_id,
                    command.actor_id,
                    command.expected_state_version,
                    command.players_json(),
                    ROOM_REQUEST_DEDUPE_TTL_MS,
                    ROOM_RUNTIME_SCHEMA_VERSION,
                ),
            )
            decoded = self._result(result)
            self._raise_rejection(decoded)
            return self._mutation_result(decoded)
        return await self._mutate(
            command.room_id,
            command.request_id,
            "start_game",
            {
                "actor_id": command.actor_id,
                "game_id": command.game_id,
                "expected_state_version": command.expected_state_version,
            },
        )

    async def complete_game(self, command: CompleteRoomGame) -> RoomMutationResult:
        return await self._mutate(
            command.room_id,
            command.request_id,
            "complete_game",
            {
                "game_id": command.game_id,
                "final_turn_no": command.final_turn_no,
                "expected_state_version": command.expected_state_version,
            },
        )

    async def connect(self, command: ConnectRoomParticipant) -> RoomMutationResult:
        return await self._mutate(
            command.room_id,
            command.request_id,
            "connect",
            {
                "participant_id": command.participant_id,
                "session_digest": command.session_digest,
                "expected_state_version": command.expected_state_version,
            },
        )

    async def disconnect(self, command: DisconnectRoomParticipant) -> RoomMutationResult:
        return await self._mutate(
            command.room_id,
            command.request_id,
            "disconnect",
            {
                "participant_id": command.participant_id,
                "connection_generation": command.connection_generation,
                "expected_state_version": command.expected_state_version,
                "active_vote_turn": command.active_vote_turn,
            },
            active_vote_turn=command.active_vote_turn,
        )

    async def expire_disconnect(self, command: ExpireRoomDisconnect) -> RoomMutationResult:
        return await self._mutate(
            command.room_id,
            command.request_id,
            "expire_disconnect",
            {
                "participant_id": command.participant_id,
                "connection_generation": command.connection_generation,
                "expected_state_version": command.expected_state_version,
                "active_vote_turn": command.active_vote_turn,
            },
            active_vote_turn=command.active_vote_turn,
        )

    async def kick(self, command: KickRoomParticipant) -> RoomMutationResult:
        return await self._mutate(
            command.room_id,
            command.request_id,
            "kick",
            {
                "actor_id": command.actor_id,
                "target_id": command.target_id,
                "expected_state_version": command.expected_state_version,
            },
        )

    async def leave(self, command: LeaveRoomRuntime) -> RoomMutationResult:
        return await self._mutate(
            command.room_id,
            command.request_id,
            "leave",
            {
                "participant_id": command.participant_id,
                "expected_state_version": command.expected_state_version,
                "active_vote_turn": command.active_vote_turn,
            },
            active_vote_turn=command.active_vote_turn,
        )

    async def _mutate(
        self,
        room_id: str,
        request_id: str,
        operation: str,
        payload: Mapping[str, object],
        *,
        active_vote_turn: int | None = None,
    ) -> RoomMutationResult:
        result = await self._scripts.execute(
            ROOM_MUTATION,
            keys=self._mutation_keys(room_id, active_vote_turn),
            args=(
                room_id,
                operation,
                request_id,
                ROOM_REQUEST_DEDUPE_TTL_MS,
                ROOM_DISCONNECT_LEASE_MS,
                ROOM_CLOSED_TOMBSTONE_TTL_MS,
                VersionedJsonCodec.encode(
                    {**payload, "schema_version": ROOM_RUNTIME_SCHEMA_VERSION}
                ),
            ),
        )
        decoded = self._result(result)
        self._raise_rejection(decoded)
        return self._mutation_result(decoded)

    @staticmethod
    def _read_keys(room_id: str) -> tuple[str, ...]:
        return (
            RedisKeyspace.room_meta(room_id),
            RedisKeyspace.room_participants(room_id),
            RedisKeyspace.room_ready(room_id),
            RedisKeyspace.room_connections(room_id),
        )

    @classmethod
    def _mutation_keys(cls, room_id: str, active_vote_turn: int | None) -> tuple[str, ...]:
        return (
            *cls._read_keys(room_id),
            RedisKeyspace.room_requests(room_id),
            RedisKeyspace.room_request_expiries(room_id),
            RedisKeyspace.room_closed(room_id),
            RedisKeyspace.room_votes(room_id, active_vote_turn),
            RedisKeyspace.room_vote_tally(room_id, active_vote_turn),
            RedisKeyspace.room_game(room_id),
        )

    @staticmethod
    def _result(result: object) -> dict[str, object]:
        if not isinstance(result, (bytes, str)):
            raise RedisProviderError("REDIS_RESPONSE_INVALID")
        decoded = VersionedJsonCodec.decode(result)
        if not isinstance(decoded.get("ok"), bool):
            raise RedisProviderError("REDIS_RESPONSE_INVALID")
        return decoded

    @staticmethod
    def _raise_rejection(result: dict[str, object]) -> None:
        if result["ok"] is True:
            return
        error = result.get("error")
        if not isinstance(error, str):
            raise RedisProviderError("REDIS_RESPONSE_INVALID")
        if error == "ROOM_SCHEMA_VERSION_MISMATCH":
            raise RedisProviderError(error)
        raise RoomRuleViolation(error)

    @classmethod
    def _mutation_result(cls, value: dict[str, object]) -> RoomMutationResult:
        return RoomMutationResult(
            snapshot=cls._optional_snapshot(value.get("snapshot")),
            replayed=_optional_bool(value, "replayed", False),
            connection_generation=_optional_integer(value, "connection_generation"),
            disconnect_expires_at_ms=_optional_integer(value, "disconnect_expires_at_ms"),
            stale_connection=_optional_bool(value, "stale_connection", False),
            vote_removed=_optional_bool(value, "vote_removed", False),
            departure=cls._optional_departure(value.get("departure")),
            start_roster=cls._optional_roster(value.get("start_roster")),
            operation_at_ms=_optional_integer(value, "operation_at_ms"),
        )

    @classmethod
    def _optional_snapshot(cls, value: object) -> RoomRuntimeSnapshot | None:
        if value is None:
            return None
        snapshot = _mapping(value)
        if _integer(snapshot, "schema_version") != ROOM_RUNTIME_SCHEMA_VERSION:
            raise RedisProviderError("ROOM_SCHEMA_VERSION_MISMATCH")
        last_game_id = _optional_string(snapshot, "last_game_id")
        last_game_turn_no = _optional_integer(snapshot, "last_game_turn_no")
        if (last_game_id is None) != (last_game_turn_no is None) or (
            last_game_turn_no is not None and last_game_turn_no < 1
        ):
            raise RedisProviderError("REDIS_RESPONSE_INVALID")
        config_value = _mapping(snapshot["config"])
        participants_value = _list(snapshot["participants"])
        return RoomRuntimeSnapshot(
            room_id=_string(snapshot, "room_id"),
            config=RoomConfig(
                name=_string(config_value, "name"),
                visibility=RoomVisibility(_string(config_value, "visibility")),
                max_participants=_integer(config_value, "max_participants"),
                minimum_ready=_integer(config_value, "minimum_ready"),
                vote_seconds=_integer(config_value, "vote_seconds"),
            ),
            status=RoomStatus(_string(snapshot, "status")),
            owner_id=_optional_string(snapshot, "owner_id"),
            state_version=_integer(snapshot, "state_version"),
            participants=tuple(cls._participant(item) for item in participants_value),
            game_id=_optional_string(snapshot, "game_id"),
            schema_version=_integer(snapshot, "schema_version"),
            last_game_id=last_game_id,
            last_game_turn_no=last_game_turn_no,
        )

    @staticmethod
    def _optional_roster(value: object) -> StartRoster | None:
        if value is None:
            return None
        return StartRoster(
            entries=tuple(
                RosterEntry(
                    participant_id=_string(_mapping(item), "participant_id"),
                    team=Team(_string(_mapping(item), "team")),
                    role=ParticipantRole(_string(_mapping(item), "role")),
                )
                for item in _list(value)
            )
        )

    @staticmethod
    def _participant(value: object) -> RoomRuntimeParticipant:
        item = _mapping(value)
        return RoomRuntimeParticipant(
            participant_id=_string(item, "participant_id"),
            actor_type=ActorType(_string(item, "actor_type")),
            joined_order=_integer(item, "joined_order"),
            connected=_boolean(item, "connected"),
            team=Team(_string(item, "team")),
            ready=_boolean(item, "ready"),
        )

    @staticmethod
    def _optional_departure(value: object) -> DepartureResult | None:
        if value is None:
            return None
        item = _mapping(value)
        return DepartureResult(
            previous_owner_id=_optional_string(item, "previous_owner_id"),
            new_owner_id=_optional_string(item, "new_owner_id"),
            room_closed=_boolean(item, "room_closed"),
            game_termination=GameTermination(_string(item, "game_termination")),
            terminated_game_id=_optional_string(item, "terminated_game_id"),
        )


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RedisProviderError("REDIS_RESPONSE_INVALID")
    return cast(dict[str, object], value)


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise RedisProviderError("REDIS_RESPONSE_INVALID")
    return cast(list[object], value)


def _string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise RedisProviderError("REDIS_RESPONSE_INVALID")
    return item


def _optional_string(value: Mapping[str, object], key: str) -> str | None:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str):
        raise RedisProviderError("REDIS_RESPONSE_INVALID")
    return item


def _integer(value: Mapping[str, object], key: str) -> int:
    item = value.get(key)
    if type(item) is not int:
        raise RedisProviderError("REDIS_RESPONSE_INVALID")
    return item


def _optional_integer(value: Mapping[str, object], key: str) -> int | None:
    item = value.get(key)
    if item is None:
        return None
    if type(item) is not int:
        raise RedisProviderError("REDIS_RESPONSE_INVALID")
    return item


def _boolean(value: Mapping[str, object], key: str) -> bool:
    item = value.get(key)
    if not isinstance(item, bool):
        raise RedisProviderError("REDIS_RESPONSE_INVALID")
    return item


def _optional_bool(value: Mapping[str, object], key: str, default: bool) -> bool:
    item = value.get(key, default)
    if not isinstance(item, bool):
        raise RedisProviderError("REDIS_RESPONSE_INVALID")
    return item
