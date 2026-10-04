"""Application use cases joining Room, Game persistence, and Vote runtime."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from seokpan.game.application.history import CompletedGameReplay, replay_completed_game
from seokpan.game.application.persistence import (
    GameParticipantRecord,
    GamePersistencePort,
    PersistenceOutcome,
    PersistenceRuleViolation,
    StartGameCommand,
    StoredGameResult,
)
from seokpan.game.domain import (
    Coordinate,
    EndReason,
    GameParticipantRole,
    GameStatus,
    RatingAdjustment,
    Stone,
)
from seokpan.identity.application import SessionActorType, SessionRecord
from seokpan.room.application import (
    NullRealtimeEventAdapter,
    RealtimeEventPort,
    RoomApplicationService,
    RoomParticipation,
    RoomRuntimeSnapshot,
)
from seokpan.room.domain import ParticipantRole as RoomParticipantRole
from seokpan.room.domain import RoomRuleViolation, RoomStatus, Team
from seokpan.vote.application import (
    CastRuntimeVote,
    InitializeVoteRuntime,
    RemoveRuntimeVote,
    VoteRuntimePort,
    VoteRuntimeSnapshot,
)
from seokpan.vote.domain import ParticipantRole as VoteParticipantRole
from seokpan.vote.domain import Voter, VoteRuleViolation

if TYPE_CHECKING:
    from seokpan.game.application.captured_startup import CapturedGameStartup

_LOGGER = logging.getLogger(__name__)


class MillisecondClock(Protocol):
    @property
    def now_ms(self) -> int: ...


@dataclass(frozen=True, slots=True)
class GameApplicationSnapshot:
    room: RoomRuntimeSnapshot
    game: VoteRuntimeSnapshot
    viewer_participant_id: str
    now_ms: int
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class GameResultSnapshot:
    result: StoredGameResult
    board: CompletedGameReplay
    turn_no: int
    my_rating: RatingAdjustment | None


class GameApplicationService:
    """Coordinate retry-safe Room, persistence, and Vote-runtime Game startup."""

    def __init__(
        self,
        *,
        rooms: RoomApplicationService,
        games: GamePersistencePort,
        votes: VoteRuntimePort,
        clock: MillisecondClock,
        events: RealtimeEventPort | None = None,
        captured_startup: CapturedGameStartup | None = None,
    ) -> None:
        self._captured_startup = captured_startup
        self._rooms = rooms
        self._games = games
        self._votes = votes
        self._clock = clock
        self._events = events or NullRealtimeEventAdapter()

    async def start_game(
        self,
        *,
        session: SessionRecord,
        room_id: str,
        request_id: str,
        expected_state_version: int,
    ) -> GameApplicationSnapshot:
        if self._captured_startup is not None:
            outcome = await self._captured_startup.start_game(
                session=session,
                room_id=room_id,
                request_id=request_id,
                expected_state_version=expected_state_version,
            )
            if outcome.initialized_now:
                await self._game_started(outcome.snapshot, request_id)
            return outcome.snapshot
        participation = await self._require_participation(session)
        if participation.room_id != room_id:
            raise RoomRuleViolation("SESSION_NOT_IN_ROOM")
        game_id = _stable_uuid4(f"{participation.room_id}\nstart\n{request_id}")

        try:
            room_result = await self._rooms.start_game(
                session=session,
                request_id=request_id,
                game_id=game_id,
                expected_state_version=expected_state_version,
                notify_realtime=False,
            )
        except RoomRuleViolation as error:
            if error.code != "ROOM_NOT_WAITING":
                raise
            recovered = await self._recover_partial_start(
                participation=participation,
                request_id=request_id,
                expected_state_version=expected_state_version,
            )
            if recovered is None:
                raise
            return recovered
        room = room_result.snapshot
        roster = room_result.start_roster
        if room is None or roster is None:
            raise RoomRuleViolation("GAME_START_RESULT_INVALID")
        current_room = await self._rooms.get(room.room_id)
        if current_room is None or current_room.game_id != game_id:
            raise RoomRuleViolation("GAME_NOT_IN_CURRENT_ROOM")
        started_at_ms = room_result.operation_at_ms
        if started_at_ms is None:
            raise RoomRuleViolation("GAME_START_TIME_MISSING")

        player_entries = tuple(
            entry for entry in roster.entries if entry.role is RoomParticipantRole.PLAYER
        )
        persistence_outcome = await self._games.start_game(
            StartGameCommand(
                game_id=game_id,
                room_id=room.room_id,
                voting_time_seconds=room.config.vote_seconds,
                started_at=datetime.fromtimestamp(started_at_ms / 1000, UTC),
                participants=tuple(
                    [
                        await self._persistence_participant(item.participant_id, item.team)
                        for item in player_entries
                    ]
                ),
            )
        )
        vote_result = await self._votes.initialize(
            InitializeVoteRuntime(
                room_id=room.room_id,
                request_id=request_id,
                game_id=game_id,
                participants=tuple(
                    Voter(
                        participant_id=item.participant_id,
                        team=_stone(item.team),
                        role=VoteParticipantRole.PLAYER,
                        connected=_connected(room, item.participant_id),
                    )
                    for item in player_entries
                ),
                deadline_ms=started_at_ms + room.config.vote_seconds * 1000,
                expected_state_version=1,
                previous_game_id=room.last_game_id,
                previous_turn_no=room.last_game_turn_no,
            )
        )
        completed_before = (
            persistence_outcome is PersistenceOutcome.UNCHANGED and vote_result.replayed
        )
        replayed = (
            room_result.replayed
            or persistence_outcome is PersistenceOutcome.UNCHANGED
            or vote_result.replayed
        )
        snapshot = GameApplicationSnapshot(
            room,
            vote_result.snapshot,
            participation.participant_id,
            self._clock.now_ms,
            replayed,
        )
        if not completed_before:
            await self._game_started(snapshot, request_id)
        return snapshot

    async def _recover_partial_start(
        self,
        *,
        participation: RoomParticipation,
        request_id: str,
        expected_state_version: int,
    ) -> GameApplicationSnapshot | None:
        room = await self._rooms.get(participation.room_id)
        if (
            room is None
            or room.status is not RoomStatus.PLAYING
            or room.game_id is None
            or room.owner_id != participation.participant_id
        ):
            return None
        if room.state_version != expected_state_version:
            raise RoomRuleViolation("STATE_VERSION_CONFLICT")

        history = await self._games.load_game(room.game_id)
        runtime = await self._votes.get(room.room_id)
        if runtime is not None:
            if runtime.game_id != room.game_id or history is None:
                raise RoomRuleViolation("GAME_START_RECOVERY_REQUIRED")
            return None

        if history is None:
            ready_players = tuple(
                item
                for item in room.participants
                if item.ready and item.team in {Team.BLACK, Team.WHITE}
            )
            if (
                len(ready_players) < room.config.minimum_ready
                or not any(item.team is Team.BLACK for item in ready_players)
                or not any(item.team is Team.WHITE for item in ready_players)
            ):
                raise RoomRuleViolation("GAME_START_RECOVERY_REQUIRED")
            intended_participants = tuple(
                [
                    await self._persistence_participant(item.participant_id, item.team)
                    for item in ready_players
                ]
            )
            command = StartGameCommand(
                game_id=room.game_id,
                room_id=room.room_id,
                voting_time_seconds=room.config.vote_seconds,
                started_at=datetime.fromtimestamp(self._clock.now_ms / 1000, UTC),
                participants=intended_participants,
            )
            try:
                await self._games.start_game(command)
            except PersistenceRuleViolation as error:
                if error.code != "GAME_START_CONFLICT":
                    raise
            history = await self._games.load_game(room.game_id)
            if history is None:
                raise PersistenceRuleViolation("GAME_NOT_FOUND")
            intended_roster = {
                (item.participant_id, item.team, item.member_id, item.guest_label)
                for item in intended_participants
            }
            persisted_roster = {
                (item.participant_id, item.team, item.member_id, item.guest_label)
                for item in history.start.participants
            }
            if (
                history.start.room_id != room.room_id
                or history.start.voting_time_seconds != room.config.vote_seconds
                or persisted_roster != intended_roster
            ):
                raise PersistenceRuleViolation("GAME_START_CONFLICT")
        if (
            history.start.game_id != room.game_id
            or history.start.room_id != room.room_id
            or history.start.voting_time_seconds != room.config.vote_seconds
        ):
            raise PersistenceRuleViolation("GAME_START_CONFLICT")
        # Missing Redis state is not proof of an unfinished startup. Never reset
        # a played or finalized Game to an empty first turn through this path.
        if history.moves or await self._games.load_result(room.game_id) is not None:
            raise RoomRuleViolation("GAME_START_RECOVERY_REQUIRED")

        persisted_players = tuple(history.participants)
        if not persisted_players:
            raise PersistenceRuleViolation("PARTICIPANTS_REQUIRED")
        latest_room = await self._rooms.get(room.room_id)
        if (
            latest_room is None
            or latest_room.status is not RoomStatus.PLAYING
            or latest_room.game_id is None
            or latest_room.game_id != room.game_id
        ):
            raise RoomRuleViolation("GAME_NOT_IN_CURRENT_ROOM")
        if (
            latest_room.state_version != expected_state_version
            or latest_room.owner_id != participation.participant_id
        ):
            raise RoomRuleViolation("STATE_VERSION_CONFLICT")
        present_ids = {item.participant_id for item in latest_room.participants}
        if any(item.participant_id not in present_ids for item in persisted_players):
            raise RoomRuleViolation("GAME_START_RECOVERY_REQUIRED")
        # This reread detects observed changes; Redis initialize remains the
        # atomic Room/Game guard. It is not a cross-provider transaction.
        game_id = latest_room.game_id
        room = latest_room
        initialize = InitializeVoteRuntime(
            room_id=room.room_id,
            request_id=request_id,
            game_id=game_id,
            participants=tuple(
                Voter(
                    participant_id=item.participant_id,
                    team=item.team,
                    role=VoteParticipantRole.PLAYER,
                    connected=_connected(room, item.participant_id),
                )
                for item in persisted_players
            ),
            deadline_ms=self._clock.now_ms + room.config.vote_seconds * 1000,
            expected_state_version=1,
            previous_game_id=room.last_game_id,
            previous_turn_no=room.last_game_turn_no,
        )
        initialized_now = False
        try:
            vote_result = await self._votes.initialize(initialize)
            runtime = vote_result.snapshot
            initialized_now = not vote_result.replayed
        except VoteRuleViolation as error:
            if error.code != "GAME_RUNTIME_ALREADY_EXISTS":
                raise
            runtime = await self._votes.get(room.room_id)
            if runtime is None or runtime.game_id != room.game_id:
                raise RoomRuleViolation("GAME_START_RECOVERY_REQUIRED") from error
        snapshot = GameApplicationSnapshot(
            room,
            runtime,
            participation.participant_id,
            self._clock.now_ms,
            replayed=True,
        )
        if initialized_now:
            await self._game_started(snapshot, request_id)
        return snapshot

    async def get_game(
        self,
        *,
        session: SessionRecord,
        game_id: str,
    ) -> GameApplicationSnapshot:
        participation, room, runtime = await self._current(session, game_id)
        return GameApplicationSnapshot(
            room, runtime, participation.participant_id, self._clock.now_ms
        )

    async def current_state_reference(
        self,
        session: SessionRecord,
    ) -> tuple[int | None, str | None]:
        participation = await self._require_participation(session)
        room = await self._rooms.get(participation.room_id)
        runtime = await self._votes.get(participation.room_id)
        if room is not None and runtime is not None and runtime.game_id == room.game_id:
            return runtime.state_version, f"/api/v1/games/{runtime.game_id}"
        if room is not None and room.last_game_id is not None:
            return room.state_version, f"/api/v1/games/{room.last_game_id}/result"
        return (None if room is None else room.state_version), None

    async def get_result(self, *, session: SessionRecord, game_id: str) -> GameResultSnapshot:
        participation = await self._require_participation(session)
        room = await self._rooms.get(participation.room_id)
        if room is None:
            raise RoomRuleViolation("ROOM_NOT_FOUND")
        if not any(
            item.participant_id == participation.participant_id for item in room.participants
        ):
            raise RoomRuleViolation("SESSION_NOT_IN_ROOM")
        try:
            history = await self._games.load_game(game_id)
        except PersistenceRuleViolation as error:
            raise PersistenceRuleViolation("GAME_RESULT_INCOMPLETE") from error
        if history is None:
            raise PersistenceRuleViolation("GAME_NOT_FOUND")
        if game_id not in {room.game_id, room.last_game_id}:
            raise RoomRuleViolation("GAME_NOT_IN_CURRENT_ROOM")
        stored = await self._games.load_result(game_id)
        if stored is None:
            if game_id == room.game_id:
                raise PersistenceRuleViolation("GAME_NOT_FINISHED")
            raise PersistenceRuleViolation("GAME_RESULT_INCOMPLETE")
        if stored.room_id != room.room_id or history.start.room_id != room.room_id:
            raise PersistenceRuleViolation("GAME_RESULT_INCOMPLETE")
        if game_id == room.last_game_id:
            final_turn_no = room.last_game_turn_no
        else:
            runtime = await self._votes.get(room.room_id)
            if (
                runtime is None
                or runtime.game_id != game_id
                or runtime.game_status is GameStatus.ACTIVE
            ):
                raise PersistenceRuleViolation("GAME_RESULT_INCOMPLETE")
            final_turn_no = runtime.turn_no
        if final_turn_no is None or final_turn_no < max(
            (item.turn_no for item in history.moves), default=1
        ):
            raise PersistenceRuleViolation("GAME_RESULT_INCOMPLETE")
        try:
            board = replay_completed_game(history, stored.status, stored.end_reason, stored.winner)
        except PersistenceRuleViolation as error:
            raise PersistenceRuleViolation("GAME_RESULT_HISTORY_MISMATCH") from error
        if stored.end_reason in {EndReason.BLACK_WIN, EndReason.WHITE_WIN, EndReason.DRAW} and (
            final_turn_no != history.moves[-1].turn_no
        ):
            raise PersistenceRuleViolation("GAME_RESULT_HISTORY_MISMATCH")
        my_rating = next(
            (
                item
                for item in stored.rating_adjustments
                if session.actor_type is SessionActorType.MEMBER
                and str(item.member_id) == session.actor_id
                and item.participant_id == participation.participant_id
            ),
            None,
        )
        return GameResultSnapshot(stored, board, final_turn_no, my_rating)

    async def cast_vote(
        self,
        *,
        session: SessionRecord,
        game_id: str,
        turn_no: int,
        request_id: str,
        coordinate: str,
        expected_state_version: int,
    ) -> GameApplicationSnapshot:
        participation, room, runtime = await self._current(session, game_id)
        self._require_voter(runtime, participation.participant_id)
        result = await self._votes.cast_vote(
            CastRuntimeVote(
                room_id=room.room_id,
                request_id=request_id,
                game_id=game_id,
                turn_no=turn_no,
                participant_id=participation.participant_id,
                coordinate=Coordinate.parse(coordinate),
                expected_state_version=expected_state_version,
            )
        )
        snapshot = GameApplicationSnapshot(
            room,
            result.snapshot,
            participation.participant_id,
            self._clock.now_ms,
            result.replayed,
        )
        if not result.replayed and result.snapshot.state_version != runtime.state_version:
            await self._vote_tally_changed(snapshot, f"vote-cast:{request_id}")
        return snapshot

    async def remove_vote(
        self,
        *,
        session: SessionRecord,
        game_id: str,
        turn_no: int,
        request_id: str,
        expected_state_version: int,
    ) -> GameApplicationSnapshot:
        participation, room, runtime = await self._current(session, game_id)
        self._require_voter(runtime, participation.participant_id)
        result = await self._votes.remove_vote(
            RemoveRuntimeVote(
                room_id=room.room_id,
                request_id=request_id,
                game_id=game_id,
                turn_no=turn_no,
                participant_id=participation.participant_id,
                expected_state_version=expected_state_version,
            )
        )
        snapshot = GameApplicationSnapshot(
            room,
            result.snapshot,
            participation.participant_id,
            self._clock.now_ms,
            result.replayed,
        )
        if not result.replayed and result.snapshot.state_version != runtime.state_version:
            await self._vote_tally_changed(snapshot, f"vote-remove:{request_id}")
        return snapshot

    async def _game_started(self, snapshot: GameApplicationSnapshot, request_id: str) -> None:
        game = snapshot.game
        payload: dict[str, object] = {
            "room_state_version": snapshot.room.state_version,
            "game_state_version": game.state_version,
            "turn_no": game.turn_no,
            "current_team": game.current_team.value,
            "deadline_ms": game.deadline_ms,
        }
        try:
            await self._events.room_changed(
                event_type="game.started",
                event_key=f"game-start:{snapshot.room.room_id}:{request_id}",
                room_id=snapshot.room.room_id,
                game_id=game.game_id,
                turn_no=game.turn_no,
                payload=payload,
            )
        except Exception:
            _LOGGER.exception(
                "Game start realtime event delivery failed: room_id=%s game_id=%s",
                snapshot.room.room_id,
                game.game_id,
            )
        try:
            await self._events.lobby_rooms_changed(
                {"reason": "GAME_STARTED", "room_id": snapshot.room.room_id},
                event_key=f"game-start:{snapshot.room.room_id}:{request_id}",
            )
        except Exception:
            _LOGGER.exception(
                "Game start lobby event delivery failed: room_id=%s game_id=%s",
                snapshot.room.room_id,
                game.game_id,
            )

    async def _vote_tally_changed(
        self,
        snapshot: GameApplicationSnapshot,
        event_key: str,
    ) -> None:
        game = snapshot.game
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
                room_id=snapshot.room.room_id,
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
                snapshot.room.room_id,
                game.game_id,
            )

    async def _current(
        self,
        session: SessionRecord,
        game_id: str,
    ) -> tuple[RoomParticipation, RoomRuntimeSnapshot, VoteRuntimeSnapshot]:
        participation = await self._require_participation(session)
        room = await self._rooms.get(participation.room_id)
        if room is None:
            raise RoomRuleViolation("ROOM_NOT_FOUND")
        if room.game_id != game_id:
            raise RoomRuleViolation("GAME_NOT_IN_CURRENT_ROOM")
        runtime = await self._votes.get(room.room_id)
        if runtime is None or runtime.game_id != game_id:
            raise RoomRuleViolation("GAME_RUNTIME_NOT_FOUND")
        return participation, room, runtime

    async def _require_participation(self, session: SessionRecord) -> RoomParticipation:
        participation = await self._rooms.resolve_participation(session.session_digest)
        if participation is None:
            raise RoomRuleViolation("SESSION_NOT_IN_ROOM")
        return participation

    @staticmethod
    def _require_voter(runtime: VoteRuntimeSnapshot, participant_id: str) -> None:
        if not any(item.participant_id == participant_id for item in runtime.participants):
            raise VoteRuleViolation("PLAYER_REQUIRED")

    async def _persistence_participant(
        self, participant_id: str, team: Team
    ) -> GameParticipantRecord:
        identity = await self._rooms.resolve_participant_identity(participant_id)
        if identity is None:
            raise RoomRuleViolation("PARTICIPANT_IDENTITY_NOT_FOUND")
        if identity.actor_type is SessionActorType.MEMBER:
            return GameParticipantRecord(
                participant_id=participant_id,
                team=_stone(team),
                role=GameParticipantRole.PLAYER,
                member_id=int(identity.actor_id),
            )
        number = int(hashlib.sha256(identity.actor_id.encode("utf-8")).hexdigest()[:8], 16) % 10_000
        return GameParticipantRecord(
            participant_id=participant_id,
            team=_stone(team),
            role=GameParticipantRole.PLAYER,
            guest_label=f"Guest-{number:04d}",
        )


def _stone(team: Team) -> Stone:
    if team is Team.NONE:
        raise RoomRuleViolation("PLAYER_TEAM_REQUIRED")
    return Stone(team.value)


def _stable_uuid4(seed: str) -> str:
    value = bytearray(hashlib.sha256(seed.encode("utf-8"), usedforsecurity=False).digest()[:16])
    value[6] = (value[6] & 0x0F) | 0x40
    value[8] = (value[8] & 0x3F) | 0x80
    return str(UUID(bytes=bytes(value)))


def _connected(room: RoomRuntimeSnapshot, participant_id: str) -> bool:
    return next(
        item.connected for item in room.participants if item.participant_id == participant_id
    )
