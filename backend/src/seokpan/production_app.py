"""Lazy production ASGI composition; import and OpenAPI export do no provider I/O."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from seokpan.health import RuntimeReadiness
from seokpan.persistence.redis.common import RedisProviderError
from seokpan.production import (
    ProductionProviderUnavailable,
    _probe_database,
    _probe_redis,
    build_production_providers,
    production_resources,
)
from seokpan.settings import Settings

if TYPE_CHECKING:
    from seokpan.app import ApplicationServices

_LOGGER = logging.getLogger(__name__)
_TRANSIENT_TURN_ERROR_CODES = {"REDIS_SNAPSHOT_CHANGED"}


def _is_transient_turn_error(error: Exception) -> bool:
    return getattr(error, "code", None) in _TRANSIENT_TURN_ERROR_CODES


def _provider_cause_kind(error: RedisProviderError) -> str:
    """Classify a chained Redis failure without recording its message or endpoint."""
    cause = error.__cause__
    if isinstance(cause, RedisTimeoutError):
        return "redis_timeout"
    if isinstance(cause, RedisConnectionError):
        return "redis_connection"
    if isinstance(cause, RedisError):
        return "redis_other"
    return "unknown"


def _request_process_shutdown() -> None:
    """Ask the production ASGI server to stop after a mandatory runner dies."""
    os.kill(os.getpid(), signal.SIGTERM)


async def _run_background_services(
    services: ApplicationServices,
    readiness: RuntimeReadiness,
    *,
    recovery_probe: Callable[[], Awaitable[None]] | None = None,
) -> None:
    disconnects = services.disconnect_expiry
    turns = services.turn_resolution
    if disconnects is None or turns is None:
        readiness.mark_not_ready()
        raise RuntimeError("PRODUCTION_RUNNERS_REQUIRED")
    retry_delay = 0.1
    provider_unavailable = False
    try:
        while True:
            try:
                await disconnects.run_once()
                await turns.reconcile_game_invalidations()
                try:
                    await turns.run_once()
                except Exception as error:
                    if not _is_transient_turn_error(error):
                        raise
                    _LOGGER.warning(
                        "Transient turn snapshot race; retrying",
                        extra={
                            "event": "turn_resolution.snapshot_changed",
                            "error_code": getattr(error, "code", None),
                        },
                    )
                if provider_unavailable and recovery_probe is not None:
                    try:
                        await recovery_probe()
                    except (
                        OSError,
                        TimeoutError,
                        RedisError,
                        SQLAlchemyError,
                        ProductionProviderUnavailable,
                    ):
                        raise RedisProviderError() from None
            except RedisProviderError as error:
                if error.code != "REDIS_PROVIDER_UNAVAILABLE":
                    raise
                readiness.mark_not_ready()
                if not provider_unavailable:
                    _LOGGER.warning(
                        "Production background runner provider unavailable; retrying",
                        extra={
                            "event": "production.runner.provider_unavailable",
                            "error_code": error.code,
                            "provider_cause": _provider_cause_kind(error),
                        },
                    )
                    provider_unavailable = True
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 2.0)
                continue
            if provider_unavailable:
                _LOGGER.info(
                    "Production background runner provider recovered",
                    extra={"event": "production.runner.provider_recovered"},
                )
                provider_unavailable = False
            retry_delay = 0.1
            readiness.mark_ready()
            await asyncio.sleep(0.1)
    except asyncio.CancelledError:
        raise
    except Exception:
        readiness.mark_not_ready()
        realtime = services.realtime_api
        if realtime is not None:
            realtime.registry.end_runtime()
        _LOGGER.exception("Production background runner stopped")
        raise


class _RuntimeDispatch:
    def __init__(self, owner: FastAPI) -> None:
        self._owner = owner

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        runtime: ASGIApp | None = getattr(self._owner.state, "runtime_application", None)
        if runtime is None:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1013})
            else:
                await JSONResponse(
                    {"status": "not_ready"},
                    status_code=503,
                    headers={"Cache-Control": "no-store"},
                )(scope, receive, send)
            return
        await runtime(scope, receive, send)


def create_production_app(settings: Settings) -> FastAPI:
    """Return an import-safe shell which builds providers only during lifespan startup."""

    if settings.environment != "production":
        raise RuntimeError("Production environment is required")
    readiness = RuntimeReadiness()

    @asynccontextmanager
    async def lifespan(shell: FastAPI) -> AsyncIterator[None]:
        from seokpan.app import build_production_services, create_app

        async with production_resources(settings, readiness) as resources:
            # Resource probes alone are insufficient: routes and mandatory
            # background runners must also be composed before readiness opens.
            readiness.mark_not_ready()
            services = build_production_services(settings, build_production_providers(resources))
            runtime = create_app(settings=settings, services=services, readiness=readiness)
            async with runtime.router.lifespan_context(runtime):

                async def probe_recovery() -> None:
                    await _probe_database(resources.databases)
                    await _probe_redis(resources.redis)

                runner = asyncio.create_task(
                    _run_background_services(services, readiness, recovery_probe=probe_recovery),
                    name="seokpan-production-runner",
                )
                await asyncio.sleep(0)
                if runner.done():
                    await runner
                shell.state.runtime_application = runtime

                def stop_after_runner_exit(task: asyncio.Task[None]) -> None:
                    if task.cancelled():
                        return
                    readiness.mark_not_ready()
                    _LOGGER.critical(
                        "Production background runner ended; requesting process shutdown",
                        extra={"event": "production.runner.process_shutdown_requested"},
                    )
                    _request_process_shutdown()

                runner.add_done_callback(stop_after_runner_exit)
                try:
                    yield
                finally:
                    readiness.mark_not_ready()
                    if services.realtime_api is not None:
                        services.realtime_api.registry.end_runtime()
                    runner.cancel()
                    await asyncio.gather(runner, return_exceptions=True)
                    shell.state.runtime_application = None

    shell = FastAPI(lifespan=lifespan, docs_url=None, openapi_url=None, redoc_url=None)
    shell.state.runtime_application = None
    shell.mount("/", _RuntimeDispatch(shell))
    return shell
