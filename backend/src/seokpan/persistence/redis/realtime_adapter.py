"""Redis-backed cross-replica realtime event stream."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

from redis.asyncio import Redis
from redis.asyncio.client import PubSub
from redis.exceptions import RedisError

from seokpan.persistence.redis.common import (
    LuaScriptRunner,
    RedisClient,
    RedisKeyspace,
    RedisProviderError,
    VersionedJsonCodec,
)
from seokpan.persistence.redis.realtime_scripts import PUBLISH_REALTIME
from seokpan.room.application.realtime import RealtimeEvent, RealtimeSubscription


class RealtimeUnavailable(RuntimeError):
    def __init__(self, code: str = "REALTIME_PROVIDER_UNAVAILABLE") -> None:
        self.code = code
        super().__init__(code)


class _RedisRealtimeSubscription:
    def __init__(
        self,
        pubsub: PubSub,
        update_version: Callable[[int], None],
        *,
        max_queue_size: int,
        room_id: str | None,
    ) -> None:
        self._pubsub = pubsub
        self._update_version = update_version
        self._queue: asyncio.Queue[RealtimeEvent | None] = asyncio.Queue(maxsize=max_queue_size)
        self._room_id = room_id
        self._closed = False
        self._receiving = False
        self._reader = asyncio.create_task(self._read())

    async def receive(self) -> RealtimeEvent:
        if self._receiving:
            raise RealtimeUnavailable("REALTIME_RECEIVER_ALREADY_WAITING")
        if self._closed and self._queue.empty():
            raise RealtimeUnavailable("REALTIME_SUBSCRIPTION_CLOSED")
        self._receiving = True
        try:
            event = await self._queue.get()
            if event is None:
                raise RealtimeUnavailable("REALTIME_SUBSCRIPTION_CLOSED")
            return event
        finally:
            self._receiving = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._reader.cancel()
        self._wake_closed()
        await self._close_pubsub()

    async def _read(self) -> None:
        try:
            while not self._closed:
                item = await self._pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=None,
                )
                if item is None:
                    continue
                event = _event(item.get("data"))
                self._update_version(event.state_version)
                try:
                    self._queue.put_nowait(event)
                except asyncio.QueueFull:
                    while not self._queue.empty():
                        self._queue.get_nowait()
                    self._queue.put_nowait(
                        RealtimeEvent(
                            event_type="snapshot.required",
                            event_id=str(uuid4()),
                            occurred_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                            state_version=event.state_version,
                            room_id=self._room_id,
                            payload={"reason": "SLOW_CONSUMER"},
                        )
                    )
                    self._closed = True
        except (RedisError, RealtimeUnavailable):
            self._closed = True
            while not self._queue.empty():
                self._queue.get_nowait()
            self._wake_closed()
        finally:
            await self._close_pubsub()

    def _wake_closed(self) -> None:
        if self._queue.empty():
            self._queue.put_nowait(None)

    async def _close_pubsub(self) -> None:
        try:
            await self._pubsub.aclose()  # type: ignore[no-untyped-call]
        except RedisError:
            return


class RedisRealtimeEventAdapter:
    def __init__(
        self,
        client: Redis,
        *,
        max_queue_size: int = 100,
        max_idempotency_keys: int = 4096,
        idempotency_seconds: float = 120,
    ) -> None:
        if (
            type(max_queue_size) is not int
            or max_queue_size < 1
            or type(max_idempotency_keys) is not int
            or max_idempotency_keys < 1
            or isinstance(idempotency_seconds, bool)
            or not isinstance(idempotency_seconds, (int, float))
            or not math.isfinite(idempotency_seconds)
            or idempotency_seconds <= 0
        ):
            raise ValueError("INVALID_REALTIME_CONFIGURATION")
        idempotency_ms = math.ceil(idempotency_seconds * 1000)
        if idempotency_ms > 2**53 - 1:
            raise ValueError("INVALID_REALTIME_CONFIGURATION")
        self._client = client
        self._scripts = LuaScriptRunner(cast(RedisClient, client))
        self._queue_limit = max_queue_size
        self._capacity = max_idempotency_keys
        self._idempotency_ms = idempotency_ms
        self._lobby_version = 1
        self._room_versions: dict[str, int] = {}

    @property
    def lobby_version(self) -> int:
        return self._lobby_version

    def room_version(self, room_id: str) -> int:
        return self._room_versions.get(room_id, 1)

    async def current_lobby_version(self) -> int:
        self._lobby_version = await self._read_version("lobby")
        return self._lobby_version

    async def current_room_version(self, room_id: str) -> int:
        scope = _room_scope(room_id)
        version = await self._read_version(scope)
        self._room_versions[room_id] = version
        return version

    async def subscribe_lobby(self) -> RealtimeSubscription:
        return await self._subscribe("lobby", self._set_lobby_version, room_id=None)

    async def subscribe_room(self, room_id: str) -> RealtimeSubscription:
        scope = _room_scope(room_id)
        return await self._subscribe(
            scope,
            lambda version: self._room_versions.__setitem__(room_id, version),
            room_id=room_id,
        )

    async def lobby_rooms_changed(
        self,
        payload: Mapping[str, object],
        *,
        event_key: str | None = None,
    ) -> None:
        event = await self._publish(
            scope="lobby",
            event_type="lobby.rooms_changed",
            payload=payload,
            event_key=event_key,
        )
        self._set_lobby_version(event.state_version)

    async def room_changed(
        self,
        *,
        event_type: str,
        room_id: str,
        payload: Mapping[str, object],
        event_key: str | None = None,
        game_id: str | None = None,
        turn_no: int | None = None,
    ) -> None:
        event = await self._publish(
            scope=_room_scope(room_id),
            event_type=event_type,
            payload=payload,
            event_key=event_key,
            room_id=room_id,
            game_id=game_id,
            turn_no=turn_no,
        )
        self._room_versions[room_id] = max(self.room_version(room_id), event.state_version)

    async def _subscribe(
        self,
        scope: str,
        update_version: Callable[[int], None],
        *,
        room_id: str | None,
    ) -> RealtimeSubscription:
        pubsub = self._client.pubsub()
        try:
            await pubsub.subscribe(RedisKeyspace.realtime_channel(scope))
            version = await self._read_version(scope)
            update_version(version)
        except (RedisError, RealtimeUnavailable):
            await pubsub.aclose()  # type: ignore[no-untyped-call]
            raise RealtimeUnavailable() from None
        return _RedisRealtimeSubscription(
            pubsub,
            update_version,
            max_queue_size=self._queue_limit,
            room_id=room_id,
        )

    async def _read_version(self, scope: str) -> int:
        try:
            raw = await self._client.get(RedisKeyspace.realtime_version(scope))
        except RedisError:
            raise RealtimeUnavailable() from None
        if raw is None:
            return 1
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise RealtimeUnavailable("REALTIME_RESPONSE_INVALID") from None
        if value < 1:
            raise RealtimeUnavailable("REALTIME_RESPONSE_INVALID")
        return value

    async def _publish(
        self,
        *,
        scope: str,
        event_type: str,
        payload: Mapping[str, object],
        event_key: str | None,
        room_id: str | None = None,
        game_id: str | None = None,
        turn_no: int | None = None,
    ) -> RealtimeEvent:
        draft = {
            "event_type": event_type,
            "schema_version": 1,
            "event_id": str(uuid4()),
            "occurred_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "state_version": 0,
            "room_id": room_id,
            "game_id": game_id,
            "turn_no": turn_no,
            "payload": dict(payload),
        }
        try:
            raw = await self._scripts.execute(
                PUBLISH_REALTIME,
                keys=(
                    RedisKeyspace.realtime_version(scope),
                    RedisKeyspace.realtime_events(scope),
                    RedisKeyspace.realtime_event_expiries(scope),
                    RedisKeyspace.realtime_channel(scope),
                ),
                args=(
                    "" if event_key is None else event_key,
                    self._capacity,
                    self._idempotency_ms,
                    VersionedJsonCodec.encode(draft),
                ),
            )
        except RedisProviderError as error:
            raise RealtimeUnavailable(error.code) from None
        result = _result(raw)
        if result["ok"] is False:
            if result.get("error") == "REALTIME_IDEMPOTENCY_CAPACITY_REACHED":
                raise RealtimeUnavailable("REALTIME_IDEMPOTENCY_CAPACITY_REACHED")
            raise RealtimeUnavailable("REALTIME_RESPONSE_INVALID")
        return _event_value(result.get("value"))

    def _set_lobby_version(self, version: int) -> None:
        self._lobby_version = max(self._lobby_version, version)


def _room_scope(room_id: str) -> str:
    if not isinstance(room_id, str) or not room_id:
        raise RealtimeUnavailable("REALTIME_SCOPE_INVALID")
    return f"room:{room_id}"


def _event(raw: object) -> RealtimeEvent:
    if not isinstance(raw, (bytes, str)):
        raise RealtimeUnavailable("REALTIME_RESPONSE_INVALID")
    try:
        return _event_value(VersionedJsonCodec.decode(raw))
    except RedisProviderError:
        raise RealtimeUnavailable("REALTIME_RESPONSE_INVALID") from None


def _event_value(value: object) -> RealtimeEvent:
    if not isinstance(value, dict):
        raise RealtimeUnavailable("REALTIME_RESPONSE_INVALID")
    item = cast(dict[str, object], value)
    try:
        payload = item["payload"]
        if not isinstance(payload, dict):
            raise TypeError
        event_type = _optional_string(item, "event_type", required=True)
        event_id = _optional_string(item, "event_id", required=True)
        occurred_at = _optional_string(item, "occurred_at", required=True)
        room_id = _optional_string(item, "room_id")
        game_id = _optional_string(item, "game_id")
        state_version = item["state_version"]
        turn_no = item["turn_no"]
        schema_version = item["schema_version"]
        if type(state_version) is not int or state_version < 1:
            raise TypeError
        if turn_no is not None and type(turn_no) is not int:
            raise TypeError
        if type(schema_version) is not int or schema_version != 1:
            raise TypeError
        return RealtimeEvent(
            cast(str, event_type),
            cast(str, event_id),
            cast(str, occurred_at),
            state_version,
            cast(dict[str, object], payload),
            room_id,
            game_id,
            turn_no,
            schema_version,
        )
    except (KeyError, TypeError):
        raise RealtimeUnavailable("REALTIME_RESPONSE_INVALID") from None


def _optional_string(value: dict[str, object], key: str, *, required: bool = False) -> str | None:
    item = value[key]
    if item is None and not required:
        return None
    if not isinstance(item, str) or (required and not item):
        raise TypeError(key)
    return item


def _result(raw: object) -> dict[str, object]:
    if not isinstance(raw, (bytes, str)):
        raise RealtimeUnavailable("REALTIME_RESPONSE_INVALID")
    try:
        result = VersionedJsonCodec.decode(raw)
    except RedisProviderError:
        raise RealtimeUnavailable("REALTIME_RESPONSE_INVALID") from None
    if not isinstance(result.get("ok"), bool):
        raise RealtimeUnavailable("REALTIME_RESPONSE_INVALID")
    return result
