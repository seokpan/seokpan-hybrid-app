"""Chat subscription setup retains ownership until the reader is returned."""

import asyncio
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from seokpan.chat import ChatDeliveryUnavailable, ChatScope, ChatScopeType, ChatSubscription
from seokpan.persistence.redis.chat_adapter import RedisChatAdapter, _RedisChatSubscription


def chat_scope(scope: str) -> ChatScope:
    return (
        ChatScope(ChatScopeType.LOBBY)
        if scope == "lobby"
        else ChatScope(ChatScopeType.ROOM, "d9c84b1e-a095-48f3-b653-81d08f6f4d12")
    )


def cleanup_error(kind: str) -> Exception | None:
    if kind == "redis":
        return RedisError("synthetic cleanup failure")
    if kind == "runtime":
        return RuntimeError("synthetic cleanup failure")
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["lobby", "room"])
@pytest.mark.parametrize("close_kind", ["none", "redis", "runtime"])
async def test_cancelled_chat_subscription_setup_closes_untransferred_pubsub(
    scope: str, close_kind: str
) -> None:
    entered = asyncio.Event()
    blocked = asyncio.Event()

    async def subscribe(_channel: str) -> None:
        entered.set()
        await blocked.wait()

    close_error = cleanup_error(close_kind)
    pubsub = SimpleNamespace(subscribe=subscribe, aclose=AsyncMock(side_effect=close_error))
    client = SimpleNamespace(pubsub=lambda: pubsub)
    task = asyncio.create_task(RedisChatAdapter(cast(Redis, client)).subscribe(chat_scope(scope)))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()
        pubsub.aclose.assert_awaited_once()
    finally:
        blocked.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["lobby", "room"])
@pytest.mark.parametrize("close_kind", ["none", "redis", "runtime"])
async def test_failed_chat_subscription_setup_keeps_public_error(
    scope: str, close_kind: str
) -> None:
    close_error = cleanup_error(close_kind)
    pubsub = SimpleNamespace(
        subscribe=AsyncMock(side_effect=RedisError("synthetic subscribe detail")),
        aclose=AsyncMock(side_effect=close_error),
    )
    client = SimpleNamespace(pubsub=lambda: pubsub)
    with pytest.raises(ChatDeliveryUnavailable, match="^CHAT_DELIVERY_UNAVAILABLE$") as caught:
        await RedisChatAdapter(cast(Redis, client)).subscribe(chat_scope(scope))
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__
    pubsub.aclose.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["lobby", "room"])
async def test_successful_chat_setup_transfers_pubsub_to_reader(scope: str) -> None:
    entered = asyncio.Event()
    blocked = asyncio.Event()

    async def get_message(**_kwargs: object) -> None:
        entered.set()
        await blocked.wait()

    pubsub = SimpleNamespace(subscribe=AsyncMock(), get_message=get_message, aclose=AsyncMock())
    client = SimpleNamespace(pubsub=lambda: pubsub)
    subscription = await RedisChatAdapter(cast(Redis, client)).subscribe(chat_scope(scope))
    assert isinstance(subscription, _RedisChatSubscription)
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        pubsub.aclose.assert_not_awaited()
        await subscription.close()
        assert subscription._reader.done()
        pubsub.aclose.assert_awaited_once()
    finally:
        blocked.set()
        await subscription.close()
        await asyncio.gather(subscription._reader, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["lobby", "room"])
@pytest.mark.parametrize("close_kind", ["none", "runtime"])
async def test_unexpected_chat_setup_failure_still_closes_owned_pubsub(
    scope: str, close_kind: str
) -> None:
    original = RuntimeError("synthetic setup failure")
    pubsub = SimpleNamespace(
        subscribe=AsyncMock(side_effect=original),
        aclose=AsyncMock(side_effect=cleanup_error(close_kind)),
    )
    client = SimpleNamespace(pubsub=lambda: pubsub)
    with pytest.raises(RuntimeError) as caught:
        await RedisChatAdapter(cast(Redis, client)).subscribe(chat_scope(scope))
    assert caught.value is original
    pubsub.aclose.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["lobby", "room"])
@pytest.mark.parametrize("close_kind", ["none", "runtime"])
async def test_chat_reader_construction_failure_keeps_pubsub_owned(
    monkeypatch: pytest.MonkeyPatch, scope: str, close_kind: str
) -> None:
    original = RuntimeError("synthetic construction failure")
    pubsub = SimpleNamespace(
        subscribe=AsyncMock(), aclose=AsyncMock(side_effect=cleanup_error(close_kind))
    )
    client = SimpleNamespace(pubsub=lambda: pubsub)

    def fail(*_args: object, **_kwargs: object) -> ChatSubscription:
        raise original

    monkeypatch.setattr("seokpan.persistence.redis.chat_adapter._RedisChatSubscription", fail)
    with pytest.raises(RuntimeError) as caught:
        await RedisChatAdapter(cast(Redis, client)).subscribe(chat_scope(scope))
    assert caught.value is original
    pubsub.aclose.assert_awaited_once()
