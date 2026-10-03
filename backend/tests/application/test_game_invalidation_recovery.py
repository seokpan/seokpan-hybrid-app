"""Failure-boundary tests for F15 orchestration, not real Provider evidence."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from seokpan.game.application import resolution
from seokpan.game.application.history import replay_game_history
from seokpan.game.application.persistence import FinalizeGameCommand, PersistenceRuleViolation
from seokpan.game.domain import EndReason, GameResult, GameStatus, Stone
from seokpan.vote.domain import VoteRuleViolation

ROOM_ID = "00000000-0000-4000-8000-000000000001"
GAME_ID = "00000000-0000-4000-8000-000000000002"
CLOSED_AT_MS = 4_321


def test_invalidation_imports_the_shared_history_replayer() -> None:
    assert resolution.replay_game_history is replay_game_history


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(stored=None, pending=True)
    events: list[str] = []
    faults: dict[str, BaseException] = {}

    def checkpoint(stage: str) -> None:
        error = faults.pop(stage, None)
        if error is not None:
            raise error

    history = SimpleNamespace(start=SimpleNamespace(room_id=ROOM_ID), participants=(), moves=())
    replay = Mock(return_value=SimpleNamespace(status=GameStatus.ACTIVE))
    monkeypatch.setattr(resolution, "replay_game_history", replay)
    result = GameResult(
        game_id=GAME_ID,
        status=GameStatus.SYSTEM_INVALID,
        end_reason=EndReason.SYSTEM_INVALID,
        winner=Stone.EMPTY,
        winning_line=(),
        stats_eligible=False,
        rating_adjustments=(),
    )
    result_service = Mock()
    result_service.return_value.finalize_system_invalid.return_value = result
    monkeypatch.setattr(resolution, "GameResultService", result_service)

    async def persist(command: FinalizeGameCommand) -> None:
        events.append("persist")
        checkpoint("persist")
        state.stored = SimpleNamespace(end_reason=command.result.end_reason)
        checkpoint("after_commit")

    async def discard(room_id: str, game_id: str) -> None:
        assert (room_id, game_id) == (ROOM_ID, GAME_ID)
        events.append("discard")
        checkpoint("discard")

    async def acknowledge(room_id: str, game_id: str) -> None:
        assert (room_id, game_id) == (ROOM_ID, GAME_ID)
        events.append("ack")
        checkpoint("ack")
        state.pending = False

    games = Mock()
    games.load_game = AsyncMock(return_value=history)
    games.load_result = AsyncMock(side_effect=lambda _game_id: state.stored)
    games.result_matches = AsyncMock(return_value=False)
    games.finalize_game = AsyncMock(side_effect=persist)
    votes = Mock()
    votes.get = AsyncMock(return_value=None)
    votes.discard_game = AsyncMock(side_effect=discard)
    rooms = Mock()
    rooms.complete_game_invalidation = AsyncMock(side_effect=acknowledge)
    runner = resolution.TurnResolutionRunner(
        due_turns=Mock(),
        finalization_gate=Mock(),
        tie_selector=Mock(),
        tie_audit=Mock(),
        votes=votes,
        games=games,
        rooms=rooms,
        clock=SimpleNamespace(now_ms=99_000),
        runner_id="invalidation-qa",
        events=Mock(),
    )
    return SimpleNamespace(
        runner=runner,
        games=games,
        votes=votes,
        rooms=rooms,
        state=state,
        events=events,
        faults=faults,
        history=history,
        replay=replay,
        result_service=result_service,
    )


async def finalize(harness: SimpleNamespace) -> bool:
    return await harness.runner.finalize_system_invalid(
        room_id=ROOM_ID, game_id=GAME_ID, closed_at_ms=CLOSED_AT_MS
    )


@pytest.mark.asyncio
async def test_fresh_invalidation_persists_before_cleanup_and_ack(harness: SimpleNamespace) -> None:
    assert await finalize(harness) is True
    assert harness.events == ["persist", "discard", "ack"]
    command = harness.games.finalize_game.await_args.args[0]
    assert command.ended_at == datetime.fromtimestamp(CLOSED_AT_MS / 1000, UTC)
    assert command.result.status is GameStatus.SYSTEM_INVALID
    assert command.result.stats_eligible is False
    assert command.result.rating_adjustments == ()
    assert harness.state.pending is False


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["persist", "discard", "ack"])
async def test_failure_retains_marker_and_retry_does_not_repeat_durable_result(
    harness: SimpleNamespace, stage: str
) -> None:
    error = PersistenceRuleViolation("PERSISTENCE_COMMIT_UNCERTAIN")
    harness.faults[stage] = error
    with pytest.raises(PersistenceRuleViolation) as caught:
        await finalize(harness)
    assert caught.value is error
    assert harness.state.pending is True
    steps = ["persist", "discard", "ack"]
    assert harness.events == steps[: steps.index(stage) + 1]

    assert await finalize(harness) is True
    assert harness.state.pending is False
    assert harness.games.finalize_game.await_count == (2 if stage == "persist" else 1)
    assert harness.events[-2:] == ["discard", "ack"]


@pytest.mark.asyncio
async def test_lost_commit_response_reuses_durable_result_on_retry(
    harness: SimpleNamespace,
) -> None:
    harness.faults["after_commit"] = PersistenceRuleViolation("PERSISTENCE_COMMIT_UNCERTAIN")
    with pytest.raises(PersistenceRuleViolation):
        await finalize(harness)
    assert harness.state.stored is not None
    assert harness.state.pending is True
    harness.votes.discard_game.assert_not_awaited()
    harness.rooms.complete_game_invalidation.assert_not_awaited()

    assert await finalize(harness) is True
    harness.games.finalize_game.assert_awaited_once()
    assert harness.state.pending is False


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["persist", "discard", "ack"])
async def test_cancellation_propagates_and_keeps_work_retryable(
    harness: SimpleNamespace, stage: str
) -> None:
    harness.faults[stage] = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await finalize(harness)
    assert harness.state.pending is True

    assert await finalize(harness) is True
    assert harness.state.pending is False
    assert harness.games.finalize_game.await_count == (2 if stage == "persist" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    [
        EndReason.BLACK_WIN,
        EndReason.WHITE_WIN,
        EndReason.DRAW,
        EndReason.FORFEIT,
        EndReason.JOINT_LOSS,
    ],
)
async def test_existing_normal_result_is_not_recomputed_or_overwritten(
    harness: SimpleNamespace, reason: EndReason
) -> None:
    stored = SimpleNamespace(end_reason=reason)
    harness.state.stored = stored
    assert await finalize(harness) is False
    assert harness.state.stored is stored
    harness.games.finalize_game.assert_not_awaited()
    harness.replay.assert_not_called()
    harness.result_service.assert_not_called()
    assert harness.events == ["discard", "ack"]


@pytest.mark.asyncio
async def test_missing_history_never_acknowledges_or_discards(harness: SimpleNamespace) -> None:
    harness.games.load_game.return_value = None
    with pytest.raises(PersistenceRuleViolation, match="GAME_NOT_FOUND"):
        await finalize(harness)
    assert harness.events == []
    assert harness.state.pending is True


@pytest.mark.asyncio
async def test_wrong_room_history_cannot_be_finalized(harness: SimpleNamespace) -> None:
    harness.history.start.room_id = "another-room"
    with pytest.raises(PersistenceRuleViolation, match="GAME_START_CONFLICT"):
        await finalize(harness)
    assert harness.events == []
    assert harness.state.pending is True


@pytest.mark.asyncio
async def test_newer_runtime_is_not_discarded_by_old_invalidation(harness: SimpleNamespace) -> None:
    harness.votes.get.return_value = SimpleNamespace(game_id="another-game")
    with pytest.raises(VoteRuleViolation, match="STALE_GAME"):
        await finalize(harness)
    assert harness.events == []
    assert harness.state.pending is True


@pytest.mark.asyncio
async def test_invalid_history_does_not_clear_recovery_evidence(harness: SimpleNamespace) -> None:
    harness.replay.side_effect = PersistenceRuleViolation("GAME_HISTORY_INVALID")
    with pytest.raises(PersistenceRuleViolation, match="GAME_HISTORY_INVALID"):
        await finalize(harness)
    assert harness.events == []
    assert harness.state.pending is True
