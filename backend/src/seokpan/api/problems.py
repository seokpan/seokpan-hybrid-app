"""RFC 9457 error responses shared by HTTP routes."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from seokpan.game.application import PersistenceRuleViolation
from seokpan.game.domain import GameRuleViolation
from seokpan.identity.application import IdentityRuleViolation, SessionRuleViolation
from seokpan.identity.application.auth_session import SessionTransitionUnavailable
from seokpan.persistence.redis.common import RedisProviderError
from seokpan.room.domain import RoomRuleViolation
from seokpan.vote.domain import VoteRuleViolation

_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ApiProblem(Exception):
    status: int
    code: str
    title: str
    current_version: int | None = None
    snapshot_url: str | None = None


class ProblemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    title: str
    status: int
    code: str
    request_id: str
    errors: list[dict[str, str]] | None = None
    current_version: int | None = None
    snapshot_url: str | None = None


_IDENTITY_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"description": "Authentication failed or is required"},
    403: {"description": "Origin or CSRF validation failed"},
    409: {"description": "Identity or active Room state conflict"},
    422: {"description": "Request validation failed"},
    503: {"description": "Identity or Session service unavailable"},
}
for _problem_response in _IDENTITY_ERROR_RESPONSES.values():
    _problem_response["content"] = {
        "application/problem+json": {"schema": ProblemResponse.model_json_schema()}
    }


def identity_problem_responses(*codes: int) -> dict[int | str, dict[str, Any]]:
    return {code: _IDENTITY_ERROR_RESPONSES[code] for code in codes}


def room_problem_responses(*codes: int) -> dict[int | str, dict[str, Any]]:
    return {
        code: {
            "description": "Room request rejected",
            "content": {
                "application/problem+json": {"schema": ProblemResponse.model_json_schema()}
            },
        }
        for code in codes
    }


def game_problem_responses(*codes: int) -> dict[int | str, dict[str, Any]]:
    return {
        code: {
            "description": "Game request rejected",
            "content": {
                "application/problem+json": {"schema": ProblemResponse.model_json_schema()}
            },
        }
        for code in codes
    }


def request_id(request: Request) -> str:
    supplied = request.headers.get("X-Request-ID", "")
    if _REQUEST_ID.fullmatch(supplied) is not None:
        return supplied
    return str(uuid4())


def install_problem_handlers(application: FastAPI) -> None:
    @application.exception_handler(RedisProviderError)
    async def redis_problem_handler(request: Request, error: RedisProviderError) -> JSONResponse:
        trace_id = request_id(request)
        _LOGGER.warning(
            "Redis provider request failed",
            extra={
                "event": "api.redis_provider_error",
                "error_code": error.code,
                "request_id": trace_id,
            },
        )
        if error.code == "REDIS_SNAPSHOT_CHANGED":
            return _response(
                request,
                503,
                "SNAPSHOT_CHANGED",
                "State changed while reading; retry the snapshot",
                request_id_value=trace_id,
            )
        return _response(
            request,
            503,
            "REDIS_PROVIDER_UNAVAILABLE",
            "State unavailable",
            request_id_value=trace_id,
        )

    @application.exception_handler(ApiProblem)
    async def api_problem_handler(request: Request, error: ApiProblem) -> JSONResponse:
        return _response(
            request,
            error.status,
            error.code,
            error.title,
            current_version=error.current_version,
            snapshot_url=error.snapshot_url,
        )

    @application.exception_handler(RoomRuleViolation)
    async def room_problem_handler(
        request: Request,
        error: RoomRuleViolation,
    ) -> JSONResponse:
        status, title = _room_status(error.code)
        return _response(request, status, error.code, title)

    @application.exception_handler(VoteRuleViolation)
    async def vote_problem_handler(
        request: Request,
        error: VoteRuleViolation,
    ) -> JSONResponse:
        status, title = _game_status(error.code)
        return _response(request, status, error.code, title)

    @application.exception_handler(GameRuleViolation)
    async def game_rule_problem_handler(
        request: Request,
        error: GameRuleViolation,
    ) -> JSONResponse:
        status, title = _game_status(error.code)
        return _response(request, status, error.code, title)

    @application.exception_handler(PersistenceRuleViolation)
    async def persistence_problem_handler(
        request: Request,
        error: PersistenceRuleViolation,
    ) -> JSONResponse:
        status, title = _game_status(error.code)
        return _response(request, status, error.code, title)

    @application.exception_handler(IdentityRuleViolation)
    async def identity_problem_handler(
        request: Request,
        error: IdentityRuleViolation,
    ) -> JSONResponse:
        status, title = _identity_status(error.code)
        return _response(request, status, error.code, title)

    @application.exception_handler(SessionRuleViolation)
    async def session_problem_handler(
        request: Request,
        error: SessionRuleViolation,
    ) -> JSONResponse:
        status = 401 if error.code == "SESSION_NOT_FOUND" else 503
        title = "Authentication required" if status == 401 else "Session unavailable"
        return _response(request, status, error.code, title)

    @application.exception_handler(SessionTransitionUnavailable)
    async def transition_problem_handler(
        request: Request,
        _error: SessionTransitionUnavailable,
    ) -> JSONResponse:
        return _response(request, 503, "SESSION_TRANSITION_UNAVAILABLE", "Session unavailable")

    @application.exception_handler(RequestValidationError)
    async def validation_problem_handler(
        request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        fields = [
            {"location": ".".join(str(part) for part in item["loc"]), "message": item["msg"]}
            for item in error.errors()
        ]
        return _response(
            request,
            422,
            "VALIDATION_FAILED",
            "Request validation failed",
            errors=fields,
        )


def _identity_status(code: str) -> tuple[int, str]:
    if code == "AUTH_INVALID_CREDENTIALS":
        return 401, "Invalid credentials"
    if code in {"LOGIN_ID_ALREADY_EXISTS", "NICKNAME_ALREADY_EXISTS"}:
        return 409, "Member already exists"
    if "PROVIDER" in code or code in {"IDENTITY_COMMIT_UNCERTAIN", "PASSWORD_HASH_UNAVAILABLE"}:
        return 503, "Identity service unavailable"
    return 422, "Request validation failed"


def _room_status(code: str) -> tuple[int, str]:
    if code in {
        "GAME_CLOSURE_UNCONFIRMED",
        "GAME_START_RECOVERY_REQUIRED",
        "GAME_START_RESULT_INVALID",
        "GAME_START_TIME_MISSING",
        "PARTICIPANT_IDENTITY_NOT_FOUND",
        "START_CLOSURE_INVALID",
        "START_COMPLETION_INVALID",
    }:
        return 503, "Room state unavailable"
    if code in {"ROOM_NOT_FOUND", "PARTICIPANT_NOT_FOUND"}:
        return 404, "Room or participant not found"
    if code in {
        "ACTIVE_ROOM_IDENTITY_CHANGE_NOT_ALLOWED",
        "ACTIVE_ROOM_MEMBER_CHANGE_NOT_ALLOWED",
        "PARTICIPANT_ALREADY_JOINED",
        "PARTICIPANT_DISCONNECTED",
        "REQUEST_ID_CONFLICT",
        "BOTH_TEAMS_REQUIRED",
        "MINIMUM_READY_NOT_MET",
        "ROOM_ALREADY_EXISTS",
        "ROOM_CAPACITY_REACHED",
        "ROOM_NOT_WAITING",
        "ROOM_RECENTLY_CLOSED",
        "SESSION_ALREADY_IN_ROOM",
        "STATE_VERSION_CONFLICT",
    }:
        return 409, "Room state conflict"
    if code in {
        "MEMBER_REQUIRED_TO_CREATE_ROOM",
        "OWNER_REQUIRED",
        "SESSION_NOT_IN_ROOM",
        "GAME_NOT_IN_CURRENT_ROOM",
    }:
        return 403, "Room operation is not allowed"
    if code == "ROOM_PASSWORD_INVALID":
        return 401, "Room password is invalid"
    return 422, "Room request is invalid"


def _game_status(code: str) -> tuple[int, str]:
    if code in {
        "GAME_END_REASON_MISSING",
        "GAME_HISTORY_INVALID",
        "GAME_RESULT_INCOMPLETE",
        "GAME_RESULT_HISTORY_MISMATCH",
        "GAME_RUNTIME_HISTORY_MISMATCH",
        "GAME_START_RECOVERY_REQUIRED",
        "INVALID_NEXT_DEADLINE",
        "RESOLUTION_CANDIDATES_MISSING",
        "TURN_CLOSURE_MISSING",
        "VALID_VOTER_COUNT_MISSING",
    }:
        return 503, "Game state unavailable"
    if code in {"GAME_NOT_FOUND", "GAME_RUNTIME_NOT_FOUND"}:
        return 404, "Game not found"
    if code in {
        "CURRENT_TEAM_REQUIRED",
        "GAME_NOT_IN_CURRENT_ROOM",
        "PARTICIPANT_DISCONNECTED",
        "PARTICIPANT_NOT_FOUND",
        "PLAYER_REQUIRED",
        "SESSION_NOT_IN_ROOM",
    }:
        return 403, "Game operation is not allowed"
    if code in {
        "GAME_RUNTIME_ALREADY_EXISTS",
        "GAME_NOT_FINISHED",
        "GAME_START_CONFLICT",
        "REQUEST_ID_CONFLICT",
        "RESOLUTION_ALREADY_APPLIED",
        "STALE_GAME",
        "STALE_TURN",
        "STATE_VERSION_CONFLICT",
        "TURN_DEADLINE_REACHED",
        "TURN_NOT_VOTING",
    }:
        return 409, "Game state conflict"
    return 422, "Game request is invalid"


def _response(
    request: Request,
    status: int,
    code: str,
    title: str,
    *,
    errors: list[dict[str, str]] | None = None,
    current_version: int | None = None,
    snapshot_url: str | None = None,
    request_id_value: str | None = None,
) -> JSONResponse:
    body: dict[str, object] = {
        "type": f"urn:seokpan:problem:{code.lower().replace('_', '-')}",
        "title": title,
        "status": status,
        "code": code,
        "request_id": request_id_value if request_id_value is not None else request_id(request),
    }
    if errors is not None:
        body["errors"] = errors
    if current_version is not None:
        body["current_version"] = current_version
    if snapshot_url is not None:
        body["snapshot_url"] = snapshot_url
    return JSONResponse(
        body,
        status_code=status,
        media_type="application/problem+json",
        headers={"Cache-Control": "no-store"},
    )
