"""Public cumulative Member statistics, without identity credentials or game history."""

from collections.abc import Iterable
from dataclasses import dataclass
from fractions import Fraction
from typing import Protocol


class StatisticsUnavailable(Exception):
    """No partial or invented statistics may be returned on a read failure."""


@dataclass(frozen=True, slots=True)
class MemberStatistics:
    member_id: int
    nickname: str
    rating: int
    wins: int = 0
    draws: int = 0
    losses: int = 0
    games_played: int = 0
    rank: int | None = None

    def __post_init__(self) -> None:
        integers = (
            self.member_id,
            self.rating,
            self.wins,
            self.draws,
            self.losses,
            self.games_played,
        )
        if (
            any(type(value) is not int for value in integers)
            or self.member_id <= 0
            or min(integers[1:]) < 0
            or not self.nickname
            or self.wins + self.draws + self.losses != self.games_played
            or (
                self.rank is not None
                and (type(self.rank) is not int or self.rank <= 0 or self.games_played == 0)
            )
        ):
            raise StatisticsUnavailable()

    @property
    def order_key(self) -> tuple[int, int, Fraction, int, str]:
        # D01 p.31: do not round the win rate before deciding the rank.
        rate = Fraction(self.wins, self.games_played) if self.games_played else Fraction(0)
        return (-self.rating, -self.wins, -rate, -self.games_played, self.nickname)


@dataclass(frozen=True, slots=True)
class StatisticsQuery:
    member_id: int | None = None
    offset: int = 0
    limit: int = 20

    def __post_init__(self) -> None:
        if (
            type(self.offset) is not int
            or not 0 <= self.offset <= 2_147_483_647
            or type(self.limit) is not int
            or not 1 <= self.limit <= 100
            or (
                self.member_id is not None
                and (type(self.member_id) is not int or self.member_id <= 0)
            )
        ):
            raise ValueError("Invalid statistics query")


@dataclass(frozen=True, slots=True)
class StatisticsPage:
    items: tuple[MemberStatistics, ...]
    has_more: bool
    me: MemberStatistics | None


def statistics_page(rows: Iterable[MemberStatistics], query: StatisticsQuery) -> StatisticsPage:
    """Build a page from ranked rows, retaining the requesting Member outside the page."""
    values = tuple(rows)
    me = next((row for row in values if row.member_id == query.member_id), None)
    if query.member_id is not None and me is None:
        raise StatisticsUnavailable()
    visible = sorted(
        (
            row
            for row in values
            if row.rank is not None and query.offset < row.rank <= query.offset + query.limit + 1
        ),
        key=lambda row: row.rank or 0,
    )
    return StatisticsPage(tuple(visible[: query.limit]), len(visible) > query.limit, me)


class StatisticsReader(Protocol):
    async def read(self, query: StatisticsQuery) -> StatisticsPage: ...
