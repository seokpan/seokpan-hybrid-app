"""Game start, snapshot, and Vote HTTP endpoints for the Headless MVP flow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Cookie, Header, Request, status
from pydantic import BaseModel, ConfigDict, Field

from seokpan.api.identity import (
    SESSION_COOKIE,
    IdentityApiServices,
    require_allowed_origin,
    require_csrf,
    require_current_session,
)
from seokpan.api.problems import ApiProblem, game_problem_responses
from seokpan.game.application import GameApplicationService, GameApplicationSnapshot
from seokpan.game.application.service import GameResultSnapshot
from seokpan.game.domain import EndReason, Game, GameStatus, MemberOutcome, Stone
from seokpan.identity.application import SessionRecord
from seokpan.room.domain import ParticipantRole, RoomRuleViolation
from seokpan.vote.domain import TurnStatus, VoteRuleViolation


@dataclass(frozen=True, slots=True)
class GameApiServices:
    identity: IdentityApiServices
    games: GameApplicationService


class StartGameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    expected_state_version: int = Field(ge=1)


class VoteRequest(StartGameRequest):
    coordinate: str


class GameParticipantResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    participant_id: str
    actor_type: str
    role: ParticipantRole
    team: Stone | None
    connected: bool


class BoardCellResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    coordinate: str
    stone: Stone


class VoteTallyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    coordinate: str
    count: int


class LastMoveResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    move_no: int = Field(ge=1, le=225)
    team: Stone
    coordinate: str


class PersonalRatingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: MemberOutcome
    rating_before: int
    rating_delta: int
    rating_after: int


class GameResultResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    game_id: str
    room_id: str
    turn_no: int
    move_no: int
    game_status: GameStatus
    end_reason: EndReason
    winner: Stone
    board: list[BoardCellResponse]
    last_move: LastMoveResponse | None = None
    winning_line: list[str] | None
    ended_at: datetime
    stats_eligible: bool
    my_rating: PersonalRatingResponse | None


class GameSnapshotResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    game_id: str
    room_id: str
    game_status: GameStatus
    turn_status: TurnStatus | None
    turn_no: int
    move_no: int
    current_team: Stone | None
    deadline_ms: int | None
    server_now_ms: int
    state_version: int
    board: list[BoardCellResponse]
    last_move: LastMoveResponse | None = None
    forbidden_for_black: list[str]
    participants: list[GameParticipantResponse]
    vote_aggregation: list[VoteTallyResponse]
    valid_voter_count: int
    candidates: list[str]
    my_vote: str | None
    can_vote: bool
    replayed: bool = False


def game_router(services: GameApiServices) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["games"])

    @router.post(
        "/rooms/{room_id}/games",
        response_model=GameSnapshotResponse,
        status_code=status.HTTP_201_CREATED,
        responses=game_problem_responses(401, 403, 404, 409, 422, 503),
    )
    async def start_game(
        room_id: str,
        payload: StartGameRequest,
        request: Request,
        session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
        csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    ) -> GameSnapshotResponse:
        current = await _mutation_session(services.identity, request, session_cookie, csrf_token)
        try:
            snapshot = await services.games.start_game(
                session=current,
                room_id=room_id,
                request_id=_uuid4(payload.request_id),
                expected_state_version=payload.expected_state_version,
            )
        except (RoomRuleViolation, VoteRuleViolation) as error:
            await _raise_stale(services, current, error)
            raise
        return game_snapshot_response(snapshot)

    @router.get(
        "/games/{game_id}",
        response_model=GameSnapshotResponse,
        responses=game_problem_responses(401, 403, 404, 503),
    )
    async def get_game(
        game_id: str,
        session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    ) -> GameSnapshotResponse:
        current = await require_current_session(services.identity, session_cookie, touch=True)
        return game_snapshot_response(
            await services.games.get_game(session=current, game_id=game_id)
        )

    @router.put(
        "/games/{game_id}/turns/{turn_no}/vote",
        response_model=GameSnapshotResponse,
        responses=game_problem_responses(401, 403, 404, 409, 422, 503),
    )
    async def put_vote(
        game_id: str,
        turn_no: int,
        payload: VoteRequest,
        request: Request,
        session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
        csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    ) -> GameSnapshotResponse:
        current = await _mutation_session(services.identity, request, session_cookie, csrf_token)
        try:
            snapshot = await services.games.cast_vote(
                session=current,
                game_id=game_id,
                turn_no=turn_no,
                request_id=_uuid4(payload.request_id),
                coordinate=payload.coordinate,
                expected_state_version=payload.expected_state_version,
            )
        except VoteRuleViolation as error:
            await _raise_stale(services, current, error)
            raise
        return game_snapshot_response(snapshot)

    @router.get(
        "/games/{game_id}/result",
        response_model=GameResultResponse,
        responses=game_problem_responses(401, 403, 404, 409, 503),
    )
    async def get_result(
        game_id: str,
        session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    ) -> GameResultResponse:
        current = await require_current_session(services.identity, session_cookie, touch=True)
        return game_result_response(
            await services.games.get_result(session=current, game_id=game_id)
        )

    @router.delete(
        "/games/{game_id}/turns/{turn_no}/vote",
        response_model=GameSnapshotResponse,
        responses=game_problem_responses(401, 403, 404, 409, 422, 503),
    )
    async def delete_vote(
        game_id: str,
        turn_no: int,
        payload: StartGameRequest,
        request: Request,
        session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
        csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    ) -> GameSnapshotResponse:
        current = await _mutation_session(services.identity, request, session_cookie, csrf_token)
        try:
            snapshot = await services.games.remove_vote(
                session=current,
                game_id=game_id,
                turn_no=turn_no,
                request_id=_uuid4(payload.request_id),
                expected_state_version=payload.expected_state_version,
            )
        except VoteRuleViolation as error:
            await _raise_stale(services, current, error)
            raise
        return game_snapshot_response(snapshot)

    return router


def game_result_response(snapshot: GameResultSnapshot) -> GameResultResponse:
    stored, board, rating = snapshot.result, snapshot.board, snapshot.my_rating
    return GameResultResponse(
        game_id=stored.game_id,
        room_id=stored.room_id,
        turn_no=snapshot.turn_no,
        move_no=board.move_no,
        last_move=None
        if board.last_move is None
        else LastMoveResponse(
            move_no=board.last_move.move_no,
            team=board.last_move.team,
            coordinate=board.last_move.coordinate.canonical,
        ),
        game_status=stored.status,
        end_reason=stored.end_reason,
        winner=stored.winner,
        board=[
            BoardCellResponse(coordinate=item.coordinate.canonical, stone=item.stone)
            for item in board.occupied_cells
        ],
        winning_line=[item.canonical for item in board.winning_line] or None,
        ended_at=stored.ended_at,
        stats_eligible=stored.stats_eligible,
        my_rating=None
        if rating is None
        else PersonalRatingResponse(
            outcome=rating.outcome,
            rating_before=rating.rating_before,
            rating_delta=rating.rating_delta,
            rating_after=rating.rating_after,
        ),
    )


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


async def _raise_stale(
    services: GameApiServices,
    session: SessionRecord,
    error: RoomRuleViolation | VoteRuleViolation,
) -> None:
    if error.code != "STATE_VERSION_CONFLICT":
        return
    version, snapshot_url = await services.games.current_state_reference(session)
    raise ApiProblem(
        409,
        "STALE_STATE",
        "Game state has changed",
        current_version=version,
        snapshot_url=snapshot_url,
    ) from error


def game_snapshot_response(value: GameApplicationSnapshot) -> GameSnapshotResponse:
    runtime = value.game
    voters = {item.participant_id: item for item in runtime.participants}
    my_vote = next(
        (
            item.coordinate.canonical
            for item in runtime.votes
            if item.participant_id == value.viewer_participant_id
        ),
        None,
    )
    viewer = voters.get(value.viewer_participant_id)
    active = runtime.game_status is GameStatus.ACTIVE
    can_vote = bool(
        viewer is not None
        and viewer.role.value == "PLAYER"
        and viewer.connected
        and viewer.team is runtime.current_team
        and runtime.turn_status is TurnStatus.VOTING
        and runtime.deadline_ms is not None
        and value.now_ms < runtime.deadline_ms
    )
    forbidden = (
        Game.black_forbidden_coordinates(runtime.occupied_cells)
        if active and runtime.current_team is Stone.BLACK
        else ()
    )
    return GameSnapshotResponse(
        game_id=runtime.game_id,
        room_id=runtime.room_id,
        game_status=runtime.game_status,
        turn_status=runtime.turn_status if active else None,
        turn_no=runtime.turn_no,
        move_no=runtime.move_no,
        last_move=None
        if runtime.last_move is None
        else LastMoveResponse(
            move_no=runtime.last_move.move_no,
            team=runtime.last_move.team,
            coordinate=runtime.last_move.coordinate.canonical,
        ),
        current_team=runtime.current_team if active else None,
        deadline_ms=runtime.deadline_ms if active else None,
        server_now_ms=value.now_ms,
        state_version=runtime.state_version,
        board=[
            BoardCellResponse(coordinate=item.coordinate.canonical, stone=item.stone)
            for item in runtime.occupied_cells
        ],
        forbidden_for_black=[item.canonical for item in forbidden],
        participants=[
            GameParticipantResponse(
                participant_id=item.participant_id,
                actor_type=item.actor_type.value,
                role=(
                    ParticipantRole.PLAYER
                    if item.participant_id in voters
                    else ParticipantRole.SPECTATOR
                ),
                team=None
                if item.participant_id not in voters
                else voters[item.participant_id].team,
                connected=item.connected,
            )
            for item in value.room.participants
        ],
        vote_aggregation=[
            VoteTallyResponse(coordinate=item.coordinate.canonical, count=item.count)
            for item in runtime.tally
        ],
        valid_voter_count=sum(
            item.role.value == "PLAYER" and item.connected and item.team is runtime.current_team
            for item in runtime.participants
        ),
        candidates=[item.canonical for item in runtime.candidates],
        my_vote=my_vote,
        can_vote=can_vote,
        replayed=value.replayed,
    )
