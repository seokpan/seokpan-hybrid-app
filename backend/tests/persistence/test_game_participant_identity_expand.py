import json
import re
from collections import Counter
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.dialects import mysql

from seokpan.persistence.mariadb.models import Base

BACKEND_ROOT = Path(__file__).parents[2]
AUDIT_SQL = BACKEND_ROOT / "migrations" / "audit" / "game_participant_identity.sql"
PRE_AUDIT_SQL = AUDIT_SQL.with_name("game_participant_identity_pre_expand.sql")
CASES = Path(__file__).parent / "fixtures" / "participant_audit_cases.json"
BASELINE_REVISION = "20260901_0001"
EXPAND_REVISION = "20260902_0002"
DATABASE_URL = "mysql+asyncmy://db_admin@db.seokpan.soldesk.store:3306/stone_game"


def offline_sql(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    revision_range: str,
) -> str:
    monkeypatch.setenv("SEOKPAN_MIGRATION_DATABASE_URL", DATABASE_URL)
    config = Config(BACKEND_ROOT / "alembic.ini")
    output = StringIO()

    with redirect_stdout(output):
        if operation == "upgrade":
            command.upgrade(config, revision_range, sql=True)
        else:
            command.downgrade(config, revision_range, sql=True)

    return output.getvalue()


def test_participant_identity_metadata_is_nullable_char_36() -> None:
    participant_id = Base.metadata.tables["game_participant"].c.participant_id

    assert participant_id.type.compile(dialect=mysql.dialect()) == "CHAR(36)"
    assert participant_id.nullable is True
    assert participant_id.unique is None


def test_expand_upgrade_only_adds_nullable_participant_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql = offline_sql(
        monkeypatch,
        "upgrade",
        f"{BASELINE_REVISION}:{EXPAND_REVISION}",
    )

    assert "ALTER TABLE game_participant ADD COLUMN participant_id CHAR(36)" in sql
    assert "CREATE TABLE" not in sql
    assert "UPDATE game_participant" not in sql
    assert "NOT NULL" not in sql
    assert "UNIQUE" not in sql
    assert "CHECK" not in sql


def test_expand_downgrade_only_drops_participant_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql = offline_sql(
        monkeypatch,
        "downgrade",
        f"{EXPAND_REVISION}:{BASELINE_REVISION}",
    )

    assert "ALTER TABLE game_participant DROP COLUMN participant_id" in sql
    assert "DROP TABLE" not in sql


def statements(path: Path) -> list[str]:
    """Extract these simple checked-in SELECTs, not a general SQL parser."""
    executable = "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("--")
    )
    return [" ".join(part.split()) for part in executable.split(";") if part.strip()]


@pytest.mark.parametrize(("path", "count"), [(PRE_AUDIT_SQL, 5), (AUDIT_SQL, 7)])
def test_audits_contain_only_the_expected_read_only_selects(path: Path, count: int) -> None:
    queries = statements(path)
    assert len(queries) == count
    assert all(query.startswith("SELECT ") for query in queries)
    executable_sql = " ".join(queries).upper()
    assert "TOTAL_PARTICIPANT_ROWS" in executable_sql
    assert "IN_PROGRESS" in executable_sql
    assert "GROUP BY GAME_ID, MEMBER_ID" in executable_sql
    assert "GROUP BY GAME_ID, GUEST_LABEL" in executable_sql
    assert "MEMBER_ID IS NOT NULL" in executable_sql
    assert "MEMBER_ID IS NULL" in executable_sql
    for forbidden in (
        "INSERT ",
        "UPDATE ",
        "DELETE ",
        "ALTER ",
        "CREATE ",
        "DROP ",
        "INTO ",
        "FOR UPDATE",
        "LOCK ",
        "SET ",
        "CALL ",
    ):
        assert forbidden not in executable_sql


def test_pre_expand_never_references_the_missing_column() -> None:
    assert not re.search(r"\bparticipant_id\b", " ".join(statements(PRE_AUDIT_SQL)))
    assert "null_participant_id_rows" not in " ".join(statements(PRE_AUDIT_SQL))


def test_common_findings_do_not_drift_between_audits() -> None:
    assert statements(PRE_AUDIT_SQL)[2:5] == statements(AUDIT_SQL)[2:5]


@pytest.mark.parametrize("path", [PRE_AUDIT_SQL, AUDIT_SQL])
def test_active_games_include_zero_participants_without_counting_the_join_row(path: Path) -> None:
    query = statements(path)[1]
    assert "FROM game AS g LEFT JOIN game_participant AS gp ON gp.game_id = g.game_id" in query
    assert "COUNT(gp.id) AS participant_rows" in query
    assert "GROUP BY g.game_id ORDER BY g.game_id" in query
    assert "COUNT(*)" not in query
    if path == AUDIT_SQL:
        assert (
            "SUM(CASE WHEN gp.id IS NOT NULL AND gp.participant_id IS NULL THEN 1 ELSE 0 END)"
            " AS null_participant_id_rows"
        ) in query


def test_combination_check_handles_null_and_uses_exact_binary_guest_format() -> None:
    query = statements(AUDIT_SQL)[4]
    assert "WHERE NOT COALESCE(" in query
    assert "is_guest = FALSE AND member_id IS NOT NULL AND guest_label IS NULL" in query
    assert "is_guest = TRUE AND member_id IS NULL" in query
    assert "OCTET_LENGTH(guest_label) = 10" in query
    assert "CAST(guest_label AS BINARY) REGEXP '^Guest-[0-9]{4}$'), FALSE )" in query


def test_identity_check_reports_populated_values_only_and_preserves_duplicate_scope() -> None:
    queries = statements(AUDIT_SQL)
    assert "COALESCE(SUM(participant_id IS NULL), 0)" in queries[0]
    assert (
        "WHERE participant_id IS NOT NULL AND ( OCTET_LENGTH(participant_id) <> 36 OR" in queries[5]
    )
    assert "CAST(participant_id AS BINARY) NOT REGEXP" in queries[5]
    assert "GROUP BY game_id, participant_id HAVING COUNT(*) > 1" in queries[6]


FORMAT_CASES = json.loads(CASES.read_text(encoding="utf-8"))["format_cases"]


@pytest.mark.parametrize("case", FORMAT_CASES, ids=[case["name"] for case in FORMAT_CASES])
def test_fixture_format_expectations_match_pattern_intent(case: dict[str, object]) -> None:
    """Checks fixture/pattern intent only; does NOT execute MariaDB SQL semantics."""
    guest_pattern = re.search(r"REGEXP '([^']+)'", statements(AUDIT_SQL)[4])
    uuid_pattern = re.search(r"REGEXP '([^']+)'", statements(AUDIT_SQL)[5])
    assert guest_pattern is not None and uuid_pattern is not None
    label = case["guest_label"]
    member = case["member_id"]
    kind = case["is_guest"]
    valid_combination = (kind == 0 and member is not None and label is None) or (
        kind == 1
        and member is None
        and isinstance(label, str)
        and len(label.encode("utf-8")) == 10
        and re.fullmatch(guest_pattern[1], label) is not None
    )
    identity = case["participant_id"]
    valid_identity = identity is None or (
        isinstance(identity, str)
        and len(identity.encode("utf-8")) == 36
        and re.fullmatch(uuid_pattern[1], identity) is not None
    )
    assert case["combination_finding"] is (not valid_combination)
    assert case["identity_finding"] is (not valid_identity)


def test_isolated_fixture_has_expected_empty_active_and_duplicate_cases() -> None:
    data = json.loads(CASES.read_text(encoding="utf-8"))
    scenarios = {case["name"]: case for case in data["set_cases"]}
    assert set(scenarios) == {
        "empty",
        "active_zero",
        "active_mixed",
        "same_game_duplicates",
        "different_games",
        "completed_game",
    }
    assert scenarios["empty"]["expected_active"] == []
    assert scenarios["active_zero"]["expected_active"] == [["g1", 0, 0]]
    assert scenarios["active_mixed"]["expected_active"] == [["g1", 2, 1]]
    assert scenarios["same_game_duplicates"]["expected_member_duplicates"] == [["g1", 1, 2]]
    assert scenarios["same_game_duplicates"]["expected_guest_duplicates"] == [
        ["g1", "Guest-0001", 2]
    ]
    assert scenarios["different_games"]["expected_member_duplicates"] == []
    assert scenarios["different_games"]["expected_identity_duplicates"] == []
    assert scenarios["completed_game"]["expected_active"] == []
    for scenario in scenarios.values():
        rows = scenario["participants"]
        actual_active = [
            [
                game["game_id"],
                sum(row["game_id"] == game["game_id"] for row in rows),
                sum(
                    row["game_id"] == game["game_id"] and row["participant_id"] is None
                    for row in rows
                ),
            ]
            for game in scenario["games"]
            if game["status"] == "IN_PROGRESS"
        ]
        assert actual_active == scenario["expected_active"], scenario["name"]
        for field, key in (
            ("member_id", "expected_member_duplicates"),
            ("guest_label", "expected_guest_duplicates"),
            ("participant_id", "expected_identity_duplicates"),
        ):
            counts = Counter((row["game_id"], row[field]) for row in rows if row[field] is not None)
            actual = [
                [game, value, count] for (game, value), count in sorted(counts.items()) if count > 1
            ]
            assert actual == scenario[key], (scenario["name"], key)
