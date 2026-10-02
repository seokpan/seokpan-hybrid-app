"""Cookie-only page startup without weakening ordinary command validation."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from seokpan.app import ApplicationServices, build_headless_services, create_app
from seokpan.identity.application import digest_opaque_token
from seokpan.identity.application.session import SESSION_IDLE_TTL_MS
from seokpan.settings import Settings

ORIGIN = "http://localhost:5173"
HEADERS = {"Origin": ORIGIN, "X-CSRF-Bootstrap": "1"}
URL = "/api/v1/session/csrf"


@pytest.fixture
def harness() -> tuple[TestClient, ApplicationServices]:
    settings = Settings(environment="test", allowed_origins=(ORIGIN,))
    services = build_headless_services(settings)
    return TestClient(create_app(settings=settings, services=services), base_url=ORIGIN), services


def issue_guest(client: TestClient) -> tuple[str, str]:
    issued = client.post("/api/v1/sessions/guest", headers={"Origin": ORIGIN})
    assert issued.status_code == 201
    assert issued.headers["Cache-Control"] == "no-store"
    return issued.cookies["seokpan_session"], issued.json()["csrf_token"]


def test_recovery_is_stable_and_does_not_touch_session(
    harness: tuple[TestClient, ApplicationServices],
) -> None:
    client, services = harness
    with client:
        cookie, csrf = issue_guest(client)
        digest = digest_opaque_token(cookie)
        before = asyncio.run(services.identity_api.sessions.find(digest))
        assert services.headless_clock is not None
        services.headless_clock.advance(12_345)
        for _ in range(2):
            response = client.post(URL, headers=HEADERS, json={})
            assert response.status_code == 200
            assert response.json()["csrf_token"] == csrf
            assert response.json()["actor_type"] == "GUEST"
            assert response.headers["Cache-Control"] == "no-store"
            assert "set-cookie" not in response.headers
        assert asyncio.run(services.identity_api.sessions.find(digest)) == before
        # Normal session activity still refreshes idle expiry.
        assert client.get("/api/v1/session").status_code == 200
        after = asyncio.run(services.identity_api.sessions.find(digest))
        assert after is not None and after.last_activity_at_ms == 12_345
        assert csrf not in client.get("/api/v1/session").text
        assert csrf not in repr(after)
        assert client.delete("/api/v1/session", headers={"Origin": ORIGIN}).status_code == 403
        assert (
            client.delete(
                "/api/v1/session", headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}
            ).status_code
            == 204
        )
        assert client.post(URL, headers=HEADERS, json={}).status_code == 401


@pytest.mark.parametrize("origin", [None, "null", "https://evil.example", ORIGIN + ".evil.example"])
def test_recovery_requires_exact_origin_even_with_valid_referer(
    harness: tuple[TestClient, ApplicationServices],
    origin: str | None,
) -> None:
    client, _ = harness
    with client:
        issue_guest(client)
        headers = {"Referer": ORIGIN + "/room", "X-CSRF-Bootstrap": "1"}
        if origin is not None:
            headers["Origin"] = origin
        response = client.post(URL, headers=headers, json={})
        assert response.status_code == 403
        assert response.json()["code"] == "ORIGIN_NOT_ALLOWED"
        assert "csrf_token" not in response.text


@pytest.mark.parametrize("marker", [None, "0", "true"])
def test_recovery_requires_bootstrap_marker(
    harness: tuple[TestClient, ApplicationServices],
    marker: str | None,
) -> None:
    client, _ = harness
    with client:
        issue_guest(client)
        headers = {"Origin": ORIGIN}
        if marker is not None:
            headers["X-CSRF-Bootstrap"] = marker
        response = client.post(URL, headers=headers, json={})
        assert response.status_code == 403
        assert response.json()["code"] == "CSRF_BOOTSTRAP_INVALID"


@pytest.mark.parametrize(
    "content_type",
    [None, "text/plain", "application/x-www-form-urlencoded", "application/problem+json"],
)
def test_recovery_rejects_non_json_media_type(
    harness: tuple[TestClient, ApplicationServices],
    content_type: str | None,
) -> None:
    client, _ = harness
    with client:
        issue_guest(client)
        headers = dict(HEADERS)
        if content_type is not None:
            headers["Content-Type"] = content_type
        response = client.post(URL, headers=headers, content="{}")
        assert response.status_code in (403, 422)
        assert "csrf_token" not in response.text


def test_recovery_does_not_extend_idle_or_restore_expired_cookie(
    harness: tuple[TestClient, ApplicationServices],
) -> None:
    client, services = harness
    with client:
        issue_guest(client)
        assert services.headless_clock is not None
        services.headless_clock.advance(SESSION_IDLE_TTL_MS - 1)
        assert client.post(URL, headers=HEADERS, json={}).status_code == 200
        services.headless_clock.advance(1)
        response = client.post(URL, headers=HEADERS, json={})
        assert response.status_code == 401
        assert "set-cookie" not in response.headers


def test_simultaneous_recovery_reads_one_value(
    harness: tuple[TestClient, ApplicationServices],
) -> None:
    client, _ = harness
    with client:
        _, csrf = issue_guest(client)

        def recover(_: int) -> str:
            response = client.post(URL, headers=HEADERS, json={})
            assert response.status_code == 200
            return str(response.json()["csrf_token"])

        with ThreadPoolExecutor(max_workers=4) as pool:
            assert set(pool.map(recover, range(8))) == {csrf}


def test_recovery_returns_current_identity_after_login_rotation(
    harness: tuple[TestClient, ApplicationServices],
) -> None:
    client, _ = harness
    with client:
        old_cookie, old_csrf = issue_guest(client)
        auth = {"Origin": ORIGIN, "X-CSRF-Token": old_csrf}
        registration = client.post(
            "/api/v1/members",
            headers=auth,
            json={
                "login_id": "member_01",
                "nickname": "돌장인",
                "password": "correct-pass",
            },
        )
        assert registration.status_code == 201
        login = client.post(
            "/api/v1/sessions/member",
            headers=auth,
            json={
                "login_id": "member_01",
                "password": "correct-pass",
            },
        )
        assert login.status_code == 200
        assert login.headers["Cache-Control"] == "no-store"
        recovered = client.post(URL, headers=HEADERS, json={}).json()
        assert recovered["actor_type"] == "MEMBER"
        assert recovered["actor_id"] == str(registration.json()["member_id"])
        assert recovered["csrf_token"] == login.json()["csrf_token"] != old_csrf
        assert client.delete("/api/v1/session", headers=auth).status_code == 403
        stale = client.post(
            URL, headers={**HEADERS, "Cookie": f"seokpan_session={old_cookie}"}, json={}
        )
        assert stale.status_code == 401


def test_recovery_does_not_turn_unavailable_session_into_new_identity(
    harness: tuple[TestClient, ApplicationServices],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from seokpan.identity.application import SessionRuleViolation

    client, services = harness
    with client:
        issue_guest(client)
        monkeypatch.setattr(
            services.identity_api.sessions,
            "find",
            AsyncMock(
                side_effect=SessionRuleViolation("UNSUPPORTED_SESSION_SCHEMA"),
            ),
        )
        response = client.post(URL, headers=HEADERS, json={})
        assert response.status_code == 503
        assert response.json()["code"] == "UNSUPPORTED_SESSION_SCHEMA"
        assert "set-cookie" not in response.headers
        assert "csrf_token" not in response.text


def test_recovery_body_and_openapi(harness: tuple[TestClient, ApplicationServices]) -> None:
    client, _ = harness
    with client:
        issue_guest(client)
        assert client.post(URL, headers=HEADERS).status_code == 422
        extra = client.post(URL, headers=HEADERS, json={"unexpected": "sensitive-value"})
        assert extra.status_code == 422 and "sensitive-value" not in extra.text
        response = client.post(
            URL,
            headers={**HEADERS, "Content-Type": "application/json; charset=utf-8"},
            content="{}",
        )
        assert response.status_code == 200
        operation = client.get("/api/openapi.json").json()["paths"][URL]["post"]
        assert {"200", "401", "403", "422", "503"} <= operation["responses"].keys()
        marker = next(p for p in operation["parameters"] if p["name"] == "X-CSRF-Bootstrap")
        assert marker["in"] == "header" and marker["required"] is True
        assert marker["schema"] == {"type": "string", "enum": ["1"]}
