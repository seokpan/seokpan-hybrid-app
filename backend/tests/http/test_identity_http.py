from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from seokpan.api.identity import IdentityApiServices
from seokpan.app import ApplicationServices, create_app
from seokpan.identity.application import (
    AuthSessionService,
    MemberIdentityService,
)
from seokpan.persistence.memory import (
    InMemoryIdentityAdapter,
    InMemorySessionAdapter,
    InMemorySessionWorkflow,
    ManualClock,
)
from seokpan.security import Argon2Parameters, Argon2PasswordHasher
from seokpan.settings import Settings

ORIGIN = "http://localhost:5173"


class SequenceTokenSource:
    def __init__(self) -> None:
        self._counter = 0

    def issue(self) -> str:
        self._counter += 1
        return f"token-{self._counter:064d}"


@pytest.fixture
def app_harness() -> tuple[TestClient, InMemorySessionWorkflow]:
    settings = Settings(environment="test", allowed_origins=(ORIGIN,))
    hasher = Argon2PasswordHasher(
        Argon2Parameters(time_cost=1, memory_cost_kib=8 * 1024, parallelism=1)
    )
    members = MemberIdentityService(
        InMemoryIdentityAdapter(),
        hasher,
        dummy_password_hash=hasher.hash("valid-dummy-password"),
    )
    workflow = InMemorySessionWorkflow(InMemorySessionAdapter(ManualClock()))
    sessions = AuthSessionService(workflow, SequenceTokenSource())
    application = create_app(
        settings=settings,
        services=ApplicationServices(IdentityApiServices(settings, members, sessions)),
    )
    return TestClient(application, base_url=ORIGIN), workflow


def _guest(client: TestClient) -> tuple[str, str]:
    response = client.post("/api/v1/sessions/guest", headers={"Origin": ORIGIN})
    assert response.status_code == 201
    return response.cookies["seokpan_session"], response.json()["csrf_token"]


def _register(client: TestClient, csrf: str | None = None) -> None:
    headers = {"Origin": ORIGIN}
    if csrf is not None:
        headers["X-CSRF-Token"] = csrf
    response = client.post(
        "/api/v1/members",
        headers=headers,
        json={"login_id": "member_01", "nickname": "돌장인", "password": "correct-pass"},
    )
    assert response.status_code == 201


def test_missing_fake_rating_is_service_unavailable_not_invalid_input() -> None:
    settings = Settings(environment="test", allowed_origins=(ORIGIN,))
    ratings: dict[int, int] = {}
    hasher = Argon2PasswordHasher(
        Argon2Parameters(time_cost=1, memory_cost_kib=8 * 1024, parallelism=1)
    )
    members = MemberIdentityService(
        InMemoryIdentityAdapter(member_ratings=ratings),
        hasher,
        dummy_password_hash=hasher.hash("valid-dummy-password"),
    )
    sessions = AuthSessionService(
        InMemorySessionWorkflow(InMemorySessionAdapter(ManualClock())), SequenceTokenSource()
    )
    application = create_app(
        settings=settings,
        services=ApplicationServices(IdentityApiServices(settings, members, sessions)),
    )
    with TestClient(application, base_url=ORIGIN) as client:
        _register(client)
        ratings.clear()
        response = client.post(
            "/api/v1/sessions/member",
            headers={"Origin": ORIGIN},
            json={"login_id": "member_01", "password": "correct-pass"},
        )
    assert response.status_code == 503
    assert response.json()["code"] == "IDENTITY_PROVIDER_UNAVAILABLE"
    assert ratings == {}


def test_guest_cookie_and_current_session(
    app_harness: tuple[TestClient, InMemorySessionWorkflow],
) -> None:
    client, _workflow = app_harness
    with client:
        issued = client.post("/api/v1/sessions/guest", headers={"Origin": ORIGIN})
        assert issued.status_code == 201
        response = client.get("/api/v1/session")

    assert response.status_code == 200
    assert response.json()["actor_type"] == "GUEST"
    assert response.json()["display_name"].startswith("Guest-")
    cookie = client.cookies.get("seokpan_session")
    assert cookie is not None
    set_cookie = issued.headers["set-cookie"].lower()
    assert "httponly" in set_cookie
    assert "samesite=lax" in set_cookie
    assert "path=/" in set_cookie
    assert "domain=" not in set_cookie
    assert "secure" not in set_cookie


def test_state_change_requires_allowed_origin(
    app_harness: tuple[TestClient, InMemorySessionWorkflow],
) -> None:
    client, _workflow = app_harness
    with client:
        response = client.post("/api/v1/sessions/guest")

    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "ORIGIN_NOT_ALLOWED"


def test_allowed_referer_is_accepted_without_origin(
    app_harness: tuple[TestClient, InMemorySessionWorkflow],
) -> None:
    client, _workflow = app_harness
    with client:
        response = client.post(
            "/api/v1/sessions/guest",
            headers={"Referer": f"{ORIGIN}/login"},
        )
    assert response.status_code == 201


def test_signup_does_not_create_login_session(
    app_harness: tuple[TestClient, InMemorySessionWorkflow],
) -> None:
    client, _workflow = app_harness
    with client:
        _register(client)
        current = client.get("/api/v1/session")

    assert current.status_code == 401
    assert current.json()["code"] == "AUTH_REQUIRED"


def test_guest_to_member_rotation_and_logout(
    app_harness: tuple[TestClient, InMemorySessionWorkflow],
) -> None:
    client, workflow = app_harness
    with client:
        old_token, guest_csrf = _guest(client)
        _register(client, guest_csrf)
        login = client.post(
            "/api/v1/sessions/member",
            headers={"Origin": ORIGIN, "X-CSRF-Token": guest_csrf},
            json={"login_id": "member_01", "password": "correct-pass"},
        )
        assert login.status_code == 200
        member_csrf = login.json()["csrf_token"]
        assert login.json()["actor_type"] == "MEMBER"
        assert login.json()["display_name"] == "돌장인"
        assert login.cookies["seokpan_session"] != old_token
        current = client.get("/api/v1/session")
        logout = client.delete(
            "/api/v1/session",
            headers={"Origin": ORIGIN, "X-CSRF-Token": member_csrf},
        )
        after_logout = client.get("/api/v1/session")

    assert current.json()["display_name"] == "돌장인"
    assert logout.status_code == 204
    assert after_logout.status_code == 401
    assert len(workflow.identity_changes) == 1
    assert workflow.identity_changes[0][0].actor_type.value == "GUEST"
    assert workflow.logouts[0].actor_type.value == "MEMBER"


def test_previous_session_token_is_rejected_after_rotation(
    app_harness: tuple[TestClient, InMemorySessionWorkflow],
) -> None:
    client, _workflow = app_harness
    with client:
        old_token, guest_csrf = _guest(client)
        _register(client, guest_csrf)
        login = client.post(
            "/api/v1/sessions/member",
            headers={"Origin": ORIGIN, "X-CSRF-Token": guest_csrf},
            json={"login_id": "member_01", "password": "correct-pass"},
        )
        assert login.status_code == 200

    stale_client = TestClient(client.app, base_url=ORIGIN)
    stale_client.cookies.set("seokpan_session", old_token)
    with stale_client:
        stale = stale_client.get("/api/v1/session")
    assert stale.status_code == 401


def test_existing_session_requires_matching_csrf(
    app_harness: tuple[TestClient, InMemorySessionWorkflow],
) -> None:
    client, _workflow = app_harness
    with client:
        _token, csrf = _guest(client)
        missing = client.post(
            "/api/v1/members",
            headers={"Origin": ORIGIN},
            json={"login_id": "member_01", "nickname": "돌장인", "password": "correct-pass"},
        )
        wrong = client.post(
            "/api/v1/members",
            headers={"Origin": ORIGIN, "X-CSRF-Token": "wrong-token"},
            json={"login_id": "member_01", "nickname": "돌장인", "password": "correct-pass"},
        )
        _register(client, csrf)

    assert missing.status_code == 403
    assert wrong.status_code == 403
    assert missing.json()["code"] == wrong.json()["code"] == "CSRF_INVALID"


def test_identity_transition_failure_keeps_previous_session(
    app_harness: tuple[TestClient, InMemorySessionWorkflow],
) -> None:
    client, workflow = app_harness
    with client:
        _token, guest_csrf = _guest(client)
        _register(client, guest_csrf)
        workflow.fail_identity_change = True
        failed = client.post(
            "/api/v1/sessions/member",
            headers={"Origin": ORIGIN, "X-CSRF-Token": guest_csrf},
            json={"login_id": "member_01", "password": "correct-pass"},
        )
        current = client.get("/api/v1/session")

    assert failed.status_code == 503
    assert failed.json()["code"] == "SESSION_TRANSITION_UNAVAILABLE"
    assert current.status_code == 200
    assert current.json()["actor_type"] == "GUEST"


def test_logout_failure_keeps_session_and_cookie(
    app_harness: tuple[TestClient, InMemorySessionWorkflow],
) -> None:
    client, workflow = app_harness
    with client:
        token, csrf = _guest(client)
        workflow.fail_logout = True
        failed = client.delete(
            "/api/v1/session",
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        )
        current = client.get("/api/v1/session")

    assert failed.status_code == 503
    assert current.status_code == 200
    assert client.cookies.get("seokpan_session") == token


@pytest.mark.parametrize(
    "login_id,password", [("missing_01", "correct-pass"), ("member_01", "wrong-pass")]
)
def test_login_failures_do_not_reveal_account_existence(
    app_harness: tuple[TestClient, InMemorySessionWorkflow],
    login_id: str,
    password: str,
) -> None:
    client, _workflow = app_harness
    with client:
        if login_id == "member_01":
            _register(client)
        response = client.post(
            "/api/v1/sessions/member",
            headers={"Origin": ORIGIN},
            json={"login_id": login_id, "password": password},
        )

    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_INVALID_CREDENTIALS"


def test_validation_problem_does_not_echo_password(
    app_harness: tuple[TestClient, InMemorySessionWorkflow],
) -> None:
    client, _workflow = app_harness
    with client:
        response = client.post(
            "/api/v1/members",
            headers={"Origin": ORIGIN},
            json={"login_id": "bad", "nickname": "x", "password": "secret"},
        )

    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_LOGIN_ID"
    assert "secret" not in response.text


def test_openapi_contains_identity_paths_and_problem_responses(
    app_harness: tuple[TestClient, InMemorySessionWorkflow],
) -> None:
    client, _workflow = app_harness
    with client:
        schema = client.get("/api/openapi.json").json()

    assert {
        "/api/v1/sessions/guest",
        "/api/v1/members",
        "/api/v1/sessions/member",
        "/api/v1/session",
    }.issubset(schema["paths"])
    assert "403" in schema["paths"]["/api/v1/sessions/guest"]["post"]["responses"]
    error_content = schema["paths"]["/api/v1/sessions/guest"]["post"]["responses"]["403"]["content"]
    assert set(error_content) == {"application/problem+json"}


def test_production_requires_explicit_provider_services() -> None:
    with pytest.raises(RuntimeError, match="Production provider configuration is required"):
        create_app(
            settings=Settings(environment="production", allowed_origins=("https://example.test",))
        )


def test_production_cookie_is_secure_when_provider_services_are_supplied() -> None:
    origin = "https://game.seokpan.soldesk.store"
    settings = Settings(environment="production", allowed_origins=(origin,))
    hasher = Argon2PasswordHasher(
        Argon2Parameters(time_cost=1, memory_cost_kib=8 * 1024, parallelism=1)
    )
    members = MemberIdentityService(
        InMemoryIdentityAdapter(),
        hasher,
        dummy_password_hash=hasher.hash("valid-dummy-password"),
    )
    workflow = InMemorySessionWorkflow(InMemorySessionAdapter(ManualClock()))
    sessions = AuthSessionService(workflow, SequenceTokenSource())
    application = create_app(
        settings=settings,
        services=ApplicationServices(IdentityApiServices(settings, members, sessions)),
    )

    with TestClient(application, base_url=origin) as client:
        response = client.post("/api/v1/sessions/guest", headers={"Origin": origin})

    assert response.status_code == 201
    assert "secure" in response.headers["set-cookie"].lower()
