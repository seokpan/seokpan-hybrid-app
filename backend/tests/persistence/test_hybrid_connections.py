"""Hybrid allowlist and pinned-driver tests without Cloud resources."""

import asyncio
import ssl
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError
from sqlalchemy import event

from seokpan.connection_contract import (
    ConnectionContractError,
    DatabaseTarget,
    parse_hybrid_redis_url,
    strict_ca_context,
    validated_redis_auth_token,
)
from seokpan.persistence.mariadb.connection import (
    DatabaseConfigurationError,
    configured_database_target,
    create_migration_engine,
    runtime_databases,
    validated_database_url,
)
from seokpan.persistence.mariadb.migration_gate import MigrationGateError, run
from seokpan.persistence.mariadb.settings import MigrationSettings
from seokpan.persistence.redis.connection import (
    ExplicitCATLSConnection,
    RedisConfigurationError,
    runtime_redis,
)
from seokpan.settings import Settings

CA = Path(__file__).parent / "fixtures/public-ca.crt"
DB_HOST = "approved-db.example.test"
REDIS_HOST = "approved-primary.example.test"
TOKEN = "synthetic-secret-do-not-log"
DB_URL = f"mysql+asyncmy://identity_svc:synthetic-only@{DB_HOST}:3307/approved_game"
REDIS_URL = f"rediss://{REDIS_HOST}:6380/0"
REDIS_MODULE = "seokpan.persistence.redis.connection"


@pytest.fixture(autouse=True)
def no_key_log(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SSLKEYLOGFILE", raising=False)


def settings(profile: str = "cloud") -> Settings:
    return Settings(
        connection_profile=profile,  # type: ignore[arg-type]
        database_expected_host=DB_HOST,
        database_expected_port=3307,
        database_expected_name="approved_game",
        identity_database_url=DB_URL,
        game_database_url=DB_URL.replace("identity_svc", "game_svc"),
        database_ca_file=str(CA),
        redis_expected_host=REDIS_HOST,
        redis_expected_port=6380,
        redis_expected_database=0,
        redis_url=REDIS_URL,
        redis_auth_token=TOKEN,
        redis_ca_file=str(CA),
    )


@pytest.mark.parametrize("profile", ["cloud", "lab", "recovery"])
def test_nonlegacy_profiles_use_exact_explicit_targets(profile: str) -> None:
    configured = settings(profile)
    target = configured_database_target(configured)
    assert target == DatabaseTarget(DB_HOST, 3307, "approved_game")
    assert validated_database_url(DB_URL, "identity_svc", target).host == DB_HOST
    redis = parse_hybrid_redis_url(REDIS_URL, REDIS_HOST, 6380, 0)
    assert (redis.host, redis.port, redis.database) == (REDIS_HOST, 6380, 0)


def test_legacy_cannot_allow_new_target_from_config() -> None:
    configured = settings("legacy")
    assert configured_database_target(configured) is None
    with pytest.raises(DatabaseConfigurationError, match="official"):
        validated_database_url(DB_URL, "identity_svc", configured_database_target(configured))


@pytest.mark.parametrize(
    "field", ["database_expected_host", "database_expected_port", "database_expected_name"]
)
def test_missing_db_target_fails_before_engine_creation(field: str) -> None:
    configured = settings()
    setattr(configured, field, None)
    with pytest.raises(DatabaseConfigurationError):
        configured_database_target(configured)


@pytest.mark.parametrize(
    "raw",
    [
        DB_URL.replace(DB_HOST, "other.example.test"),
        DB_URL.replace(":3307", ":3306"),
        DB_URL.replace("approved_game", "other_game"),
        DB_URL.replace("identity_svc", "db_admin"),
        DB_URL.replace("mysql+asyncmy", "mysql+pymysql"),
        DB_URL.replace(":synthetic-only", ""),
        DB_URL.replace("synthetic-only", "%0Asensitive-value"),
        DB_URL + "?ssl=false",
        DB_URL + "?charset=utf8mb4&charset=utf8mb4",
        DB_URL + "#private-fragment",
    ],
)
def test_hybrid_db_mismatch_is_redacted(raw: str) -> None:
    with pytest.raises(DatabaseConfigurationError) as error:
        validated_database_url(raw, "identity_svc", configured_database_target(settings()))
    assert all(value not in str(error.value) for value in ["synthetic", "sensitive", DB_HOST])


@pytest.mark.asyncio
async def test_actual_asyncmy_engine_arguments_match_runtime_and_migration() -> None:
    class BeforeNetwork(Exception):
        pass

    observed: list[dict[str, object]] = []

    def capture(
        _dialect: object, _record: object, _args: object, params: dict[str, object]
    ) -> None:
        observed.append(dict(params))
        raise BeforeNetwork

    configured = settings()
    migration = create_migration_engine(
        MigrationSettings(
            connection_profile="cloud",
            database_expected_host=DB_HOST,
            database_expected_port=3307,
            database_expected_name="approved_game",
            migration_database_url=DB_URL.replace("identity_svc", "db_admin"),
            database_ca_file=str(CA),
        )
    )
    try:
        async with runtime_databases(configured) as databases:
            for engine in (databases.identity, databases.game, migration):
                event.listen(engine.sync_engine, "do_connect", capture)
                with pytest.raises(BeforeNetwork):
                    await engine.connect()
    finally:
        await migration.dispose()
    assert [item["user"] for item in observed] == ["identity_svc", "game_svc", "db_admin"]
    for item in observed:
        assert (item["host"], item["port"], item["db"]) == (DB_HOST, 3307, "approved_game")
        context = item["ssl"]
        assert isinstance(context, ssl.SSLContext)
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED


def test_environment_target_is_not_bypassed_by_migration_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in {
        "CONNECTION_PROFILE": "cloud",
        "DATABASE_EXPECTED_HOST": DB_HOST,
        "DATABASE_EXPECTED_PORT": "3307",
        "DATABASE_EXPECTED_NAME": "approved_game",
        "DATABASE_CA_FILE": str(CA),
        "MIGRATION_DATABASE_URL": DB_URL.replace("identity_svc", "db_admin"),
    }.items():
        monkeypatch.setenv("SEOKPAN_" + name, value)
    runner = MagicMock()
    args = [
        "current",
        "--expect-host",
        DB_HOST,
        "--expect-port",
        "3307",
        "--expect-database",
        "approved_game",
    ]
    assert run(args, runner=runner, stdout=StringIO()) == 0
    runner.current.assert_called_once()
    runner.reset_mock()
    monkeypatch.setenv("SEOKPAN_DATABASE_EXPECTED_HOST", "different.example.test")
    with pytest.raises(MigrationGateError, match="approved"):
        run(args, runner=runner, stdout=StringIO())
    runner.current.assert_not_called()


@pytest.mark.parametrize(
    "raw",
    [
        REDIS_URL.replace("rediss", "redis"),
        REDIS_URL.replace("rediss://", "rediss://:synthetic-secret@"),
        REDIS_URL.replace("rediss://", "rediss://user@"),
        REDIS_URL.replace(REDIS_HOST, "replica.example.test"),
        REDIS_URL.replace(":6380", ":6379"),
        REDIS_URL.replace("/0", "/1"),
        REDIS_URL + "?ssl_check_hostname=false",
        REDIS_URL + "#sensitive-value",
        REDIS_URL + "\n",
    ],
)
def test_hybrid_redis_rejects_plaintext_url_auth_and_unapproved_targets(raw: str) -> None:
    with pytest.raises(ConnectionContractError) as error:
        parse_hybrid_redis_url(raw, REDIS_HOST, 6380, 0)
    assert all(value not in str(error.value) for value in ["synthetic", "sensitive", REDIS_HOST])


@pytest.mark.parametrize("token", [None, "", "secret\nvalue", "secret\0value"])
def test_missing_or_invalid_separate_redis_auth_rejected(token: str | None) -> None:
    with pytest.raises(ConnectionContractError) as error:
        validated_redis_auth_token(token)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    "field",
    [
        "redis_expected_host",
        "redis_expected_port",
        "redis_expected_database",
        "redis_auth_token",
        "redis_ca_file",
    ],
)
@pytest.mark.asyncio
async def test_hybrid_redis_missing_input_fails_before_client_creation(field: str) -> None:
    configured = settings()
    setattr(configured, field, None)
    with patch(REDIS_MODULE + ".Redis") as create, pytest.raises(RedisConfigurationError):
        async with runtime_redis(configured):
            pytest.fail("must not enter")
    create.assert_not_called()


@pytest.mark.asyncio
async def test_actual_redis_pool_uses_separate_auth_and_only_explicit_tls_context() -> None:
    configured = settings()
    async with runtime_redis(configured) as client:
        assert TOKEN not in repr(client)
        pool = client.connection_pool
        connection = pool.make_connection()
        try:
            assert isinstance(connection, ExplicitCATLSConnection)
            assert connection.password == TOKEN and connection.username is None
            assert (connection.host, connection.port, connection.db) == (REDIS_HOST, 6380, 0)
            with patch.object(
                type(connection.ssl_context), "get", side_effect=AssertionError("OS trust")
            ):
                arguments = connection._connection_arguments()
            assert arguments["host"] == REDIS_HOST
            context = arguments["ssl"]
            assert isinstance(context, ssl.SSLContext)
            assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
            assert context.verify_flags & ssl.VERIFY_X509_STRICT
            assert context.cert_store_stats()["x509_ca"] == 1
        finally:
            await connection.disconnect()
    assert "strict_context" in pool.connection_kwargs


@pytest.mark.parametrize("failure", [RuntimeError("synthetic"), asyncio.CancelledError()])
@pytest.mark.asyncio
async def test_hybrid_client_and_owned_pool_close_on_error_or_cancellation(
    failure: BaseException,
) -> None:
    client, pool = AsyncMock(), AsyncMock()
    with (
        patch(REDIS_MODULE + ".Redis", return_value=client),
        patch(REDIS_MODULE + ".ConnectionPool", return_value=pool),
        pytest.raises(type(failure)),
    ):
        async with runtime_redis(settings()):
            raise failure
    client.aclose.assert_awaited_once()
    pool.aclose.assert_awaited_once()


def test_profile_and_secrets_are_safe_in_settings_errors_and_repr() -> None:
    configured = settings()
    assert TOKEN not in repr(configured) and "synthetic-only" not in repr(configured)
    assert str(CA) not in repr(configured)
    migration = MigrationSettings(
        migration_database_url=DB_URL.replace("identity_svc", "db_admin"),
        database_ca_file=str(CA),
    )
    assert str(CA) not in repr(migration) and "synthetic-only" not in repr(migration)
    with pytest.raises(ValidationError) as error:
        Settings(connection_profile=TOKEN)  # type: ignore[arg-type]
    assert TOKEN not in str(error.value)


@pytest.mark.parametrize("ca", [None, "", "missing-private-ca.crt"])
def test_redis_explicit_ca_required_without_os_fallback(ca: str | None) -> None:
    with pytest.raises(ConnectionContractError):
        strict_ca_context(ca)


def test_redis_tls_key_logging_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSLKEYLOGFILE", "sensitive-path")
    with pytest.raises(ConnectionContractError, match="key logging") as error:
        strict_ca_context(str(CA))
    assert "sensitive" not in str(error.value)


def test_redis_malformed_ca_is_redacted(tmp_path: Path) -> None:
    ca = tmp_path / "private-ca.crt"
    ca.write_text("sensitive-malformed-ca", encoding="ascii")
    with pytest.raises(ConnectionContractError) as error:
        strict_ca_context(str(ca))
    assert "sensitive" not in str(error.value) and "private-ca" not in str(error.value)
