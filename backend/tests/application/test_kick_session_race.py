import asyncio

import pytest

from seokpan.identity.application import CreateSession, SessionActorType, digest_opaque_token
from seokpan.persistence.memory import (
    InMemoryRoomRuntimeAdapter,
    InMemorySessionAdapter,
    ManualClock,
)
from seokpan.persistence.memory.session_workflow import InMemorySessionWorkflow
from seokpan.room.application import ChangeRoomIdentity, RoomApplicationService, RoomMutationResult
from seokpan.room.application.runtime import RoomRuntimeSnapshot
from seokpan.room.domain import RoomConfig


class UnusedPassword:
    async def encode(self, raw_password: str) -> str:
        raise AssertionError("Public rooms do not encode a password")

    async def verify(self, encoded_password: str, candidate_password: str) -> bool:
        raise AssertionError("Public rooms do not verify a password")


class PausedRuntime(InMemoryRoomRuntimeAdapter):
    def __init__(self, clock: ManualClock) -> None:
        super().__init__(clock)
        self.stage = ""
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.read_count = 0

    async def pause(self) -> None:
        self.stage = ""  # Only the identity task pauses, not the following kick.
        self.entered.set()
        await self.release.wait()

    async def get(self, room_id: str) -> RoomRuntimeSnapshot | None:
        result = await super().get(room_id)
        self.read_count += 1
        if self.stage == "read" or (self.stage == "after-binding" and self.read_count == 2):
            await self.pause()
        return result

    async def change_identity(self, command: ChangeRoomIdentity) -> RoomMutationResult:
        if self.stage == "before-identity":
            await self.pause()
        result = await super().change_identity(command)
        if self.stage == "after-identity":
            await self.pause()
        return result


def session_command(token: str, actor_type: SessionActorType, actor_id: str) -> CreateSession:
    return CreateSession(
        session_digest=digest_opaque_token(token),
        actor_type=actor_type,
        actor_id=actor_id,
        csrf_digest=digest_opaque_token(f"csrf-{token}"),
        csrf_token=f"csrf-{token}",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["read", "before-identity", "after-identity", "after-binding"])
async def test_kick_during_login_does_not_restore_participation(stage: str) -> None:
    clock = ManualClock()
    runtime = PausedRuntime(clock)
    rooms = RoomApplicationService(runtime, UnusedPassword())
    sessions = InMemorySessionAdapter(clock)
    workflow = InMemorySessionWorkflow(sessions, rooms)
    owner = await workflow.create(session_command("owner", SessionActorType.MEMBER, "1"))
    guest = await workflow.create(session_command("guest", SessionActorType.GUEST, "guest-1"))
    created = await rooms.create_room(
        session=owner,
        request_id="create",
        config=RoomConfig(name="Race", minimum_ready=2),
        password=None,
    )
    assert created.snapshot is not None
    room_id = created.snapshot.room_id
    await rooms.join_room(
        session=guest, room_id=room_id, request_id="join", expected_state_version=1, password=None
    )
    binding = rooms.participation(guest.session_digest)
    assert binding is not None
    replacement = session_command("member", SessionActorType.MEMBER, "2")
    runtime.stage = stage
    runtime.read_count = 0
    rotation = asyncio.create_task(
        workflow.rotate_identity(previous=guest, replacement=replacement)
    )
    try:
        await asyncio.wait_for(runtime.entered.wait(), 2)
        snapshot = await runtime.get(room_id)
        assert snapshot is not None
        await rooms.kick_participant(
            session=owner,
            target_id=binding.participant_id,
            request_id="kick",
            expected_state_version=snapshot.state_version,
        )
    finally:
        runtime.release.set()
        await asyncio.wait_for(rotation, 2)
    assert await workflow.get(guest.session_digest) is None
    logged_in = await workflow.get(replacement.session_digest)
    assert logged_in is not None and logged_in.actor_type is SessionActorType.MEMBER
    assert rooms.current_room(guest.session_digest) is None
    assert rooms.current_room(replacement.session_digest) is None
    assert rooms.participant_identity(binding.participant_id) is None
    assert rooms.current_room(owner.session_digest) is not None
    final = await runtime.get(room_id)
    assert final is not None and len(final.participants) == 1
