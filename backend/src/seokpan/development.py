"""Explicit loopback-only, volatile Browser development entrypoint.

Run: python -m uvicorn seokpan.development:create_development_app --factory
      --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
Never use this factory in a production image or multi-worker deployment.
"""

import asyncio
import logging
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from seokpan.app import ApplicationServices, build_headless_services, create_app
from seokpan.clock import MillisecondClock
from seokpan.settings import Settings

_LOGGER = logging.getLogger(__name__)
POLL_SECONDS = 0.1
MAX_PAUSE_MS = 2000


class MonotonicDevelopmentClock:
    """Epoch anchored once; later wall-clock adjustments cannot reverse deadlines."""

    def __init__(self) -> None:
        self._epoch = time.time_ns() // 1_000_000
        self._start = time.monotonic_ns()

    @property
    def now_ms(self) -> int:
        return self._epoch + (time.monotonic_ns() - self._start) // 1_000_000


class DevelopmentTieSelector:
    async def select(self, *, game_id: str, turn_no: int, candidates: tuple[str, ...]) -> str:
        del game_id, turn_no
        if not candidates:
            raise ValueError("TIE_CANDIDATES_REQUIRED")
        return secrets.choice(candidates)


class DevelopmentRunner:
    def __init__(self, services: ApplicationServices, clock: MillisecondClock) -> None:
        if services.turn_resolution is None or services.disconnect_expiry is None:
            raise ValueError("DEVELOPMENT_RUNNERS_REQUIRED")
        self.services = services
        self.clock = clock
        self.running = False
        self.failed = False
        self._last_tick = clock.now_ms

    def available(self) -> bool:
        # Guard requests as well as the loop: a resumed request must not beat the poll.
        if self.clock.now_ms - self._last_tick > MAX_PAUSE_MS:
            self.failed = True
        return self.running and not self.failed

    async def tick(self) -> None:
        if not self.available():
            raise RuntimeError("DEVELOPMENT_RUNTIME_PAUSED")
        assert self.services.disconnect_expiry is not None
        assert self.services.turn_resolution is not None
        await self.services.disconnect_expiry.run_once()
        await self.services.turn_resolution.reconcile_game_invalidations()
        await self.services.turn_resolution.run_once()
        self._last_tick = self.clock.now_ms

    async def run(self) -> None:
        if self.running or self.failed:
            raise RuntimeError("DEVELOPMENT_RUNTIME_NOT_RESTARTABLE")
        self.running = True
        self._last_tick = self.clock.now_ms
        try:
            while True:
                await self.tick()
                await asyncio.sleep(POLL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.failed = True
            if self.services.realtime_api is not None:
                # Close existing sockets as server shutdown, not participant departures.
                self.services.realtime_api.registry.end_runtime()
            # Do not include raw exceptions: they may contain credentials or user input.
            _LOGGER.error("Development runtime paused; restart with fresh test data")
        finally:
            self.running = False


class DevelopmentGuard:
    def __init__(self, app: ASGIApp, runner: DevelopmentRunner) -> None:
        self.app = app
        self.runner = runner

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in {"http", "websocket"} and not self.runner.available():
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1013})
            else:
                await JSONResponse(
                    {
                        "code": "DEVELOPMENT_RUNTIME_PAUSED",
                        "detail": "Restart the local test server",
                    },
                    status_code=503,
                    headers={"Cache-Control": "no-store"},
                )(scope, receive, send)
            return
        await self.app(scope, receive, send)


def create_development_app(
    *,
    settings: Settings | None = None,
    clock: MillisecondClock | None = None,
) -> FastAPI:
    return _create_browser_app(settings or Settings(), clock, "http://localhost:5173")


def create_browser_e2e_app() -> FastAPI:
    """Fixed isolated test origin; never reuse the user's trial server/data."""
    return _create_browser_app(
        Settings(allowed_origins=("http://localhost:5175",)), None, "http://localhost:5175"
    )


def _create_browser_app(resolved: Settings, clock: MillisecondClock | None, origin: str) -> FastAPI:
    if resolved.environment not in {"local", "test"}:
        raise RuntimeError("Browser development requires local/test environment")
    if resolved.identity_database_url or resolved.game_database_url or resolved.redis_url:
        raise RuntimeError("Browser development does not accept real provider configuration")
    if resolved.allowed_origins != (origin,):
        raise RuntimeError("Browser development requires the loopback frontend origin")
    resolved_clock = clock or MonotonicDevelopmentClock()
    services = build_headless_services(
        resolved, clock=resolved_clock, discover_turns=True, tie_selector=DevelopmentTieSelector()
    )
    application = create_app(settings=resolved, services=services)
    runner = DevelopmentRunner(services, resolved_clock)
    base_lifespan = application.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with base_lifespan(app):
            task = asyncio.create_task(runner.run(), name="seokpan-development-runner")
            await asyncio.sleep(0)
            try:
                if not runner.available():
                    raise RuntimeError("DEVELOPMENT_STARTUP_FAILED")
                yield
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    application.router.lifespan_context = lifespan
    application.add_middleware(DevelopmentGuard, runner=runner)
    application.state.development_runner = runner
    return application
