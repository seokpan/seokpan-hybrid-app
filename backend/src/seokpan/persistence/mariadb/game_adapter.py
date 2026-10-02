"""SQLAlchemy async adapter for durable Game history in MariaDB."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from seokpan.game.application import (
    FinalizeGameCommand,
    GameParticipantRecord,
    GamePersistenceSnapshot,
    OfficialMoveRecord,
    PersistenceOutcome,
    PersistenceRuleViolation,
    StartGameCommand,
    StoredGameResult,
)
from seokpan.game.domain import (
    Coordinate,
    EndReason,
    GameParticipantRole,
    GameParticipantSnapshot,
    GameResult,
    GameStatus,
    MemberOutcome,
    RatingAdjustment,
    Stone,
)
from seokpan.persistence.mariadb.models import (
    GameParticipantRow,
    GameResultRow,
    GameRow,
    MemberRow,
    MemberStatsRow,
    MoveRow,
    RatingHistoryRow,
)


def _to_db_datetime(value: datetime) -> datetime:
    """Store UTC in MariaDB DATETIME(3), which carries no timezone metadata."""
    normalized = value if value.tzinfo is None else value.astimezone(UTC).replace(tzinfo=None)
    return normalized.replace(microsecond=(normalized.microsecond // 1000) * 1000)


def _from_db_datetime(value: datetime) -> datetime:
    """Interpret timezone-naive MariaDB DATETIME values as UTC application time."""
    normalized = value if value.tzinfo is None else value.astimezone(UTC).replace(tzinfo=None)
    normalized = normalized.replace(microsecond=(normalized.microsecond // 1000) * 1000)
    return normalized.replace(tzinfo=UTC)


def _same_db_datetime(left: datetime, right: datetime) -> bool:
    return _to_db_datetime(left) == _to_db_datetime(right)


SessionFactory = Callable[[], AsyncSession]


class MariaDBGamePersistenceAdapter:
    """Persist Game facts while keeping one explicit transaction per command."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def start_game(self, command: StartGameCommand) -> PersistenceOutcome:
        async def write(session: AsyncSession) -> PersistenceOutcome:
            existing = await session.get(GameRow, command.game_id, with_for_update=True)
            if existing is not None:
                return await self._compare_existing_game(session, command, existing)
            session.add(
                GameRow(
                    game_id=command.game_id,
                    room_id=command.room_id,
                    voting_time_seconds=command.voting_time_seconds,
                    status="IN_PROGRESS",
                    started_at=_to_db_datetime(command.started_at),
                    ended_at=None,
                )
            )
            # 수정자: 이유빈
            # 수정일: 2026-09-16
            # 수정 내용: GameRow를 GameParticipantRow보다 먼저 flush하도록 변경
            await session.flush()
            session.add_all(self._participant_rows(command.game_id, command.participants))
            return PersistenceOutcome.CREATED

        return await self._transaction(write, lambda: self._game_matches(command))

    async def append_move(self, command: OfficialMoveRecord) -> PersistenceOutcome:
        async def write(session: AsyncSession) -> PersistenceOutcome:
            by_turn = await session.get(MoveRow, (command.game_id, command.turn_no))
            by_move = (
                await session.execute(
                    select(MoveRow).where(
                        MoveRow.game_id == command.game_id,
                        MoveRow.move_no == command.move_no,
                    )
                )
            ).scalar_one_or_none()
            existing = by_turn if by_turn is not None else by_move
            if existing is not None:
                if self._move_matches(existing, command):
                    return PersistenceOutcome.UNCHANGED
                raise PersistenceRuleViolation("MOVE_SEQUENCE_CONFLICT")
            session.add(self._move_row(command))
            return PersistenceOutcome.CREATED

        return await self._transaction(write, lambda: self._move_exists(command))

    async def finalize_game(self, command: FinalizeGameCommand) -> PersistenceOutcome:
        async def write(session: AsyncSession) -> PersistenceOutcome:
            game = await session.get(
                GameRow,
                command.result.game_id,
                with_for_update=True,
            )
            if game is None:
                raise PersistenceRuleViolation("GAME_NOT_FOUND")
            existing = await session.get(
                GameResultRow,
                command.result.game_id,
                with_for_update=True,
            )
            if existing is not None:
                if await self._result_is_complete(session, existing, command):
                    return PersistenceOutcome.UNCHANGED
                raise PersistenceRuleViolation("GAME_RESULT_CONFLICT")
            if game.status != "IN_PROGRESS":
                raise PersistenceRuleViolation("GAME_STATUS_CONFLICT")

            members = await self._lock_members(session, command.result)
            result_row = self._result_row(command)
            session.add(result_row)
            game.status = self._game_status(command.result.status)
            game.ended_at = _to_db_datetime(command.ended_at)
            if command.result.stats_eligible:
                await self._apply_member_updates(session, command.result, members)
            result_row.reflected_to_stats = True
            return PersistenceOutcome.CREATED

        return await self._transaction(write, lambda: self._result_exists(command))

    async def load_result(self, game_id: str) -> StoredGameResult | None:
        async with self._session_factory() as session:
            game = await session.get(GameRow, game_id)
            result = await session.get(GameResultRow, game_id)
            if result is None:
                if game is not None and game.status != "IN_PROGRESS":
                    raise PersistenceRuleViolation("GAME_RESULT_INCOMPLETE")
                return None
            if (
                game is None
                or game.room_id is None
                or game.ended_at is None
                or not _same_db_datetime(game.ended_at, result.ended_at)
                or not result.reflected_to_stats
            ):
                raise PersistenceRuleViolation("GAME_RESULT_INCOMPLETE")
            conclusion = {
                ("COMPLETED", "NORMAL_WIN", "BLACK"): (
                    GameStatus.FINISHED,
                    EndReason.BLACK_WIN,
                    Stone.BLACK,
                ),
                ("COMPLETED", "NORMAL_WIN", "WHITE"): (
                    GameStatus.FINISHED,
                    EndReason.WHITE_WIN,
                    Stone.WHITE,
                ),
                ("COMPLETED", "DRAW", "DRAW"): (GameStatus.FINISHED, EndReason.DRAW, Stone.EMPTY),
                ("COMPLETED", "FORFEIT", "BLACK"): (
                    GameStatus.FINISHED,
                    EndReason.FORFEIT,
                    Stone.BLACK,
                ),
                ("COMPLETED", "FORFEIT", "WHITE"): (
                    GameStatus.FINISHED,
                    EndReason.FORFEIT,
                    Stone.WHITE,
                ),
                ("COMPLETED", "MUTUAL_FORFEIT", "NONE"): (
                    GameStatus.FINISHED,
                    EndReason.JOINT_LOSS,
                    Stone.EMPTY,
                ),
                ("SYSTEM_INVALID", "SYSTEM_INVALID", "NONE"): (
                    GameStatus.SYSTEM_INVALID,
                    EndReason.SYSTEM_INVALID,
                    Stone.EMPTY,
                ),
            }.get((game.status, result.end_reason, result.winner))
            if conclusion is None:
                raise PersistenceRuleViolation("GAME_RESULT_INCOMPLETE")
            stored = StoredGameResult(
                game_id,
                game.room_id,
                *conclusion,
                _from_db_datetime(game.ended_at),
                (),
            )
            participant_rows = (
                (
                    await session.execute(
                        select(GameParticipantRow).where(GameParticipantRow.game_id == game_id)
                    )
                )
                .scalars()
                .all()
            )
            histories = (
                (
                    await session.execute(
                        select(RatingHistoryRow).where(RatingHistoryRow.game_id == game_id)
                    )
                )
                .scalars()
                .all()
            )
            return self._stored_ratings(stored, participant_rows, histories)

    @staticmethod
    def _stored_ratings(
        result: StoredGameResult,
        participants: Sequence[GameParticipantRow],
        histories: Sequence[RatingHistoryRow],
    ) -> StoredGameResult:
        # Read only this game's history, never the Member's current rating.
        members = {item.member_id: item for item in participants if item.member_id is not None}
        expected = set(members) if result.stats_eligible else set()
        if (
            len(members) != sum(item.member_id is not None for item in participants)
            or {item.member_id for item in histories} != expected
            or len(histories) != len(expected)
        ):
            raise PersistenceRuleViolation("GAME_RESULT_INCOMPLETE")
        adjustments: list[RatingAdjustment] = []
        for history in sorted(histories, key=lambda item: item.member_id):
            participant = members[history.member_id]
            if participant.participant_id is None or participant.team not in {"BLACK", "WHITE"}:
                raise PersistenceRuleViolation("GAME_RESULT_INCOMPLETE")
            team = Stone(participant.team)
            adjustments.append(
                RatingAdjustment(
                    participant.participant_id,
                    history.member_id,
                    team,
                    result.outcome_for(team),
                    history.rating_before,
                    history.rating_delta,
                    history.rating_after,
                )
            )
        return StoredGameResult(
            result.game_id,
            result.room_id,
            result.status,
            result.end_reason,
            result.winner,
            result.ended_at,
            tuple(adjustments),
        )

    async def load_game(self, game_id: str) -> GamePersistenceSnapshot | None:
        async with self._session_factory() as session:
            game = await session.get(GameRow, game_id)
            if game is None:
                return None
            participant_rows = (
                (
                    await session.execute(
                        select(GameParticipantRow)
                        .where(GameParticipantRow.game_id == game_id)
                        .order_by(GameParticipantRow.participant_id)
                    )
                )
                .scalars()
                .all()
            )
            move_rows = (
                (
                    await session.execute(
                        select(MoveRow).where(MoveRow.game_id == game_id).order_by(MoveRow.turn_no)
                    )
                )
                .scalars()
                .all()
            )
            histories = (
                (
                    await session.execute(
                        select(RatingHistoryRow).where(RatingHistoryRow.game_id == game_id)
                    )
                )
                .scalars()
                .all()
            )
            history_ratings = {item.member_id: item.rating_before for item in histories}
            member_ids = sorted(
                item.member_id for item in participant_rows if item.member_id is not None
            )
            members = (
                {}
                if not member_ids
                else {
                    item.member_id: item
                    for item in (
                        (
                            await session.execute(
                                select(MemberRow)
                                .options(load_only(MemberRow.member_id, MemberRow.rating))
                                .where(MemberRow.member_id.in_(member_ids))
                            )
                        )
                        .scalars()
                        .all()
                    )
                }
            )
            participants: list[GameParticipantSnapshot] = []
            records: list[GameParticipantRecord] = []
            for item in participant_rows:
                team = Stone(item.team)
                records.append(
                    GameParticipantRecord(
                        participant_id=cast(str, item.participant_id),
                        team=team,
                        member_id=item.member_id,
                        guest_label=item.guest_label,
                    )
                )
                rating = None
                if item.member_id is not None:
                    member = members.get(item.member_id)
                    if member is None:
                        raise PersistenceRuleViolation("MEMBER_NOT_FOUND")
                    rating = history_ratings.get(item.member_id, member.rating)
                participants.append(
                    GameParticipantSnapshot(
                        participant_id=cast(str, item.participant_id),
                        team=team,
                        role=GameParticipantRole.PLAYER,
                        member_id=item.member_id,
                        rating=rating,
                    )
                )
            return GamePersistenceSnapshot(
                start=StartGameCommand(
                    game_id=game.game_id,
                    room_id=cast(str, game.room_id),
                    voting_time_seconds=game.voting_time_seconds,
                    started_at=_from_db_datetime(game.started_at),
                    participants=tuple(records),
                ),
                participants=tuple(participants),
                moves=tuple(self._move_record(item) for item in move_rows),
            )

    async def get_move(self, game_id: str, turn_no: int) -> OfficialMoveRecord | None:
        async with self._session_factory() as session:
            row = await session.get(MoveRow, (game_id, turn_no))
            return None if row is None else self._move_record(row)

    async def result_matches(self, command: FinalizeGameCommand) -> bool:
        return await self._result_exists(command)

    async def game_is_finalized(self, game_id: str) -> bool:
        async with self._session_factory() as session:
            game = await session.get(GameRow, game_id)
            result = await session.get(GameResultRow, game_id)
            return bool(
                game is not None
                and game.status != "IN_PROGRESS"
                and game.ended_at is not None
                and result is not None
                and result.reflected_to_stats
            )

    async def _transaction(
        self,
        write: Callable[[AsyncSession], Awaitable[PersistenceOutcome]],
        verify_after_error: Callable[[], Awaitable[bool]],
    ) -> PersistenceOutcome:
        async with self._session_factory() as session:
            await session.begin()
            try:
                outcome = await write(session)
                await session.flush()
                await session.commit()
                return outcome
            except PersistenceRuleViolation:
                await session.rollback()
                raise
            except SQLAlchemyError as error:
                await session.rollback()
                try:
                    if await verify_after_error():
                        return PersistenceOutcome.UNCHANGED
                except SQLAlchemyError:
                    pass
                raise PersistenceRuleViolation("PERSISTENCE_COMMIT_UNCERTAIN") from error

    async def _compare_existing_game(
        self,
        session: AsyncSession,
        command: StartGameCommand,
        existing: GameRow,
    ) -> PersistenceOutcome:
        participants = (
            (
                await session.execute(
                    select(GameParticipantRow).where(GameParticipantRow.game_id == command.game_id)
                )
            )
            .scalars()
            .all()
        )
        if self._game_row_matches(existing, command) and self._participants_match(
            participants, command.participants
        ):
            return PersistenceOutcome.UNCHANGED
        raise PersistenceRuleViolation("GAME_START_CONFLICT")

    async def _game_matches(self, command: StartGameCommand) -> bool:
        async with self._session_factory() as session:
            existing = await session.get(GameRow, command.game_id)
            if existing is None:
                return False
            participants = (
                (
                    await session.execute(
                        select(GameParticipantRow).where(
                            GameParticipantRow.game_id == command.game_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            if self._game_row_matches(existing, command) and self._participants_match(
                participants, command.participants
            ):
                return True
            raise PersistenceRuleViolation("GAME_START_CONFLICT")

    async def _move_exists(self, command: OfficialMoveRecord) -> bool:
        async with self._session_factory() as session:
            by_turn = await session.get(MoveRow, (command.game_id, command.turn_no))
            by_move = (
                await session.execute(
                    select(MoveRow).where(
                        MoveRow.game_id == command.game_id,
                        MoveRow.move_no == command.move_no,
                    )
                )
            ).scalar_one_or_none()
            existing = by_turn if by_turn is not None else by_move
            if existing is None:
                return False
            if self._move_matches(existing, command):
                return True
            raise PersistenceRuleViolation("MOVE_SEQUENCE_CONFLICT")

    async def _result_exists(self, command: FinalizeGameCommand) -> bool:
        async with self._session_factory() as session:
            existing = await session.get(GameResultRow, command.result.game_id)
            if existing is None:
                return False
            if await self._result_is_complete(session, existing, command):
                return True
            raise PersistenceRuleViolation("GAME_RESULT_CONFLICT")

    async def _result_is_complete(
        self,
        session: AsyncSession,
        row: GameResultRow,
        command: FinalizeGameCommand,
    ) -> bool:
        if not row.reflected_to_stats or not self._result_matches(row, command):
            return False
        histories = (
            (
                await session.execute(
                    select(RatingHistoryRow).where(
                        RatingHistoryRow.game_id == command.result.game_id
                    )
                )
            )
            .scalars()
            .all()
        )
        actual = {
            (item.member_id, item.rating_before, item.rating_after, item.rating_delta)
            for item in histories
        }
        expected = {
            (
                item.member_id,
                item.rating_before,
                item.rating_after,
                item.rating_delta,
            )
            for item in command.result.rating_adjustments
        }
        return actual == expected

    async def _lock_members(
        self,
        session: AsyncSession,
        result: GameResult,
    ) -> dict[int, MemberRow]:
        member_ids = sorted(item.member_id for item in result.rating_adjustments)
        if not member_ids:
            return {}
        rows = (
            (
                await session.execute(
                    select(MemberRow)
                    .options(load_only(MemberRow.member_id, MemberRow.rating))
                    .where(MemberRow.member_id.in_(member_ids))
                    .order_by(MemberRow.member_id)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        members = {row.member_id: row for row in rows}
        if set(members) != set(member_ids):
            raise PersistenceRuleViolation("MEMBER_NOT_FOUND")
        return members

    async def _apply_member_updates(
        self,
        session: AsyncSession,
        result: GameResult,
        members: dict[int, MemberRow],
    ) -> None:
        for adjustment in sorted(result.rating_adjustments, key=lambda item: item.member_id):
            member = members[adjustment.member_id]
            if member.rating != adjustment.rating_before:
                raise PersistenceRuleViolation("STALE_MEMBER_RATING")
            stats = await session.get(
                MemberStatsRow,
                adjustment.member_id,
                with_for_update=True,
            )
            if stats is None:
                stats = MemberStatsRow(
                    member_id=adjustment.member_id,
                    wins=0,
                    draws=0,
                    losses=0,
                    games_played=0,
                )
                session.add(stats)
            stats.games_played += 1
            if adjustment.outcome is MemberOutcome.WIN:
                stats.wins += 1
            elif adjustment.outcome is MemberOutcome.DRAW:
                stats.draws += 1
            else:
                stats.losses += 1
            member.rating = adjustment.rating_after
            session.add(
                RatingHistoryRow(
                    member_id=adjustment.member_id,
                    game_id=result.game_id,
                    rating_before=adjustment.rating_before,
                    rating_after=adjustment.rating_after,
                    rating_delta=adjustment.rating_delta,
                )
            )

    @staticmethod
    def _participant_rows(
        game_id: str,
        participants: tuple[GameParticipantRecord, ...],
    ) -> list[GameParticipantRow]:
        return [
            GameParticipantRow(
                game_id=game_id,
                participant_id=item.participant_id,
                team=item.team.value,
                member_id=item.member_id,
                is_guest=item.member_id is None,
                guest_label=item.guest_label,
            )
            for item in participants
        ]

    @staticmethod
    def _move_row(command: OfficialMoveRecord) -> MoveRow:
        return MoveRow(
            game_id=command.game_id,
            turn_no=command.turn_no,
            move_no=command.move_no,
            team=command.team.value,
            pos_x=command.coordinate.column - 1,
            pos_y=command.coordinate.row - 1,
            final_vote_count=command.final_vote_count,
            valid_voter_count=command.valid_voter_count,
            confirmed_at=_to_db_datetime(command.confirmed_at),
        )

    @staticmethod
    def _move_record(row: MoveRow) -> OfficialMoveRecord:
        return OfficialMoveRecord(
            game_id=row.game_id,
            turn_no=row.turn_no,
            move_no=row.move_no,
            team=Stone(row.team),
            coordinate=Coordinate(column=row.pos_x + 1, row=row.pos_y + 1),
            final_vote_count=row.final_vote_count,
            valid_voter_count=row.valid_voter_count,
            confirmed_at=_from_db_datetime(row.confirmed_at),
        )

    @staticmethod
    def _result_row(command: FinalizeGameCommand) -> GameResultRow:
        return GameResultRow(
            game_id=command.result.game_id,
            winner=MariaDBGamePersistenceAdapter._winner(command.result),
            end_reason=MariaDBGamePersistenceAdapter._end_reason(command.result.end_reason),
            reflected_to_stats=False,
            ended_at=_to_db_datetime(command.ended_at),
        )

    @staticmethod
    def _game_row_matches(row: GameRow, command: StartGameCommand) -> bool:
        return (
            row.room_id == command.room_id
            and row.voting_time_seconds == command.voting_time_seconds
            and row.status == "IN_PROGRESS"
            and _same_db_datetime(row.started_at, command.started_at)
            and row.ended_at is None
        )

    @staticmethod
    def _participants_match(
        rows: Sequence[GameParticipantRow],
        expected: tuple[GameParticipantRecord, ...],
    ) -> bool:
        actual_values = {
            (row.participant_id, row.team, row.member_id, row.guest_label, row.is_guest)
            for row in rows
        }
        expected_values = {
            (
                item.participant_id,
                item.team.value,
                item.member_id,
                item.guest_label,
                item.member_id is None,
            )
            for item in expected
        }
        return actual_values == expected_values

    @staticmethod
    def _move_matches(row: MoveRow, command: OfficialMoveRecord) -> bool:
        return (
            row.game_id == command.game_id
            and row.turn_no == command.turn_no
            and row.move_no == command.move_no
            and row.team == command.team.value
            and row.pos_x == command.coordinate.column - 1
            and row.pos_y == command.coordinate.row - 1
            and row.final_vote_count == command.final_vote_count
            and row.valid_voter_count == command.valid_voter_count
            and _same_db_datetime(row.confirmed_at, command.confirmed_at)
        )

    @staticmethod
    def _result_matches(row: GameResultRow, command: FinalizeGameCommand) -> bool:
        return (
            row.winner == MariaDBGamePersistenceAdapter._winner(command.result)
            and row.end_reason
            == MariaDBGamePersistenceAdapter._end_reason(command.result.end_reason)
            and _same_db_datetime(row.ended_at, command.ended_at)
        )

    @staticmethod
    def _game_status(status: GameStatus) -> str:
        return "SYSTEM_INVALID" if status is GameStatus.SYSTEM_INVALID else "COMPLETED"

    @staticmethod
    def _winner(result: GameResult) -> str:
        if result.end_reason is EndReason.DRAW:
            return "DRAW"
        return result.winner.value if result.winner is not Stone.EMPTY else "NONE"

    @staticmethod
    def _end_reason(reason: EndReason) -> str:
        if reason in {EndReason.BLACK_WIN, EndReason.WHITE_WIN}:
            return "NORMAL_WIN"
        if reason is EndReason.JOINT_LOSS:
            return "MUTUAL_FORFEIT"
        return reason.value
