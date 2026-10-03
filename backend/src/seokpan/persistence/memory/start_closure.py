"""Captured closure read/ACK on the same Memory Room/Vote adapter instances."""

from __future__ import annotations

import json

from seokpan.persistence.memory.room_adapter import InMemoryRoomRuntimeAdapter
from seokpan.persistence.memory.vote_adapter import InMemoryVoteRuntimeAdapter
from seokpan.room.application.runtime import ROOM_REQUEST_DEDUPE_TTL_MS
from seokpan.room.application.start_closure import (
    ClosedStartIntent,
    decode_closed_start,
    terminal_phase,
)
from seokpan.room.domain import RoomRuleViolation


class InMemoryCapturedClosureStore:
    def __init__(
        self,
        *,
        rooms: InMemoryRoomRuntimeAdapter,
        votes: InMemoryVoteRuntimeAdapter,
    ) -> None:
        self._rooms, self._votes = rooms, votes
        # Receipts live on the shared Room instance, not this wrapper instance.

    async def read_closed_start(
        self,
        room_id: str,
        game_id: str,
        closed_at_ms: int,
    ) -> ClosedStartIntent | None:
        self._rooms._purge_expired()
        if room_id in self._rooms._rooms:
            raise RoomRuleViolation("GAME_CLOSURE_UNCONFIRMED")
        marker = self._rooms._pending_game_invalidations.get(room_id)
        receipt = self._rooms._captured_closure_receipts.get((room_id, game_id))
        if marker is not None:
            marker_wire = json.dumps(
                {
                    "room_id": marker.room_id,
                    "terminated_game_id": marker.game_id,
                    "closed_at_ms": marker.closed_at_ms,
                    "invalidation_pending": True,
                }
            )
        elif receipt is not None:
            marker_wire = receipt
        else:
            raise RoomRuleViolation("GAME_CLOSURE_UNCONFIRMED")
        intent = self._rooms._start_intents.get((room_id, game_id))
        return decode_closed_start(
            room_id=room_id,
            game_id=game_id,
            closed_at_ms=closed_at_ms,
            marker_wire=marker_wire,
            intent_wire=None if intent is None else intent.to_json(),
            phase_wire=self._rooms._start_phases.get((room_id, game_id)),
        )

    async def acknowledge(self, value: ClosedStartIntent) -> None:
        intent = value.intent
        # One event-loop transition: all validation/encoding precedes mutation.
        self._rooms._purge_expired()
        key = (intent.room_id, intent.game_id)
        if intent.room_id in self._rooms._rooms or intent.room_id in self._votes._states:
            raise RoomRuleViolation("GAME_CLOSURE_UNCONFIRMED")
        marker = self._rooms._pending_game_invalidations.get(intent.room_id)
        if marker is None:
            stored_intent = self._rooms._start_intents.get(key)
            receipt = self._rooms._captured_closure_receipts.get(key)
            if stored_intent != intent or receipt is None:
                raise RoomRuleViolation("START_CLOSURE_CHANGED")
            current = decode_closed_start(
                room_id=intent.room_id,
                game_id=intent.game_id,
                closed_at_ms=value.closed_at_ms,
                marker_wire=receipt,
                intent_wire=stored_intent.to_json(),
                phase_wire=self._rooms._start_phases.get(key),
            )
            if current is None or not current.acknowledged:
                raise RoomRuleViolation("START_CLOSURE_CHANGED")
            return
        if marker.game_id != intent.game_id or marker.closed_at_ms != value.closed_at_ms:
            raise RoomRuleViolation("GAME_CLOSURE_UNCONFIRMED")
        phase = self._rooms._start_phases.get(key)
        if self._rooms._start_intents.get(key) != intent or phase != value.phase_wire:
            raise RoomRuleViolation("START_CLOSURE_CHANGED")
        terminal = terminal_phase(value)
        receipt = json.dumps(
            {
                "room_id": intent.room_id,
                "terminated_game_id": intent.game_id,
                "closed_at_ms": value.closed_at_ms,
                "invalidation_pending": False,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        expires = self._rooms._clock.now_ms + ROOM_REQUEST_DEDUPE_TTL_MS
        self._rooms._start_phases[key] = terminal
        self._rooms._start_record_expiries[key] = expires
        self._rooms._captured_closure_receipts[key] = receipt
        self._rooms._tombstones[intent.room_id] = expires
        self._rooms._pending_game_invalidations.pop(intent.room_id)
