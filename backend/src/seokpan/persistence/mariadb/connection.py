"""Validated DB connection construction; no network I/O until Engine checkout."""

from __future__ import annotations

import os
import ssl
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, urlsplit

from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from seokpan.connection_contract import ConnectionContractError, DatabaseTarget
from seokpan.connection_settings import DatabaseTargetSettings
from seokpan.persistence.mariadb.settings import MigrationSettings
from seokpan.settings import Settings

DATABASE_HOST = "db.seokpan.soldesk.store"
DATABASE_PORT = 3306
DATABASE_NAME = "stone_game"
DATABASE_DRIVER = "mysql+asyncmy"
DatabaseAccount = Literal["identity_svc", "game_svc", "db_admin"]


class DatabaseConfigurationError(ValueError):
    """Safe configuration failure: never include caller input or a raw driver exception."""


class DatabaseConnectionError(RuntimeError):
    """Safe online migration failure; inspect separately controlled DB diagnostics."""


def configured_database_target(settings: DatabaseTargetSettings) -> DatabaseTarget | None:
    if settings.connection_profile == "legacy":
        return None
    try:
        return DatabaseTarget.from_fields(
            settings.database_expected_host,
            settings.database_expected_port,
            settings.database_expected_name,
        )
    except ConnectionContractError as error:
        raise DatabaseConfigurationError(str(error)) from None


def validated_database_url(
    raw_url: str | None, account: DatabaseAccount, target: DatabaseTarget | None = None
) -> URL:
    if not raw_url or "#" in raw_url or any(ord(char) < 32 or ord(char) == 127 for char in raw_url):
        raise DatabaseConfigurationError("database URL is missing or malformed")
    try:
        parts = urlsplit(raw_url)
        query = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
        url = make_url(raw_url)
        port = DATABASE_PORT if url.port is None else url.port
    except (ValueError, TypeError, SQLAlchemyError):
        raise DatabaseConfigurationError("database URL is malformed") from None

    if query not in ([], [("charset", "utf8mb4")]):
        raise DatabaseConfigurationError("database URL options are not permitted")
    if url.drivername != DATABASE_DRIVER:
        raise DatabaseConfigurationError(f"database URL must use {DATABASE_DRIVER}")
    if url.username != account:
        raise DatabaseConfigurationError(f"database URL must use the {account} account")
    expected_host = DATABASE_HOST if target is None else target.host
    expected_port = DATABASE_PORT if target is None else target.port
    expected_name = DATABASE_NAME if target is None else target.database
    if url.host != expected_host or port != expected_port or url.database != expected_name:
        raise DatabaseConfigurationError(
            "database URL must use the official host, port and database"
            if target is None
            else "database URL must match the approved host, port and database"
        )
    if target is not None and (
        not url.password
        or any(ord(char) < 32 or ord(char) == 127 for char in url.password)
        or raw_url != raw_url.strip()
    ):
        raise DatabaseConfigurationError("database URL credentials are missing or malformed")
    return url


def database_ssl_context(ca_file: str | None) -> ssl.SSLContext:
    if "SSLKEYLOGFILE" in os.environ:
        raise DatabaseConfigurationError(
            "TLS key logging is not permitted for database connections"
        )
    if not ca_file or not ca_file.strip():
        raise DatabaseConfigurationError("SEOKPAN_DATABASE_CA_FILE is required for online DB use")
    try:
        if not Path(ca_file).is_file():
            raise OSError
        # Explicit cafile prevents fallback to the OS trust store. Keep Python's strict defaults.
        return ssl.create_default_context(cafile=ca_file)
    except (OSError, ValueError):
        raise DatabaseConfigurationError("database CA file is unreadable or invalid") from None


def _engine(url: URL, context: ssl.SSLContext, *, migration: bool) -> AsyncEngine:
    try:
        if migration:
            return create_async_engine(
                url,
                connect_args={"ssl": context},
                poolclass=NullPool,
                hide_parameters=True,
            )
        return create_async_engine(
            url,
            connect_args={"ssl": context},
            pool_pre_ping=True,
            hide_parameters=True,
        )
    except (ValueError, TypeError, SQLAlchemyError):
        raise DatabaseConfigurationError("database engine configuration failed") from None


def create_migration_engine(settings: MigrationSettings) -> AsyncEngine:
    url = validated_database_url(
        settings.migration_database_url, "db_admin", configured_database_target(settings)
    )
    return _engine(url, database_ssl_context(settings.database_ca_file), migration=True)


@dataclass(frozen=True, slots=True)
class RuntimeDatabases:
    """One pair per application lifetime; factories match the existing Adapter interfaces."""

    identity: AsyncEngine = field(repr=False)
    game: AsyncEngine = field(repr=False)
    identity_sessions: async_sessionmaker[AsyncSession] = field(repr=False)
    game_sessions: async_sessionmaker[AsyncSession] = field(repr=False)


@asynccontextmanager
async def runtime_databases(settings: Settings) -> AsyncIterator[RuntimeDatabases]:
    target = configured_database_target(settings)
    identity_url = validated_database_url(settings.identity_database_url, "identity_svc", target)
    game_url = validated_database_url(settings.game_database_url, "game_svc", target)
    context = database_ssl_context(settings.database_ca_file)
    async with AsyncExitStack() as cleanup:
        identity = _engine(identity_url, context, migration=False)
        cleanup.push_async_callback(identity.dispose)
        game = _engine(game_url, context, migration=False)
        cleanup.push_async_callback(game.dispose)
        yield RuntimeDatabases(
            identity,
            game,
            async_sessionmaker(identity, expire_on_commit=False),
            async_sessionmaker(game, expire_on_commit=False),
        )
