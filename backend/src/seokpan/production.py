"""Production provider resource startup without import-time external I/O."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import cast

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from seokpan.clock import SystemClock
from seokpan.health import RuntimeReadiness
from seokpan.persistence.mariadb import (
    MariaDBGamePersistenceAdapter,
    MariaDBIdentityAdapter,
)
from seokpan.persistence.mariadb.connection import RuntimeDatabases, runtime_databases
from seokpan.persistence.mariadb.statistics_adapter import MariaDBStatisticsAdapter
from seokpan.persistence.redis import (
    RedisChatAdapter,
    RedisPresenceAdapter,
    RedisRealtimeEventAdapter,
    RedisSessionAdapter,
    RedisVoteRuntimeAdapter,
)
from seokpan.persistence.redis.common import RedisClient
from seokpan.persistence.redis.connection import runtime_redis
from seokpan.persistence.redis.room_admission import (
    SessionAdmissionRedisRoomAdapter as RedisRoomRuntimeAdapter,
)
from seokpan.security import (
    PRODUCTION_ARGON2_PARAMETERS,
    Argon2PasswordHasher,
    Argon2RoomPassword,
    SecretsTokenSource,
)
from seokpan.settings import Settings


class ProductionProviderUnavailable(RuntimeError):
    """Sanitized startup failure; underlying connection details stay out of responses."""


@dataclass(frozen=True, slots=True)
class ProductionResources:
    databases: RuntimeDatabases = field(repr=False)
    redis: Redis = field(repr=False)


@dataclass(frozen=True, slots=True)
class ProductionProviders:
    """Provider objects only; service coordination and runners are composed separately."""

    identities: MariaDBIdentityAdapter
    games: MariaDBGamePersistenceAdapter
    statistics: MariaDBStatisticsAdapter
    sessions: RedisSessionAdapter
    rooms: RedisRoomRuntimeAdapter
    votes: RedisVoteRuntimeAdapter
    presence: RedisPresenceAdapter
    chat: RedisChatAdapter
    realtime: RedisRealtimeEventAdapter
    passwords: Argon2PasswordHasher
    room_passwords: Argon2RoomPassword
    tokens: SecretsTokenSource
    clock: SystemClock
    redis_client: RedisClient = field(repr=False)


def build_production_providers(resources: ProductionResources) -> ProductionProviders:
    """Wire role-separated factories and one shared Redis client without doing I/O."""

    password_hasher = Argon2PasswordHasher(PRODUCTION_ARGON2_PARAMETERS)
    redis_client = cast(RedisClient, resources.redis)
    return ProductionProviders(
        identities=MariaDBIdentityAdapter(resources.databases.identity_sessions),
        games=MariaDBGamePersistenceAdapter(resources.databases.game_sessions),
        statistics=MariaDBStatisticsAdapter(resources.databases.game_sessions),
        sessions=RedisSessionAdapter(redis_client),
        rooms=RedisRoomRuntimeAdapter(redis_client),
        votes=RedisVoteRuntimeAdapter(redis_client),
        presence=RedisPresenceAdapter(
            redis_client,
            lease_seconds=15,
            max_connections=1000,
        ),
        chat=RedisChatAdapter(resources.redis),
        realtime=RedisRealtimeEventAdapter(resources.redis),
        passwords=password_hasher,
        room_passwords=Argon2RoomPassword(password_hasher),
        tokens=SecretsTokenSource(),
        clock=SystemClock(),
        redis_client=redis_client,
    )


async def _probe_database(databases: RuntimeDatabases) -> None:
    for engine in (databases.identity, databases.game):
        async with engine.connect() as connection:
            result = await connection.execute(text("SELECT 1"))
            if result.scalar_one() != 1:
                raise ProductionProviderUnavailable("database readiness probe failed")


async def _probe_redis(client: Redis) -> None:
    if await client.ping() is not True:
        raise ProductionProviderUnavailable("Redis readiness probe failed")


@asynccontextmanager
async def production_resources(
    settings: Settings,
    readiness: RuntimeReadiness,
) -> AsyncIterator[ProductionResources]:
    """Own DB/Redis clients for one process and expose readiness only after probes pass."""

    readiness.mark_not_ready()
    try:
        async with AsyncExitStack() as stack:
            databases = await stack.enter_async_context(runtime_databases(settings))
            redis = await stack.enter_async_context(runtime_redis(settings))
            await _probe_database(databases)
            await _probe_redis(redis)
            readiness.mark_ready()
            yield ProductionResources(databases=databases, redis=redis)
    except (OSError, TimeoutError, RedisError, SQLAlchemyError):
        raise ProductionProviderUnavailable("production provider startup failed") from None
    finally:
        readiness.mark_not_ready()
