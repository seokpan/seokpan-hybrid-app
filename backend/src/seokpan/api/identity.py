"""Guest, Member, and server-side Session HTTP endpoints."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Annotated, Protocol
from urllib.parse import urlsplit

from fastapi import APIRouter, Cookie, Header, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from seokpan.api.problems import ApiProblem, identity_problem_responses
from seokpan.identity.application import (
    AuthenticateMember,
    AuthSessionService,
    MemberIdentityService,
    RegisterMember,
    SessionActorType,
    SessionRecord,
    digest_opaque_token,
)
from seokpan.identity.domain import Member
from seokpan.settings import Settings

SESSION_COOKIE = "seokpan_session"


@dataclass(frozen=True, slots=True)
class IdentityApiServices:
    settings: Settings
    members: MemberIdentityService
    sessions: AuthSessionService
    participations: SessionParticipationLookup | None = None


class SessionParticipationLookup(Protocol):
    async def resolve_current_room(self, session_digest: str) -> tuple[str, str] | None: ...


class IssuedSessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor_type: SessionActorType
    actor_id: str
    display_name: str
    csrf_token: str = Field(repr=False)
    absolute_expires_at_ms: int


class MemberRegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    login_id: str
    nickname: str
    password: str = Field(repr=False)


class MemberLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    login_id: str
    password: str = Field(repr=False)


class MemberResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    member_id: int
    login_id: str
    nickname: str
    rating: int


class CurrentSessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor_type: SessionActorType
    actor_id: str
    display_name: str
    absolute_expires_at_ms: int
    room_id: str | None = None
    participant_id: str | None = None


class CsrfBootstrapRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CsrfBootstrapResponse(CurrentSessionResponse):
    csrf_token: str = Field(repr=False)


def identity_router(services: IdentityApiServices) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["identity"])

    @router.post(
        "/sessions/guest",
        response_model=IssuedSessionResponse,
        status_code=status.HTTP_201_CREATED,
        responses=identity_problem_responses(403, 409, 422, 503),
    )
    async def issue_guest_session(
        request: Request,
        response: Response,
        session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
        csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    ) -> IssuedSessionResponse:
        current = await state_change_context(services, request, session_cookie, csrf_token)
        issued = await services.sessions.issue_guest(current)
        _set_session_cookie(response, services.settings, issued.token)
        return IssuedSessionResponse(
            actor_type=issued.record.actor_type,
            actor_id=issued.record.actor_id,
            display_name=guest_display_name(issued.record.actor_id),
            csrf_token=issued.csrf_token,
            absolute_expires_at_ms=issued.record.absolute_expires_at_ms,
        )

    @router.post(
        "/members",
        response_model=MemberResponse,
        status_code=status.HTTP_201_CREATED,
        responses=identity_problem_responses(403, 409, 422, 503),
    )
    async def register_member(
        payload: MemberRegistrationRequest,
        request: Request,
        session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
        csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    ) -> MemberResponse:
        await state_change_context(services, request, session_cookie, csrf_token)
        member = await services.members.register(
            RegisterMember(payload.login_id, payload.nickname, payload.password)
        )
        return _member_response(member)

    @router.post(
        "/sessions/member",
        response_model=IssuedSessionResponse,
        responses=identity_problem_responses(401, 403, 409, 422, 503),
    )
    async def login_member(
        payload: MemberLoginRequest,
        request: Request,
        response: Response,
        session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
        csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    ) -> IssuedSessionResponse:
        current = await state_change_context(services, request, session_cookie, csrf_token)
        authenticated = await services.members.authenticate(
            AuthenticateMember(payload.login_id, payload.password)
        )
        issued = await services.sessions.issue_member(authenticated.member.member_id, current)
        _set_session_cookie(response, services.settings, issued.token)
        return IssuedSessionResponse(
            actor_type=issued.record.actor_type,
            actor_id=issued.record.actor_id,
            display_name=authenticated.member.nickname,
            csrf_token=issued.csrf_token,
            absolute_expires_at_ms=issued.record.absolute_expires_at_ms,
        )

    @router.get(
        "/session",
        response_model=CurrentSessionResponse,
        responses=identity_problem_responses(401, 503),
    )
    async def current_session(
        session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    ) -> CurrentSessionResponse:
        current = await require_current_session(services, session_cookie, touch=True)
        return await _current_response(services, current)

    @router.post(
        "/session/csrf",
        response_model=CsrfBootstrapResponse,
        responses=identity_problem_responses(401, 403, 422, 503),
        # Keep the security rejection at 403 instead of Pydantic's missing-header 422.
        openapi_extra={
            "parameters": [
                {
                    "name": "X-CSRF-Bootstrap",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string", "enum": ["1"]},
                    "description": "Required for Cookie-based CSRF recovery; exact value 1.",
                }
            ]
        },
    )
    async def recover_csrf(
        payload: CsrfBootstrapRequest,
        request: Request,
        response: Response,
        session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    ) -> CsrfBootstrapResponse:
        # This read-only bootstrap cannot require the token it recovers.
        # Unlike ordinary API calls, a Referer fallback is not accepted here.
        if request.headers.get("Origin") not in services.settings.allowed_origins:
            raise ApiProblem(403, "ORIGIN_NOT_ALLOWED", "Request origin is not allowed")
        content_type = request.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if request.headers.get("X-CSRF-Bootstrap") != "1" or content_type != "application/json":
            raise ApiProblem(403, "CSRF_BOOTSTRAP_INVALID", "Invalid CSRF recovery request")
        current = await require_current_session(services, session_cookie, touch=False)
        identity = await _current_response(services, current)
        response.headers["Cache-Control"] = "no-store"
        return CsrfBootstrapResponse(**identity.model_dump(), csrf_token=current.csrf_token)

    @router.delete(
        "/session",
        status_code=status.HTTP_204_NO_CONTENT,
        responses=identity_problem_responses(401, 403, 503),
    )
    async def logout(
        request: Request,
        response: Response,
        session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
        csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    ) -> None:
        require_allowed_origin(services.settings, request)
        current = await require_current_session(services, session_cookie)
        require_csrf(current, csrf_token)
        await services.sessions.logout(current)
        response.delete_cookie(
            SESSION_COOKIE,
            path="/",
            secure=services.settings.environment == "production",
            httponly=True,
            samesite="lax",
        )

    return router


async def state_change_context(
    services: IdentityApiServices,
    request: Request,
    raw_session: str | None,
    csrf_token: str | None,
) -> SessionRecord | None:
    require_allowed_origin(services.settings, request)
    if raw_session is None:
        return None
    current = await services.sessions.find(digest_opaque_token(raw_session))
    if current is None:
        return None
    require_csrf(current, csrf_token)
    return current


async def require_current_session(
    services: IdentityApiServices,
    raw_session: str | None,
    *,
    touch: bool = False,
) -> SessionRecord:
    if raw_session is None:
        raise ApiProblem(401, "AUTH_REQUIRED", "Authentication required")
    digest = digest_opaque_token(raw_session)
    current = (
        await services.sessions.current(digest) if touch else await services.sessions.find(digest)
    )
    if current is None:
        raise ApiProblem(401, "AUTH_REQUIRED", "Authentication required")
    return current


def require_csrf(current: SessionRecord, raw_csrf: str | None) -> None:
    if raw_csrf is None:
        raise ApiProblem(403, "CSRF_INVALID", "CSRF validation failed")
    supplied = digest_opaque_token(raw_csrf)
    if not hmac.compare_digest(supplied, current.csrf_digest):
        raise ApiProblem(403, "CSRF_INVALID", "CSRF validation failed")


def require_allowed_origin(settings: Settings, request: Request) -> None:
    origin = request.headers.get("Origin")
    if origin is None:
        referer = request.headers.get("Referer")
        if referer is not None:
            parsed = urlsplit(referer)
            origin = (
                f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else None
            )
    if origin not in settings.allowed_origins:
        raise ApiProblem(403, "ORIGIN_NOT_ALLOWED", "Request origin is not allowed")


async def _current_response(
    services: IdentityApiServices,
    current: SessionRecord,
) -> CurrentSessionResponse:
    if current.actor_type is SessionActorType.GUEST:
        display_name = guest_display_name(current.actor_id)
    else:
        try:
            member_id = int(current.actor_id)
        except ValueError as error:
            raise ApiProblem(401, "AUTH_REQUIRED", "Authentication required") from error
        member = await services.members.find_member(member_id)
        if member is None:
            raise ApiProblem(401, "AUTH_REQUIRED", "Authentication required")
        display_name = member.nickname
    participation = (
        None
        if services.participations is None
        else await services.participations.resolve_current_room(current.session_digest)
    )
    return CurrentSessionResponse(
        actor_type=current.actor_type,
        actor_id=current.actor_id,
        display_name=display_name,
        absolute_expires_at_ms=current.absolute_expires_at_ms,
        room_id=None if participation is None else participation[0],
        participant_id=None if participation is None else participation[1],
    )


def _member_response(member: Member) -> MemberResponse:
    return MemberResponse(
        member_id=member.member_id,
        login_id=member.login_id,
        nickname=member.nickname,
        rating=member.rating,
    )


def guest_display_name(actor_id: str) -> str:
    number = int(hashlib.sha256(actor_id.encode("utf-8")).hexdigest()[:8], 16) % 10_000
    return f"Guest-{number:04d}"


def _set_session_cookie(response: Response, settings: Settings, token: str) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=settings.environment == "production",
        samesite="lax",
        path="/",
    )
