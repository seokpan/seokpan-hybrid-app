from io import StringIO
from pathlib import Path

import pytest
from alembic.config import Config

from seokpan.persistence.mariadb.migration_gate import MigrationGateError, run

DATABASE_URL = "mysql+asyncmy://db_admin:do-not-print@db.seokpan.soldesk.store:3306/stone_game"


@pytest.fixture(autouse=True)
def public_ca(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SSLKEYLOGFILE", raising=False)
    monkeypatch.setenv(
        "SEOKPAN_DATABASE_CA_FILE", str(Path(__file__).parent / "fixtures/public-ca.crt")
    )


class FakeRunner:
    def __init__(self) -> None:
        self.actions: list[str] = []

    def current(self, config: Config) -> None:
        assert config.config_file_name is not None
        self.actions.append("current")

    def stamp_baseline(self, config: Config) -> None:
        assert config.config_file_name is not None
        self.actions.append("stamp-baseline")

    def upgrade_head(self, config: Config) -> None:
        assert config.config_file_name is not None
        self.actions.append("upgrade-head")


def base_args(action: str) -> list[str]:
    return [
        action,
        "--expect-host",
        "db.seokpan.soldesk.store",
        "--expect-database",
        "stone_game",
    ]


def test_current_is_read_only_and_redacts_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SEOKPAN_MIGRATION_DATABASE_URL", DATABASE_URL)
    runner = FakeRunner()
    output = StringIO()

    assert run(base_args("current"), runner=runner, stdout=output) == 0

    assert runner.actions == ["current"]
    text = output.getvalue()
    assert "db.seokpan.soldesk.store:3306/stone_game" in text
    assert "db_admin" not in text
    assert "do-not-print" not in text


@pytest.mark.parametrize("action", ["stamp-baseline", "upgrade-head"])
def test_mutation_requires_execute_and_approval_reference(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    monkeypatch.setenv("SEOKPAN_MIGRATION_DATABASE_URL", DATABASE_URL)
    runner = FakeRunner()

    with pytest.raises(MigrationGateError, match="--execute"):
        run(base_args(action), runner=runner)

    with pytest.raises(MigrationGateError, match="--approval-ref"):
        run([*base_args(action), "--execute"], runner=runner)

    assert runner.actions == []


@pytest.mark.parametrize("action", ["stamp-baseline", "upgrade-head"])
def test_approved_mutation_invokes_only_the_selected_action(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    monkeypatch.setenv("SEOKPAN_MIGRATION_DATABASE_URL", DATABASE_URL)
    runner = FakeRunner()

    result = run(
        [*base_args(action), "--execute", "--approval-ref", "seokpan-app#22-runtime-gate"],
        runner=runner,
        stdout=StringIO(),
    )

    assert result == 0
    assert runner.actions == [action]


@pytest.mark.parametrize(
    ("changed_args", "message"),
    [
        (["--expect-host", "wrong.example"], "host mismatch"),
        (["--expect-port", "3307"], "port mismatch"),
        (["--expect-database", "wrong_db"], "name mismatch"),
    ],
)
def test_target_mismatch_refuses_before_alembic(
    monkeypatch: pytest.MonkeyPatch,
    changed_args: list[str],
    message: str,
) -> None:
    monkeypatch.setenv("SEOKPAN_MIGRATION_DATABASE_URL", DATABASE_URL)
    args = base_args("current")
    option = changed_args[0]
    option_index = args.index(option) if option in args else -1
    if option_index >= 0:
        args[option_index + 1] = changed_args[1]
    else:
        args.extend(changed_args)
    runner = FakeRunner()

    with pytest.raises(MigrationGateError, match=message):
        run(args, runner=runner)

    assert runner.actions == []


def test_missing_alembic_config_refuses_before_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SEOKPAN_MIGRATION_DATABASE_URL", DATABASE_URL)
    runner = FakeRunner()

    with pytest.raises(MigrationGateError, match="config does not exist"):
        run([*base_args("current"), "--config", "missing-alembic.ini"], runner=runner)

    assert runner.actions == []


@pytest.mark.parametrize(
    ("database_url", "message"),
    [
        (
            "postgresql+asyncpg://db_admin@db.seokpan.soldesk.store/stone_game",
            "must use mysql\\+asyncmy",
        ),
        (
            "mysql+asyncmy://game_svc@db.seokpan.soldesk.store/stone_game",
            "must use the db_admin account",
        ),
    ],
)
def test_wrong_driver_or_runtime_account_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    database_url: str,
    message: str,
) -> None:
    monkeypatch.setenv("SEOKPAN_MIGRATION_DATABASE_URL", database_url)
    runner = FakeRunner()

    with pytest.raises(MigrationGateError, match=message):
        run(base_args("current"), runner=runner)

    assert runner.actions == []
