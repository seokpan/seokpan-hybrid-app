"""Production Session/Room workflow with provider-backed convergence."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from seokpan.identity.application import (
    CreateSession,
    ParticipantSessionPort,
    SessionRecord,
    SessionRuleViolation,
    SessionTransitionUnavailable,
)
from seokpan.persistence.redis.session_adapter import RedisSessionAdapter

_LOGGER = logging.getLogger(__name__)


class SessionAdmissionGate(Protocol):
    async def acquire_session_admission(self, digest: str) -> str: ...

    async def release_session_admission(self, digest: str, token: str) -> None: ...


class RedisSessionWorkflow:
    """Coordinate Redis Session rotation with Redis Room participation.

    Redis scripts provide compare-and-set/idempotency. No process-local lock or
    uncertainty cache is used as cross-replica authority.
    """

    def __init__(
        self,
        sessions: RedisSessionAdapter,
        participants: ParticipantSessionPort,
        admissions: SessionAdmissionGate | None = None,
    ) -> None:
        self._sessions = sessions
        self._participants = participants
        self._admissions = admissions

    async def create(self, command: CreateSession) -> SessionRecord:
        return await self._sessions.create(command)

    async def get(self, session_digest: str) -> SessionRecord | None:
        return await self._sessions.get(session_digest)

    async def touch(self, session_digest: str) -> SessionRecord | None:
        return await self._sessions.touch(session_digest)

    async def rotate_identity(
        self,
        *,
        previous: SessionRecord,
        replacement: CreateSession,
    ) -> SessionRecord:
        try:
            rotated = await self._sessions.rotate(
                previous_session_digest=previous.session_digest,
                replacement=replacement,
            )
        except SessionRuleViolation:
            raise
        except BaseException:
            raise SessionTransitionUnavailable from None
        try:
            await self._participants.change_identity(previous, replacement)
        except asyncio.CancelledError:
            # Cancellation cannot prove whether the provider write committed.
            raise
        except SessionTransitionUnavailable:
            # Room write outcome could not be proven. Rolling the Session back
            # could create a confirmed cross-provider identity mismatch.
            raise
        except BaseException:
            try:
                await self._sessions.restore_after_failed_rotation(
                    failed_replacement_digest=replacement.session_digest,
                    previous=previous,
                )
            except BaseException as rollback_error:
                raise SessionTransitionUnavailable from rollback_error
            raise
        return rotated

    async def logout(self, current: SessionRecord) -> bool:
        token: str | None = None
        try:
            if self._admissions is not None:
                token = await self._admissions.acquire_session_admission(current.session_digest)
            await self._participants.leave(current)
            return await self._sessions.revoke(current.session_digest)
        except SessionRuleViolation:
            raise
        except asyncio.CancelledError:
            raise
        except BaseException:
            raise SessionTransitionUnavailable from None
        finally:
            if token is not None and self._admissions is not None:
                try:
                    await self._admissions.release_session_admission(
                        current.session_digest,
                        token,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # The bounded lease expires. Never turn a committed logout
                    # into a second ambiguous command only because release failed.
                    _LOGGER.warning("Session admission lease release failed during logout")
