"""Own the development runner before the first startup cancellation checkpoint."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

import seokpan.development as development_module
from seokpan.settings import Settings


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_startup", [True, False])
async def test_development_lifespan_joins_runner_before_base_exit(
    monkeypatch: pytest.MonkeyPatch, cancel_startup: bool
) -> None:
    events: list[str] = []
    runners: list[asyncio.Task[None]] = []
    owner: asyncio.Task[None] | None = None

    @asynccontextmanager
    async def base_lifespan(_app: FastAPI) -> AsyncIterator[None]:
        events.append("base-open")
        try:
            yield
        finally:
            events.append("base-close")

    class Runner:
        def __init__(self, _services: object, _clock: object) -> None:
            self.running = False

        def available(self) -> bool:
            return self.running

        async def run(self) -> None:
            current = asyncio.current_task()
            assert current is not None
            runners.append(current)
            self.running = True
            events.append("runner-start")
            assert owner is not None
            if cancel_startup:
                owner.cancel()
            try:
                await asyncio.Event().wait()
            finally:
                self.running = False
                events.append("runner-stop")

    monkeypatch.setattr(development_module, "DevelopmentRunner", Runner)
    monkeypatch.setattr(development_module, "build_headless_services", lambda *_a, **_k: object())
    monkeypatch.setattr(
        development_module, "create_app", lambda **_kwargs: FastAPI(lifespan=base_lifespan)
    )
    shell = development_module.create_development_app(
        settings=Settings(environment="test"), clock=SimpleNamespace(now_ms=1234)
    )

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
        assert runners[0].done(), "startup must join the development runner before leaving"
        assert runners[0].cancelled()
        assert events.index("runner-stop") < events.index("base-close")
        assert ("published" in events) is (not cancel_startup)
    finally:
        startup.cancel()
        for task in runners:
            task.cancel()
        await asyncio.gather(startup, *runners, return_exceptions=True)
