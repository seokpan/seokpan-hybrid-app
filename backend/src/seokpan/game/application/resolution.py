"""Provider-neutral runner for closing due Turns and confirming official results."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from seokpan.game.application.history import replay_game_history
from seokpan.game.application.persistence import (
    FinalizeGameCommand,
    GamePersistencePort,
    GamePersistenceSnapshot,
    OfficialMoveRecord,
    PersistenceRuleViolation,
)
from seokpan.game.domain import (
    EndReason,
    Game,
    GameResultRuleViolation,
    GameResultService,
    GameRuleViolation,
    GameStatus,
)
from seokpan.room.application import (
    CompleteRoomGame,
    NullRealtimeEventAdapter,
    RealtimeEventPort,
    RoomRuntimePort,
    RoomRuntimeSnapshot,
)
from seokpan.room.domain import RoomRuleViolation, RoomStatus
from seokpan.vote.application import (
    AcquireRuntimeResolver,
    ApplyRuntimeResolution,
    CloseRuntimeTurn,
    FinalizeRuntimeGame,
    VoteRuntimePort,
    VoteRuntimeSnapshot,
)
from seokpan.vote.domain import (
    TurnResolution,
    TurnResultKind,
    TurnStatus,
    VoteRuleViolation,
    VoteTurnGame,
)

if TYPE_CHECKING:
    from seokpan.game.application.captured_completion import CapturedGameCompletion
    from seokpan.game.application.captured_invalidation import CapturedGameInvalidation

_LOGGER = logging.getLogger(__name__)


class TurnFinalizationApproval(StrEnum):
    ALLOWED = "ALLOWED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


class TurnProcessingStatus(StrEnum):
    PASS = "PASS"
    MOVE = "MOVE"
    GAME_ENDED = "GAME_ENDED"
    NOT_DUE = "NOT_DUE"
    STALE = "STALE"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    RESOLVER_BUSY = "RESOLVER_BUSY"
    RETRY_REQUIRED = "RETRY_REQUIRED"


@dataclass(frozen=True, slots=True)
class DueTurn:
    room_id: str
    game_id: str
    turn_no: int


@dataclass(frozen=True, slots=True)
class TieSelectionRecord:
    game_id: str
    turn_no: int
    candidates: tuple[str, ...]
    selected_coordinate: str


@dataclass(frozen=True, slots=True)
class TurnProcessingResult:
    due_turn: DueTurn
    status: TurnProcessingStatus
    resolution: TurnResolution | None = None


class MillisecondClock(Protocol):
    @property
    def now_ms(self) -> int: ...


class DueTurnSource(Protocol):
    async def due_turns(self, *, now_ms: int, limit: int) -> tuple[DueTurn, ...]: ...


class TurnFinalizationGate(Protocol):
    async def assess(
        self,
        *,
        due_turn: DueTurn,
        snapshot: VoteRuntimeSnapshot,
    ) -> TurnFinalizationApproval: ...


class TieSelector(Protocol):
    async def select(
        self,
        *,
        game_id: str,
        turn_no: int,
        candidates: tuple[str, ...],
    ) -> str: ...


class TieSelectionAuditPort(Protocol):
    async def record(self, value: TieSelectionRecord) -> None: ...


class TurnResolutionRunner:
    """Close due Turns without making client traffic the scheduler or authority."""

    def __init__(
        self,
        *,
        due_turns: DueTurnSource,
        finalization_gate: TurnFinalizationGate,
        tie_selector: TieSelector,
        tie_audit: TieSelectionAuditPort,
        votes: VoteRuntimePort,
        games: GamePersistencePort,
        rooms: RoomRuntimePort,
        clock: MillisecondClock,
        runner_id: str,
        events: RealtimeEventPort | None = None,
        captured_invalidation: CapturedGameInvalidation | None = None,
        captured_completion: CapturedGameCompletion | None = None,
    ) -> None:
        if not runner_id:
            raise ValueError("INVALID_RUNNER_ID")
        self._due_turns = due_turns
        self._finalization_gate = finalization_gate
        self._tie_selector = tie_selector
        self._tie_audit = tie_audit
        self._votes = votes
        self._games = games
        self._rooms = rooms
        self._clock = clock
        self._runner_id = hashlib.sha256(runner_id.encode()).hexdigest()[:12]
        self._events = events or NullRealtimeEventAdapter()
        self._invalidation_failures: set[tuple[str, str]] = set()
        self._captured_invalidation = captured_invalidation
        self._captured_completion = captured_completion

    async def finalize_departures(self, *, room_id: str, game_id: str) -> bool:
        """Finalize an active Game after Room state confirms player departures."""
        room = await self._rooms.get(room_id)
        runtime = await self._votes.get(room_id)
        if room is None or runtime is None or room.game_id != game_id or runtime.game_id != game_id:
            return False

        history = await self._games.load_game(game_id)
        if history is None:
            raise PersistenceRuleViolation("GAME_NOT_FOUND")
        roster_ids = {item.participant_id for item in history.participants}
        present_ids = {item.participant_id for item in room.participants}
        departed = frozenset(roster_ids - present_ids)
        if not departed:
            return False

        stored = await self._games.load_result(game_id)
        if stored is None:
            game = self._rebuild_before_turn(runtime, history)
            try:
                result = GameResultService(
                    game_id=game_id,
                    game=game,
                    participants=history.participants,
                ).finalize_confirmed_departures(departed_participant_ids=departed)
            except GameResultRuleViolation as error:
                if error.code == "FORFEIT_NOT_CONFIRMED":
                    return False
                raise
            command = FinalizeGameCommand(
                result=result,
                ended_at=datetime.fromtimestamp(self._clock.now_ms / 1000, UTC),
            )
            if not await self._games.result_matches(command):
                await self._games.finalize_game(command)
            end_reason = result.end_reason
            winner = result.winner
        else:
            if stored.end_reason not in {EndReason.FORFEIT, EndReason.JOINT_LOSS}:
                return False
            end_reason = stored.end_reason
            winner = stored.winner

        runtime = await self._votes.get(room_id)
        if runtime is None or runtime.game_id != game_id:
            return False
        if runtime.game_status is GameStatus.ACTIVE:
            finalized = await self._votes.finalize_game(
                FinalizeRuntimeGame(
                    room_id=room_id,
                    request_id=_stable_id(
                        "departure-finalize",
                        DueTurn(room_id, game_id, runtime.turn_no),
                    ),
                    game_id=game_id,
                    turn_no=runtime.turn_no,
                    expected_state_version=runtime.state_version,
                    end_reason=end_reason,
                    winner=winner,
                )
            )
            runtime = finalized.snapshot

        due = DueTurn(room_id, game_id, runtime.turn_no)
        await self._game_finished(due, runtime)
        await self._complete_room(due)
        return True

    async def finalize_system_invalid(
        self,
        *,
        room_id: str,
        game_id: str,
        closed_at_ms: int,
    ) -> bool:
        """Converge a closed Room to one durable Game result, then remove ephemeral runtime."""
        if self._captured_invalidation is not None:
            await self._captured_invalidation.prepare_history(
                room_id=room_id,
                game_id=game_id,
                closed_at_ms=closed_at_ms,
            )
        history = await self._games.load_game(game_id)
        if history is None:
            raise PersistenceRuleViolation("GAME_NOT_FOUND")
        if history.start.room_id != room_id:
            raise PersistenceRuleViolation("GAME_START_CONFLICT")

        stored = await self._games.load_result(game_id)
        runtime = await self._votes.get(room_id)
        runtime_to_discard = game_id
        if runtime is not None and runtime.game_id != game_id:
            if self._captured_invalidation is None or not (
                await self._captured_invalidation.permits_previous_runtime_cleanup(
                    room_id=room_id,
                    game_id=game_id,
                    closed_at_ms=closed_at_ms,
                    runtime=runtime,
                )
            ):
                raise VoteRuleViolation("STALE_GAME")
            runtime_to_discard = runtime.game_id
            runtime = None

        if stored is not None and stored.end_reason is not EndReason.SYSTEM_INVALID:
            await self._votes.discard_game(room_id, runtime_to_discard)
            await self._acknowledge_invalidation(room_id, game_id, closed_at_ms)
            return False

        if stored is None:
            history_game = replay_game_history(history)
            if history_game.status is not GameStatus.ACTIVE:
                result = GameResultService(
                    game_id=game_id,
                    game=history_game,
                    participants=history.participants,
                ).finalize_completed_game()
                if not history.moves:
                    raise PersistenceRuleViolation("GAME_RESULT_HISTORY_MISMATCH")
                command = FinalizeGameCommand(
                    result=result,
                    ended_at=history.moves[-1].confirmed_at,
                )
                if not await self._games.result_matches(command):
                    await self._games.finalize_game(command)
                await self._votes.discard_game(room_id, runtime_to_discard)
                await self._acknowledge_invalidation(room_id, game_id, closed_at_ms)
                return False

            game = history_game if runtime is None else self._rebuild_before_turn(runtime, history)
            result = GameResultService(
                game_id=game_id,
                game=game,
                participants=history.participants,
            ).finalize_system_invalid()
            command = FinalizeGameCommand(
                result=result,
                ended_at=datetime.fromtimestamp(closed_at_ms / 1000, UTC),
            )
            if not await self._games.result_matches(command):
                await self._games.finalize_game(command)

        await self._votes.discard_game(room_id, runtime_to_discard)
        await self._acknowledge_invalidation(room_id, game_id, closed_at_ms)
        return True

    async def _acknowledge_invalidation(
        self,
        room_id: str,
        game_id: str,
        closed_at_ms: int,
    ) -> None:
        if self._captured_invalidation is not None:
            handled = await self._captured_invalidation.acknowledge(
                room_id=room_id,
                game_id=game_id,
                closed_at_ms=closed_at_ms,
            )
            if handled:
                return
        await self._rooms.complete_game_invalidation(room_id, game_id)

    async def reconcile_game_invalidations(self, *, limit: int = 100) -> int:
        """Retry room-closure invalidations from durable Room provider markers."""
        pending = await self._rooms.pending_game_invalidations(limit=limit)
        completed = 0
        for item in pending:
            key = (item.room_id, item.game_id)
            try:
                await self.finalize_system_invalid(
                    room_id=item.room_id,
                    game_id=item.game_id,
                    closed_at_ms=item.closed_at_ms,
                )
                self._invalidation_failures.discard(key)
                completed += 1
            except (
                VoteRuleViolation,
                PersistenceRuleViolation,
                GameRuleViolation,
                GameResultRuleViolation,
                RoomRuleViolation,
            ):
                if key not in self._invalidation_failures:
                    _LOGGER.exception(
                        "Game invalidation item failed",
                        extra={
                            "event": "game_invalidation.item_failed",
                            "room_id": item.room_id,
                            "game_id": item.game_id,
                        },
                    )
                    self._invalidation_failures.add(key)
        return completed

    async def run_once(self, *, limit: int = 100) -> tuple[TurnProcessingResult, ...]:
        if limit < 1:
            raise ValueError("INVALID_DUE_TURN_LIMIT")
        if self._captured_completion is not None:
            try:
                # Independent of PLAYING Room discovery; repair WAITING/successor tasks too.
                await self._captured_completion.reconcile(limit=limit)
            except Exception:
                _LOGGER.exception("Normal completion discovery failed")
        due = await self._due_turns.due_turns(now_ms=self._clock.now_ms, limit=limit)
        results: list[TurnProcessingResult] = []
        for item in due:
            try:
                results.append(await self.process(item))
            except (
                VoteRuleViolation,
                PersistenceRuleViolation,
                GameRuleViolation,
                GameResultRuleViolation,
                RoomRuleViolation,
            ):
                _LOGGER.exception(
                    "Turn resolution item failed",
                    extra={
                        "event": "turn_resolution.item_failed",
                        "room_id": item.room_id,
                        "game_id": item.game_id,
                        "turn_no": item.turn_no,
                    },
                )
        return tuple(results)

    async def process(self, due_turn: DueTurn) -> TurnProcessingResult:
        snapshot = await self._votes.get(due_turn.room_id)
        if snapshot is None or snapshot.game_id != due_turn.game_id:
            return TurnProcessingResult(due_turn, TurnProcessingStatus.STALE)
        if snapshot.turn_no != due_turn.turn_no:
            return TurnProcessingResult(due_turn, TurnProcessingStatus.STALE)
        if (
            snapshot.game_status is GameStatus.FINISHED
            and snapshot.end_reason is not None
            and snapshot.turn_status in {TurnStatus.MOVE_APPLIED, TurnStatus.PASSED}
        ):
            if not await self._games.game_is_finalized(due_turn.game_id):
                return TurnProcessingResult(due_turn, TurnProcessingStatus.RETRY_REQUIRED)
            await self._game_finished(due_turn, snapshot)
            await self._complete_room(due_turn)
            return TurnProcessingResult(due_turn, TurnProcessingStatus.GAME_ENDED)
        if snapshot.deadline_ms is not None and self._clock.now_ms < snapshot.deadline_ms:
            return TurnProcessingResult(due_turn, TurnProcessingStatus.NOT_DUE)
        if snapshot.turn_status is TurnStatus.VOTING:
            approval = await self._finalization_gate.assess(
                due_turn=due_turn,
                snapshot=snapshot,
            )
            if approval is not TurnFinalizationApproval.ALLOWED:
                return TurnProcessingResult(due_turn, TurnProcessingStatus.RECOVERY_REQUIRED)
            room = await self._require_playing_room(due_turn)
            assert snapshot.deadline_ms is not None
            closed = await self._votes.close_turn(
                CloseRuntimeTurn(
                    room_id=due_turn.room_id,
                    request_id=_stable_id("close", due_turn),
                    game_id=due_turn.game_id,
                    turn_no=due_turn.turn_no,
                    expected_state_version=snapshot.state_version,
                    next_deadline_ms=snapshot.deadline_ms + room.config.vote_seconds * 1000,
                )
            )
            if closed.closure is None:
                raise VoteRuleViolation("TURN_CLOSURE_MISSING")
            if closed.closure.result is TurnResultKind.PASSED:
                await self._turn_passed(due_turn, closed.snapshot)
                return TurnProcessingResult(due_turn, TurnProcessingStatus.PASS)
            snapshot = closed.snapshot
        elif snapshot.turn_status is not TurnStatus.RESOLVING:
            return TurnProcessingResult(due_turn, TurnProcessingStatus.STALE)

        await self._turn_resolving(due_turn, snapshot)

        try:
            leased = await self._votes.acquire_resolver(
                AcquireRuntimeResolver(
                    room_id=due_turn.room_id,
                    request_id=_stable_id(
                        f"lease-{self._runner_id}-{self._clock.now_ms}", due_turn
                    ),
                    game_id=due_turn.game_id,
                    turn_no=due_turn.turn_no,
                    resolution_id=self._resolution_id(due_turn),
                    expected_state_version=snapshot.state_version,
                )
            )
        except VoteRuleViolation as error:
            if error.code == "RESOLVER_LEASE_HELD":
                return TurnProcessingResult(due_turn, TurnProcessingStatus.RESOLVER_BUSY)
            raise

        history = await self._games.load_game(due_turn.game_id)
        if history is None:
            raise PersistenceRuleViolation("GAME_NOT_FOUND")
        resolution = await self._resolution(due_turn, leased.snapshot, history)
        if resolution.applied_move is not None:
            await self._persist_move(due_turn, leased.snapshot, resolution)

        if resolution.end_reason is not None:
            game = self._rebuild_before_turn(leased.snapshot, history)
            if resolution.result is TurnResultKind.JOINT_LOSS:
                game.finish_joint_loss()
            else:
                assert resolution.selected_coordinate is not None
                game.apply_move(
                    team=resolution.team,
                    coordinate=resolution.selected_coordinate,
                )
            result = GameResultService(
                game_id=due_turn.game_id,
                game=game,
                participants=history.participants,
            ).finalize_completed_game()
            ended_at = self._event_time(leased.snapshot)
            command = FinalizeGameCommand(result=result, ended_at=ended_at)
            if not await self._games.result_matches(command):
                await self._games.finalize_game(command)

        try:
            applied = await self._votes.apply_resolution(
                ApplyRuntimeResolution(
                    room_id=due_turn.room_id,
                    request_id=_stable_id("apply", due_turn),
                    game_id=due_turn.game_id,
                    turn_no=due_turn.turn_no,
                    resolution_id=self._resolution_id(due_turn),
                    resolution=resolution,
                    expected_state_version=leased.snapshot.state_version,
                    persistence_confirmed=True,
                    next_deadline_ms=(
                        None
                        if resolution.end_reason is not None
                        else self._next_deadline(leased.snapshot, history)
                    ),
                )
            )
        except VoteRuleViolation as error:
            if error.code in {"RESOLVER_LEASE_EXPIRED", "RESOLVER_NOT_OWNER"}:
                return TurnProcessingResult(
                    due_turn,
                    TurnProcessingStatus.RETRY_REQUIRED,
                    resolution,
                )
            raise

        if resolution.result is TurnResultKind.JOINT_LOSS:
            await self._turn_passed(due_turn, applied.snapshot)
        else:
            await self._move_applied(due_turn, leased.snapshot, applied.snapshot)

        if resolution.end_reason is None:
            return TurnProcessingResult(due_turn, TurnProcessingStatus.MOVE, applied.resolution)
        await self._game_finished(due_turn, applied.snapshot)
        await self._complete_room(due_turn)
        return TurnProcessingResult(due_turn, TurnProcessingStatus.GAME_ENDED, applied.resolution)

    async def _resolution(
        self,
        due_turn: DueTurn,
        snapshot: VoteRuntimeSnapshot,
        history: GamePersistenceSnapshot,
    ) -> TurnResolution:
        if not snapshot.candidates:
            if snapshot.consecutive_passes != 1:
                raise VoteRuleViolation("RESOLUTION_CANDIDATES_MISSING")
            return TurnResolution(
                game_id=due_turn.game_id,
                turn_no=due_turn.turn_no,
                team=snapshot.current_team,
                result=TurnResultKind.JOINT_LOSS,
                status=TurnStatus.PASSED,
                selected_coordinate=None,
                applied_move=None,
                end_reason=EndReason.JOINT_LOSS,
            )

        existing = await self._games.get_move(due_turn.game_id, due_turn.turn_no)
        candidate_values = tuple(item.canonical for item in snapshot.candidates)
        selected_value: str | None
        if existing is not None:
            selected_value = existing.coordinate.canonical
        elif len(candidate_values) == 1:
            selected_value = None
        else:
            selected_value = await self._tie_selector.select(
                game_id=due_turn.game_id,
                turn_no=due_turn.turn_no,
                candidates=candidate_values,
            )
        selected = VoteTurnGame.select_candidate(snapshot.candidates, selected_value)
        if len(candidate_values) > 1:
            await self._tie_audit.record(
                TieSelectionRecord(
                    due_turn.game_id,
                    due_turn.turn_no,
                    candidate_values,
                    selected.canonical,
                )
            )
        game = self._rebuild_before_turn(snapshot, history)
        outcome = game.apply_move(team=snapshot.current_team, coordinate=selected)
        return TurnResolution(
            game_id=due_turn.game_id,
            turn_no=due_turn.turn_no,
            team=snapshot.current_team,
            result=TurnResultKind.MOVE_APPLIED,
            status=TurnStatus.MOVE_APPLIED,
            selected_coordinate=selected,
            applied_move=outcome.move,
            end_reason=outcome.end_reason,
        )

    async def _persist_move(
        self,
        due_turn: DueTurn,
        snapshot: VoteRuntimeSnapshot,
        resolution: TurnResolution,
    ) -> None:
        move = resolution.applied_move
        assert move is not None
        existing = await self._games.get_move(due_turn.game_id, due_turn.turn_no)
        final_vote_count = next(
            item.count for item in snapshot.tally if item.coordinate == move.coordinate
        )
        valid_voter_count = snapshot.valid_voter_count
        if valid_voter_count is None:
            raise VoteRuleViolation("VALID_VOTER_COUNT_MISSING")
        confirmed_at = existing.confirmed_at if existing is not None else self._event_time(snapshot)
        command = OfficialMoveRecord(
            game_id=due_turn.game_id,
            turn_no=due_turn.turn_no,
            move_no=move.move_no,
            team=move.team,
            coordinate=move.coordinate,
            final_vote_count=final_vote_count,
            valid_voter_count=valid_voter_count,
            confirmed_at=confirmed_at,
        )
        if existing is not None:
            if existing != command:
                raise PersistenceRuleViolation("MOVE_SEQUENCE_CONFLICT")
            return
        await self._games.append_move(command)

    def _rebuild_before_turn(
        self,
        snapshot: VoteRuntimeSnapshot,
        history: GamePersistenceSnapshot,
    ) -> Game:
        game = Game()
        next_turn = 1
        for move in history.moves:
            if move.turn_no >= snapshot.turn_no:
                break
            while next_turn < move.turn_no:
                game.pass_turn()
                next_turn += 1
            outcome = game.apply_move(team=move.team, coordinate=move.coordinate)
            if outcome.move.move_no != move.move_no:
                raise PersistenceRuleViolation("MOVE_SEQUENCE_CONFLICT")
            next_turn = move.turn_no + 1
        while next_turn < snapshot.turn_no:
            game.pass_turn()
            next_turn += 1
        if (
            game.current_team is not snapshot.current_team
            or game.move_no != snapshot.move_no
            or set(game.occupied_cells) != set(snapshot.occupied_cells)
        ):
            raise PersistenceRuleViolation("GAME_RUNTIME_HISTORY_MISMATCH")
        return game

    async def _require_playing_room(self, due_turn: DueTurn) -> RoomRuntimeSnapshot:
        room = await self._rooms.get(due_turn.room_id)
        if room is None:
            raise VoteRuleViolation("ROOM_NOT_FOUND")
        if room.status is not RoomStatus.PLAYING or room.game_id != due_turn.game_id:
            raise VoteRuleViolation("GAME_NOT_IN_CURRENT_ROOM")
        return room

    async def _complete_room(self, due_turn: DueTurn) -> None:
        if self._captured_completion is not None:
            changed = await self._captured_completion.complete(
                room_id=due_turn.room_id,
                game_id=due_turn.game_id,
                final_turn_no=due_turn.turn_no,
            )
            if changed is not None:
                if changed:
                    await self._room_completed_events(due_turn)
                return
        room = await self._rooms.get(due_turn.room_id)
        if room is None:
            raise VoteRuleViolation("ROOM_NOT_FOUND")
        if (
            room.status is RoomStatus.WAITING
            and room.game_id is None
            and room.last_game_id == due_turn.game_id
            and room.last_game_turn_no == due_turn.turn_no
        ):
            return
        completed = await self._rooms.complete_game(
            CompleteRoomGame(
                room_id=due_turn.room_id,
                request_id=_stable_id("room-complete", due_turn),
                game_id=due_turn.game_id,
                expected_state_version=room.state_version,
                final_turn_no=due_turn.turn_no,
            )
        )
        if completed.replayed or completed.snapshot is None:
            return
        await self._room_completed_events(due_turn)

    async def _room_completed_events(self, due_turn: DueTurn) -> None:
        try:
            await self._events.room_changed(
                event_type="snapshot.required",
                room_id=due_turn.room_id,
                game_id=due_turn.game_id,
                payload={"reason": "GAME_COMPLETED"},
            )
        except Exception:
            _LOGGER.exception(
                "Game completion room event delivery failed: room_id=%s game_id=%s",
                due_turn.room_id,
                due_turn.game_id,
            )
        try:
            await self._events.lobby_rooms_changed(
                {"reason": "GAME_COMPLETED", "room_id": due_turn.room_id}
            )
        except Exception:
            _LOGGER.exception(
                "Game completion lobby event delivery failed: room_id=%s game_id=%s",
                due_turn.room_id,
                due_turn.game_id,
            )

    async def _turn_resolving(
        self,
        due_turn: DueTurn,
        snapshot: VoteRuntimeSnapshot,
    ) -> None:
        await self._emit(
            due_turn,
            "turn.resolving",
            snapshot,
            {
                "game_state_version": snapshot.state_version,
                "team": snapshot.current_team.value,
                "tally": self._tally(snapshot),
                "valid_voter_count": snapshot.valid_voter_count,
                "candidates": [item.canonical for item in snapshot.candidates],
            },
        )

    async def _turn_passed(
        self,
        due_turn: DueTurn,
        snapshot: VoteRuntimeSnapshot,
    ) -> None:
        game_ended = snapshot.game_status is not GameStatus.ACTIVE
        await self._emit(
            due_turn,
            "turn.passed",
            snapshot,
            {
                "game_state_version": snapshot.state_version,
                "completed_turn_no": due_turn.turn_no,
                "next_turn_no": None if game_ended else snapshot.turn_no,
                "next_team": None if game_ended else snapshot.current_team.value,
                "deadline_ms": snapshot.deadline_ms,
                "consecutive_passes": snapshot.consecutive_passes,
            },
        )

    async def _move_applied(
        self,
        due_turn: DueTurn,
        closed: VoteRuntimeSnapshot,
        applied: VoteRuntimeSnapshot,
    ) -> None:
        stored = await self._games.get_move(due_turn.game_id, due_turn.turn_no)
        if stored is None:
            raise PersistenceRuleViolation("MOVE_NOT_FOUND")
        await self._emit(
            due_turn,
            "game.move_applied",
            applied,
            {
                "game_state_version": applied.state_version,
                "move_no": stored.move_no,
                "team": stored.team.value,
                "coordinate": stored.coordinate.canonical,
                "final_vote_count": stored.final_vote_count,
                "valid_voter_count": stored.valid_voter_count,
                "board": self._board(applied),
                "next_turn_no": (
                    applied.turn_no if applied.game_status is GameStatus.ACTIVE else None
                ),
                "next_team": (
                    applied.current_team.value if applied.game_status is GameStatus.ACTIVE else None
                ),
                "deadline_ms": applied.deadline_ms,
                "final_tally": self._tally(closed),
            },
        )

    async def _game_finished(
        self,
        due_turn: DueTurn,
        snapshot: VoteRuntimeSnapshot,
    ) -> None:
        if snapshot.end_reason is None:
            raise VoteRuleViolation("GAME_END_REASON_MISSING")
        winner = {
            EndReason.BLACK_WIN: "BLACK",
            EndReason.WHITE_WIN: "WHITE",
        }.get(snapshot.end_reason)
        await self._emit(
            due_turn,
            "game.finished",
            snapshot,
            {
                "game_state_version": snapshot.state_version,
                "end_reason": snapshot.end_reason.value,
                "winner": winner,
                "board": self._board(snapshot),
            },
        )

    async def _emit(
        self,
        due_turn: DueTurn,
        event_type: str,
        snapshot: VoteRuntimeSnapshot,
        payload: dict[str, object],
    ) -> None:
        try:
            await self._events.room_changed(
                event_type=event_type,
                event_key=f"{event_type}:{due_turn.game_id}:{due_turn.turn_no}",
                room_id=due_turn.room_id,
                game_id=due_turn.game_id,
                turn_no=due_turn.turn_no,
                payload=payload,
            )
        except Exception:
            _LOGGER.exception(
                "Turn realtime event delivery failed: event_type=%s room_id=%s "
                "game_id=%s turn_no=%s game_state_version=%s",
                event_type,
                due_turn.room_id,
                due_turn.game_id,
                due_turn.turn_no,
                snapshot.state_version,
            )

    @staticmethod
    def _tally(snapshot: VoteRuntimeSnapshot) -> list[dict[str, object]]:
        return [
            {"coordinate": item.coordinate.canonical, "count": item.count}
            for item in snapshot.tally
        ]

    @staticmethod
    def _board(snapshot: VoteRuntimeSnapshot) -> list[dict[str, object]]:
        return [
            {"coordinate": item.coordinate.canonical, "stone": item.stone.value}
            for item in snapshot.occupied_cells
        ]

    def _next_deadline(
        self,
        snapshot: VoteRuntimeSnapshot,
        history: GamePersistenceSnapshot,
    ) -> int:
        if snapshot.deadline_ms is None:
            raise VoteRuleViolation("INVALID_NEXT_DEADLINE")
        return snapshot.deadline_ms + history.start.voting_time_seconds * 1000

    def _event_time(self, snapshot: VoteRuntimeSnapshot) -> datetime:
        timestamp_ms = snapshot.deadline_ms
        if timestamp_ms is None:
            timestamp_ms = self._clock.now_ms
        return datetime.fromtimestamp(timestamp_ms / 1000, UTC)

    def _resolution_id(self, due_turn: DueTurn) -> str:
        return _stable_id(f"resolution-{self._runner_id}", due_turn)


def _stable_id(prefix: str, due_turn: DueTurn) -> str:
    value = f"{due_turn.game_id}:{due_turn.turn_no}".encode()
    return f"{prefix}-{hashlib.sha256(value).hexdigest()[:24]}"
