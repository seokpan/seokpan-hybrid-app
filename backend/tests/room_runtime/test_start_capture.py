"""Atomic-capture adapter boundary tests; Redis execution is a separate gate."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from seokpan.persistence.memory.room_adapter import InMemoryRoomRuntimeAdapter, _RoomState
from seokpan.persistence.redis.room_adapter import RedisRoomRuntimeAdapter
from seokpan.persistence.redis.start_capture_script import ROOM_START_CAPTURE, start_intent_key
from seokpan.room.application.runtime import RoomRuntimeParticipant, RoomRuntimeSnapshot
from seokpan.room.application.start_capture import CaptureRoomGameStart, accepted_start_intent
from seokpan.room.application.start_intent import StartIntentPlayer
from seokpan.room.domain import ActorType, RoomConfig, RoomRuleViolation, RoomStatus, Team

ROOM = "00000000-0000-4000-8000-000000000001"
GAME = "00000000-0000-4000-8000-000000000002"
BLACK = "00000000-0000-4000-8000-000000000003"
WHITE = "00000000-0000-4000-8000-000000000004"
OWNER = "00000000-0000-4000-8000-000000000005"


@pytest.fixture
def command() -> CaptureRoomGameStart:
    return CaptureRoomGameStart(
        room_id=ROOM,
        request_id="start-1",
        actor_id=OWNER,
        game_id=GAME,
        expected_state_version=6,
        players=(
            StartIntentPlayer(BLACK, "BLACK", member_id="18446744073709551615"),
            StartIntentPlayer(WHITE, "WHITE", guest_label="Guest-0001"),
        ),
    )


@pytest.fixture
def waiting() -> RoomRuntimeSnapshot:
    return RoomRuntimeSnapshot(
        room_id=ROOM,
        config=RoomConfig(name="room", minimum_ready=2),
        status=RoomStatus.WAITING,
        owner_id=OWNER,
        state_version=6,
        participants=(
            RoomRuntimeParticipant(OWNER, ActorType.MEMBER, 1, True, Team.NONE, False),
            RoomRuntimeParticipant(BLACK, ActorType.MEMBER, 2, True, Team.BLACK, True),
            RoomRuntimeParticipant(WHITE, ActorType.GUEST, 3, True, Team.WHITE, True),
        ),
    )


def memory(waiting: RoomRuntimeSnapshot) -> tuple[InMemoryRoomRuntimeAdapter, Mock]:
    # Stub only the Domain transition: these tests observe Adapter admission and storage order.
    domain = Mock()
    for field in ("config", "status", "owner_id", "state_version", "participants", "game_id"):
        setattr(domain, field, getattr(waiting, field))

    def start_game(*, actor_id: str, game_id: str) -> object:
        assert actor_id == waiting.owner_id
        domain.status = RoomStatus.PLAYING
        domain.game_id = game_id
        domain.state_version += 1
        return SimpleNamespace(entries=())

    domain.start_game = Mock(side_effect=start_game)
    adapter = InMemoryRoomRuntimeAdapter(SimpleNamespace(now_ms=123_456_789_012_345))
    adapter._rooms[ROOM] = _RoomState(domain, None, {})
    return adapter, domain


def test_candidate_sorting_and_member_id_precision(command: CaptureRoomGameStart) -> None:
    reordered = replace(command, players=tuple(reversed(command.players)))
    assert command == reordered
    assert json.loads(command.players_json())[0]["member_id"] == "18446744073709551615"


def test_capture_uses_provider_time_config_and_spectator_owner(
    command: CaptureRoomGameStart,
    waiting: RoomRuntimeSnapshot,
) -> None:
    room = replace(waiting, config=RoomConfig(name="r", minimum_ready=2, vote_seconds=30))
    intent = accepted_start_intent(command, room, 123_456_789_012_345)
    assert intent.started_at_ms == 123_456_789_012_345
    assert intent.accepted_state_version == 7
    assert intent.vote_seconds == 30
    assert intent.owner_id not in {p.participant_id for p in intent.players}


@pytest.mark.asyncio
async def test_memory_captures_once_and_replay_does_not_reset_time(
    command: CaptureRoomGameStart,
    waiting: RoomRuntimeSnapshot,
) -> None:
    adapter, domain = memory(waiting)
    first = await adapter.start_game(command)
    intent = await adapter.get_start_intent(ROOM, GAME)
    assert intent is not None
    assert first.operation_at_ms == intent.started_at_ms
    assert first.snapshot.status is RoomStatus.PLAYING
    adapter._clock.now_ms += 1_000
    replay = await adapter.start_game(command)
    assert replay.replayed
    assert await adapter.get_start_intent(ROOM, GAME) is intent
    domain.start_game.assert_called_once()
    assert adapter._start_phases[(ROOM, GAME)] == "PENDING"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["team", "identity", "missing", "extra", "version", "owner"])
async def test_memory_rejection_writes_neither_room_nor_intent(
    command: CaptureRoomGameStart,
    waiting: RoomRuntimeSnapshot,
    change: str,
) -> None:
    participants = list(waiting.participants)
    if change == "team":
        participants[1] = replace(participants[1], team=Team.WHITE)
    elif change == "identity":
        participants[2] = replace(participants[2], actor_type=ActorType.MEMBER)
    elif change == "missing":
        participants = participants[:2]
    elif change == "extra":
        participants[0] = replace(participants[0], ready=True, team=Team.BLACK)
    room = replace(waiting, participants=tuple(participants))
    if change == "version":
        room = replace(room, state_version=7)
    elif change == "owner":
        room = replace(room, owner_id=BLACK)
    adapter, domain = memory(room)
    with pytest.raises(RoomRuleViolation):
        await adapter.start_game(command)
    domain.start_game.assert_not_called()
    assert await adapter.get_start_intent(ROOM, GAME) is None
    assert domain.status is RoomStatus.WAITING


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["identity", "request", "closed", "phase_missing"])
async def test_accepted_intent_cannot_be_overwritten_or_replayed_without_evidence(
    command: CaptureRoomGameStart,
    waiting: RoomRuntimeSnapshot,
    change: str,
) -> None:
    adapter, _ = memory(waiting)
    await adapter.start_game(command)
    original = await adapter.get_start_intent(ROOM, GAME)
    retry = command
    if change == "identity":
        retry = replace(
            command,
            players=(
                replace(command.players[0], member_id="9"),
                command.players[1],
            ),
        )
    elif change == "request":
        retry = replace(command, request_id="new-start")
    elif change == "closed":
        adapter._rooms.pop(ROOM)
    else:
        adapter._start_phases.pop((ROOM, GAME))
    with pytest.raises(RoomRuleViolation):
        await adapter.start_game(retry)
    assert await adapter.get_start_intent(ROOM, GAME) is original


@pytest.mark.asyncio
async def test_redis_dispatch_uses_one_script_and_explicit_same_slot_keys(
    command: CaptureRoomGameStart,
) -> None:
    adapter = RedisRoomRuntimeAdapter(Mock())
    adapter._scripts.execute = AsyncMock(return_value=json.dumps({"ok": True}))
    adapter._mutation_result = Mock(return_value="captured")
    assert await adapter.start_game(command) == "captured"
    call = adapter._scripts.execute.await_args
    assert call.args == (ROOM_START_CAPTURE,)
    assert len(call.kwargs["keys"]) == 9
    assert all(f"{{{ROOM}}}" in key for key in call.kwargs["keys"])
    assert len(set(call.kwargs["keys"])) == 9
    assert json.loads(call.kwargs["args"][5])[0]["member_id"] == "18446744073709551615"


@pytest.mark.asyncio
async def test_redis_reads_original_intent_independently_of_live_room(
    command: CaptureRoomGameStart,
    waiting: RoomRuntimeSnapshot,
) -> None:
    intent = accepted_start_intent(command, waiting, 10_001)
    client = Mock()
    client.get = AsyncMock(return_value=intent.to_json().encode())
    adapter = RedisRoomRuntimeAdapter(client)
    assert await adapter.get_start_intent(ROOM, GAME) == intent
    client.get.assert_awaited_once_with(start_intent_key(ROOM, GAME))


def test_lua_has_prewrite_guards_and_no_pending_record_ttl() -> None:
    script = ROOM_START_CAPTURE.source
    write = script.index("redis.call('SET', KEYS[8], intent_json)")
    assert script.index("START_CAPTURE_KEY_TYPE") < write
    assert script.index("START_INTENT_ROSTER_CHANGED") < write
    assert script.index("local cached_result = exact_json") < write
    assert "cjson.encode_number_precision(16)" not in script
    assert "redis.call('SET', KEYS[9], 'PENDING')" in script
    assert "PEXPIRE', KEYS[8]" not in script
    assert "PEXPIRE', KEYS[9]" not in script
    assert "tonumber(player.member_id)" not in script
