"""Bounded in-memory realtime streams for Headless tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from uuid import uuid4

from seokpan.room.application.realtime import RealtimeEvent, RealtimeSubscription


class _Subscription:
    def __init__(
        self,
        *,
        max_queue_size: int,
        remove: Callable[[_Subscription], None],
        scope_room_id: str | None,
    ) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue: asyncio.Queue[RealtimeEvent] = asyncio.Queue(maxsize=max_queue_size)
        self._remove = remove
        self._scope_room_id = scope_room_id
        self._closed = False

    async def receive(self) -> RealtimeEvent:
        return await self._queue.get()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._remove(self)

    def offer(self, event: RealtimeEvent) -> None:
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is not self._loop:
            self._loop.call_soon_threadsafe(self._offer, event)
            return
        self._offer(event)

    def _offer(self, event: RealtimeEvent) -> None:
        if self._closed:
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            while not self._queue.empty():
                self._queue.get_nowait()
            self._queue.put_nowait(
                RealtimeEvent(
                    event_type="snapshot.required",
                    event_id=str(uuid4()),
                    occurred_at=_occurred_at(),
                    state_version=event.state_version,
                    room_id=self._scope_room_id,
                    payload={"reason": "SLOW_CONSUMER"},
                )
            )


class InMemoryRealtimeEventAdapter:
    """A process-local Fake; it is not Redis Pub/Sub or multi-Replica evidence."""

    def __init__(self, *, max_queue_size: int = 100) -> None:
        if max_queue_size < 1:
            raise ValueError("INVALID_REALTIME_QUEUE_SIZE")
        self._max_queue_size = max_queue_size
        self._lobby_version = 1
        self._room_versions: dict[str, int] = {}
        self._published: dict[tuple[str, str], RealtimeEvent] = {}
        self._lobby_subscriptions: set[_Subscription] = set()
        self._room_subscriptions: dict[str, set[_Subscription]] = {}

    @property
    def lobby_version(self) -> int:
        return self._lobby_version

    def room_version(self, room_id: str) -> int:
        return self._room_versions.get(room_id, 1)

    async def current_lobby_version(self) -> int:
        return self.lobby_version

    async def current_room_version(self, room_id: str) -> int:
        return self.room_version(room_id)

    async def subscribe_lobby(self) -> RealtimeSubscription:
        subscription = _Subscription(
            max_queue_size=self._max_queue_size,
            remove=self._lobby_subscriptions.discard,
            scope_room_id=None,
        )
        self._lobby_subscriptions.add(subscription)
        return subscription

    async def subscribe_room(self, room_id: str) -> RealtimeSubscription:
        subscriptions = self._room_subscriptions.setdefault(room_id, set())

        def remove(subscription: _Subscription) -> None:
            subscriptions.discard(subscription)
            if not subscriptions:
                self._room_subscriptions.pop(room_id, None)

        subscription = _Subscription(
            max_queue_size=self._max_queue_size,
            remove=remove,
            scope_room_id=room_id,
        )
        subscriptions.add(subscription)
        return subscription

    async def lobby_rooms_changed(
        self,
        payload: Mapping[str, object],
        *,
        event_key: str | None = None,
    ) -> None:
        cache_key = None if event_key is None else ("lobby", event_key)
        event = None if cache_key is None else self._published.get(cache_key)
        if event is None:
            self._lobby_version += 1
            event = RealtimeEvent(
                event_type="lobby.rooms_changed",
                event_id=str(uuid4()),
                occurred_at=_occurred_at(),
                state_version=self._lobby_version,
                payload=dict(payload),
            )
            if cache_key is not None:
                self._published[cache_key] = event
        for subscription in tuple(self._lobby_subscriptions):
            subscription.offer(event)

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
        cache_key = None if event_key is None else (f"room:{room_id}", event_key)
        event = None if cache_key is None else self._published.get(cache_key)
        if event is None:
            version = self.room_version(room_id) + 1
            self._room_versions[room_id] = version
            event = RealtimeEvent(
                event_type=event_type,
                event_id=str(uuid4()),
                occurred_at=_occurred_at(),
                state_version=version,
                room_id=room_id,
                game_id=game_id,
                turn_no=turn_no,
                payload=dict(payload),
            )
            if cache_key is not None:
                self._published[cache_key] = event
        for subscription in tuple(self._room_subscriptions.get(room_id, ())):
            subscription.offer(event)


def _occurred_at() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
