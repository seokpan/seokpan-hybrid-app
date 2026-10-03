from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import ValidationError
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, UniqueConstraint
from sqlalchemy.dialects import mysql

from seokpan.persistence.mariadb.models import Base
from seokpan.persistence.mariadb.settings import MigrationSettings

BACKEND_ROOT = Path(__file__).parents[2]

EXPECTED_COLUMNS = {
    "member": {
        "member_id",
        "login_id",
        "nickname",
        "password_hash",
        "rating",
        "created_at",
        "updated_at",
    },
    "member_stats": {"member_id", "wins", "draws", "losses", "games_played", "updated_at"},
    "game": {
        "game_id",
        "room_id",
        "voting_time_seconds",
        "status",
        "started_at",
        "ended_at",
    },
    "game_participant": {
        "id",
        "game_id",
        "participant_id",
        "team",
        "member_id",
        "is_guest",
        "guest_label",
    },
    "move": {
        "game_id",
        "turn_no",
        "move_no",
        "team",
        "pos_x",
        "pos_y",
        "final_vote_count",
        "valid_voter_count",
        "confirmed_at",
    },
    "game_result": {"game_id", "winner", "end_reason", "reflected_to_stats", "ended_at"},
    "rating_history": {
        "id",
        "member_id",
        "game_id",
        "rating_before",
        "rating_after",
        "rating_delta",
        "recorded_at",
    },
}


def names(
    items: set[CheckConstraint | ForeignKeyConstraint | UniqueConstraint | Index],
) -> set[str]:
    return {item.name for item in items if item.name is not None}


def test_metadata_contains_exact_adopted_table_and_column_set() -> None:
    assert set(Base.metadata.tables) == set(EXPECTED_COLUMNS)
    assert {
        table_name: set(table.columns.keys()) for table_name, table in Base.metadata.tables.items()
    } == EXPECTED_COLUMNS


def test_metadata_preserves_named_constraints_and_indexes() -> None:
    member = Base.metadata.tables["member"]
    participant = Base.metadata.tables["game_participant"]
    move = Base.metadata.tables["move"]
    rating = Base.metadata.tables["rating_history"]

    assert names(set(member.constraints)) >= {
        "uk_member_login_id",
        "uk_member_nickname",
        "chk_member_rating_nonneg",
        "chk_member_nickname_len",
    }
    assert names(set(participant.constraints)) >= {
        "fk_participant_game",
        "fk_participant_member",
        "chk_participant_guest_label",
    }
    assert names(set(participant.indexes)) == {"idx_participant_game", "fk_participant_member"}
    assert names(set(move.constraints)) >= {
        "fk_move_game",
        "uk_move_game_move_no",
        "chk_move_pos_x",
        "chk_move_pos_y",
    }
    assert names(set(rating.constraints)) >= {
        "fk_rating_member",
        "fk_rating_game",
        "uk_rating_member_game",
    }
    assert names(set(rating.indexes)) == {"fk_rating_game"}


def test_mariadb_specific_types_match_runtime_ddl() -> None:
    dialect = mysql.dialect()
    member = Base.metadata.tables["member"]
    game = Base.metadata.tables["game"]
    game_result = Base.metadata.tables["game_result"]

    assert member.c.member_id.type.compile(dialect=dialect) == "BIGINT UNSIGNED"
    assert game.c.game_id.type.compile(dialect=dialect) == "CHAR(36)"
    assert member.c.login_id.type.collation == "utf8mb4_bin"
    assert member.c.nickname.type.collation == "utf8mb4_bin"
    assert game_result.c.winner.type.compile(dialect=dialect) == (
        "ENUM('BLACK','WHITE','DRAW','NONE')"
    )
    assert game_result.c.end_reason.type.compile(dialect=dialect) == (
        "ENUM('NORMAL_WIN','DRAW','FORFEIT','MUTUAL_FORFEIT','SYSTEM_INVALID')"
    )
    assert game_result.c.winner.nullable is False


def test_migration_settings_are_separate_and_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEOKPAN_MIGRATION_DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        MigrationSettings()

    monkeypatch.setenv(
        "SEOKPAN_MIGRATION_DATABASE_URL",
        "mysql+asyncmy://db_admin@db.seokpan.soldesk.store:3306/stone_game",
    )
    settings = MigrationSettings()

    assert settings.migration_database_url.endswith("/stone_game")


def test_alembic_has_one_head_after_participant_identity_expand() -> None:
    config = Config(BACKEND_ROOT / "alembic.ini")
    script = ScriptDirectory.from_config(config)

    assert script.get_heads() == ["20260902_0002"]
    assert script.get_base() == "20260901_0001"


def test_offline_upgrade_emits_all_seven_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "SEOKPAN_MIGRATION_DATABASE_URL",
        "mysql+asyncmy://db_admin@db.seokpan.soldesk.store:3306/stone_game",
    )
    config = Config(BACKEND_ROOT / "alembic.ini")
    output = StringIO()

    with redirect_stdout(output):
        command.upgrade(config, "head", sql=True)

    sql = output.getvalue()
    for table_name in EXPECTED_COLUMNS:
        assert f"CREATE TABLE {table_name}" in sql
    assert "CREATE TABLE alembic_version" in sql
    assert "INSERT INTO alembic_version (version_num) VALUES ('20260901_0001')" in sql
    assert "ALTER TABLE game_participant ADD COLUMN participant_id CHAR(36)" in sql
    assert "UPDATE alembic_version SET version_num='20260902_0002'" in sql
