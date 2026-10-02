"""Captured-proof guards at adapter boundaries; actual Lua has a separate gate."""

import asyncio
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest

from seokpan.clock import MillisecondClock
from seokpan.game.domain import Stone
from seokpan.persistence.memory.room_adapter import InMemoryRoomRuntimeAdapter
from seokpan.persistence.memory.start_initialization import InMemoryCapturedVoteInitializer
from seokpan.persistence.memory.vote_adapter import InMemoryVoteRuntimeAdapter
from seokpan.persistence.redis.vote_adapter import RedisVoteRuntimeAdapter
from seokpan.persistence.redis.vote_scripts import VOTE_MUTATION
from seokpan.room.domain import RoomStatus
from seokpan.vote.application import InitializeVoteRuntime
from seokpan.vote.domain import Voter, VoteRuleViolation

ROOM, GAME = "00000000-0000-4000-8000-000000000101", "00000000-0000-4000-8000-000000000102"


def command(previous: bool = False) -> InitializeVoteRuntime:
    return InitializeVoteRuntime(
        ROOM,
        "same-request",
        GAME,
        (
            Voter("black", Stone.BLACK),
            Voter("white", Stone.WHITE),
        ),
        1000,
        1,
        previous_game_id="00000000-0000-4000-8000-000000000105" if previous else None,
        previous_turn_no=4 if previous else None,
    )


def memory_adapter():
    clock = cast(MillisecondClock, SimpleNamespace(now_ms=0))
    adapter = InMemoryVoteRuntimeAdapter(clock)
    intents, phases = {}, {}
    adapter.bind_captured_start_records(intents, phases)
    return adapter, intents, phases, clock


@pytest.mark.asyncio
@pytest.mark.parametrize("store_name", ["intent", "phase"])
@pytest.mark.parametrize("value", [None, "", "PENDING", "INITIALIZED", "CLOSED", "broken"])
@pytest.mark.parametrize("cached", [False, True])
async def test_existing_proof_blocks_legacy_before_cache_or_mutation(store_name, value, cached):
    adapter, intents, phases, _ = memory_adapter()
    (intents if store_name == "intent" else phases)[(ROOM, GAME)] = value
    replay = Mock(return_value=object() if cached else None)
    adapter._replay = replay
    with pytest.raises(VoteRuleViolation, match="^CAPTURED_START_REQUIRED$"):
        await adapter.initialize(command())
    replay.assert_not_called()
    assert not adapter._states and not adapter._requests


@pytest.mark.asyncio
async def test_guard_observes_capture_during_async_room_lookup():
    adapter, _, phases, _ = memory_adapter()

    async def lookup(room_id):
        phases[(room_id, GAME)] = "PENDING"
        return SimpleNamespace(
            status=RoomStatus.PLAYING,
            game_id=GAME,
            last_game_id=None,
            last_game_turn_no=None,
        )

    adapter._room_lookup = lookup
    adapter._replay = Mock(return_value=object())
    with pytest.raises(VoteRuleViolation, match="CAPTURED_START_REQUIRED"):
        await adapter.initialize(command())
    adapter._replay.assert_not_called()
    assert not adapter._states


@pytest.mark.asyncio
async def test_unmarked_legacy_start_and_retry_still_work():
    adapter, intents, phases, _ = memory_adapter()
    intents[(ROOM, "00000000-0000-4000-8000-000000000106")] = "unrelated"
    phases[("other-room", GAME)] = "unrelated"
    first = await adapter.initialize(command())
    retry = await adapter.initialize(command())
    assert first.snapshot.game_id == GAME and not first.replayed
    assert retry.replayed and retry.snapshot == first.snapshot
    assert len(adapter._states) == 1


@pytest.mark.asyncio
async def test_binding_uses_live_maps_and_blocks_after_runtime_discard():
    adapter, _, phases, _ = memory_adapter()
    await adapter.initialize(command())
    phases[(ROOM, GAME)] = "INITIALIZED"
    await adapter.discard_game(ROOM, GAME)
    with pytest.raises(VoteRuleViolation, match="CAPTURED_START_REQUIRED"):
        await adapter.initialize(command())
    assert not adapter._states and phases[(ROOM, GAME)] == "INITIALIZED"


@pytest.mark.asyncio
async def test_room_lookup_cancellation_is_not_a_legacy_fallback():
    adapter, _, _, _ = memory_adapter()
    adapter._room_lookup = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await adapter.initialize(command())
    assert not adapter._states and not adapter._requests


def test_binding_different_store_instances_fails_without_losing_original_guard():
    adapter, intents, phases, _ = memory_adapter()
    adapter.bind_captured_start_records(intents, phases)
    with pytest.raises(ValueError, match="CAPTURED_START_STORE_MISMATCH"):
        adapter.bind_captured_start_records({}, phases)
    with pytest.raises(ValueError, match="CAPTURED_START_STORE_MISMATCH"):
        adapter.bind_captured_start_records(intents, {})
    assert adapter._captured_start_records[0] is intents
    assert adapter._captured_start_records[1] is phases


def test_captured_initializer_binds_its_actual_room_stores():
    clock = cast(MillisecondClock, SimpleNamespace(now_ms=0))
    votes = InMemoryVoteRuntimeAdapter(clock)
    rooms = SimpleNamespace(_start_intents={}, _start_phases={})
    InMemoryCapturedVoteInitializer(
        rooms=cast(InMemoryRoomRuntimeAdapter, rooms),
        votes=votes,
        clock=clock,
    )
    assert votes._captured_start_records[0] is rooms._start_intents
    assert votes._captured_start_records[1] is rooms._start_phases


@pytest.mark.asyncio
@pytest.mark.parametrize("previous", [False, True])
async def test_redis_initialization_declares_guard_keys_after_predecessor(previous):
    adapter = RedisVoteRuntimeAdapter(SimpleNamespace())
    adapter._scripts.execute = AsyncMock(
        return_value='{"ok":false,"error":"CAPTURED_START_REQUIRED"}',
    )
    with pytest.raises(VoteRuleViolation, match="CAPTURED_START_REQUIRED"):
        await adapter.initialize(command(previous))
    call = adapter._scripts.execute.await_args
    keys = call.kwargs["keys"]
    assert call.args[0] is VOTE_MUTATION
    assert len(keys) == (18 if previous else 15)
    assert keys[-2:] == (
        "stone:v1:room:{00000000-0000-4000-8000-000000000101}:start-intent:00000000-0000-4000-8000-000000000102",
        "stone:v1:room:{00000000-0000-4000-8000-000000000101}:start-phase:00000000-0000-4000-8000-000000000102",
    )
    assert all("{00000000-0000-4000-8000-000000000101}" in key for key in keys)
    if previous:
        assert keys[13:16] == (
            "stone:v1:room:{00000000-0000-4000-8000-000000000101}:votes:4",
            "stone:v1:room:{00000000-0000-4000-8000-000000000101}:vote-tally:4",
            "stone:v1:room:{00000000-0000-4000-8000-000000000101}:resolver:4",
        )


def test_lua_guard_precedes_cache_expiry_and_does_not_decode_partial_proof():
    source = VOTE_MUTATION.source
    assert VOTE_MUTATION.version == 8
    assert source.index("CAPTURED_START_REQUIRED") < source.index("local expired")
    assert source.index("CAPTURED_START_REQUIRED") < source.index("if cached then")
    assert "redis.call('EXISTS', KEYS[key_count - 1], KEYS[key_count])" in source
