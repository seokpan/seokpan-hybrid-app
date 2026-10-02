from __future__ import annotations

import asyncio
from collections.abc import Mapping
from functools import partial
from typing import cast
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from seokpan.app import ApplicationServices, build_headless_services, create_app
from seokpan.identity.application import SessionActorType, SessionRecord, digest_opaque_token
from seokpan.persistence.memory import (
    InMemoryRealtimeEventAdapter,
    InMemoryRoomRuntimeAdapter,
    InMemoryVoteRuntimeAdapter,
    ManualClock,
)
from seokpan.room.application import (
    ROOM_DISCONNECT_LEASE_MS,
    DisconnectExpiryResult,
    DisconnectExpiryRunner,
    DisconnectExpiryStatus,
    DueRoomDisconnect,
    RealtimeSubscription,
    RoomApplicationService,
    RoomConnectionCoordinator,
    RoomMutationResult,
)
from seokpan.room.domain import DepartureResult, GameTermination, RoomConfig, RoomRuleViolation
from seokpan.settings import Settings

ORIGIN = "http://localhost:5173"


def test_kick_terminal_notice_closes_only_target_socket(
    headless: tuple[FastAPI, ApplicationServices],
) -> None:
    application, _services = headless
    with (
        TestClient(application, base_url=ORIGIN) as owner,
        TestClient(application, base_url=ORIGIN) as guest,
    ):
        owner_csrf = _member(owner, "kickws")
        guest_csrf = _guest(guest)
        room = _create_room(owner, owner_csrf)
        room_id = str(room["room_id"])
        _join(guest, guest_csrf, room_id, int(room["state_version"]))
        participant_id = guest.get("/api/v1/session").json()["participant_id"]
        with owner.websocket_connect(
            f"/ws/v1/rooms/{room_id}", headers=_ws_headers(owner)
        ) as owner_ws:
            owner_ws.receive_json()
            # Two identities, one ASGI loop (the same arrangement as one server).
            with owner.websocket_connect(
                f"/ws/v1/rooms/{room_id}", headers=_ws_headers(guest)
            ) as target_ws:
                initial = target_ws.receive_json()["payload"]["room"]
                response = owner.post(
                    f"/api/v1/rooms/{room_id}/participants/{participant_id}/kick",
                    headers={"Origin": ORIGIN, "X-CSRF-Token": owner_csrf},
                    json={
                        "request_id": str(uuid4()),
                        "expected_state_version": initial["state_version"],
                    },
                )
                assert response.status_code == 200, response.text
                notice = target_ws.receive_json()
                assert notice["event_type"] == "room.participant_left"
                assert notice["payload"]["reason"] == "KICKED"
                assert notice["payload"]["participant_id"] == participant_id
                assert "session_digest" not in str(notice)
                with pytest.raises(WebSocketDisconnect) as closed:
                    target_ws.receive_json()
                assert closed.value.code == 1000
                owner_notice = owner_ws.receive_json()
                assert owner_notice["event_type"] == "room.participant_left"
                # The owner's connection remains usable after the terminal target notice.
                _room_mutation(
                    owner,
                    owner_csrf,
                    f"/api/v1/rooms/{room_id}/participants/me/team",
                    response.json()["state_version"],
                    team="BLACK",
                )
                assert owner_ws.receive_json()["event_type"] == "room.team_changed"
            assert guest.get("/api/v1/session").json()["room_id"] is None


def test_member_in_room_cannot_downgrade_to_guest(
    headless: tuple[FastAPI, ApplicationServices],
) -> None:
    application, _services = headless
    with TestClient(application, base_url=ORIGIN) as client:
        csrf = _member(client, "downgrade")
        room = _create_room(client, csrf)
        cookie = client.cookies.get("seokpan_session")
        with client.websocket_connect(
            f"/ws/v1/rooms/{room['room_id']}", headers=_ws_headers(client)
        ) as socket:
            before = socket.receive_json()["payload"]["room"]
            response = client.post(
                "/api/v1/sessions/guest", headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}
            )
            assert response.status_code == 409
            assert response.json()["code"] == "ACTIVE_ROOM_IDENTITY_CHANGE_NOT_ALLOWED"
            assert client.cookies.get("seokpan_session") == cookie
            after = client.get(f"/api/v1/rooms/{room['room_id']}/snapshot").json()
            assert after == before
            # The same Socket still receives a valid subsequent state change.
            _room_mutation(
                client,
                csrf,
                f"/api/v1/rooms/{room['room_id']}/participants/me/team",
                int(after["state_version"]),
                team="BLACK",
            )
            assert _next_room_event(socket)["event_type"] == "room.team_changed"


@pytest.mark.parametrize("path", ["/ws/v1/lobby", "/ws/v1/rooms/unopened"])
def test_upgrade_storage_failure_is_not_missing_authentication(
    headless: tuple[FastAPI, ApplicationServices],
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    application, services = headless

    async def failure(_digest: str) -> SessionRecord | None:
        raise RuntimeError("storage unavailable")

    with TestClient(application, base_url=ORIGIN) as client:
        _guest(client)
        monkeypatch.setattr(services.identity_api.sessions, "current", failure)
        with (
            pytest.raises(WebSocketDisconnect) as closed,
            client.websocket_connect(path, headers=_ws_headers(client)),
        ):
            pytest.fail("must not accept an unverifiable session")
        assert closed.value.code == 1011


@pytest.mark.parametrize("expiry_ms", [7_200_000, 86_400_000])
def test_open_lobby_expires_without_events(
    headless: tuple[FastAPI, ApplicationServices],
    monkeypatch: pytest.MonkeyPatch,
    expiry_ms: int,
) -> None:
    application, services = headless
    assert services.headless_clock is not None
    monkeypatch.setattr("seokpan.api.realtime.SESSION_CHECK_INTERVAL_SECONDS", 0.01)
    with TestClient(application, base_url=ORIGIN) as client:
        _guest(client)
        with client.websocket_connect("/ws/v1/lobby", headers=_ws_headers(client)) as socket:
            socket.receive_json()
            services.headless_clock.advance(expiry_ms)
            with pytest.raises(WebSocketDisconnect) as closed:
                socket.receive_json()
            assert closed.value.code == 4401


def test_lobby_event_is_not_delivered_after_session_expiry(
    headless: tuple[FastAPI, ApplicationServices],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, services = headless
    assert services.headless_clock is not None and services.realtime_api is not None
    monkeypatch.setattr("seokpan.api.realtime.SESSION_CHECK_INTERVAL_SECONDS", 60)
    with TestClient(application, base_url=ORIGIN) as client:
        _guest(client)
        with client.websocket_connect("/ws/v1/lobby", headers=_ws_headers(client)) as socket:
            socket.receive_json()
            services.headless_clock.advance(7_200_000)
            assert client.portal is not None
            client.portal.call(services.realtime_api.events.lobby_rooms_changed, {"reason": "TEST"})
            with pytest.raises(WebSocketDisconnect) as closed:
                socket.receive_json()
            assert closed.value.code == 4401


def test_open_lobby_closes_after_logout(
    headless: tuple[FastAPI, ApplicationServices],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, _services = headless
    monkeypatch.setattr("seokpan.api.realtime.SESSION_CHECK_INTERVAL_SECONDS", 0.01)
    with TestClient(application, base_url=ORIGIN) as client:
        csrf = _guest(client)
        with client.websocket_connect("/ws/v1/lobby", headers=_ws_headers(client)) as socket:
            socket.receive_json()
            assert (
                client.delete(
                    "/api/v1/session", headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}
                ).status_code
                == 204
            )
            with pytest.raises(WebSocketDisconnect) as closed:
                socket.receive_json()
            assert closed.value.code == 4401


@pytest.mark.parametrize("failure", ["exception", "timeout"])
def test_session_store_failure_keeps_room_participation(
    headless: tuple[FastAPI, ApplicationServices],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    application, services = headless
    assert services.realtime_api is not None
    monkeypatch.setattr("seokpan.api.realtime.SESSION_CHECK_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr("seokpan.api.stream_access.SESSION_CHECK_TIMEOUT_SECONDS", 0.03)

    async def unavailable(_digest: str) -> SessionRecord | None:
        if failure == "timeout":
            await asyncio.Event().wait()
        raise RuntimeError("do-not-log-secret")

    with TestClient(application, base_url=ORIGIN) as client:
        csrf = _member(client, "failure")
        room = _create_room(client, csrf)
        room_id = str(room["room_id"])
        with client.websocket_connect(
            f"/ws/v1/rooms/{room_id}", headers=_ws_headers(client)
        ) as socket:
            first = socket.receive_json()["payload"]["room"]
            monkeypatch.setattr(services.identity_api.sessions, "find", unavailable)
            with pytest.raises(WebSocketDisconnect) as closed:
                socket.receive_json()
            assert closed.value.code == 1011
        assert client.portal is not None
        current = client.portal.call(services.realtime_api.rooms.rooms.get, room_id)
        assert current is not None
        assert current.state_version == first["state_version"]
        assert current.owner_id == first["owner_id"]
        assert current.participants[0].connected


def test_expired_guest_socket_starts_disconnect_lease(
    headless: tuple[FastAPI, ApplicationServices],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, services = headless
    assert services.realtime_api is not None
    monkeypatch.setattr("seokpan.api.realtime.SESSION_CHECK_INTERVAL_SECONDS", 0.01)
    with (
        TestClient(application, base_url=ORIGIN) as owner,
        TestClient(application, base_url=ORIGIN) as guest,
    ):
        room = _create_room(owner, _member(owner, "exguest"))
        guest_csrf = _guest(guest)
        _join(guest, guest_csrf, str(room["room_id"]), int(room["state_version"]))
        digest = digest_opaque_token(str(guest.cookies.get("seokpan_session")))
        original_find = services.identity_api.sessions.find

        async def expired(value: str) -> SessionRecord | None:
            return None if value == digest else await original_find(value)

        with guest.websocket_connect(
            f"/ws/v1/rooms/{room['room_id']}", headers=_ws_headers(guest)
        ) as socket:
            first = socket.receive_json()["payload"]["room"]
            monkeypatch.setattr(services.identity_api.sessions, "find", expired)
            with pytest.raises(WebSocketDisconnect) as closed:
                socket.receive_json()
            assert closed.value.code == 4401
        current = owner.get(f"/api/v1/rooms/{room['room_id']}/snapshot").json()
        assert current["owner_id"] == first["owner_id"]
        assert len(current["participants"]) == 2
        assert not current["participants"][1]["connected"]
        assert current["state_version"] == first["state_version"] + 1


class _FailingRealtimeEvents(InMemoryRealtimeEventAdapter):
    async def lobby_rooms_changed(
        self,
        payload: Mapping[str, object],
        *,
        event_key: str | None = None,
    ) -> None:
        del payload, event_key
        raise RuntimeError("EVENT_PROVIDER_UNAVAILABLE")


class _FailingRoomSubscriptionEvents(InMemoryRealtimeEventAdapter):
    async def subscribe_room(self, room_id: str) -> RealtimeSubscription:
        del room_id
        raise RuntimeError("EVENT_PROVIDER_UNAVAILABLE")


class _UnusedRoomPasswords:
    async def encode(self, raw_password: str) -> str:
        return f"$argon2id${raw_password}"

    async def verify(self, encoded_password: str, candidate_password: str) -> bool:
        return encoded_password == f"$argon2id${candidate_password}"


def _session(token: str, actor_type: SessionActorType, actor_id: str) -> SessionRecord:
    return SessionRecord(
        session_digest=digest_opaque_token(token),
        actor_type=actor_type,
        actor_id=actor_id,
        csrf_digest=digest_opaque_token(f"csrf-{token}"),
        csrf_token=f"csrf-{token}",
        created_at_ms=0,
        last_activity_at_ms=0,
        absolute_expires_at_ms=86_400_000,
    )


@pytest.fixture
def headless() -> tuple[FastAPI, ApplicationServices]:
    settings = Settings(environment="test", allowed_origins=(ORIGIN,))
    services = build_headless_services(settings)
    return create_app(settings=settings, services=services), services


def _member(client: TestClient, suffix: str) -> str:
    registered = client.post(
        "/api/v1/members",
        headers={"Origin": ORIGIN},
        json={
            "login_id": f"ws_member_{suffix}",
            "nickname": f"회원{suffix[:6]}",
            "password": "correct-pass",
        },
    )
    assert registered.status_code == 201
    login = client.post(
        "/api/v1/sessions/member",
        headers={"Origin": ORIGIN},
        json={"login_id": f"ws_member_{suffix}", "password": "correct-pass"},
    )
    assert login.status_code == 200
    return str(login.json()["csrf_token"])


def _guest(client: TestClient) -> str:
    response = client.post("/api/v1/sessions/guest", headers={"Origin": ORIGIN})
    assert response.status_code == 201
    return str(response.json()["csrf_token"])


def _upgrade_guest_to_member(client: TestClient, csrf: str, suffix: str) -> str:
    registered = client.post(
        "/api/v1/members",
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        json={
            "login_id": f"ws_member_{suffix}",
            "nickname": f"회원{suffix[:6]}",
            "password": "correct-pass",
        },
    )
    assert registered.status_code == 201
    login = client.post(
        "/api/v1/sessions/member",
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        json={"login_id": f"ws_member_{suffix}", "password": "correct-pass"},
    )
    assert login.status_code == 200
    return str(login.json()["csrf_token"])


def _ws_headers(client: TestClient, *, origin: str = ORIGIN) -> dict[str, str]:
    token = client.cookies.get("seokpan_session")
    assert token is not None
    return {"Origin": origin, "Cookie": f"seokpan_session={token}"}


def _assert_event_envelope(value: dict[str, object]) -> None:
    assert {
        "event_type",
        "schema_version",
        "event_id",
        "occurred_at",
        "state_version",
        "room_id",
        "game_id",
        "turn_no",
        "payload",
    } == set(value)
    assert value["schema_version"] == 1
    UUID(str(value["event_id"]))
    assert str(value["occurred_at"]).endswith("Z")
    assert int(str(value["state_version"])) >= 1
    serialized = repr(value).lower()
    assert "session_digest" not in serialized
    assert "csrf" not in serialized
    assert "encoded_password" not in serialized


def _next_room_event(socket: WebSocketTestSession) -> dict[str, object]:
    while True:
        value = cast(dict[str, object], socket.receive_json())
        payload = cast(dict[str, object], value.get("payload", {}))
        if value.get("event_type") != "snapshot.required" or payload.get("reason") != (
            "PARTICIPANT_CONNECTED"
        ):
            return value


def _create_room(client: TestClient, csrf: str) -> dict[str, object]:
    response = client.post(
        "/api/v1/rooms",
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        json={
            "request_id": str(uuid4()),
            "name": "WebSocket 검증방",
            "minimum_ready": 2,
            "vote_seconds": 15,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _join(
    client: TestClient,
    csrf: str,
    room_id: str,
    state_version: int,
) -> dict[str, object]:
    response = client.post(
        f"/api/v1/rooms/{room_id}/joins",
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        json={
            "request_id": str(uuid4()),
            "expected_state_version": state_version,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _room_mutation(
    client: TestClient,
    csrf: str,
    path: str,
    state_version: int,
    *,
    method: str = "PUT",
    request_id: str | None = None,
    **values: object,
) -> dict[str, object]:
    response = client.request(
        method,
        path,
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        json={
            "request_id": request_id or str(uuid4()),
            "expected_state_version": state_version,
            **values,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _start_game(
    client: TestClient,
    csrf: str,
    room_id: str,
    state_version: int,
) -> dict[str, object]:
    response = client.post(
        f"/api/v1/rooms/{room_id}/games",
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        json={
            "request_id": str(uuid4()),
            "expected_state_version": state_version,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_websocket_rejects_missing_session_origin_and_query_token(
    headless: tuple[FastAPI, ApplicationServices],
) -> None:
    application, _services = headless
    with TestClient(application, base_url=ORIGIN) as client:
        with (
            pytest.raises(WebSocketDisconnect) as missing_session,
            client.websocket_connect("/ws/v1/lobby", headers={"Origin": ORIGIN}),
        ):
            pass
        assert missing_session.value.code == 4401

        _member(client, "auth")
        with (
            pytest.raises(WebSocketDisconnect) as invalid_origin,
            client.websocket_connect(
                "/ws/v1/lobby",
                headers=_ws_headers(client, origin="https://invalid.example"),
            ),
        ):
            pass
        assert invalid_origin.value.code == 4403

        with (
            pytest.raises(WebSocketDisconnect) as query_token,
            client.websocket_connect(
                "/ws/v1/lobby?token=must-not-be-used",
                headers=_ws_headers(client),
            ),
        ):
            pass
        assert query_token.value.code == 4403


def test_websocket_rejects_expired_session(
    headless: tuple[FastAPI, ApplicationServices],
) -> None:
    application, services = headless
    assert services.headless_clock is not None
    with TestClient(application, base_url=ORIGIN) as client:
        _guest(client)
        services.headless_clock.advance(86_400_001)

        with (
            pytest.raises(WebSocketDisconnect) as expired,
            client.websocket_connect("/ws/v1/lobby", headers=_ws_headers(client)),
        ):
            pass

        assert expired.value.code == 4401


def test_lobby_first_message_and_version_change_only_for_visible_list_change(
    headless: tuple[FastAPI, ApplicationServices],
) -> None:
    application, services = headless
    assert services.realtime_api is not None
    with TestClient(application, base_url=ORIGIN) as client:
        csrf = _member(client, "lobby")
        with client.websocket_connect("/ws/v1/lobby", headers=_ws_headers(client)) as socket:
            snapshot = socket.receive_json()
            _assert_event_envelope(snapshot)
            assert snapshot["event_type"] == "lobby.snapshot"
            assert snapshot["payload"] == {"rooms": []}
            initial_version = int(snapshot["state_version"])

            room = _create_room(client, csrf)
            changed = socket.receive_json()
            _assert_event_envelope(changed)
            assert changed["event_type"] == "lobby.rooms_changed"
            assert changed["state_version"] == initial_version + 1

            after_create = services.realtime_api.events.lobby_version
            _room_mutation(
                client,
                csrf,
                f"/api/v1/rooms/{room['room_id']}/participants/me/team",
                int(room["state_version"]),
                team="BLACK",
            )
            assert services.realtime_api.events.lobby_version == after_create


def test_vote_seconds_changes_lobby_version_only_for_actual_state_change(
    headless: tuple[FastAPI, ApplicationServices],
) -> None:
    application, services = headless
    assert services.realtime_api is not None
    with TestClient(application, base_url=ORIGIN) as client:
        csrf = _member(client, "votesec")
        room = _create_room(client, csrf)
        room_id = str(room["room_id"])
        initial_room_version = int(room["state_version"])
        initial_lobby_version = services.realtime_api.events.lobby_version
        path = f"/api/v1/rooms/{room_id}/settings"

        changed = _room_mutation(
            client,
            csrf,
            path,
            initial_room_version,
            method="PATCH",
            request_id="00000000-0000-4000-8000-000000000051",
            vote_seconds=30,
        )
        assert int(changed["state_version"]) == initial_room_version + 1
        assert services.realtime_api.events.lobby_version == initial_lobby_version + 1

        replayed = _room_mutation(
            client,
            csrf,
            path,
            initial_room_version,
            method="PATCH",
            request_id="00000000-0000-4000-8000-000000000051",
            vote_seconds=30,
        )
        assert replayed["replayed"] is True
        assert services.realtime_api.events.lobby_version == initial_lobby_version + 1

        unchanged = _room_mutation(
            client,
            csrf,
            path,
            int(changed["state_version"]),
            method="PATCH",
            request_id="00000000-0000-4000-8000-000000000052",
            vote_seconds=30,
        )
        assert int(unchanged["state_version"]) == int(changed["state_version"])
        assert services.realtime_api.events.lobby_version == initial_lobby_version + 1


def test_room_socket_rejects_session_that_did_not_join_room(
    headless: tuple[FastAPI, ApplicationServices],
) -> None:
    application, _services = headless
    with (
        TestClient(application, base_url=ORIGIN) as owner,
        TestClient(application, base_url=ORIGIN) as outsider,
    ):
        owner_csrf = _member(owner, "roomowner")
        room = _create_room(owner, owner_csrf)
        _member(outsider, "outsider")

        with (
            pytest.raises(WebSocketDisconnect) as forbidden,
            outsider.websocket_connect(
                f"/ws/v1/rooms/{room['room_id']}",
                headers=_ws_headers(outsider),
            ),
        ):
            pass

        assert forbidden.value.code == 4403


def test_room_socket_rejects_command_messages(
    headless: tuple[FastAPI, ApplicationServices],
) -> None:
    application, _services = headless
    with TestClient(application, base_url=ORIGIN) as owner:
        owner_csrf = _member(owner, "norpc")
        room = _create_room(owner, owner_csrf)

        with owner.websocket_connect(
            f"/ws/v1/rooms/{room['room_id']}",
            headers=_ws_headers(owner),
        ) as socket:
            snapshot = socket.receive_json()
            _assert_event_envelope(snapshot)
            assert snapshot["event_type"] == "room.snapshot"
            socket.send_json({"command": "ready"})
            with pytest.raises(WebSocketDisconnect) as rejected:
                _next_room_event(socket)

        assert rejected.value.code == 1008


def test_new_room_socket_replaces_old_generation_without_disconnecting_participant(
    headless: tuple[FastAPI, ApplicationServices],
) -> None:
    application, _services = headless
    with (
        TestClient(application, base_url=ORIGIN) as owner,
        TestClient(application, base_url=ORIGIN) as guest,
    ):
        owner_csrf = _member(owner, "replace")
        room = _create_room(owner, owner_csrf)
        guest_csrf = _guest(guest)
        _join(guest, guest_csrf, str(room["room_id"]), int(room["state_version"]))

        with owner.websocket_connect(
            f"/ws/v1/rooms/{room['room_id']}", headers=_ws_headers(owner)
        ) as first:
            assert first.receive_json()["event_type"] == "room.snapshot"
            with owner.websocket_connect(
                f"/ws/v1/rooms/{room['room_id']}", headers=_ws_headers(owner)
            ) as replacement:
                assert replacement.receive_json()["event_type"] == "room.snapshot"
                replaced = _next_room_event(first)
                assert replaced["event_type"] == "connection.reconnect_required"
                current = owner.get(f"/api/v1/rooms/{room['room_id']}/snapshot")
                assert current.status_code == 200
                assert current.json()["participants"][0]["connected"] is True


def test_owner_socket_disconnect_preserves_owner_until_lease_expiry(
    headless: tuple[FastAPI, ApplicationServices],
) -> None:
    application, services = headless
    assert services.disconnect_expiry is not None
    assert services.headless_clock is not None
    with (
        TestClient(application, base_url=ORIGIN) as owner,
        TestClient(application, base_url=ORIGIN) as member,
    ):
        owner_csrf = _member(owner, "owner")
        room = _create_room(owner, owner_csrf)
        member_csrf = _member(member, "successor")
        room = _join(member, member_csrf, str(room["room_id"]), int(room["state_version"]))
        room = _room_mutation(
            owner,
            owner_csrf,
            f"/api/v1/rooms/{room['room_id']}/participants/me/team",
            int(room["state_version"]),
            team="BLACK",
        )
        room = _room_mutation(
            owner,
            owner_csrf,
            f"/api/v1/rooms/{room['room_id']}/participants/me/ready",
            int(room["state_version"]),
            ready=True,
        )
        room = _room_mutation(
            member,
            member_csrf,
            f"/api/v1/rooms/{room['room_id']}/participants/me/team",
            int(room["state_version"]),
            team="WHITE",
        )
        room = _room_mutation(
            member,
            member_csrf,
            f"/api/v1/rooms/{room['room_id']}/participants/me/ready",
            int(room["state_version"]),
            ready=True,
        )
        successor_id = str(room["participants"][1]["participant_id"])  # type: ignore[index]

        with owner.websocket_connect(
            f"/ws/v1/rooms/{room['room_id']}", headers=_ws_headers(owner)
        ) as socket:
            assert socket.receive_json()["event_type"] == "room.snapshot"

        current = member.get(f"/api/v1/rooms/{room['room_id']}/snapshot")
        assert current.status_code == 200
        assert current.json()["owner_id"] == room["owner_id"]
        assert current.json()["participants"][0]["connected"] is False
        assert all(item["ready"] for item in current.json()["participants"])

        assert member.portal is not None
        services.headless_clock.advance(ROOM_DISCONNECT_LEASE_MS)
        expired = member.portal.call(services.disconnect_expiry.run_once)
        assert len(expired) == 1
        after_expiry = member.get(f"/api/v1/rooms/{room['room_id']}/snapshot")
        assert after_expiry.status_code == 200
        assert after_expiry.json()["owner_id"] == successor_id
        assert all(not item["ready"] for item in after_expiry.json()["participants"])


def test_explicit_leave_closes_that_participants_room_socket(
    headless: tuple[FastAPI, ApplicationServices],
) -> None:
    application, _services = headless
    with (
        TestClient(application, base_url=ORIGIN) as owner,
        TestClient(application, base_url=ORIGIN) as guest,
    ):
        owner_csrf = _member(owner, "leaveowner")
        room = _create_room(owner, owner_csrf)
        guest_csrf = _guest(guest)
        _join(guest, guest_csrf, str(room["room_id"]), int(room["state_version"]))

        with guest.websocket_connect(
            f"/ws/v1/rooms/{room['room_id']}",
            headers=_ws_headers(guest),
        ) as socket:
            snapshot = socket.receive_json()
            left = guest.request(
                "DELETE",
                f"/api/v1/rooms/{room['room_id']}/participants/me",
                headers={"Origin": ORIGIN, "X-CSRF-Token": guest_csrf},
                json={
                    "request_id": str(uuid4()),
                    "expected_state_version": cast(
                        dict[str, object],
                        cast(dict[str, object], snapshot["payload"])["room"],
                    )["state_version"],
                },
            )
            event = _next_room_event(socket)
            with pytest.raises(WebSocketDisconnect) as closed:
                socket.receive_json()

        assert left.status_code == 200
        assert event["event_type"] == "room.participant_left"
        assert closed.value.code == 1000


def test_socket_disconnect_after_guest_to_member_transition_uses_participant_identity(
    headless: tuple[FastAPI, ApplicationServices],
) -> None:
    application, _services = headless
    with (
        TestClient(application, base_url=ORIGIN) as owner,
        TestClient(application, base_url=ORIGIN) as guest,
    ):
        owner_csrf = _member(owner, "idowner")
        room = _create_room(owner, owner_csrf)
        guest_csrf = _guest(guest)
        joined = _join(
            guest,
            guest_csrf,
            str(room["room_id"]),
            int(room["state_version"]),
        )
        participant_id = joined["participants"][1]["participant_id"]  # type: ignore[index]

        with guest.websocket_connect(
            f"/ws/v1/rooms/{room['room_id']}",
            headers=_ws_headers(guest),
        ) as socket:
            assert socket.receive_json()["event_type"] == "room.snapshot"
            _upgrade_guest_to_member(guest, guest_csrf, "idmember")
            changed = _next_room_event(socket)
            assert changed["event_type"] == "snapshot.required"
            assert cast(dict[str, object], changed["payload"])["reason"] == (
                "PARTICIPANT_IDENTITY_CHANGED"
            )
            assert int(str(cast(dict[str, object], changed["payload"])["room_state_version"])) >= 1

        current = guest.get(f"/api/v1/rooms/{room['room_id']}/snapshot")
        assert current.status_code == 200
        participant = next(
            item
            for item in current.json()["participants"]
            if item["participant_id"] == participant_id
        )
        assert participant["actor_type"] == "MEMBER"
        assert participant["connected"] is False


def test_disconnect_expiry_runner_removes_participant_once(
    headless: tuple[FastAPI, ApplicationServices],
) -> None:
    application, services = headless
    assert services.disconnect_expiry is not None
    assert services.headless_clock is not None
    with (
        TestClient(application, base_url=ORIGIN) as owner,
        TestClient(application, base_url=ORIGIN) as guest,
    ):
        owner_csrf = _member(owner, "expiry")
        room = _create_room(owner, owner_csrf)
        guest_csrf = _guest(guest)
        joined = _join(guest, guest_csrf, str(room["room_id"]), int(room["state_version"]))

        with guest.websocket_connect(
            f"/ws/v1/rooms/{room['room_id']}", headers=_ws_headers(guest)
        ) as socket:
            assert socket.receive_json()["event_type"] == "room.snapshot"

        before = owner.get(f"/api/v1/rooms/{room['room_id']}/snapshot").json()
        assert len(before["participants"]) == 2
        assert before["participants"][1]["connected"] is False

        assert guest.portal is not None
        assert guest.portal.call(services.disconnect_expiry.run_once) == ()
        services.headless_clock.advance(9_999)
        assert guest.portal.call(services.disconnect_expiry.run_once) == ()
        services.headless_clock.advance(1)
        first = guest.portal.call(services.disconnect_expiry.run_once)
        second = guest.portal.call(services.disconnect_expiry.run_once)

        assert len(first) == 1
        assert second == ()
        after = owner.get(f"/api/v1/rooms/{room['room_id']}/snapshot").json()
        assert len(after["participants"]) == 1
        assert joined["participants"][1]["participant_id"] not in str(after)


def test_planned_shutdown_does_not_record_participant_disconnect(
    headless: tuple[FastAPI, ApplicationServices],
) -> None:
    application, services = headless
    assert services.realtime_api is not None
    with TestClient(application, base_url=ORIGIN) as owner:
        owner_csrf = _member(owner, "shutdown")
        room = _create_room(owner, owner_csrf)

        with owner.websocket_connect(
            f"/ws/v1/rooms/{room['room_id']}",
            headers=_ws_headers(owner),
        ) as socket:
            assert socket.receive_json()["event_type"] == "room.snapshot"
            assert owner.portal is not None
            owner.portal.call(services.realtime_api.registry.end_runtime)
            with pytest.raises(WebSocketDisconnect) as shutdown:
                _next_room_event(socket)

        assert shutdown.value.code == 1012
        current = owner.get(f"/api/v1/rooms/{room['room_id']}/snapshot")
        assert current.status_code == 200
        assert current.json()["participants"][0]["connected"] is True


@pytest.mark.parametrize("expired", [False, True])
def test_game_vote_is_removed_when_room_socket_disconnects(
    headless: tuple[FastAPI, ApplicationServices],
    monkeypatch: pytest.MonkeyPatch,
    expired: bool,
) -> None:
    application, services = headless
    monkeypatch.setattr("seokpan.api.realtime.SESSION_CHECK_INTERVAL_SECONDS", 0.01)
    with (
        TestClient(application, base_url=ORIGIN) as owner,
        TestClient(application, base_url=ORIGIN) as member,
    ):
        owner_csrf = _member(owner, "voteowner")
        room = _create_room(owner, owner_csrf)
        member_csrf = _member(member, "votemem")
        room = _join(member, member_csrf, str(room["room_id"]), int(room["state_version"]))
        for client, csrf, team in (
            (owner, owner_csrf, "BLACK"),
            (member, member_csrf, "WHITE"),
        ):
            room = _room_mutation(
                client,
                csrf,
                f"/api/v1/rooms/{room['room_id']}/participants/me/team",
                int(room["state_version"]),
                team=team,
            )
            room = _room_mutation(
                client,
                csrf,
                f"/api/v1/rooms/{room['room_id']}/participants/me/ready",
                int(room["state_version"]),
                ready=True,
            )
        game = _start_game(owner, owner_csrf, str(room["room_id"]), int(room["state_version"]))
        vote = owner.put(
            f"/api/v1/games/{game['game_id']}/turns/{game['turn_no']}/vote",
            headers={"Origin": ORIGIN, "X-CSRF-Token": owner_csrf},
            json={
                "request_id": str(uuid4()),
                "expected_state_version": game["state_version"],
                "coordinate": "H8",
            },
        )
        assert vote.status_code == 200, vote.text
        assert vote.json()["vote_aggregation"] == [{"coordinate": "H8", "count": 1}]

        with owner.websocket_connect(
            f"/ws/v1/rooms/{room['room_id']}", headers=_ws_headers(owner)
        ) as socket:
            first = socket.receive_json()
            assert first["event_type"] == "room.snapshot"
            assert first["payload"]["game"] is not None

            if expired:
                digest = digest_opaque_token(str(owner.cookies.get("seokpan_session")))
                original_find = services.identity_api.sessions.find

                async def expired_owner(value: str) -> SessionRecord | None:
                    return None if value == digest else await original_find(value)

                monkeypatch.setattr(services.identity_api.sessions, "find", expired_owner)
                with pytest.raises(WebSocketDisconnect) as closed:
                    socket.receive_json()
                assert closed.value.code == 4401

        current = member.get(f"/api/v1/games/{game['game_id']}")
        assert current.status_code == 200
        assert current.json()["state_version"] == first["payload"]["game"]["state_version"] + 1
        assert current.json()["vote_aggregation"] == []
        owner_state = next(
            item
            for item in current.json()["participants"]
            if item["participant_id"] == room["participants"][0]["participant_id"]
        )
        assert owner_state["connected"] is False
        current_room = member.get(f"/api/v1/rooms/{room['room_id']}/snapshot")
        assert current_room.status_code == 200
        assert current_room.json()["state_version"] == first["payload"]["room"]["state_version"] + 1


def test_explicit_player_leave_removes_vote_and_updates_both_resources_once(
    headless: tuple[FastAPI, ApplicationServices],
) -> None:
    application, _services = headless
    with (
        TestClient(application, base_url=ORIGIN) as owner,
        TestClient(application, base_url=ORIGIN) as member,
    ):
        owner_csrf = _member(owner, "lvowner")
        room = _create_room(owner, owner_csrf)
        member_csrf = _member(member, "lvmember")
        room = _join(member, member_csrf, str(room["room_id"]), int(room["state_version"]))
        for client, csrf, team in (
            (owner, owner_csrf, "BLACK"),
            (member, member_csrf, "WHITE"),
        ):
            room = _room_mutation(
                client,
                csrf,
                f"/api/v1/rooms/{room['room_id']}/participants/me/team",
                int(room["state_version"]),
                team=team,
            )
            room = _room_mutation(
                client,
                csrf,
                f"/api/v1/rooms/{room['room_id']}/participants/me/ready",
                int(room["state_version"]),
                ready=True,
            )
        game = _start_game(owner, owner_csrf, str(room["room_id"]), int(room["state_version"]))
        vote = owner.put(
            f"/api/v1/games/{game['game_id']}/turns/{game['turn_no']}/vote",
            headers={"Origin": ORIGIN, "X-CSRF-Token": owner_csrf},
            json={
                "request_id": str(uuid4()),
                "expected_state_version": game["state_version"],
                "coordinate": "H8",
            },
        )
        assert vote.status_code == 200, vote.text

        with member.websocket_connect(
            f"/ws/v1/rooms/{room['room_id']}", headers=_ws_headers(member)
        ) as socket:
            first = socket.receive_json()
            room_before = first["payload"]["room"]["state_version"]
            left = owner.request(
                "DELETE",
                f"/api/v1/rooms/{room['room_id']}/participants/me",
                headers={"Origin": ORIGIN, "X-CSRF-Token": owner_csrf},
                json={
                    "request_id": str(uuid4()),
                    "expected_state_version": room_before,
                },
            )
            events = tuple([_next_room_event(socket) for _ in range(3)])
            current_game = member.get(f"/api/v1/games/{game['game_id']}")
            current_room = member.get(f"/api/v1/rooms/{room['room_id']}/snapshot")

        assert left.status_code == 200, left.text
        assert tuple(item["event_type"] for item in events) == (
            "room.participant_left",
            "room.owner_changed",
            "vote.tally_changed",
        )
        assert events[-1]["payload"]["tally"] == []
        assert events[-1]["payload"]["valid_voter_count"] == 0
        assert tuple(int(item["state_version"]) for item in events) == tuple(
            range(int(events[0]["state_version"]), int(events[0]["state_version"]) + 3)
        )
        assert current_game.status_code == 403
        assert current_game.json()["code"] == "GAME_NOT_IN_CURRENT_ROOM"
        assert current_room.status_code == 200
        assert current_room.json()["status"] == "WAITING"
        assert current_room.json()["last_game_id"] == game["game_id"]
        assert current_room.json()["state_version"] > room_before


def test_waiting_room_close_notifies_guest_to_return_to_lobby(
    headless: tuple[FastAPI, ApplicationServices],
) -> None:
    application, _services = headless
    with (
        TestClient(application, base_url=ORIGIN) as owner,
        TestClient(application, base_url=ORIGIN) as guest,
    ):
        owner_csrf = _member(owner, "close")
        room = _create_room(owner, owner_csrf)
        guest_csrf = _guest(guest)
        room = _join(guest, guest_csrf, str(room["room_id"]), int(room["state_version"]))

        with guest.websocket_connect(
            f"/ws/v1/rooms/{room['room_id']}", headers=_ws_headers(guest)
        ) as socket:
            assert socket.receive_json()["event_type"] == "room.snapshot"
            left = owner.request(
                "DELETE",
                f"/api/v1/rooms/{room['room_id']}/participants/me",
                headers={"Origin": ORIGIN, "X-CSRF-Token": owner_csrf},
                json={
                    "request_id": str(uuid4()),
                    "expected_state_version": room["state_version"],
                },
            )
            closed = _next_room_event(socket)

        assert left.status_code == 200
        assert left.json() is None
        assert closed["event_type"] == "room.closed"
        assert closed["payload"] == {"action": "RETURN_TO_LOBBY"}
        assert guest.get("/api/v1/session").json()["room_id"] is None


def test_event_setup_failure_does_not_change_disconnected_participant() -> None:
    settings = Settings(environment="test", allowed_origins=(ORIGIN,))
    services = build_headless_services(
        settings,
        realtime_events=_FailingRoomSubscriptionEvents(),
    )
    assert services.realtime_api is not None
    application = create_app(settings=settings, services=services)
    with (
        TestClient(application, base_url=ORIGIN) as owner,
        TestClient(application, base_url=ORIGIN) as member,
    ):
        owner_csrf = _member(owner, "setupfail")
        room = _create_room(owner, owner_csrf)
        member_csrf = _member(member, "setupok")
        joined = _join(
            member,
            member_csrf,
            str(room["room_id"]),
            int(room["state_version"]),
        )
        participant = joined["participants"][1]
        assert member.portal is not None
        member.portal.call(
            partial(
                services.realtime_api.connections.disconnect,
                room_id=str(room["room_id"]),
                participant_id=str(participant["participant_id"]),
                connection_generation=1,
            )
        )

        with (
            pytest.raises(WebSocketDisconnect) as failed_setup,
            member.websocket_connect(
                f"/ws/v1/rooms/{room['room_id']}",
                headers=_ws_headers(member),
            ),
        ):
            pass
        assert failed_setup.value.code == 1011

        current = member.get(f"/api/v1/rooms/{room['room_id']}/snapshot")
        assert current.status_code == 200
        assert current.json()["owner_id"] == joined["owner_id"]
        disconnected = next(
            item
            for item in current.json()["participants"]
            if item["participant_id"] == participant["participant_id"]
        )
        assert disconnected["connected"] is False


def test_room_setup_failure_after_generation_claim_starts_disconnect_lease(
    headless: tuple[FastAPI, ApplicationServices],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, services = headless
    assert services.realtime_api is not None

    async def fail_snapshot(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("snapshot provider failed after connect")

    with (
        TestClient(application, base_url=ORIGIN) as owner,
        TestClient(application, base_url=ORIGIN) as member,
    ):
        owner_csrf = _member(owner, "setupown")
        room = _create_room(owner, owner_csrf)
        member_csrf = _member(member, "setupmem")
        joined = _join(
            member,
            member_csrf,
            str(room["room_id"]),
            int(room["state_version"]),
        )
        participant_id = str(joined["participants"][1]["participant_id"])

        monkeypatch.setattr("seokpan.api.realtime.SnapshotReader.room", fail_snapshot)
        with (
            member.websocket_connect(
                f"/ws/v1/rooms/{room['room_id']}",
                headers=_ws_headers(member),
            ) as socket,
            pytest.raises(WebSocketDisconnect) as failed,
        ):
            socket.receive_json()
        assert failed.value.code == 1011

        current = owner.get(f"/api/v1/rooms/{room['room_id']}/snapshot")
        assert current.status_code == 200
        participant = next(
            item
            for item in current.json()["participants"]
            if item["participant_id"] == participant_id
        )
        assert participant["connected"] is False
        assert current.json()["owner_id"] == joined["owner_id"]


@pytest.mark.asyncio
async def test_slow_consumer_receives_snapshot_required_instead_of_unbounded_queue() -> None:
    events = InMemoryRealtimeEventAdapter(max_queue_size=1)
    subscription = await events.subscribe_room("room-1")
    await events.room_changed(
        event_type="room.ready_changed",
        room_id="room-1",
        payload={},
    )
    await events.room_changed(
        event_type="room.team_changed",
        room_id="room-1",
        payload={},
    )

    overflow = await subscription.receive()

    assert overflow.event_type == "snapshot.required"
    assert overflow.payload == {"reason": "SLOW_CONSUMER"}
    assert overflow.state_version == 3
    await subscription.close()


@pytest.mark.asyncio
async def test_room_stream_version_is_independent_and_reuses_stable_event_envelope() -> None:
    events = InMemoryRealtimeEventAdapter()
    subscription = await events.subscribe_room("room-1")

    await events.room_changed(
        event_type="room.ready_changed",
        room_id="room-1",
        event_key="room-ready:8",
        payload={"room_state_version": 8},
    )
    first = await subscription.receive()
    await events.room_changed(
        event_type="vote.tally_changed",
        room_id="room-1",
        event_key="vote-change:41",
        game_id="game-1",
        turn_no=3,
        payload={"game_state_version": 41},
    )
    second = await subscription.receive()
    await events.room_changed(
        event_type="vote.tally_changed",
        room_id="room-1",
        event_key="vote-change:41",
        game_id="game-1",
        turn_no=3,
        payload={"game_state_version": 41},
    )
    replayed = await subscription.receive()

    assert (first.state_version, second.state_version) == (2, 3)
    assert first.payload["room_state_version"] == 8
    assert second.payload["game_state_version"] == 41
    assert replayed == second
    assert events.room_version("room-1") == 3
    assert events.room_version("room-2") == 1
    await subscription.close()


@pytest.mark.asyncio
async def test_participant_left_event_is_delayed_until_disconnect_lease_expires() -> None:
    clock = ManualClock()
    votes = InMemoryVoteRuntimeAdapter(clock)
    runtime = InMemoryRoomRuntimeAdapter(clock, vote_connections=votes)
    events = InMemoryRealtimeEventAdapter()
    rooms = RoomApplicationService(runtime, _UnusedRoomPasswords(), events)
    connections = RoomConnectionCoordinator(rooms=rooms, votes=votes, clock=clock)
    runner = DisconnectExpiryRunner(
        due_disconnects=runtime,
        connections=connections,
        clock=clock,
    )
    owner = _session("owner", SessionActorType.MEMBER, "1")
    guest = _session("guest", SessionActorType.GUEST, "guest-1")
    created = await rooms.create_room(
        session=owner,
        request_id="create-1",
        config=RoomConfig(name="Event Room", minimum_ready=2),
        password=None,
    )
    assert created.snapshot is not None
    joined = await rooms.join_room(
        session=guest,
        room_id=created.snapshot.room_id,
        request_id="join-1",
        expected_state_version=created.snapshot.state_version,
        password=None,
    )
    assert joined.snapshot is not None
    subscription = await events.subscribe_room(created.snapshot.room_id)

    await rooms.disconnect(
        session=guest,
        room_id=created.snapshot.room_id,
        connection_generation=1,
    )
    disconnected = await subscription.receive()

    assert disconnected.event_type == "snapshot.required"
    assert disconnected.payload["reason"] == "PARTICIPANT_DISCONNECTED"
    assert disconnected.payload["room_state_version"] == 3
    assert await runner.run_once() == ()

    clock.advance(ROOM_DISCONNECT_LEASE_MS)
    assert len(await runner.run_once()) == 1
    left = await subscription.receive()

    assert left.event_type == "room.participant_left"
    assert left.payload == {
        "participant_id": joined.snapshot.participants[1].participant_id,
        "room_state_version": 4,
    }
    assert await runner.run_once() == ()
    await subscription.close()


def test_event_delivery_failure_does_not_roll_back_completed_http_mutation() -> None:
    settings = Settings(environment="test", allowed_origins=(ORIGIN,))
    services = build_headless_services(
        settings,
        realtime_events=_FailingRealtimeEvents(),
    )
    application = create_app(settings=settings, services=services)

    with TestClient(application, base_url=ORIGIN) as client:
        csrf = _member(client, "eventfail")
        room = _create_room(client, csrf)
        snapshot = client.get(f"/api/v1/rooms/{room['room_id']}/snapshot")

    assert snapshot.status_code == 200
    assert snapshot.json()["room_id"] == room["room_id"]


@pytest.mark.asyncio
async def test_system_invalid_room_closure_is_left_for_background_reconciliation() -> None:
    rooms = Mock()
    rooms.disconnect_participant = AsyncMock(
        return_value=RoomMutationResult(
            snapshot=None,
            departure=DepartureResult(
                previous_owner_id="owner",
                new_owner_id=None,
                room_closed=True,
                game_termination=GameTermination.SYSTEM_INVALID,
                terminated_game_id="game-closed",
            ),
            operation_at_ms=12_345,
        )
    )
    votes = Mock()
    votes.get = AsyncMock(return_value=None)
    finalizer = Mock()
    finalizer.finalize_departures = AsyncMock()
    coordinator = RoomConnectionCoordinator(
        rooms=rooms,
        votes=votes,
        clock=ManualClock(),
        departures=finalizer,
    )

    await coordinator.disconnect(
        room_id="room-closed",
        participant_id="owner",
        connection_generation=1,
    )

    finalizer.finalize_departures.assert_not_awaited()


@pytest.mark.asyncio
async def test_disconnect_expiry_runner_isolates_item_failure_and_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = DueRoomDisconnect("room-a", "participant-a", 1, 1)
    second = DueRoomDisconnect("room-b", "participant-b", 2, 1)

    source = Mock()
    source.due_disconnects = AsyncMock(return_value=(first, second))
    connections = Mock()
    connections.expire = AsyncMock(
        side_effect=(
            RoomRuleViolation("SIMULATED_ITEM_RULE_VIOLATION"),
            DisconnectExpiryResult(second, DisconnectExpiryStatus.EXPIRED),
        )
    )
    log_exception = Mock()
    monkeypatch.setattr(
        "seokpan.room.application.disconnects._LOGGER.exception",
        log_exception,
    )

    runner = DisconnectExpiryRunner(
        due_disconnects=source,
        connections=connections,
        clock=ManualClock(),
    )

    results = await runner.run_once()

    assert results == (DisconnectExpiryResult(second, DisconnectExpiryStatus.EXPIRED),)
    assert connections.expire.await_count == 2
    log_exception.assert_called_once_with(
        "Disconnect expiry item failed",
        extra={
            "event": "disconnect_expiry.item_failed",
            "room_id": first.room_id,
            "participant_id": first.participant_id,
        },
    )


@pytest.mark.asyncio
async def test_disconnect_expiry_runner_propagates_due_source_failure() -> None:
    source = Mock()
    source.due_disconnects = AsyncMock(side_effect=RuntimeError("disconnect source unavailable"))
    connections = Mock()
    connections.expire = AsyncMock()

    runner = DisconnectExpiryRunner(
        due_disconnects=source,
        connections=connections,
        clock=ManualClock(),
    )

    with pytest.raises(RuntimeError, match="disconnect source unavailable"):
        await runner.run_once()

    connections.expire.assert_not_awaited()


@pytest.mark.asyncio
async def test_disconnect_expiry_runner_propagates_item_provider_failure() -> None:
    due = DueRoomDisconnect("room-a", "participant-a", 1, 1)

    source = Mock()
    source.due_disconnects = AsyncMock(return_value=(due,))

    votes = Mock()
    votes.get = AsyncMock(side_effect=RuntimeError("vote provider unavailable"))

    rooms = Mock()
    rooms.expire_disconnect = AsyncMock()

    coordinator = RoomConnectionCoordinator(
        rooms=rooms,
        votes=votes,
        clock=ManualClock(),
    )
    runner = DisconnectExpiryRunner(
        due_disconnects=source,
        connections=coordinator,
        clock=ManualClock(),
    )

    with pytest.raises(RuntimeError, match="vote provider unavailable"):
        await runner.run_once()

    votes.get.assert_awaited_once_with(due.room_id)
    rooms.expire_disconnect.assert_not_awaited()
