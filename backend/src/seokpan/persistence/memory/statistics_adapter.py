"""Headless statistics derived from finalized Fake results, never demo numbers."""

from dataclasses import replace

from seokpan.game.application import PersistenceRuleViolation
from seokpan.game.domain import MemberOutcome
from seokpan.persistence.memory.game_adapter import InMemoryGamePersistenceAdapter
from seokpan.persistence.memory.identity_adapter import InMemoryIdentityAdapter
from seokpan.statistics import (
    StatisticsPage,
    StatisticsQuery,
    StatisticsUnavailable,
    statistics_page,
)


class InMemoryStatisticsAdapter:
    def __init__(
        self, members: InMemoryIdentityAdapter, games: InMemoryGamePersistenceAdapter
    ) -> None:
        self._members = members
        self._games = games

    async def read(self, query: StatisticsQuery) -> StatisticsPage:
        # All Fake reads below complete without suspension; no external provider is called.
        members = {row.member_id: row for row in self._members.public_statistics_members()}
        try:
            for game_id in self._games.results:
                result = await self._games.load_result(game_id)
                if result is None:
                    raise StatisticsUnavailable()
                if not result.stats_eligible:
                    continue
                for adjustment in result.rating_adjustments:
                    row = members.get(adjustment.member_id)
                    if row is None:
                        raise StatisticsUnavailable()
                    members[row.member_id] = replace(
                        row,
                        wins=row.wins + int(adjustment.outcome is MemberOutcome.WIN),
                        draws=row.draws + int(adjustment.outcome is MemberOutcome.DRAW),
                        losses=row.losses + int(adjustment.outcome is MemberOutcome.LOSS),
                        games_played=row.games_played + 1,
                    )
        except PersistenceRuleViolation as error:
            raise StatisticsUnavailable() from error
        ordered = sorted(
            (row for row in members.values() if row.games_played), key=lambda row: row.order_key
        )
        for rank, row in enumerate(ordered, 1):
            members[row.member_id] = replace(row, rank=rank)
        return statistics_page(members.values(), query)
