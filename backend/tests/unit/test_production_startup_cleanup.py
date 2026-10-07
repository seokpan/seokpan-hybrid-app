"""Own background tasks across startup cancellation; no operational providers."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI

import seokpan.app as app_module
import seokpan.production_app as production_app_module
from seokpan.health import RuntimeReadiness
from seokpan.settings import Settings


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_startup", [True, False])
async def test_lifespan_joins_runner_before_releasing_providers(
    monkeypatch: pytest.MonkeyPatch, cancel_startup: bool
) -> None:
    events: list[str] = []
    runners: list[asyncio.Task[None]] = []
    owner: asyncio.Task[None] | None = None
    registry = SimpleNamespace(end_runtime=Mock())
    services = SimpleNamespace(realtime_api=SimpleNamespace(registry=registry))
    shutdown = Mock()

    @asynccontextmanager
    async def resources(_settings: Settings, _readiness: RuntimeReadiness) -> AsyncIterator[object]:
        events.append("providers-open")
        try:
            yield object()
        finally:
            events.append("providers-close")

    async def runner(*_args: object, **_kwargs: object) -> None:
        current = asyncio.current_task()
        assert current is not None
        runners.append(current)
        events.append("runner-start")
        assert owner is not None
        if cancel_startup:
            # The parent is suspended at the first post-create_task checkpoint.
            owner.cancel()
        try:
            await asyncio.Event().wait()
        finally:
            events.append("runner-stop")

    monkeypatch.setattr(production_app_module, "production_resources", resources)
    monkeypatch.setattr(production_app_module, "build_production_providers", lambda value: value)
    monkeypatch.setattr(production_app_module, "_run_background_services", runner)
    monkeypatch.setattr(production_app_module, "_request_process_shutdown", shutdown)
    monkeypatch.setattr(app_module, "build_production_services", lambda *_args: services)
    monkeypatch.setattr(app_module, "create_app", lambda **_kwargs: FastAPI())
    shell = production_app_module.create_production_app(Settings(environment="production"))

    async def enter() -> None:
        nonlocal owner
        owner = asyncio.current_task()
        async with shell.router.lifespan_context(shell):
            events.append("published")

    startup = asyncio.create_task(enter())
    try:
        if cancel_startup:
            with pytest.raises(asyncio.CancelledError):
                await startup
        else:
            await startup
        assert len(runners) == 1
        assert runners[0].done(), "startup must not leave a task using closed providers"
        assert runners[0].cancelled()
        assert events.index("runner-stop") < events.index("providers-close")
        assert ("published" in events) is (not cancel_startup)
        assert shell.state.runtime_application is None
        registry.end_runtime.assert_called_once()
        shutdown.assert_not_called()
    finally:
        # A regression must fail without leaking its own deliberately suspended task.
        startup.cancel()
        for task in runners:
            task.cancel()
        await asyncio.gather(startup, *runners, return_exceptions=True)
