from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from redis.asyncio import Redis

from seokpan.health import RuntimeReadiness
from seokpan.persistence.mariadb.connection import RuntimeDatabases
from seokpan.persistence.mariadb.game_adapter import MariaDBGamePersistenceAdapter
from seokpan.persistence.mariadb.identity_adapter import MariaDBIdentityAdapter
from seokpan.persistence.mariadb.statistics_adapter import MariaDBStatisticsAdapter
from seokpan.persistence.redis import (
    RedisChatAdapter,
    RedisPresenceAdapter,
    RedisRealtimeEventAdapter,
    RedisRoomRuntimeAdapter,
    RedisSessionAdapter,
    RedisVoteRuntimeAdapter,
)
from seokpan.production import (
    ProductionProviderUnavailable,
    ProductionResources,
    _probe_database,
    _probe_redis,
    build_production_providers,
    production_resources,
)
from seokpan.settings import Settings


def settings() -> Settings:
    return Settings(environment="production")


class _Connection:
    def __init__(self, result: int) -> None:
        self.result = result

    async def __aenter__(self) -> _Connection:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args

    async def execute(self, statement: object) -> object:
        assert str(statement) == "SELECT 1"
        return SimpleNamespace(scalar_one=lambda: self.result)


class _Engine:
    def __init__(self, result: int = 1) -> None:
        self.result = result

    def connect(self) -> _Connection:
        return _Connection(self.result)


@pytest.mark.asyncio
async def test_database_probe_checks_both_runtime_roles() -> None:
    identity = _Engine()
    game = _Engine()
    databases = SimpleNamespace(identity=identity, game=game)

    await _probe_database(cast(RuntimeDatabases, databases))


@pytest.mark.asyncio
async def test_database_probe_rejects_unexpected_result() -> None:
    databases = SimpleNamespace(identity=_Engine(result=0), game=_Engine())

    with pytest.raises(ProductionProviderUnavailable, match="database readiness probe failed"):
        await _probe_database(cast(RuntimeDatabases, databases))


@pytest.mark.asyncio
async def test_redis_probe_requires_true_ping() -> None:
    client = SimpleNamespace(ping=AsyncMock(return_value=False))

    with pytest.raises(ProductionProviderUnavailable, match="Redis readiness probe failed"):
        await _probe_redis(cast(Redis, client))


def test_production_providers_use_role_separated_db_and_shared_redis() -> None:
    identity_sessions = object()
    game_sessions = object()
    databases = SimpleNamespace(
        identity_sessions=identity_sessions,
        game_sessions=game_sessions,
    )
    redis = object()
    resources = ProductionResources(
        databases=cast(RuntimeDatabases, databases),
        redis=cast(Redis, redis),
    )

    providers = build_production_providers(resources)

    assert isinstance(providers.identities, MariaDBIdentityAdapter)
    assert isinstance(providers.games, MariaDBGamePersistenceAdapter)
    assert isinstance(providers.statistics, MariaDBStatisticsAdapter)
    assert isinstance(providers.sessions, RedisSessionAdapter)
    assert isinstance(providers.rooms, RedisRoomRuntimeAdapter)
    assert isinstance(providers.votes, RedisVoteRuntimeAdapter)
    assert isinstance(providers.presence, RedisPresenceAdapter)
    assert isinstance(providers.chat, RedisChatAdapter)
    assert isinstance(providers.realtime, RedisRealtimeEventAdapter)
    assert providers.identities._session_factory is identity_sessions
    assert providers.games._session_factory is game_sessions
    assert providers.statistics._sessions is game_sessions
    assert providers.sessions._client is redis
    assert providers.votes._client is redis


@pytest.mark.asyncio
async def test_resources_become_ready_only_after_both_probes_and_close_on_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    databases = SimpleNamespace(identity=object(), game=object())
    redis = object()

    @asynccontextmanager
    async def fake_databases(_settings: Settings) -> AsyncIterator[object]:
        events.append("databases-open")
        try:
            yield databases
        finally:
            events.append("databases-close")

    @asynccontextmanager
    async def fake_redis(_settings: Settings) -> AsyncIterator[object]:
        events.append("redis-open")
        try:
            yield redis
        finally:
            events.append("redis-close")

    async def probe_databases(value: object) -> None:
        assert value is databases
        events.append("databases-ready")

    async def probe_redis(value: object) -> None:
        assert value is redis
        events.append("redis-ready")

    monkeypatch.setattr("seokpan.production.runtime_databases", fake_databases)
    monkeypatch.setattr("seokpan.production.runtime_redis", fake_redis)
    monkeypatch.setattr("seokpan.production._probe_database", probe_databases)
    monkeypatch.setattr("seokpan.production._probe_redis", probe_redis)

    readiness = RuntimeReadiness()
    async with production_resources(settings(), readiness) as resources:
        assert readiness.ready is True
        assert resources.databases is databases
        assert resources.redis is redis
        assert events == [
            "databases-open",
            "redis-open",
            "databases-ready",
            "redis-ready",
        ]

    assert readiness.ready is False
    assert events[-2:] == ["redis-close", "databases-close"]


@pytest.mark.asyncio
async def test_probe_failure_keeps_not_ready_and_closes_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    @asynccontextmanager
    async def fake_databases(_settings: Settings) -> AsyncIterator[object]:
        events.append("databases-open")
        try:
            yield object()
        finally:
            events.append("databases-close")

    @asynccontextmanager
    async def fake_redis(_settings: Settings) -> AsyncIterator[object]:
        events.append("redis-open")
        try:
            yield object()
        finally:
            events.append("redis-close")

    probe = AsyncMock(side_effect=ProductionProviderUnavailable("probe failed"))
    redis_probe = AsyncMock()
    monkeypatch.setattr("seokpan.production.runtime_databases", fake_databases)
    monkeypatch.setattr("seokpan.production.runtime_redis", fake_redis)
    monkeypatch.setattr("seokpan.production._probe_database", probe)
    monkeypatch.setattr("seokpan.production._probe_redis", redis_probe)

    readiness = RuntimeReadiness()
    with pytest.raises(ProductionProviderUnavailable, match="probe failed"):
        async with production_resources(settings(), readiness):
            pytest.fail("failed provider probe must not yield resources")

    assert readiness.ready is False
    redis_probe.assert_not_awaited()
    assert events == [
        "databases-open",
        "redis-open",
        "redis-close",
        "databases-close",
    ]


@pytest.mark.asyncio
async def test_driver_failure_is_sanitized_and_resources_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed = False

    @asynccontextmanager
    async def fake_databases(_settings: Settings) -> AsyncIterator[object]:
        nonlocal closed
        try:
            yield object()
        finally:
            closed = True

    @asynccontextmanager
    async def fake_redis(_settings: Settings) -> AsyncIterator[object]:
        yield object()

    monkeypatch.setattr("seokpan.production.runtime_databases", fake_databases)
    monkeypatch.setattr("seokpan.production.runtime_redis", fake_redis)
    monkeypatch.setattr(
        "seokpan.production._probe_database",
        AsyncMock(side_effect=OSError("private endpoint detail")),
    )

    readiness = RuntimeReadiness()
    with pytest.raises(
        ProductionProviderUnavailable,
        match="^production provider startup failed$",
    ) as error:
        async with production_resources(settings(), readiness):
            pytest.fail("failed provider probe must not yield resources")

    assert "private endpoint detail" not in str(error.value)
    assert error.value.__cause__ is None
    assert readiness.ready is False
    assert closed is True
