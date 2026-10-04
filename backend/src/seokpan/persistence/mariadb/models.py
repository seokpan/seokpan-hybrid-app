from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

TABLE_OPTIONS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
}


class Base(DeclarativeBase):
    """Declarative base for the existing stone_game schema."""


class MemberRow(Base):
    __tablename__ = "member"
    __table_args__ = (
        UniqueConstraint("login_id", name="uk_member_login_id"),
        UniqueConstraint("nickname", name="uk_member_nickname"),
        CheckConstraint("rating >= 0", name="chk_member_rating_nonneg"),
        CheckConstraint(
            "CHAR_LENGTH(nickname) BETWEEN 2 AND 12",
            name="chk_member_nickname_len",
        ),
        TABLE_OPTIONS,
    )

    member_id: Mapped[int] = mapped_column(mysql.BIGINT(unsigned=True), primary_key=True)
    login_id: Mapped[str] = mapped_column(String(32, collation="utf8mb4_bin"), nullable=False)
    nickname: Mapped[str] = mapped_column(String(12, collation="utf8mb4_bin"), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    rating: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1000"))
    created_at: Mapped[datetime] = mapped_column(
        mysql.DATETIME(fsp=3),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(3)"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        mysql.DATETIME(fsp=3),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3)"),
    )


class MemberStatsRow(Base):
    __tablename__ = "member_stats"
    __table_args__ = (TABLE_OPTIONS,)

    member_id: Mapped[int] = mapped_column(
        mysql.BIGINT(unsigned=True),
        ForeignKey("member.member_id", name="fk_member_stats_member"),
        primary_key=True,
    )
    wins: Mapped[int] = mapped_column(
        mysql.INTEGER(unsigned=True), nullable=False, server_default=text("0")
    )
    draws: Mapped[int] = mapped_column(
        mysql.INTEGER(unsigned=True), nullable=False, server_default=text("0")
    )
    losses: Mapped[int] = mapped_column(
        mysql.INTEGER(unsigned=True), nullable=False, server_default=text("0")
    )
    games_played: Mapped[int] = mapped_column(
        mysql.INTEGER(unsigned=True), nullable=False, server_default=text("0")
    )
    updated_at: Mapped[datetime] = mapped_column(
        mysql.DATETIME(fsp=3),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3)"),
    )


class GameRow(Base):
    __tablename__ = "game"
    __table_args__ = (
        CheckConstraint(
            "voting_time_seconds IN (5,10,15,30)",
            name="chk_game_voting_time",
        ),
        TABLE_OPTIONS,
    )

    game_id: Mapped[str] = mapped_column(mysql.CHAR(36), primary_key=True)
    room_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    voting_time_seconds: Mapped[int] = mapped_column(mysql.TINYINT(), nullable=False)
    status: Mapped[str] = mapped_column(
        mysql.ENUM("IN_PROGRESS", "COMPLETED", "SYSTEM_INVALID"),
        nullable=False,
        server_default=text("'IN_PROGRESS'"),
    )
    started_at: Mapped[datetime] = mapped_column(mysql.DATETIME(fsp=3), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(mysql.DATETIME(fsp=3), nullable=True)


class GameParticipantRow(Base):
    __tablename__ = "game_participant"
    __table_args__ = (
        CheckConstraint(
            "(is_guest = FALSE AND guest_label IS NULL) "
            "OR (is_guest = TRUE AND guest_label REGEXP '^Guest-[0-9]{4}$')",
            name="chk_participant_guest_label",
        ),
        Index("idx_participant_game", "game_id"),
        Index("fk_participant_member", "member_id"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(mysql.BIGINT(unsigned=True), primary_key=True)
    game_id: Mapped[str] = mapped_column(
        mysql.CHAR(36),
        ForeignKey("game.game_id", name="fk_participant_game"),
        nullable=False,
    )
    participant_id: Mapped[str | None] = mapped_column(mysql.CHAR(36), nullable=True)
    team: Mapped[str] = mapped_column(mysql.ENUM("BLACK", "WHITE"), nullable=False)
    member_id: Mapped[int | None] = mapped_column(
        mysql.BIGINT(unsigned=True),
        ForeignKey("member.member_id", name="fk_participant_member"),
        nullable=True,
    )
    is_guest: Mapped[bool] = mapped_column(Boolean, nullable=False)
    guest_label: Mapped[str | None] = mapped_column(String(10), nullable=True)


class MoveRow(Base):
    __tablename__ = "move"
    __table_args__ = (
        PrimaryKeyConstraint("game_id", "turn_no"),
        UniqueConstraint("game_id", "move_no", name="uk_move_game_move_no"),
        CheckConstraint("pos_x BETWEEN 0 AND 14", name="chk_move_pos_x"),
        CheckConstraint("pos_y BETWEEN 0 AND 14", name="chk_move_pos_y"),
        TABLE_OPTIONS,
    )

    game_id: Mapped[str] = mapped_column(
        mysql.CHAR(36),
        ForeignKey("game.game_id", name="fk_move_game"),
        nullable=False,
    )
    turn_no: Mapped[int] = mapped_column(mysql.SMALLINT(unsigned=True), nullable=False)
    move_no: Mapped[int] = mapped_column(mysql.SMALLINT(unsigned=True), nullable=False)
    team: Mapped[str] = mapped_column(mysql.ENUM("BLACK", "WHITE"), nullable=False)
    pos_x: Mapped[int] = mapped_column(mysql.TINYINT(unsigned=True), nullable=False)
    pos_y: Mapped[int] = mapped_column(mysql.TINYINT(unsigned=True), nullable=False)
    final_vote_count: Mapped[int] = mapped_column(mysql.SMALLINT(unsigned=True), nullable=False)
    valid_voter_count: Mapped[int] = mapped_column(mysql.SMALLINT(unsigned=True), nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(mysql.DATETIME(fsp=3), nullable=False)


class GameResultRow(Base):
    __tablename__ = "game_result"
    __table_args__ = (TABLE_OPTIONS,)

    game_id: Mapped[str] = mapped_column(
        mysql.CHAR(36),
        ForeignKey("game.game_id", name="fk_result_game"),
        primary_key=True,
    )
    winner: Mapped[str] = mapped_column(
        mysql.ENUM("BLACK", "WHITE", "DRAW", "NONE"), nullable=False
    )
    end_reason: Mapped[str] = mapped_column(
        mysql.ENUM("NORMAL_WIN", "DRAW", "FORFEIT", "MUTUAL_FORFEIT", "SYSTEM_INVALID"),
        nullable=False,
    )
    reflected_to_stats: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("0")
    )
    ended_at: Mapped[datetime] = mapped_column(mysql.DATETIME(fsp=3), nullable=False)


class RatingHistoryRow(Base):
    __tablename__ = "rating_history"
    __table_args__ = (
        UniqueConstraint("member_id", "game_id", name="uk_rating_member_game"),
        Index("fk_rating_game", "game_id"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(mysql.BIGINT(unsigned=True), primary_key=True)
    member_id: Mapped[int] = mapped_column(
        mysql.BIGINT(unsigned=True),
        ForeignKey("member.member_id", name="fk_rating_member"),
        nullable=False,
    )
    game_id: Mapped[str] = mapped_column(
        mysql.CHAR(36),
        ForeignKey("game.game_id", name="fk_rating_game"),
        nullable=False,
    )
    rating_before: Mapped[int] = mapped_column(Integer, nullable=False)
    rating_after: Mapped[int] = mapped_column(Integer, nullable=False)
    rating_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        mysql.DATETIME(fsp=3),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(3)"),
    )
