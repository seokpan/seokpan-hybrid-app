"""Initializer boundary tests; not Redis, HTTP or full Domain evidence."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest

from seokpan.clock import MillisecondClock
from seokpan.persistence.memory.room_adapter import InMemoryRoomRuntimeAdapter
from seokpan.persistence.memory.start_initialization import InMemoryCapturedVoteInitializer
from seokpan.persistence.memory.vote_adapter import InMemoryVoteRuntimeAdapter
from seokpan.room.application.start_intent import RoomGameStartIntent, StartIntentPlayer
from seokpan.room.domain import RoomStatus
from seokpan.vote.application.start_initialization import InitializeCapturedGame
from seokpan.vote.domain import VoteRuleViolation

ROOM = "00000000-0000-4000-8000-000000000001"
GAME = "00000000-0000-4000-8000-000000000002"
BLACK = "00000000-0000-4000-8000-000000000003"
WHITE = "00000000-0000-4000-8000-000000000004"


@pytest.fixture
def memory() -> SimpleNamespace:
    intent = RoomGameStartIntent(
        room_id=ROOM,
        game_id=GAME,
        original_request_id="accepted",
        owner_id=BLACK,
        accepted_state_version=7,
        started_at_ms=1000,
        vote_seconds=15,
        players=(
            StartIntentPlayer(BLACK, "BLACK", member_id="1"),
            StartIntentPlayer(WHITE, "WHITE", guest_label="Guest-0001"),
        ),
    )
    room = SimpleNamespace(
        status=RoomStatus.PLAYING,
        game_id=GAME,
        owner_id=BLACK,
        state_version=7,
        last_game_id=None,
        last_game_turn_no=None,
        participants=(
            SimpleNamespace(participant_id=BLACK, connected=True),
            SimpleNamespace(participant_id=WHITE, connected=False),
        ),
    )
    rooms = SimpleNamespace(
        _rooms={ROOM: object()},
        _start_intents={(ROOM, GAME): intent},
        _start_phases={(ROOM, GAME): "PENDING"},
        _snapshot=lambda *args: room,
    )
    votes = SimpleNamespace(
        bind_captured_start_records=Mock(),
        _states={},
        _snapshot=lambda room_id, state: SimpleNamespace(
            room_id=room_id,
            game_id=state.game.game_id,
            turn_no=state.game.turn_no,
            deadline_ms=state.game.deadline_ms,
            participants=state.game.participants,
        ),
    )
    clock = SimpleNamespace(now_ms=5000)
    init = InMemoryCapturedVoteInitializer(
        rooms=cast(InMemoryRoomRuntimeAdapter, rooms),
        votes=cast(InMemoryVoteRuntimeAdapter, votes),
        clock=cast(MillisecondClock, clock),
    )
    votes.bind_captured_start_records.assert_called_once_with(
        rooms._start_intents,
        rooms._start_phases,
    )
    command = InitializeCapturedGame(intent, "init-1", BLACK, 7)
    return SimpleNamespace(**locals())


@pytest.mark.asyncio
async def test_first_initialization_writes_witness_and_uses_provider_clock(memory):
    first = await memory.init.initialize(memory.command)
    assert first.snapshot.deadline_ms == 20000
    assert first.snapshot.participants[1].connected is False
    witness = json.loads(memory.rooms._start_phases[(ROOM, GAME)])
    assert witness["phase"] == "INITIALIZED"
    assert witness["first_deadline_ms"] == 20000
    assert witness["initialized_at_ms"] == 5000
    assert memory.rooms._start_intents[(ROOM, GAME)] == memory.intent


@pytest.mark.asyncio
@pytest.mark.parametrize("request_id", ["init-1", "new-retry"])
async def test_retries_do_not_extend_deadline_or_rewind_a_pass_only_game(memory, request_id):
    await memory.init.initialize(memory.command)
    witness = memory.rooms._start_phases[(ROOM, GAME)]
    memory.votes._states[ROOM] = SimpleNamespace(
        game=SimpleNamespace(
            game_id=GAME,
            turn_no=2,
            deadline_ms=35000,
            participants=(),
        )
    )
    memory.clock.now_ms = 12000
    result = await memory.init.initialize(replace(memory.command, request_id=request_id))
    assert result.replayed and result.snapshot.turn_no == 2
    assert result.snapshot.deadline_ms == 35000
    assert memory.rooms._start_phases[(ROOM, GAME)] == witness


@pytest.mark.asyncio
async def test_initialized_runtime_loss_never_creates_an_empty_game(memory):
    await memory.init.initialize(memory.command)
    memory.votes._states.clear()
    with pytest.raises(VoteRuleViolation, match="GAME_START_RECOVERY_REQUIRED"):
        await memory.init.initialize(memory.command)
    assert not memory.votes._states


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", [None, "CLOSED", "broken", '{"phase":"INITIALIZED"}'])
async def test_missing_or_untrusted_phase_is_not_pending(memory, phase):
    memory.rooms._start_phases[(ROOM, GAME)] = phase
    with pytest.raises(VoteRuleViolation, match="GAME_START_RECOVERY_REQUIRED"):
        await memory.init.initialize(memory.command)
    assert not memory.votes._states


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["closed", "version", "owner", "departed", "wrong_intent"])
async def test_admission_change_does_not_write_runtime_or_witness(memory, change):
    if change == "closed":
        memory.room.status = RoomStatus.CLOSED
    elif change == "version":
        memory.room.state_version = 8
    elif change == "owner":
        memory.room.owner_id = WHITE
    elif change == "departed":
        memory.room.participants = memory.room.participants[:1]
    else:
        memory.rooms._start_intents[(ROOM, GAME)] = None
    with pytest.raises(VoteRuleViolation):
        await memory.init.initialize(memory.command)
    assert not memory.votes._states
    assert memory.rooms._start_phases[(ROOM, GAME)] == "PENDING"


@pytest.mark.asyncio
async def test_simultaneous_ensure_calls_initialize_once_in_shared_memory_adapter(memory):
    results = await asyncio.gather(
        memory.init.initialize(memory.command),
        memory.init.initialize(replace(memory.command, request_id="init-2")),
    )
    assert [result.replayed for result in results] == [False, True]
    assert len(memory.votes._states) == 1


@pytest.mark.parametrize("version", [True, 6, 7.0, 2**53])
def test_internal_command_rejects_bad_versions(memory, version):
    with pytest.raises(VoteRuleViolation):
        replace(memory.command, expected_room_version=version)


@pytest.fixture
def redis_boundary(memory):
    from seokpan.persistence.redis.start_initialization import RedisCapturedVoteInitializer
    from seokpan.persistence.redis.vote_adapter import RedisVoteRuntimeAdapter

    client = SimpleNamespace(get=AsyncMock(return_value=b"PENDING"))
    votes = RedisVoteRuntimeAdapter(client)
    votes.get = AsyncMock()
    initializer = RedisCapturedVoteInitializer(client, votes)
    initializer._scripts.execute = AsyncMock()
    return SimpleNamespace(client=client, votes=votes, init=initializer, command=memory.command)


@pytest.mark.asyncio
async def test_redis_replay_reads_current_snapshot_without_initial_response_reuse(redis_boundary):
    r = redis_boundary
    r.init._scripts.execute.return_value = '{"ok":true,"replayed":true}'
    live = SimpleNamespace(game_id=GAME, turn_no=2, deadline_ms=35000)
    r.votes.get.return_value = live
    result = await r.init.initialize(r.command)
    assert result.replayed and result.snapshot is live
    r.votes.get.assert_awaited_once_with(ROOM)
    args = r.init._scripts.execute.await_args.kwargs
    assert len(args["keys"]) == 13
    assert all("{" + ROOM + "}" in key for key in args["keys"])
    assert args["args"][0] == r.command.intent.to_json()
    assert 35000 not in args["args"]


@pytest.mark.asyncio
@pytest.mark.parametrize("live", [None, SimpleNamespace(game_id="another-game")])
async def test_redis_replay_snapshot_race_fails_closed(redis_boundary, live):
    r = redis_boundary
    r.init._scripts.execute.return_value = '{"ok":true,"replayed":true}'
    r.votes.get.return_value = live
    with pytest.raises(VoteRuleViolation, match="GAME_START_RECOVERY_REQUIRED"):
        await r.init.initialize(r.command)
    r.init._scripts.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_redis_declares_previous_turn_keys(redis_boundary):
    r = redis_boundary
    old = "00000000-0000-4000-8000-000000000009"
    intent = replace(r.command.intent, previous_game_id=old, previous_turn_no=8)
    r.init._scripts.execute.return_value = '{"ok":false,"error":"STALE_GAME"}'
    with pytest.raises(VoteRuleViolation, match="STALE_GAME"):
        await r.init.initialize(replace(r.command, intent=intent))
    assert len(r.init._scripts.execute.await_args.kwargs["keys"]) == 16
    r.votes.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_redis_cancellation_does_not_fall_back_to_legacy_initialization(redis_boundary):
    r = redis_boundary
    r.init._scripts.execute.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await r.init.initialize(r.command)
    r.votes.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_phase_reader_does_not_invent_pending(redis_boundary):
    r = redis_boundary
    assert await r.init.get_phase(r.command.intent) == "PENDING"
    r.client.get.return_value = None
    assert await r.init.get_phase(r.command.intent) is None
