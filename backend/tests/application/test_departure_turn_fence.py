"""Deterministic scheduling races with shared Memory ports, not Redis/Cloud proof."""

import asyncio
import json
from uuid import uuid4

import pytest
from test_turn_resolution_runner import (
    BLACK_ID,
    BLACK_TWO_ID,
    GAME_ID,
    ROOM_ID,
    WHITE_ID,
    setup_runner,
)

from seokpan.game.application import DueTurn, TurnProcessingStatus, TurnResolutionRunner
from seokpan.game.application.captured_completion import CapturedGameCompletion
from seokpan.game.application.captured_invalidation import CapturedGameInvalidation
from seokpan.game.domain import EndReason, Stone
from seokpan.persistence.memory import InMemoryGamePersistenceAdapter
from seokpan.persistence.memory.resolution import MemoryRoomTurnSource
from seokpan.persistence.memory.start_closure import InMemoryCapturedClosureStore
from seokpan.persistence.memory.start_completion import InMemoryCapturedCompletionStore
from seokpan.persistence.redis.common import RedisProviderError
from seokpan.room.application import JoinRoomRuntime, LeaveRoomRuntime
from seokpan.room.application.start_intent import RoomGameStartIntent, StartIntentPlayer
from seokpan.room.domain import ActorType, RoomStatus
from seokpan.vote.application import CastRuntimeVote
from seokpan.vote.domain import VoteRuleViolation


class PausedResult(InMemoryGamePersistenceAdapter):
    def __init__(self, reason):
        super().__init__({1: 1000, 2: 1000, 3: 1000})
        self.reason = reason
        self.pause_once = True
        self.ready, self.resume = asyncio.Event(), asyncio.Event()
        self.created_results = 0

    async def finalize_game(self, command):
        if self.pause_once and command.result.end_reason is self.reason:
            self.pause_once = False
            self.ready.set()
            await self.resume.wait()
        outcome = await super().finalize_game(command)
        self.created_results += outcome.value == "CREATED"
        return outcome


def captured(runner, rooms, votes, games):
    intent = RoomGameStartIntent(
        room_id=ROOM_ID,
        game_id=GAME_ID,
        owner_id=BLACK_ID,
        original_request_id="start",
        accepted_state_version=10,
        started_at_ms=0,
        vote_seconds=5,
        players=tuple(
            StartIntentPlayer(p, t, member_id=m)
            for p, t, m in (
                (BLACK_ID, "BLACK", "1"),
                (WHITE_ID, "WHITE", "2"),
                (BLACK_TWO_ID, "BLACK", "3"),
            )
        ),
    )
    rooms._start_intents[ROOM_ID, GAME_ID] = intent
    rooms._start_phases[ROOM_ID, GAME_ID] = json.dumps(
        {
            "schema_version": 1,
            "phase": "INITIALIZED",
            "game_id": GAME_ID,
            "intent_fingerprint": intent.fingerprint,
            "initialized_at_ms": 0,
            "first_deadline_ms": 5000,
        }
    )
    runner._captured_completion = CapturedGameCompletion(
        records=InMemoryCapturedCompletionStore(rooms=rooms, votes=votes),
        rooms=rooms,
        games=games,
    )
    runner._captured_invalidation = CapturedGameInvalidation(
        closures=InMemoryCapturedClosureStore(rooms=rooms, votes=votes),
        games=games,
    )


def fork(runner, rooms, votes, games, clock, *, owner="other-pod"):
    return TurnResolutionRunner(
        due_turns=MemoryRoomTurnSource(rooms, votes),
        finalization_gate=runner._finalization_gate,
        tie_selector=runner._tie_selector,
        tie_audit=runner._tie_audit,
        votes=votes,
        games=games,
        rooms=rooms,
        clock=clock,
        runner_id=owner,
        events=runner._events,
        captured_completion=runner._captured_completion,
        captured_invalidation=runner._captured_invalidation,
    )


async def leave(rooms, participant):
    room = await rooms.get(ROOM_ID)
    return await rooms.leave(
        LeaveRoomRuntime(
            ROOM_ID,
            f"leave-{participant}",
            participant,
            room.state_version,
            active_vote_turn=None,
        )
    )


async def winning_turn(runner, clock, votes):
    for turn_no, (participant, coordinate) in enumerate(
        (
            (BLACK_ID, "A1"),
            (WHITE_ID, "A2"),
            (BLACK_ID, "B1"),
            (WHITE_ID, "B2"),
            (BLACK_ID, "C1"),
            (WHITE_ID, "C2"),
            (BLACK_ID, "D1"),
            (WHITE_ID, "D2"),
        ),
        1,
    ):
        state = await votes.get(ROOM_ID)
        await votes.cast_vote(
            CastRuntimeVote(
                ROOM_ID,
                f"vote-{turn_no}",
                GAME_ID,
                turn_no,
                participant,
                coordinate,
                state.state_version,
            )
        )
        clock.advance(5000)
        await runner.process(DueTurn(ROOM_ID, GAME_ID, turn_no))
    state = await votes.get(ROOM_ID)
    await votes.cast_vote(
        CastRuntimeVote(
            ROOM_ID,
            "vote-9",
            GAME_ID,
            9,
            BLACK_ID,
            "E1",
            state.state_version,
        )
    )
    before = await votes.get(ROOM_ID)
    clock.advance(5000)
    return before


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "captured"])
@pytest.mark.parametrize("stale_voting_read", [False, True])
async def test_durable_winning_move_cannot_be_replaced_by_departure(
    mode,
    stale_voting_read,
    monkeypatch,
):
    games = PausedResult(EndReason.BLACK_WIN)
    runner, clock, rooms, votes, _, _ = await setup_runner(games=games)
    if mode == "captured":
        captured(runner, rooms, votes, games)
    before = await winning_turn(runner, clock, votes)
    task = asyncio.create_task(runner.process(DueTurn(ROOM_ID, GAME_ID, 9)))
    await games.ready.wait()
    try:
        assert (await games.get_move(GAME_ID, 9)).coordinate.canonical == "E1"
        await leave(rooms, BLACK_ID)
        await leave(rooms, BLACK_TWO_ID)
        if stale_voting_read:
            original = votes.get
            first = True

            async def old_read(room_id):
                nonlocal first
                if first:
                    first = False
                    return before
                return await original(room_id)

            monkeypatch.setattr(votes, "get", old_read)
        assert not await runner.finalize_departures(room_id=ROOM_ID, game_id=GAME_ID)
        assert await games.load_result(GAME_ID) is None
    finally:
        games.resume.set()
    assert (await task).status is TurnProcessingStatus.GAME_ENDED
    assert (await games.load_result(GAME_ID)).end_reason is EndReason.BLACK_WIN
    runtime = await votes.get(ROOM_ID)
    assert runtime.move_no == 9 and len(runtime.occupied_cells) == 9
    assert (await rooms.get(ROOM_ID)).status is RoomStatus.WAITING
    assert games.created_results == 1
    assert games.member_ratings == {1: 1016, 2: 984, 3: 1016}
    if mode == "captured":
        assert (
            json.loads(rooms._start_phases[ROOM_ID, GAME_ID])["normal_completion"]["end_reason"]
            == "BLACK_WIN"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "captured"])
async def test_new_worker_recovers_winning_move_after_lease_expiry(mode):
    games = PausedResult(EndReason.BLACK_WIN)
    runner, clock, rooms, votes, _, _ = await setup_runner(games=games)
    if mode == "captured":
        captured(runner, rooms, votes, games)
    await winning_turn(runner, clock, votes)
    task = asyncio.create_task(runner.process(DueTurn(ROOM_ID, GAME_ID, 9)))
    await games.ready.wait()
    try:
        await leave(rooms, BLACK_ID)
        await leave(rooms, BLACK_TWO_ID)
        worker = fork(runner, rooms, votes, games, clock)
        assert not await worker.finalize_departures(room_id=ROOM_ID, game_id=GAME_ID)
        clock.advance(5001)
        assert (
            await worker.process(DueTurn(ROOM_ID, GAME_ID, 9))
        ).status is TurnProcessingStatus.GAME_ENDED
    finally:
        games.resume.set()
    assert (await task).status is TurnProcessingStatus.RETRY_REQUIRED
    assert (await games.load_result(GAME_ID)).winner is Stone.BLACK
    assert games.created_results == 1 and (await votes.get(ROOM_ID)).move_no == 9


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "captured"])
async def test_lost_nonterminal_apply_reply_rediscovers_departure_before_next_deadline(
    mode,
    monkeypatch,
):
    runner, clock, rooms, votes, games, _ = await setup_runner()
    if mode == "captured":
        captured(runner, rooms, votes, games)
    state = await votes.get(ROOM_ID)
    await votes.cast_vote(
        CastRuntimeVote(
            ROOM_ID,
            "vote",
            GAME_ID,
            1,
            BLACK_ID,
            "A1",
            state.state_version,
        )
    )
    original = votes.apply_resolution

    async def lose_reply(command):
        await leave(rooms, BLACK_ID)
        await leave(rooms, BLACK_TWO_ID)
        assert not await runner.finalize_departures(room_id=ROOM_ID, game_id=GAME_ID)
        await original(command)
        raise RedisProviderError()

    monkeypatch.setattr(votes, "apply_resolution", lose_reply)
    clock.advance(5000)
    with pytest.raises(RedisProviderError):
        await runner.process(DueTurn(ROOM_ID, GAME_ID, 1))
    runtime = await votes.get(ROOM_ID)
    assert runtime.turn_no == 2 and runtime.deadline_ms == 10000
    worker = fork(runner, rooms, votes, games, clock)
    assert (await worker.run_once())[0].status is TurnProcessingStatus.GAME_ENDED
    assert (await games.load_result(GAME_ID)).end_reason is EndReason.FORFEIT
    assert len((await games.load_game(GAME_ID)).moves) == (await votes.get(ROOM_ID)).move_no == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "captured"])
async def test_claim_before_closure_preserves_one_decision_after_crash_and_retry(mode):
    games = PausedResult(EndReason.FORFEIT)
    runner, clock, rooms, votes, _, _ = await setup_runner(games=games)
    if mode == "captured":
        captured(runner, rooms, votes, games)
    await leave(rooms, BLACK_ID)
    await leave(rooms, BLACK_TWO_ID)
    task = asyncio.create_task(runner.finalize_departures(room_id=ROOM_ID, game_id=GAME_ID))
    await games.ready.wait()
    try:
        intent = (await votes.get(ROOM_ID)).resolver.departure
        assert intent.winner is Stone.WHITE and intent.ended_at_ms == 0
        # A concurrent invocation in the same Pod cannot share/renew this owner.
        assert not await runner.finalize_departures(room_id=ROOM_ID, game_id=GAME_ID)
        await leave(rooms, WHITE_ID)
        assert await rooms.get(ROOM_ID) is None
        clock.advance(5001)
        worker = fork(runner, rooms, votes, games, clock)
        assert await worker.reconcile_game_invalidations() == 1
        assert await rooms.pending_game_invalidations(limit=100) == ()
        assert await votes.get(ROOM_ID) is None
        assert (await games.load_result(GAME_ID)).end_reason is EndReason.FORFEIT
        assert (await games.load_result(GAME_ID)).ended_at.timestamp() == 0
        assert await worker.reconcile_game_invalidations() == 0
    finally:
        games.resume.set()
    assert not await task
    assert games.created_results == 1 and games.member_ratings == {1: 984, 2: 1016, 3: 984}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "captured"])
async def test_closure_before_atomic_claim_cannot_create_departure_result(mode, monkeypatch):
    runner, clock, rooms, votes, games, _ = await setup_runner()
    if mode == "captured":
        captured(runner, rooms, votes, games)
    await leave(rooms, BLACK_ID)
    await leave(rooms, BLACK_TWO_ID)
    ready, resume = asyncio.Event(), asyncio.Event()
    original = votes.acquire_departure

    async def pause(command):
        ready.set()
        await resume.wait()
        return await original(command)

    monkeypatch.setattr(votes, "acquire_departure", pause)
    task = asyncio.create_task(runner.finalize_departures(room_id=ROOM_ID, game_id=GAME_ID))
    await ready.wait()
    try:
        await leave(rooms, WHITE_ID)
        worker = fork(runner, rooms, votes, games, clock)
        assert await worker.reconcile_game_invalidations() == 1
        assert (await games.load_result(GAME_ID)).end_reason is EndReason.SYSTEM_INVALID
    finally:
        resume.set()
    assert not await task
    assert games.member_ratings == {1: 1000, 2: 1000, 3: 1000}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "captured"])
async def test_expired_departure_owner_takeover_preserves_time_result_and_ratings(
    mode,
    monkeypatch,
):
    games = PausedResult(EndReason.FORFEIT)
    runner, clock, rooms, votes, _, _ = await setup_runner(games=games)
    if mode == "captured":
        captured(runner, rooms, votes, games)
    await leave(rooms, BLACK_ID)
    await leave(rooms, BLACK_TWO_ID)
    leases = []
    original = votes.acquire_departure

    async def remember(command):
        result = await original(command)
        leases.append(result.snapshot.resolver)
        return result

    monkeypatch.setattr(votes, "acquire_departure", remember)
    task = asyncio.create_task(runner.finalize_departures(room_id=ROOM_ID, game_id=GAME_ID))
    await games.ready.wait()
    try:
        clock.advance(5001)
        worker = fork(runner, rooms, votes, games, clock)
        assert (await worker.run_once())[0].status is TurnProcessingStatus.GAME_ENDED
    finally:
        games.resume.set()
    assert await task
    assert len(leases) == 2 and leases[0].resolution_id != leases[1].resolution_id
    assert leases[0].departure == leases[1].departure
    assert (await games.load_result(GAME_ID)).ended_at.timestamp() == 0
    assert games.created_results == 1 and games.member_ratings == {1: 984, 2: 1016, 3: 984}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "captured"])
async def test_late_runtime_finalizer_after_confirmed_closure_ack_is_stale(mode, monkeypatch):
    runner, clock, rooms, votes, games, _ = await setup_runner()
    if mode == "captured":
        captured(runner, rooms, votes, games)
    await leave(rooms, BLACK_ID)
    await leave(rooms, BLACK_TWO_ID)
    ready, resume = asyncio.Event(), asyncio.Event()
    original = votes.finalize_game

    async def pause(command):
        ready.set()
        await resume.wait()
        return await original(command)

    monkeypatch.setattr(votes, "finalize_game", pause)
    task = asyncio.create_task(runner.finalize_departures(room_id=ROOM_ID, game_id=GAME_ID))
    await ready.wait()
    try:
        assert (await games.load_result(GAME_ID)).end_reason is EndReason.FORFEIT
        await leave(rooms, WHITE_ID)
        worker = fork(runner, rooms, votes, games, clock)
        assert await worker.reconcile_game_invalidations() == 1
        assert await votes.get(ROOM_ID) is None
    finally:
        resume.set()
    assert not await task
    assert games.member_ratings == {1: 984, 2: 1016, 3: 984}


@pytest.mark.asyncio
async def test_unexplained_departure_runtime_rejection_is_not_swallowed(monkeypatch):
    runner, _clock, rooms, votes, games, _ = await setup_runner()
    await leave(rooms, BLACK_ID)
    await leave(rooms, BLACK_TWO_ID)

    async def unexplained(_command):
        raise VoteRuleViolation("STATE_VERSION_CONFLICT")

    monkeypatch.setattr(votes, "finalize_game", unexplained)
    with pytest.raises(VoteRuleViolation, match="STATE_VERSION_CONFLICT"):
        await runner.finalize_departures(room_id=ROOM_ID, game_id=GAME_ID)
    assert (await games.load_result(GAME_ID)).end_reason is EndReason.FORFEIT
    assert (await votes.get(ROOM_ID)).resolver.departure is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "captured"])
async def test_spectator_join_cas_conflict_cannot_turn_confirmed_forfeit_into_passes(
    mode,
    monkeypatch,
):
    runner, clock, rooms, votes, games, _ = await setup_runner()
    if mode == "captured":
        captured(runner, rooms, votes, games)
    await leave(rooms, BLACK_ID)
    await leave(rooms, BLACK_TWO_ID)
    original = votes.acquire_departure

    async def contend(command):
        room = await rooms.get(ROOM_ID)
        guest = uuid4()
        await rooms.join(
            JoinRoomRuntime(
                ROOM_ID,
                f"join-{guest.hex}",
                str(guest),
                ActorType.GUEST,
                guest.hex * 2,
                room.state_version,
            )
        )
        return await original(command)

    monkeypatch.setattr(votes, "acquire_departure", contend)
    for _ in range(2):
        clock.advance(5000)
        assert (
            await runner.process(DueTurn(ROOM_ID, GAME_ID, 1))
        ).status is TurnProcessingStatus.RETRY_REQUIRED
        runtime = await votes.get(ROOM_ID)
        assert runtime.turn_no == 1 and runtime.consecutive_passes == 0
        assert await games.load_result(GAME_ID) is None
    monkeypatch.setattr(votes, "acquire_departure", original)
    worker = fork(runner, rooms, votes, games, clock)
    assert (await worker.run_once())[0].status is TurnProcessingStatus.GAME_ENDED
    assert (await games.load_result(GAME_ID)).end_reason is EndReason.FORFEIT
    assert (await games.load_result(GAME_ID)).winner is Stone.WHITE
    assert games.member_ratings == {1: 984, 2: 1016, 3: 984}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "captured"])
async def test_departure_after_pending_read_is_atomically_rejected_by_close(mode, monkeypatch):
    runner, clock, rooms, votes, games, _ = await setup_runner()
    if mode == "captured":
        captured(runner, rooms, votes, games)
    original = votes.close_turn

    async def leave_before_close(command):
        await leave(rooms, BLACK_ID)
        await leave(rooms, BLACK_TWO_ID)
        return await original(command)

    monkeypatch.setattr(votes, "close_turn", leave_before_close)
    clock.advance(5000)
    assert (
        await runner.process(DueTurn(ROOM_ID, GAME_ID, 1))
    ).status is TurnProcessingStatus.RETRY_REQUIRED
    assert (await votes.get(ROOM_ID)).turn_no == 1
    assert await games.load_result(GAME_ID) is None
    worker = fork(runner, rooms, votes, games, clock)
    assert (await worker.run_once())[0].status is TurnProcessingStatus.GAME_ENDED
    assert (await games.load_result(GAME_ID)).end_reason is EndReason.FORFEIT
    assert (await games.load_result(GAME_ID)).winner is Stone.WHITE
