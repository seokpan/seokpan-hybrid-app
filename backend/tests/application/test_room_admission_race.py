"""F05 regression: per-Room versions do not serialize a Session across Rooms.

The guarded Memory Provider uses the same committed Room store as both services.
Actual Redis/2-Pod evidence is a separate gate.
"""

import asyncio
from types import SimpleNamespace

import pytest

from seokpan.identity.application import SessionActorType, SessionRecord
from seokpan.identity.application.session import digest_opaque_token
from seokpan.persistence.memory.room_admission import (
    SessionAdmissionMemoryRoomAdapter as InMemoryRoomRuntimeAdapter,
)
from seokpan.room.application.lobby import RoomApplicationService, RoomParticipation
from seokpan.room.domain import RoomConfig, RoomRuleViolation


class PauseFirstAdmission(InMemoryRoomRuntimeAdapter):
    """Delay only the first target write; other calls use the actual Memory adapter."""

    def __init__(self) -> None:
        super().__init__(SimpleNamespace(now_ms=1_000))
        self.target_digest: str | None = None
        self.paused = asyncio.Event()
        self.release = asyncio.Event()
        self.held_once = False

    async def _pause(self, digest: str) -> None:
        if digest == self.target_digest and not self.held_once:
            self.held_once = True
            self.paused.set()
            await self.release.wait()

    async def create(self, command):
        await self._pause(command.owner_session_digest)
        return await super().create(command)

    async def join(self, command):
        await self._pause(command.session_digest)
        return await super().join(command)

    def memberships(self, digest: str) -> list[tuple[str, str]]:
        return [
            (room_id, participant_id)
            for room_id, state in self._rooms.items()
            for participant_id, connection in state.connections.items()
            if connection.session_digest == digest
        ]


class SharedParticipationRead:
    """Read committed memberships, not either service's process-local binding cache."""

    def __init__(self, runtime: PauseFirstAdmission) -> None:
        self.runtime = runtime

    async def by_session(self, digest: str) -> RoomParticipation | None:
        matches = self.runtime.memberships(digest)
        if not matches:
            return None
        room_id, participant_id = matches[0]
        return RoomParticipation(
            session_digest=digest,
            room_id=room_id,
            participant_id=participant_id,
            actor_type=SessionActorType.MEMBER,
            actor_id="3",
        )


class UnusedPasswords:
    async def encode(self, value: str) -> str:
        raise AssertionError("public admission must not encode a password")

    async def verify(self, encoded: str, supplied: str) -> bool:
        raise AssertionError("public admission must not verify a password")


def session(number: int) -> SessionRecord:
    token = f"synthetic-csrf-{number}"
    return SessionRecord(
        session_digest=f"{number:064x}",
        actor_type=SessionActorType.MEMBER,
        actor_id=str(number),
        csrf_digest=digest_opaque_token(token),
        csrf_token=token,
        created_at_ms=0,
        last_activity_at_ms=0,
        absolute_expires_at_ms=86_400_000,
    )


def service(runtime: PauseFirstAdmission) -> RoomApplicationService:
    return RoomApplicationService(
        runtime,
        UnusedPasswords(),
        participation_resolver=SharedParticipationRead(runtime),
    )


async def prepared():
    runtime = PauseFirstAdmission()
    seed = service(runtime)
    rooms = []
    for number in (1, 2):
        created = await seed.create_room(
            session=session(number),
            request_id=f"seed-{number}",
            config=RoomConfig(name=f"room-{number}", minimum_ready=2),
            password=None,
        )
        if created.snapshot is None:
            pytest.fail("seed Room creation unexpectedly failed")
        rooms.append(created.snapshot.room_id)
    return runtime, rooms


@pytest.mark.asyncio
async def test_guarded_memory_join_uses_current_state_when_observation_is_stale():
    runtime, rooms = await prepared()
    app = service(runtime)
    actor = session(3)
    snapshot = await runtime.get(rooms[0])
    assert snapshot is not None

    joined = await app.join_room(
        session=actor,
        room_id=rooms[0],
        request_id="stale-observation",
        expected_state_version=snapshot.state_version + 10,
        password=None,
    )

    assert joined.snapshot is not None
    memberships = runtime.memberships(actor.session_digest)
    assert len(memberships) == 1
    assert memberships[0][0] == rooms[0]
    assert memberships[0][1] in {
        participant.participant_id for participant in joined.snapshot.participants
    }


async def admit(app, actor, action, room_id, request_id):
    if action == "create":
        return await app.create_room(
            session=actor,
            request_id=request_id,
            config=RoomConfig(name=request_id, minimum_ready=2),
            password=None,
        )
    return await app.join_room(
        session=actor,
        room_id=room_id,
        request_id=request_id,
        expected_state_version=1,
        password=None,
    )


async def overlap(runtime, first, second):
    first_task = asyncio.create_task(first)
    second_task = None
    try:
        async with asyncio.timeout(2):
            await runtime.paused.wait()
            second_task = asyncio.create_task(second)
            # One scheduling turn, not a wall-clock race: let the second call run
            # or wait for a future shared guard before releasing the first writer.
            await asyncio.sleep(0)
            runtime.release.set()
            return await asyncio.gather(first_task, second_task, return_exceptions=True)
    finally:
        runtime.release.set()
        if second_task is None:
            second.close()
        tasks = [task for task in (first_task, second_task) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("service_count", [1, 2])
@pytest.mark.parametrize(
    "actions",
    [("join", "join"), ("create", "join"), ("create", "create")],
)
async def test_one_session_cannot_be_admitted_to_two_rooms(service_count, actions):
    runtime, rooms = await prepared()
    first = service(runtime)
    second = first if service_count == 1 else service(runtime)
    actor = session(3)
    runtime.target_digest = actor.session_digest
    outcomes = await overlap(
        runtime,
        admit(first, actor, actions[0], rooms[0], "first-admission"),
        admit(second, actor, actions[1], rooms[1], "second-admission"),
    )
    for outcome in outcomes:
        if isinstance(outcome, BaseException) and not isinstance(outcome, RoomRuleViolation):
            raise outcome  # Setup, timeout and provider errors are not expected failures.
    memberships = runtime.memberships(actor.session_digest)
    successes = sum(not isinstance(outcome, BaseException) for outcome in outcomes)
    assert len(memberships) == successes == 1, (
        f"F05 reproduced: {len(memberships)} committed memberships / {successes} admissions"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("service_count", [1, 2])
async def test_sequential_other_room_admission_is_rejected(service_count):
    runtime, rooms = await prepared()
    first = service(runtime)
    second = first if service_count == 1 else service(runtime)
    actor = session(3)
    await admit(first, actor, "join", rooms[0], "first")
    with pytest.raises(RoomRuleViolation, match="SESSION_ALREADY_IN_ROOM"):
        await admit(second, actor, "join", rooms[1], "second")
    assert len(runtime.memberships(actor.session_digest)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("service_count", [1, 2])
async def test_same_session_same_room_overlap_still_has_one_admission(service_count):
    runtime, rooms = await prepared()
    first = service(runtime)
    second = first if service_count == 1 else service(runtime)
    actor = session(3)
    runtime.target_digest = actor.session_digest
    outcomes = await overlap(
        runtime,
        admit(first, actor, "join", rooms[0], "first"),
        admit(second, actor, "join", rooms[0], "second"),
    )
    failures = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
    assert len(failures) == 1
    assert isinstance(failures[0], RoomRuleViolation)
    assert failures[0].code == "SESSION_ALREADY_IN_ROOM"
    assert len(runtime.memberships(actor.session_digest)) == 1
