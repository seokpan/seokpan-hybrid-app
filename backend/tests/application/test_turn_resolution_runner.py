from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest

from seokpan.game.application import (
    DueTurn,
    FinalizeGameCommand,
    GameParticipantRecord,
    OfficialMoveRecord,
    PersistenceOutcome,
    PersistenceRuleViolation,
    StartGameCommand,
    TurnFinalizationApproval,
    TurnProcessingResult,
    TurnProcessingStatus,
    TurnResolutionRunner,
)
from seokpan.game.domain import Coordinate, EndReason, GameStatus, Stone
from seokpan.persistence.memory import (
    InMemoryDueTurnSource,
    InMemoryGamePersistenceAdapter,
    InMemoryRealtimeEventAdapter,
    InMemoryRoomRuntimeAdapter,
    InMemoryTieSelectionAudit,
    InMemoryTieSelector,
    InMemoryTurnFinalizationGate,
    InMemoryVoteRuntimeAdapter,
    ManualClock,
)
from seokpan.room.application import (
    ChangeRoomTeam,
    CompleteRoomGame,
    CreateRoomRuntime,
    JoinRoomRuntime,
    LeaveRoomRuntime,
    PendingGameInvalidation,
    RealtimeEventPort,
    RoomMutationResult,
    SetRoomReady,
    StartRoomGame,
)
from seokpan.room.domain import ActorType, RoomConfig, RoomStatus, Team
from seokpan.vote.application import (
    AcquireRuntimeResolver,
    ApplyRuntimeResolution,
    CastRuntimeVote,
    CloseRuntimeTurn,
    InitializeVoteRuntime,
    VoteMutationResult,
)
from seokpan.vote.domain import Voter, VoteRuleViolation

ROOM_ID = "11111111-1111-4111-8111-111111111111"
GAME_ID = "22222222-2222-4222-8222-222222222222"
BLACK_ID = "33333333-3333-4333-8333-333333333333"
WHITE_ID = "44444444-4444-4444-8444-444444444444"
BLACK_TWO_ID = "55555555-5555-4555-8555-555555555555"


class FailOnceAfterPersistenceVoteAdapter(InMemoryVoteRuntimeAdapter):
    def __init__(self, clock: ManualClock) -> None:
        super().__init__(clock)
        self._clock_for_failure = clock
        self.fail_apply_once = True

    async def apply_resolution(self, command: ApplyRuntimeResolution) -> VoteMutationResult:
        if self.fail_apply_once:
            self.fail_apply_once = False
            self._clock_for_failure.advance(5_001)
        return await super().apply_resolution(command)


class FailOnceRoomCompletionAdapter(InMemoryRoomRuntimeAdapter):
    def __init__(self, clock: ManualClock) -> None:
        super().__init__(clock)
        self.fail_complete_once = True
        self.complete_calls = 0

    async def complete_game(self, command: CompleteRoomGame) -> RoomMutationResult:
        self.complete_calls += 1
        if self.fail_complete_once:
            self.fail_complete_once = False
            raise RuntimeError("simulated Room completion failure")
        return await super().complete_game(command)


class CountingGamePersistenceAdapter(InMemoryGamePersistenceAdapter):
    def __init__(self) -> None:
        super().__init__({1: 1000, 2: 1000, 3: 1000})
        self.append_calls = 0
        self.finalize_calls = 0

    async def append_move(self, command: OfficialMoveRecord) -> PersistenceOutcome:
        self.append_calls += 1
        return await super().append_move(command)

    async def finalize_game(self, command: FinalizeGameCommand) -> PersistenceOutcome:
        self.finalize_calls += 1
        return await super().finalize_game(command)


class UncertainResultPersistenceAdapter(CountingGamePersistenceAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.fail_finalize_once = True

    async def finalize_game(self, command: FinalizeGameCommand) -> PersistenceOutcome:
        outcome = await super().finalize_game(command)
        if self.fail_finalize_once:
            self.fail_finalize_once = False
            raise PersistenceRuleViolation("PERSISTENCE_COMMIT_UNCERTAIN")
        return outcome


class FailBeforeResultPersistenceAdapter(CountingGamePersistenceAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.fail_finalize_once = True

    async def finalize_game(self, command: FinalizeGameCommand) -> PersistenceOutcome:
        if self.fail_finalize_once:
            self.fail_finalize_once = False
            self.finalize_calls += 1
            raise RuntimeError("simulated Result persistence failure")
        return await super().finalize_game(command)


async def setup_runner(
    *,
    gate: TurnFinalizationApproval = TurnFinalizationApproval.ALLOWED,
    tie: str = "A1",
    votes: InMemoryVoteRuntimeAdapter | None = None,
    rooms: InMemoryRoomRuntimeAdapter | None = None,
    games: InMemoryGamePersistenceAdapter | None = None,
    events: RealtimeEventPort | None = None,
) -> tuple[
    TurnResolutionRunner,
    ManualClock,
    InMemoryRoomRuntimeAdapter,
    InMemoryVoteRuntimeAdapter,
    InMemoryGamePersistenceAdapter,
    InMemoryTieSelectionAudit,
]:
    clock = ManualClock()
    room_store = rooms if rooms is not None else InMemoryRoomRuntimeAdapter(clock)
    vote_store = votes if votes is not None else InMemoryVoteRuntimeAdapter(clock)
    member_ratings = {1: 1000, 2: 1000, 3: 1000}
    game_store = games if games is not None else InMemoryGamePersistenceAdapter(member_ratings)
    await room_store.create(
        CreateRoomRuntime(
            ROOM_ID,
            "create",
            RoomConfig(name="runner", minimum_ready=2, vote_seconds=5),
            BLACK_ID,
            "a" * 64,
            None,
        )
    )
    await room_store.join(
        JoinRoomRuntime(ROOM_ID, "join-black", BLACK_TWO_ID, ActorType.MEMBER, "b" * 64, 1)
    )
    await room_store.join(
        JoinRoomRuntime(ROOM_ID, "join-white", WHITE_ID, ActorType.MEMBER, "c" * 64, 2)
    )
    await room_store.change_team(ChangeRoomTeam(ROOM_ID, "black", BLACK_ID, Team.BLACK, 3))
    await room_store.change_team(ChangeRoomTeam(ROOM_ID, "black-two", BLACK_TWO_ID, Team.BLACK, 4))
    await room_store.change_team(ChangeRoomTeam(ROOM_ID, "white", WHITE_ID, Team.WHITE, 5))
    await room_store.set_ready(SetRoomReady(ROOM_ID, "ready-black", BLACK_ID, True, 6))
    await room_store.set_ready(SetRoomReady(ROOM_ID, "ready-black-two", BLACK_TWO_ID, True, 7))
    await room_store.set_ready(SetRoomReady(ROOM_ID, "ready-white", WHITE_ID, True, 8))
    await room_store.start_game(StartRoomGame(ROOM_ID, "start", BLACK_ID, GAME_ID, 9))
    await game_store.start_game(
        StartGameCommand(
            game_id=GAME_ID,
            room_id=ROOM_ID,
            voting_time_seconds=5,
            started_at=datetime.fromtimestamp(0, UTC),
            participants=(
                GameParticipantRecord(BLACK_ID, Stone.BLACK, member_id=1),
                GameParticipantRecord(WHITE_ID, Stone.WHITE, member_id=2),
                GameParticipantRecord(BLACK_TWO_ID, Stone.BLACK, member_id=3),
            ),
        )
    )
    await vote_store.initialize(
        InitializeVoteRuntime(
            ROOM_ID,
            "initialize",
            GAME_ID,
            (
                Voter(BLACK_ID, Stone.BLACK),
                Voter(BLACK_TWO_ID, Stone.BLACK),
                Voter(WHITE_ID, Stone.WHITE),
            ),
            5_000,
            1,
        )
    )
    audit = InMemoryTieSelectionAudit()
    runner = TurnResolutionRunner(
        due_turns=InMemoryDueTurnSource((DueTurn(ROOM_ID, GAME_ID, 1),)),
        finalization_gate=InMemoryTurnFinalizationGate(gate),
        tie_selector=InMemoryTieSelector(tie),
        tie_audit=audit,
        votes=vote_store,
        games=game_store,
        rooms=room_store,
        clock=clock,
        runner_id="runner-a",
        events=events,
    )
    return runner, clock, room_store, vote_store, game_store, audit


@pytest.mark.asyncio
async def test_departure_finalization_ignores_room_without_confirmed_departure() -> None:
    runner, _clock, rooms, votes, games, _ = await setup_runner()

    finalized = await runner.finalize_departures(
        room_id=ROOM_ID,
        game_id=GAME_ID,
    )

    assert finalized is False
    assert games.results == {}
    runtime = await votes.get(ROOM_ID)
    assert runtime is not None
    assert runtime.game_status is GameStatus.ACTIVE
    room = await rooms.get(ROOM_ID)
    assert room is not None
    assert room.status is RoomStatus.PLAYING


@pytest.mark.asyncio
async def test_departure_finalization_waits_until_an_entire_team_has_left() -> None:
    runner, _clock, rooms, votes, games, _ = await setup_runner()

    room = await rooms.get(ROOM_ID)
    assert room is not None
    await rooms.leave(
        LeaveRoomRuntime(
            ROOM_ID,
            "leave-one-black",
            BLACK_ID,
            room.state_version,
            active_vote_turn=1,
        )
    )

    finalized = await runner.finalize_departures(
        room_id=ROOM_ID,
        game_id=GAME_ID,
    )

    assert finalized is False
    assert games.results == {}
    runtime = await votes.get(ROOM_ID)
    assert runtime is not None
    assert runtime.game_status is GameStatus.ACTIVE
    room = await rooms.get(ROOM_ID)
    assert room is not None
    assert room.status is RoomStatus.PLAYING
    assert room.owner_id == BLACK_TWO_ID


@pytest.mark.asyncio
async def test_departure_finalization_persists_forfeit_then_finishes_runtime_and_room() -> None:
    events = InMemoryRealtimeEventAdapter()
    runner, _clock, rooms, votes, games, _ = await setup_runner(events=events)

    room = await rooms.get(ROOM_ID)
    assert room is not None
    await rooms.leave(
        LeaveRoomRuntime(
            ROOM_ID,
            "leave-black-owner",
            BLACK_ID,
            room.state_version,
            active_vote_turn=1,
        )
    )

    room = await rooms.get(ROOM_ID)
    assert room is not None
    await rooms.leave(
        LeaveRoomRuntime(
            ROOM_ID,
            "leave-black-two",
            BLACK_TWO_ID,
            room.state_version,
            active_vote_turn=1,
        )
    )

    finalized = await runner.finalize_departures(
        room_id=ROOM_ID,
        game_id=GAME_ID,
    )

    assert finalized is True

    stored = await games.load_result(GAME_ID)
    assert stored is not None
    assert stored.status is GameStatus.FINISHED
    assert stored.end_reason is EndReason.FORFEIT
    assert stored.winner is Stone.WHITE

    runtime = await votes.get(ROOM_ID)
    assert runtime is not None
    assert runtime.game_status is GameStatus.FINISHED
    assert runtime.end_reason is EndReason.FORFEIT

    room = await rooms.get(ROOM_ID)
    assert room is not None
    assert room.status is RoomStatus.WAITING
    assert room.game_id is None
    assert room.last_game_id == GAME_ID
    assert room.owner_id == WHITE_ID


@pytest.mark.asyncio
async def test_departure_retry_reuses_persisted_forfeit_after_room_failure() -> None:
    rooms = FailOnceRoomCompletionAdapter(ManualClock())
    games = CountingGamePersistenceAdapter()
    runner, _clock, _, votes, _, _ = await setup_runner(
        rooms=rooms,
        games=games,
    )

    room = await rooms.get(ROOM_ID)
    assert room is not None
    await rooms.leave(
        LeaveRoomRuntime(
            ROOM_ID,
            "leave-black-owner",
            BLACK_ID,
            room.state_version,
            active_vote_turn=1,
        )
    )

    room = await rooms.get(ROOM_ID)
    assert room is not None
    await rooms.leave(
        LeaveRoomRuntime(
            ROOM_ID,
            "leave-black-two",
            BLACK_TWO_ID,
            room.state_version,
            active_vote_turn=1,
        )
    )

    with pytest.raises(RuntimeError, match="simulated Room completion failure"):
        await runner.finalize_departures(
            room_id=ROOM_ID,
            game_id=GAME_ID,
        )

    assert games.finalize_calls == 1

    stored_before_retry = await games.load_result(GAME_ID)
    assert stored_before_retry is not None
    assert stored_before_retry.end_reason is EndReason.FORFEIT
    assert stored_before_retry.winner is Stone.WHITE

    runtime_before_retry = await votes.get(ROOM_ID)
    assert runtime_before_retry is not None
    assert runtime_before_retry.game_status is GameStatus.FINISHED
    assert runtime_before_retry.end_reason is EndReason.FORFEIT

    room_before_retry = await rooms.get(ROOM_ID)
    assert room_before_retry is not None
    assert room_before_retry.status is RoomStatus.PLAYING
    assert room_before_retry.game_id == GAME_ID
    assert rooms.complete_calls == 1

    finalized = await runner.finalize_departures(
        room_id=ROOM_ID,
        game_id=GAME_ID,
    )

    assert finalized is True
    assert games.finalize_calls == 1
    assert await games.load_result(GAME_ID) == stored_before_retry

    runtime_after_retry = await votes.get(ROOM_ID)
    assert runtime_after_retry is not None
    assert runtime_after_retry.game_status is GameStatus.FINISHED
    assert runtime_after_retry.end_reason is EndReason.FORFEIT

    room_after_retry = await rooms.get(ROOM_ID)
    assert room_after_retry is not None
    assert room_after_retry.status is RoomStatus.WAITING
    assert room_after_retry.game_id is None
    assert room_after_retry.last_game_id == GAME_ID
    assert rooms.complete_calls == 2


@pytest.mark.asyncio
async def test_departure_requires_durable_game_history() -> None:
    runner, _clock, rooms, _votes, games, _ = await setup_runner()

    room = await rooms.get(ROOM_ID)
    assert room is not None
    await rooms.leave(
        LeaveRoomRuntime(
            ROOM_ID,
            "leave-black",
            BLACK_ID,
            room.state_version,
            active_vote_turn=1,
        )
    )
    games.games.pop(GAME_ID)

    with pytest.raises(PersistenceRuleViolation, match="^GAME_NOT_FOUND$"):
        await runner.finalize_departures(
            room_id=ROOM_ID,
            game_id=GAME_ID,
        )


@pytest.mark.asyncio
async def test_departure_retry_recovers_after_runtime_lookup_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    games = CountingGamePersistenceAdapter()
    runner, _clock, rooms, votes, _, _ = await setup_runner(games=games)

    room = await rooms.get(ROOM_ID)
    assert room is not None
    await rooms.leave(
        LeaveRoomRuntime(
            ROOM_ID,
            "leave-black-owner",
            BLACK_ID,
            room.state_version,
            active_vote_turn=1,
        )
    )

    room = await rooms.get(ROOM_ID)
    assert room is not None
    await rooms.leave(
        LeaveRoomRuntime(
            ROOM_ID,
            "leave-black-two",
            BLACK_TWO_ID,
            room.state_version,
            active_vote_turn=1,
        )
    )

    original_get = votes.get
    runtime = await original_get(ROOM_ID)
    assert runtime is not None

    monkeypatch.setattr(
        votes,
        "get",
        AsyncMock(side_effect=(runtime, None)),
    )

    assert (
        await runner.finalize_departures(
            room_id=ROOM_ID,
            game_id=GAME_ID,
        )
        is False
    )
    assert games.finalize_calls == 1

    stored = await games.load_result(GAME_ID)
    assert stored is not None
    assert stored.end_reason is EndReason.FORFEIT
    assert stored.winner is Stone.WHITE

    room = await rooms.get(ROOM_ID)
    assert room is not None
    assert room.status is RoomStatus.PLAYING
    assert room.game_id == GAME_ID

    monkeypatch.setattr(votes, "get", original_get)

    assert await runner.finalize_departures(
        room_id=ROOM_ID,
        game_id=GAME_ID,
    )
    assert games.finalize_calls == 1

    room = await rooms.get(ROOM_ID)
    assert room is not None
    assert room.status is RoomStatus.WAITING
    assert room.game_id is None


@pytest.mark.asyncio
async def test_finished_runtime_without_durable_result_requires_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, clock, _rooms, _votes, games, _ = await setup_runner()

    clock.advance(5_000)
    assert (await runner.process(DueTurn(ROOM_ID, GAME_ID, 1))).status is (
        TurnProcessingStatus.PASS
    )

    clock.advance(5_000)
    due = DueTurn(ROOM_ID, GAME_ID, 2)
    assert (await runner.process(due)).status is TurnProcessingStatus.GAME_ENDED

    finalized = AsyncMock(return_value=False)
    monkeypatch.setattr(games, "game_is_finalized", finalized)

    retried = await runner.process(due)

    assert retried.status is TurnProcessingStatus.RETRY_REQUIRED
    finalized.assert_awaited_once_with(GAME_ID)


@pytest.mark.asyncio
async def test_resolution_rejects_missing_game_history_after_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, clock, _rooms, votes, games, _ = await setup_runner()

    snapshot = await votes.get(ROOM_ID)
    assert snapshot is not None
    await votes.cast_vote(
        CastRuntimeVote(
            ROOM_ID,
            "vote",
            GAME_ID,
            1,
            BLACK_ID,
            "H8",
            snapshot.state_version,
        )
    )
    clock.advance(5_000)

    monkeypatch.setattr(
        games,
        "load_game",
        AsyncMock(return_value=None),
    )

    with pytest.raises(PersistenceRuleViolation, match="^GAME_NOT_FOUND$"):
        await runner.process(DueTurn(ROOM_ID, GAME_ID, 1))

    current = await votes.get(ROOM_ID)
    assert current is not None
    assert current.game_status is GameStatus.ACTIVE
    assert current.move_no == 0
    assert games.moves == {}


@pytest.mark.asyncio
async def test_persisted_move_conflict_prevents_resolution_application() -> None:
    runner, clock, _rooms, votes, games, _ = await setup_runner()

    snapshot = await votes.get(ROOM_ID)
    assert snapshot is not None
    await votes.cast_vote(
        CastRuntimeVote(
            ROOM_ID,
            "vote",
            GAME_ID,
            1,
            BLACK_ID,
            "H8",
            snapshot.state_version,
        )
    )

    conflict = OfficialMoveRecord(
        game_id=GAME_ID,
        turn_no=1,
        move_no=1,
        team=Stone.BLACK,
        coordinate=Coordinate.parse("H8"),
        final_vote_count=0,
        valid_voter_count=2,
        confirmed_at=datetime.fromtimestamp(5, UTC),
    )
    await games.append_move(conflict)

    clock.advance(5_000)

    with pytest.raises(PersistenceRuleViolation, match="^MOVE_SEQUENCE_CONFLICT$"):
        await runner.process(DueTurn(ROOM_ID, GAME_ID, 1))

    current = await votes.get(ROOM_ID)
    assert current is not None
    assert current.game_status is GameStatus.ACTIVE
    assert current.move_no == 0
    assert games.moves == {(GAME_ID, 1): conflict}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("move_no", "expected_code"),
    (
        (2, "MOVE_SEQUENCE_CONFLICT"),
        (1, "GAME_RUNTIME_HISTORY_MISMATCH"),
    ),
)
async def test_corrupt_durable_history_is_rejected(
    move_no: int,
    expected_code: str,
) -> None:
    runner, clock, _rooms, votes, games, _ = await setup_runner()

    clock.advance(5_000)
    assert (await runner.process(DueTurn(ROOM_ID, GAME_ID, 1))).status is (
        TurnProcessingStatus.PASS
    )

    await games.append_move(
        OfficialMoveRecord(
            game_id=GAME_ID,
            turn_no=1,
            move_no=move_no,
            team=Stone.BLACK,
            coordinate=Coordinate.parse("A1"),
            final_vote_count=0,
            valid_voter_count=1,
            confirmed_at=datetime.fromtimestamp(5, UTC),
        )
    )

    snapshot = await votes.get(ROOM_ID)
    assert snapshot is not None
    await votes.cast_vote(
        CastRuntimeVote(
            ROOM_ID,
            "white-vote",
            GAME_ID,
            2,
            WHITE_ID,
            "H8",
            snapshot.state_version,
        )
    )
    clock.advance(5_000)

    with pytest.raises(PersistenceRuleViolation, match=f"^{expected_code}$"):
        await runner.process(DueTurn(ROOM_ID, GAME_ID, 2))

    current = await votes.get(ROOM_ID)
    assert current is not None
    assert current.game_status is GameStatus.ACTIVE
    assert current.move_no == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("boundary", "expected_code"),
    (
        ("missing", "ROOM_NOT_FOUND"),
        ("waiting", "GAME_NOT_IN_CURRENT_ROOM"),
    ),
)
async def test_due_turn_rejects_missing_or_stale_room(
    boundary: str,
    expected_code: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, clock, rooms, votes, games, _ = await setup_runner()

    room_value = None if boundary == "missing" else Mock(status=RoomStatus.WAITING, game_id=GAME_ID)
    monkeypatch.setattr(
        rooms,
        "get",
        AsyncMock(return_value=room_value),
    )

    clock.advance(5_000)

    with pytest.raises(VoteRuleViolation, match=f"^{expected_code}$"):
        await runner.process(DueTurn(ROOM_ID, GAME_ID, 1))

    runtime = await votes.get(ROOM_ID)
    assert runtime is not None
    assert runtime.game_status is GameStatus.ACTIVE
    assert runtime.move_no == 0
    assert games.moves == {}
    assert games.results == {}


@pytest.mark.asyncio
async def test_realtime_delivery_failure_does_not_abort_move() -> None:
    events = Mock(spec=RealtimeEventPort)
    events.room_changed = AsyncMock(side_effect=RuntimeError("event unavailable"))
    events.lobby_rooms_changed = AsyncMock()

    runner, clock, _rooms, votes, games, _ = await setup_runner(events=events)

    snapshot = await votes.get(ROOM_ID)
    assert snapshot is not None
    await votes.cast_vote(
        CastRuntimeVote(
            ROOM_ID,
            "vote",
            GAME_ID,
            1,
            BLACK_ID,
            "H8",
            snapshot.state_version,
        )
    )
    clock.advance(5_000)

    result = await runner.process(DueTurn(ROOM_ID, GAME_ID, 1))

    assert result.status is TurnProcessingStatus.MOVE
    assert games.moves[(GAME_ID, 1)].coordinate.canonical == "H8"

    current = await votes.get(ROOM_ID)
    assert current is not None
    assert current.move_no == 1
    assert current.turn_no == 2

    assert events.room_changed.await_count == 2
    events.lobby_rooms_changed.assert_not_awaited()


@pytest.mark.asyncio
async def test_departure_finalization_does_not_overwrite_normal_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, _clock, rooms, votes, games, _ = await setup_runner()

    room = await rooms.get(ROOM_ID)
    assert room is not None
    await rooms.leave(
        LeaveRoomRuntime(
            ROOM_ID,
            "leave-black-owner",
            BLACK_ID,
            room.state_version,
            active_vote_turn=1,
        )
    )

    room = await rooms.get(ROOM_ID)
    assert room is not None
    await rooms.leave(
        LeaveRoomRuntime(
            ROOM_ID,
            "leave-black-two",
            BLACK_TWO_ID,
            room.state_version,
            active_vote_turn=1,
        )
    )

    normal_result = Mock(
        end_reason=EndReason.BLACK_WIN,
        winner=Stone.BLACK,
    )
    monkeypatch.setattr(
        games,
        "load_result",
        AsyncMock(return_value=normal_result),
    )

    finalized = await runner.finalize_departures(
        room_id=ROOM_ID,
        game_id=GAME_ID,
    )

    assert finalized is False

    runtime = await votes.get(ROOM_ID)
    assert runtime is not None
    assert runtime.game_status is GameStatus.ACTIVE

    room = await rooms.get(ROOM_ID)
    assert room is not None
    assert room.status is RoomStatus.PLAYING
    assert room.game_id == GAME_ID


@pytest.mark.asyncio
async def test_system_invalid_can_remove_authorized_previous_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous_game_id = "66666666-6666-4666-8666-666666666666"

    runner, _clock, rooms, votes, games, _ = await setup_runner()

    previous_runtime = Mock(
        room_id=ROOM_ID,
        game_id=previous_game_id,
        turn_no=7,
        game_status=GameStatus.FINISHED,
    )

    monkeypatch.setattr(
        votes,
        "get",
        AsyncMock(return_value=previous_runtime),
    )
    discard = AsyncMock()
    monkeypatch.setattr(votes, "discard_game", discard)

    captured = Mock()
    captured.prepare_history = AsyncMock()
    captured.permits_previous_runtime_cleanup = AsyncMock(return_value=True)
    captured.acknowledge = AsyncMock(return_value=False)
    runner._captured_invalidation = captured

    finalized = await runner.finalize_system_invalid(
        room_id=ROOM_ID,
        game_id=GAME_ID,
        closed_at_ms=9_000,
    )

    assert finalized is True
    captured.permits_previous_runtime_cleanup.assert_awaited_once()
    discard.assert_awaited_once_with(ROOM_ID, previous_game_id)
    captured.acknowledge.assert_awaited_once()

    stored = await games.load_result(GAME_ID)
    assert stored is not None
    assert stored.end_reason is EndReason.SYSTEM_INVALID

    # Captured ACK=False deliberately falls back to the legacy Room ACK path.
    assert await rooms.get(ROOM_ID) is not None


@pytest.mark.asyncio
async def test_unexpected_resolver_violation_is_not_treated_as_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, clock, _rooms, votes, games, _ = await setup_runner()

    snapshot = await votes.get(ROOM_ID)
    assert snapshot is not None
    await votes.cast_vote(
        CastRuntimeVote(
            ROOM_ID,
            "vote",
            GAME_ID,
            1,
            BLACK_ID,
            "H8",
            snapshot.state_version,
        )
    )
    clock.advance(5_000)

    monkeypatch.setattr(
        votes,
        "acquire_resolver",
        AsyncMock(side_effect=VoteRuleViolation("STALE_GAME")),
    )

    with pytest.raises(VoteRuleViolation, match="^STALE_GAME$"):
        await runner.process(DueTurn(ROOM_ID, GAME_ID, 1))

    assert games.moves == {}
    assert games.results == {}


@pytest.mark.asyncio
async def test_unexpected_apply_violation_is_not_treated_as_lease_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, clock, _rooms, votes, games, _ = await setup_runner()

    snapshot = await votes.get(ROOM_ID)
    assert snapshot is not None
    await votes.cast_vote(
        CastRuntimeVote(
            ROOM_ID,
            "vote",
            GAME_ID,
            1,
            BLACK_ID,
            "H8",
            snapshot.state_version,
        )
    )
    clock.advance(5_000)

    monkeypatch.setattr(
        votes,
        "apply_resolution",
        AsyncMock(side_effect=VoteRuleViolation("STATE_VERSION_CONFLICT")),
    )

    with pytest.raises(VoteRuleViolation, match="^STATE_VERSION_CONFLICT$"):
        await runner.process(DueTurn(ROOM_ID, GAME_ID, 1))

    assert len(games.moves) == 1
    assert games.moves[(GAME_ID, 1)].coordinate.canonical == "H8"

    current = await votes.get(ROOM_ID)
    assert current is not None
    assert current.game_status is GameStatus.ACTIVE
    assert current.move_no == 0


@pytest.mark.asyncio
async def test_missing_persisted_move_after_apply_is_detected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, clock, _rooms, votes, games, _ = await setup_runner()

    snapshot = await votes.get(ROOM_ID)
    assert snapshot is not None
    await votes.cast_vote(
        CastRuntimeVote(
            ROOM_ID,
            "vote",
            GAME_ID,
            1,
            BLACK_ID,
            "H8",
            snapshot.state_version,
        )
    )
    clock.advance(5_000)

    original_append_move = games.append_move
    original_get_move = games.get_move
    hide_next_read = False

    async def append_then_hide(command: OfficialMoveRecord) -> PersistenceOutcome:
        nonlocal hide_next_read
        outcome = await original_append_move(command)
        hide_next_read = True
        return outcome

    async def missing_once_after_append(
        game_id: str,
        turn_no: int,
    ) -> OfficialMoveRecord | None:
        nonlocal hide_next_read
        if hide_next_read:
            hide_next_read = False
            return None
        return await original_get_move(game_id, turn_no)

    monkeypatch.setattr(games, "append_move", append_then_hide)
    monkeypatch.setattr(games, "get_move", missing_once_after_append)

    with pytest.raises(PersistenceRuleViolation, match="^MOVE_NOT_FOUND$"):
        await runner.process(DueTurn(ROOM_ID, GAME_ID, 1))

    assert len(games.moves) == 1
    stored = games.moves[(GAME_ID, 1)]
    assert stored.coordinate.canonical == "H8"

    current = await votes.get(ROOM_ID)
    assert current is not None
    assert current.move_no == 1
    assert current.turn_no == 2


@pytest.mark.asyncio
async def test_departure_finalization_ignores_room_lost_before_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, _clock, rooms, votes, games, _ = await setup_runner()

    runtime = await votes.get(ROOM_ID)
    assert runtime is not None

    monkeypatch.setattr(
        rooms,
        "get",
        AsyncMock(return_value=None),
    )

    finalized = await runner.finalize_departures(
        room_id=ROOM_ID,
        game_id=GAME_ID,
    )

    assert finalized is False
    assert games.results == {}


@pytest.mark.asyncio
async def test_deadline_and_serviceability_gate_leave_turn_unchanged() -> None:
    runner, clock, _, votes, games, _ = await setup_runner(
        gate=TurnFinalizationApproval.RECOVERY_REQUIRED
    )
    due = DueTurn(ROOM_ID, GAME_ID, 1)
    before = await votes.get(ROOM_ID)

    assert (await runner.process(due)).status is TurnProcessingStatus.NOT_DUE
    clock.advance(5_000)
    assert (await runner.process(due)).status is TurnProcessingStatus.RECOVERY_REQUIRED
    assert await votes.get(ROOM_ID) == before
    assert games.moves == {}
    assert games.results == {}


@pytest.mark.asyncio
async def test_single_candidate_persists_official_move_before_runtime_advances() -> None:
    events = InMemoryRealtimeEventAdapter()
    runner, clock, _, votes, games, _ = await setup_runner(events=events)
    room_events = await events.subscribe_room(ROOM_ID)
    snapshot = await votes.get(ROOM_ID)
    assert snapshot is not None
    await votes.cast_vote(
        CastRuntimeVote(ROOM_ID, "vote", GAME_ID, 1, BLACK_ID, "H8", snapshot.state_version)
    )
    clock.advance(5_000)

    result = (await runner.run_once(limit=1))[0]

    assert result.status is TurnProcessingStatus.MOVE
    stored = games.moves[(GAME_ID, 1)]
    assert stored.coordinate.canonical == "H8"
    assert stored.final_vote_count == 1
    assert stored.valid_voter_count == 2
    current = await votes.get(ROOM_ID)
    assert current is not None
    assert current.move_no == 1
    assert current.turn_no == 2
    resolving = await room_events.receive()
    moved = await room_events.receive()
    assert (resolving.event_type, moved.event_type) == (
        "turn.resolving",
        "game.move_applied",
    )
    assert resolving.payload == {
        "game_state_version": 4,
        "team": "BLACK",
        "tally": [{"coordinate": "H8", "count": 1}],
        "valid_voter_count": 2,
        "candidates": ["H8"],
    }
    assert moved.payload["coordinate"] == "H8"
    assert moved.payload["move_no"] == 1
    assert moved.payload["board"] == [{"coordinate": "H8", "stone": "BLACK"}]
    assert moved.payload["final_tally"] == [{"coordinate": "H8", "count": 1}]
    assert moved.state_version == resolving.state_version + 1
    await room_events.close()

    with pytest.raises(ValueError, match="INVALID_DUE_TURN_LIMIT"):
        await runner.run_once(limit=0)


@pytest.mark.asyncio
async def test_tie_selection_is_domain_checked_and_audited() -> None:
    runner, clock, _, votes, games, audit = await setup_runner(tie="B1")
    snapshot = await votes.get(ROOM_ID)
    assert snapshot is not None
    first = await votes.cast_vote(
        CastRuntimeVote(ROOM_ID, "vote-a", GAME_ID, 1, BLACK_ID, "A1", snapshot.state_version)
    )
    await votes.cast_vote(
        CastRuntimeVote(
            ROOM_ID,
            "vote-b",
            GAME_ID,
            1,
            BLACK_TWO_ID,
            "B1",
            first.snapshot.state_version,
        )
    )
    clock.advance(5_000)

    await runner.process(DueTurn(ROOM_ID, GAME_ID, 1))

    assert games.moves[(GAME_ID, 1)].coordinate.canonical == "B1"
    assert audit.records[0].candidates == ("A1", "B1")
    assert audit.records[0].selected_coordinate == "B1"


@pytest.mark.asyncio
async def test_second_zero_vote_waits_for_result_then_resets_room() -> None:
    runner, clock, rooms, votes, games, _ = await setup_runner()
    clock.advance(5_000)
    first = await runner.process(DueTurn(ROOM_ID, GAME_ID, 1))
    assert first.status is TurnProcessingStatus.PASS
    after_first = await votes.get(ROOM_ID)
    assert after_first is not None
    assert after_first.move_no == 0
    clock.advance(5_000)

    second = await runner.process(DueTurn(ROOM_ID, GAME_ID, 2))

    assert second.status is TurnProcessingStatus.GAME_ENDED
    assert games.results[GAME_ID].result.end_reason is EndReason.JOINT_LOSS
    runtime = await votes.get(ROOM_ID)
    assert runtime is not None
    assert runtime.game_status is GameStatus.FINISHED
    assert runtime.move_no == 0
    room = await rooms.get(ROOM_ID)
    assert room is not None
    assert room.status is RoomStatus.WAITING
    assert room.game_id is None
    assert all(not participant.ready for participant in room.participants)


@pytest.mark.asyncio
async def test_game_completion_notifies_room_snapshot_and_lobby_availability() -> None:
    events = InMemoryRealtimeEventAdapter()
    runner, clock, _, _, _, _ = await setup_runner(events=events)
    room_events = await events.subscribe_room(ROOM_ID)
    lobby_events = await events.subscribe_lobby()
    clock.advance(5_000)
    assert (await runner.process(DueTurn(ROOM_ID, GAME_ID, 1))).status is (
        TurnProcessingStatus.PASS
    )
    clock.advance(5_000)

    assert (await runner.process(DueTurn(ROOM_ID, GAME_ID, 2))).status is (
        TurnProcessingStatus.GAME_ENDED
    )
    room_events_received = tuple([await room_events.receive() for _ in range(5)])
    lobby_event = await lobby_events.receive()

    assert tuple(item.event_type for item in room_events_received) == (
        "turn.passed",
        "turn.resolving",
        "turn.passed",
        "game.finished",
        "snapshot.required",
    )
    first_pass, resolving, second_pass, finished, _snapshot = room_events_received
    assert first_pass.payload == {
        "game_state_version": 3,
        "completed_turn_no": 1,
        "next_turn_no": 2,
        "next_team": "WHITE",
        "deadline_ms": 10_000,
        "consecutive_passes": 1,
    }
    assert resolving.payload["valid_voter_count"] == 1
    assert second_pass.payload["next_turn_no"] is None
    assert second_pass.payload["consecutive_passes"] == 2
    assert finished.payload["end_reason"] == "JOINT_LOSS"
    assert finished.payload["winner"] is None
    assert finished.payload["board"] == []
    assert tuple(item.state_version for item in room_events_received) == (2, 3, 4, 5, 6)
    assert lobby_event.event_type == "lobby.rooms_changed"
    assert lobby_event.payload == {"reason": "GAME_COMPLETED", "room_id": ROOM_ID}
    await room_events.close()
    await lobby_events.close()


@pytest.mark.asyncio
async def test_retry_reuses_persisted_move_after_resolver_lease_expiry() -> None:
    clock = ManualClock()
    failing_votes = FailOnceAfterPersistenceVoteAdapter(clock)
    runner, runner_clock, _, votes, games, _ = await setup_runner(votes=failing_votes)
    assert runner_clock is not clock
    snapshot = await votes.get(ROOM_ID)
    assert snapshot is not None
    await votes.cast_vote(
        CastRuntimeVote(ROOM_ID, "vote", GAME_ID, 1, BLACK_ID, "H8", snapshot.state_version)
    )
    runner_clock.advance(5_000)
    clock.advance(5_000)

    first = await runner.process(DueTurn(ROOM_ID, GAME_ID, 1))
    assert first.status is TurnProcessingStatus.RETRY_REQUIRED
    assert len(games.moves) == 1

    runner_clock.advance(1)
    second = await runner.process(DueTurn(ROOM_ID, GAME_ID, 1))
    assert second.status is TurnProcessingStatus.MOVE
    assert len(games.moves) == 1


@pytest.mark.asyncio
async def test_retry_after_pass_reuses_move_with_different_turn_number() -> None:
    provider_clock = ManualClock()
    failing_votes = FailOnceAfterPersistenceVoteAdapter(provider_clock)
    failing_votes.fail_apply_once = False
    events = InMemoryRealtimeEventAdapter()
    runner, clock, _, votes, games, _ = await setup_runner(votes=failing_votes, events=events)
    clock.advance(5_000)
    provider_clock.advance(5_000)
    assert (await runner.process(DueTurn(ROOM_ID, GAME_ID, 1))).status is (
        TurnProcessingStatus.PASS
    )
    snapshot = await votes.get(ROOM_ID)
    assert snapshot is not None
    await votes.cast_vote(
        CastRuntimeVote(ROOM_ID, "white-vote", GAME_ID, 2, WHITE_ID, "H8", snapshot.state_version)
    )
    failing_votes.fail_apply_once = True
    clock.advance(5_000)
    provider_clock.advance(5_000)
    due = DueTurn(ROOM_ID, GAME_ID, 2)
    assert (await runner.process(due)).status is TurnProcessingStatus.RETRY_REQUIRED
    stored = games.moves[(GAME_ID, 1)]
    assert (stored.turn_no, stored.move_no) == (2, 1)
    clock.advance(1)
    assert (await runner.process(due)).status is TurnProcessingStatus.MOVE
    assert games.moves == {(GAME_ID, 1): stored}
    current = await votes.get(ROOM_ID)
    assert current is not None
    assert (current.turn_no, current.move_no, current.consecutive_passes) == (3, 1, 0)
    assert (await runner.process(due)).status is TurnProcessingStatus.STALE
    assert games.results == {}


@pytest.mark.asyncio
async def test_uncertain_result_commit_is_confirmed_before_runtime_and_room_advance() -> None:
    games = UncertainResultPersistenceAdapter()
    runner, clock, rooms, votes, _, _ = await setup_runner(games=games)
    clock.advance(5_000)
    assert (await runner.process(DueTurn(ROOM_ID, GAME_ID, 1))).status is (
        TurnProcessingStatus.PASS
    )
    clock.advance(5_000)
    due = DueTurn(ROOM_ID, GAME_ID, 2)

    with pytest.raises(PersistenceRuleViolation, match="PERSISTENCE_COMMIT_UNCERTAIN"):
        await runner.process(due)

    assert games.finalize_calls == 1
    assert GAME_ID in games.results
    resolving = await votes.get(ROOM_ID)
    assert resolving is not None
    assert resolving.game_status is GameStatus.ACTIVE
    assert (await runner.process(due)).status is TurnProcessingStatus.GAME_ENDED
    assert games.finalize_calls == 1
    room = await rooms.get(ROOM_ID)
    assert room is not None
    assert room.status is RoomStatus.WAITING


@pytest.mark.asyncio
async def test_result_write_failure_retries_before_runtime_and_room_advance() -> None:
    games = FailBeforeResultPersistenceAdapter()
    runner, clock, rooms, votes, _, _ = await setup_runner(games=games)
    clock.advance(5_000)
    assert (await runner.process(DueTurn(ROOM_ID, GAME_ID, 1))).status is (
        TurnProcessingStatus.PASS
    )
    clock.advance(5_000)
    due = DueTurn(ROOM_ID, GAME_ID, 2)

    with pytest.raises(RuntimeError, match="simulated Result persistence failure"):
        await runner.process(due)

    assert GAME_ID not in games.results
    resolving = await votes.get(ROOM_ID)
    assert resolving is not None
    assert resolving.game_status is GameStatus.ACTIVE
    assert (await runner.process(due)).status is TurnProcessingStatus.GAME_ENDED
    assert games.finalize_calls == 2
    room = await rooms.get(ROOM_ID)
    assert room is not None
    assert room.status is RoomStatus.WAITING


@pytest.mark.asyncio
async def test_replayed_due_item_is_stale_after_one_runner_finishes() -> None:
    runner, clock, _, votes, games, _ = await setup_runner()
    snapshot = await votes.get(ROOM_ID)
    assert snapshot is not None
    await votes.cast_vote(
        CastRuntimeVote(ROOM_ID, "vote", GAME_ID, 1, BLACK_ID, "H8", snapshot.state_version)
    )
    clock.advance(5_000)
    due = DueTurn(ROOM_ID, GAME_ID, 1)

    assert (await runner.process(due)).status is TurnProcessingStatus.MOVE
    assert (await runner.process(due)).status is TurnProcessingStatus.STALE
    assert len(games.moves) == 1


@pytest.mark.asyncio
async def test_competing_runner_observes_existing_resolver_lease() -> None:
    runner, clock, _, votes, _, _ = await setup_runner()
    snapshot = await votes.get(ROOM_ID)
    assert snapshot is not None
    voted = await votes.cast_vote(
        CastRuntimeVote(ROOM_ID, "vote", GAME_ID, 1, BLACK_ID, "H8", snapshot.state_version)
    )
    clock.advance(5_000)
    closed = await votes.close_turn(
        CloseRuntimeTurn(ROOM_ID, "manual-close", GAME_ID, 1, voted.snapshot.state_version, 10_000)
    )
    await votes.acquire_resolver(
        AcquireRuntimeResolver(
            ROOM_ID,
            "manual-lease",
            GAME_ID,
            1,
            "other-runner",
            closed.snapshot.state_version,
        )
    )

    result = await runner.process(DueTurn(ROOM_ID, GAME_ID, 1))

    assert result.status is TurnProcessingStatus.RESOLVER_BUSY


@pytest.mark.asyncio
async def test_winning_move_persists_result_before_room_returns_to_waiting() -> None:
    runner, clock, rooms, votes, games, _ = await setup_runner()
    sequence = (
        (BLACK_ID, "A1"),
        (WHITE_ID, "A2"),
        (BLACK_ID, "B1"),
        (WHITE_ID, "B2"),
        (BLACK_ID, "C1"),
        (WHITE_ID, "C2"),
        (BLACK_ID, "D1"),
        (WHITE_ID, "D2"),
        (BLACK_ID, "E1"),
    )

    final = None
    for turn_no, (participant_id, coordinate) in enumerate(sequence, 1):
        snapshot = await votes.get(ROOM_ID)
        assert snapshot is not None
        await votes.cast_vote(
            CastRuntimeVote(
                ROOM_ID,
                f"vote-{turn_no}",
                GAME_ID,
                turn_no,
                participant_id,
                coordinate,
                snapshot.state_version,
            )
        )
        clock.advance(5_000)
        final = await runner.process(DueTurn(ROOM_ID, GAME_ID, turn_no))

    assert final is not None
    assert final.status is TurnProcessingStatus.GAME_ENDED
    assert games.results[GAME_ID].result.end_reason is EndReason.BLACK_WIN
    assert len(games.results[GAME_ID].result.rating_adjustments) == 3
    room = await rooms.get(ROOM_ID)
    assert room is not None
    assert room.status is RoomStatus.WAITING


@pytest.mark.asyncio
async def test_finished_runtime_retry_only_completes_room_after_room_failure() -> None:
    rooms = FailOnceRoomCompletionAdapter(ManualClock())
    games = CountingGamePersistenceAdapter()
    events = InMemoryRealtimeEventAdapter()
    runner, clock, _, votes, _, _ = await setup_runner(
        rooms=rooms,
        games=games,
        events=events,
    )
    sequence = (
        (BLACK_ID, "A1"),
        (WHITE_ID, "A2"),
        (BLACK_ID, "B1"),
        (WHITE_ID, "B2"),
        (BLACK_ID, "C1"),
        (WHITE_ID, "C2"),
        (BLACK_ID, "D1"),
        (WHITE_ID, "D2"),
        (BLACK_ID, "E1"),
    )

    for turn_no, (participant_id, coordinate) in enumerate(sequence[:-1], 1):
        snapshot = await votes.get(ROOM_ID)
        assert snapshot is not None
        await votes.cast_vote(
            CastRuntimeVote(
                ROOM_ID,
                f"vote-{turn_no}",
                GAME_ID,
                turn_no,
                participant_id,
                coordinate,
                snapshot.state_version,
            )
        )
        clock.advance(5_000)
        assert (await runner.process(DueTurn(ROOM_ID, GAME_ID, turn_no))).status is (
            TurnProcessingStatus.MOVE
        )

    final_turn = len(sequence)
    participant_id, coordinate = sequence[-1]
    snapshot = await votes.get(ROOM_ID)
    assert snapshot is not None
    await votes.cast_vote(
        CastRuntimeVote(
            ROOM_ID,
            f"vote-{final_turn}",
            GAME_ID,
            final_turn,
            participant_id,
            coordinate,
            snapshot.state_version,
        )
    )
    clock.advance(5_000)
    due = DueTurn(ROOM_ID, GAME_ID, final_turn)

    with pytest.raises(RuntimeError, match="simulated Room completion failure"):
        await runner.process(due)

    finished = await votes.get(ROOM_ID)
    assert finished is not None
    assert finished.game_status is GameStatus.FINISHED
    room_before_retry = await rooms.get(ROOM_ID)
    assert room_before_retry is not None
    assert room_before_retry.status is RoomStatus.PLAYING
    stored_result = games.results[GAME_ID]
    assert (games.append_calls, games.finalize_calls, rooms.complete_calls) == (9, 1, 1)
    stream_version_before_retry = events.room_version(ROOM_ID)

    retried = await runner.process(due)

    assert retried.status is TurnProcessingStatus.GAME_ENDED
    assert games.results[GAME_ID] == stored_result
    assert (games.append_calls, games.finalize_calls, rooms.complete_calls) == (9, 1, 2)
    assert events.room_version(ROOM_ID) == stream_version_before_retry + 1
    room = await rooms.get(ROOM_ID)
    assert room is not None
    assert room.status is RoomStatus.WAITING
    assert room.game_id is None
    assert all(not participant.ready for participant in room.participants)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["room_missing", "replayed", "no_snapshot", "event_failure"])
async def test_room_completion_keeps_state_safe_at_recovery_boundaries(
    boundary: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = Mock(spec=RealtimeEventPort)
    events.room_changed = AsyncMock()
    events.lobby_rooms_changed = AsyncMock()
    runner, _, rooms, _, games, _ = await setup_runner(events=events)
    due = DueTurn(ROOM_ID, GAME_ID, 13)
    if boundary == "room_missing":
        monkeypatch.setattr(rooms, "get", AsyncMock(return_value=None))
        with pytest.raises(VoteRuleViolation, match="ROOM_NOT_FOUND"):
            await runner._complete_room(due)
    else:
        if boundary in {"replayed", "no_snapshot"}:
            snapshot = await rooms.get(ROOM_ID)
            monkeypatch.setattr(
                rooms,
                "complete_game",
                AsyncMock(
                    return_value=RoomMutationResult(
                        snapshot=snapshot if boundary == "replayed" else None,
                        replayed=boundary == "replayed",
                    )
                ),
            )
        else:
            events.room_changed.side_effect = RuntimeError("simulated event failure")
            events.lobby_rooms_changed.side_effect = RuntimeError("simulated event failure")
        await runner._complete_room(due)
    if boundary == "event_failure":
        room = await rooms.get(ROOM_ID)
        assert room is not None and room.status is RoomStatus.WAITING
        assert (room.last_game_id, room.last_game_turn_no) == (GAME_ID, 13)
    else:
        events.room_changed.assert_not_called()
        events.lobby_rooms_changed.assert_not_called()
    # Completion notification does not write persistent results or ratings.
    assert games.results == {}


@pytest.mark.asyncio
async def test_turn_resolution_runner_isolates_item_failure_and_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = DueTurn("room-a", "game-a", 1)
    second = DueTurn("room-b", "game-b", 2)

    source = Mock()
    source.due_turns = AsyncMock(return_value=(first, second))

    runner = TurnResolutionRunner(
        due_turns=source,
        finalization_gate=Mock(),
        tie_selector=Mock(),
        tie_audit=Mock(),
        votes=Mock(),
        games=Mock(),
        rooms=Mock(),
        clock=ManualClock(),
        runner_id="runner-isolation",
    )
    process = AsyncMock(
        side_effect=(
            VoteRuleViolation("SIMULATED_ITEM_RULE_VIOLATION"),
            TurnProcessingResult(second, TurnProcessingStatus.STALE),
        )
    )
    log_exception = Mock()
    monkeypatch.setattr(runner, "process", process)
    monkeypatch.setattr(
        "seokpan.game.application.resolution._LOGGER.exception",
        log_exception,
    )

    results = await runner.run_once()

    assert results == (TurnProcessingResult(second, TurnProcessingStatus.STALE),)
    assert process.await_count == 2
    log_exception.assert_called_once_with(
        "Turn resolution item failed",
        extra={
            "event": "turn_resolution.item_failed",
            "room_id": first.room_id,
            "game_id": first.game_id,
            "turn_no": first.turn_no,
        },
    )


@pytest.mark.asyncio
async def test_turn_resolution_runner_propagates_due_source_failure() -> None:
    source = Mock()
    source.due_turns = AsyncMock(side_effect=RuntimeError("turn source unavailable"))

    runner = TurnResolutionRunner(
        due_turns=source,
        finalization_gate=Mock(),
        tie_selector=Mock(),
        tie_audit=Mock(),
        votes=Mock(),
        games=Mock(),
        rooms=Mock(),
        clock=ManualClock(),
        runner_id="runner-source-failure",
    )

    with pytest.raises(RuntimeError, match="turn source unavailable"):
        await runner.run_once()


@pytest.mark.asyncio
async def test_turn_resolution_runner_propagates_item_provider_failure() -> None:
    due = DueTurn("room-a", "game-a", 1)

    source = Mock()
    source.due_turns = AsyncMock(return_value=(due,))

    votes = Mock()
    votes.get = AsyncMock(side_effect=RuntimeError("vote provider unavailable"))

    runner = TurnResolutionRunner(
        due_turns=source,
        finalization_gate=Mock(),
        tie_selector=Mock(),
        tie_audit=Mock(),
        votes=votes,
        games=Mock(),
        rooms=Mock(),
        clock=ManualClock(),
        runner_id="runner-provider-failure",
    )

    with pytest.raises(RuntimeError, match="vote provider unavailable"):
        await runner.run_once()

    votes.get.assert_awaited_once_with(due.room_id)


@pytest.mark.asyncio
async def test_system_invalid_closure_finalizes_without_stats() -> None:
    runner, clock, _rooms, votes, games, _ = await setup_runner()
    clock.advance(1_234)

    finalized = await runner.finalize_system_invalid(
        room_id=ROOM_ID,
        game_id=GAME_ID,
        closed_at_ms=clock.now_ms,
    )

    assert finalized is True
    stored = await games.load_result(GAME_ID)
    assert stored is not None
    assert stored.status is GameStatus.SYSTEM_INVALID
    assert stored.end_reason is EndReason.SYSTEM_INVALID
    assert stored.winner is Stone.EMPTY
    assert stored.rating_adjustments == ()
    assert await votes.get(ROOM_ID) is None


@pytest.mark.asyncio
async def test_system_invalid_closure_retry_is_idempotent() -> None:
    games = CountingGamePersistenceAdapter()
    runner, _clock, _rooms, votes, _, _ = await setup_runner(games=games)

    assert await runner.finalize_system_invalid(
        room_id=ROOM_ID,
        game_id=GAME_ID,
        closed_at_ms=1_000,
    )
    assert await votes.get(ROOM_ID) is None
    assert await runner.finalize_system_invalid(
        room_id=ROOM_ID,
        game_id=GAME_ID,
        closed_at_ms=1_000,
    )
    assert await votes.get(ROOM_ID) is None

    assert games.finalize_calls == 1


@pytest.mark.asyncio
async def test_system_invalid_closure_does_not_overwrite_existing_normal_result() -> None:
    runner, clock, _rooms, _votes, games, _ = await setup_runner()
    clock.advance(5_000)
    assert (await runner.process(DueTurn(ROOM_ID, GAME_ID, 1))).status is TurnProcessingStatus.PASS
    clock.advance(5_000)
    assert (await runner.process(DueTurn(ROOM_ID, GAME_ID, 2))).status is (
        TurnProcessingStatus.GAME_ENDED
    )
    stored_before = await games.load_result(GAME_ID)
    assert stored_before is not None
    assert stored_before.end_reason is EndReason.JOINT_LOSS

    assert (
        await runner.finalize_system_invalid(
            room_id=ROOM_ID,
            game_id=GAME_ID,
            closed_at_ms=clock.now_ms,
        )
        is False
    )
    assert await games.load_result(GAME_ID) == stored_before


@pytest.mark.asyncio
async def test_system_invalid_closure_persists_from_history_when_vote_runtime_is_missing() -> None:
    clock = ManualClock(now_ms=2_000)
    rooms = InMemoryRoomRuntimeAdapter(clock)
    votes = InMemoryVoteRuntimeAdapter(clock)
    games = InMemoryGamePersistenceAdapter({1: 1000, 2: 1000, 3: 1000})
    runner, _, _, _, _, _ = await setup_runner(
        rooms=rooms,
        votes=votes,
        games=games,
    )
    # Simulate a provider-loss boundary where durable Game history remains but Vote runtime is gone.
    votes._states.pop(ROOM_ID, None)

    assert await runner.finalize_system_invalid(
        room_id=ROOM_ID,
        game_id=GAME_ID,
        closed_at_ms=2_000,
    )
    stored = await games.load_result(GAME_ID)
    assert stored is not None
    assert stored.status is GameStatus.SYSTEM_INVALID
    assert stored.end_reason is EndReason.SYSTEM_INVALID
    assert stored.rating_adjustments == ()


@pytest.mark.asyncio
async def test_system_invalid_closure_preserves_board_conclusion_proven_by_durable_moves() -> None:
    runner, _clock, _rooms, votes, games, _ = await setup_runner()
    for move_no, turn_no, coordinate in (
        (1, 1, "A1"),
        (2, 3, "B1"),
        (3, 5, "C1"),
        (4, 7, "D1"),
        (5, 9, "E1"),
    ):
        await games.append_move(
            OfficialMoveRecord(
                game_id=GAME_ID,
                turn_no=turn_no,
                move_no=move_no,
                team=Stone.BLACK,
                coordinate=Coordinate.parse(coordinate),
                final_vote_count=1,
                valid_voter_count=1,
                confirmed_at=datetime.fromtimestamp(turn_no, UTC),
            )
        )

    invalidated = await runner.finalize_system_invalid(
        room_id=ROOM_ID,
        game_id=GAME_ID,
        closed_at_ms=20_000,
    )

    assert invalidated is False
    stored = await games.load_result(GAME_ID)
    assert stored is not None
    assert stored.status is GameStatus.FINISHED
    assert stored.end_reason is EndReason.BLACK_WIN
    assert stored.winner is Stone.BLACK
    assert stored.ended_at == datetime.fromtimestamp(9, UTC)
    assert await votes.get(ROOM_ID) is None


@pytest.mark.asyncio
async def test_invalidation_reconciler_consumes_pending_marker_and_acks_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, clock, rooms, votes, games, _ = await setup_runner()
    pending = PendingGameInvalidation(ROOM_ID, GAME_ID, 4_321)
    monkeypatch.setattr(
        rooms,
        "pending_game_invalidations",
        AsyncMock(return_value=(pending,)),
    )
    acknowledge = AsyncMock()
    monkeypatch.setattr(rooms, "complete_game_invalidation", acknowledge)

    completed = await runner.reconcile_game_invalidations(limit=10)

    assert completed == 1
    stored = await games.load_result(GAME_ID)
    assert stored is not None
    assert stored.status is GameStatus.SYSTEM_INVALID
    assert stored.ended_at == datetime.fromtimestamp(4.321, UTC)
    assert await votes.get(ROOM_ID) is None
    acknowledge.assert_awaited_once_with(ROOM_ID, GAME_ID)
    assert clock.now_ms == 0


@pytest.mark.asyncio
async def test_invalidation_reconciler_logs_stable_failure_once_until_it_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = PendingGameInvalidation("room-missing", "game-missing", 1_000)
    rooms = Mock()
    rooms.pending_game_invalidations = AsyncMock(return_value=(pending,))
    rooms.complete_game_invalidation = AsyncMock()
    games = Mock()
    games.load_game = AsyncMock(return_value=None)
    votes = Mock()
    log = Mock()
    monkeypatch.setattr("seokpan.game.application.resolution._LOGGER.exception", log)
    runner = TurnResolutionRunner(
        due_turns=Mock(),
        finalization_gate=Mock(),
        tie_selector=Mock(),
        tie_audit=Mock(),
        votes=votes,
        games=games,
        rooms=rooms,
        clock=ManualClock(),
        runner_id="invalidation-retry",
    )

    assert await runner.reconcile_game_invalidations() == 0
    assert await runner.reconcile_game_invalidations() == 0

    log.assert_called_once_with(
        "Game invalidation item failed",
        extra={
            "event": "game_invalidation.item_failed",
            "room_id": "room-missing",
            "game_id": "game-missing",
        },
    )
