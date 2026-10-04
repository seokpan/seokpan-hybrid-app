"""Resolve Room participation from shared Redis state across Backend replicas."""

from __future__ import annotations

from seokpan.identity.application import CreateSession, SessionActorType, SessionRecord
from seokpan.persistence.redis.room_adapter import RedisRoomRuntimeAdapter
from seokpan.persistence.redis.session_adapter import RedisSessionAdapter
from seokpan.room.application.lobby import RoomParticipation
from seokpan.room.application.runtime import RoomSessionBinding


class RedisRoomParticipationResolver:
    def __init__(self, rooms: RedisRoomRuntimeAdapter, sessions: RedisSessionAdapter) -> None:
        self._rooms = rooms
        self._sessions = sessions

    async def by_session(self, session_digest: str) -> RoomParticipation | None:
        binding = await self._rooms.find_by_session(session_digest)
        return None if binding is None else await self._resolve(binding.session_digest, binding)

    async def by_participant(self, participant_id: str) -> RoomParticipation | None:
        binding = await self._rooms.find_by_participant(participant_id)
        return None if binding is None else await self._resolve(binding.session_digest, binding)

    async def by_transition_source(self, previous: SessionRecord) -> RoomParticipation | None:
        binding = await self._rooms.find_by_session(previous.session_digest)
        if binding is None or binding.actor_type.value != previous.actor_type.value:
            return None
        return RoomParticipation(
            previous.session_digest,
            binding.room_id,
            binding.participant_id,
            previous.actor_type,
            previous.actor_id,
            binding.connection_generation,
            binding.connected,
        )

    async def identity_transition_applied(
        self,
        previous: SessionRecord,
        replacement: CreateSession,
        participant_id: str,
    ) -> bool | None:
        binding = await self._rooms.find_by_participant(participant_id)
        if binding is None:
            return None
        current = (binding.session_digest, binding.actor_type.value)
        if current == (replacement.session_digest, replacement.actor_type.value):
            return True
        if current == (previous.session_digest, previous.actor_type.value):
            return False
        return None

    async def _resolve(
        self, session_digest: str, binding: RoomSessionBinding
    ) -> RoomParticipation | None:
        # Deliberately re-read the Session authority. A stale Room connection must
        # never recreate an expired or revoked identity on another replica.
        session = await self._sessions.get(session_digest)
        if session is None:
            return None
        actor_type = SessionActorType(binding.actor_type.value)
        if session.actor_type is not actor_type:
            return None
        return RoomParticipation(
            session.session_digest,
            binding.room_id,
            binding.participant_id,
            session.actor_type,
            session.actor_id,
            binding.connection_generation,
            binding.connected,
        )
