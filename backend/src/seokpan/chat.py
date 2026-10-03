"""Transient chat values and delivery boundary; not Room/Game recovery state."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from seokpan.identity.application import SessionActorType
from seokpan.identity.domain.member import MemberRuleViolation, Nickname


class ChatRuleViolation(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ChatDeliveryUnavailable(RuntimeError):
    """Do not report a dropped or unavailable delivery as a successful send."""


class ChatSubscriptionClosed(RuntimeError):
    """The transport must stop this subscription; no history can be recovered."""


class ChatScopeType(StrEnum):
    LOBBY = "LOBBY"
    ROOM = "ROOM"


def _uuid4(value: str, code: str) -> None:
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise ChatRuleViolation(code) from None
    if parsed.version != 4 or str(parsed) != value:
        raise ChatRuleViolation(code)


@dataclass(frozen=True, slots=True)
class ChatScope:
    kind: ChatScopeType
    room_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ChatScopeType):
            raise ChatRuleViolation("INVALID_CHAT_SCOPE")
        if self.kind is ChatScopeType.LOBBY:
            if self.room_id is not None:
                raise ChatRuleViolation("INVALID_CHAT_SCOPE")
        elif self.room_id is None:
            raise ChatRuleViolation("INVALID_CHAT_SCOPE")
        else:
            _uuid4(self.room_id, "INVALID_CHAT_SCOPE")


@dataclass(frozen=True, slots=True)
class ChatSender:
    """Public projection resolved by the server, never accepted from an HTTP body."""

    actor_type: SessionActorType
    display_name: str

    def __post_init__(self) -> None:
        if not isinstance(self.actor_type, SessionActorType) or not isinstance(
            self.display_name, str
        ):
            raise ChatRuleViolation("INVALID_CHAT_SENDER")
        if self.actor_type is SessionActorType.GUEST:
            if re.fullmatch(r"Guest-[0-9]{4}", self.display_name) is None:
                raise ChatRuleViolation("INVALID_CHAT_SENDER")
        else:
            try:
                normalized = Nickname(self.display_name).value
            except MemberRuleViolation:
                raise ChatRuleViolation("INVALID_CHAT_SENDER") from None
            if normalized != self.display_name:
                raise ChatRuleViolation("INVALID_CHAT_SENDER")


@dataclass(frozen=True, slots=True)
class SendChat:
    """Already-authorized command. Caller must recheck access even for a retry."""

    scope: ChatScope
    sender: ChatSender
    sender_key: str = field(repr=False)
    request_id: str
    text: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.scope, ChatScope) or not isinstance(self.sender, ChatSender):
            raise ChatRuleViolation("INVALID_CHAT_COMMAND")
        if (
            not isinstance(self.sender_key, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.sender_key) is None
        ):
            raise ChatRuleViolation("INVALID_CHAT_SENDER_KEY")
        _uuid4(self.request_id, "INVALID_REQUEST_ID")
        if not isinstance(self.text, str):
            raise ChatRuleViolation("INVALID_CHAT_TEXT")
        text = self.text.strip()
        if not 1 <= len(text) <= 200 or any(0xD800 <= ord(char) <= 0xDFFF for char in text):
            raise ChatRuleViolation("INVALID_CHAT_TEXT")
        object.__setattr__(self, "text", text)


@dataclass(frozen=True, slots=True)
class ChatMessage:
    message_id: str
    occurred_at: str
    scope: ChatScope
    sender: ChatSender
    text: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ChatReceipt:
    message_id: str
    occurred_at: str
    replayed: bool = False


class ChatSubscription(Protocol):
    async def receive(self) -> ChatMessage: ...

    async def close(self) -> None: ...


class ChatDeliveryPort(Protocol):
    """No history API. Subscriptions begin with an empty transient queue.

    Delivery isolation is not authorization: the application/transport must
    verify Session and scope at subscription, send, and immediately before
    forwarding a received message. A receipt means accepted, not read by peers.
    """

    async def subscribe(self, scope: ChatScope) -> ChatSubscription: ...

    async def publish(self, command: SendChat) -> ChatReceipt: ...
