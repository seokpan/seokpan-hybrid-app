"""Subscription setup failures survive Redis cleanup errors before ownership transfer."""

import asyncio
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from seokpan.persistence.redis.realtime_adapter import (
    RealtimeUnavailable,
    RedisRealtimeEventAdapter,
)


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


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["lobby", "room"])
@pytest.mark.parametrize("fail_at", ["subscribe", "version", "invalid_version"])
@pytest.mark.parametrize("close_fails", [False, True])
async def test_failed_subscription_setup_keeps_provider_error_contract(
    scope: str, fail_at: str, close_fails: bool
) -> None:
    subscribe_error = RedisError("synthetic subscribe detail") if fail_at == "subscribe" else None
    version_error = RedisError("synthetic version detail") if fail_at == "version" else None
    close_error = RedisError("synthetic cleanup detail") if close_fails else None
    pubsub = SimpleNamespace(
        subscribe=AsyncMock(side_effect=subscribe_error),
        aclose=AsyncMock(side_effect=close_error),
    )
    client = SimpleNamespace(
        pubsub=lambda: pubsub,
        get=AsyncMock(
            side_effect=version_error,
            return_value=b"invalid" if fail_at == "invalid_version" else b"1",
        ),
    )
    adapter = RedisRealtimeEventAdapter(cast(Redis, client))
    operation = adapter.subscribe_lobby() if scope == "lobby" else adapter.subscribe_room("room-1")

    with pytest.raises(RealtimeUnavailable, match="^REALTIME_PROVIDER_UNAVAILABLE$") as caught:
        await operation

    assert caught.value.code == "REALTIME_PROVIDER_UNAVAILABLE"
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__
    pubsub.aclose.assert_awaited_once()
    assert client.get.await_count == (0 if fail_at == "subscribe" else 1)
