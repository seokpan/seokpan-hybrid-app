"""Validated Redis client lifecycle for the production Application runtime."""

from __future__ import annotations

import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

from redis.asyncio import ConnectionPool, Redis
from redis.asyncio.connection import Connection, SSLConnection

from seokpan.connection_contract import (
    ConnectionContractError,
    parse_hybrid_redis_url,
    strict_ca_context,
    validated_redis_auth_token,
)
from seokpan.settings import Settings

REDIS_HOST = "redis.platform.svc.cluster.local"
REDIS_PORT = 6379
REDIS_DATABASE = 0


class RedisConfigurationError(ValueError):
    """Safe configuration failure that never includes the supplied URL."""


def validated_redis_url(raw_url: str | None) -> str:
    if not raw_url or any(ord(char) < 32 or ord(char) == 127 for char in raw_url):
        raise RedisConfigurationError("Redis URL is missing or malformed")
    try:
        parts = urlsplit(raw_url)
        port = REDIS_PORT if parts.port is None else parts.port
    except ValueError:
        raise RedisConfigurationError("Redis URL is malformed") from None
    if (
        parts.scheme != "redis"
        or parts.hostname != REDIS_HOST
        or port != REDIS_PORT
        or parts.path != f"/{REDIS_DATABASE}"
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise RedisConfigurationError("Redis URL must use the official service endpoint")
    return f"redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_DATABASE}"


class ExplicitCATLSConnection(SSLConnection):
    """Pinned redis-py seam: trust only the supplied CA, including hostname checks."""

    def __init__(self, *, strict_context: ssl.SSLContext, **kwargs: Any) -> None:
        self._strict_context = strict_context
        super().__init__(ssl_cert_reqs=ssl.CERT_REQUIRED, ssl_check_hostname=True, **kwargs)

    def _connection_arguments(self) -> dict[str, Any]:
        # Avoid constructing the inherited context, which would load OS trust roots.
        arguments = dict(Connection._connection_arguments(self))
        arguments["ssl"] = self._strict_context
        return arguments


@asynccontextmanager
async def runtime_redis(settings: Settings) -> AsyncIterator[Redis]:
    """Create one lazy client per Application lifetime and always close its pool."""

    pool: ConnectionPool | None = None
    if settings.connection_profile == "legacy":
        url = validated_redis_url(settings.redis_url)
        client = Redis.from_url(
            url,
            decode_responses=False,
            socket_connect_timeout=5.0,
            socket_timeout=5.0,
            retry_on_timeout=False,
        )
    else:
        try:
            target = parse_hybrid_redis_url(
                settings.redis_url,
                settings.redis_expected_host,
                settings.redis_expected_port,
                settings.redis_expected_database,
            )
            token = validated_redis_auth_token(settings.redis_auth_token)
            context = strict_ca_context(settings.redis_ca_file)
        except ConnectionContractError as error:
            raise RedisConfigurationError(str(error)) from None
        try:
            pool = ConnectionPool(
                connection_class=ExplicitCATLSConnection,
                host=target.host,
                port=target.port,
                db=target.database,
                password=token,
                strict_context=context,
                decode_responses=False,
                socket_connect_timeout=5.0,
                socket_timeout=5.0,
                retry_on_timeout=False,
            )
            client = Redis(connection_pool=pool)
        except (ValueError, TypeError):
            if pool is not None:
                await pool.aclose()
            raise RedisConfigurationError("Redis client configuration failed") from None
    try:
        yield client
    finally:
        try:
            await client.aclose()
        finally:
            if pool is not None:
                await pool.aclose()
