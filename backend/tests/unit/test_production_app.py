import asyncio
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError

import seokpan.app as app_module
import seokpan.production_app as production_app_module
from seokpan.health import RuntimeReadiness
from seokpan.health import router as health_router
from seokpan.persistence.redis.common import RedisProviderError
from seokpan.production_app import create_production_app
from seokpan.settings import Settings


def test_fatal_runner_shutdown_uses_process_sigterm() -> None:
    with (
        patch.object(production_app_module.os, "getpid", return_value=12345),
        patch.object(production_app_module.os, "kill") as kill,
    ):
        production_app_module._request_process_shutdown()
    kill.assert_called_once_with(12345, signal.SIGTERM)


@pytest.mark.parametrize(
    ("cause", "expected"),
    [
        (RedisTimeoutError("private endpoint"), "redis_timeout"),
        (RedisConnectionError("private endpoint"), "redis_connection"),
        (RedisError("private endpoint"), "redis_other"),
        (ValueError("private endpoint"), "unknown"),
    ],
)
def test_provider_cause_kind_uses_only_fixed_categories(cause: Exception, expected: str) -> None:
    try:
        raise RedisProviderError() from cause
    except RedisProviderError as error:
        assert production_app_module._provider_cause_kind(error) == expected
    assert production_app_module._provider_cause_kind(RedisProviderError()) == "unknown"


def _patch_production_shell(
    monkeypatch: pytest.MonkeyPatch,
    services: object,
    events: list[str],
) -> None:
    resource = object()
    providers = object()

    @asynccontextmanager
    async def resources(_settings: Settings, readiness: object) -> AsyncIterator[object]:
        events.append("resources-open")
        readiness.mark_ready()
        try:
            yield resource
        finally:
            events.append("resources-close")

    def create_inner_app(*, settings: Settings, services: object, readiness: object) -> FastAPI:
        del settings, services
        runtime = FastAPI()
        runtime.state.readiness = readiness
        runtime.include_router(health_router)

        @runtime.get("/probe")
        async def probe() -> dict[str, str]:
            return {"status": "ok"}

        return runtime

    monkeypatch.setattr(production_app_module, "production_resources", resources)
    monkeypatch.setattr(
        production_app_module,
        "build_production_providers",
        lambda value: providers if value is resource else None,
    )
    monkeypatch.setattr(
        app_module,
        "build_production_services",
        lambda settings, value: (
            services if settings.environment == "production" and value is providers else None
        ),
    )
    monkeypatch.setattr(app_module, "create_app", create_inner_app)


def test_production_shell_opens_only_after_services_and_runners_are_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    disconnect = SimpleNamespace(
        run_once=AsyncMock(side_effect=lambda: events.append("disconnect"))
    )
    turns = SimpleNamespace(
        reconcile_game_invalidations=AsyncMock(side_effect=lambda: events.append("invalidation")),
        run_once=AsyncMock(side_effect=lambda: events.append("turn")),
    )
    registry = SimpleNamespace(end_runtime=Mock(side_effect=lambda: events.append("registry-end")))
    services = SimpleNamespace(
        disconnect_expiry=disconnect,
        turn_resolution=turns,
        realtime_api=SimpleNamespace(registry=registry),
    )
    _patch_production_shell(monkeypatch, services, events)

    shell = create_production_app(Settings(environment="production"))
    assert events == []
    with TestClient(shell) as client:
        assert client.get("/probe").json() == {"status": "ok"}
        assert client.get("/health/ready").json() == {"status": "ready"}
        assert disconnect.run_once.await_count >= 1
        assert turns.reconcile_game_invalidations.await_count >= 1
        assert turns.run_once.await_count >= 1
        assert events[:4] == ["resources-open", "disconnect", "invalidation", "turn"]

    assert events[-2:] == ["registry-end", "resources-close"]


def test_transient_snapshot_race_does_not_drop_production_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    disconnect = SimpleNamespace(run_once=AsyncMock())
    first_turn = True

    async def run_turn() -> None:
        nonlocal first_turn
        if first_turn:
            first_turn = False
            raise RedisProviderError("REDIS_SNAPSHOT_CHANGED")

    turns = SimpleNamespace(
        reconcile_game_invalidations=AsyncMock(return_value=0),
        run_once=AsyncMock(side_effect=run_turn),
    )
    registry = SimpleNamespace(end_runtime=Mock(side_effect=lambda: events.append("registry-end")))
    services = SimpleNamespace(
        disconnect_expiry=disconnect,
        turn_resolution=turns,
        realtime_api=SimpleNamespace(registry=registry),
    )
    _patch_production_shell(monkeypatch, services, events)

    shell = create_production_app(Settings(environment="production"))
    with TestClient(shell) as client:
        assert client.get("/health/ready").json() == {"status": "ready"}
        assert client.get("/probe").json() == {"status": "ok"}
        assert turns.reconcile_game_invalidations.await_count >= 1
        assert turns.run_once.await_count >= 1
        assert registry.end_runtime.call_count == 0


def test_late_fatal_runner_error_requests_process_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    failure_seen = Event()
    shutdown_requested = Mock()
    calls = 0

    async def run_disconnects() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            failure_seen.set()
            raise RedisProviderError("REDIS_RESPONSE_INVALID")

    registry = SimpleNamespace(end_runtime=Mock())
    services = SimpleNamespace(
        disconnect_expiry=SimpleNamespace(run_once=run_disconnects),
        turn_resolution=SimpleNamespace(
            reconcile_game_invalidations=AsyncMock(return_value=0),
            run_once=AsyncMock(),
        ),
        realtime_api=SimpleNamespace(registry=registry),
    )
    _patch_production_shell(monkeypatch, services, events)
    monkeypatch.setattr(production_app_module, "_request_process_shutdown", shutdown_requested)

    with TestClient(create_production_app(Settings(environment="production"))) as client:
        assert failure_seen.wait(2)
        deadline = monotonic() + 2
        while shutdown_requested.call_count == 0 and monotonic() < deadline:
            sleep(0.01)
        shutdown_requested.assert_called_once_with()
        assert client.get("/health/ready").status_code == 503
        assert client.get("/health/live").status_code == 200
        registry.end_runtime.assert_called_once()


def test_persistent_provider_outage_does_not_request_process_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    four_retries_seen = Event()
    shutdown_requested = Mock()
    calls = 0

    async def run_disconnects() -> None:
        nonlocal calls
        calls += 1
        if calls >= 4:
            four_retries_seen.set()
        raise RedisProviderError("REDIS_PROVIDER_UNAVAILABLE")

    registry = SimpleNamespace(end_runtime=Mock())
    services = SimpleNamespace(
        disconnect_expiry=SimpleNamespace(run_once=run_disconnects),
        turn_resolution=SimpleNamespace(
            reconcile_game_invalidations=AsyncMock(return_value=0),
            run_once=AsyncMock(),
        ),
        realtime_api=SimpleNamespace(registry=registry),
    )
    _patch_production_shell(monkeypatch, services, events)
    monkeypatch.setattr(production_app_module, "_request_process_shutdown", shutdown_requested)

    with TestClient(create_production_app(Settings(environment="production"))) as client:
        assert four_retries_seen.wait(2)
        assert client.get("/health/ready").status_code == 503
        assert client.get("/health/live").status_code == 200
        assert services.turn_resolution.run_once.await_count == 0
        registry.end_runtime.assert_not_called()
        shutdown_requested.assert_not_called()


def test_non_transient_provider_failure_still_fails_production_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    services = SimpleNamespace(
        disconnect_expiry=SimpleNamespace(run_once=AsyncMock()),
        turn_resolution=SimpleNamespace(
            reconcile_game_invalidations=AsyncMock(return_value=0),
            run_once=AsyncMock(side_effect=RedisProviderError("REDIS_RESPONSE_INVALID")),
        ),
        realtime_api=SimpleNamespace(registry=SimpleNamespace(end_runtime=Mock())),
    )
    _patch_production_shell(monkeypatch, services, events)

    shell = create_production_app(Settings(environment="production"))
    with (
        pytest.raises(RedisProviderError, match="REDIS_RESPONSE_INVALID"),
        TestClient(shell),
    ):
        raise AssertionError("non-transient provider failure must fail startup")
    services.turn_resolution.reconcile_game_invalidations.assert_awaited_once()
    services.turn_resolution.run_once.assert_awaited_once()


@pytest.mark.asyncio
async def test_provider_outage_logs_one_sanitized_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warning = Mock()
    monkeypatch.setattr(production_app_module._LOGGER, "warning", warning)
    recovered = asyncio.Event()
    calls = 0

    async def run_disconnects() -> None:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise RedisProviderError() from RedisConnectionError("private endpoint")
        recovered.set()

    registry = SimpleNamespace(end_runtime=Mock())
    services = SimpleNamespace(
        disconnect_expiry=SimpleNamespace(run_once=run_disconnects),
        turn_resolution=SimpleNamespace(
            reconcile_game_invalidations=AsyncMock(return_value=0),
            run_once=AsyncMock(),
        ),
        realtime_api=SimpleNamespace(registry=registry),
    )
    readiness = RuntimeReadiness(ready=True)
    task = asyncio.create_task(production_app_module._run_background_services(services, readiness))
    try:
        await asyncio.wait_for(recovered.wait(), timeout=2)
        await asyncio.sleep(0)
        warning.assert_called_once()
        args, kwargs = warning.call_args
        assert args == ("Production background runner provider unavailable; retrying",)
        assert kwargs["extra"] == {
            "event": "production.runner.provider_unavailable",
            "error_code": "REDIS_PROVIDER_UNAVAILABLE",
            "provider_cause": "redis_connection",
        }
        assert "private endpoint" not in repr(warning.call_args)
        assert readiness.ready is True
        registry.end_runtime.assert_not_called()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_runtime_provider_outage_recovers_without_ending_registry() -> None:
    failure_seen = asyncio.Event()
    recovery_seen = asyncio.Event()
    calls = 0

    async def run_disconnects() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            failure_seen.set()
            raise RedisProviderError()
        recovery_seen.set()

    registry = SimpleNamespace(end_runtime=Mock())
    services = SimpleNamespace(
        disconnect_expiry=SimpleNamespace(run_once=run_disconnects),
        turn_resolution=SimpleNamespace(
            reconcile_game_invalidations=AsyncMock(return_value=0),
            run_once=AsyncMock(),
        ),
        realtime_api=SimpleNamespace(registry=registry),
    )
    readiness = RuntimeReadiness(ready=True)
    task = asyncio.create_task(production_app_module._run_background_services(services, readiness))
    try:
        await asyncio.wait_for(failure_seen.wait(), timeout=1)
        assert readiness.ready is False
        await asyncio.wait_for(recovery_seen.wait(), timeout=1)
        await asyncio.sleep(0)
        assert readiness.ready is True
        assert calls >= 2
        registry.end_runtime.assert_not_called()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_runtime_recovery_waits_for_provider_probes() -> None:
    failure_seen = asyncio.Event()
    probe_recovered = asyncio.Event()
    calls = 0

    async def run_disconnects() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            failure_seen.set()
            raise RedisProviderError()

    async def probe() -> None:
        if not probe_recovered.is_set():
            raise ConnectionError("database temporarily unavailable")

    registry = SimpleNamespace(end_runtime=Mock())
    services = SimpleNamespace(
        disconnect_expiry=SimpleNamespace(run_once=run_disconnects),
        turn_resolution=SimpleNamespace(
            reconcile_game_invalidations=AsyncMock(return_value=0),
            run_once=AsyncMock(),
        ),
        realtime_api=SimpleNamespace(registry=registry),
    )
    readiness = RuntimeReadiness(ready=True)
    task = asyncio.create_task(
        production_app_module._run_background_services(services, readiness, recovery_probe=probe)
    )
    try:
        await asyncio.wait_for(failure_seen.wait(), timeout=1)
        assert readiness.ready is False
        await asyncio.sleep(0.15)
        assert calls >= 2
        assert readiness.ready is False
        registry.end_runtime.assert_not_called()
        probe_recovered.set()
        async with asyncio.timeout(2):
            while not readiness.ready:
                await asyncio.sleep(0.01)
        registry.end_runtime.assert_not_called()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_runtime_invalid_response_still_ends_registry_after_success() -> None:
    calls = 0

    async def run_disconnects() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RedisProviderError("REDIS_RESPONSE_INVALID")

    registry = SimpleNamespace(end_runtime=Mock())
    services = SimpleNamespace(
        disconnect_expiry=SimpleNamespace(run_once=run_disconnects),
        turn_resolution=SimpleNamespace(
            reconcile_game_invalidations=AsyncMock(return_value=0),
            run_once=AsyncMock(),
        ),
        realtime_api=SimpleNamespace(registry=registry),
    )
    readiness = RuntimeReadiness()

    with pytest.raises(RedisProviderError, match="REDIS_RESPONSE_INVALID"):
        await production_app_module._run_background_services(services, readiness)

    assert calls == 2
    assert readiness.ready is False
    registry.end_runtime.assert_called_once()


def test_production_shell_rejects_missing_mandatory_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = SimpleNamespace(
        disconnect_expiry=None,
        turn_resolution=SimpleNamespace(
            reconcile_game_invalidations=AsyncMock(return_value=0),
            run_once=AsyncMock(),
        ),
        realtime_api=None,
    )

    @asynccontextmanager
    async def resources(_settings: Settings, readiness: object) -> AsyncIterator[object]:
        readiness.mark_ready()
        yield object()

    monkeypatch.setattr(production_app_module, "production_resources", resources)
    monkeypatch.setattr(production_app_module, "build_production_providers", lambda value: value)
    monkeypatch.setattr(
        app_module, "build_production_services", lambda settings, providers: services
    )
    monkeypatch.setattr(app_module, "create_app", lambda **values: FastAPI())

    shell = create_production_app(Settings(environment="production"))
    try:
        with TestClient(shell):
            raise AssertionError("missing production runner must fail startup")
    except RuntimeError as error:
        assert str(error) == "PRODUCTION_RUNNERS_REQUIRED"


def test_invalidation_provider_outage_keeps_runner_not_ready_without_turn_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    turns = SimpleNamespace(
        reconcile_game_invalidations=AsyncMock(
            side_effect=RedisProviderError("REDIS_PROVIDER_UNAVAILABLE")
        ),
        run_once=AsyncMock(),
    )
    registry = SimpleNamespace(end_runtime=Mock())
    services = SimpleNamespace(
        disconnect_expiry=SimpleNamespace(run_once=AsyncMock()),
        turn_resolution=turns,
        realtime_api=SimpleNamespace(registry=registry),
    )
    _patch_production_shell(monkeypatch, services, events)

    shell = create_production_app(Settings(environment="production"))
    with TestClient(shell) as client:
        assert client.get("/health/ready").status_code == 503
        turns.run_once.assert_not_awaited()
        registry.end_runtime.assert_not_called()
    assert shell.state.runtime_application is None
    assert turns.reconcile_game_invalidations.await_count >= 1
    turns.run_once.assert_not_awaited()
    registry.end_runtime.assert_called_once()
    assert events[-1] == "resources-close"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["disconnect", "invalidation", "turn"])
async def test_background_cancellation_stops_later_stages(stage: str) -> None:
    disconnect = AsyncMock()
    invalidations = AsyncMock(return_value=0)
    turn = AsyncMock()
    ordered = [disconnect, invalidations, turn]
    position = ["disconnect", "invalidation", "turn"].index(stage)
    ordered[position].side_effect = asyncio.CancelledError()
    registry = SimpleNamespace(end_runtime=Mock())
    services = SimpleNamespace(
        disconnect_expiry=SimpleNamespace(run_once=disconnect),
        turn_resolution=SimpleNamespace(
            reconcile_game_invalidations=invalidations,
            run_once=turn,
        ),
        realtime_api=SimpleNamespace(registry=registry),
    )
    readiness = RuntimeReadiness(ready=True)

    with pytest.raises(asyncio.CancelledError):
        await production_app_module._run_background_services(services, readiness)

    for call in ordered[: position + 1]:
        call.assert_awaited_once()
    for call in ordered[position + 1 :]:
        call.assert_not_awaited()
    # Cancellation is owned by lifespan shutdown, not the runner's failure handler.
    assert readiness.ready
    registry.end_runtime.assert_not_called()
