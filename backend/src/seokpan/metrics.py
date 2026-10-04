from __future__ import annotations

from time import perf_counter
from typing import Final

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.middleware.base import RequestResponseEndpoint

_METRICS_PATH: Final = "/metrics"

HTTP_REQUESTS: Final = Counter(
    "seokpan_http_requests_total",
    "Total HTTP requests handled by the backend.",
    ("method", "route", "status"),
)

HTTP_REQUEST_DURATION: Final = Histogram(
    "seokpan_http_request_duration_seconds",
    "HTTP request duration in seconds.",
    ("method", "route", "status"),
)


def _route_template(request: Request) -> str:
    route = request.scope.get("route")

    path_format = getattr(route, "path_format", None)
    if isinstance(path_format, str):
        return path_format

    path = getattr(route, "path", None)
    if isinstance(path, str):
        return path

    return "unmatched"


def install_metrics(application: FastAPI) -> None:
    @application.get(_METRICS_PATH, include_in_schema=False)
    async def metrics() -> Response:
        return Response(
            content=generate_latest(),
            media_type=CONTENT_TYPE_LATEST,
        )

    @application.middleware("http")
    async def observe_http(
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        if request.url.path == _METRICS_PATH:
            return await call_next(request)

        started = perf_counter()
        status_code = 500

        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            elapsed = max(0.0, perf_counter() - started)
            labels = {
                "method": request.method,
                "route": _route_template(request),
                "status": str(status_code),
            }

            HTTP_REQUESTS.labels(**labels).inc()
            HTTP_REQUEST_DURATION.labels(**labels).observe(elapsed)
