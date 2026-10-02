from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.exc import SQLAlchemyError

from seokpan.game.application import (
    FinalizeGameCommand,
    GameParticipantRecord,
    OfficialMoveRecord,
    PersistenceOutcome,
    PersistenceRuleViolation,
    StartGameCommand,
    StoredGameResult,
)
from seokpan.game.domain import (
    Coordinate,
    EndReason,
    GameResult,
    GameStatus,
    MemberOutcome,
    RatingAdjustment,
    Stone,
)
from seokpan.persistence.mariadb.game_adapter import MariaDBGamePersistenceAdapter
from seokpan.persistence.mariadb.models import (
    GameParticipantRow,
    GameResultRow,
    GameRow,
    MemberRow,
    MemberStatsRow,
    MoveRow,
    RatingHistoryRow,
)

GAME_ID = "00000000-0000-4000-8000-000000000001"
ROOM_ID = "00000000-0000-4000-8000-000000000002"
BLACK_ID = "00000000-0000-4000-8000-000000000003"
WHITE_ID = "00000000-0000-4000-8000-000000000004"
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
DB_NOW = NOW.replace(tzinfo=None)


class ResultBag:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows

    def scalar_one_or_none(self) -> object | None:
        if len(self.rows) > 1:
            raise AssertionError("expected at most one row")
        return self.rows[0] if self.rows else None

    def scalars(self) -> ResultBag:
        return self

    def all(self) -> list[object]:
        return self.rows


class FakeSession:
    def __init__(
        self,
        *,
        rows: dict[tuple[type[object], object], object] | None = None,
        execute_results: list[list[object]] | None = None,
        fail_commit: bool = False,
    ) -> None:
        self.rows = rows or {}
        self.execute_results = list(execute_results or [])
        self.fail_commit = fail_commit
        self.added: list[object] = []
        self.begin_count = 0
        self.commit_count = 0
        self.rollback_count = 0
        self.flush_count = 0

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def begin(self) -> None:
        self.begin_count += 1

    async def commit(self) -> None:
        self.commit_count += 1
        if self.fail_commit:
            raise SQLAlchemyError("commit outcome unavailable")

    async def flush(self) -> None:
        self.flush_count += 1

    async def rollback(self) -> None:
        self.rollback_count += 1

    async def get(
        self,
        entity: type[object],
        key: object,
        **_kwargs: object,
    ) -> object | None:
        return self.rows.get((entity, key))

    async def execute(self, _statement: object) -> ResultBag:
        return ResultBag(self.execute_results.pop(0))

    def add(self, row: object) -> None:
        self.added.append(row)

    def add_all(self, rows: list[object]) -> None:
        self.added.extend(rows)


class SessionFactory:
    def __init__(self, *sessions: FakeSession) -> None:
        self.sessions = list(sessions)

    def __call__(self) -> Any:
        return self.sessions.pop(0)


def participants() -> tuple[GameParticipantRecord, ...]:
    return (
        GameParticipantRecord(
            participant_id=BLACK_ID,
            team=Stone.BLACK,
            member_id=1,
        ),
        GameParticipantRecord(
            participant_id=WHITE_ID,
            team=Stone.WHITE,
            guest_label="Guest-0001",
        ),
    )


def start_command() -> StartGameCommand:
    return StartGameCommand(
        game_id=GAME_ID,
        room_id=ROOM_ID,
        voting_time_seconds=10,
        started_at=NOW,
        participants=participants(),
    )


def move_command() -> OfficialMoveRecord:
    return OfficialMoveRecord(
        game_id=GAME_ID,
        turn_no=1,
        move_no=1,
        team=Stone.BLACK,
        coordinate=Coordinate.parse("A15"),
        final_vote_count=2,
        valid_voter_count=3,
        confirmed_at=NOW,
    )


def completed_result(*, system_invalid: bool = False) -> FinalizeGameCommand:
    if system_invalid:
        result = GameResult(
            game_id=GAME_ID,
            status=GameStatus.SYSTEM_INVALID,
            end_reason=EndReason.SYSTEM_INVALID,
            winner=Stone.EMPTY,
            winning_line=(),
            stats_eligible=False,
            rating_adjustments=(),
        )
    else:
        result = GameResult(
            game_id=GAME_ID,
            status=GameStatus.FINISHED,
            end_reason=EndReason.BLACK_WIN,
            winner=Stone.BLACK,
            winning_line=(),
            stats_eligible=True,
            rating_adjustments=(
                RatingAdjustment(
                    participant_id=BLACK_ID,
                    member_id=1,
                    team=Stone.BLACK,
                    outcome=MemberOutcome.WIN,
                    rating_before=1000,
                    rating_delta=16,
                    rating_after=1016,
                ),
                RatingAdjustment(
                    participant_id=WHITE_ID,
                    member_id=2,
                    team=Stone.WHITE,
                    outcome=MemberOutcome.LOSS,
                    rating_before=1000,
                    rating_delta=-16,
                    rating_after=984,
                ),
            ),
        )
    return FinalizeGameCommand(result=result, ended_at=NOW)


def game_row() -> GameRow:
    return GameRow(
        game_id=GAME_ID,
        room_id=ROOM_ID,
        voting_time_seconds=10,
        status="IN_PROGRESS",
        started_at=NOW,
        ended_at=None,
    )


@pytest.mark.asyncio
async def test_load_game_returns_start_participants_ratings_and_ordered_moves() -> None:
    black = MemberRow(
        member_id=1, login_id="black", nickname="Black", password_hash="x", rating=1000
    )
    participant_rows = [
        GameParticipantRow(
            game_id=GAME_ID,
            participant_id=BLACK_ID,
            team="BLACK",
            member_id=1,
            is_guest=False,
            guest_label=None,
        ),
        GameParticipantRow(
            game_id=GAME_ID,
            participant_id=WHITE_ID,
            team="WHITE",
            member_id=None,
            is_guest=True,
            guest_label="Guest-0001",
        ),
    ]
    move = MoveRow(
        game_id=GAME_ID,
        turn_no=1,
        move_no=1,
        team="BLACK",
        pos_x=0,
        pos_y=14,
        final_vote_count=2,
        valid_voter_count=3,
        confirmed_at=NOW,
    )
    session = FakeSession(
        rows={(GameRow, GAME_ID): game_row()},
        execute_results=[participant_rows, [move], [], [black]],
    )
    adapter = MariaDBGamePersistenceAdapter(SessionFactory(session))

    snapshot = await adapter.load_game(GAME_ID)

    assert snapshot is not None
    assert snapshot.start == start_command()
    assert snapshot.participants[0].rating == 1000
    assert snapshot.participants[1].rating is None
    assert snapshot.moves == (move_command(),)


@pytest.mark.asyncio
async def test_get_move_returns_recoverable_official_record() -> None:
    row = MoveRow(
        game_id=GAME_ID,
        turn_no=1,
        move_no=1,
        team="BLACK",
        pos_x=0,
        pos_y=14,
        final_vote_count=2,
        valid_voter_count=3,
        confirmed_at=NOW,
    )
    adapter = MariaDBGamePersistenceAdapter(
        SessionFactory(FakeSession(rows={(MoveRow, (GAME_ID, 1)): row}))
    )

    assert await adapter.get_move(GAME_ID, 1) == move_command()


@pytest.mark.parametrize(
    ("build", "code"),
    [
        (lambda: StartGameCommand("bad", ROOM_ID, 10, NOW, participants()), "INVALID_GAME_ID"),
        (
            lambda: GameParticipantRecord("bad", Stone.BLACK, member_id=1),
            "INVALID_PARTICIPANT_ID",
        ),
        (
            lambda: OfficialMoveRecord(
                GAME_ID,
                1,
                1,
                Stone.BLACK,
                Coordinate.parse("A1"),
                2,
                1,
                NOW,
            ),
            "INVALID_VOTE_COUNT",
        ),
    ],
)
def test_commands_reject_invalid_provider_boundary_input(build: Any, code: str) -> None:
    with pytest.raises(PersistenceRuleViolation, match=code) as error:
        build()
    assert error.value.code == code


@pytest.mark.asyncio
async def test_start_game_writes_game_and_member_guest_snapshots_in_one_commit() -> None:
    session = FakeSession()
    adapter = MariaDBGamePersistenceAdapter(SessionFactory(session))

    outcome = await adapter.start_game(start_command())

    assert outcome is PersistenceOutcome.CREATED
    assert (session.begin_count, session.commit_count, session.rollback_count) == (1, 1, 0)
    game = next(row for row in session.added if isinstance(row, GameRow))
    snapshots = [row for row in session.added if isinstance(row, GameParticipantRow)]
    assert (game.game_id, game.room_id, game.status) == (GAME_ID, ROOM_ID, "IN_PROGRESS")
    assert [
        (row.participant_id, row.member_id, row.is_guest, row.guest_label) for row in snapshots
    ] == [
        (BLACK_ID, 1, False, None),
        (WHITE_ID, None, True, "Guest-0001"),
    ]


# 작성자: 이유빈
# 작성일자: 2026-09-16
# 작성 내용: F16 Game Start parent 선행 flush 회귀 검증
@pytest.mark.asyncio
async def test_start_game_flushes_parent_before_participant_rows() -> None:
    class OrderingSession(FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self.flush_snapshots: list[tuple[type[object], ...]] = []

        async def flush(self) -> None:
            self.flush_snapshots.append(tuple(type(row) for row in self.added))
            await super().flush()

    session = OrderingSession()
    adapter = MariaDBGamePersistenceAdapter(SessionFactory(session))

    outcome = await adapter.start_game(start_command())

    assert outcome is PersistenceOutcome.CREATED
    assert session.flush_snapshots
    first_flush = session.flush_snapshots[0]
    assert GameRow in first_flush
    assert GameParticipantRow not in first_flush


# 작성자: 이유빈
# 작성일자: 2026-09-16
# 작성 내용: F16 parent flush 이후 participant flush 실패 시 rollback 회귀 검증
@pytest.mark.asyncio
async def test_start_game_rolls_back_when_participant_flush_fails_after_parent_flush() -> None:
    class SecondFlushFailsSession(FakeSession):
        async def flush(self) -> None:
            await super().flush()
            if self.flush_count == 2:
                raise SQLAlchemyError("participant flush failed")

    writing = SecondFlushFailsSession()
    verifying = FakeSession()
    adapter = MariaDBGamePersistenceAdapter(SessionFactory(writing, verifying))

    with pytest.raises(
        PersistenceRuleViolation,
        match="PERSISTENCE_COMMIT_UNCERTAIN",
    ):
        await adapter.start_game(start_command())

    assert writing.flush_count == 2
    assert writing.commit_count == 0
    assert writing.rollback_count == 1


@pytest.mark.asyncio
async def test_existing_identical_game_start_is_idempotent_but_changed_start_conflicts() -> None:
    command = start_command()
    snapshots = MariaDBGamePersistenceAdapter._participant_rows(GAME_ID, participants())
    same = FakeSession(
        rows={(GameRow, GAME_ID): game_row()},
        execute_results=[snapshots],
    )
    outcome = await MariaDBGamePersistenceAdapter(SessionFactory(same)).start_game(command)
    assert outcome is PersistenceOutcome.UNCHANGED

    changed_row = game_row()
    changed_row.voting_time_seconds = 5
    conflict = FakeSession(
        rows={(GameRow, GAME_ID): changed_row},
        execute_results=[snapshots],
    )
    with pytest.raises(PersistenceRuleViolation, match="GAME_START_CONFLICT"):
        await MariaDBGamePersistenceAdapter(SessionFactory(conflict)).start_game(command)
    assert conflict.rollback_count == 1


@pytest.mark.asyncio
async def test_existing_start_treats_naive_mariadb_datetime_as_utc_for_idempotency() -> None:
    command = start_command()
    persisted = game_row()
    persisted.started_at = DB_NOW
    snapshots = MariaDBGamePersistenceAdapter._participant_rows(GAME_ID, participants())
    session = FakeSession(
        rows={(GameRow, GAME_ID): persisted},
        execute_results=[snapshots],
    )

    outcome = await MariaDBGamePersistenceAdapter(SessionFactory(session)).start_game(command)

    assert outcome is PersistenceOutcome.UNCHANGED


@pytest.mark.asyncio
async def test_existing_move_treats_naive_mariadb_datetime_as_utc_for_idempotency() -> None:
    command = move_command()
    existing = MariaDBGamePersistenceAdapter._move_row(command)
    existing.confirmed_at = DB_NOW
    session = FakeSession(
        rows={(MoveRow, (GAME_ID, 1)): existing},
        execute_results=[[existing]],
    )

    outcome = await MariaDBGamePersistenceAdapter(SessionFactory(session)).append_move(command)

    assert outcome is PersistenceOutcome.UNCHANGED


@pytest.mark.asyncio
async def test_load_game_restores_mariadb_datetime_as_utc_application_time() -> None:
    persisted = game_row()
    persisted.started_at = DB_NOW
    black = MemberRow(
        member_id=1, login_id="black", nickname="Black", password_hash="x", rating=1000
    )
    participant_rows = MariaDBGamePersistenceAdapter._participant_rows(GAME_ID, participants())
    session = FakeSession(
        rows={(GameRow, GAME_ID): persisted},
        execute_results=[participant_rows, [], [], [black]],
    )

    snapshot = await MariaDBGamePersistenceAdapter(SessionFactory(session)).load_game(GAME_ID)

    assert snapshot is not None
    assert snapshot.start.started_at == NOW
    assert snapshot.start.started_at.tzinfo is UTC


@pytest.mark.asyncio
async def test_append_move_maps_canonical_coordinate_to_schema_zero_based_axes() -> None:
    session = FakeSession(execute_results=[[]])
    adapter = MariaDBGamePersistenceAdapter(SessionFactory(session))

    outcome = await adapter.append_move(move_command())

    assert outcome is PersistenceOutcome.CREATED
    row = next(row for row in session.added if isinstance(row, MoveRow))
    assert (row.pos_x, row.pos_y) == (0, 14)
    assert (row.turn_no, row.move_no, row.final_vote_count, row.valid_voter_count) == (1, 1, 2, 3)


@pytest.mark.asyncio
async def test_same_move_is_idempotent_and_conflicting_sequence_is_rejected() -> None:
    command = move_command()
    existing = MariaDBGamePersistenceAdapter._move_row(command)
    same = FakeSession(
        rows={(MoveRow, (GAME_ID, 1)): existing},
        execute_results=[[existing]],
    )
    assert await MariaDBGamePersistenceAdapter(SessionFactory(same)).append_move(command) is (
        PersistenceOutcome.UNCHANGED
    )

    conflicting = FakeSession(
        rows={(MoveRow, (GAME_ID, 1)): existing},
        execute_results=[[existing]],
    )
    changed = OfficialMoveRecord(
        game_id=GAME_ID,
        turn_no=1,
        move_no=2,
        team=Stone.BLACK,
        coordinate=Coordinate.parse("B1"),
        final_vote_count=1,
        valid_voter_count=1,
        confirmed_at=NOW,
    )
    with pytest.raises(PersistenceRuleViolation, match="MOVE_SEQUENCE_CONFLICT"):
        await MariaDBGamePersistenceAdapter(SessionFactory(conflicting)).append_move(changed)
    assert conflicting.rollback_count == 1


@pytest.mark.asyncio
async def test_finalize_updates_result_stats_rating_and_history_atomically() -> None:
    game = game_row()
    black = MemberRow(
        member_id=1, login_id="black", nickname="Black", password_hash="x", rating=1000
    )
    white = MemberRow(
        member_id=2, login_id="white", nickname="White", password_hash="x", rating=1000
    )
    black_stats = MemberStatsRow(member_id=1, wins=2, draws=0, losses=1, games_played=3)
    session = FakeSession(
        rows={
            (GameRow, GAME_ID): game,
            (MemberStatsRow, 1): black_stats,
        },
        execute_results=[[black, white]],
    )
    adapter = MariaDBGamePersistenceAdapter(SessionFactory(session))

    outcome = await adapter.finalize_game(completed_result())

    assert outcome is PersistenceOutcome.CREATED
    result = next(row for row in session.added if isinstance(row, GameResultRow))
    histories = [row for row in session.added if isinstance(row, RatingHistoryRow)]
    created_stats = next(
        row for row in session.added if isinstance(row, MemberStatsRow) and row.member_id == 2
    )
    assert (game.status, game.ended_at) == ("COMPLETED", DB_NOW)
    assert (result.winner, result.end_reason, result.reflected_to_stats) == (
        "BLACK",
        "NORMAL_WIN",
        True,
    )
    assert (black.rating, white.rating) == (1016, 984)
    assert (black_stats.wins, black_stats.games_played) == (3, 4)
    assert (created_stats.losses, created_stats.games_played) == (1, 1)
    assert [(row.member_id, row.rating_delta) for row in histories] == [(1, 16), (2, -16)]
    assert session.commit_count == 1


@pytest.mark.asyncio
async def test_system_invalid_finalizes_without_personal_updates() -> None:
    game = game_row()
    session = FakeSession(rows={(GameRow, GAME_ID): game})
    adapter = MariaDBGamePersistenceAdapter(SessionFactory(session))

    outcome = await adapter.finalize_game(completed_result(system_invalid=True))

    result = next(row for row in session.added if isinstance(row, GameResultRow))
    assert outcome is PersistenceOutcome.CREATED
    assert (game.status, result.winner, result.end_reason) == (
        "SYSTEM_INVALID",
        "NONE",
        "SYSTEM_INVALID",
    )
    assert not any(isinstance(row, (MemberStatsRow, RatingHistoryRow)) for row in session.added)


@pytest.mark.asyncio
async def test_existing_result_requires_matching_history_before_idempotent_success() -> None:
    command = completed_result()
    persisted = MariaDBGamePersistenceAdapter._result_row(command)
    persisted.reflected_to_stats = True
    histories = [
        RatingHistoryRow(
            member_id=item.member_id,
            game_id=GAME_ID,
            rating_before=item.rating_before,
            rating_after=item.rating_after,
            rating_delta=item.rating_delta,
        )
        for item in command.result.rating_adjustments
    ]
    same = FakeSession(
        rows={(GameRow, GAME_ID): game_row(), (GameResultRow, GAME_ID): persisted},
        execute_results=[histories],
    )
    outcome = await MariaDBGamePersistenceAdapter(SessionFactory(same)).finalize_game(command)
    assert outcome is PersistenceOutcome.UNCHANGED

    incomplete = FakeSession(
        rows={(GameRow, GAME_ID): game_row(), (GameResultRow, GAME_ID): persisted},
        execute_results=[histories[:1]],
    )
    with pytest.raises(PersistenceRuleViolation, match="GAME_RESULT_CONFLICT"):
        await MariaDBGamePersistenceAdapter(SessionFactory(incomplete)).finalize_game(command)
    assert incomplete.rollback_count == 1


@pytest.mark.asyncio
async def test_existing_result_treats_naive_mariadb_datetime_as_utc_for_idempotency() -> None:
    command = completed_result(system_invalid=True)
    game = game_row()
    game.status = "SYSTEM_INVALID"
    game.ended_at = DB_NOW
    result = MariaDBGamePersistenceAdapter._result_row(command)
    result.ended_at = DB_NOW
    result.reflected_to_stats = True
    session = FakeSession(
        rows={(GameRow, GAME_ID): game, (GameResultRow, GAME_ID): result},
        execute_results=[[]],
    )

    outcome = await MariaDBGamePersistenceAdapter(SessionFactory(session)).finalize_game(command)

    assert outcome is PersistenceOutcome.UNCHANGED


@pytest.mark.asyncio
async def test_game_is_finalized_requires_completed_game_and_reflected_result() -> None:
    game = game_row()
    game.status = "COMPLETED"
    game.ended_at = NOW
    result = MariaDBGamePersistenceAdapter._result_row(completed_result())
    result.reflected_to_stats = True
    complete = FakeSession(rows={(GameRow, GAME_ID): game, (GameResultRow, GAME_ID): result})
    assert await MariaDBGamePersistenceAdapter(SessionFactory(complete)).game_is_finalized(GAME_ID)

    result.reflected_to_stats = False
    incomplete = FakeSession(rows={(GameRow, GAME_ID): game, (GameResultRow, GAME_ID): result})
    assert not await MariaDBGamePersistenceAdapter(SessionFactory(incomplete)).game_is_finalized(
        GAME_ID
    )


@pytest.mark.asyncio
async def test_finalize_rejects_missing_member_before_personal_updates() -> None:
    black = MemberRow(
        member_id=1, login_id="black", nickname="Black", password_hash="x", rating=1000
    )
    session = FakeSession(
        rows={(GameRow, GAME_ID): game_row()},
        execute_results=[[black]],
    )

    with pytest.raises(PersistenceRuleViolation, match="MEMBER_NOT_FOUND"):
        await MariaDBGamePersistenceAdapter(SessionFactory(session)).finalize_game(
            completed_result()
        )
    assert session.rollback_count == 1


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (EndReason.BLACK_WIN, "NORMAL_WIN"),
        (EndReason.WHITE_WIN, "NORMAL_WIN"),
        (EndReason.JOINT_LOSS, "MUTUAL_FORFEIT"),
        (EndReason.FORFEIT, "FORFEIT"),
        (EndReason.DRAW, "DRAW"),
    ],
)
def test_end_reason_mapping_preserves_the_adopted_schema(reason: EndReason, expected: str) -> None:
    assert MariaDBGamePersistenceAdapter._end_reason(reason) == expected


@pytest.mark.asyncio
async def test_stale_member_rating_rolls_back_entire_result_transaction() -> None:
    game = game_row()
    stale = MemberRow(
        member_id=1, login_id="black", nickname="Black", password_hash="x", rating=999
    )
    white = MemberRow(
        member_id=2, login_id="white", nickname="White", password_hash="x", rating=1000
    )
    session = FakeSession(
        rows={(GameRow, GAME_ID): game},
        execute_results=[[stale, white]],
    )

    with pytest.raises(PersistenceRuleViolation, match="STALE_MEMBER_RATING"):
        await MariaDBGamePersistenceAdapter(SessionFactory(session)).finalize_game(
            completed_result()
        )
    assert (session.commit_count, session.rollback_count) == (0, 1)


@pytest.mark.asyncio
async def test_uncertain_commit_converges_only_when_result_is_visible_as_complete() -> None:
    command = completed_result(system_invalid=True)
    writing = FakeSession(
        rows={(GameRow, GAME_ID): game_row()},
        fail_commit=True,
    )
    persisted = GameResultRow(
        game_id=GAME_ID,
        winner="NONE",
        end_reason="SYSTEM_INVALID",
        reflected_to_stats=True,
        ended_at=NOW,
    )
    verifying = FakeSession(
        rows={(GameResultRow, GAME_ID): persisted},
        execute_results=[[]],
    )
    adapter = MariaDBGamePersistenceAdapter(SessionFactory(writing, verifying))

    outcome = await adapter.finalize_game(command)

    assert outcome is PersistenceOutcome.UNCHANGED
    assert writing.rollback_count == 1


@pytest.mark.asyncio
async def test_uncertain_commit_without_matching_row_returns_stable_error() -> None:
    writing = FakeSession(execute_results=[[]], fail_commit=True)
    verifying = FakeSession(execute_results=[[]])
    adapter = MariaDBGamePersistenceAdapter(SessionFactory(writing, verifying))

    with pytest.raises(PersistenceRuleViolation, match="PERSISTENCE_COMMIT_UNCERTAIN") as error:
        await adapter.append_move(move_command())
    assert error.value.code == "PERSISTENCE_COMMIT_UNCERTAIN"


def stored_result_session(command: FinalizeGameCommand) -> FakeSession:
    game = game_row()
    game.status = (
        "SYSTEM_INVALID" if command.result.status is GameStatus.SYSTEM_INVALID else "COMPLETED"
    )
    game.ended_at = command.ended_at
    participants = [
        GameParticipantRow(
            game_id=GAME_ID,
            participant_id=participant_id,
            team=team,
            member_id=member_id,
            is_guest=False,
            guest_label=None,
        )
        for participant_id, team, member_id in [(BLACK_ID, "BLACK", 1), (WHITE_ID, "WHITE", 2)]
    ]
    histories = [
        RatingHistoryRow(
            game_id=GAME_ID,
            member_id=item.member_id,
            rating_before=item.rating_before,
            rating_delta=item.rating_delta,
            rating_after=item.rating_after,
            recorded_at=command.ended_at,
        )
        for item in reversed(command.result.rating_adjustments)
    ]
    return FakeSession(
        rows={
            (GameRow, GAME_ID): game,
            (GameResultRow, GAME_ID): MariaDBGamePersistenceAdapter._result_row(command),
        },
        execute_results=[participants, histories],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "winner"),
    [
        (EndReason.BLACK_WIN, Stone.BLACK),
        (EndReason.WHITE_WIN, Stone.WHITE),
        (EndReason.DRAW, Stone.EMPTY),
        (EndReason.FORFEIT, Stone.BLACK),
        (EndReason.FORFEIT, Stone.WHITE),
        (EndReason.JOINT_LOSS, Stone.EMPTY),
        (EndReason.SYSTEM_INVALID, Stone.EMPTY),
    ],
)
async def test_load_result_returns_only_completed_game_history(
    reason: EndReason,
    winner: Stone,
) -> None:
    original = completed_result(system_invalid=reason is EndReason.SYSTEM_INVALID)
    ratings = tuple(
        replace(
            item,
            outcome=MemberOutcome.DRAW
            if reason is EndReason.DRAW
            else (MemberOutcome.WIN if item.team is winner else MemberOutcome.LOSS),
        )
        for item in original.result.rating_adjustments
    )
    command = replace(
        original,
        result=replace(
            original.result,
            end_reason=reason,
            winner=winner,
            rating_adjustments=ratings,
        ),
    )
    session = stored_result_session(command)
    result_row = session.rows[(GameResultRow, GAME_ID)]
    assert isinstance(result_row, GameResultRow)
    result_row.reflected_to_stats = True
    result = await MariaDBGamePersistenceAdapter(SessionFactory(session)).load_result(GAME_ID)
    assert result == StoredGameResult(
        GAME_ID,
        ROOM_ID,
        command.result.status,
        reason,
        winner,
        NOW,
        ratings,
    )
    assert result.stats_eligible is (reason is not EndReason.SYSTEM_INVALID)
    assert session.execute_results == []
    assert session.added == []
    assert session.commit_count == session.flush_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_game", [False, True])
async def test_load_result_missing_or_active_game_returns_none(existing_game: bool) -> None:
    session = FakeSession(rows={(GameRow, GAME_ID): game_row()} if existing_game else {})
    assert await MariaDBGamePersistenceAdapter(SessionFactory(session)).load_result(GAME_ID) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "broken",
    [
        "orphan",
        "room_id",
        "missing_result",
        "not_reflected",
        "active",
        "ended_at",
        "time_mismatch",
        "conclusion",
        "missing_rating",
        "extra_rating",
        "duplicate_rating",
        "duplicate_member",
        "participant_id",
        "team",
        "invalid_rating",
        "invalid_stats",
    ],
)
async def test_load_result_rejects_incomplete_records(broken: str) -> None:
    session = stored_result_session(completed_result())
    game = session.rows[(GameRow, GAME_ID)]
    result = session.rows[(GameResultRow, GAME_ID)]
    assert isinstance(game, GameRow)
    assert isinstance(result, GameResultRow)
    result.reflected_to_stats = True
    participants, histories = session.execute_results
    if broken == "orphan":
        del session.rows[(GameRow, GAME_ID)]
    elif broken == "missing_result":
        del session.rows[(GameResultRow, GAME_ID)]
    elif broken == "room_id":
        game.room_id = None
    elif broken == "not_reflected":
        result.reflected_to_stats = False
    elif broken == "active":
        game.status = "IN_PROGRESS"
    elif broken == "ended_at":
        game.ended_at = None
    elif broken == "time_mismatch":
        result.ended_at = NOW.replace(second=1)
    elif broken == "conclusion":
        result.winner = "NONE"
    elif broken == "missing_rating":
        histories.pop()
    elif broken == "extra_rating":
        histories.append(RatingHistoryRow(member_id=3))
    elif broken == "duplicate_rating":
        histories.append(histories[0])
    elif broken == "duplicate_member":
        participants.append(participants[0])
    elif broken in {"participant_id", "team"}:
        setattr(participants[0], broken, None if broken == "participant_id" else "EMPTY")
    elif broken == "invalid_stats":
        game.status = "SYSTEM_INVALID"
        result.end_reason = "SYSTEM_INVALID"
        result.winner = "NONE"
    else:
        history = histories[0]
        assert isinstance(history, RatingHistoryRow)
        history.rating_after = -1
    with pytest.raises(PersistenceRuleViolation, match="GAME_RESULT_INCOMPLETE"):
        await MariaDBGamePersistenceAdapter(SessionFactory(session)).load_result(GAME_ID)
    assert session.added == []
    assert session.commit_count == 0


@pytest.mark.asyncio
async def test_load_result_guest_has_no_rating_history() -> None:
    command = completed_result()
    command = replace(
        command,
        result=replace(
            command.result,
            rating_adjustments=command.result.rating_adjustments[:1],
        ),
    )
    session = stored_result_session(command)
    result = session.rows[(GameResultRow, GAME_ID)]
    assert isinstance(result, GameResultRow)
    result.reflected_to_stats = True
    white = session.execute_results[0][1]
    assert isinstance(white, GameParticipantRow)
    white.member_id, white.is_guest, white.guest_label = None, True, "Guest-0001"
    stored = await MariaDBGamePersistenceAdapter(SessionFactory(session)).load_result(GAME_ID)
    assert stored is not None
    assert tuple(item.member_id for item in stored.rating_adjustments) == (1,)
