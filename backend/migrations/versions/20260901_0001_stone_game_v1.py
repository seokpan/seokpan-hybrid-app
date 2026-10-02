"""Create the adopted stone_game v1 baseline.

Revision ID: 20260901_0001
Revises:
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "20260901_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_OPTIONS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
}


def upgrade() -> None:
    op.create_table(
        "member",
        sa.Column("member_id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("login_id", sa.String(32, collation="utf8mb4_bin"), nullable=False),
        sa.Column("nickname", sa.String(12, collation="utf8mb4_bin"), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("rating", sa.Integer(), server_default=sa.text("1000"), nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=3),
            server_default=sa.text("CURRENT_TIMESTAMP(3)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=3),
            server_default=sa.text("CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("member_id"),
        sa.UniqueConstraint("login_id", name="uk_member_login_id"),
        sa.UniqueConstraint("nickname", name="uk_member_nickname"),
        sa.CheckConstraint("rating >= 0", name="chk_member_rating_nonneg"),
        sa.CheckConstraint(
            "CHAR_LENGTH(nickname) BETWEEN 2 AND 12",
            name="chk_member_nickname_len",
        ),
        **TABLE_OPTIONS,
    )
    op.create_table(
        "member_stats",
        sa.Column("member_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column(
            "wins", mysql.INTEGER(unsigned=True), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "draws", mysql.INTEGER(unsigned=True), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "losses", mysql.INTEGER(unsigned=True), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "games_played",
            mysql.INTEGER(unsigned=True),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=3),
            server_default=sa.text("CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["member_id"], ["member.member_id"], name="fk_member_stats_member"),
        sa.PrimaryKeyConstraint("member_id"),
        **TABLE_OPTIONS,
    )
    op.create_table(
        "game",
        sa.Column("game_id", mysql.CHAR(36), nullable=False),
        sa.Column("room_id", sa.String(64), nullable=True),
        sa.Column("voting_time_seconds", mysql.TINYINT(), nullable=False),
        sa.Column(
            "status",
            mysql.ENUM("IN_PROGRESS", "COMPLETED", "SYSTEM_INVALID"),
            server_default=sa.text("'IN_PROGRESS'"),
            nullable=False,
        ),
        sa.Column("started_at", mysql.DATETIME(fsp=3), nullable=False),
        sa.Column("ended_at", mysql.DATETIME(fsp=3), nullable=True),
        sa.PrimaryKeyConstraint("game_id"),
        sa.CheckConstraint("voting_time_seconds IN (5,10,15,30)", name="chk_game_voting_time"),
        **TABLE_OPTIONS,
    )
    op.create_table(
        "game_participant",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("game_id", mysql.CHAR(36), nullable=False),
        sa.Column("team", mysql.ENUM("BLACK", "WHITE"), nullable=False),
        sa.Column("member_id", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("is_guest", sa.Boolean(), nullable=False),
        sa.Column("guest_label", sa.String(10), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "(is_guest = FALSE AND guest_label IS NULL) "
            "OR (is_guest = TRUE AND guest_label REGEXP '^Guest-[0-9]{4}$')",
            name="chk_participant_guest_label",
        ),
        **TABLE_OPTIONS,
    )
    op.create_index("idx_participant_game", "game_participant", ["game_id"], unique=False)
    op.create_index("fk_participant_member", "game_participant", ["member_id"], unique=False)
    op.create_foreign_key(
        "fk_participant_game", "game_participant", "game", ["game_id"], ["game_id"]
    )
    op.create_foreign_key(
        "fk_participant_member", "game_participant", "member", ["member_id"], ["member_id"]
    )
    op.create_table(
        "move",
        sa.Column("game_id", mysql.CHAR(36), nullable=False),
        sa.Column("turn_no", mysql.SMALLINT(unsigned=True), nullable=False),
        sa.Column("move_no", mysql.SMALLINT(unsigned=True), nullable=False),
        sa.Column("team", mysql.ENUM("BLACK", "WHITE"), nullable=False),
        sa.Column("pos_x", mysql.TINYINT(unsigned=True), nullable=False),
        sa.Column("pos_y", mysql.TINYINT(unsigned=True), nullable=False),
        sa.Column("final_vote_count", mysql.SMALLINT(unsigned=True), nullable=False),
        sa.Column("valid_voter_count", mysql.SMALLINT(unsigned=True), nullable=False),
        sa.Column("confirmed_at", mysql.DATETIME(fsp=3), nullable=False),
        sa.ForeignKeyConstraint(["game_id"], ["game.game_id"], name="fk_move_game"),
        sa.PrimaryKeyConstraint("game_id", "turn_no"),
        sa.UniqueConstraint("game_id", "move_no", name="uk_move_game_move_no"),
        sa.CheckConstraint("pos_x BETWEEN 0 AND 14", name="chk_move_pos_x"),
        sa.CheckConstraint("pos_y BETWEEN 0 AND 14", name="chk_move_pos_y"),
        **TABLE_OPTIONS,
    )
    op.create_table(
        "game_result",
        sa.Column("game_id", mysql.CHAR(36), nullable=False),
        sa.Column("winner", mysql.ENUM("BLACK", "WHITE", "DRAW", "NONE"), nullable=False),
        sa.Column(
            "end_reason",
            mysql.ENUM("NORMAL_WIN", "DRAW", "FORFEIT", "MUTUAL_FORFEIT", "SYSTEM_INVALID"),
            nullable=False,
        ),
        sa.Column("reflected_to_stats", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("ended_at", mysql.DATETIME(fsp=3), nullable=False),
        sa.ForeignKeyConstraint(["game_id"], ["game.game_id"], name="fk_result_game"),
        sa.PrimaryKeyConstraint("game_id"),
        **TABLE_OPTIONS,
    )
    op.create_table(
        "rating_history",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("member_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("game_id", mysql.CHAR(36), nullable=False),
        sa.Column("rating_before", sa.Integer(), nullable=False),
        sa.Column("rating_after", sa.Integer(), nullable=False),
        sa.Column("rating_delta", sa.Integer(), nullable=False),
        sa.Column(
            "recorded_at",
            mysql.DATETIME(fsp=3),
            server_default=sa.text("CURRENT_TIMESTAMP(3)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("member_id", "game_id", name="uk_rating_member_game"),
        **TABLE_OPTIONS,
    )
    op.create_index("fk_rating_game", "rating_history", ["game_id"], unique=False)
    op.create_foreign_key(
        "fk_rating_member", "rating_history", "member", ["member_id"], ["member_id"]
    )
    op.create_foreign_key("fk_rating_game", "rating_history", "game", ["game_id"], ["game_id"])


def downgrade() -> None:
    op.drop_table("rating_history")
    op.drop_table("game_result")
    op.drop_table("move")
    op.drop_table("game_participant")
    op.drop_table("game")
    op.drop_table("member_stats")
    op.drop_table("member")
