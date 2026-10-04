"""Validated captured closure reads and terminal retention through explicit same-tag keys."""

from __future__ import annotations

from seokpan.persistence.redis.common import LuaScriptRunner, RedisClient, RedisKeyspace
from seokpan.persistence.redis.room_adapter import RedisRoomRuntimeAdapter
from seokpan.persistence.redis.start_capture_script import start_intent_key, start_phase_key
from seokpan.persistence.redis.start_closure_script import CLOSED_START_ACK, CLOSED_START_READ
from seokpan.room.application.runtime import ROOM_REQUEST_DEDUPE_TTL_MS
from seokpan.room.application.start_capture import validate_intent_lookup
from seokpan.room.application.start_closure import (
    ClosedStartIntent,
    decode_closed_start,
    terminal_phase,
)


class RedisCapturedClosureStore:
    def __init__(self, client: RedisClient) -> None:
        self._scripts = LuaScriptRunner(client)

    @staticmethod
    def _keys(room_id: str, game_id: str) -> tuple[str, ...]:
        validate_intent_lookup(room_id, game_id)
        return (
            RedisKeyspace.room_meta(room_id),
            RedisKeyspace.room_closed(room_id),
            start_intent_key(room_id, game_id),
            start_phase_key(room_id, game_id),
        )

    async def read_closed_start(
        self,
        room_id: str,
        game_id: str,
        closed_at_ms: int,
    ) -> ClosedStartIntent | None:
        raw = await self._scripts.execute(
            CLOSED_START_READ,
            keys=self._keys(room_id, game_id),
            args=(),
        )
        result = RedisRoomRuntimeAdapter._result(raw)
        RedisRoomRuntimeAdapter._raise_rejection(result)
        return decode_closed_start(
            room_id=room_id,
            game_id=game_id,
            closed_at_ms=closed_at_ms,
            marker_wire=result.get("marker"),
            intent_wire=result.get("intent"),
            phase_wire=result.get("phase"),
        )

    async def acknowledge(self, value: ClosedStartIntent) -> None:
        intent = value.intent
        raw = await self._scripts.execute(
            CLOSED_START_ACK,
            keys=(
                *self._keys(intent.room_id, intent.game_id),
                RedisKeyspace.room_game(intent.room_id),
            ),
            args=(
                intent.room_id,
                intent.game_id,
                value.closed_at_ms,
                intent.to_json(),
                value.phase_wire,
                terminal_phase(value),
                ROOM_REQUEST_DEDUPE_TTL_MS,
            ),
        )
        result = RedisRoomRuntimeAdapter._result(raw)
        RedisRoomRuntimeAdapter._raise_rejection(result)
