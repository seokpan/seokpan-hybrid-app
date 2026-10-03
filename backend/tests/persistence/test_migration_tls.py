import asyncio
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from alembic import command
from alembic.config import Config

from seokpan.persistence.mariadb.connection import (
    DatabaseConfigurationError,
    DatabaseConnectionError,
)
from seokpan.persistence.mariadb.migration_gate import MigrationGateError, main, run

MODULE = "seokpan.persistence.mariadb.connection"
GATE = "seokpan.persistence.mariadb.migration_gate"
ROOT = Path(__file__).resolve().parents[2]
URL = "mysql+asyncmy://db_admin:synthetic-sensitive@db.seokpan.soldesk.store:3306/stone_game"


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SSLKEYLOGFILE", raising=False)
    monkeypatch.setenv("SEOKPAN_MIGRATION_DATABASE_URL", URL)
    monkeypatch.setenv("SEOKPAN_DATABASE_CA_FILE", "missing-public-ca.crt")


@pytest.mark.parametrize("action", ["current", "stamp-baseline", "upgrade-head"])
def test_gate_checks_ca_before_any_runner(action: str) -> None:
    runner = MagicMock()
    args = [action, "--expect-host", "db.seokpan.soldesk.store", "--expect-database", "stone_game"]
    if action != "current":
        args.extend(["--execute", "--approval-ref", "approved-synthetic-reference"])
    with pytest.raises(DatabaseConfigurationError):
        run(args, runner=runner)
    assert runner.mock_calls == []


def test_matching_but_unofficial_target_cannot_bypass_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "SEOKPAN_MIGRATION_DATABASE_URL", URL.replace("db.seokpan.soldesk.store", "wrong.example")
    )
    with pytest.raises(MigrationGateError, match="official"):
        run(["current", "--expect-host", "wrong.example", "--expect-database", "stone_game"])


def test_direct_alembic_online_checks_ca_before_engine_creation() -> None:
    with (
        patch(MODULE + ".create_async_engine") as create,
        pytest.raises(DatabaseConfigurationError),
    ):
        command.current(Config(ROOT / "alembic.ini"))
    create.assert_not_called()


def test_offline_does_not_load_ca_or_create_engine() -> None:
    output = StringIO()
    with (
        patch(MODULE + ".database_ssl_context", side_effect=AssertionError("must not load CA")),
        patch(
            MODULE + ".create_async_engine", side_effect=AssertionError("must not create engine")
        ),
        redirect_stdout(output),
    ):
        command.upgrade(Config(ROOT / "alembic.ini"), "head", sql=True)
    assert "CREATE TABLE member" in output.getvalue()
    assert "synthetic-sensitive" not in output.getvalue()


@pytest.mark.parametrize("failure", [RuntimeError("synthetic-sensitive"), asyncio.CancelledError()])
def test_online_failure_disposes_engine_and_preserves_cancellation(failure: BaseException) -> None:
    engine = MagicMock()
    engine.dispose = AsyncMock()
    engine.connect.return_value.__aenter__ = AsyncMock(side_effect=failure)
    expected = (
        asyncio.CancelledError
        if isinstance(failure, asyncio.CancelledError)
        else DatabaseConnectionError
    )
    with (
        patch(MODULE + ".create_migration_engine", return_value=engine),
        pytest.raises(expected) as error,
    ):
        command.current(Config(ROOT / "alembic.ini"))
    engine.dispose.assert_awaited_once()
    assert "synthetic-sensitive" not in str(error.value)


def test_dispose_failure_does_not_expose_driver_error() -> None:
    engine = MagicMock()
    engine.dispose = AsyncMock(side_effect=RuntimeError("synthetic-sensitive"))
    engine.connect.return_value.__aenter__ = AsyncMock(side_effect=RuntimeError("connect failed"))
    with (
        patch(MODULE + ".create_migration_engine", return_value=engine),
        pytest.raises(DatabaseConnectionError) as error,
    ):
        command.current(Config(ROOT / "alembic.ini"))
    assert "synthetic-sensitive" not in str(error.value)


def test_cli_malformed_url_returns_safe_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SEOKPAN_MIGRATION_DATABASE_URL", "synthetic-sensitive-malformed")
    monkeypatch.setattr(
        "sys.argv",
        [
            "gate",
            "current",
            "--expect-host",
            "db.seokpan.soldesk.store",
            "--expect-database",
            "stone_game",
        ],
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert "synthetic-sensitive" not in captured.out + captured.err
    assert "migration gate refused" in captured.err


def test_missing_settings_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEOKPAN_MIGRATION_DATABASE_URL")
    with pytest.raises(DatabaseConfigurationError, match="settings"):
        command.current(Config(ROOT / "alembic.ini"))
    with pytest.raises(MigrationGateError, match="settings"):
        run(
            [
                "current",
                "--expect-host",
                "db.seokpan.soldesk.store",
                "--expect-database",
                "stone_game",
            ]
        )


@pytest.mark.parametrize(
    ("action", "method", "arguments"),
    [
        ("current", "current", {"verbose": True}),
        ("stamp-baseline", "stamp", {}),
        ("upgrade-head", "upgrade", {}),
    ],
)
def test_default_runner_keeps_command_mapping_without_db(
    monkeypatch: pytest.MonkeyPatch, action: str, method: str, arguments: dict[str, object]
) -> None:
    monkeypatch.setenv(
        "SEOKPAN_DATABASE_CA_FILE", str(Path(__file__).parent / "fixtures/public-ca.crt")
    )
    args = [action, "--expect-host", "db.seokpan.soldesk.store", "--expect-database", "stone_game"]
    if action != "current":
        args += ["--execute", "--approval-ref", "synthetic-approved-reference"]
    with patch(GATE + ".command." + method) as invoke:
        assert run(args, stdout=StringIO()) == 0
    assert invoke.call_count == 1
    assert invoke.call_args.kwargs == arguments
    assert (
        invoke.call_args.args[1:]
        == {"current": (), "stamp-baseline": ("20260901_0001",), "upgrade-head": ("head",)}[action]
    )


def test_cli_success_exit_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    with patch(GATE + ".run", return_value=0), pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 0
