from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from seokpan.app import build_headless_services, create_app
from seokpan.settings import Settings
from seokpan.statistics import StatisticsUnavailable

ORIGIN = "http://localhost:5173"


def test_guest_can_read_rankings_without_creating_personal_statistics() -> None:
    with TestClient(create_app(settings=Settings(environment="test")), base_url=ORIGIN) as client:
        assert client.get("/api/v1/rankings").status_code == 401
        assert client.post("/api/v1/sessions/guest", headers={"Origin": ORIGIN}).status_code == 201
        before = client.get("/api/v1/session").json()
        response = client.get("/api/v1/rankings?member_id=1")
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert response.json() == {
            "items": [],
            "offset": 0,
            "limit": 20,
            "has_more": False,
            "me": None,
        }
        assert client.get("/api/v1/session").json() == before
        assert "Set-Cookie" not in response.headers


def test_new_member_has_initial_rating_zero_record_and_no_rank() -> None:
    with TestClient(create_app(settings=Settings(environment="test")), base_url=ORIGIN) as client:
        registered = client.post(
            "/api/v1/members",
            headers={"Origin": ORIGIN},
            json={
                "login_id": "rank_member",
                "nickname": "돌장인",
                "password": "correct-pass",
            },
        )
        assert registered.status_code == 201
        login = client.post(
            "/api/v1/sessions/member",
            headers={"Origin": ORIGIN},
            json={
                "login_id": "rank_member",
                "password": "correct-pass",
            },
        )
        assert login.status_code == 200
        response = client.get("/api/v1/rankings?offset=100&member_id=999")
        assert response.status_code == 200
        assert response.json()["items"] == []
        assert response.json()["me"] == {
            "member_id": registered.json()["member_id"],
            "nickname": "돌장인",
            "rating": 1000,
            "wins": 0,
            "draws": 0,
            "losses": 0,
            "games_played": 0,
            "rank": None,
        }
        for private in ("login_id", "rank_member", "password", "csrf", "session", "participant"):
            assert private not in response.text
        logout = client.delete(
            "/api/v1/session",
            headers={
                "Origin": ORIGIN,
                "X-CSRF-Token": login.json()["csrf_token"],
            },
        )
        assert logout.status_code == 204
        assert client.get("/api/v1/rankings").status_code == 401


@pytest.mark.parametrize(
    "query", ["offset=-1", "offset=2147483648", "limit=0", "limit=101", "limit=bad"]
)
def test_invalid_page_does_not_call_statistics_provider(query: str) -> None:
    settings = Settings(environment="test")
    services = build_headless_services(settings)
    assert services.statistics_api is not None
    reader = AsyncMock()
    services = replace(services, statistics_api=replace(services.statistics_api, reader=reader))
    with TestClient(create_app(settings=settings, services=services), base_url=ORIGIN) as client:
        assert client.post("/api/v1/sessions/guest", headers={"Origin": ORIGIN}).status_code == 201
        assert client.get(f"/api/v1/rankings?{query}").status_code == 422
    reader.read.assert_not_awaited()


def test_read_failure_returns_problem_without_internal_details_or_successful_empty_page() -> None:
    settings = Settings(environment="test")
    services = build_headless_services(settings)
    assert services.statistics_api is not None
    reader = AsyncMock()
    reader.read.side_effect = StatisticsUnavailable("private connection detail")
    services = replace(services, statistics_api=replace(services.statistics_api, reader=reader))
    with TestClient(create_app(settings=settings, services=services), base_url=ORIGIN) as client:
        assert client.post("/api/v1/sessions/guest", headers={"Origin": ORIGIN}).status_code == 201
        response = client.get("/api/v1/rankings")
    assert response.status_code == 503
    assert response.headers["Content-Type"].startswith("application/problem+json")
    assert response.json()["code"] == "STATISTICS_UNAVAILABLE"
    assert "private" not in response.text and "items" not in response.json()
