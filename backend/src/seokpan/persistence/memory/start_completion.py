"""Normal completion on the shared Memory stores, preserving FINISHED Vote Runtime."""

from __future__ import annotations

import json

from seokpan.game.domain import GameStatus
from seokpan.persistence.memory.room_adapter import InMemoryRoomRuntimeAdapter
from seokpan.persistence.memory.vote_adapter import InMemoryVoteRuntimeAdapter
from seokpan.room.application.runtime import ROOM_REQUEST_DEDUPE_TTL_MS
from seokpan.room.application.start_capture import validate_intent_lookup
from seokpan.room.application.start_completion import (
    CompleteCapturedGame,
    PendingCapturedCompletion,
    initialized_phase,
    pending_completion_wire,
)
from seokpan.room.application.start_intent import RoomGameStartIntent
from seokpan.room.domain import RoomRuleViolation, RoomStatus


class InMemoryCapturedCompletionStore:
    def __init__(
        self,
        *,
        rooms: InMemoryRoomRuntimeAdapter,
        votes: InMemoryVoteRuntimeAdapter,
    ) -> None:
        self._rooms, self._votes = rooms, votes
        # Shared on the Room adapter, not on this wrapper. Memory is not durable Redis.
        self._pending = rooms._normal_completion_pending
        self._offset = 0

    async def pending(self, *, limit: int) -> tuple[tuple[str, str], ...]:
        if type(limit) is not int or limit < 1:
            raise ValueError("INVALID_COMPLETION_LIMIT")
        keys = tuple(sorted(self._pending))
        if not keys:
            self._offset = 0
            return ()
        start = self._offset % len(keys)
        selected = (keys[start:] + keys[:start])[:limit]
        self._offset = (start + len(selected)) % len(keys)
        return selected

    async def read_pending(self, room_id: str, game_id: str) -> PendingCapturedCompletion | None:
        validate_intent_lookup(room_id, game_id)
        raw = self._pending.get((room_id, game_id))
        return None if raw is None else PendingCapturedCompletion.from_json(raw)

    async def read_start(
        self,
        room_id: str,
        game_id: str,
    ) -> tuple[RoomGameStartIntent, str] | None:
        validate_intent_lookup(room_id, game_id)
        self._rooms._purge_expired()
        key = (room_id, game_id)
        intent, phase = self._rooms._start_intents.get(key), self._rooms._start_phases.get(key)
        if intent is None and phase is None:
            return None
        if intent is None or not isinstance(phase, str):
            raise RoomRuleViolation("START_COMPLETION_INVALID")
        initialized_phase(intent, phase)
        return intent, phase

    async def complete(self, command: CompleteCapturedGame) -> bool:
        # No await from validation through Room release and per-game retention.
        self._rooms._purge_expired()
        intent = command.intent
        key = (intent.room_id, intent.game_id)
        stored_task = self._pending.get(key)
        if command.pending_wire is not None:
            if stored_task is None:
                return False
            if stored_task != command.pending_wire:
                raise RoomRuleViolation("START_COMPLETION_CHANGED")
        task = None if stored_task is None else PendingCapturedCompletion.from_json(stored_task)
        marker = self._rooms._pending_game_invalidations.get(intent.room_id)
        receipt = self._rooms._captured_closure_receipts.get(key)
        if task is not None and intent.room_id not in self._rooms._rooms and receipt is not None:
            closed = json.loads(receipt)
            final = json.loads(self._rooms._start_phases.get(key, "null"))
            if (
                marker is None
                and intent.room_id not in self._votes._states
                and self._rooms._start_intents.get(key) == intent
                and closed.get("room_id") == intent.room_id
                and closed.get("terminated_game_id") == intent.game_id
                and closed.get("invalidation_pending") is False
                and isinstance(final, dict)
                and final.get("phase") == "FINALIZED"
                and type(final.get("schema_version")) is int
                and final["schema_version"] == 1
                and final.get("game_id") == intent.game_id
                and type(closed.get("closed_at_ms")) is int
                and final.get("intent_fingerprint") == intent.fingerprint
                and final.get("closed_at_ms") == closed.get("closed_at_ms")
            ):
                self._pending.pop(key, None)
                return False
        if task is not None and task.released:
            if (
                task.command.intent != intent
                or task.command.final_turn_no != command.final_turn_no
                or task.command.end_reason != command.end_reason
                or task.command.ended_at_ms != command.ended_at_ms
            ):
                raise RoomRuleViolation("START_COMPLETION_CHANGED")
            state = self._rooms._rooms.get(intent.room_id)
            if (
                state is not None
                and self._rooms._snapshot(intent.room_id, state).game_id == intent.game_id
            ):
                raise RoomRuleViolation("GAME_COMPLETION_UNCONFIRMED")
            marker = self._rooms._pending_game_invalidations.get(intent.room_id)
            if marker is not None and marker.game_id == intent.game_id:
                raise RoomRuleViolation("GAME_COMPLETION_UNCONFIRMED")
            _, expiry, _ = command.receipt(
                initialized_phase(intent, task.command.phase_wire),
                now_ms=self._rooms._clock.now_ms,
                retention_ms=ROOM_REQUEST_DEDUPE_TTL_MS,
            )
            saved_intent = self._rooms._start_intents.get(key)
            saved_phase = self._rooms._start_phases.get(key)
            if (
                (saved_intent is not None and saved_intent != intent)
                or (saved_phase is not None and saved_phase != task.command.phase_wire)
                or (
                    self._rooms._clock.now_ms < expiry
                    and (saved_intent is None or saved_phase is None)
                )
            ):
                raise RoomRuleViolation("START_COMPLETION_CHANGED")
            self._rooms._start_record_expiries[key] = expiry
            self._rooms._purge_expired()
            self._pending.pop(key, None)
            return False
        state = self._rooms._rooms.get(intent.room_id)
        if state is None or intent.room_id in self._rooms._pending_game_invalidations:
            raise RoomRuleViolation("GAME_COMPLETION_UNCONFIRMED")
        if self._rooms._start_intents.get(key) != intent:
            raise RoomRuleViolation("START_COMPLETION_CHANGED")
        raw_phase = self._rooms._start_phases.get(key)
        if not isinstance(raw_phase, str):
            raise RoomRuleViolation("START_COMPLETION_INVALID")
        phase = initialized_phase(intent, raw_phase)
        if task is not None and not task.released and "normal_completion" not in phase:
            if task.command.intent != intent:
                raise RoomRuleViolation("START_COMPLETION_CHANGED")
            phase = initialized_phase(intent, task.command.phase_wire)
        wire, expiry, replay = command.receipt(
            phase,
            now_ms=self._rooms._clock.now_ms,
            retention_ms=ROOM_REQUEST_DEDUPE_TTL_MS,
        )
        if not replay and raw_phase != command.phase_wire:
            raise RoomRuleViolation("START_COMPLETION_CHANGED")
        room = self._rooms._snapshot(intent.room_id, state)
        if room.status not in {RoomStatus.PLAYING, RoomStatus.WAITING}:
            raise RoomRuleViolation("GAME_COMPLETION_UNCONFIRMED")
        releasing = room.status is RoomStatus.PLAYING and room.game_id == intent.game_id
        waiting = (
            room.status is RoomStatus.WAITING
            and room.game_id is None
            and room.last_game_id == intent.game_id
            and room.last_game_turn_no == command.final_turn_no
        )
        if not releasing and not waiting and not replay:
            raise RoomRuleViolation("GAME_COMPLETION_UNCONFIRMED")
        if releasing or not replay:
            runtime = self._votes._states.get(intent.room_id)
            if (
                runtime is None
                or runtime.game.game_id != intent.game_id
                or runtime.game.game.status is not GameStatus.FINISHED
                or runtime.game.turn_no != command.final_turn_no
                or runtime.game.game.end_reason is None
                or runtime.game.game.end_reason.value != command.end_reason
            ):
                raise RoomRuleViolation("GAME_COMPLETION_UNCONFIRMED")
        if releasing:
            self._rooms._require_expected_version(state, command.expected_room_version)
        self._pending[key] = pending_completion_wire(intent, wire, released=False)
        self._rooms._start_phases[key] = wire
        if releasing:
            state.room.complete_game(game_id=intent.game_id)
            state.last_game_id, state.last_game_turn_no = intent.game_id, command.final_turn_no
        self._pending[key] = pending_completion_wire(intent, wire, released=True)
        self._rooms._start_record_expiries[key] = expiry
        # Never reset Ready again in WAITING or touch a successor's Runtime.
        self._rooms._purge_expired()
        self._pending.pop(key, None)
        return releasing
