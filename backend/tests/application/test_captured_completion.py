"""Normal completion, replay and retention contract; Provider tests are separate."""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from seokpan.game.application.captured_completion import CapturedGameCompletion
from seokpan.game.application.captured_startup import CapturedGameStartup
from seokpan.game.application.persistence import PersistenceRuleViolation
from seokpan.game.application.resolution import DueTurn, TurnResolutionRunner
from seokpan.game.domain import EndReason, GameStatus
from seokpan.persistence.memory.start_completion import InMemoryCapturedCompletionStore
from seokpan.room.application.runtime import ROOM_REQUEST_DEDUPE_TTL_MS
from seokpan.room.application.start_completion import CompleteCapturedGame, initialized_phase
from seokpan.room.application.start_intent import RoomGameStartIntent, StartIntentPlayer
from seokpan.room.domain import RoomRuleViolation, RoomStatus

R, G, B, W = [f"00000000-0000-4000-8000-{i:012d}" for i in range(1, 5)]


@pytest.fixture
def context():
    intent = RoomGameStartIntent(
        room_id=R,
        game_id=G,
        owner_id=B,
        original_request_id="start-1",
        accepted_state_version=7,
        started_at_ms=1000,
        vote_seconds=15,
        players=(
            StartIntentPlayer(B, "BLACK", member_id="1"),
            StartIntentPlayer(W, "WHITE", guest_label="Guest-0001"),
        ),
    )
    phase = json.dumps(
        {
            "schema_version": 1,
            "phase": "INITIALIZED",
            "game_id": G,
            "intent_fingerprint": intent.fingerprint,
            "initialized_at_ms": 2000,
            "first_deadline_ms": 17000,
        }
    )
    view = SimpleNamespace(
        room_id=R,
        game_id=G,
        status=RoomStatus.PLAYING,
        state_version=7,
        last_game_id=None,
        last_game_turn_no=None,
    )
    ready = {B, W}

    class Domain:
        def complete_game(self, *, game_id):
            assert game_id == G
            assert view.status is RoomStatus.PLAYING
            view.status, view.game_id = RoomStatus.WAITING, None
            view.state_version += 1
            ready.clear()

    state = SimpleNamespace(room=Domain(), last_game_id=None, last_game_turn_no=None)

    def snapshot(room_id, stored):
        view.last_game_id, view.last_game_turn_no = stored.last_game_id, stored.last_game_turn_no
        return view

    def expected(stored, version):
        if view.state_version != version:
            raise RoomRuleViolation("STATE_VERSION_CONFLICT")

    rooms = SimpleNamespace(
        _purge_expired=lambda: None,
        _rooms={R: state},
        _start_intents={(R, G): intent},
        _start_phases={(R, G): phase},
        _start_record_expiries={},
        _captured_closure_receipts={},
        _normal_completion_pending={},
        _pending_game_invalidations={},
        _clock=SimpleNamespace(now_ms=8000),
        _snapshot=snapshot,
        _require_expected_version=expected,
        get=AsyncMock(side_effect=lambda _: snapshot(R, state)),
    )
    runtime = SimpleNamespace(
        game=SimpleNamespace(
            game_id=G,
            turn_no=2,
            game=SimpleNamespace(
                status=GameStatus.FINISHED,
                end_reason=EndReason.JOINT_LOSS,
            ),
        )
    )
    votes = SimpleNamespace(_states={R: runtime})
    store = InMemoryCapturedCompletionStore(rooms=rooms, votes=votes)
    games = SimpleNamespace(
        load_game=AsyncMock(
            return_value=SimpleNamespace(
                start=CapturedGameStartup.persistence_command(intent),
                moves=(),
            )
        ),
        load_result=AsyncMock(
            return_value=SimpleNamespace(
                game_id=G,
                room_id=R,
                ended_at=datetime.fromtimestamp(5, UTC),
                end_reason=EndReason.JOINT_LOSS,
            )
        ),
        game_is_finalized=AsyncMock(return_value=True),
    )
    service = CapturedGameCompletion(records=store, rooms=rooms, games=games)
    return SimpleNamespace(
        intent=intent,
        phase=phase,
        view=view,
        state=state,
        ready=ready,
        rooms=rooms,
        votes=votes,
        runtime=runtime,
        store=store,
        games=games,
        service=service,
    )


async def complete(c):
    return await c.service.complete(room_id=R, game_id=G, final_turn_no=2)


@pytest.mark.asyncio
async def test_normal_completion_releases_room_preserving_finished_runtime(context):
    c = context
    assert await complete(c) is True
    assert c.view.status is RoomStatus.WAITING and not c.ready
    assert c.state.last_game_id == G and c.state.last_game_turn_no == 2
    assert c.votes._states[R] is c.runtime
    phase = json.loads(c.rooms._start_phases[R, G])
    assert phase["phase"] == "INITIALIZED"
    assert phase["first_deadline_ms"] == 17000
    assert phase["normal_completion"]["retain_until_ms"] == 8000 + ROOM_REQUEST_DEDUPE_TTL_MS


@pytest.mark.asyncio
async def test_replay_preserves_new_ready_and_does_not_extend_retention(context):
    c = context
    await complete(c)
    c.ready.add(B)
    expiry = c.rooms._start_record_expiries[R, G]
    c.rooms._clock.now_ms += 1000
    assert await complete(c) is False
    assert c.ready == {B} and c.view.state_version == 8
    assert c.rooms._start_record_expiries[R, G] == expiry


@pytest.mark.asyncio
async def test_old_replay_never_modifies_successor_runtime_or_room(context):
    c = context
    await complete(c)
    c.view.status, c.view.game_id, c.view.state_version = RoomStatus.PLAYING, "next-game", 20
    next_runtime = object()
    c.votes._states[R] = next_runtime
    c.ready.add(W)
    assert await complete(c) is False
    assert c.view.game_id == "next-game" and c.view.state_version == 20
    assert c.ready == {W} and c.votes._states[R] is next_runtime


@pytest.mark.asyncio
async def test_already_waiting_without_receipt_is_finalized_without_ready_reset(context):
    c = context
    c.view.status, c.view.game_id, c.view.state_version = RoomStatus.WAITING, None, 8
    c.state.last_game_id, c.state.last_game_turn_no = G, 2
    assert await complete(c) is False
    assert c.ready == {B, W}
    assert (R, G) in c.rooms._start_record_expiries


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    ["history", "result", "not_final", "identity", "time", "result_room"],
)
async def test_bad_durable_evidence_has_no_room_or_retention_write(context, field):
    c = context
    if field == "history":
        c.games.load_game.return_value = None
    elif field == "result":
        c.games.load_result.return_value = None
    elif field == "not_final":
        c.games.game_is_finalized.return_value = False
    elif field == "identity":
        c.games.load_game.return_value.start = SimpleNamespace(
            game_id=G,
            room_id=R,
            voting_time_seconds=15,
            started_at=c.intent.started_at,
            participants=(),
        )
    elif field == "time":
        c.games.load_result.return_value.ended_at = datetime(2026, 1, 1)
    else:
        c.games.load_result.return_value.room_id = "wrong-room"
    with pytest.raises(PersistenceRuleViolation):
        await complete(c)
    assert c.view.status is RoomStatus.PLAYING
    assert c.rooms._start_phases[R, G] == c.phase and not c.rooms._start_record_expiries


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_provider_failure_or_cancel_does_not_complete(context, failure):
    c = context
    c.games.load_result.side_effect = failure("read failed")
    with pytest.raises(failure):
        await complete(c)
    assert c.view.status is RoomStatus.PLAYING and not c.rooms._start_record_expiries


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "active",
        "turn",
        "other_runtime",
        "closure",
        "no_room",
        "pending",
        "missing_phase",
        "missing_intent",
    ],
)
async def test_ambiguous_runtime_or_proof_fails_closed(context, change):
    c = context
    if change == "active":
        c.runtime.game.game.status = GameStatus.ACTIVE
    elif change == "turn":
        c.runtime.game.turn_no = 3
    elif change == "other_runtime":
        c.runtime.game.game_id = "other-game"
    elif change == "closure":
        c.rooms._pending_game_invalidations[R] = object()
    elif change == "no_room":
        c.rooms._rooms.clear()
    elif change == "pending":
        c.rooms._start_phases[R, G] = "PENDING"
    elif change == "missing_phase":
        c.rooms._start_phases.clear()
    else:
        c.rooms._start_intents.clear()
    with pytest.raises(RoomRuleViolation):
        await complete(c)
    assert c.view.status is RoomStatus.PLAYING and not c.rooms._start_record_expiries


@pytest.mark.asyncio
async def test_legacy_record_falls_back_without_reading_results(context):
    c = context
    c.rooms._start_intents.clear()
    c.rooms._start_phases.clear()
    assert await complete(c) is None
    c.games.load_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_prepared_receipt_recovers_before_room_release(context):
    c = context
    cmd = CompleteCapturedGame(c.intent, c.phase, 7, 2, "JOINT_LOSS", 5000)
    wire, expiry, _ = cmd.receipt(
        initialized_phase(c.intent, c.phase),
        now_ms=8000,
        retention_ms=ROOM_REQUEST_DEDUPE_TTL_MS,
    )
    c.rooms._start_phases[R, G] = wire
    assert await complete(c) is True
    assert c.rooms._start_record_expiries[R, G] == expiry


@pytest.mark.asyncio
async def test_version_race_does_not_write(context):
    c = context
    cmd = CompleteCapturedGame(c.intent, c.phase, 6, 2, "JOINT_LOSS", 5000)
    with pytest.raises(RoomRuleViolation, match="STATE_VERSION_CONFLICT"):
        await c.store.complete(cmd)
    assert c.rooms._start_phases[R, G] == c.phase and not c.rooms._start_record_expiries


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [True, False])
async def test_runner_uses_completion_before_legacy_early_return(changed):
    runner = object.__new__(TurnResolutionRunner)
    runner._captured_completion = SimpleNamespace(complete=AsyncMock(return_value=changed))
    runner._rooms = SimpleNamespace(get=AsyncMock(side_effect=AssertionError("legacy path")))
    runner._room_completed_events = AsyncMock()
    await runner._complete_room(DueTurn(R, G, 2))
    runner._captured_completion.complete.assert_awaited_once()
    assert runner._room_completed_events.await_count == int(changed)


@pytest.mark.parametrize("bad", [None, "PENDING", "{}", "[]", '{"x":1,"x":2}'])
def test_malformed_phase_is_rejected(context, bad):
    with pytest.raises(RoomRuleViolation):
        initialized_phase(context.intent, bad)


@pytest.mark.parametrize("reason", ["BLACK_WIN", "WHITE_WIN", "DRAW", "FORFEIT", "JOINT_LOSS"])
def test_normal_end_reasons_allowed(context, reason):
    CompleteCapturedGame(context.intent, context.phase, 7, 2, reason, 5000)


def test_system_invalid_is_not_normal_completion(context):
    with pytest.raises(RoomRuleViolation):
        CompleteCapturedGame(context.intent, context.phase, 7, 2, "SYSTEM_INVALID", 5000)


@pytest.mark.asyncio
async def test_redis_adapter_declares_seven_same_room_keys(context):
    from seokpan.persistence.redis.start_completion import RedisCapturedCompletionStore

    store = RedisCapturedCompletionStore(object())
    store._scripts = SimpleNamespace(
        execute=AsyncMock(
            return_value=json.dumps(
                {
                    "ok": True,
                    "error": None,
                    "changed": True,
                }
            )
        )
    )
    c = context
    assert await store.complete(CompleteCapturedGame(c.intent, c.phase, 7, 2, "JOINT_LOSS", 5000))
    call = store._scripts.execute.await_args
    assert call.args[0].name == "normal-start-complete"
    assert len(call.kwargs["keys"]) == 7
    assert all("{" + R + "}" in key for key in call.kwargs["keys"])
    assert call.kwargs["keys"][1].endswith(":ready")
    assert call.kwargs["args"][-2] == ROOM_REQUEST_DEDUPE_TTL_MS


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["both", "intent", "phase"])
async def test_redis_atomic_reader_does_not_hide_partial_proof(context, missing):
    from seokpan.persistence.redis.start_completion import RedisCapturedCompletionStore

    store = RedisCapturedCompletionStore(object())
    c = context
    store._scripts = SimpleNamespace(
        execute=AsyncMock(
            return_value=json.dumps(
                {
                    "ok": True,
                    "error": None,
                    "intent": None if missing in {"both", "intent"} else c.intent.to_json(),
                    "phase": None if missing in {"both", "phase"} else c.phase,
                }
            )
        )
    )
    if missing == "both":
        assert await store.read_start(R, G) is None
    else:
        with pytest.raises(RoomRuleViolation):
            await store.read_start(R, G)


@pytest.mark.parametrize("receipt", [None, {}, {"schema_version": True}])
def test_corrupt_existing_receipt_is_not_replaced(context, receipt):
    c = context
    phase = initialized_phase(c.intent, c.phase)
    phase["normal_completion"] = receipt
    cmd = CompleteCapturedGame(c.intent, c.phase, 7, 2, "JOINT_LOSS", 5000)
    with pytest.raises(RoomRuleViolation):
        cmd.receipt(phase, now_ms=8000, retention_ms=ROOM_REQUEST_DEDUPE_TTL_MS)


@pytest.mark.asyncio
async def test_runner_keeps_legacy_waiting_early_return_when_no_captured_proof():
    runner = object.__new__(TurnResolutionRunner)
    runner._captured_completion = SimpleNamespace(complete=AsyncMock(return_value=None))
    runner._rooms = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(
                status=RoomStatus.WAITING,
                game_id=None,
                last_game_id=G,
                last_game_turn_no=2,
            )
        )
    )
    runner._room_completed_events = AsyncMock()
    await runner._complete_room(DueTurn(R, G, 2))
    runner._captured_completion.complete.assert_awaited_once()
    runner._rooms.get.assert_awaited_once()
    runner._room_completed_events.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_live_room_status_is_not_terminal_authority(context):
    c = context
    await complete(c)
    c.view.status = "CLOSED"
    previous = c.rooms._start_record_expiries[R, G]
    with pytest.raises(RoomRuleViolation):
        await complete(c)
    assert c.rooms._start_record_expiries[R, G] == previous


@pytest.mark.asyncio
async def test_shared_task_survives_release_failure_and_new_wrapper(context):
    c = context

    class Cut(dict):
        def __setitem__(self, key, value):
            raise RuntimeError("expiry cut")

    c.rooms._start_record_expiries = Cut()
    with pytest.raises(RuntimeError, match="expiry cut"):
        await complete(c)
    assert c.view.status is RoomStatus.WAITING
    tasks = c.store._pending.copy()
    assert len(tasks) == 1
    c.rooms._start_record_expiries = {}
    new_store = InMemoryCapturedCompletionStore(rooms=c.rooms, votes=c.votes)
    new_service = CapturedGameCompletion(records=new_store, rooms=c.rooms, games=c.games)
    c.ready.add(B)
    c.rooms._clock.now_ms += 500
    assert await new_service.reconcile() == 1
    assert not new_store._pending and c.ready == {B}
    assert c.rooms._start_record_expiries[R, G] == 86408000
    assert c.votes._states[R] is c.runtime


@pytest.mark.asyncio
async def test_reconciliation_never_touches_successor_after_release(context):
    c = context
    original = c.rooms._purge_expired
    n = 0

    def cut():
        nonlocal n
        n += 1
        if n == 3:
            raise RuntimeError("post-release cut")

    c.rooms._purge_expired = cut
    with pytest.raises(RuntimeError):
        await complete(c)
    assert c.store._pending
    c.rooms._purge_expired = original
    c.view.game_id, c.view.status = "next-game", RoomStatus.PLAYING
    c.view.state_version = 99
    successor = object()
    c.votes._states[R] = successor
    c.ready.add(W)
    assert await c.service.reconcile() == 1
    assert c.view.game_id == "next-game" and c.view.state_version == 99
    assert c.votes._states[R] is successor and c.ready == {W}


@pytest.mark.asyncio
async def test_task_repairs_elapsed_partial_expiry_without_recreating_proof(context):
    c = context
    from seokpan.room.application.start_completion import pending_completion_wire

    await complete(c)
    phase = c.rooms._start_phases[R, G]
    c.store._pending[R, G] = pending_completion_wire(c.intent, phase, released=True)
    c.rooms._clock.now_ms = 86408001
    c.rooms._start_intents.clear()
    c.rooms._start_phases.clear()
    c.rooms._rooms.clear()
    c.rooms.get.return_value = None
    c.rooms.get.side_effect = None
    assert await c.service.reconcile() == 1
    assert not c.store._pending
    assert not c.rooms._start_intents and not c.rooms._start_phases


@pytest.mark.asyncio
async def test_corrupt_pending_item_does_not_prevent_healthy_completion(context):
    c = context
    from seokpan.room.application.start_completion import pending_completion_wire

    await complete(c)
    phase = c.rooms._start_phases[R, G]
    c.store._pending[R, "000-broken"] = "["
    c.store._pending[R, G] = pending_completion_wire(c.intent, phase, released=True)
    assert await c.service.reconcile() == 1
    assert list(c.store._pending) == [(R, "000-broken")]


@pytest.mark.asyncio
async def test_reconcile_cancellation_keeps_shared_task(context):
    c = context
    from seokpan.room.application.start_completion import pending_completion_wire

    await complete(c)
    c.store._pending[R, G] = pending_completion_wire(
        c.intent,
        c.rooms._start_phases[R, G],
        released=True,
    )
    c.games.load_result.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await c.service.reconcile()
    assert (R, G) in c.store._pending


@pytest.mark.asyncio
async def test_retention_not_applied_during_matching_f15_invalidation(context):
    c = context
    from seokpan.room.application.start_completion import pending_completion_wire

    await complete(c)
    c.store._pending[R, G] = pending_completion_wire(
        c.intent,
        c.rooms._start_phases[R, G],
        released=True,
    )
    c.rooms._pending_game_invalidations[R] = SimpleNamespace(game_id=G)
    assert await c.service.reconcile() == 0
    assert (R, G) in c.store._pending


@pytest.mark.asyncio
async def test_acked_f15_owns_retention_and_obsolete_normal_task_is_removed(context):
    c = context
    from seokpan.room.application.start_completion import pending_completion_wire

    await complete(c)
    c.store._pending[R, G] = pending_completion_wire(
        c.intent,
        c.rooms._start_phases[R, G],
        released=False,
    )
    c.rooms._rooms.clear()
    c.votes._states.clear()
    c.rooms.get.side_effect = None
    c.rooms.get.return_value = None
    c.rooms._captured_closure_receipts = {
        (R, G): json.dumps(
            {
                "room_id": R,
                "terminated_game_id": G,
                "closed_at_ms": 9000,
                "invalidation_pending": False,
            }
        )
    }
    terminal = json.dumps(
        {
            "schema_version": 1,
            "phase": "FINALIZED",
            "game_id": G,
            "intent_fingerprint": c.intent.fingerprint,
            "closed_at_ms": 9000,
        }
    )
    c.rooms._start_phases[R, G] = terminal
    expiry = c.rooms._start_record_expiries.copy()
    assert await c.service.reconcile() == 1
    assert not c.store._pending
    assert c.rooms._start_phases[R, G] == terminal
    assert c.rooms._start_record_expiries == expiry


@pytest.mark.asyncio
async def test_periodic_runner_reconciles_even_without_playing_rooms():
    runner = object.__new__(TurnResolutionRunner)
    order = []

    async def cleanup(**kwargs):
        order.append("cleanup")
        return 1

    async def due(**kwargs):
        order.append("due")
        return ()

    runner._captured_completion = SimpleNamespace(reconcile=AsyncMock(side_effect=cleanup))
    runner._due_turns = SimpleNamespace(due_turns=AsyncMock(side_effect=due))
    runner._clock = SimpleNamespace(now_ms=8000)
    assert await runner.run_once() == ()
    assert order == ["cleanup", "due"]


@pytest.mark.asyncio
async def test_cleanup_discovery_failure_does_not_stop_turn_discovery():
    runner = object.__new__(TurnResolutionRunner)
    runner._captured_completion = SimpleNamespace(reconcile=AsyncMock(side_effect=RuntimeError))
    runner._due_turns = SimpleNamespace(due_turns=AsyncMock(return_value=()))
    runner._clock = SimpleNamespace(now_ms=8000)
    assert await runner.run_once() == ()
    runner._due_turns.due_turns.assert_awaited_once()


@pytest.mark.asyncio
async def test_redis_pending_scan_resumes_between_batches_and_new_instance_rediscovers():
    from seokpan.persistence.redis.start_completion import RedisCapturedCompletionStore

    keys = [f"stone:v1:room:{{{R}}}:normal-completion-pending:{g}" for g in [G, G, "later"]]

    class Client:
        def scan_iter(self, **kwargs):
            async def scan():
                for key in keys:
                    yield key

            return scan()

    client = Client()
    store = RedisCapturedCompletionStore(client)
    assert await store.pending(limit=2) == ((R, G),)
    assert await store.pending(limit=2) == ((R, "later"),)
    assert await RedisCapturedCompletionStore(client).pending(limit=2) == ((R, G),)


@pytest.mark.parametrize("value", ["[]", "null", "{}", '{"schema_version":1,"schema_version":1}'])
def test_invalid_pending_json_rejected(value):
    from seokpan.room.application.start_completion import PendingCapturedCompletion

    with pytest.raises(RoomRuleViolation):
        PendingCapturedCompletion.from_json(value)


@pytest.mark.asyncio
async def test_redis_reader_uses_one_script_positional_argument(context):
    from seokpan.persistence.redis.start_completion import RedisCapturedCompletionStore

    store = RedisCapturedCompletionStore(object())
    store._scripts = SimpleNamespace(
        execute=AsyncMock(
            return_value=json.dumps(
                {
                    "ok": True,
                    "error": None,
                    "intent": context.intent.to_json(),
                    "phase": context.phase,
                }
            )
        )
    )
    await store.read_start(R, G)
    assert len(store._scripts.execute.call_args.args) == 1
