import asyncio
from logging.config import fileConfig

from alembic import context
from pydantic import ValidationError
from sqlalchemy import Connection

from seokpan.persistence.mariadb.connection import (
    DatabaseConfigurationError,
    DatabaseConnectionError,
    configured_database_target,
    create_migration_engine,
    validated_database_url,
)
from seokpan.persistence.mariadb.models import Base
from seokpan.persistence.mariadb.settings import MigrationSettings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def migration_settings() -> MigrationSettings:
    try:
        return MigrationSettings()  # type: ignore[call-arg]
    except ValidationError:
        raise DatabaseConfigurationError("migration settings are missing or invalid") from None


def run_migrations_offline() -> None:
    settings = migration_settings()
    context.configure(
        url=validated_database_url(
            settings.migration_database_url, "db_admin", configured_database_target(settings)
        ),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_sync_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = create_migration_engine(migration_settings())
    try:
        try:
            async with connectable.connect() as connection:
                await connection.run_sync(run_sync_migrations)
        finally:
            await connectable.dispose()
    except Exception:
        # Do not expose URL, credentials, SQL parameters or the original exception chain.
        raise DatabaseConnectionError(
            "online migration failed; do not automatically retry"
        ) from None


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
