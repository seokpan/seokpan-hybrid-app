from dataclasses import asdict, replace
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import SQLAlchemyError

from seokpan.game.application import FinalizeGameCommand, GameParticipantRecord, StartGameCommand
from seokpan.game.domain import (
    EndReason,
    GameResult,
    GameStatus,
    MemberOutcome,
    RatingAdjustment,
    Stone,
)
from seokpan.identity.application import CreateMember
from seokpan.persistence.mariadb.statistics_adapter import (
    MariaDBStatisticsAdapter,
    _statistics_row,
    statistics_statement,
)
from seokpan.persistence.memory.game_adapter import InMemoryGamePersistenceAdapter
from seokpan.persistence.memory.identity_adapter import InMemoryIdentityAdapter
from seokpan.persistence.memory.statistics_adapter import InMemoryStatisticsAdapter
from seokpan.statistics import (
    MemberStatistics,
    StatisticsQuery,
    StatisticsUnavailable,
    statistics_page,
)


def example_rows() -> tuple[MemberStatistics, ...]:
    return (
        MemberStatistics(1, "가가", 1000, 2, 0, 1, 3),
        MemberStatistics(2, "나나", 1001, 0, 0, 1, 1),
        MemberStatistics(3, "다다", 1000, 3, 0, 7, 10),
        MemberStatistics(4, "라라", 1000, 2, 0, 0, 2),
        MemberStatistics(5, "마마", 1000, 0, 0, 5, 5),
        MemberStatistics(6, "바바", 1000, 0, 0, 4, 4),
        MemberStatistics(7, "사사", 1000, 0, 2, 3, 5),
        MemberStatistics(8, "아아", 3000),  # High rating does not make a zero-game Member eligible.
    )


def test_exact_document_order_including_zero_win_ties() -> None:
    eligible = sorted(
        (row for row in example_rows() if row.games_played), key=lambda r: r.order_key
    )
    assert [row.member_id for row in eligible] == [2, 3, 4, 1, 5, 7, 6]
    # Win-rate rounding must not turn nearby unsigned-32-bit ratios into a tie.
    smaller = MemberStatistics(9, "가나", 1000, 1, 0, 4_294_967_293, 4_294_967_294)
    larger = MemberStatistics(10, "가다", 1000, 1, 0, 4_294_967_294, 4_294_967_295)
    assert smaller.order_key < larger.order_key


@pytest.mark.parametrize(
    "offset,limit,member_id,expected,more,own_rank",
    [
        (0, 2, 6, [2, 3], True, 7),
        (5, 2, 6, [7, 6], False, 7),
        (99, 2, 8, [], False, None),
        (0, 100, None, [2, 3, 4, 1, 5, 7, 6], False, None),
    ],
)
def test_query_executes_with_bounded_projection_and_matches_memory_order(
    offset: int,
    limit: int,
    member_id: int | None,
    expected: list[int],
    more: bool,
    own_rank: int | None,
) -> None:
    # SQLite executes relational logic only. MariaDB precision, plans, grants and TLS are separate.
    engine = create_engine("sqlite://")
    query = StatisticsQuery(member_id, offset, limit)
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE member (member_id INTEGER, nickname TEXT, rating INTEGER)")
        )
        connection.execute(
            text(
                "CREATE TABLE member_stats (member_id INTEGER, wins INTEGER, draws INTEGER, "
                "losses INTEGER, games_played INTEGER)"
            )
        )
        for row in example_rows():
            connection.execute(
                text("INSERT INTO member VALUES (:member_id, :nickname, :rating)"), asdict(row)
            )
            if row.games_played:
                connection.execute(
                    text(
                        "INSERT INTO member_stats VALUES "
                        "(:member_id, :wins, :draws, :losses, :games_played)"
                    ),
                    asdict(row),
                )
        result = tuple(
            MemberStatistics(**dict(row))
            for row in connection.execute(statistics_statement(query)).mappings()
        )
    engine.dispose()
    assert len(result) <= limit + 2
    page = statistics_page(result, query)
    assert [row.member_id for row in page.items] == expected
    assert page.has_more is more
    assert (page.me.rank if page.me else None) == own_rank
    assert (page.me.member_id if page.me else None) == member_id


def test_mariadb_statement_is_one_read_without_private_columns_or_new_tables() -> None:
    statement = statistics_statement(StatisticsQuery(15, 20, 20))
    sql = str(statement.compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "row_number() OVER" in sql
    assert "DECIMAL(65, 30)" in sql
    assert "BETWEEN 21 AND 41" in sql
    assert "stats.member_id = 15" in sql
    assert "stats.games_played > 0" in sql
    assert all(
        word not in sql
        for word in (
            "password_hash",
            "login_id",
            "rating_history",
            "FOR UPDATE",
            "INSERT",
            "UPDATE",
        )
    )
    assert list(statement.selected_columns.keys()) == [
        "member_id",
        "nickname",
        "rating",
        "wins",
        "draws",
        "losses",
        "games_played",
        "rank",
    ]


def test_mariadb_row_normalizes_integral_decimal_counters() -> None:
    row = {
        "member_id": 1,
        "nickname": "가가",
        "rating": 1000,
        "wins": Decimal("2"),
        "draws": Decimal("0"),
        "losses": Decimal("1"),
        "games_played": Decimal("3"),
        "rank": 1,
    }

    assert _statistics_row(row) == MemberStatistics(1, "가가", 1000, 2, 0, 1, 3, 1)


@pytest.mark.parametrize("value", [Decimal("1.5"), Decimal("NaN"), Decimal("Infinity")])
def test_mariadb_row_rejects_non_integral_decimal_counters(value: Decimal) -> None:
    row = {
        "member_id": 1,
        "nickname": "가가",
        "rating": 1000,
        "wins": value,
        "draws": Decimal("0"),
        "losses": Decimal("0"),
        "games_played": Decimal("1"),
        "rank": 1,
    }

    with pytest.raises(StatisticsUnavailable):
        _statistics_row(row)


@pytest.mark.asyncio
async def test_mariadb_adapter_reads_once_and_closes_session() -> None:
    row = MemberStatistics(1, "가가", 1000, 0, 1, 0, 1, 1)
    result = MagicMock()
    result.mappings.return_value = [asdict(row)]
    session = AsyncMock()
    session.execute.return_value = result
    context = AsyncMock()
    context.__aenter__.return_value = session
    factory = MagicMock(return_value=context)
    page = await MariaDBStatisticsAdapter(factory).read(StatisticsQuery(1))
    assert page.items == (row,) and page.me == row and not page.has_more
    session.execute.assert_awaited_once()
    session.commit.assert_not_awaited()
    context.__aexit__.assert_awaited_once()


@pytest.mark.asyncio
async def test_mariadb_failure_is_not_an_empty_ranking_or_secret_message() -> None:
    context = AsyncMock()
    context.__aenter__.side_effect = SQLAlchemyError("private connection detail")
    with pytest.raises(StatisticsUnavailable) as error:
        await MariaDBStatisticsAdapter(lambda: context).read(StatisticsQuery())
    assert str(error.value) == ""


@pytest.mark.parametrize(
    "values",
    [
        {"wins": -1},
        {"wins": 1},
        {"rating": -1},
        {"rank": 1},
        {"member_id": True},
    ],
)
def test_invalid_cumulative_data_is_not_published(values: dict[str, object]) -> None:
    with pytest.raises(StatisticsUnavailable):
        replace(MemberStatistics(1, "가가", 1000), **values)


@pytest.mark.parametrize(
    "query",
    [
        {"offset": -1},
        {"offset": True},
        {"limit": 0},
        {"limit": 101},
        {"member_id": 0},
    ],
)
def test_query_bounds_are_checked_outside_http_too(query: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        StatisticsQuery(**query)


@pytest.mark.asyncio
async def test_memory_finalizations_include_draw_forfeit_joint_loss_but_not_invalid_or_guest() -> (
    None
):
    ratings: dict[int, int] = {}
    members = InMemoryIdentityAdapter(member_ratings=ratings)
    for number in range(3):
        await members.create(CreateMember(f"member_{number}", f"회원{number}", "unused-hash", 1000))
    games = InMemoryGamePersistenceAdapter(ratings)
    reader = InMemoryStatisticsAdapter(members, games)
    first = await reader.read(StatisticsQuery(1))
    assert first.items == () and first.me == MemberStatistics(1, "회원0", 1000)
    for end, winner, eligible in (
        (EndReason.BLACK_WIN, Stone.BLACK, True),
        (EndReason.DRAW, Stone.EMPTY, True),
        (EndReason.FORFEIT, Stone.WHITE, True),
        (EndReason.JOINT_LOSS, Stone.EMPTY, True),
        (EndReason.SYSTEM_INVALID, Stone.EMPTY, False),
    ):
        participants = (
            GameParticipantRecord(str(uuid4()), Stone.BLACK, member_id=1),
            GameParticipantRecord(str(uuid4()), Stone.WHITE, member_id=2),
            GameParticipantRecord(str(uuid4()), Stone.BLACK, guest_label="Guest-1234"),
        )
        start = StartGameCommand(str(uuid4()), str(uuid4()), 15, datetime.now(UTC), participants)
        await games.start_game(start)
        adjustments = tuple(
            RatingAdjustment(
                participant.participant_id,
                participant.member_id,
                participant.team,
                MemberOutcome.DRAW
                if end is EndReason.DRAW
                else (MemberOutcome.WIN if participant.team is winner else MemberOutcome.LOSS),
                ratings[participant.member_id],
                0,
                ratings[participant.member_id],
            )
            for participant in participants
            if participant.member_id is not None and eligible
        )
        final = FinalizeGameCommand(
            GameResult(
                start.game_id,
                GameStatus.FINISHED if eligible else GameStatus.SYSTEM_INVALID,
                end,
                winner,
                (),
                eligible,
                adjustments,
            ),
            datetime.now(UTC),
        )
        await games.finalize_game(final)
        await games.finalize_game(final)  # Replay must never increase cumulative counts.
    page = await reader.read(StatisticsQuery(3))
    assert len(page.items) == 2
    assert all((r.wins, r.draws, r.losses, r.games_played) == (1, 1, 2, 4) for r in page.items)
    assert page.me == MemberStatistics(3, "회원2", 1000)
    before = dict(games.results)
    assert await reader.read(StatisticsQuery(3)) == page
    assert games.results == before  # Reads never repair or create stored records.
    with pytest.raises(StatisticsUnavailable):
        await reader.read(StatisticsQuery(999))


@pytest.mark.asyncio
async def test_memory_incomplete_result_is_rejected_not_partially_counted() -> None:
    ratings = {1: 1000}
    members = InMemoryIdentityAdapter(member_ratings=ratings)
    await members.create(CreateMember("member_1", "회원1", "unused-hash", 1000))
    games = InMemoryGamePersistenceAdapter(ratings)
    participant = GameParticipantRecord(str(uuid4()), Stone.BLACK, member_id=1)
    start = StartGameCommand(str(uuid4()), str(uuid4()), 15, datetime.now(UTC), (participant,))
    await games.start_game(start)
    await games.finalize_game(
        FinalizeGameCommand(
            GameResult(
                start.game_id,
                GameStatus.FINISHED,
                EndReason.BLACK_WIN,
                Stone.BLACK,
                (),
                True,
                (),
            ),
            datetime.now(UTC),
        )
    )
    with pytest.raises(StatisticsUnavailable):
        await InMemoryStatisticsAdapter(members, games).read(StatisticsQuery())
