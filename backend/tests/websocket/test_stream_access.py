from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from fastapi import WebSocket

from seokpan.api.identity import IdentityApiServices
from seokpan.api.realtime import ActiveWebSocketRegistry, StreamEnd, _stream_events
from seokpan.api.stream_access import StreamAccess, StreamAccessState
from seokpan.identity.application import (
    AuthSessionService,
    CreateSession,
    MemberIdentityService,
    SessionActorType,
    SessionRecord,
    SessionTransitionUnavailable,
    digest_opaque_token,
)
from seokpan.persistence.memory import InMemorySessionAdapter, ManualClock
from seokpan.persistence.memory.session_workflow import InMemorySessionWorkflow
from seokpan.room.application import RealtimeEvent, RealtimeSubscription, RoomApplicationService
from seokpan.room.application.lobby import RoomParticipation
from seokpan.settings import Settings


def command(digest: str, actor: SessionActorType) -> CreateSession:
    return CreateSession(
        session_digest=digest * 64,
        actor_type=actor,
        actor_id="1",
        csrf_digest=digest_opaque_token("x" * 43),
        csrf_token="x" * 43,
    )


class Binding:
    def __init__(self, session: SessionRecord) -> None:
        self.value: RoomParticipation | None = RoomParticipation(
            session.session_digest,
            "room",
            "participant",
            session.actor_type,
            session.actor_id,
        )
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.fail = False

    def participant_identity(self, _participant_id: str) -> RoomParticipation | None:
        return self.value

    async def resolve_participant_identity(self, _participant_id: str) -> RoomParticipation | None:
        return self.value

    async def change_identity(self, previous: SessionRecord, replacement: CreateSession) -> None:
        self.entered.set()
        await self.release.wait()
        if self.fail:
            raise ValueError("transition failed before binding")
        self.value = RoomParticipation(
            replacement.session_digest,
            "room",
            "participant",
            replacement.actor_type,
            replacement.actor_id,
        )

    async def leave(self, _current: SessionRecord) -> None:
        self.value = None


class UnusedTokens:
    def issue(self) -> str:
        raise AssertionError("checking access must not issue sessions")


@pytest.mark.asyncio
@pytest.mark.parametrize("room", [False, True])
async def test_identity_mismatch_is_not_authorized(
    room: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = InMemorySessionAdapter(ManualClock())
    previous = await store.create(command("a", SessionActorType.GUEST))
    binding = Binding(previous)
    sessions = AuthSessionService(InMemorySessionWorkflow(store, binding), UnusedTokens())
    identity = IdentityApiServices(
        Settings(environment="test"), cast(MemberIdentityService, None), sessions
    )
    monkeypatch.setattr(
        sessions, "find", AsyncMock(return_value=replace(previous, actor_id="other"))
    )
    access = StreamAccess(
        identity,
        previous,
        rooms=cast(RoomApplicationService, binding) if room else None,
        room_id="room" if room else None,
        participant_id="participant" if room else None,
    )
    with pytest.raises(SessionTransitionUnavailable):
        await access.check()


@pytest.mark.asyncio
async def test_repeated_binding_changes_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    store = InMemorySessionAdapter(ManualClock())
    previous = await store.create(command("a", SessionActorType.GUEST))
    binding = Binding(previous)
    sessions = AuthSessionService(InMemorySessionWorkflow(store, binding), UnusedTokens())
    identity = IdentityApiServices(
        Settings(environment="test"), cast(MemberIdentityService, None), sessions
    )
    calls = 0

    async def changing(_digest: str) -> SessionRecord:
        nonlocal calls
        calls += 1
        assert binding.value is not None
        binding.value = replace(binding.value, session_digest=str(calls) * 64)
        return previous

    monkeypatch.setattr(sessions, "find", changing)
    access = StreamAccess(
        identity,
        previous,
        rooms=cast(RoomApplicationService, binding),
        room_id="room",
        participant_id="participant",
    )
    with pytest.raises(SessionTransitionUnavailable):
        await access.check()
    assert calls == 3


@pytest.mark.asyncio
async def test_uncertain_rotation_write_blocks_both_session_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemorySessionAdapter(ManualClock())
    previous = await store.create(command("a", SessionActorType.GUEST))
    workflow = InMemorySessionWorkflow(store)
    original = store.rotate

    async def lost_response(
        *, previous_session_digest: str, replacement: CreateSession
    ) -> SessionRecord:
        await original(previous_session_digest=previous_session_digest, replacement=replacement)
        raise ConnectionError("unknown write result")

    monkeypatch.setattr(store, "rotate", lost_response)
    with pytest.raises(ConnectionError):
        await workflow.rotate_identity(
            previous=previous, replacement=command("b", SessionActorType.MEMBER)
        )
    for digest in ("a" * 64, "b" * 64):
        with pytest.raises(SessionTransitionUnavailable):
            await workflow.get(digest)


@pytest.mark.asyncio
async def test_partial_binding_rollback_is_not_expiry() -> None:
    store = InMemorySessionAdapter(ManualClock())
    previous = await store.create(command("a", SessionActorType.GUEST))
    binding = Binding(previous)
    binding.value = RoomParticipation("b" * 64, "room", "participant", SessionActorType.MEMBER, "1")
    identity = IdentityApiServices(
        Settings(environment="test"),
        cast(MemberIdentityService, None),
        AuthSessionService(InMemorySessionWorkflow(store, binding), UnusedTokens()),
    )
    access = StreamAccess(
        identity,
        previous,
        rooms=cast(RoomApplicationService, binding),
        room_id="room",
        participant_id="participant",
    )
    with pytest.raises(SessionTransitionUnavailable):
        await access.check()


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["shutdown", "replacement"])
async def test_control_change_during_access_read_prevents_stale_delivery(ending: str) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def check() -> StreamAccessState:
        entered.set()
        await release.wait()
        return StreamAccessState.EXPIRED

    async def receive() -> dict[str, object]:
        await asyncio.Event().wait()
        return {}

    socket = SimpleNamespace(receive=receive, send_json=AsyncMock(), close=AsyncMock())
    queue: asyncio.Queue[RealtimeEvent] = asyncio.Queue()
    queue.put_nowait(
        RealtimeEvent(
            event_type="game.move_applied",
            event_id="event",
            occurred_at="2026-09-07T00:00:00Z",
            state_version=2,
            payload={},
            room_id="room",
        )
    )
    registry = ActiveWebSocketRegistry()
    registry.begin_runtime()
    replaced = asyncio.Event()
    before = asyncio.all_tasks()
    task = asyncio.create_task(
        _stream_events(
            cast(WebSocket, socket),
            cast(RealtimeSubscription, SimpleNamespace(receive=queue.get)),
            registry,
            access=cast(StreamAccess, SimpleNamespace(check=check)),
            replaced=replaced,
            room_id="room",
            participant_id="participant",
            snapshot_version=1,
        )
    )
    await entered.wait()
    if ending == "shutdown":
        registry.end_runtime()
    else:
        replaced.set()
    release.set()
    result = await asyncio.wait_for(task, 1)
    assert result is (StreamEnd.SHUTDOWN if ending == "shutdown" else StreamEnd.REPLACED)
    socket.close.assert_awaited_once_with(code=1012 if ending == "shutdown" else 4001)
    assert all(
        call.args[0]["event_type"] != "game.move_applied"
        for call in socket.send_json.call_args_list
    )
    await asyncio.sleep(0)
    assert asyncio.all_tasks() <= before


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "rollback", "cancel"])
async def test_rotation_barrier_preserves_normal_participation(outcome: str) -> None:
    clock = ManualClock()
    store = InMemorySessionAdapter(clock)
    previous = await store.create(command("a", SessionActorType.GUEST))
    binding = Binding(previous)
    binding.fail = outcome == "rollback"
    workflow = InMemorySessionWorkflow(store, binding)
    identity = IdentityApiServices(
        Settings(environment="test"),
        cast(MemberIdentityService, None),
        AuthSessionService(workflow, UnusedTokens()),
    )
    access = StreamAccess(
        identity,
        previous,
        rooms=cast(RoomApplicationService, binding),
        room_id="room",
        participant_id="participant",
    )
    rotation = asyncio.create_task(
        workflow.rotate_identity(
            previous=previous, replacement=command("b", SessionActorType.MEMBER)
        )
    )
    await binding.entered.wait()
    with pytest.raises(SessionTransitionUnavailable):
        await workflow.rotate_identity(
            previous=previous, replacement=command("c", SessionActorType.MEMBER)
        )
    check = asyncio.create_task(access.check())
    await asyncio.sleep(0)
    assert not check.done()
    if outcome == "cancel":
        rotation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await rotation
    else:
        binding.release.set()
        if outcome == "rollback":
            with pytest.raises(ValueError):
                await rotation
        else:
            await rotation
    assert await check is StreamAccessState.ALLOWED
    assert binding.value is not None
    expected = "b" if outcome == "success" else "a"
    assert binding.value.session_digest == expected * 64
    assert await workflow.get(expected * 64) is not None
    if outcome == "success":
        clock.advance(7_200_000)
        assert await access.check() is StreamAccessState.EXPIRED


@pytest.mark.asyncio
async def test_uncertain_rollback_is_not_session_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    store = InMemorySessionAdapter(ManualClock())
    previous = await store.create(command("a", SessionActorType.GUEST))
    binding = Binding(previous)
    binding.fail = True
    binding.release.set()
    workflow = InMemorySessionWorkflow(store, binding)

    async def failed_restore(**_kwargs: object) -> SessionRecord:
        raise RuntimeError("unavailable")

    monkeypatch.setattr(store, "restore_after_failed_rotation", failed_restore)
    with pytest.raises(SessionTransitionUnavailable):
        await workflow.rotate_identity(
            previous=previous, replacement=command("b", SessionActorType.MEMBER)
        )
    for digest in ("a" * 64, "b" * 64):
        with pytest.raises(SessionTransitionUnavailable):
            await workflow.get(digest)


@pytest.mark.asyncio
async def test_checks_do_not_extend_idle_or_absolute_expiry() -> None:
    clock = ManualClock()
    store = InMemorySessionAdapter(clock)
    previous = await store.create(command("a", SessionActorType.GUEST))
    workflow = InMemorySessionWorkflow(store)
    identity = IdentityApiServices(
        Settings(environment="test"),
        cast(MemberIdentityService, None),
        AuthSessionService(workflow, UnusedTokens()),
    )
    access = StreamAccess(identity, previous)
    for _ in range(3):
        clock.advance(1_000_000)
        assert await access.check() is StreamAccessState.ALLOWED
        assert await store.get(previous.session_digest) == previous
    clock.advance(4_200_000)
    assert await access.check() is StreamAccessState.EXPIRED


@pytest.mark.asyncio
async def test_missing_or_different_room_binding_ends_access() -> None:
    store = InMemorySessionAdapter(ManualClock())
    previous = await store.create(command("a", SessionActorType.GUEST))
    binding = Binding(previous)
    workflow = InMemorySessionWorkflow(store, binding)
    identity = IdentityApiServices(
        Settings(environment="test"),
        cast(MemberIdentityService, None),
        AuthSessionService(workflow, UnusedTokens()),
    )
    access = StreamAccess(
        identity,
        previous,
        rooms=cast(RoomApplicationService, binding),
        room_id="another-room",
        participant_id="participant",
    )
    assert await access.check() is StreamAccessState.LEFT
    binding.value = None
    assert await access.check() is StreamAccessState.LEFT


@pytest.mark.asyncio
async def test_room_access_rejects_superseded_shared_connection_generation() -> None:
    store = InMemorySessionAdapter(ManualClock())
    previous = await store.create(command("a", SessionActorType.MEMBER))
    binding = Binding(previous)
    assert binding.value is not None
    binding.value = replace(
        binding.value,
        connection_generation=2,
        connected=True,
    )
    identity = IdentityApiServices(
        Settings(environment="test"),
        cast(MemberIdentityService, None),
        AuthSessionService(InMemorySessionWorkflow(store, binding), UnusedTokens()),
    )
    access = StreamAccess(
        identity,
        previous,
        rooms=cast(RoomApplicationService, binding),
        room_id="room",
        participant_id="participant",
        connection_generation=1,
    )

    assert await access.check() is StreamAccessState.REPLACED


@pytest.mark.asyncio
async def test_room_access_requires_current_generation_to_remain_connected() -> None:
    store = InMemorySessionAdapter(ManualClock())
    previous = await store.create(command("a", SessionActorType.MEMBER))
    binding = Binding(previous)
    assert binding.value is not None
    binding.value = replace(
        binding.value,
        connection_generation=3,
        connected=False,
    )
    identity = IdentityApiServices(
        Settings(environment="test"),
        cast(MemberIdentityService, None),
        AuthSessionService(InMemorySessionWorkflow(store, binding), UnusedTokens()),
    )
    access = StreamAccess(
        identity,
        previous,
        rooms=cast(RoomApplicationService, binding),
        room_id="room",
        participant_id="participant",
        connection_generation=3,
    )

    assert await access.check() is StreamAccessState.REPLACED


@pytest.mark.asyncio
async def test_stream_access_replacement_sends_reconnect_required_and_closes_4001() -> None:
    async def receive() -> dict[str, object]:
        await asyncio.Event().wait()
        return {}

    async def receive_event() -> RealtimeEvent:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    socket = SimpleNamespace(receive=receive, send_json=AsyncMock(), close=AsyncMock())
    subscription = cast(
        RealtimeSubscription,
        SimpleNamespace(receive=receive_event),
    )
    registry = ActiveWebSocketRegistry()
    registry.begin_runtime()
    result = await asyncio.wait_for(
        _stream_events(
            cast(WebSocket, socket),
            subscription,
            registry,
            access=cast(
                StreamAccess,
                SimpleNamespace(check=AsyncMock(return_value=StreamAccessState.REPLACED)),
            ),
            room_id="room",
            participant_id="participant",
            state_version=lambda: 9,
            snapshot_version=8,
        ),
        2,
    )

    assert result is StreamEnd.REPLACED
    message = socket.send_json.await_args.args[0]
    assert message["event_type"] == "connection.reconnect_required"
    assert message["state_version"] == 9
    assert message["payload"] == {"reason": "CONNECTION_REPLACED"}
    socket.close.assert_awaited_once_with(code=4001)
