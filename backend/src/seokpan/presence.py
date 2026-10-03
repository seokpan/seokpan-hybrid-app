"""User-level presence bookkeeping, separate from Session and Room liveness."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from seokpan.identity.application import SessionActorType


class PresenceRuleViolation(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class PresenceUnavailable(RuntimeError):
    """Unknown is not a successful zero-user snapshot."""


@dataclass(frozen=True, slots=True)
class PresenceIdentity:
    actor_type: SessionActorType
    actor_id: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.actor_type, SessionActorType):
            raise PresenceRuleViolation("INVALID_PRESENCE_IDENTITY")
        if not isinstance(self.actor_id, str) or not self.actor_id or len(self.actor_id) > 128:
            raise PresenceRuleViolation("INVALID_PRESENCE_IDENTITY")
        if self.actor_type is SessionActorType.MEMBER and not re.fullmatch(
            r"[1-9][0-9]*", self.actor_id
        ):
            raise PresenceRuleViolation("INVALID_PRESENCE_IDENTITY")


@dataclass(frozen=True, slots=True)
class PresenceLease:
    """Server-only handle for one connection; never a browser credential."""

    lease_id: str = field(repr=False)

    def __post_init__(self) -> None:
        try:
            parsed = UUID(self.lease_id)
        except (ValueError, TypeError, AttributeError):
            raise PresenceRuleViolation("INVALID_PRESENCE_LEASE") from None
        if parsed.version != 4 or str(parsed) != self.lease_id:
            raise PresenceRuleViolation("INVALID_PRESENCE_LEASE")


@dataclass(frozen=True, slots=True)
class PresenceSnapshot:
    online_users: int


@dataclass(frozen=True, slots=True, repr=False)
class PresenceConnection:
    """Internal inspection only; never a public response."""

    lease: PresenceLease
    identity: PresenceIdentity
    session_digest: str


class PresencePort(Protocol):
    """Bookkeeping is NOT authorization or transport liveness detection.

    Only the server may register a verified Session/identity, and renew after
    checking both that Session and the connection. Revocation/expiry must be
    reconciled before publishing a count; unknown checks must not publish zero.
    A Provider lease bounds abandoned bookkeeping, not the authentication TTL
    or the Room reconnect grace. Methods never touch either of those lifetimes.
    """

    async def open(self, identity: PresenceIdentity, session_digest: str) -> PresenceLease: ...

    async def renew(self, lease: PresenceLease) -> None: ...

    async def close(self, lease: PresenceLease) -> None: ...

    async def invalidate_session(self, session_digest: str) -> None: ...

    async def snapshot(self) -> PresenceSnapshot: ...

    async def connections(self) -> tuple[PresenceConnection, ...]: ...
