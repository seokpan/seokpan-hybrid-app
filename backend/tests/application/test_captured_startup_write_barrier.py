"""A missing history is not permission to write before checking durable results."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from seokpan.game.application.captured_startup import CapturedGameStartup
from seokpan.game.application.persistence import PersistenceOutcome, PersistenceRuleViolation
from seokpan.room.application.start_intent import RoomGameStartIntent, StartIntentPlayer
from seokpan.room.domain import RoomRuleViolation, RoomStatus

ROOM = "00000000-0000-4000-8000-000000000001"
GAME = "00000000-0000-4000-8000-000000000002"
OWNER = "00000000-0000-4000-8000-000000000003"
WHITE = "00000000-0000-4000-8000-000000000004"


@pytest.fixture
def pending_start() -> SimpleNamespace:
    intent = RoomGameStartIntent(
        room_id=ROOM,
        game_id=GAME,
        owner_id=OWNER,
        original_request_id="original",
        accepted_state_version=7,
        started_at_ms=1000,
        vote_seconds=15,
        players=(
            StartIntentPlayer(OWNER, "BLACK", member_id="1"),
            StartIntentPlayer(WHITE, "WHITE", member_id="2"),
        ),
    )
    playing = SimpleNamespace(
        room_id=ROOM,
        game_id=GAME,
        owner_id=OWNER,
        status=RoomStatus.PLAYING,
        state_version=7,
    )
    rooms = SimpleNamespace(
        resolve_participation=AsyncMock(
            return_value=SimpleNamespace(
                room_id=ROOM,
                participant_id=OWNER,
            )
        )
    )
    runtime = SimpleNamespace(
        get=AsyncMock(return_value=playing),
        get_start_intent=AsyncMock(return_value=intent),
    )
    history = SimpleNamespace(start=CapturedGameStartup.persistence_command(intent), moves=())
    games = SimpleNamespace(
        load_game=AsyncMock(side_effect=[None, history]),
        load_result=AsyncMock(return_value=None),
        start_game=AsyncMock(return_value=PersistenceOutcome.CREATED),
    )
    votes = SimpleNamespace(get=AsyncMock(return_value=None))
    initializer = SimpleNamespace(
        get_phase=AsyncMock(return_value="PENDING"),
        initialize=AsyncMock(
            return_value=SimpleNamespace(
                snapshot=object(),
                replayed=False,
            )
        ),
    )
    startup = CapturedGameStartup(
        rooms=rooms,
        runtime=runtime,
        games=games,
        votes=votes,
        initializer=initializer,
        clock=SimpleNamespace(now_ms=5000),
    )
    return SimpleNamespace(
        startup=startup,
        games=games,
        initializer=initializer,
        intent=intent,
    )


async def recover(value: SimpleNamespace, request: str = "retry", version: int = 7):
    return await value.startup.start_game(
        session=SimpleNamespace(session_digest="a" * 64),
        room_id=ROOM,
        request_id=request,
        expected_state_version=version,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("request_id,version", [("original", 6), ("retry", 7)])
async def test_result_without_history_is_rejected_before_any_start_write(
    pending_start,
    request_id,
    version,
):
    pending_start.games.load_result.return_value = object()
    with pytest.raises(PersistenceRuleViolation, match="GAME_RESULT_HISTORY_MISMATCH"):
        await recover(pending_start, request_id, version)
    pending_start.games.start_game.assert_not_awaited()
    pending_start.initializer.initialize.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_result_read_failure_or_cancellation_does_not_write_history(pending_start, failure):
    pending_start.games.load_result.side_effect = failure("result read interrupted")
    with pytest.raises(failure):
        await recover(pending_start)
    pending_start.games.start_game.assert_not_awaited()
    pending_start.initializer.initialize.assert_not_awaited()


@pytest.mark.asyncio
async def test_later_result_still_prevents_initialization_after_initial_absence(pending_start):
    pending_start.games.load_result.side_effect = [None, object()]
    with pytest.raises(RoomRuleViolation, match="GAME_START_RECOVERY_REQUIRED"):
        await recover(pending_start)
    pending_start.games.start_game.assert_awaited_once()
    assert pending_start.games.load_result.await_count == 2
    pending_start.initializer.initialize.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_history_without_result_preserves_original_start_command(pending_start):
    outcome = await recover(pending_start)
    pending_start.games.start_game.assert_awaited_once()
    command = pending_start.games.start_game.await_args.args[0]
    assert command.game_id == GAME
    assert command.started_at == pending_start.intent.started_at
    assert outcome.initialized_now
    pending_start.initializer.initialize.assert_awaited_once()
