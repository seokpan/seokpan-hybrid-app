"""Restore only a provably uninitialized captured Game before durable invalidation."""

from __future__ import annotations

from seokpan.game.application.persistence import (
    GameParticipantRecord,
    GamePersistencePort,
    GamePersistenceSnapshot,
    PersistenceRuleViolation,
    StartGameCommand,
)
from seokpan.game.domain import GameStatus, Stone
from seokpan.room.application.start_closure import CapturedClosurePort, ClosedStartIntent
from seokpan.vote.application.runtime import VoteRuntimeSnapshot


class CapturedGameInvalidation:
    """Optional runner dependency. Composition activation is a separate rollout gate."""

    def __init__(self, *, closures: CapturedClosurePort, games: GamePersistencePort) -> None:
        self._closures = closures
        self._games = games

    async def prepare_history(
        self,
        *,
        room_id: str,
        game_id: str,
        closed_at_ms: int,
    ) -> None:
        closure = await self._closures.read_closed_start(room_id, game_id, closed_at_ms)
        if closure is None:
            # Existing runner handles legacy history. No guessed backfill.
            return
        history = await self._games.load_game(game_id)
        if history is None:
            if closure.phase_wire != "PENDING" or closure.acknowledged:
                raise PersistenceRuleViolation("GAME_START_RECOVERY_REQUIRED")
            if await self._games.load_result(game_id) is not None:
                raise PersistenceRuleViolation("GAME_RESULT_HISTORY_MISMATCH")
            try:
                await self._games.start_game(self._command(closure))
            except PersistenceRuleViolation as error:
                if error.code != "GAME_START_CONFLICT":
                    raise
            history = await self._games.load_game(game_id)
        if history is None:
            raise PersistenceRuleViolation("GAME_NOT_FOUND")
        self._validate_history(closure, history)

    async def acknowledge(
        self,
        *,
        room_id: str,
        game_id: str,
        closed_at_ms: int,
    ) -> bool:
        """Return False for legacy markers; the runner uses its existing ACK path."""
        closure = await self._closures.read_closed_start(room_id, game_id, closed_at_ms)
        if closure is None:
            return False
        history = await self._games.load_game(game_id)
        if history is None:
            raise PersistenceRuleViolation("GAME_NOT_FOUND")
        self._validate_history(closure, history)
        result = await self._games.load_result(game_id)
        if result is None or result.game_id != game_id or result.room_id != room_id:
            raise PersistenceRuleViolation("GAME_RESULT_HISTORY_MISMATCH")
        await self._closures.acknowledge(closure)
        return True

    async def permits_previous_runtime_cleanup(
        self,
        *,
        room_id: str,
        game_id: str,
        closed_at_ms: int,
        runtime: VoteRuntimeSnapshot,
    ) -> bool:
        """Only the captured, durably finished predecessor may remain from next-game startup."""
        closure = await self._closures.read_closed_start(room_id, game_id, closed_at_ms)
        if closure is None:
            return False
        intent = closure.intent
        if (
            intent.previous_game_id is None
            or runtime.room_id != room_id
            or runtime.game_id != intent.previous_game_id
            or runtime.turn_no != intent.previous_turn_no
            or runtime.game_status is not GameStatus.FINISHED
        ):
            return False
        result = await self._games.load_result(runtime.game_id)
        return (
            result is not None and result.game_id == runtime.game_id and result.room_id == room_id
        )

    @staticmethod
    def _command(value: ClosedStartIntent) -> StartGameCommand:
        intent = value.intent
        return StartGameCommand(
            game_id=intent.game_id,
            room_id=intent.room_id,
            voting_time_seconds=intent.vote_seconds,
            started_at=intent.started_at,
            participants=tuple(
                GameParticipantRecord(
                    participant_id=item.participant_id,
                    team=Stone(item.team),
                    member_id=None if item.member_id is None else int(item.member_id),
                    guest_label=item.guest_label,
                )
                for item in intent.players
            ),
        )

    @classmethod
    def _validate_history(cls, value: ClosedStartIntent, history: GamePersistenceSnapshot) -> None:
        expected, actual = cls._command(value), history.start

        def roster(items: tuple[GameParticipantRecord, ...]) -> list[tuple[object, ...]]:
            return sorted(
                [(p.participant_id, p.team.value, p.member_id, p.guest_label) for p in items],
                key=lambda item: str(item[0]),
            )

        if (
            actual.game_id != expected.game_id
            or actual.room_id != expected.room_id
            or actual.voting_time_seconds != expected.voting_time_seconds
            or actual.started_at != expected.started_at
            or roster(actual.participants) != roster(expected.participants)
        ):
            raise PersistenceRuleViolation("GAME_START_CONFLICT")
