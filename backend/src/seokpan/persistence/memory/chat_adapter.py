"""Single-loop chat Fake, with bounded queues and short-lived retry receipts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import uuid4

from seokpan.chat import (
    ChatDeliveryUnavailable,
    ChatMessage,
    ChatReceipt,
    ChatRuleViolation,
    ChatScope,
    ChatSubscription,
    ChatSubscriptionClosed,
    SendChat,
)


class _Subscription:
    def __init__(
        self,
        limit: int,
        remove: Callable[[_Subscription], None],
        check_loop: Callable[[], None],
    ) -> None:
        self._queue: asyncio.Queue[ChatMessage | None] = asyncio.Queue(maxsize=limit)
        self._remove = remove
        self._check_loop = check_loop
        self._closed: str | None = None
        self._receiving = False

    async def receive(self) -> ChatMessage:
        self._check_loop()
        if self._receiving:
            raise ChatDeliveryUnavailable("CHAT_RECEIVER_ALREADY_WAITING")
        if self._closed is not None:
            raise ChatSubscriptionClosed(self._closed)
        self._receiving = True
        try:
            message = await self._queue.get()
            # Closing can happen after get wakes but before this task resumes.
            if self._closed is not None or message is None:
                raise ChatSubscriptionClosed(self._closed or "CLOSED")
            return message
        finally:
            self._receiving = False

    async def close(self) -> None:
        self._check_loop()
        self._stop("CLOSED")

    def offer(self, message: ChatMessage) -> None:
        if self._closed is not None:
            return
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            self._stop("SLOW_CONSUMER")

    def _stop(self, reason: str) -> None:
        if self._closed is not None:
            return
        self._closed = reason
        self._remove(self)
        while not self._queue.empty():
            self._queue.get_nowait()
        self._queue.put_nowait(None)


@dataclass(frozen=True, slots=True)
class _RecentRequest:
    expires_at: float
    fingerprint: str
    receipt: ChatReceipt


class InMemoryChatAdapter:
    """No Redis, DB, cross-loop use, persistence, or multi-Replica guarantees.

    Retry tracking retains only a hash and receipt, not message text. Defaults
    bound this Fake's memory; they are not approved production sizing values.
    """

    def __init__(
        self,
        *,
        max_queue_size: int = 100,
        max_recent_requests: int = 4096,
        retry_window_seconds: float = 120.0,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if (
            type(max_queue_size) is not int
            or max_queue_size < 1
            or type(max_recent_requests) is not int
            or max_recent_requests < 1
            or not math.isfinite(retry_window_seconds)
            or retry_window_seconds <= 0
        ):
            raise ValueError("INVALID_CHAT_DELIVERY_LIMIT")
        self._queue_limit = max_queue_size
        self._recent_limit = max_recent_requests
        self._retry_window = retry_window_seconds
        self._monotonic = monotonic
        self._now = now
        self._subscriptions: dict[ChatScope, set[_Subscription]] = {}
        self._recent: OrderedDict[tuple[str, str], _RecentRequest] = OrderedDict()
        self._loop: asyncio.AbstractEventLoop | None = None

    def _check_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise ChatDeliveryUnavailable("CHAT_LOOP_MISMATCH")

    async def subscribe(self, scope: ChatScope) -> ChatSubscription:
        self._check_loop()
        subscriptions = self._subscriptions.setdefault(scope, set())

        def remove(subscription: _Subscription) -> None:
            subscriptions.discard(subscription)
            if not subscriptions:
                self._subscriptions.pop(scope, None)

        subscription = _Subscription(self._queue_limit, remove, self._check_loop)
        subscriptions.add(subscription)
        return subscription

    async def publish(self, command: SendChat) -> ChatReceipt:
        # No await between retry check, admission and offer in this single-loop
        # Fake. Real Provider implementations must prove their own concurrency.
        self._check_loop()
        current = self._monotonic()
        while self._recent:
            first = next(iter(self._recent.values()))
            if first.expires_at > current:
                break
            self._recent.popitem(last=False)
        key = (command.sender_key, command.request_id)
        fingerprint = _fingerprint(command)
        previous = self._recent.get(key)
        if previous is not None:
            if previous.fingerprint != fingerprint:
                raise ChatRuleViolation("REQUEST_ID_REUSED")
            return replace(previous.receipt, replayed=True)
        if len(self._recent) >= self._recent_limit:
            # Do not evict a live receipt and accidentally accept a duplicate.
            raise ChatDeliveryUnavailable("CHAT_RETRY_CAPACITY_REACHED")
        instant = self._now()
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ChatDeliveryUnavailable("CHAT_CLOCK_UNAVAILABLE")
        occurred_at = instant.astimezone(UTC).isoformat().replace("+00:00", "Z")
        receipt = ChatReceipt(str(uuid4()), occurred_at)
        message = ChatMessage(
            receipt.message_id, occurred_at, command.scope, command.sender, command.text
        )
        self._recent[key] = _RecentRequest(current + self._retry_window, fingerprint, receipt)
        for subscription in tuple(self._subscriptions.get(command.scope, ())):
            subscription.offer(message)
        return receipt


def _fingerprint(command: SendChat) -> str:
    value = (
        command.scope.kind,
        command.scope.room_id,
        command.sender.actor_type,
        command.sender.display_name,
        command.text,
    )
    return hashlib.sha256(json.dumps(value, ensure_ascii=True).encode("utf-8")).hexdigest()
