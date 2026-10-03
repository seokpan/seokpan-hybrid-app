"""In-memory Session adapter with the same public lifecycle as Redis."""

from __future__ import annotations

from dataclasses import dataclass, replace

from seokpan.clock import MillisecondClock
from seokpan.identity.application.session import (
    SESSION_ABSOLUTE_TTL_MS,
    SESSION_IDLE_TTL_MS,
    CreateSession,
    SessionActorType,
    SessionRecord,
    SessionRuleViolation,
    validate_digest,
)


@dataclass(slots=True)
class ManualClock:
    now_ms: int = 0

    def advance(self, milliseconds: int) -> None:
        if milliseconds < 0:
            raise ValueError("milliseconds must be non-negative")
        self.now_ms += milliseconds


@dataclass(slots=True)
class _StoredSession:
    record: SessionRecord
    idle_expires_at_ms: int


class InMemorySessionAdapter:
    """A deterministic Fake; passing it is not Redis Provider evidence."""

    def __init__(self, clock: MillisecondClock) -> None:
        self._clock = clock
        self._sessions: dict[str, _StoredSession] = {}
        self._member_sessions: dict[str, dict[str, int]] = {}

    async def create(self, command: CreateSession) -> SessionRecord:
        self._purge_expired()
        if command.session_digest in self._sessions:
            raise SessionRuleViolation("SESSION_ALREADY_EXISTS")
        return self._store(command)

    async def get(self, session_digest: str) -> SessionRecord | None:
        validate_digest(session_digest)
        self._purge_expired()
        stored = self._sessions.get(session_digest)
        return stored.record if stored is not None else None

    async def touch(self, session_digest: str) -> SessionRecord | None:
        validate_digest(session_digest)
        self._purge_expired()
        stored = self._sessions.get(session_digest)
        if stored is None:
            return None
        now_ms = self._clock.now_ms
        record = replace(stored.record, last_activity_at_ms=now_ms)
        stored.record = record
        stored.idle_expires_at_ms = min(
            now_ms + SESSION_IDLE_TTL_MS,
            record.absolute_expires_at_ms,
        )
        return record

    async def rotate(
        self,
        *,
        previous_session_digest: str,
        replacement: CreateSession,
    ) -> SessionRecord:
        validate_digest(previous_session_digest)
        self._purge_expired()
        previous = self._sessions.get(previous_session_digest)
        if previous is None:
            raise SessionRuleViolation("SESSION_NOT_FOUND")
        if replacement.session_digest == previous_session_digest:
            raise SessionRuleViolation("SESSION_ROTATION_REQUIRES_NEW_DIGEST")
        if replacement.session_digest in self._sessions:
            raise SessionRuleViolation("SESSION_ALREADY_EXISTS")

        self._remove(previous.record)
        return self._store(replacement)

    async def restore_after_failed_rotation(
        self,
        *,
        failed_replacement_digest: str,
        previous: SessionRecord,
    ) -> SessionRecord:
        """Restore the exact previous identity after a later Room step fails."""
        validate_digest(failed_replacement_digest)
        self._purge_expired()
        failed_replacement = self._sessions.get(failed_replacement_digest)
        if failed_replacement is None:
            raise SessionRuleViolation("SESSION_NOT_FOUND")
        if previous.session_digest in self._sessions:
            raise SessionRuleViolation("SESSION_ALREADY_EXISTS")
        idle_expires_at_ms = min(
            previous.last_activity_at_ms + SESSION_IDLE_TTL_MS,
            previous.absolute_expires_at_ms,
        )
        self._remove(failed_replacement.record)
        if idle_expires_at_ms <= self._clock.now_ms:
            raise SessionRuleViolation("SESSION_ROLLBACK_EXPIRED")

        self._sessions[previous.session_digest] = _StoredSession(
            record=previous,
            idle_expires_at_ms=idle_expires_at_ms,
        )
        if previous.actor_type is SessionActorType.MEMBER:
            self._member_sessions.setdefault(previous.actor_id, {})[previous.session_digest] = (
                idle_expires_at_ms
            )
        return previous

    async def revoke(self, session_digest: str) -> bool:
        validate_digest(session_digest)
        self._purge_expired()
        stored = self._sessions.get(session_digest)
        if stored is None:
            return False
        self._remove(stored.record)
        return True

    def member_session_digests(self, member_id: str) -> tuple[str, ...]:
        """Test observation that mirrors the Redis member ZSET contents."""
        self._purge_expired()
        return tuple(sorted(self._member_sessions.get(member_id, {})))

    def _store(self, command: CreateSession) -> SessionRecord:
        now_ms = self._clock.now_ms
        absolute_expires_at_ms = now_ms + SESSION_ABSOLUTE_TTL_MS
        record = SessionRecord(
            session_digest=command.session_digest,
            actor_type=command.actor_type,
            actor_id=command.actor_id,
            csrf_digest=command.csrf_digest,
            csrf_token=command.csrf_token,
            created_at_ms=now_ms,
            last_activity_at_ms=now_ms,
            absolute_expires_at_ms=absolute_expires_at_ms,
        )
        self._sessions[record.session_digest] = _StoredSession(
            record=record,
            idle_expires_at_ms=now_ms + SESSION_IDLE_TTL_MS,
        )
        if record.actor_type is SessionActorType.MEMBER:
            self._member_sessions.setdefault(record.actor_id, {})[record.session_digest] = (
                now_ms + SESSION_IDLE_TTL_MS
            )
        return record

    def _remove(self, record: SessionRecord) -> None:
        self._sessions.pop(record.session_digest, None)
        if record.actor_type is not SessionActorType.MEMBER:
            return
        sessions = self._member_sessions.get(record.actor_id)
        if sessions is None:
            return
        sessions.pop(record.session_digest, None)
        if not sessions:
            del self._member_sessions[record.actor_id]

    def _purge_expired(self) -> None:
        now_ms = self._clock.now_ms
        expired = tuple(
            stored.record
            for stored in self._sessions.values()
            if stored.idle_expires_at_ms <= now_ms or stored.record.absolute_expires_at_ms <= now_ms
        )
        for record in expired:
            self._remove(record)

        for member_id, sessions in tuple(self._member_sessions.items()):
            for digest, expires_at_ms in tuple(sessions.items()):
                if expires_at_ms <= now_ms:
                    sessions.pop(digest, None)
            if not sessions:
                self._member_sessions.pop(member_id, None)
