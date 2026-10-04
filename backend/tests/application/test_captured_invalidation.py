"""Captured F09-to-F15 orchestration and immutable evidence boundary tests."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from seokpan.game.application.captured_invalidation import CapturedGameInvalidation
from seokpan.game.application.persistence import PersistenceRuleViolation
from seokpan.room.application.start_closure import (
    decode_closed_start,
    terminal_phase,
)
from seokpan.room.application.start_intent import RoomGameStartIntent, StartIntentPlayer
from seokpan.room.domain import RoomRuleViolation

R = "00000000-0000-4000-8000-000000000001"
G = "00000000-0000-4000-8000-000000000002"
B = "00000000-0000-4000-8000-000000000003"
W = "00000000-0000-4000-8000-000000000004"
CLOSED = 9000


@pytest.fixture
def intent() -> RoomGameStartIntent:
    return RoomGameStartIntent(
        room_id=R,
        game_id=G,
        original_request_id="start",
        owner_id=B,
        accepted_state_version=7,
        started_at_ms=1000,
        vote_seconds=15,
        players=(
            StartIntentPlayer(B, "BLACK", member_id="18446744073709551615"),
            StartIntentPlayer(W, "WHITE", guest_label="Guest-0001"),
        ),
    )


def marker(**changes: object) -> str:
    return json.dumps(
        {
            "room_id": R,
            "terminated_game_id": G,
            "closed_at_ms": CLOSED,
            "invalidation_pending": True,
        }
        | changes
    )


def decode(intent: RoomGameStartIntent, phase: object = "PENDING", **changes: object):
    return decode_closed_start(
        room_id=R,
        game_id=G,
        closed_at_ms=CLOSED,
        marker_wire=marker(**changes),
        intent_wire=intent.to_json(),
        phase_wire=phase,
    )


def initialized(intent: RoomGameStartIntent) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "phase": "INITIALIZED",
            "game_id": G,
            "initialized_at_ms": 2000,
            "first_deadline_ms": 17000,
            "intent_fingerprint": intent.fingerprint,
        }
    )


def test_pending_is_proof_of_uninitialized_closure(intent):
    value = decode(intent)
    assert value.intent == intent
    assert not value.initialized and not value.acknowledged


def test_initialized_closure_remains_distinct_from_never_initialized(intent):
    assert decode(intent, initialized(intent)).initialized


def test_terminal_cut_before_ack_and_duplicate_after_ack_are_decodable(intent):
    terminal = terminal_phase(decode(intent))
    assert not decode(intent, terminal).acknowledged
    assert decode(intent, terminal, invalidation_pending=False).acknowledged


@pytest.mark.parametrize(
    "change",
    [
        {"room_id": "other"},
        {"terminated_game_id": "other"},
        {"closed_at_ms": True},
        {"closed_at_ms": 9001},
        {"invalidation_pending": 1},
        {"invalidation_pending": None},
    ],
)
def test_closure_identity_and_type_must_match(intent, change):
    with pytest.raises(RoomRuleViolation):
        decode(intent, **change)


@pytest.mark.parametrize(
    "phase",
    [None, "", "CLOSED", "{}", "null", "[]", '{"phase":"PENDING","phase":"INITIALIZED"}'],
)
def test_unknown_missing_or_duplicate_phase_is_not_pending(intent, phase):
    with pytest.raises(RoomRuleViolation):
        decode(intent, phase)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("game_id", "other"),
        ("intent_fingerprint", "other"),
        ("initialized_at_ms", 999),
        ("initialized_at_ms", 10000),
        ("initialized_at_ms", True),
        ("first_deadline_ms", 17001),
    ],
)
def test_invalid_initialization_witness_rejected(intent, field, value):
    data = json.loads(initialized(intent))
    data[field] = value
    with pytest.raises(RoomRuleViolation):
        decode(intent, json.dumps(data))


def test_acknowledged_marker_cannot_claim_pending(intent):
    with pytest.raises(RoomRuleViolation):
        decode(intent, invalidation_pending=False)


def test_legacy_absence_is_allowed_only_with_real_matching_closure():
    assert (
        decode_closed_start(
            room_id=R,
            game_id=G,
            closed_at_ms=CLOSED,
            marker_wire=marker(),
            intent_wire=None,
            phase_wire=None,
        )
        is None
    )
    with pytest.raises(RoomRuleViolation):
        decode_closed_start(
            room_id=R,
            game_id=G,
            closed_at_ms=CLOSED,
            marker_wire=None,
            intent_wire=None,
            phase_wire=None,
        )


def test_half_missing_record_and_closure_before_start_are_rejected(intent):
    with pytest.raises(RoomRuleViolation):
        decode_closed_start(
            room_id=R,
            game_id=G,
            closed_at_ms=CLOSED,
            marker_wire=marker(),
            intent_wire=None,
            phase_wire="PENDING",
        )
    with pytest.raises(RoomRuleViolation):
        decode(replace(intent, started_at_ms=CLOSED + 1))


@pytest.fixture
def harness(intent):
    value = decode(intent)
    state = SimpleNamespace(history=None, result=None)
    trace = []
    closures = Mock(read_closed_start=AsyncMock(return_value=value), acknowledge=AsyncMock())
    games = Mock()
    games.load_game = AsyncMock(side_effect=lambda _: state.history)
    games.load_result = AsyncMock(side_effect=lambda _: state.result)

    async def start(command):
        trace.append(command)
        state.history = SimpleNamespace(start=command, moves=(), participants=())

    games.start_game = AsyncMock(side_effect=start)
    service = CapturedGameInvalidation(closures=closures, games=games)
    return SimpleNamespace(
        value=value,
        state=state,
        games=games,
        closures=closures,
        service=service,
        trace=trace,
    )


async def prepare(h):
    await h.service.prepare_history(room_id=R, game_id=G, closed_at_ms=CLOSED)


async def ack(h):
    return await h.service.acknowledge(room_id=R, game_id=G, closed_at_ms=CLOSED)


@pytest.mark.asyncio
async def test_closed_pending_start_restores_original_identity_time_and_config(harness):
    h = harness
    await prepare(h)
    command = h.trace[0]
    assert command.started_at == h.value.intent.started_at
    assert command.voting_time_seconds == 15
    assert command.participants[0].member_id == 18446744073709551615
    assert command.participants[1].guest_label == "Guest-0001"
    await prepare(h)
    h.games.start_game.assert_awaited_once()
    h.closures.acknowledge.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["initialized", "terminal", "acknowledged"])
async def test_missing_history_not_recreated_after_initialization_or_finalization(harness, state):
    h = harness
    if state == "initialized":
        value = decode(h.value.intent, initialized(h.value.intent))
    else:
        value = decode(
            h.value.intent,
            terminal_phase(h.value),
            invalidation_pending=state != "acknowledged",
        )
    h.closures.read_closed_start.return_value = value
    with pytest.raises(PersistenceRuleViolation):
        await prepare(h)
    h.games.start_game.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_result_without_history_never_causes_fabrication(harness):
    h = harness
    h.state.result = SimpleNamespace(game_id=G, room_id=R)
    with pytest.raises(PersistenceRuleViolation):
        await prepare(h)
    h.games.start_game.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("cut", ["load_game", "load_result", "start_game"])
async def test_provider_error_or_cancellation_never_becomes_success(harness, cut):
    h = harness
    getattr(h.games, cut).side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await prepare(h)
    h.closures.acknowledge.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_exact_start_winner_is_accepted(harness):
    h = harness

    async def winner(command):
        h.state.history = SimpleNamespace(start=command, moves=())
        raise PersistenceRuleViolation("GAME_START_CONFLICT")

    h.games.start_game.side_effect = winner
    await prepare(h)
    assert h.state.history.start.game_id == G


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("room_id", "other"),
        ("game_id", "other"),
        ("voting_time_seconds", 30),
        ("started_at", None),
        ("participants", ()),
    ],
)
async def test_existing_history_must_match_immutable_intent(harness, field, value):
    h = harness
    await prepare(h)
    command = h.state.history.start
    fields = {
        name: getattr(command, name)
        for name in ("room_id", "game_id", "voting_time_seconds", "started_at", "participants")
    }
    fields[field] = value
    h.state.history.start = SimpleNamespace(**fields)
    with pytest.raises(PersistenceRuleViolation):
        await prepare(h)


@pytest.mark.asyncio
async def test_ack_requires_a_durable_result_and_preserves_it(harness):
    h = harness
    await prepare(h)
    with pytest.raises(PersistenceRuleViolation):
        await ack(h)
    h.closures.acknowledge.assert_not_awaited()
    stored = SimpleNamespace(game_id=G, room_id=R, end_reason="BLACK_WIN")
    h.state.result = stored
    assert await ack(h)
    assert h.state.result is stored
    h.closures.acknowledge.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_path_returns_to_existing_runner_without_creating_history(harness):
    h = harness
    h.closures.read_closed_start.return_value = None
    await prepare(h)
    assert not await ack(h)
    h.games.start_game.assert_not_awaited()


@pytest.fixture
def runner_harness(harness, monkeypatch):
    from seokpan.game.application import resolution
    from seokpan.game.domain import EndReason, GameResult, GameStatus, Stone

    h = harness
    events = []
    faults = {}

    def cut(stage):
        error = faults.pop(stage, None)
        if error is not None:
            raise error

    original_start = h.games.start_game.side_effect

    async def start(command):
        events.append("history")
        await original_start(command)
        cut("history")

    h.games.start_game.side_effect = start
    result = GameResult(
        game_id=G,
        status=GameStatus.SYSTEM_INVALID,
        end_reason=EndReason.SYSTEM_INVALID,
        winner=Stone.EMPTY,
        winning_line=(),
        stats_eligible=False,
        rating_adjustments=(),
    )
    domain = Mock()
    domain.return_value.finalize_system_invalid.return_value = result
    monkeypatch.setattr(resolution, "GameResultService", domain)
    monkeypatch.setattr(
        resolution,
        "replay_game_history",
        Mock(return_value=SimpleNamespace(status=GameStatus.ACTIVE)),
    )

    async def persist(command):
        events.append("result")
        h.state.result = SimpleNamespace(
            game_id=G,
            room_id=R,
            end_reason=command.result.end_reason,
        )
        cut("result")

    async def discard(*_):
        events.append("discard")
        cut("discard")

    async def acknowledge(_):
        events.append("ack")
        cut("ack")

    h.games.result_matches = AsyncMock(return_value=False)
    h.games.finalize_game = AsyncMock(side_effect=persist)
    h.closures.acknowledge.side_effect = acknowledge
    rooms = Mock(complete_game_invalidation=AsyncMock())
    runner = resolution.TurnResolutionRunner(
        due_turns=Mock(),
        finalization_gate=Mock(),
        tie_selector=Mock(),
        tie_audit=Mock(),
        votes=Mock(
            get=AsyncMock(return_value=None),
            discard_game=AsyncMock(side_effect=discard),
        ),
        games=h.games,
        rooms=rooms,
        clock=SimpleNamespace(now_ms=99000),
        runner_id="closure-qa",
        events=Mock(),
        captured_invalidation=h.service,
    )
    return SimpleNamespace(h=h, runner=runner, events=events, faults=faults, rooms=rooms)


@pytest.mark.asyncio
async def test_runner_connects_missing_history_to_result_discard_and_captured_ack(runner_harness):
    q = runner_harness
    assert await q.runner.finalize_system_invalid(room_id=R, game_id=G, closed_at_ms=CLOSED)
    assert q.events == ["history", "result", "discard", "ack"]
    assert q.h.games.finalize_game.await_args.args[0].result.stats_eligible is False
    q.rooms.complete_game_invalidation.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["history", "result", "discard", "ack"])
async def test_combined_retry_after_write_response_loss_or_cancel(runner_harness, stage):
    q = runner_harness
    q.faults[stage] = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await q.runner.finalize_system_invalid(room_id=R, game_id=G, closed_at_ms=CLOSED)
    if stage != "ack":
        q.h.closures.acknowledge.assert_not_awaited()
    assert await q.runner.finalize_system_invalid(room_id=R, game_id=G, closed_at_ms=CLOSED)
    q.h.games.start_game.assert_awaited_once()
    q.h.games.finalize_game.assert_awaited_once()
    assert q.events[-2:] == ["discard", "ack"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None, "active", "wrong_turn", "wrong_game", "no_result"])
async def test_only_durably_finished_captured_predecessor_is_authorized(harness, fault):
    from seokpan.game.domain import GameStatus

    h = harness
    previous = "00000000-0000-4000-8000-000000000009"
    old = replace(h.value.intent, previous_game_id=previous, previous_turn_no=8)
    h.closures.read_closed_start.return_value = decode(old)
    runtime = SimpleNamespace(
        room_id=R,
        game_id=previous,
        turn_no=8,
        game_status=GameStatus.FINISHED,
    )
    h.state.result = SimpleNamespace(game_id=previous, room_id=R)
    if fault == "active":
        runtime.game_status = GameStatus.ACTIVE
    elif fault == "wrong_turn":
        runtime.turn_no = 9
    elif fault == "wrong_game":
        runtime.game_id = G
    elif fault == "no_result":
        h.state.result = None
    allowed = await h.service.permits_previous_runtime_cleanup(
        room_id=R,
        game_id=G,
        closed_at_ms=CLOSED,
        runtime=runtime,
    )
    assert allowed is (fault is None)
