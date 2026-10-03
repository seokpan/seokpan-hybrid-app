"""Application boundaries; real Provider integration remains a separate gate."""

from dataclasses import fields
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from seokpan.game.application.captured_startup import CapturedGameStartup
from seokpan.game.application.persistence import PersistenceOutcome, PersistenceRuleViolation
from seokpan.game.application.service import GameApplicationService, _stable_uuid4
from seokpan.identity.application import SessionActorType
from seokpan.room.application.start_capture import CaptureRoomGameStart
from seokpan.room.application.start_intent import RoomGameStartIntent, StartIntentPlayer
from seokpan.room.domain import RoomRuleViolation, RoomStatus, Team

ROOM = "00000000-0000-4000-8000-000000000001"
BLACK = "00000000-0000-4000-8000-000000000003"
WHITE = "00000000-0000-4000-8000-000000000004"
GAME = _stable_uuid4(f"{ROOM}\nstart\nstart-1")


@pytest.fixture
def flow() -> SimpleNamespace:
    intent = RoomGameStartIntent(
        room_id=ROOM,
        game_id=GAME,
        owner_id=BLACK,
        original_request_id="start-1",
        accepted_state_version=7,
        started_at_ms=1000,
        vote_seconds=15,
        players=(
            StartIntentPlayer(BLACK, "BLACK", member_id="1"),
            StartIntentPlayer(WHITE, "WHITE", member_id="2"),
        ),
    )
    waiting = SimpleNamespace(
        room_id=ROOM,
        status=RoomStatus.WAITING,
        game_id=None,
        owner_id=BLACK,
        state_version=6,
        participants=(
            SimpleNamespace(participant_id=BLACK, ready=True, team=Team.BLACK),
            SimpleNamespace(participant_id=WHITE, ready=True, team=Team.WHITE),
        ),
    )
    playing = SimpleNamespace(
        **{
            **vars(waiting),
            "status": RoomStatus.PLAYING,
            "game_id": GAME,
            "state_version": 7,
        }
    )
    runtime = Mock()
    runtime.get = AsyncMock(side_effect=[waiting, playing])
    runtime.start_game = AsyncMock()
    runtime.get_start_intent = AsyncMock(return_value=intent)
    rooms = Mock()
    rooms.resolve_participation = AsyncMock(
        return_value=SimpleNamespace(
            room_id=ROOM,
            participant_id=BLACK,
        )
    )
    rooms.resolve_participant_identity = AsyncMock(
        side_effect=lambda pid: SimpleNamespace(
            room_id=ROOM,
            actor_type=SessionActorType.MEMBER,
            actor_id="1" if pid == BLACK else "2",
        )
    )
    command = CapturedGameStartup.persistence_command(intent)
    history = SimpleNamespace(start=command, participants=(), moves=())
    games = Mock()
    games.start_game = AsyncMock(return_value=PersistenceOutcome.CREATED)
    games.load_game = AsyncMock(side_effect=[None, history])
    games.load_result = AsyncMock(return_value=None)
    votes = Mock(get=AsyncMock(return_value=None))
    snapshot = SimpleNamespace(game_id=GAME, turn_no=1, deadline_ms=20000)
    initializer = Mock(
        initialize=AsyncMock(
            return_value=SimpleNamespace(
                snapshot=snapshot,
                replayed=False,
            )
        )
    )
    initializer.get_phase = AsyncMock(return_value="PENDING")
    clock = SimpleNamespace(now_ms=5000)
    startup = CapturedGameStartup(
        rooms=rooms,
        runtime=runtime,
        games=games,
        votes=votes,
        initializer=initializer,
        clock=clock,
    )
    return SimpleNamespace(**locals())


async def start(flow: SimpleNamespace, request: str = "start-1", version: int = 6):
    return await flow.startup.start_game(
        session=SimpleNamespace(session_digest="a" * 64),
        room_id=ROOM,
        request_id=request,
        expected_state_version=version,
    )


@pytest.mark.asyncio
async def test_initial_start_captures_trusted_identity_then_uses_original_time(flow):
    outcome = await start(flow)
    capture = flow.runtime.start_game.await_args.args[0]
    assert isinstance(capture, CaptureRoomGameStart)
    assert capture.players == flow.intent.players
    persisted = flow.games.start_game.await_args.args[0]
    assert persisted.started_at == flow.intent.started_at
    assert persisted.started_at.timestamp() == 1
    assert outcome.initialized_now and not outcome.snapshot.replayed
    command = flow.initializer.initialize.await_args.args[0]
    assert command.intent == flow.intent
    assert command.expected_room_version == 7
    assert "deadline_ms" not in {field.name for field in fields(command)}


@pytest.mark.asyncio
@pytest.mark.parametrize("request_id,version", [("start-1", 6), ("recover-2", 7)])
async def test_recovery_uses_frozen_roster_without_live_identity_lookup(flow, request_id, version):
    flow.runtime.get.side_effect = [flow.playing, flow.playing]
    outcome = await start(flow, request_id, version)
    assert outcome.snapshot.replayed and outcome.initialized_now
    flow.rooms.resolve_participant_identity.assert_not_awaited()
    flow.runtime.start_game.assert_not_awaited()
    assert flow.games.start_game.await_args.args[0].participants == flow.command.participants


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", [None, "INITIALIZED", "corrupt", "CLOSED"])
async def test_missing_history_is_not_recreated_without_pending_proof(flow, phase):
    flow.runtime.get.side_effect = [flow.playing, flow.playing]
    flow.initializer.get_phase.return_value = phase
    with pytest.raises(RoomRuleViolation, match="GAME_START_RECOVERY_REQUIRED"):
        await start(flow)
    flow.games.start_game.assert_not_awaited()
    flow.initializer.initialize.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_history_with_same_runtime_is_not_reconstructed(flow):
    flow.votes.get.return_value = flow.snapshot
    with pytest.raises(RoomRuleViolation, match="GAME_START_RECOVERY_REQUIRED"):
        await start(flow)
    flow.games.start_game.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_intent_does_not_fall_back_to_live_ready(flow):
    flow.runtime.get.side_effect = [flow.playing]
    flow.runtime.get_start_intent.return_value = None
    with pytest.raises(RoomRuleViolation, match="GAME_START_RECOVERY_REQUIRED"):
        await start(flow)
    flow.rooms.resolve_participant_identity.assert_not_awaited()
    flow.games.start_game.assert_not_awaited()


@pytest.mark.asyncio
async def test_wrong_room_identity_rejected_before_capture(flow):
    flow.rooms.resolve_participant_identity.return_value = None
    flow.rooms.resolve_participant_identity.side_effect = None
    with pytest.raises(RoomRuleViolation, match="PARTICIPANT_IDENTITY_NOT_FOUND"):
        await start(flow)
    flow.runtime.start_game.assert_not_awaited()


@pytest.mark.asyncio
async def test_source_closure_during_capture_never_initializes(flow):
    flow.runtime.get.side_effect = [flow.waiting, None]
    with pytest.raises(RoomRuleViolation, match="GAME_NOT_IN_CURRENT_ROOM"):
        await start(flow)
    flow.games.start_game.assert_not_awaited()
    flow.initializer.initialize.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_persistent_config_is_not_accepted_as_race_winner(flow):
    flow.games.start_game.side_effect = PersistenceRuleViolation("GAME_START_CONFLICT")
    flow.history.start = SimpleNamespace(
        game_id=flow.command.game_id,
        room_id=flow.command.room_id,
        started_at=flow.command.started_at,
        participants=flow.command.participants,
        voting_time_seconds=30,
    )
    with pytest.raises(PersistenceRuleViolation, match="GAME_START_CONFLICT"):
        await start(flow)
    flow.initializer.initialize.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("progress", ["move", "result"])
async def test_result_and_move_history_are_not_reset(flow, progress):
    flow.games.load_game.side_effect = None
    flow.games.load_game.return_value = flow.history
    if progress == "move":
        flow.history.moves = (object(),)
    else:
        flow.games.load_result.return_value = object()
    with pytest.raises(RoomRuleViolation, match="GAME_START_RECOVERY_REQUIRED"):
        await start(flow)
    flow.initializer.initialize.assert_not_awaited()


@pytest.mark.asyncio
async def test_event_only_on_new_runtime_and_default_injection_stays_opt_in(flow):
    service = GameApplicationService(
        rooms=flow.rooms,
        games=flow.games,
        votes=flow.votes,
        clock=flow.clock,
        captured_startup=flow.startup,
    )
    service._game_started = AsyncMock()
    first = await service.start_game(
        session=SimpleNamespace(session_digest="a" * 64),
        room_id=ROOM,
        request_id="start-1",
        expected_state_version=6,
    )
    assert first.game is flow.snapshot
    service._game_started.assert_awaited_once()
    flow.runtime.get.side_effect = [flow.playing, flow.playing]
    flow.games.load_game.side_effect = None
    flow.games.load_game.return_value = flow.history
    flow.initializer.initialize.return_value.replayed = True
    service._game_started.reset_mock()
    await service.start_game(
        session=SimpleNamespace(session_digest="a" * 64),
        room_id=ROOM,
        request_id="start-1",
        expected_state_version=6,
    )
    service._game_started.assert_not_awaited()
    default = GameApplicationService(
        rooms=flow.rooms,
        games=flow.games,
        votes=flow.votes,
        clock=flow.clock,
    )
    assert default._captured_startup is None
