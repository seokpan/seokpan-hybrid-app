from __future__ import annotations

import asyncio
from typing import cast
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from redis.asyncio.client import PubSub
from redis.exceptions import RedisError

from seokpan.chat import (
    ChatDeliveryUnavailable,
    ChatMessage,
    ChatRuleViolation,
    ChatScope,
    ChatScopeType,
    ChatSender,
    ChatSubscriptionClosed,
    SendChat,
)
from seokpan.identity.application import SessionActorType
from seokpan.persistence.redis.chat_adapter import (
    RedisChatAdapter,
    _message,
    _RedisChatSubscription,
)
from seokpan.persistence.redis.chat_scripts import PUBLISH_CHAT
from seokpan.persistence.redis.common import VersionedJsonCodec


class ScriptedClient:
    def __init__(self, result: object) -> None:
        self.result = result
        self.call: tuple[str, int, tuple[object, ...]] | None = None

    async def evalsha(self, sha: str, numkeys: int, *values: object) -> object:
        self.call = (sha, numkeys, values)
        return self.result

    async def script_load(self, script: str) -> bytes | str:
        raise AssertionError(script)


def command() -> SendChat:
    return SendChat(
        scope=ChatScope(ChatScopeType.LOBBY),
        sender=ChatSender(SessionActorType.GUEST, "Guest-0042"),
        sender_key="a" * 64,
        request_id=str(uuid4()),
        text=" hello ",
    )


def result(*, ok: bool, value: object = None, error: object = None) -> str:
    return VersionedJsonCodec.encode({"ok": ok, "value": value, "error": error})


@pytest.mark.asyncio
async def test_publish_uses_scope_keys_and_returns_receipt() -> None:
    response = {
        "message_id": str(uuid4()),
        "occurred_at": "2026-09-14T00:00:00Z",
        "replayed": False,
    }
    client = ScriptedClient(result(ok=True, value=response))
    receipt = await RedisChatAdapter(
        cast(Redis, client), max_recent_requests=8, retry_window_seconds=2
    ).publish(command())

    assert receipt.message_id == response["message_id"]
    assert not receipt.replayed
    assert client.call is not None
    sha, numkeys, values = client.call
    assert sha == PUBLISH_CHAT.sha
    assert numkeys == 3
    assert values[:3] == (
        "stone:v1:chat:{lobby}:receipts",
        "stone:v1:chat:{lobby}:receipt-expiries",
        "stone:v1:chat:{lobby}:messages",
    )
    assert values[5] == 8
    assert values[8] == 2000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "exception"),
    (
        ("REQUEST_ID_REUSED", ChatRuleViolation),
        ("CHAT_RETRY_CAPACITY_REACHED", ChatDeliveryUnavailable),
    ),
)
async def test_publish_maps_known_rejections(error: str, exception: type[Exception]) -> None:
    client = ScriptedClient(result(ok=False, error=error))
    with pytest.raises(exception, match=error):
        await RedisChatAdapter(cast(Redis, client)).publish(command())


@pytest.mark.asyncio
async def test_publish_sanitizes_provider_failure() -> None:
    class FailingClient(ScriptedClient):
        async def evalsha(self, sha: str, numkeys: int, *values: object) -> object:
            raise RedisError("provider details")

    with pytest.raises(ChatDeliveryUnavailable, match="REDIS_PROVIDER_UNAVAILABLE"):
        await RedisChatAdapter(cast(Redis, FailingClient(None))).publish(command())


def test_pubsub_message_is_validated() -> None:
    message_id = str(uuid4())
    value = VersionedJsonCodec.encode(
        {
            "message_id": message_id,
            "occurred_at": "2026-09-14T00:00:00Z",
            "scope": {"kind": "LOBBY", "room_id": None},
            "sender": {"actor_type": "GUEST", "display_name": "Guest-0042"},
            "text": "hello",
        }
    )
    assert _message(value).message_id == message_id
    with pytest.raises(ChatDeliveryUnavailable, match="CHAT_DELIVERY_RESPONSE_INVALID"):
        _message(b"{}")


class ControlledPubSub:
    def __init__(self, data: object) -> None:
        self.data = data
        self.requested = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = asyncio.Event()

    async def get_message(
        self,
        *,
        ignore_subscribe_messages: bool,
        timeout: object,
    ) -> dict[str, object]:
        assert ignore_subscribe_messages is True
        assert timeout is None
        self.requested.set()
        await self.release.wait()
        return {"data": self.data}

    async def aclose(self) -> None:
        self.closed.set()


@pytest.mark.asyncio
async def test_invalid_pubsub_message_closes_waiting_receive() -> None:
    pubsub = ControlledPubSub(b"{}")
    subscription = _RedisChatSubscription(
        cast(PubSub, pubsub),
        max_queue_size=1,
    )

    await asyncio.wait_for(pubsub.requested.wait(), timeout=1.0)

    receiver_started = asyncio.Event()

    async def receive() -> object:
        receiver_started.set()
        return await subscription.receive()

    receiver = asyncio.create_task(receive())
    await receiver_started.wait()

    pubsub.release.set()

    try:
        with pytest.raises(ChatSubscriptionClosed):
            await asyncio.wait_for(receiver, timeout=1.0)
    finally:
        await subscription.close()
        if subscription._reader.done() and not subscription._reader.cancelled():
            subscription._reader.exception()


@pytest.mark.asyncio
async def test_close_preserves_unavailable_reason() -> None:
    pubsub = ControlledPubSub(b"{}")
    subscription = _RedisChatSubscription(
        cast(PubSub, pubsub),
        max_queue_size=1,
    )

    await asyncio.wait_for(pubsub.requested.wait(), timeout=1.0)

    pubsub.release.set()

    await asyncio.wait_for(
        asyncio.shield(subscription._reader),
        timeout=1.0,
    )

    await subscription.close()

    with pytest.raises(ChatSubscriptionClosed, match="UNAVAILABLE"):
        await subscription.receive()


class PausingReceiveQueue(asyncio.Queue[ChatMessage | None]):
    def __init__(self) -> None:
        super().__init__(maxsize=1)
        self.taken = asyncio.Event()
        self.release = asyncio.Event()

    async def get(self) -> ChatMessage | None:
        message = await super().get()
        self.taken.set()
        await self.release.wait()
        return message


@pytest.mark.asyncio
async def test_close_does_not_deliver_message_after_shutdown() -> None:
    pubsub = ControlledPubSub(b"{}")
    subscription = _RedisChatSubscription(
        cast(PubSub, pubsub),
        max_queue_size=1,
    )

    await asyncio.wait_for(pubsub.requested.wait(), timeout=1.0)

    queue = PausingReceiveQueue()
    subscription._queue = queue

    queue.put_nowait(
        _message(
            VersionedJsonCodec.encode(
                {
                    "message_id": str(uuid4()),
                    "occurred_at": "2026-09-14T00:00:00Z",
                    "scope": {"kind": "LOBBY", "room_id": None},
                    "sender": {
                        "actor_type": "GUEST",
                        "display_name": "Guest-0042",
                    },
                    "text": "hello",
                }
            )
        )
    )

    receiver = asyncio.create_task(subscription.receive())
    await asyncio.wait_for(queue.taken.wait(), timeout=1.0)

    close_task = asyncio.create_task(subscription.close())
    await asyncio.wait_for(pubsub.closed.wait(), timeout=1.0)

    queue.release.set()

    with pytest.raises(ChatSubscriptionClosed, match="CLOSED"):
        await asyncio.wait_for(receiver, timeout=1.0)

    await asyncio.wait_for(close_task, timeout=1.0)


class BlockingClosePubSub(ControlledPubSub):
    def __init__(self) -> None:
        super().__init__(b"{}")
        self.close_started = asyncio.Event()
        self.allow_close = asyncio.Event()
        self.close_finished = asyncio.Event()

    async def aclose(self) -> None:
        self.close_started.set()
        await self.allow_close.wait()
        self.close_finished.set()
        self.closed.set()


@pytest.mark.asyncio
async def test_close_waits_for_reader_and_pubsub_cleanup() -> None:
    pubsub = BlockingClosePubSub()
    subscription = _RedisChatSubscription(
        cast(PubSub, pubsub),
        max_queue_size=1,
    )

    await asyncio.wait_for(pubsub.requested.wait(), timeout=1.0)

    close_task = asyncio.create_task(subscription.close())

    await asyncio.wait_for(pubsub.close_started.wait(), timeout=1.0)

    assert not close_task.done()
    assert not pubsub.close_finished.is_set()

    pubsub.allow_close.set()

    await asyncio.wait_for(close_task, timeout=1.0)

    assert subscription._reader.done()
    assert pubsub.close_finished.is_set()


@pytest.mark.asyncio
async def test_close_does_not_hide_caller_cancellation() -> None:
    pubsub = BlockingClosePubSub()
    subscription = _RedisChatSubscription(
        cast(PubSub, pubsub),
        max_queue_size=1,
    )

    await asyncio.wait_for(pubsub.requested.wait(), timeout=1.0)

    close_task = asyncio.create_task(subscription.close())

    await asyncio.wait_for(pubsub.close_started.wait(), timeout=1.0)

    close_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await close_task

    pubsub.allow_close.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(
            asyncio.shield(subscription._reader),
            timeout=1.0,
        )

    assert subscription._reader.done()
    assert pubsub.close_finished.is_set()


@pytest.mark.asyncio
async def test_close_before_reader_starts_closes_pubsub() -> None:
    pubsub = ControlledPubSub(b"{}")
    subscription = _RedisChatSubscription(
        cast(PubSub, pubsub),
        max_queue_size=1,
    )

    await subscription.close()

    assert subscription._reader.done()
    assert pubsub.closed.is_set()


@pytest.mark.asyncio
async def test_close_preserves_slow_consumer_reason() -> None:
    value = VersionedJsonCodec.encode(
        {
            "message_id": str(uuid4()),
            "occurred_at": "2026-09-14T00:00:00Z",
            "scope": {"kind": "LOBBY", "room_id": None},
            "sender": {
                "actor_type": "GUEST",
                "display_name": "Guest-0042",
            },
            "text": "hello",
        }
    )
    pubsub = ControlledPubSub(value)
    subscription = _RedisChatSubscription(
        cast(PubSub, pubsub),
        max_queue_size=1,
    )

    await asyncio.wait_for(pubsub.requested.wait(), timeout=1.0)

    pubsub.release.set()

    await asyncio.wait_for(
        asyncio.shield(subscription._reader),
        timeout=1.0,
    )

    await subscription.close()

    with pytest.raises(ChatSubscriptionClosed, match="SLOW_CONSUMER"):
        await subscription.receive()


class CountingClosePubSub(ControlledPubSub):
    def __init__(self) -> None:
        super().__init__(b"{}")
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1
        await super().aclose()


@pytest.mark.asyncio
async def test_close_is_idempotent() -> None:
    pubsub = CountingClosePubSub()
    subscription = _RedisChatSubscription(
        cast(PubSub, pubsub),
        max_queue_size=1,
    )

    await asyncio.wait_for(pubsub.requested.wait(), timeout=1.0)

    await subscription.close()
    await subscription.close()

    assert subscription._reader.done()
    assert pubsub.closed.is_set()
    assert pubsub.close_calls == 1


class FailingClosePubSub(ControlledPubSub):
    def __init__(self, *, failures: int) -> None:
        super().__init__(b"{}")
        self.failures = failures
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1
        if self.close_calls <= self.failures:
            raise RedisError("pubsub close failed")
        await super().aclose()


@pytest.mark.asyncio
async def test_close_retries_pubsub_cleanup_after_reader_cleanup_failure() -> None:
    pubsub = FailingClosePubSub(failures=1)
    subscription = _RedisChatSubscription(
        cast(PubSub, pubsub),
        max_queue_size=1,
    )

    await asyncio.wait_for(pubsub.requested.wait(), timeout=1.0)

    await subscription.close()

    assert subscription._reader.done()
    assert pubsub.closed.is_set()
    assert pubsub.close_calls == 2
    assert subscription._pubsub_closed is True


@pytest.mark.asyncio
async def test_close_reports_pubsub_cleanup_failure() -> None:
    pubsub = FailingClosePubSub(failures=2)
    subscription = _RedisChatSubscription(
        cast(PubSub, pubsub),
        max_queue_size=1,
    )

    await asyncio.wait_for(pubsub.requested.wait(), timeout=1.0)

    with pytest.raises(RedisError, match="pubsub close failed"):
        await subscription.close()

    assert subscription._reader.done()
    assert not pubsub.closed.is_set()
    assert pubsub.close_calls == 2
    assert subscription._pubsub_closed is False


class SingleMessagePubSub:
    def __init__(self, data: object) -> None:
        self.data = data
        self.requested = asyncio.Event()
        self.delivered = asyncio.Event()
        self.closed = asyncio.Event()
        self._sent = False
        self._wait_forever = asyncio.Event()

    async def get_message(
        self,
        *,
        ignore_subscribe_messages: bool,
        timeout: object,
    ) -> dict[str, object]:
        assert ignore_subscribe_messages is True
        assert timeout is None
        self.requested.set()

        if not self._sent:
            self._sent = True
            self.delivered.set()
            return {"data": self.data}

        await self._wait_forever.wait()
        raise AssertionError("unreachable")

    async def aclose(self) -> None:
        self.closed.set()


@pytest.mark.asyncio
async def test_normal_message_and_new_subscription_receive() -> None:
    first_message_id = str(uuid4())
    first_value = VersionedJsonCodec.encode(
        {
            "message_id": first_message_id,
            "occurred_at": "2026-09-14T00:00:00Z",
            "scope": {"kind": "LOBBY", "room_id": None},
            "sender": {
                "actor_type": "GUEST",
                "display_name": "Guest-0042",
            },
            "text": "first",
        }
    )
    first_pubsub = SingleMessagePubSub(first_value)
    first_subscription = _RedisChatSubscription(
        cast(PubSub, first_pubsub),
        max_queue_size=1,
    )

    first_message = await asyncio.wait_for(
        first_subscription.receive(),
        timeout=1.0,
    )
    assert first_message.message_id == first_message_id

    await first_subscription.close()
    assert first_pubsub.closed.is_set()

    second_message_id = str(uuid4())
    second_value = VersionedJsonCodec.encode(
        {
            "message_id": second_message_id,
            "occurred_at": "2026-09-14T00:00:01Z",
            "scope": {"kind": "LOBBY", "room_id": None},
            "sender": {
                "actor_type": "GUEST",
                "display_name": "Guest-0042",
            },
            "text": "second",
        }
    )
    second_pubsub = SingleMessagePubSub(second_value)
    second_subscription = _RedisChatSubscription(
        cast(PubSub, second_pubsub),
        max_queue_size=1,
    )

    second_message = await asyncio.wait_for(
        second_subscription.receive(),
        timeout=1.0,
    )
    assert second_message.message_id == second_message_id

    await second_subscription.close()
    assert second_pubsub.closed.is_set()
