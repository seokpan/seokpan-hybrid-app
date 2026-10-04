from __future__ import annotations

import pytest

from seokpan.identity.application import SessionActorType, SessionRecord, digest_opaque_token
from seokpan.persistence.memory import InMemoryRoomRuntimeAdapter, ManualClock
from seokpan.room.application import ConnectRoomParticipant, RoomApplicationService
from seokpan.room.domain import RoomConfig


class UnusedPasswordPort:
    async def encode(self, raw_password: str) -> str:
        raise AssertionError(raw_password)

    async def verify(self, encoded_password: str, candidate_password: str) -> bool:
        raise AssertionError(encoded_password, candidate_password)


class CommitThenFailConnectAdapter(InMemoryRoomRuntimeAdapter):
    def __init__(self, clock: ManualClock) -> None:
        super().__init__(clock)
        self.fail_once = True

    async def connect(self, command: ConnectRoomParticipant):
        result = await super().connect(command)
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("simulated lost connect response")
        return result


def member() -> SessionRecord:
    return SessionRecord(
        session_digest="a" * 64,
        actor_type=SessionActorType.MEMBER,
        actor_id="1",
        csrf_digest=digest_opaque_token("b" * 64),
        csrf_token="b" * 64,
        created_at_ms=0,
        last_activity_at_ms=0,
        absolute_expires_at_ms=100_000,
    )


@pytest.mark.asyncio
async def test_uncertain_connect_converges_on_new_generation_without_stale_disconnect() -> None:
    clock = ManualClock(now_ms=1_000)
    runtime = CommitThenFailConnectAdapter(clock)
    rooms = RoomApplicationService(runtime, UnusedPasswordPort())
    session = member()
    created = await rooms.create_room(
        session=session,
        request_id="create",
        config=RoomConfig(name="reconnect"),
        password=None,
    )
    assert created.snapshot is not None
    room_id = created.snapshot.room_id
    participant_id = created.snapshot.owner_id
    assert participant_id is not None

    with pytest.raises(RuntimeError, match="simulated lost connect response"):
        await rooms.connect(session=session, room_id=room_id)

    uncertain = runtime._rooms[room_id].connections[participant_id]
    assert uncertain.generation == 2
    assert uncertain.connected is True

    recovered = await rooms.connect(session=session, room_id=room_id)

    assert recovered.connection_generation == 3
    binding = await rooms.resolve_participant_identity(participant_id)
    assert binding is not None
    assert binding.connection_generation == 3
    assert binding.connected is True

    stale = await rooms.disconnect_participant(
        room_id=room_id,
        participant_id=participant_id,
        connection_generation=2,
    )

    assert stale.stale_connection is True
    current = runtime._rooms[room_id].connections[participant_id]
    assert current.generation == 3
    assert current.connected is True
