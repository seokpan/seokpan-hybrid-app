"""HTTP recovery reads; mutation/event races are injected at the read boundary."""

from collections.abc import Iterator
from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from seokpan.app import ApplicationServices, build_headless_services, create_app
from seokpan.room.application import RoomRuntimeSnapshot
from seokpan.settings import Settings

ORIGIN = "http://localhost:5173"


@pytest.fixture
def recovery() -> Iterator[tuple[TestClient, ApplicationServices, str]]:
    settings = Settings(environment="test", allowed_origins=(ORIGIN,))
    services = build_headless_services(settings)
    with TestClient(create_app(settings=settings, services=services), base_url=ORIGIN) as client:
        assert (
            client.post(
                "/api/v1/members",
                headers={"Origin": ORIGIN},
                json={
                    "login_id": "recovery_member",
                    "nickname": "복구시험",
                    "password": "correct-pass",
                },
            ).status_code
            == 201
        )
        login = client.post(
            "/api/v1/sessions/member",
            headers={"Origin": ORIGIN},
            json={"login_id": "recovery_member", "password": "correct-pass"},
        )
        assert login.status_code == 200
        created = client.post(
            "/api/v1/rooms",
            headers={"Origin": ORIGIN, "X-CSRF-Token": login.json()["csrf_token"]},
            json={"request_id": str(uuid4()), "name": "복구 검사방"},
        )
        assert created.status_code == 201
        yield client, services, created.json()["room_id"]


def test_recovery_keeps_room_connection_and_versions(
    recovery: tuple[TestClient, ApplicationServices, str],
) -> None:
    client, services, room_id = recovery
    assert services.room_api is not None and services.realtime_api is not None
    assert client.portal is not None
    with client.websocket_connect(
        f"/ws/v1/rooms/{room_id}",
        headers={
            "Origin": ORIGIN,
            "Cookie": f"seokpan_session={client.cookies['seokpan_session']}",
        },
    ) as socket:
        assert socket.receive_json()["event_type"] == "room.snapshot"
        before = client.portal.call(services.room_api.rooms.get, room_id)
        version = services.realtime_api.events.room_version(room_id)
        for _ in range(2):
            response = client.get(f"/api/v1/rooms/{room_id}/state")
            assert response.status_code == 200
            assert response.headers["Cache-Control"] == "no-store"
            payload = response.json()
            assert payload["game"] is None
            assert payload["stream_version"] == version
            assert before is not None and payload["room"]["state_version"] == before.state_version
            assert client.portal.call(services.room_api.rooms.get, room_id) == before
        lobby = client.get("/api/v1/lobby/snapshot")
        assert lobby.status_code == 200
        assert lobby.headers["Cache-Control"] == "no-store"
        assert lobby.json()["stream_version"] == services.realtime_api.events.lobby_version
        assert lobby.json()["rooms"][0]["status"] == "WAITING"
        schema = client.get("/api/openapi.json").json()
        assert "403" in schema["paths"]["/api/v1/rooms/{room_id}/state"]["get"]["responses"]


def test_room_change_during_read_retries_without_mixing_versions(
    recovery: tuple[TestClient, ApplicationServices, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, services, room_id = recovery
    assert services.room_api is not None and client.portal is not None
    old = client.portal.call(services.room_api.rooms.get, room_id)
    assert old is not None
    new = replace(
        old, state_version=old.state_version + 1, config=replace(old.config, vote_seconds=30)
    )
    reads = AsyncMock(side_effect=[old, new, new, new])
    monkeypatch.setattr(services.room_api.rooms, "get", reads)
    result = client.get(f"/api/v1/rooms/{room_id}/state")
    assert result.status_code == 200
    assert result.json()["room"]["vote_seconds"] == 30
    assert result.json()["room"]["state_version"] == new.state_version
    assert reads.await_count == 4


def test_continuous_event_race_returns_unavailable_not_partial_state(
    recovery: tuple[TestClient, ApplicationServices, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, services, room_id = recovery
    assert services.room_api is not None and services.realtime_api is not None
    original = services.room_api.rooms.get
    events = services.realtime_api.events
    calls = 0

    async def racing_get(value: str) -> RoomRuntimeSnapshot | None:
        nonlocal calls
        calls += 1
        await events.room_changed(event_type="snapshot.required", room_id=value, payload={})
        return await original(value)

    monkeypatch.setattr(services.room_api.rooms, "get", racing_get)
    response = client.get(f"/api/v1/rooms/{room_id}/state")
    assert response.status_code == 503
    assert response.json()["code"] == "SNAPSHOT_CHANGED"
    assert "room" not in response.json() and calls == 6


def test_lobby_change_during_read_retries(
    recovery: tuple[TestClient, ApplicationServices, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, services, room_id = recovery
    assert services.room_api is not None and client.portal is not None
    old = client.portal.call(services.room_api.rooms.get, room_id)
    assert old is not None
    new = replace(
        old, state_version=old.state_version + 1, config=replace(old.config, vote_seconds=30)
    )
    reads = AsyncMock(side_effect=[(old,), (new,), (new,), (new,)])
    monkeypatch.setattr(services.room_api.rooms, "list_rooms", reads)
    response = client.get("/api/v1/lobby/snapshot")
    assert response.status_code == 200 and response.json()["rooms"][0]["vote_seconds"] == 30
    assert reads.await_count == 4


def test_recovery_denies_missing_session_and_other_room(
    recovery: tuple[TestClient, ApplicationServices, str],
) -> None:
    client, _, room_id = recovery
    assert client.get(f"/api/v1/rooms/{uuid4()}/state").status_code == 403
    client.cookies.clear()
    assert client.get(f"/api/v1/rooms/{room_id}/state").status_code == 401
    assert client.get("/api/v1/lobby/snapshot").status_code == 401
