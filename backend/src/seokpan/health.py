from dataclasses import dataclass
from typing import Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel

router = APIRouter(prefix="/health", tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["started", "alive", "ready", "not_ready"]


@dataclass(slots=True)
class RuntimeReadiness:
    """Sanitized readiness state; provider errors are never retained or returned."""

    ready: bool = False

    def mark_ready(self) -> None:
        self.ready = True

    def mark_not_ready(self) -> None:
        self.ready = False


@router.get("/startup", response_model=HealthResponse)
async def startup() -> HealthResponse:
    return HealthResponse(status="started")


@router.get("/live", response_model=HealthResponse)
async def live() -> HealthResponse:
    return HealthResponse(status="alive")


@router.get(
    "/ready",
    response_model=HealthResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": HealthResponse}},
)
async def ready(request: Request, response: Response) -> HealthResponse:
    readiness = getattr(request.app.state, "readiness", None)
    if readiness is not None and not readiness.ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        response.headers["Cache-Control"] = "no-store"
        return HealthResponse(status="not_ready")
    return HealthResponse(status="ready")
