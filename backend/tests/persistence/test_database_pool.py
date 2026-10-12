from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.util import greenlet_spawn

from seokpan.persistence.mariadb.connection import (
    DatabaseConfigurationError,
    create_migration_engine,
    runtime_databases,
)
from seokpan.persistence.mariadb.settings import MigrationSettings
from seokpan.settings import Settings

CA = Path(__file__).parent / "fixtures/public-ca.crt"
MODULE = "seokpan.persistence.mariadb.connection"
BASE = "mysql+asyncmy://identity_svc:synthetic-only@db.seokpan.soldesk.store:3306/stone_game"


def settings() -> Settings:
    return Settings(
        identity_database_url=BASE,
        game_database_url=BASE.replace("identity_svc", "game_svc"),
        database_ca_file=str(CA),
    )


@pytest.fixture(autouse=True)
def isolated_pool_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "SSLKEYLOGFILE",
        "SEOKPAN_DATABASE_POOL_SIZE",
        "SEOKPAN_DATABASE_MAX_OVERFLOW",
        "SEOKPAN_DATABASE_POOL_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)


def configured() -> Settings:
    return Settings(
        identity_database_url=BASE,
        game_database_url=BASE.replace("identity_svc", "game_svc"),
        database_ca_file=str(CA),
        # Synthetic fixture only; not an adopted deployment configuration.
        database_pool_size=2,
        database_max_overflow=1,
        database_pool_timeout_seconds=0.02,
    )


def test_environment_bundle_consumed_and_prefix_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEOKPAN_DATABASE_POOL_SIZE", "2")
    monkeypatch.setenv("SEOKPAN_DATABASE_MAX_OVERFLOW", "0")
    monkeypatch.setenv("SEOKPAN_DATABASE_POOL_TIMEOUT_SECONDS", "0.25")
    monkeypatch.setenv("SEOKPAN_INSTANCE_ID", "fixture-instance")
    value = Settings()
    assert value.engine_options() == {"pool_size": 2, "max_overflow": 0, "pool_timeout": 0.25}
    assert value.instance_id == "fixture-instance"
    assert value.model_config["hide_input_in_errors"]


@pytest.mark.parametrize(
    "override",
    [
        {"database_pool_size": None},
        {"database_max_overflow": None},
        {"database_pool_timeout_seconds": None},
        {"database_pool_size": 0},
        {"database_pool_size": -1},
        {"database_max_overflow": -1},
        {"database_pool_size": True},
        {"database_max_overflow": False},
        {"database_pool_size": 2.0},
        {"database_pool_size": "1e2"},
        {"database_pool_size": "sensitive-fixture"},
        {"database_pool_timeout_seconds": 0},
        {"database_pool_timeout_seconds": -1},
        {"database_pool_timeout_seconds": True},
        {"database_pool_timeout_seconds": float("nan")},
        {"database_pool_timeout_seconds": float("inf")},
        {"database_pool_timeout_seconds": "sensitive-fixture"},
    ],
)
def test_invalid_inputs_are_rejected_without_echo(override: dict[str, object]) -> None:
    fields: dict[str, object] = {
        "database_pool_size": 2,
        "database_max_overflow": 1,
        "database_pool_timeout_seconds": 1,
    }
    fields.update(override)
    with pytest.raises(ValidationError) as error:
        Settings(**fields)  # type: ignore[arg-type]
    assert "sensitive-fixture" not in str(error.value)


@pytest.mark.asyncio
async def test_absent_bundle_preserves_constructor_defaults() -> None:
    with patch(MODULE + ".create_async_engine", wraps=create_async_engine) as create:
        async with runtime_databases(settings()) as databases:
            assert databases.identity.pool.size() == 5
            assert databases.game.pool.size() == 5
        for call in create.call_args_list:
            assert not {"pool_size", "max_overflow", "pool_timeout"} & call.kwargs.keys()


@pytest.mark.asyncio
async def test_explicit_bundle_reaches_both_pools_and_keeps_tls() -> None:
    with patch(MODULE + ".create_async_engine", wraps=create_async_engine) as create:
        async with runtime_databases(configured()) as databases:
            for engine in (databases.identity, databases.game):
                assert engine.pool.size() == 2
                assert engine.pool.timeout() == 0.02
                assert engine.sync_engine.hide_parameters
        assert create.call_count == 2
        for call in create.call_args_list:
            assert call.kwargs["pool_size"] == 2
            assert call.kwargs["max_overflow"] == 1
            assert call.kwargs["pool_pre_ping"] is True
            context = call.kwargs["connect_args"]["ssl"]
            assert context.check_hostname
            assert "pool_recycle" not in call.kwargs


@pytest.mark.asyncio
async def test_mutated_settings_rejected_before_engine_and_tls() -> None:
    value = configured()
    value.database_max_overflow = -1
    with (
        patch(MODULE + ".create_async_engine") as create,
        patch(MODULE + ".database_ssl_context") as tls,
        pytest.raises(DatabaseConfigurationError, match="pool configuration"),
    ):
        async with runtime_databases(value):
            pytest.fail("invalid settings must not enter runtime")
    create.assert_not_called()
    tls.assert_not_called()


@pytest.mark.asyncio
async def test_pool_revalidation_uses_captured_settings_without_target_env_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = configured()
    # A lifespan consumes its captured Settings; pool validation must not load
    # a second, unrelated target configuration from a later environment.
    monkeypatch.setenv("SEOKPAN_CONNECTION_PROFILE", "invalid-later-profile")
    monkeypatch.setenv("SEOKPAN_DATABASE_EXPECTED_PORT", "invalid-later-port")
    monkeypatch.setenv("SEOKPAN_DATABASE_POOL_SIZE", "invalid-later-pool")
    async with runtime_databases(value) as databases:
        assert databases.identity.pool.size() == 2
        assert databases.game.pool.size() == 2


@pytest.mark.asyncio
async def test_migration_ignores_runtime_pool_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEOKPAN_DATABASE_POOL_SIZE", "invalid-runtime-only")
    engine = create_migration_engine(
        MigrationSettings(
            migration_database_url=BASE.replace("identity_svc", "db_admin"),
            database_ca_file=str(CA),
        )
    )
    try:
        assert isinstance(engine.pool, NullPool)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_real_async_pool_cap_wait_timeout_and_reuse_without_database() -> None:
    # Run the actual bounded AsyncAdaptedQueuePool with a fake connection creator.
    # No DBAPI/network checkout is possible. This is not a RDS load test.
    async with runtime_databases(configured()) as databases:
        pool = databases.identity.pool
        created: list[object] = []
        closed: list[object] = []

        class Connection:
            def ping(self, reconnect: bool) -> None:
                pass

            def rollback(self) -> None:
                pass

            def close(self) -> None:
                closed.append(self)

        def creator() -> Connection:
            connection = Connection()
            created.append(connection)
            return connection

        pool._creator = creator
        # Engine dialect hooks expect a real DBAPI; disable them only in this fixture.
        pool.dispatch._clear()
        held = [await greenlet_spawn(pool.connect) for _ in range(3)]
        try:
            assert len(created) == 3
            with pytest.raises(PoolTimeoutError):
                await greenlet_spawn(pool.connect)
            assert len(created) == 3
            held.pop().close()
            reused = await greenlet_spawn(pool.connect)
            assert len(created) == 3
            reused.close()
        finally:
            for connection in held:
                connection.close()
        assert pool.checkedout() == 0
    assert len(closed) == 3
