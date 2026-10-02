from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from seokpan.game.application import FinalizeGameCommand
from seokpan.game.domain import (
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
)

GAME_ID = "00000000-0000-4000-8000-000000000101"
ROOM_ID = "00000000-0000-4000-8000-000000000102"
PARTICIPANT_ID = "00000000-0000-4000-8000-000000000103"
NOW = datetime(2026, 9, 18, 1, 30, tzinfo=UTC)


class ResultBag:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows

    def scalars(self) -> ResultBag:
        return self

    def all(self) -> list[object]:
        return self.rows


class LoadGameSession:
    def __init__(self) -> None:
        self.statements: list[object] = []
        self.game = GameRow(
            game_id=GAME_ID,
            room_id=ROOM_ID,
            voting_time_seconds=10,
            status="IN_PROGRESS",
            started_at=NOW,
            ended_at=None,
        )
        self.participant = GameParticipantRow(
            game_id=GAME_ID,
            participant_id=PARTICIPANT_ID,
            team="BLACK",
            member_id=1,
            is_guest=False,
            guest_label=None,
        )
        self.member = MemberRow(
            member_id=1,
            login_id="member",
            nickname="Member",
            password_hash="hash",
            rating=1000,
        )

    async def __aenter__(self) -> LoadGameSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get(
        self,
        entity: type[object],
        key: object,
        **_kwargs: object,
    ) -> object | None:
        if entity is GameRow and key == GAME_ID:
            return self.game
        return None

    async def execute(self, statement: object) -> ResultBag:
        self.statements.append(statement)
        index = len(self.statements)
        if index == 1:
            return ResultBag([self.participant])
        if index in {2, 3}:
            return ResultBag([])
        if index == 4:
            return ResultBag([self.member])
        raise AssertionError("unexpected execute call")


class FinalizeSession:
    def __init__(self) -> None:
        self.statements: list[object] = []
        self.game = GameRow(
            game_id=GAME_ID,
            room_id=ROOM_ID,
            voting_time_seconds=10,
            status="IN_PROGRESS",
            started_at=NOW,
            ended_at=None,
        )
        self.member = MemberRow(
            member_id=1,
            login_id="member",
            nickname="Member",
            password_hash="hash",
            rating=1000,
        )
        self.stats = MemberStatsRow(
            member_id=1,
            wins=0,
            draws=0,
            losses=0,
            games_played=0,
        )
        self.added: list[object] = []

    async def __aenter__(self) -> FinalizeSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def begin(self) -> None:
        return None

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def get(
        self,
        entity: type[object],
        key: object,
        **_kwargs: object,
    ) -> object | None:
        if entity is GameRow and key == GAME_ID:
            return self.game
        if entity is GameResultRow and key == GAME_ID:
            return None
        if entity is MemberStatsRow and key == 1:
            return self.stats
        return None

    async def execute(self, statement: object) -> ResultBag:
        self.statements.append(statement)
        return ResultBag([self.member])

    def add(self, row: object) -> None:
        self.added.append(row)


class SessionFactory:
    def __init__(self, *sessions: object) -> None:
        self.sessions = list(sessions)

    def __call__(self) -> Any:
        return self.sessions.pop(0)


def assert_member_query_uses_only_game_granted_columns(statement: object) -> None:
    sql = str(statement)
    assert "member.member_id" in sql
    assert "member.rating" in sql
    forbidden_columns = (
        "member.login_id",
        "member.nickname",
        "member.password_hash",
        "member.created_at",
    )
    for forbidden in forbidden_columns:
        assert forbidden not in sql


@pytest.mark.asyncio
async def test_load_game_member_lookup_stays_within_game_service_grants() -> None:
    session = LoadGameSession()
    adapter = MariaDBGamePersistenceAdapter(SessionFactory(session))

    snapshot = await adapter.load_game(GAME_ID)

    assert snapshot is not None
    assert snapshot.participants[0].rating == 1000
    assert len(session.statements) == 4
    assert_member_query_uses_only_game_granted_columns(session.statements[-1])


@pytest.mark.asyncio
async def test_finalize_member_lock_stays_within_game_service_grants() -> None:
    session = FinalizeSession()
    verify_session = FinalizeSession()
    adapter = MariaDBGamePersistenceAdapter(SessionFactory(session, verify_session))
    result = GameResult(
        game_id=GAME_ID,
        status=GameStatus.FINISHED,
        end_reason=EndReason.BLACK_WIN,
        winner=Stone.BLACK,
        winning_line=(),
        stats_eligible=True,
        rating_adjustments=(
            RatingAdjustment(
                participant_id=PARTICIPANT_ID,
                member_id=1,
                team=Stone.BLACK,
                outcome=MemberOutcome.WIN,
                rating_before=1000,
                rating_delta=16,
                rating_after=1016,
            ),
        ),
    )

    await adapter.finalize_game(FinalizeGameCommand(result=result, ended_at=NOW))

    assert session.statements
    assert_member_query_uses_only_game_granted_columns(session.statements[0])
