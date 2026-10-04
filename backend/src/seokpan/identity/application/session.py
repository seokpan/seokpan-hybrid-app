"""Provider-neutral server-side Session lifecycle contract."""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

SESSION_SCHEMA_VERSION = 2
SESSION_IDLE_TTL_MS = 2 * 60 * 60 * 1000
SESSION_ABSOLUTE_TTL_MS = 24 * 60 * 60 * 1000
_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")


class SessionActorType(StrEnum):
    MEMBER = "MEMBER"
    GUEST = "GUEST"


class SessionRuleViolation(ValueError):
    """A stable Session rejection without secret-bearing context."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def digest_opaque_token(token: str) -> str:
    """Return the non-reversible Redis identifier for a high-entropy token."""
    if not token:
        raise SessionRuleViolation("INVALID_OPAQUE_TOKEN")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def validate_digest(value: str, *, code: str = "INVALID_SESSION_DIGEST") -> None:
    if _DIGEST_PATTERN.fullmatch(value) is None:
        raise SessionRuleViolation(code)


def validate_csrf_pair(token: str, digest: str) -> None:
    if not token or not hmac.compare_digest(digest_opaque_token(token), digest):
        raise SessionRuleViolation("INVALID_CSRF_TOKEN")


@dataclass(frozen=True, slots=True)
class CreateSession:
    session_digest: str
    actor_type: SessionActorType
    actor_id: str
    csrf_digest: str
    csrf_token: str = field(kw_only=True, repr=False)

    def __post_init__(self) -> None:
        validate_digest(self.session_digest)
        validate_digest(self.csrf_digest, code="INVALID_CSRF_DIGEST")
        validate_csrf_pair(self.csrf_token, self.csrf_digest)
        if not self.actor_id:
            raise SessionRuleViolation("INVALID_SESSION_ACTOR")
        if self.actor_type is SessionActorType.MEMBER and not self.actor_id.isdecimal():
            raise SessionRuleViolation("INVALID_MEMBER_ID")


@dataclass(frozen=True, slots=True)
class SessionRecord:
    session_digest: str
    actor_type: SessionActorType
    actor_id: str
    csrf_digest: str
    created_at_ms: int
    last_activity_at_ms: int
    absolute_expires_at_ms: int
    schema_version: int = SESSION_SCHEMA_VERSION
    csrf_token: str = field(kw_only=True, repr=False)

    def __post_init__(self) -> None:
        validate_digest(self.session_digest)
        validate_digest(self.csrf_digest, code="INVALID_CSRF_DIGEST")
        if self.schema_version != SESSION_SCHEMA_VERSION:
            raise SessionRuleViolation("UNSUPPORTED_SESSION_SCHEMA")
        validate_csrf_pair(self.csrf_token, self.csrf_digest)
        if not self.actor_id:
            raise SessionRuleViolation("INVALID_SESSION_ACTOR")
        if self.actor_type is SessionActorType.MEMBER and not self.actor_id.isdecimal():
            raise SessionRuleViolation("INVALID_MEMBER_ID")
        if self.created_at_ms < 0 or self.last_activity_at_ms < self.created_at_ms:
            raise SessionRuleViolation("INVALID_SESSION_TIMESTAMPS")
        if self.absolute_expires_at_ms <= self.last_activity_at_ms:
            raise SessionRuleViolation("INVALID_SESSION_EXPIRY")


class SessionPort(Protocol):
    async def create(self, command: CreateSession) -> SessionRecord: ...

    async def get(self, session_digest: str) -> SessionRecord | None: ...

    async def touch(self, session_digest: str) -> SessionRecord | None: ...

    async def rotate(
        self,
        *,
        previous_session_digest: str,
        replacement: CreateSession,
    ) -> SessionRecord: ...

    async def restore_after_failed_rotation(
        self,
        *,
        failed_replacement_digest: str,
        previous: SessionRecord,
    ) -> SessionRecord: ...

    async def revoke(self, session_digest: str) -> bool: ...
