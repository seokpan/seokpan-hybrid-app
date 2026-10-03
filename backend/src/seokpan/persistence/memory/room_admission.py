"""Session admission checks over the existing shared Memory Room store."""

from seokpan.persistence.memory.room_adapter import InMemoryRoomRuntimeAdapter
from seokpan.room.application.runtime import (
    ChangeRoomIdentity,
    ConnectRoomParticipant,
    CreateRoomRuntime,
    JoinRoomRuntime,
    RoomMutationResult,
)
from seokpan.room.domain import RoomRuleViolation


class SessionAdmissionMemoryRoomAdapter(InMemoryRoomRuntimeAdapter):
    """No independent reservation to leak on leave, kick, expiry or Room closure."""

    def _require_single_binding(self, digest: str, room_id: str, participant_id: str) -> None:
        self._purge_expired()
        for existing_room, state in self._rooms.items():
            for existing_participant, connection in state.connections.items():
                if connection.session_digest == digest and (
                    existing_room,
                    existing_participant,
                ) != (room_id, participant_id):
                    raise RoomRuleViolation("SESSION_ALREADY_IN_ROOM")

    async def create(self, command: CreateRoomRuntime) -> RoomMutationResult:
        self._require_single_binding(
            command.owner_session_digest,
            command.room_id,
            command.owner_id,
        )
        # The inherited create/join/change_identity paths do not suspend before
        # recording the binding. Re-check this invariant if those paths change.
        return await super().create(command)

    async def join(self, command: JoinRoomRuntime) -> RoomMutationResult:
        self._require_single_binding(
            command.session_digest,
            command.room_id,
            command.participant_id,
        )
        return await super().join(command)

    async def change_identity(self, command: ChangeRoomIdentity) -> RoomMutationResult:
        self._require_single_binding(
            command.session_digest,
            command.room_id,
            command.participant_id,
        )
        return await super().change_identity(command)

    async def connect(self, command: ConnectRoomParticipant) -> RoomMutationResult:
        self._require_single_binding(
            command.session_digest,
            command.room_id,
            command.participant_id,
        )
        # The base writes Room/connection before awaiting the optional Vote mirror.
        return await super().connect(command)
