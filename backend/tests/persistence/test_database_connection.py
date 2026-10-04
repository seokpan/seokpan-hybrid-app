import asyncio
import ssl
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import quote

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from seokpan.persistence.mariadb.connection import (
    DatabaseConfigurationError,
    create_migration_engine,
    database_ssl_context,
    runtime_databases,
    validated_database_url,
)
from seokpan.persistence.mariadb.settings import MigrationSettings
from seokpan.settings import Settings

CA = Path(__file__).parent / "fixtures/public-ca.crt"
MODULE = "seokpan.persistence.mariadb.connection"
BASE = "mysql+asyncmy://identity_svc:synthetic-only@db.seokpan.soldesk.store:3306/stone_game"


@pytest.fixture(autouse=True)
def no_key_log(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SSLKEYLOGFILE", raising=False)


def settings() -> Settings:
    return Settings(
        identity_database_url=BASE,
        game_database_url=BASE.replace("identity_svc", "game_svc"),
        database_ca_file=str(CA),
    )


@pytest.mark.parametrize("suffix", ["", "?charset=utf8mb4"])
def test_valid_url_preserves_encoded_password(suffix: str) -> None:
    password = "synthetic:p@ss/%?+#"
    raw = BASE.replace("synthetic-only", quote(password, safe="")) + suffix
    url = validated_database_url(raw, "identity_svc")
    assert url.password == password
    assert url.host == "db.seokpan.soldesk.store"
    assert validated_database_url(raw.replace(":3306/", "/"), "identity_svc").port is None


@pytest.mark.parametrize(
    "query",
    [
        "ssl=false",
        "ssl_ca=secret",
        "ssl_disabled=true",
        "ssl_check_hostname=false",
        "host=wrong",
        "user=db_admin",
        "db=wrong",
        "port=3307",
        "password=secret",
        "unix_socket=/wrong",
        "read_default_file=wrong",
        "read_default_group=client",
        "charset=",
        "charset=latin1",
        "charset=utf8mb4&charset=utf8mb4",
        "unknown=value",
        "SSL=false",
        "charset=utf8mb4&ssl=",
        "option-without-value",
    ],
)
def test_query_options_fail_closed(query: str) -> None:
    with pytest.raises(DatabaseConfigurationError):
        validated_database_url(BASE + "?" + query, "identity_svc")


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "malformed-sensitive-url",
        BASE + "\n",
        BASE + "#fragment",
        BASE.replace("mysql+asyncmy", "mysql+pymysql"),
        BASE.replace("identity_svc", "db_admin"),
        BASE.replace("3306", "0"),
        BASE.replace("3306", "3307"),
        BASE.replace("3306", "invalid-sensitive-port"),
        BASE.replace("db.seokpan.soldesk.store", "10.1.93.90"),
        BASE.replace("stone_game", "wrong"),
        BASE.replace("/stone_game", "//stone_game"),
    ],
)
def test_invalid_url_does_not_echo_input(raw: str | None) -> None:
    with pytest.raises(DatabaseConfigurationError) as error:
        validated_database_url(raw, "identity_svc")
    assert "synthetic-only" not in str(error.value)
    assert "sensitive" not in str(error.value)


def test_context_preserves_strict_validation_and_only_explicit_ca() -> None:
    context = database_ssl_context(str(CA))
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname
    assert context.verify_flags & ssl.VERIFY_X509_STRICT
    assert context.cert_store_stats()["x509_ca"] == 1
    assert context.keylog_filename is None


@pytest.mark.parametrize("value", [None, "", " ", "not-present.crt"])
def test_ca_missing_or_invalid_path(value: str | None) -> None:
    with pytest.raises(DatabaseConfigurationError):
        database_ssl_context(value)


def test_invalid_and_unreadable_ca(tmp_path: Path) -> None:
    malformed = tmp_path / "invalid.crt"
    malformed.write_text("not a certificate", encoding="ascii")
    with pytest.raises(DatabaseConfigurationError):
        database_ssl_context(str(malformed))
    with (
        patch(MODULE + ".ssl.create_default_context", side_effect=PermissionError("sensitive")),
        pytest.raises(DatabaseConfigurationError, match="unreadable or invalid") as error,
    ):
        database_ssl_context(str(CA))
    assert "sensitive" not in str(error.value)
    assert error.value.__suppress_context__


def test_key_logging_rejected_before_context_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSLKEYLOGFILE", "synthetic-sensitive-path")
    with (
        patch(MODULE + ".ssl.create_default_context") as create,
        pytest.raises(DatabaseConfigurationError, match="key logging"),
    ):
        database_ssl_context(str(CA))
    create.assert_not_called()


@pytest.mark.asyncio
async def test_runtime_engines_are_separate_pooled_and_reuse_one_context() -> None:
    with patch(
        MODULE + ".create_async_engine",
        wraps=create_async_engine,
    ) as create:
        async with runtime_databases(settings()) as databases:
            assert databases.identity is not databases.game
            assert databases.identity.url.username == "identity_svc"
            assert databases.game.url.username == "game_svc"
            assert databases.identity_sessions.kw["bind"] is databases.identity
            assert databases.game_sessions.kw["bind"] is databases.game
            assert "synthetic-only" not in repr(databases)
            assert create.call_count == 2
            options = [call.kwargs for call in create.call_args_list]
            assert options[0]["connect_args"]["ssl"] is options[1]["connect_args"]["ssl"]
            for option in options:
                assert option["pool_pre_ping"] is True
                assert option["hide_parameters"] is True
                assert "pool_recycle" not in option


@pytest.mark.asyncio
async def test_migration_has_null_pool_and_common_tls() -> None:
    engine = create_migration_engine(
        MigrationSettings(
            migration_database_url=BASE.replace("identity_svc", "db_admin"),
            database_ca_file=str(CA),
        )
    )
    try:
        assert isinstance(engine.pool, NullPool)
        assert engine.sync_engine.hide_parameters
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_final_driver_arguments_keep_tls_target_and_role_without_network() -> None:
    class StopBeforeNetwork(Exception):
        pass

    observed: list[dict[str, object]] = []

    def capture(
        _dialect: object, _record: object, _args: object, params: dict[str, object]
    ) -> None:
        observed.append(dict(params))
        raise StopBeforeNetwork

    configured = settings()
    configured.identity_database_url = BASE + "?charset=utf8mb4"
    async with runtime_databases(configured) as databases:
        migration = create_migration_engine(
            MigrationSettings(
                migration_database_url=BASE.replace("identity_svc", "db_admin"),
                database_ca_file=str(CA),
            )
        )
        try:
            for engine in (databases.identity, databases.game, migration):
                event.listen(engine.sync_engine, "do_connect", capture)
                with pytest.raises(StopBeforeNetwork):
                    await engine.connect()
        finally:
            await migration.dispose()
    assert [params["user"] for params in observed] == ["identity_svc", "game_svc", "db_admin"]
    for params in observed:
        assert params["host"] == "db.seokpan.soldesk.store"
        assert params["port"] == 3306
        assert params["db"] == "stone_game"
        context = params["ssl"]
        assert isinstance(context, ssl.SSLContext)
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        assert not {"unix_socket", "read_default_file", "ssl_disabled"} & params.keys()
    assert observed[0]["charset"] == "utf8mb4"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("synthetic"), asyncio.CancelledError()])
async def test_runtime_cleanup_on_error_or_cancellation(failure: BaseException) -> None:
    first, second = AsyncMock(), AsyncMock()
    with patch(MODULE + "._engine", side_effect=[first, second]), pytest.raises(type(failure)):
        async with runtime_databases(settings()):
            raise failure
    first.dispose.assert_awaited_once()
    second.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_first_engine_cleaned_if_second_creation_fails() -> None:
    first = AsyncMock()
    with (
        patch(MODULE + "._engine", side_effect=[first, DatabaseConfigurationError("failed")]),
        pytest.raises(DatabaseConfigurationError),
    ):
        async with runtime_databases(settings()):
            pytest.fail("must not enter")
    first.dispose.assert_awaited_once()


def test_ca_is_not_required_by_settings_and_urls_are_not_in_repr() -> None:
    configured = settings()
    assert "synthetic-only" not in repr(configured)
    assert Settings(database_ca_file=None).database_ca_file is None
    assert "migration_database_url" not in Settings.model_fields


@pytest.mark.asyncio
async def test_invalid_engine_configuration_is_redacted() -> None:
    with (
        patch(MODULE + ".create_async_engine", side_effect=ValueError("synthetic-sensitive")),
        pytest.raises(DatabaseConfigurationError) as error,
    ):
        async with runtime_databases(settings()):
            pytest.fail("must not enter")
    assert "synthetic-sensitive" not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.asyncio
async def test_both_engines_disposed_even_when_one_cleanup_fails() -> None:
    first, second = AsyncMock(), AsyncMock()
    second.dispose.side_effect = RuntimeError("cleanup failed")
    with patch(MODULE + "._engine", side_effect=[first, second]), pytest.raises(RuntimeError):
        async with runtime_databases(settings()):
            pass
    first.dispose.assert_awaited_once()
    second.dispose.assert_awaited_once()
