"""Lobby and Room HTTP endpoints for the headless MVP flow."""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Annotated, Protocol
from uuid import UUID

from fastapi import APIRouter, Cookie, Header, Request, status
from pydantic import BaseModel, ConfigDict, Field

from seokpan.api.identity import (
    SESSION_COOKIE,
    IdentityApiServices,
    guest_display_name,
    require_allowed_origin,
    require_csrf,
    require_current_session,
)
from seokpan.api.problems import ApiProblem, room_problem_responses
from seokpan.identity.application import SessionActorType, SessionRecord
from seokpan.room.application import RoomApplicationService, RoomMutationResult, RoomRuntimeSnapshot
from seokpan.room.domain import (
    GameTermination,
    RoomConfig,
    RoomRuleViolation,
    RoomStatus,
    RoomVisibility,
    Team,
)

UNKNOWN_PARTICIPANT_DISPLAY_NAME = "참가자"


class ConfirmedDepartureFinalizer(Protocol):
    async def finalize_departures(self, *, room_id: str, game_id: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class RoomApiServices:
    identity: IdentityApiServices
    rooms: RoomApplicationService
    departures: ConfirmedDepartureFinalizer | None = None


class CreateRoomRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    name: str
    visibility: RoomVisibility = RoomVisibility.PUBLIC
    password: str | None = Field(default=None, repr=False)
    max_participants: int = 100
    minimum_ready: int = 4
    vote_seconds: int = 15


class JoinRoomRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    expected_state_version: int = Field(ge=1)
    password: str | None = Field(default=None, repr=False)


class VersionedMutationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    expected_state_version: int = Field(ge=1)


class ChangeRoomSettingsRequest(VersionedMutationRequest):
    vote_seconds: int


class ChangeTeamRequest(VersionedMutationRequest):
    team: Team


class SetReadyRequest(VersionedMutationRequest):
    ready: bool


class RoomParticipantResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    participant_id: str
    actor_type: str
    display_name: str
    joined_order: int
    connected: bool
    team: Team
    ready: bool


class RoomSnapshotResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    room_id: str
    name: str
    visibility: RoomVisibility
    password_required: bool
    max_participants: int
    minimum_ready: int
    vote_seconds: int
    status: RoomStatus
    owner_id: str | None
    state_version: int
    participants: list[RoomParticipantResponse]
    game_id: str | None = None
    last_game_id: str | None = None
    replayed: bool = False


class LobbyRoomResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    room_id: str
    name: str
    visibility: RoomVisibility
    password_required: bool
    participant_count: int
    max_participants: int
    minimum_ready: int
    vote_seconds: int
    status: RoomStatus
    state_version: int


class LobbyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rooms: list[LobbyRoomResponse]


def room_router(services: RoomApiServices) -> APIRouter:
    router = APIRouter(prefix="/api/v1/rooms", tags=["rooms"])

    @router.get("", response_model=LobbyResponse, responses=room_problem_responses(401, 503))
    async def list_rooms(
        session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    ) -> LobbyResponse:
        await require_current_session(services.identity, session_cookie, touch=True)
        snapshots = await services.rooms.list_rooms()
        return LobbyResponse(rooms=[lobby_room_response(item) for item in snapshots])

    @router.post(
        "",
        response_model=RoomSnapshotResponse,
        status_code=status.HTTP_201_CREATED,
        responses=room_problem_responses(401, 403, 409, 422, 503),
    )
    async def create_room(
        payload: CreateRoomRequest,
        request: Request,
        session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
        csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    ) -> RoomSnapshotResponse:
        current = await _mutation_session(services.identity, request, session_cookie, csrf_token)
        result = await services.rooms.create_room(
            session=current,
            request_id=_uuid4(payload.request_id),
            config=RoomConfig(
                name=payload.name,
                visibility=payload.visibility,
                max_participants=payload.max_participants,
                minimum_ready=payload.minimum_ready,
                vote_seconds=payload.vote_seconds,
            ),
            password=payload.password,
        )
        return await room_snapshot_response(services, result.snapshot, result.replayed)

    @router.get(
        "/{room_id}/snapshot",
        response_model=RoomSnapshotResponse,
        responses=room_problem_responses(401, 404, 503),
    )
    async def room_snapshot(
        room_id: str,
        session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    ) -> RoomSnapshotResponse:
        current = await require_current_session(services.identity, session_cookie, touch=True)
        await _require_current_room(services.rooms, current, room_id)
        snapshot = await services.rooms.get(room_id)
        return await room_snapshot_response(services, snapshot)

    @router.post(
        "/{room_id}/joins",
        response_model=RoomSnapshotResponse,
        status_code=status.HTTP_201_CREATED,
        responses=room_problem_responses(401, 403, 404, 409, 422, 503),
    )
    async def join_room(
        room_id: str,
        payload: JoinRoomRequest,
        request: Request,
        session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
        csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    ) -> RoomSnapshotResponse:
        current = await _mutation_session(services.identity, request, session_cookie, csrf_token)
        result = await _stale_guard(
            services,
            room_id,
            services.rooms.join_room(
                session=current,
                room_id=room_id,
                request_id=_uuid4(payload.request_id),
                expected_state_version=payload.expected_state_version,
                password=payload.password,
            ),
        )
        return await room_snapshot_response(services, result.snapshot, result.replayed)

    @router.delete(
        "/{room_id}/participants/me",
        response_model=RoomSnapshotResponse | None,
        responses=room_problem_responses(401, 403, 404, 409, 422, 503),
    )
    async def leave_room(
        room_id: str,
        payload: VersionedMutationRequest,
        request: Request,
        session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
        csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    ) -> RoomSnapshotResponse | None:
        current = await _mutation_session(services.identity, request, session_cookie, csrf_token)
        await _require_current_room(services.rooms, current, room_id)
        result = await _stale_guard(
            services,
            room_id,
            services.rooms.leave_room(
                session=current,
                request_id=_uuid4(payload.request_id),
                expected_state_version=payload.expected_state_version,
            ),
        )
        if (
            services.departures is not None
            and not result.replayed
            and result.game_termination is not GameTermination.SYSTEM_INVALID
            and result.snapshot is not None
            and result.snapshot.game_id is not None
        ):
            await services.departures.finalize_departures(
                room_id=room_id,
                game_id=result.snapshot.game_id,
            )
        latest = await services.rooms.get(room_id)
        if latest is None:
            return None
        return await room_snapshot_response(services, latest, result.replayed)

    @router.post(
        "/{room_id}/participants/{participant_id}/kick",
        response_model=RoomSnapshotResponse,
        responses=room_problem_responses(401, 403, 404, 409, 422, 503),
    )
    async def kick_participant(
        room_id: str,
        participant_id: str,
        payload: VersionedMutationRequest,
        request: Request,
        session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
        csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    ) -> RoomSnapshotResponse:
        current = await _mutation_session(services.identity, request, session_cookie, csrf_token)
        await _require_current_room(services.rooms, current, room_id)
        result = await _stale_guard(
            services,
            room_id,
            services.rooms.kick_participant(
                session=current,
                target_id=participant_id,
                request_id=_uuid4(payload.request_id),
                expected_state_version=payload.expected_state_version,
            ),
        )
        return await room_snapshot_response(services, result.snapshot, result.replayed)

    @router.patch(
        "/{room_id}/settings",
        response_model=RoomSnapshotResponse,
        responses=room_problem_responses(401, 403, 404, 409, 422, 503),
    )
    async def change_settings(
        room_id: str,
        payload: ChangeRoomSettingsRequest,
        request: Request,
        session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
        csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    ) -> RoomSnapshotResponse:
        current = await _mutation_session(services.identity, request, session_cookie, csrf_token)
        await _require_current_room(services.rooms, current, room_id)
        result = await _stale_guard(
            services,
            room_id,
            services.rooms.change_vote_seconds(
                session=current,
                request_id=_uuid4(payload.request_id),
                expected_state_version=payload.expected_state_version,
                vote_seconds=payload.vote_seconds,
            ),
        )
        return await room_snapshot_response(services, result.snapshot, result.replayed)

    @router.put(
        "/{room_id}/participants/me/team",
        response_model=RoomSnapshotResponse,
        responses=room_problem_responses(401, 403, 404, 409, 422, 503),
    )
    async def change_team(
        room_id: str,
        payload: ChangeTeamRequest,
        request: Request,
        session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
        csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    ) -> RoomSnapshotResponse:
        current = await _mutation_session(services.identity, request, session_cookie, csrf_token)
        await _require_current_room(services.rooms, current, room_id)
        result = await _stale_guard(
            services,
            room_id,
            services.rooms.change_team(
                session=current,
                request_id=_uuid4(payload.request_id),
                expected_state_version=payload.expected_state_version,
                team=payload.team,
            ),
        )
        return await room_snapshot_response(services, result.snapshot, result.replayed)

    @router.put(
        "/{room_id}/participants/me/ready",
        response_model=RoomSnapshotResponse,
        responses=room_problem_responses(401, 403, 404, 409, 422, 503),
    )
    async def set_ready(
        room_id: str,
        payload: SetReadyRequest,
        request: Request,
        session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
        csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    ) -> RoomSnapshotResponse:
        current = await _mutation_session(services.identity, request, session_cookie, csrf_token)
        await _require_current_room(services.rooms, current, room_id)
        result = await _stale_guard(
            services,
            room_id,
            services.rooms.set_ready(
                session=current,
                request_id=_uuid4(payload.request_id),
                expected_state_version=payload.expected_state_version,
                ready=payload.ready,
            ),
        )
        return await room_snapshot_response(services, result.snapshot, result.replayed)

    return router


async def _mutation_session(
    services: IdentityApiServices,
    request: Request,
    raw_session: str | None,
    csrf_token: str | None,
) -> SessionRecord:
    require_allowed_origin(services.settings, request)
    current = await require_current_session(services, raw_session)
    require_csrf(current, csrf_token)
    return current


def _uuid4(value: UUID) -> str:
    if value.version != 4:
        raise ApiProblem(422, "INVALID_REQUEST_ID", "Request ID must be UUIDv4")
    return str(value)


async def _require_current_room(
    rooms: RoomApplicationService,
    session: SessionRecord,
    room_id: str,
) -> None:
    participation = await rooms.resolve_participation(session.session_digest)
    if participation is None or participation.room_id != room_id:
        raise RoomRuleViolation("SESSION_NOT_IN_ROOM")


async def _stale_guard(
    services: RoomApiServices,
    room_id: str,
    operation: Awaitable[RoomMutationResult],
) -> RoomMutationResult:
    try:
        return await operation
    except RoomRuleViolation as error:
        if error.code != "STATE_VERSION_CONFLICT":
            raise
        snapshot = await services.rooms.get(room_id)
        raise ApiProblem(
            409,
            "STALE_STATE",
            "Room state has changed",
            current_version=None if snapshot is None else snapshot.state_version,
            snapshot_url=f"/api/v1/rooms/{room_id}/snapshot",
        ) from error


async def room_snapshot_response(
    services: RoomApiServices,
    snapshot: RoomRuntimeSnapshot | None,
    replayed: bool = False,
) -> RoomSnapshotResponse:
    if snapshot is None:
        raise RoomRuleViolation("ROOM_NOT_FOUND")
    participants: list[RoomParticipantResponse] = []
    for participant in snapshot.participants:
        identity = await services.rooms.resolve_participant_identity(participant.participant_id)
        if identity is None:
            display_name = UNKNOWN_PARTICIPANT_DISPLAY_NAME
        elif identity.actor_type is SessionActorType.GUEST:
            display_name = guest_display_name(identity.actor_id)
        else:
            member = await services.identity.members.find_member(int(identity.actor_id))
            display_name = UNKNOWN_PARTICIPANT_DISPLAY_NAME if member is None else member.nickname
        participants.append(
            RoomParticipantResponse(
                participant_id=participant.participant_id,
                actor_type=participant.actor_type.value,
                display_name=display_name,
                joined_order=participant.joined_order,
                connected=participant.connected,
                team=participant.team,
                ready=participant.ready,
            )
        )
    return RoomSnapshotResponse(
        room_id=snapshot.room_id,
        name=snapshot.config.name,
        visibility=snapshot.config.visibility,
        password_required=snapshot.password_required,
        max_participants=snapshot.config.max_participants,
        minimum_ready=snapshot.config.minimum_ready,
        vote_seconds=snapshot.config.vote_seconds,
        status=snapshot.status,
        owner_id=snapshot.owner_id,
        state_version=snapshot.state_version,
        participants=participants,
        game_id=snapshot.game_id,
        last_game_id=snapshot.last_game_id,
        replayed=replayed,
    )


def lobby_room_response(snapshot: RoomRuntimeSnapshot) -> LobbyRoomResponse:
    return LobbyRoomResponse(
        room_id=snapshot.room_id,
        name=snapshot.config.name,
        visibility=snapshot.config.visibility,
        password_required=snapshot.password_required,
        participant_count=len(snapshot.participants),
        max_participants=snapshot.config.max_participants,
        minimum_ready=snapshot.config.minimum_ready,
        vote_seconds=snapshot.config.vote_seconds,
        status=snapshot.status,
        state_version=snapshot.state_version,
    )
