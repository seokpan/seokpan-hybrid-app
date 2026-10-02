"""Session-scoped public rankings and the current Member's cumulative statistics."""

from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Cookie, Query, Response
from pydantic import BaseModel, ConfigDict

from seokpan.api.identity import SESSION_COOKIE, IdentityApiServices, require_current_session
from seokpan.api.problems import ApiProblem, game_problem_responses
from seokpan.identity.application import SessionActorType
from seokpan.statistics import (
    MemberStatistics,
    StatisticsQuery,
    StatisticsReader,
    StatisticsUnavailable,
)


@dataclass(frozen=True, slots=True)
class StatisticsApiServices:
    identity: IdentityApiServices
    reader: StatisticsReader


class MemberStatisticsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)
    member_id: int
    nickname: str
    rating: int
    wins: int
    draws: int
    losses: int
    games_played: int
    rank: int | None


class RankingsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[MemberStatisticsResponse]
    offset: int
    limit: int
    has_more: bool
    me: MemberStatisticsResponse | None


def _response(row: MemberStatistics | None) -> MemberStatisticsResponse | None:
    return None if row is None else MemberStatisticsResponse.model_validate(row)


def statistics_router(services: StatisticsApiServices) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["statistics"])

    @router.get(
        "/rankings",
        response_model=RankingsResponse,
        responses=game_problem_responses(401, 422, 503),
    )
    async def rankings(
        response: Response,
        offset: Annotated[int, Query(ge=0, le=2_147_483_647)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    ) -> RankingsResponse:
        current = await require_current_session(services.identity, session_cookie, touch=True)
        member_id = int(current.actor_id) if current.actor_type is SessionActorType.MEMBER else None
        try:
            page = await services.reader.read(StatisticsQuery(member_id, offset, limit))
        except StatisticsUnavailable as error:
            raise ApiProblem(503, "STATISTICS_UNAVAILABLE", "Statistics are unavailable") from error
        response.headers["Cache-Control"] = "no-store"
        return RankingsResponse(
            items=[MemberStatisticsResponse.model_validate(row) for row in page.items],
            offset=offset,
            limit=limit,
            has_more=page.has_more,
            me=_response(page.me),
        )

    return router
