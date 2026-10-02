"""Captured normal Room completion through explicit same-tag Redis keys."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator

from redis.exceptions import RedisError

from seokpan.persistence.redis.common import (
    LuaScriptRunner,
    RedisClient,
    RedisKeyspace,
    RedisProviderError,
)
from seokpan.persistence.redis.room_adapter import RedisRoomRuntimeAdapter
from seokpan.persistence.redis.start_capture_script import start_intent_key, start_phase_key
from seokpan.persistence.redis.start_completion_script import (
    NORMAL_START_COMPLETE,
    START_COMPLETION_READ,
    completion_pending_key,
)
from seokpan.room.application.runtime import ROOM_REQUEST_DEDUPE_TTL_MS
from seokpan.room.application.start_capture import validate_intent_lookup
from seokpan.room.application.start_completion import (
    CompleteCapturedGame,
    PendingCapturedCompletion,
    initialized_phase,
)
from seokpan.room.application.start_intent import RoomGameStartIntent
from seokpan.room.domain import RoomRuleViolation

_LOGGER = logging.getLogger(__name__)


class RedisCapturedCompletionStore:
    def __init__(self, client: RedisClient) -> None:
        self._scripts = LuaScriptRunner(client)
        self._client = client
        self._scan: AsyncIterator[bytes | str] | None = None
        self._scan_lock = asyncio.Lock()

    async def pending(self, *, limit: int) -> tuple[tuple[str, str], ...]:
        if type(limit) is not int or limit < 1:
            raise ValueError("INVALID_COMPLETION_LIMIT")
        result: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        async with self._scan_lock:
            if self._scan is None:
                self._scan = self._client.scan_iter(
                    match="stone:v1:room:*:normal-completion-pending:*",
                    count=100,
                ).__aiter__()
            try:
                # COUNT is a server hint, not a hard work bound. Keep iteration
                # across ticks so a poison prefix cannot starve later tasks.
                for _ in range(limit):
                    try:
                        raw = await anext(self._scan)
                    except StopAsyncIteration:
                        self._scan = None
                        break
                    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
                    match = re.fullmatch(
                        r"stone:v1:room:\{([A-Za-z0-9_-]{1,64})\}:"
                        r"normal-completion-pending:([A-Za-z0-9_-]{1,64})",
                        text,
                    )
                    if match is None:
                        _LOGGER.warning("Invalid normal completion task key")
                        continue
                    pair = (match[1], match[2])
                    if pair not in seen:
                        seen.add(pair)
                        result.append(pair)
            except BaseException:
                self._scan = None
                raise
        return tuple(result)

    async def read_pending(self, room_id: str, game_id: str) -> PendingCapturedCompletion | None:
        validate_intent_lookup(room_id, game_id)
        try:
            raw = await self._client.get(completion_pending_key(room_id, game_id))
        except RedisError as error:
            raise RedisProviderError() from error
        if raw is None:
            return None
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        value = PendingCapturedCompletion.from_json(text)
        if value.command.intent.room_id != room_id or value.command.intent.game_id != game_id:
            raise RoomRuleViolation("START_COMPLETION_INVALID")
        return value

    async def read_start(
        self,
        room_id: str,
        game_id: str,
    ) -> tuple[RoomGameStartIntent, str] | None:
        validate_intent_lookup(room_id, game_id)
        raw = await self._scripts.execute(
            START_COMPLETION_READ,
            keys=(start_intent_key(room_id, game_id), start_phase_key(room_id, game_id)),
            args=(),
        )
        result = RedisRoomRuntimeAdapter._result(raw)
        RedisRoomRuntimeAdapter._raise_rejection(result)
        value, phase = result.get("intent"), result.get("phase")
        if value is None and phase is None:
            return None
        if not isinstance(value, str) or not isinstance(phase, str):
            raise RoomRuleViolation("START_COMPLETION_INVALID")
        try:
            intent = RoomGameStartIntent.from_json(value)
        except (ValueError, TypeError) as error:
            raise RoomRuleViolation("START_COMPLETION_INVALID") from error
        if intent.room_id != room_id or intent.game_id != game_id:
            raise RoomRuleViolation("START_COMPLETION_INVALID")
        initialized_phase(intent, phase)
        return intent, phase

    async def complete(self, command: CompleteCapturedGame) -> bool:
        intent = command.intent
        validate_intent_lookup(intent.room_id, intent.game_id)
        raw = await self._scripts.execute(
            NORMAL_START_COMPLETE,
            keys=(
                RedisKeyspace.room_meta(intent.room_id),
                RedisKeyspace.room_ready(intent.room_id),
                RedisKeyspace.room_game(intent.room_id),
                start_intent_key(intent.room_id, intent.game_id),
                start_phase_key(intent.room_id, intent.game_id),
                RedisKeyspace.room_closed(intent.room_id),
                completion_pending_key(intent.room_id, intent.game_id),
            ),
            args=(
                intent.room_id,
                intent.game_id,
                intent.to_json(),
                command.phase_wire,
                intent.fingerprint,
                command.expected_room_version,
                command.final_turn_no,
                command.end_reason,
                command.ended_at_ms,
                ROOM_REQUEST_DEDUPE_TTL_MS,
                command.pending_wire or "",
            ),
        )
        result = RedisRoomRuntimeAdapter._result(raw)
        RedisRoomRuntimeAdapter._raise_rejection(result)
        changed = result.get("changed")
        if type(changed) is not bool:
            raise RoomRuleViolation("START_COMPLETION_INVALID")
        return changed
