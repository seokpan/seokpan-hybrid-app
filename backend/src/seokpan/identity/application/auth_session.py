"""Guest and Member session workflows used by HTTP and later transports."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from seokpan.identity.application.session import (
    CreateSession,
    SessionActorType,
    SessionRecord,
    digest_opaque_token,
)


class SessionWorkflowPort(Protocol):
    async def create(self, command: CreateSession) -> SessionRecord: ...

    async def get(self, session_digest: str) -> SessionRecord | None: ...

    async def touch(self, session_digest: str) -> SessionRecord | None: ...

    async def rotate_identity(
        self,
        *,
        previous: SessionRecord,
        replacement: CreateSession,
    ) -> SessionRecord: ...

    async def logout(self, current: SessionRecord) -> bool: ...


class TokenSource(Protocol):
    def issue(self) -> str: ...


class ParticipantSessionPort(Protocol):
    """Keep Room participation aligned with Session identity changes."""

    async def change_identity(
        self,
        previous: SessionRecord,
        replacement: CreateSession,
    ) -> None: ...

    async def leave(self, current: SessionRecord) -> None: ...


@dataclass(frozen=True, slots=True)
class IssuedSession:
    token: str = field(repr=False)
    csrf_token: str = field(repr=False)
    record: SessionRecord


class AuthSessionService:
    def __init__(self, sessions: SessionWorkflowPort, tokens: TokenSource) -> None:
        self._sessions = sessions
        self._tokens = tokens

    async def issue_guest(self, current: SessionRecord | None = None) -> IssuedSession:
        actor_id = f"guest-{self._tokens.issue()[:24]}"
        return await self._issue(SessionActorType.GUEST, actor_id, current)

    async def issue_member(
        self,
        member_id: int,
        current: SessionRecord | None = None,
    ) -> IssuedSession:
        if member_id <= 0:
            raise ValueError("member_id must be positive")
        return await self._issue(SessionActorType.MEMBER, str(member_id), current)

    async def current(self, session_digest: str) -> SessionRecord | None:
        return await self._sessions.touch(session_digest)

    async def find(self, session_digest: str) -> SessionRecord | None:
        return await self._sessions.get(session_digest)

    async def logout(self, current: SessionRecord) -> bool:
        return await self._sessions.logout(current)

    async def _issue(
        self,
        actor_type: SessionActorType,
        actor_id: str,
        current: SessionRecord | None,
    ) -> IssuedSession:
        raw_token = self._tokens.issue()
        csrf_token = self._tokens.issue()
        command = CreateSession(
            session_digest=digest_opaque_token(raw_token),
            actor_type=actor_type,
            actor_id=actor_id,
            csrf_digest=digest_opaque_token(csrf_token),
            csrf_token=csrf_token,
        )
        if current is None:
            record = await self._sessions.create(command)
        else:
            record = await self._sessions.rotate_identity(previous=current, replacement=command)
        return IssuedSession(token=raw_token, csrf_token=csrf_token, record=record)


class SessionTransitionUnavailable(RuntimeError):
    pass
