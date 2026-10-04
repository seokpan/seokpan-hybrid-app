"""In-memory authentication Session workflow used by Headless tests."""

from __future__ import annotations

import asyncio

from seokpan.identity.application import (
    CreateSession,
    ParticipantSessionPort,
    SessionPort,
    SessionRecord,
    SessionRuleViolation,
    SessionTransitionUnavailable,
)


class InMemorySessionWorkflow:
    """A Fake; real Redis/Room coordination is a later provider gate."""

    def __init__(
        self,
        sessions: SessionPort,
        participants: ParticipantSessionPort | None = None,
    ) -> None:
        self._sessions = sessions
        self._participants = participants or NoopParticipantSessionAdapter()
        self.identity_changes: list[tuple[SessionRecord, CreateSession]] = []
        self.logouts: list[SessionRecord] = []
        self.fail_identity_change = False
        self.fail_logout = False
        self._transitions: dict[str, asyncio.Event] = {}
        self._uncertain: set[str] = set()

    async def create(self, command: CreateSession) -> SessionRecord:
        return await self._sessions.create(command)

    async def get(self, session_digest: str) -> SessionRecord | None:
        await self._stable(session_digest)
        return await self._sessions.get(session_digest)

    async def touch(self, session_digest: str) -> SessionRecord | None:
        await self._stable(session_digest)
        return await self._sessions.touch(session_digest)

    async def _stable(self, session_digest: str) -> None:
        while (pending := self._transitions.get(session_digest)) is not None:
            await pending.wait()
        if session_digest in self._uncertain:
            raise SessionTransitionUnavailable

    async def rotate_identity(
        self,
        *,
        previous: SessionRecord,
        replacement: CreateSession,
    ) -> SessionRecord:
        if self.fail_identity_change:
            raise SessionTransitionUnavailable
        digest = previous.session_digest
        if any(
            value in self._transitions or value in self._uncertain
            for value in (digest, replacement.session_digest)
        ):
            raise SessionTransitionUnavailable
        pending = asyncio.Event()
        self._transitions[digest] = pending
        self._transitions[replacement.session_digest] = pending
        try:
            try:
                result = await self._sessions.rotate(
                    previous_session_digest=digest,
                    replacement=replacement,
                )
            except SessionRuleViolation:
                raise
            except BaseException:
                self._uncertain.update((digest, replacement.session_digest))
                raise
            try:
                await self._participants.change_identity(previous, replacement)
            except BaseException:
                try:
                    await self._sessions.restore_after_failed_rotation(
                        failed_replacement_digest=replacement.session_digest,
                        previous=previous,
                    )
                except BaseException as rollback_error:
                    self._uncertain.update((digest, replacement.session_digest))
                    raise SessionTransitionUnavailable from rollback_error
                raise
            self.identity_changes.append((previous, replacement))
            return result
        finally:
            self._transitions.pop(digest, None)
            self._transitions.pop(replacement.session_digest, None)
            pending.set()

    async def logout(self, current: SessionRecord) -> bool:
        if self.fail_logout:
            raise SessionTransitionUnavailable
        await self._participants.leave(current)
        revoked = await self._sessions.revoke(current.session_digest)
        if revoked:
            self.logouts.append(current)
        return revoked


class NoopParticipantSessionAdapter:
    """Used only before Room HTTP can create participant state."""

    async def change_identity(
        self,
        previous: SessionRecord,
        replacement: CreateSession,
    ) -> None:
        del previous, replacement

    async def leave(self, current: SessionRecord) -> None:
        del current
