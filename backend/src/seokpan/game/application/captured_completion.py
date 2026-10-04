"""Validate durable normal results before releasing a captured Room and its retention."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from seokpan.game.application.captured_startup import CapturedGameStartup
from seokpan.game.application.persistence import (
    GameParticipantRecord,
    GamePersistencePort,
    PersistenceRuleViolation,
)
from seokpan.room.application.runtime import RoomRuntimePort
from seokpan.room.application.start_completion import CapturedCompletionPort, CompleteCapturedGame
from seokpan.room.application.start_intent import RoomGameStartIntent
from seokpan.room.domain import RoomRuleViolation

_LOGGER = logging.getLogger(__name__)


class CapturedGameCompletion:
    """Optional runner dependency; no composition activation in this change."""

    def __init__(
        self,
        *,
        records: CapturedCompletionPort,
        games: GamePersistencePort,
        rooms: RoomRuntimePort,
    ) -> None:
        self._records, self._games, self._rooms = records, games, rooms

    async def complete(self, *, room_id: str, game_id: str, final_turn_no: int) -> bool | None:
        proof = await self._records.read_start(room_id, game_id)
        if proof is None:
            return None  # Existing legacy completion remains separate; never guessed backfill.
        intent, phase_wire = proof
        return await self._complete_proof(room_id, game_id, final_turn_no, intent, phase_wire)

    async def reconcile(self, *, limit: int = 100) -> int:
        if type(limit) is not int or limit < 1:
            raise ValueError("INVALID_COMPLETION_LIMIT")
        completed = 0
        for room_id, game_id in await self._records.pending(limit=limit):
            try:
                task = await self._records.read_pending(room_id, game_id)
                if task is None:
                    continue  # Another worker settled it; SCAN may duplicate entries.
                command = task.command
                await self._complete_proof(
                    room_id,
                    game_id,
                    command.final_turn_no,
                    command.intent,
                    command.phase_wire,
                    pending_wire=task.wire,
                )
                completed += 1
            except Exception:
                # Keep the shared task. Cancellation is a BaseException and propagates.
                _LOGGER.exception(
                    "Normal completion reconciliation item failed",
                    extra={"room_id": room_id, "game_id": game_id},
                )
        return completed

    async def _complete_proof(
        self,
        room_id: str,
        game_id: str,
        final_turn_no: int,
        intent: RoomGameStartIntent,
        phase_wire: str,
        *,
        pending_wire: str | None = None,
    ) -> bool:
        if intent.room_id != room_id or intent.game_id != game_id:
            raise RoomRuleViolation("START_COMPLETION_INVALID")
        history = await self._games.load_game(game_id)
        result = await self._games.load_result(game_id)
        if history is None or result is None:
            raise PersistenceRuleViolation("GAME_RESULT_HISTORY_MISMATCH")
        expected, actual = CapturedGameStartup.persistence_command(intent), history.start

        def roster(
            values: tuple[GameParticipantRecord, ...],
        ) -> list[tuple[str, str, int | None, str | None]]:
            return sorted(
                [(p.participant_id, p.team.value, p.member_id, p.guest_label) for p in values],
                key=lambda item: item[0],
            )

        if (
            actual.game_id != game_id
            or actual.room_id != room_id
            or actual.voting_time_seconds != expected.voting_time_seconds
            or actual.started_at != expected.started_at
            or roster(actual.participants) != roster(expected.participants)
            or result.game_id != game_id
            or result.room_id != room_id
            or not await self._games.game_is_finalized(game_id)
        ):
            raise PersistenceRuleViolation("GAME_RESULT_HISTORY_MISMATCH")
        ended = result.ended_at
        if ended.tzinfo is None or ended.utcoffset() is None:
            raise PersistenceRuleViolation("GAME_RESULT_HISTORY_MISMATCH")
        delta = ended.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
        ended_at_ms = delta.days * 86400000 + delta.seconds * 1000 + delta.microseconds // 1000
        room = await self._rooms.get(room_id)
        if room is None and pending_wire is None:
            # Closure owns retention now. Do not expire pending F15 recovery records.
            raise RoomRuleViolation("ROOM_NOT_FOUND")
        return await self._records.complete(
            CompleteCapturedGame(
                intent=intent,
                phase_wire=phase_wire,
                expected_room_version=1 if room is None else room.state_version,
                final_turn_no=final_turn_no,
                end_reason=result.end_reason.value,
                ended_at_ms=ended_at_ms,
                pending_wire=pending_wire,
            )
        )
