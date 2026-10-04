"""Memory adapter lifecycle regressions; real Redis coverage is a separate gate."""

from types import SimpleNamespace

import pytest

from seokpan.persistence.memory.room_adapter import InMemoryRoomRuntimeAdapter
from seokpan.persistence.memory.start_closure import InMemoryCapturedClosureStore
from seokpan.room.application.runtime import ROOM_REQUEST_DEDUPE_TTL_MS
from seokpan.room.application.start_intent import RoomGameStartIntent, StartIntentPlayer
from seokpan.room.domain import GameTermination, RoomRuleViolation

R = "00000000-0000-4000-8000-000000000001"
G = "00000000-0000-4000-8000-000000000002"
B = "00000000-0000-4000-8000-000000000003"
W = "00000000-0000-4000-8000-000000000004"


@pytest.fixture
def harness():
    clock = SimpleNamespace(now_ms=5000)
    rooms = InMemoryRoomRuntimeAdapter(clock)
    votes = SimpleNamespace(_states={})
    intent = RoomGameStartIntent(
        room_id=R,
        game_id=G,
        original_request_id="accepted",
        owner_id=B,
        accepted_state_version=7,
        started_at_ms=1000,
        vote_seconds=15,
        players=(
            StartIntentPlayer(B, "BLACK", member_id="1"),
            StartIntentPlayer(W, "WHITE", guest_label="Guest-0001"),
        ),
    )
    rooms._start_intents[(R, G)] = intent
    rooms._start_phases[(R, G)] = "PENDING"
    rooms._close(
        SimpleNamespace(room_id=R, request_id="leave"),
        departure=SimpleNamespace(
            game_termination=GameTermination.SYSTEM_INVALID,
            terminated_game_id=G,
        ),
        vote_removed=False,
    )
    store = InMemoryCapturedClosureStore(rooms=rooms, votes=votes)
    return SimpleNamespace(
        clock=clock,
        rooms=rooms,
        votes=votes,
        intent=intent,
        store=store,
    )


@pytest.mark.asyncio
async def test_pending_closure_survives_original_ttl_and_still_has_intent(harness):
    h = harness
    h.clock.now_ms += ROOM_REQUEST_DEDUPE_TTL_MS + 1
    h.rooms._purge_expired()
    value = await h.store.read_closed_start(R, G, 5000)
    assert value is not None and not value.initialized
    assert await h.rooms.pending_game_invalidations(limit=100)
    assert await h.rooms.get_start_intent(R, G) == h.intent


@pytest.mark.asyncio
async def test_ack_retains_receipt_then_expires_once_without_sliding(harness):
    h = harness
    value = await h.store.read_closed_start(R, G, 5000)
    await h.store.acknowledge(value)
    expiry = h.rooms._start_record_expiries[(R, G)]
    assert not await h.rooms.pending_game_invalidations(limit=100)
    h.clock.now_ms += 100
    receipt = await h.store.read_closed_start(R, G, 5000)
    assert receipt.acknowledged
    await h.store.acknowledge(receipt)
    assert h.rooms._start_record_expiries[(R, G)] == expiry
    h.clock.now_ms = expiry
    assert await h.rooms.get_start_intent(R, G) is None
    assert (R, G) not in h.rooms._start_phases
    assert (R, G) not in h.rooms._captured_closure_receipts
    assert not h.rooms.has_tombstone(R)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "conflict",
    ["live_room", "runtime", "foreign_runtime", "marker", "intent", "phase"],
)
async def test_ack_conflict_keeps_pending_evidence(harness, conflict):
    h = harness
    value = await h.store.read_closed_start(R, G, 5000)
    if conflict == "live_room":
        h.rooms._rooms[R] = SimpleNamespace()
    elif conflict in {"runtime", "foreign_runtime"}:
        h.votes._states[R] = SimpleNamespace(game_id=G if conflict == "runtime" else "other")
    elif conflict == "marker":
        h.rooms._pending_game_invalidations[R] = SimpleNamespace(game_id="other", closed_at_ms=5000)
    elif conflict == "intent":
        h.rooms._start_intents.pop((R, G))
    else:
        h.rooms._start_phases[(R, G)] = "corrupt"
    with pytest.raises(RoomRuleViolation):
        await h.store.acknowledge(value)
    assert R in h.rooms._pending_game_invalidations
    assert not h.rooms._start_record_expiries


@pytest.mark.asyncio
async def test_legacy_ack_sets_retention_only_after_completion(harness):
    h = harness
    h.rooms._start_intents.clear()
    h.rooms._start_phases.clear()
    assert await h.store.read_closed_start(R, G, 5000) is None
    await h.rooms.complete_game_invalidation(R, G)
    expiry = h.rooms._tombstones[R]
    h.clock.now_ms += 10
    await h.rooms.complete_game_invalidation(R, G)
    assert h.rooms._tombstones[R] == expiry
