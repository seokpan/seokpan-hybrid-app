"""Recheck receive-only streams without renewing idle sessions."""

from __future__ import annotations

import asyncio
from enum import StrEnum

from seokpan.api.identity import IdentityApiServices
from seokpan.identity.application import SessionRecord, SessionTransitionUnavailable
from seokpan.room.application import RoomApplicationService

SESSION_CHECK_INTERVAL_SECONDS = 1.0
SESSION_CHECK_TIMEOUT_SECONDS = 2.0


class StreamAccessState(StrEnum):
    ALLOWED = "ALLOWED"
    EXPIRED = "EXPIRED"
    LEFT = "LEFT"
    REPLACED = "REPLACED"


class StreamAccess:
    """Room identity comes from the server binding, never from a Socket message."""

    def __init__(
        self,
        identity: IdentityApiServices,
        session: SessionRecord,
        *,
        rooms: RoomApplicationService | None = None,
        room_id: str | None = None,
        participant_id: str | None = None,
        connection_generation: int | None = None,
    ) -> None:
        self._identity = identity
        self._session = session
        self._rooms = rooms
        self._room_id = room_id
        self._participant_id = participant_id
        self._connection_generation = connection_generation

    async def check(self) -> StreamAccessState:
        # Unknown/slow storage must not become an expired-session verdict.
        async with asyncio.timeout(SESSION_CHECK_TIMEOUT_SECONDS):
            return await self._check()

    async def _check(self) -> StreamAccessState:
        if self._rooms is None or self._participant_id is None:
            current = await self._identity.sessions.find(self._session.session_digest)
            if current is not None and (current.actor_type, current.actor_id) != (
                self._session.actor_type,
                self._session.actor_id,
            ):
                raise SessionTransitionUnavailable
            return StreamAccessState.EXPIRED if current is None else StreamAccessState.ALLOWED
        for _ in range(3):
            binding = await self._rooms.resolve_participant_identity(self._participant_id)
            if binding is None or binding.room_id != self._room_id:
                return StreamAccessState.LEFT
            if self._connection_generation is not None:
                if binding.connection_generation is None:
                    raise SessionTransitionUnavailable
                if (
                    binding.connection_generation != self._connection_generation
                    or not binding.connected
                ):
                    return StreamAccessState.REPLACED
            current = await self._identity.sessions.find(binding.session_digest)
            # A completed Guest login may have replaced the digest during the read.
            if await self._rooms.resolve_participant_identity(self._participant_id) != binding:
                continue
            if current is None:
                if binding.session_digest != self._session.session_digest:
                    previous = await self._identity.sessions.find(self._session.session_digest)
                    if previous is not None:
                        # Session rollback without a matching Room rollback is
                        # inconsistent storage, not a participant timing out.
                        raise SessionTransitionUnavailable
                return StreamAccessState.EXPIRED
            if (current.actor_type, current.actor_id) != (binding.actor_type, binding.actor_id):
                raise SessionTransitionUnavailable
            return StreamAccessState.ALLOWED
        raise SessionTransitionUnavailable
