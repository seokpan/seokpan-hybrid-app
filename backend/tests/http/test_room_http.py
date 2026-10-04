from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from seokpan.app import build_headless_services, create_app
from seokpan.settings import Settings

ORIGIN = "http://localhost:5173"


@pytest.mark.parametrize("is_guest", [True, False])
def test_kick_preserves_login_and_old_request_does_not_kick_rejoined_user(
    application: FastAPI, is_guest: bool
) -> None:
    with (
        TestClient(application, base_url=ORIGIN) as owner,
        TestClient(application, base_url=ORIGIN) as target,
    ):
        owner_csrf = _register_and_login(owner, "kickowner")
        target_csrf = _guest(target) if is_guest else _register_and_login(target, "kicked")
        room = _create_room(owner, owner_csrf)
        joined = _join_room(target, target_csrf, str(room["room_id"]), int(room["state_version"]))
        before = target.get("/api/v1/session").json()
        cookie = target.cookies.get("seokpan_session")
        path = f"/api/v1/rooms/{room['room_id']}/participants/{before['participant_id']}/kick"
        headers = {"Origin": ORIGIN, "X-CSRF-Token": owner_csrf}
        body = {"request_id": str(uuid4()), "expected_state_version": joined["state_version"]}
        response = owner.post(path, headers=headers, json=body)
        assert response.status_code == 200, response.text
        current = target.get("/api/v1/session").json()
        assert current["room_id"] is None and current["participant_id"] is None
        assert current["actor_id"] == before["actor_id"]
        assert current["actor_type"] == before["actor_type"]
        assert target.cookies.get("seokpan_session") == cookie
        assert target.get(f"/api/v1/rooms/{room['room_id']}/state").status_code == 403
        assert owner.get("/api/v1/session").json()["room_id"] == room["room_id"]
        rejoined = _join_room(
            target, target_csrf, str(room["room_id"]), response.json()["state_version"]
        )
        new_participant = target.get("/api/v1/session").json()["participant_id"]
        assert new_participant != before["participant_id"]
        replay = owner.post(path, headers=headers, json=body)
        assert replay.status_code == 200 and replay.json()["replayed"]
        assert target.get("/api/v1/session").json()["participant_id"] == new_participant
        assert owner.get(f"/api/v1/rooms/{room['room_id']}/snapshot").json() == rejoined


@pytest.mark.parametrize("failure", ["csrf", "origin", "non-owner", "self", "stale", "outside"])
def test_kick_rejects_unsafe_requests_without_removing_participants(
    application: FastAPI, failure: str
) -> None:
    with (
        TestClient(application, base_url=ORIGIN) as owner,
        TestClient(application, base_url=ORIGIN) as other,
    ):
        csrf = _register_and_login(owner, "kickguard")
        other_csrf = _guest(other)
        room = _create_room(owner, csrf)
        joined = _join_room(other, other_csrf, str(room["room_id"]), int(room["state_version"]))
        target_id = other.get("/api/v1/session").json()["participant_id"]
        owner_id = room["owner_id"]
        headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
        version = joined["state_version"]
        client = owner
        room_id = room["room_id"]
        expected = 403
        if failure == "csrf":
            headers.pop("X-CSRF-Token")
        elif failure == "origin":
            headers["Origin"] = "https://untrusted.invalid"
        elif failure == "non-owner":
            client, headers, target_id = (
                other,
                {"Origin": ORIGIN, "X-CSRF-Token": other_csrf},
                owner_id,
            )
        elif failure == "self":
            target_id, expected = owner_id, 422
        elif failure == "stale":
            version, expected = 1, 409
        elif failure == "outside":
            room_id = str(uuid4())
        response = client.post(
            f"/api/v1/rooms/{room_id}/participants/{target_id}/kick",
            headers=headers,
            json={"request_id": str(uuid4()), "expected_state_version": version},
        )
        assert response.status_code == expected, response.text
        assert owner.get(f"/api/v1/rooms/{room['room_id']}/snapshot").json() == joined


@pytest.fixture
def application() -> FastAPI:
    settings = Settings(environment="test", allowed_origins=(ORIGIN,))
    return create_app(settings=settings, services=build_headless_services(settings))


def _register_and_login(client: TestClient, suffix: str) -> str:
    register = client.post(
        "/api/v1/members",
        headers={"Origin": ORIGIN},
        json={
            "login_id": f"member_{suffix}",
            "nickname": f"돌장인{suffix}",
            "password": "correct-pass",
        },
    )
    assert register.status_code == 201
    login = client.post(
        "/api/v1/sessions/member",
        headers={"Origin": ORIGIN},
        json={"login_id": f"member_{suffix}", "password": "correct-pass"},
    )
    assert login.status_code == 200
    return str(login.json()["csrf_token"])


def _guest(client: TestClient) -> str:
    response = client.post("/api/v1/sessions/guest", headers={"Origin": ORIGIN})
    assert response.status_code == 201
    return str(response.json()["csrf_token"])


def _create_room(
    client: TestClient,
    csrf: str,
    *,
    visibility: str = "PUBLIC",
    password: str | None = None,
    max_participants: int = 100,
) -> dict[str, object]:
    response = client.post(
        "/api/v1/rooms",
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        json={
            "request_id": str(uuid4()),
            "name": "MVP 검증방",
            "visibility": visibility,
            "password": password,
            "max_participants": max_participants,
            "minimum_ready": 2,
            "vote_seconds": 15,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _join_room(
    client: TestClient,
    csrf: str,
    room_id: str,
    state_version: int,
    *,
    password: str | None = None,
) -> dict[str, object]:
    response = client.post(
        f"/api/v1/rooms/{room_id}/joins",
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        json={
            "request_id": str(uuid4()),
            "expected_state_version": state_version,
            "password": password,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _mutate(
    client: TestClient,
    csrf: str,
    method: str,
    path: str,
    state_version: int,
    **values: object,
) -> dict[str, object] | None:
    response = client.request(
        method,
        path,
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        json={
            "request_id": str(uuid4()),
            "expected_state_version": state_version,
            **values,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_member_creates_room_and_lobby_exposes_only_public_state(application: FastAPI) -> None:
    with TestClient(application, base_url=ORIGIN) as member:
        csrf = _register_and_login(member, "01")
        room = _create_room(member, csrf, visibility="PRIVATE", password="room-pass")
        lobby = member.get("/api/v1/rooms")
        current = member.get("/api/v1/session")

    assert UUID(str(room["room_id"])).version == 4
    participant = room["participants"][0]  # type: ignore[index]
    assert UUID(str(participant["participant_id"])).version == 4
    assert room["password_required"] is True
    assert "encoded_password" not in room
    assert "room-pass" not in str(room)
    assert lobby.status_code == 200
    assert lobby.json()["rooms"][0]["password_required"] is True
    assert "password" not in lobby.text.lower().replace("password_required", "")
    assert current.json()["room_id"] == room["room_id"]
    assert current.json()["participant_id"] == participant["participant_id"]


def test_guest_cannot_create_but_can_join_private_room(application: FastAPI) -> None:
    with (
        TestClient(application, base_url=ORIGIN) as owner,
        TestClient(application, base_url=ORIGIN) as guest,
    ):
        owner_csrf = _register_and_login(owner, "02")
        room = _create_room(owner, owner_csrf, visibility="PRIVATE", password="room-pass")
        guest_csrf = _guest(guest)
        forbidden = guest.post(
            "/api/v1/rooms",
            headers={"Origin": ORIGIN, "X-CSRF-Token": guest_csrf},
            json={"request_id": str(uuid4()), "name": "금지된 방"},
        )
        wrong = guest.post(
            f"/api/v1/rooms/{room['room_id']}/joins",
            headers={"Origin": ORIGIN, "X-CSRF-Token": guest_csrf},
            json={
                "request_id": str(uuid4()),
                "expected_state_version": room["state_version"],
                "password": "wrong-pass",
            },
        )
        joined = _join_room(
            guest,
            guest_csrf,
            str(room["room_id"]),
            int(room["state_version"]),
            password="room-pass",
        )

    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "MEMBER_REQUIRED_TO_CREATE_ROOM"
    assert wrong.status_code == 401
    assert wrong.json()["code"] == "ROOM_PASSWORD_INVALID"
    assert len(joined["participants"]) == 2  # type: ignore[arg-type]


def test_room_capacity_is_rechecked_when_joining(application: FastAPI) -> None:
    with (
        TestClient(application, base_url=ORIGIN) as owner,
        TestClient(application, base_url=ORIGIN) as first_guest,
        TestClient(application, base_url=ORIGIN) as second_guest,
    ):
        owner_csrf = _register_and_login(owner, "08")
        room = _create_room(owner, owner_csrf, max_participants=2)
        first_csrf = _guest(first_guest)
        room = _join_room(first_guest, first_csrf, str(room["room_id"]), int(room["state_version"]))
        second_csrf = _guest(second_guest)
        listed = second_guest.get("/api/v1/rooms").json()["rooms"]
        assert len(listed) == 1
        assert listed[0]["status"] == "WAITING"
        assert listed[0]["participant_count"] == listed[0]["max_participants"] == 2
        full = second_guest.post(
            f"/api/v1/rooms/{room['room_id']}/joins",
            headers={"Origin": ORIGIN, "X-CSRF-Token": second_csrf},
            json={
                "request_id": str(uuid4()),
                "expected_state_version": room["state_version"],
            },
        )

    assert full.status_code == 409
    assert full.json()["code"] == "ROOM_CAPACITY_REACHED"


def test_stale_state_returns_current_version_and_snapshot_url(application: FastAPI) -> None:
    with TestClient(application, base_url=ORIGIN) as member:
        csrf = _register_and_login(member, "03")
        room = _create_room(member, csrf)
        response = member.put(
            f"/api/v1/rooms/{room['room_id']}/participants/me/team",
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
            json={
                "request_id": str(uuid4()),
                "expected_state_version": 999,
                "team": "BLACK",
            },
        )

    assert response.status_code == 409
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "STALE_STATE"
    assert response.json()["current_version"] == room["state_version"]
    assert response.json()["snapshot_url"].endswith(f"/{room['room_id']}/snapshot")


def test_room_snapshot_is_limited_to_current_participants(application: FastAPI) -> None:
    with (
        TestClient(application, base_url=ORIGIN) as owner,
        TestClient(application, base_url=ORIGIN) as outsider,
    ):
        owner_csrf = _register_and_login(owner, "09")
        room = _create_room(owner, owner_csrf)
        _guest(outsider)
        response = outsider.get(f"/api/v1/rooms/{room['room_id']}/snapshot")

    assert response.status_code == 403
    assert response.json()["code"] == "SESSION_NOT_IN_ROOM"


def test_owner_leave_promotes_member_and_resets_every_ready(application: FastAPI) -> None:
    with (
        TestClient(application, base_url=ORIGIN) as owner,
        TestClient(application, base_url=ORIGIN) as successor,
    ):
        owner_csrf = _register_and_login(owner, "04")
        room = _create_room(owner, owner_csrf)
        successor_csrf = _register_and_login(successor, "05")
        room = _join_room(
            successor, successor_csrf, str(room["room_id"]), int(room["state_version"])
        )
        successor_id = room["participants"][1]["participant_id"]  # type: ignore[index]
        room = _mutate(
            owner,
            owner_csrf,
            "PUT",
            f"/api/v1/rooms/{room['room_id']}/participants/me/team",
            int(room["state_version"]),
            team="BLACK",
        )
        assert room is not None
        room = _mutate(
            owner,
            owner_csrf,
            "PUT",
            f"/api/v1/rooms/{room['room_id']}/participants/me/ready",
            int(room["state_version"]),
            ready=True,
        )
        assert room is not None
        room = _mutate(
            successor,
            successor_csrf,
            "PUT",
            f"/api/v1/rooms/{room['room_id']}/participants/me/team",
            int(room["state_version"]),
            team="WHITE",
        )
        assert room is not None
        room = _mutate(
            successor,
            successor_csrf,
            "PUT",
            f"/api/v1/rooms/{room['room_id']}/participants/me/ready",
            int(room["state_version"]),
            ready=True,
        )
        assert room is not None
        room = _mutate(
            owner,
            owner_csrf,
            "PATCH",
            f"/api/v1/rooms/{room['room_id']}/settings",
            int(room["state_version"]),
            vote_seconds=30,
        )
        assert room is not None
        assert room["vote_seconds"] == 30
        assert all(not item["ready"] for item in room["participants"])  # type: ignore[union-attr]
        room = _mutate(
            owner,
            owner_csrf,
            "PUT",
            f"/api/v1/rooms/{room['room_id']}/participants/me/ready",
            int(room["state_version"]),
            ready=True,
        )
        assert room is not None
        room = _mutate(
            successor,
            successor_csrf,
            "PUT",
            f"/api/v1/rooms/{room['room_id']}/participants/me/ready",
            int(room["state_version"]),
            ready=True,
        )
        assert room is not None
        left = _mutate(
            owner,
            owner_csrf,
            "DELETE",
            f"/api/v1/rooms/{room['room_id']}/participants/me",
            int(room["state_version"]),
        )

    assert left is not None
    assert left["owner_id"] == successor_id
    assert all(not item["ready"] for item in left["participants"])  # type: ignore[union-attr]


def test_guest_login_preserves_participant_and_member_switch_is_rejected(
    application: FastAPI,
) -> None:
    with (
        TestClient(application, base_url=ORIGIN) as owner,
        TestClient(application, base_url=ORIGIN) as guest,
    ):
        owner_csrf = _register_and_login(owner, "06")
        room = _create_room(owner, owner_csrf)
        guest_csrf = _guest(guest)
        room = _join_room(guest, guest_csrf, str(room["room_id"]), int(room["state_version"]))
        participant_id = room["participants"][1]["participant_id"]  # type: ignore[index]
        room = _mutate(
            guest,
            guest_csrf,
            "PUT",
            f"/api/v1/rooms/{room['room_id']}/participants/me/team",
            int(room["state_version"]),
            team="WHITE",
        )
        assert room is not None
        room = _mutate(
            guest,
            guest_csrf,
            "PUT",
            f"/api/v1/rooms/{room['room_id']}/participants/me/ready",
            int(room["state_version"]),
            ready=True,
        )
        assert room is not None
        register = guest.post(
            "/api/v1/members",
            headers={"Origin": ORIGIN, "X-CSRF-Token": guest_csrf},
            json={"login_id": "promoted", "nickname": "승격회원", "password": "correct-pass"},
        )
        assert register.status_code == 201
        previous_cookie = guest.cookies.get("seokpan_session")
        assert previous_cookie is not None
        login = guest.post(
            "/api/v1/sessions/member",
            headers={"Origin": ORIGIN, "X-CSRF-Token": guest_csrf},
            json={"login_id": "promoted", "password": "correct-pass"},
        )
        assert login.status_code == 200
        member_csrf = login.json()["csrf_token"]
        snapshot = guest.get(f"/api/v1/rooms/{room['room_id']}/snapshot").json()
        promoted = next(
            item for item in snapshot["participants"] if item["participant_id"] == participant_id
        )
        assert promoted["actor_type"] == "MEMBER"
        assert promoted["team"] == "WHITE"
        assert promoted["ready"] is True
        with TestClient(application, base_url=ORIGIN) as previous_session:
            previous_session.cookies.set("seokpan_session", previous_cookie)
            assert previous_session.get("/api/v1/session").status_code == 401
        register_other = guest.post(
            "/api/v1/members",
            headers={"Origin": ORIGIN, "X-CSRF-Token": member_csrf},
            json={"login_id": "other", "nickname": "다른회원", "password": "correct-pass"},
        )
        assert register_other.status_code == 201
        switch = guest.post(
            "/api/v1/sessions/member",
            headers={"Origin": ORIGIN, "X-CSRF-Token": member_csrf},
            json={"login_id": "other", "password": "correct-pass"},
        )

    assert switch.status_code == 409
    assert switch.json()["code"] == "ACTIVE_ROOM_MEMBER_CHANGE_NOT_ALLOWED"


def test_create_is_idempotent_and_logout_leaves_before_session_revoke(application: FastAPI) -> None:
    with TestClient(application, base_url=ORIGIN) as member:
        csrf = _register_and_login(member, "07")
        request_id = str(uuid4())
        payload = {
            "request_id": request_id,
            "name": "중복 검증방",
            "minimum_ready": 2,
        }
        first = member.post(
            "/api/v1/rooms",
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
            json=payload,
        )
        replay = member.post(
            "/api/v1/rooms",
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
            json=payload,
        )
        conflict = member.post(
            "/api/v1/rooms",
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
            json={**payload, "name": "다른 내용"},
        )
        logout = member.delete(
            "/api/v1/session",
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        )
        current = member.get("/api/v1/session")
        room_snapshot = member.get(f"/api/v1/rooms/{first.json()['room_id']}/snapshot")

    assert first.status_code == 201
    assert replay.status_code == 201
    assert replay.json()["room_id"] == first.json()["room_id"]
    assert replay.json()["replayed"] is True
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "REQUEST_ID_CONFLICT"
    assert logout.status_code == 204
    assert current.status_code == 401
    assert room_snapshot.status_code == 401


def test_room_close_clears_remaining_guest_participation(application: FastAPI) -> None:
    with (
        TestClient(application, base_url=ORIGIN) as owner,
        TestClient(application, base_url=ORIGIN) as guest,
    ):
        owner_csrf = _register_and_login(owner, "10")
        room = _create_room(owner, owner_csrf)
        guest_csrf = _guest(guest)
        room = _join_room(guest, guest_csrf, str(room["room_id"]), int(room["state_version"]))
        logout = owner.delete(
            "/api/v1/session",
            headers={"Origin": ORIGIN, "X-CSRF-Token": owner_csrf},
        )
        guest_session = guest.get("/api/v1/session")

    assert logout.status_code == 204
    assert guest_session.status_code == 200
    assert guest_session.json()["room_id"] is None
    assert guest_session.json()["participant_id"] is None


def test_openapi_contains_all_room_routes_and_hides_internal_fields(application: FastAPI) -> None:
    with TestClient(application, base_url=ORIGIN) as client:
        schema = client.get("/api/openapi.json").json()

    expected = {
        "/api/v1/rooms",
        "/api/v1/rooms/{room_id}/snapshot",
        "/api/v1/rooms/{room_id}/joins",
        "/api/v1/rooms/{room_id}/participants/me",
        "/api/v1/rooms/{room_id}/settings",
        "/api/v1/rooms/{room_id}/participants/me/team",
        "/api/v1/rooms/{room_id}/participants/me/ready",
    }
    assert expected <= set(schema["paths"])
    snapshot_schema = str(schema["components"]["schemas"]["RoomSnapshotResponse"])
    assert "encoded_password" not in snapshot_schema
    assert "session_digest" not in snapshot_schema
    assert "csrf" not in snapshot_schema.lower()
