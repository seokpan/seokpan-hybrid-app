"""Cancellation before PubSub ownership transfer survives Redis cleanup errors."""

import asyncio
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from seokpan.persistence.redis.realtime_adapter import RedisRealtimeEventAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["lobby", "room"])
@pytest.mark.parametrize("cancel_at", ["subscribe", "version"])
@pytest.mark.parametrize("close_fails", [False, True])
async def test_cancelled_subscription_setup_closes_untransferred_pubsub(
    scope: str, cancel_at: str, close_fails: bool
) -> None:
    entered = asyncio.Event()
    blocked = asyncio.Event()

    async def subscribe(_channel: str) -> None:
        if cancel_at == "subscribe":
            entered.set()
            await blocked.wait()

    async def get(_key: str) -> bytes:
        if cancel_at == "version":
            entered.set()
            await blocked.wait()
        return b"1"

    close_error = RedisError("synthetic cleanup failure") if close_fails else None
    pubsub = SimpleNamespace(subscribe=subscribe, aclose=AsyncMock(side_effect=close_error))
    client = SimpleNamespace(pubsub=lambda: pubsub, get=get)
    adapter = RedisRealtimeEventAdapter(cast(Redis, client))
    operation = adapter.subscribe_lobby() if scope == "lobby" else adapter.subscribe_room("room-1")
    task = asyncio.create_task(operation)
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()
        assert pubsub.aclose.await_count == 1, "cancelled setup must attempt PubSub close once"
    finally:
        blocked.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
