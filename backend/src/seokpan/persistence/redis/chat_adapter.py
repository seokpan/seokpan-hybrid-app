"""Redis Pub/Sub chat with bounded, atomic retry receipts and no history."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from contextlib import suppress
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

from redis.asyncio import Redis
from redis.asyncio.client import PubSub
from redis.exceptions import RedisError

from seokpan.chat import (
    ChatDeliveryUnavailable,
    ChatMessage,
    ChatReceipt,
    ChatRuleViolation,
    ChatScope,
    ChatScopeType,
    ChatSender,
    ChatSubscription,
    ChatSubscriptionClosed,
    SendChat,
)
from seokpan.identity.application import SessionActorType
from seokpan.persistence.redis.chat_scripts import PUBLISH_CHAT
from seokpan.persistence.redis.common import (
    LuaScriptRunner,
    RedisClient,
    RedisKeyspace,
    RedisProviderError,
    VersionedJsonCodec,
)


class _RedisChatSubscription:
    def __init__(self, pubsub: PubSub, *, max_queue_size: int) -> None:
        self._pubsub = pubsub
        self._queue: asyncio.Queue[ChatMessage | None] = asyncio.Queue(maxsize=max_queue_size)
        self._closed = False
        self._receiving = False
        self._close_reason = "CLOSED"
        self._pubsub_closed = False
        self._reader = asyncio.create_task(self._read())

    async def receive(self) -> ChatMessage:
        if self._receiving:
            raise ChatDeliveryUnavailable("CHAT_RECEIVER_ALREADY_WAITING")
        if self._closed:
            raise ChatSubscriptionClosed(self._close_reason)
        self._receiving = True
        try:
            message = await self._queue.get()
            if self._closed or message is None:
                raise ChatSubscriptionClosed(self._close_reason)
            return message
        finally:
            self._receiving = False

    async def close(self) -> None:
        self._stop("CLOSED")

        reader = self._reader
        if reader is asyncio.current_task():
            return

        if not reader.done():
            reader.cancel()

        caller = asyncio.current_task()
        cancelling_before = caller.cancelling() if caller is not None else 0

        try:
            await asyncio.shield(reader)
        except asyncio.CancelledError:
            if caller is not None and caller.cancelling() > cancelling_before:
                raise
            if not reader.cancelled():
                raise

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
                try:
                    self._queue.put_nowait(_message(item.get("data")))
                except asyncio.QueueFull:
                    self._stop("SLOW_CONSUMER")
        except (ChatDeliveryUnavailable, RedisError):
            self._stop("UNAVAILABLE")
        finally:
            # close() performs the final cleanup attempt and reports
            # failure if Pub/Sub still cannot be closed.
            with suppress(RedisError):
                await self._close_pubsub()

    def _stop(self, reason: str) -> None:
        if self._closed:
            return
        self._closed = True
        self._close_reason = reason
        while not self._queue.empty():
            self._queue.get_nowait()
        self._queue.put_nowait(None)

    async def _close_pubsub(self) -> None:
        if self._pubsub_closed:
            return
        await self._pubsub.aclose()  # type: ignore[no-untyped-call]
        self._pubsub_closed = True


class RedisChatAdapter:
    def __init__(
        self,
        client: Redis,
        *,
        max_queue_size: int = 100,
        max_recent_requests: int = 4096,
        retry_window_seconds: float = 120,
    ) -> None:
        if (
            type(max_queue_size) is not int
            or max_queue_size < 1
            or type(max_recent_requests) is not int
            or max_recent_requests < 1
            or isinstance(retry_window_seconds, bool)
            or not isinstance(retry_window_seconds, (int, float))
            or not math.isfinite(retry_window_seconds)
            or retry_window_seconds <= 0
        ):
            raise ValueError("INVALID_CHAT_DELIVERY_LIMIT")
        retry_ms = math.ceil(retry_window_seconds * 1000)
        if retry_ms > 2**53 - 1:
            raise ValueError("INVALID_CHAT_DELIVERY_LIMIT")
        self._client = client
        self._scripts = LuaScriptRunner(cast(RedisClient, client))
        self._queue_limit = max_queue_size
        self._recent_limit = max_recent_requests
        self._retry_ms = retry_ms

    async def subscribe(self, scope: ChatScope) -> ChatSubscription:
        if not isinstance(scope, ChatScope):
            raise ChatRuleViolation("INVALID_CHAT_SCOPE")
        pubsub = self._client.pubsub()
        try:
            await pubsub.subscribe(RedisKeyspace.chat_channel(_scope_key(scope)))
        except RedisError:
            await pubsub.aclose()  # type: ignore[no-untyped-call]
            raise ChatDeliveryUnavailable("CHAT_DELIVERY_UNAVAILABLE") from None
        return _RedisChatSubscription(pubsub, max_queue_size=self._queue_limit)

    async def publish(self, command: SendChat) -> ChatReceipt:
        if not isinstance(command, SendChat):
            raise ChatRuleViolation("INVALID_CHAT_COMMAND")
        scope_key = _scope_key(command.scope)
        message_id = str(uuid4())
        occurred_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        message = ChatMessage(
            message_id,
            occurred_at,
            command.scope,
            command.sender,
            command.text,
        )
        try:
            raw = await self._scripts.execute(
                PUBLISH_CHAT,
                keys=(
                    RedisKeyspace.chat_receipts(scope_key),
                    RedisKeyspace.chat_receipt_expiries(scope_key),
                    RedisKeyspace.chat_channel(scope_key),
                ),
                args=(
                    f"{command.sender_key}:{command.request_id}",
                    _fingerprint(command),
                    self._recent_limit,
                    message_id,
                    occurred_at,
                    self._retry_ms,
                    VersionedJsonCodec.encode(_message_value(message)),
                ),
            )
        except RedisProviderError as error:
            raise ChatDeliveryUnavailable(error.code) from None
        decoded = _result(raw)
        if decoded["ok"] is False:
            response_error = decoded.get("error")
            if response_error == "REQUEST_ID_REUSED":
                raise ChatRuleViolation(response_error)
            if response_error == "CHAT_RETRY_CAPACITY_REACHED":
                raise ChatDeliveryUnavailable(response_error)
            raise ChatDeliveryUnavailable("CHAT_DELIVERY_RESPONSE_INVALID")
        value = decoded.get("value")
        if not isinstance(value, dict):
            raise ChatDeliveryUnavailable("CHAT_DELIVERY_RESPONSE_INVALID")
        try:
            response_id = value["message_id"]
            response_time = value["occurred_at"]
            replayed = value["replayed"]
            if not isinstance(response_id, str) or not isinstance(response_time, str):
                raise TypeError
            if not isinstance(replayed, bool):
                raise TypeError
            return ChatReceipt(response_id, response_time, replayed)
        except (KeyError, TypeError):
            raise ChatDeliveryUnavailable("CHAT_DELIVERY_RESPONSE_INVALID") from None


def _scope_key(scope: ChatScope) -> str:
    return "lobby" if scope.kind is ChatScopeType.LOBBY else f"room:{scope.room_id}"


def _fingerprint(command: SendChat) -> str:
    value = (
        command.scope.kind,
        command.scope.room_id,
        command.sender.actor_type,
        command.sender.display_name,
        command.text,
    )
    return hashlib.sha256(json.dumps(value, ensure_ascii=True).encode("utf-8")).hexdigest()


def _message_value(message: ChatMessage) -> dict[str, object]:
    return {
        "message_id": message.message_id,
        "occurred_at": message.occurred_at,
        "scope": {"kind": message.scope.kind.value, "room_id": message.scope.room_id},
        "sender": {
            "actor_type": message.sender.actor_type.value,
            "display_name": message.sender.display_name,
        },
        "text": message.text,
    }


def _message(raw: object) -> ChatMessage:
    if not isinstance(raw, (bytes, str)):
        raise ChatDeliveryUnavailable("CHAT_DELIVERY_RESPONSE_INVALID")
    try:
        value = VersionedJsonCodec.decode(raw)
        raw_scope = value["scope"]
        raw_sender = value["sender"]
        if not isinstance(raw_scope, dict) or not isinstance(raw_sender, dict):
            raise TypeError
        scope = cast(dict[str, object], raw_scope)
        sender = cast(dict[str, object], raw_sender)
        kind = scope["kind"]
        room_id = scope["room_id"]
        actor_type = sender["actor_type"]
        display_name = sender["display_name"]
        message_id = value["message_id"]
        occurred_at = value["occurred_at"]
        text = value["text"]
        if not all(isinstance(item, str) for item in (kind, actor_type, display_name)):
            raise TypeError
        if room_id is not None and not isinstance(room_id, str):
            raise TypeError
        if not all(isinstance(item, str) for item in (message_id, occurred_at, text)):
            raise TypeError
        return ChatMessage(
            cast(str, message_id),
            cast(str, occurred_at),
            ChatScope(ChatScopeType(cast(str, kind)), room_id),
            ChatSender(SessionActorType(cast(str, actor_type)), cast(str, display_name)),
            cast(str, text),
        )
    except (KeyError, TypeError, ValueError, RedisProviderError, ChatRuleViolation):
        raise ChatDeliveryUnavailable("CHAT_DELIVERY_RESPONSE_INVALID") from None


def _result(raw: object) -> dict[str, object]:
    if not isinstance(raw, (bytes, str)):
        raise ChatDeliveryUnavailable("CHAT_DELIVERY_RESPONSE_INVALID")
    try:
        result = VersionedJsonCodec.decode(raw)
    except RedisProviderError:
        raise ChatDeliveryUnavailable("CHAT_DELIVERY_RESPONSE_INVALID") from None
    if not isinstance(result.get("ok"), bool):
        raise ChatDeliveryUnavailable("CHAT_DELIVERY_RESPONSE_INVALID")
    return result
