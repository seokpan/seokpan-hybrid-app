from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta, timezone
from typing import cast
from uuid import uuid1, uuid4

import pytest

from seokpan.chat import (
    ChatDeliveryUnavailable,
    ChatRuleViolation,
    ChatScope,
    ChatScopeType,
    ChatSender,
    ChatSubscription,
    ChatSubscriptionClosed,
    SendChat,
)
from seokpan.identity.application import SessionActorType
from seokpan.persistence.memory.chat_adapter import InMemoryChatAdapter
from seokpan.persistence.memory.realtime_adapter import InMemoryRealtimeEventAdapter

LOBBY = ChatScope(ChatScopeType.LOBBY)
ROOM = ChatScope(ChatScopeType.ROOM, str(uuid4()))
OTHER_ROOM = ChatScope(ChatScopeType.ROOM, str(uuid4()))
MEMBER = ChatSender(SessionActorType.MEMBER, "석판_1")
GUEST = ChatSender(SessionActorType.GUEST, "Guest-0001")
INSTANT = datetime(2026, 9, 8, tzinfo=UTC)


def command(scope: ChatScope = LOBBY, text: str = "안녕하세요") -> SendChat:
    return SendChat(scope, MEMBER, "a" * 64, str(uuid4()), text)


async def assert_empty(subscription: ChatSubscription) -> None:
    # A cancelled pending receive must not consume a later message.
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(subscription.receive(), 0.01)


@pytest.mark.parametrize("text", ["", " \t\n", "가" * 201, "😀" * 201, "a\ud800"])
def test_invalid_text_is_rejected_without_content_in_error(text: str) -> None:
    with pytest.raises(ChatRuleViolation, match="^INVALID_CHAT_TEXT$"):
        command(text=text)


@pytest.mark.parametrize("text", ["가", "가" * 200, "😀" * 200, "<script>alert(1)</script>"])
def test_text_is_plain_content_and_trimmed_by_unicode_codepoint(text: str) -> None:
    value = command(text=f" \t{text}\n ")
    assert value.text == text
    assert text not in repr(value)
    assert value.sender_key not in repr(value)


@pytest.mark.parametrize(
    ("kind", "room_id"),
    [
        (ChatScopeType.LOBBY, str(uuid4())),
        (ChatScopeType.ROOM, None),
        (ChatScopeType.ROOM, "not-a-room"),
        (ChatScopeType.ROOM, str(uuid1())),
        ("ROOM", str(uuid4())),
    ],
)
def test_invalid_scope_is_rejected(kind: ChatScopeType, room_id: str | None) -> None:
    with pytest.raises(ChatRuleViolation, match="INVALID_CHAT_SCOPE"):
        ChatScope(kind, room_id)


@pytest.mark.parametrize(
    ("actor_type", "name"),
    [
        (SessionActorType.MEMBER, " x "),
        (SessionActorType.MEMBER, " 정상 "),
        (SessionActorType.MEMBER, "<script>"),
        (SessionActorType.GUEST, "Guest-ABCD"),
        (SessionActorType.GUEST, "Guest-1"),
        ("MEMBER", "정상"),
    ],
)
def test_sender_uses_existing_member_and_guest_name_rules(
    actor_type: SessionActorType, name: str
) -> None:
    with pytest.raises(ChatRuleViolation, match="INVALID_CHAT_SENDER"):
        ChatSender(actor_type, name)


def test_invalid_request_key_and_non_string_input() -> None:
    original = command()
    with pytest.raises(ChatRuleViolation, match="INVALID_REQUEST_ID"):
        replace(original, request_id=str(uuid1()))
    with pytest.raises(ChatRuleViolation, match="INVALID_CHAT_SENDER_KEY"):
        replace(original, sender_key="raw-session-cookie")
    with pytest.raises(ChatRuleViolation, match="INVALID_CHAT_TEXT"):
        replace(original, text=cast(str, 3))


@pytest.mark.asyncio
async def test_lobby_and_each_room_have_only_their_live_messages() -> None:
    adapter = InMemoryChatAdapter(now=lambda: INSTANT)
    lobby = await adapter.subscribe(LOBBY)
    room = await adapter.subscribe(ROOM)
    other = await adapter.subscribe(OTHER_ROOM)
    second_room_peer = await adapter.subscribe(ROOM)
    sent = command(ROOM)
    receipt = await adapter.publish(sent)
    first = await room.receive()
    assert first == await second_room_peer.receive()
    assert first.message_id == receipt.message_id
    assert first.occurred_at == "2026-09-08T00:00:00Z"
    assert first.scope == ROOM and first.sender == MEMBER and first.text == sent.text
    assert set(asdict(first)) == {"message_id", "occurred_at", "scope", "sender", "text"}
    assert sent.sender_key not in repr(asdict(first))
    assert "안녕하세요" not in repr(first)
    await assert_empty(lobby)
    await assert_empty(other)
    await adapter.publish(replace(command(), sender=GUEST))
    assert (await lobby.receive()).sender == GUEST
    await assert_empty(room)
    for subscription in (lobby, room, other, second_room_peer):
        await subscription.close()


@pytest.mark.asyncio
async def test_subscribe_reconnect_and_retry_never_replay_history() -> None:
    adapter = InMemoryChatAdapter()
    first = command()
    await adapter.publish(first)
    subscriber = await adapter.subscribe(LOBBY)
    await adapter.publish(first)
    await assert_empty(subscriber)
    second = command(text="두 번째")
    await adapter.publish(second)
    await subscriber.close()  # pending message is discarded on leaving
    with pytest.raises(ChatSubscriptionClosed, match="CLOSED"):
        await subscriber.receive()
    renewed = await adapter.subscribe(LOBBY)
    await assert_empty(renewed)
    await adapter.publish(command(text="다시 접속한 이후"))
    assert (await renewed.receive()).text == "다시 접속한 이후"
    await renewed.close()


@pytest.mark.asyncio
async def test_duplicate_requests_broadcast_once_even_when_scheduled_together() -> None:
    adapter = InMemoryChatAdapter()
    subscriber = await adapter.subscribe(ROOM)
    sent = command(ROOM)
    receipts = await asyncio.gather(*(adapter.publish(sent) for _ in range(10)))
    assert sum(not receipt.replayed for receipt in receipts) == 1
    assert len({receipt.message_id for receipt in receipts}) == 1
    assert (await subscriber.receive()).message_id == receipts[0].message_id
    await assert_empty(subscriber)
    await subscriber.close()


@pytest.mark.asyncio
async def test_request_reuse_checks_scope_body_sender_but_not_trimmed_whitespace() -> None:
    adapter = InMemoryChatAdapter()
    subscriber = await adapter.subscribe(LOBBY)
    sent = command()
    original = await adapter.publish(sent)
    assert (await adapter.publish(replace(sent, text=f" {sent.text} "))).replayed
    for changed in (
        replace(sent, scope=ROOM),
        replace(sent, text="다른 말"),
        replace(sent, sender=GUEST),
    ):
        with pytest.raises(ChatRuleViolation, match="REQUEST_ID_REUSED"):
            await adapter.publish(changed)
    assert (await subscriber.receive()).message_id == original.message_id
    # Another authenticated session does not collide with this request ID.
    other = await adapter.publish(replace(sent, sender_key="b" * 64))
    assert not other.replayed and other.message_id != original.message_id
    assert (await subscriber.receive()).message_id == other.message_id
    await assert_empty(subscriber)
    await subscriber.close()


@pytest.mark.asyncio
async def test_full_retry_cache_rejects_new_send_without_evicting_live_receipt() -> None:
    clock = [0.0]
    adapter = InMemoryChatAdapter(
        max_recent_requests=1, retry_window_seconds=2, monotonic=lambda: clock[0]
    )
    subscriber = await adapter.subscribe(LOBBY)
    first = command()
    receipt = await adapter.publish(first)
    await subscriber.receive()
    with pytest.raises(ChatDeliveryUnavailable, match="CHAT_RETRY_CAPACITY_REACHED"):
        await adapter.publish(command())
    assert (await adapter.publish(first)).replayed
    await assert_empty(subscriber)
    clock[0] = 2
    # This is deliberately not an unlimited idempotency guarantee.
    after_expiry = await adapter.publish(first)
    assert not after_expiry.replayed and after_expiry.message_id != receipt.message_id
    assert (await subscriber.receive()).message_id == after_expiry.message_id
    await subscriber.close()


@pytest.mark.asyncio
async def test_slow_peer_is_closed_without_blocking_other_scopes_or_peers() -> None:
    adapter = InMemoryChatAdapter(max_queue_size=1)
    slow = await adapter.subscribe(ROOM)
    fast = await adapter.subscribe(ROOM)
    lobby = await adapter.subscribe(LOBBY)
    for _ in range(3):
        receipt = await adapter.publish(command(ROOM))
        assert (await fast.receive()).message_id == receipt.message_id
    with pytest.raises(ChatSubscriptionClosed, match="SLOW_CONSUMER"):
        await slow.receive()
    await assert_empty(lobby)
    await slow.close()
    await fast.close()
    await lobby.close()


@pytest.mark.asyncio
async def test_close_wakes_waiter_and_is_idempotent() -> None:
    adapter = InMemoryChatAdapter()
    subscriber = await adapter.subscribe(LOBBY)
    pending = asyncio.create_task(subscriber.receive())
    await asyncio.sleep(0)
    with pytest.raises(ChatDeliveryUnavailable, match="CHAT_RECEIVER_ALREADY_WAITING"):
        await subscriber.receive()
    await subscriber.close()
    await subscriber.close()
    with pytest.raises(ChatSubscriptionClosed, match="CLOSED"):
        await asyncio.wait_for(pending, 1)
    await adapter.publish(command())
    with pytest.raises(ChatSubscriptionClosed, match="CLOSED"):
        await subscriber.receive()


@pytest.mark.asyncio
async def test_close_after_offer_does_not_deliver_to_departed_subscription() -> None:
    adapter = InMemoryChatAdapter()
    subscriber = await adapter.subscribe(LOBBY)
    pending = asyncio.create_task(subscriber.receive())
    await asyncio.sleep(0)
    await adapter.publish(command())
    await subscriber.close()
    with pytest.raises(ChatSubscriptionClosed):
        await pending


@pytest.mark.asyncio
async def test_chat_does_not_change_game_or_lobby_recovery_versions() -> None:
    state_events = InMemoryRealtimeEventAdapter()
    state_subscription = await state_events.subscribe_lobby()
    adapter = InMemoryChatAdapter()
    await adapter.publish(command())
    await adapter.publish(command(ROOM))
    assert state_events.lobby_version == 1
    assert state_events.room_version(str(ROOM.room_id)) == 1
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(state_subscription.receive(), 0.01)
    await state_subscription.close()


@pytest.mark.asyncio
async def test_timestamp_uses_utc_and_naive_clock_fails_without_queued_message() -> None:
    current = [INSTANT.replace(tzinfo=None)]
    adapter = InMemoryChatAdapter(now=lambda: current[0])
    subscriber = await adapter.subscribe(LOBBY)
    sent = command()
    with pytest.raises(ChatDeliveryUnavailable, match="CHAT_CLOCK_UNAVAILABLE"):
        await adapter.publish(sent)
    await assert_empty(subscriber)
    current[0] = INSTANT.astimezone(timezone(timedelta(hours=9)))
    receipt = await adapter.publish(sent)
    assert receipt.occurred_at == "2026-09-08T00:00:00Z" and not receipt.replayed
    await subscriber.close()


def test_fake_rejects_cross_loop_access_explicitly() -> None:
    adapter = InMemoryChatAdapter()
    asyncio.run(adapter.publish(command()))
    with pytest.raises(ChatDeliveryUnavailable, match="CHAT_LOOP_MISMATCH"):
        asyncio.run(adapter.publish(command()))


@pytest.mark.parametrize("limit", [0, -1, True])
def test_invalid_delivery_capacity(limit: int) -> None:
    with pytest.raises(ValueError, match="INVALID_CHAT_DELIVERY_LIMIT"):
        InMemoryChatAdapter(max_queue_size=limit)
    with pytest.raises(ValueError, match="INVALID_CHAT_DELIVERY_LIMIT"):
        InMemoryChatAdapter(max_recent_requests=limit)


@pytest.mark.parametrize("window", [0.0, -1.0, float("nan"), float("inf")])
def test_invalid_retry_window(window: float) -> None:
    with pytest.raises(ValueError, match="INVALID_CHAT_DELIVERY_LIMIT"):
        InMemoryChatAdapter(retry_window_seconds=window)
