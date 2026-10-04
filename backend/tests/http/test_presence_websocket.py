from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from seokpan.app import ApplicationServices, build_headless_services, create_app
from seokpan.identity.application import digest_opaque_token
from seokpan.settings import Settings

ORIGIN = "http://localhost:5173"
PATH = "/ws/v1/presence"


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, ApplicationServices]]:
    monkeypatch.setattr("seokpan.api.presence.PRESENCE_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr("seokpan.api.presence.PRESENCE_REPLY_TIMEOUT_SECONDS", 0.5)
    settings = Settings(environment="test", allowed_origins=(ORIGIN,))
    services = build_headless_services(settings)
    with TestClient(create_app(settings=settings, services=services), base_url=ORIGIN) as client:
        yield client, services


def guest(client: TestClient) -> dict[str, str]:
    client.cookies.clear()
    result = client.post("/api/v1/sessions/guest", headers={"Origin": ORIGIN})
    assert result.status_code == 201
    headers = {
        "Origin": ORIGIN,
        "Cookie": f"seokpan_session={client.cookies.get('seokpan_session')}",
        "X-CSRF-Token": result.json()["csrf_token"],
    }
    client.cookies.clear()
    return headers


def round_trip(ws: WebSocketTestSession) -> int:
    ping = ws.receive_json()
    assert set(ping) == {"schema_version", "event_type", "challenge"}
    assert ping["event_type"] == "presence.ping"
    ws.send_json({"event_type": "presence.pong", "challenge": ping["challenge"]})
    snapshot = ws.receive_json()
    assert snapshot == {
        "schema_version": 1,
        "event_type": "presence.snapshot",
        "challenge": ping["challenge"],
        "online_users": snapshot["online_users"],
    }
    return int(snapshot["online_users"])


def test_tabs_guests_and_old_connection_close(setup: Any) -> None:
    client, _ = setup
    one, two = guest(client), guest(client)
    with client.websocket_connect(PATH, headers=one) as first:
        assert round_trip(first) == 1
        with client.websocket_connect(PATH, headers=one) as tab:
            assert round_trip(tab) == 1
            with client.websocket_connect(PATH, headers=two) as other:
                assert round_trip(other) == 2
            assert round_trip(tab) == 1
        assert round_trip(first) == 1


def test_logout_removes_other_tabs_before_publishing_count(setup: Any) -> None:
    client, _ = setup
    one, two = guest(client), guest(client)
    with client.websocket_connect(PATH, headers=one) as first:
        assert round_trip(first) == 1
        with client.websocket_connect(PATH, headers=two) as other:
            assert round_trip(other) == 2
            assert client.delete("/api/v1/session", headers=one).status_code == 204
            assert round_trip(other) == 1
            with pytest.raises(WebSocketDisconnect) as ended:
                round_trip(first)
            assert ended.value.code == 4401


def test_connect_pong_and_snapshot_do_not_renew_idle_session(setup: Any) -> None:
    client, services = setup
    headers = guest(client)
    digest = digest_opaque_token(headers["Cookie"].split("=", 1)[1])
    assert client.portal is not None
    before = client.portal.call(services.identity_api.sessions.find, digest)
    services.headless_clock.advance(60_000)
    with client.websocket_connect(PATH, headers=headers) as ws:
        assert round_trip(ws) == 1
        after = client.portal.call(services.identity_api.sessions.find, digest)
        assert after == before
        services.headless_clock.advance(2 * 60 * 60 * 1000)
        with pytest.raises(WebSocketDisconnect) as ended:
            round_trip(ws)
        assert ended.value.code == 4401


@pytest.mark.parametrize("bad", [None, "http://foreign.test"])
def test_origin_is_required(setup: Any, bad: str | None) -> None:
    client, _ = setup
    headers = guest(client)
    headers.pop("Origin")
    if bad:
        headers["Origin"] = bad
    with (
        pytest.raises(WebSocketDisconnect) as denied,
        client.websocket_connect(PATH, headers=headers),
    ):
        pass
    assert denied.value.code == 4403


def test_cookie_required_and_query_credentials_rejected(setup: Any) -> None:
    client, _ = setup
    with (
        pytest.raises(WebSocketDisconnect) as denied,
        client.websocket_connect(PATH, headers={"Origin": ORIGIN}),
    ):
        pass
    assert denied.value.code == 4401
    headers = guest(client)
    with (
        pytest.raises(WebSocketDisconnect) as denied,
        client.websocket_connect(PATH + "?actor_id=1", headers=headers),
    ):
        pass
    assert denied.value.code == 4403


@pytest.mark.parametrize("payload", ["not json", "{}", '"pong"', "x" * 151])
def test_unexpected_messages_rejected(setup: Any, payload: str) -> None:
    client, _ = setup
    with client.websocket_connect(PATH, headers=guest(client)) as ws:
        ws.receive_json()
        ws.send_text(payload)
        with pytest.raises(WebSocketDisconnect) as ended:
            ws.receive_json()
        assert ended.value.code == 4400


def test_silent_connection_never_enters_count(setup: Any) -> None:
    client, _ = setup
    one, two = guest(client), guest(client)
    with client.websocket_connect(PATH, headers=one) as silent:
        silent.receive_json()
        with client.websocket_connect(PATH, headers=two) as active:
            assert round_trip(active) == 1
        with pytest.raises(WebSocketDisconnect) as ended:
            silent.receive_json()
        assert ended.value.code == 1011


def test_session_read_failure_is_not_a_zero_snapshot(setup: Any, monkeypatch: Any) -> None:
    client, services = setup
    with client.websocket_connect(PATH, headers=guest(client)) as ws:
        assert round_trip(ws) == 1
        monkeypatch.setattr(services.identity_api.sessions, "find", AsyncMock(side_effect=OSError))
        with pytest.raises(WebSocketDisconnect) as ended:
            round_trip(ws)
        assert ended.value.code == 1011


def test_replayed_pong_is_not_another_heartbeat(setup: Any) -> None:
    client, _ = setup
    with client.websocket_connect(PATH, headers=guest(client)) as ws:
        ping = ws.receive_json()
        pong = {"event_type": "presence.pong", "challenge": ping["challenge"]}
        ws.send_json(pong)
        ws.receive_json()
        ws.send_json(pong)
        with pytest.raises(WebSocketDisconnect) as ended:
            ws.receive_json()
        assert ended.value.code == 4400


def test_guest_login_replaces_old_identity_not_two_users(setup: Any) -> None:
    client, _ = setup
    member = {"login_id": "presenceuser", "nickname": "presenceuser", "password": "correct-pass"}
    assert (
        client.post("/api/v1/members", headers={"Origin": ORIGIN}, json=member).status_code == 201
    )
    credentials = {"login_id": member["login_id"], "password": member["password"]}
    old = guest(client)
    with client.websocket_connect(PATH, headers=old) as previous:
        assert round_trip(previous) == 1
        result = client.post("/api/v1/sessions/member", headers=old, json=credentials)
        assert result.status_code == 200
        headers = {
            "Origin": ORIGIN,
            "Cookie": f"seokpan_session={client.cookies.get('seokpan_session')}",
        }
        client.cookies.clear()
        with client.websocket_connect(PATH, headers=headers) as current:
            assert round_trip(current) == 1
            device = client.post(
                "/api/v1/sessions/member", headers={"Origin": ORIGIN}, json=credentials
            )
            assert device.status_code == 200
            other_headers = {
                "Origin": ORIGIN,
                "Cookie": f"seokpan_session={client.cookies.get('seokpan_session')}",
            }
            client.cookies.clear()
            with client.websocket_connect(PATH, headers=other_headers) as other:
                assert round_trip(other) == 1
            with pytest.raises(WebSocketDisconnect) as ended:
                round_trip(previous)
            assert ended.value.code == 4401


def test_slow_other_session_does_not_publish_partial_count(setup: Any, monkeypatch: Any) -> None:
    client, services = setup
    one, two = guest(client), guest(client)
    find = services.identity_api.sessions.find
    blocked = digest_opaque_token(one["Cookie"].split("=", 1)[1])

    async def slow(digest: str) -> Any:
        if digest == blocked:
            await asyncio.sleep(1)
        return await find(digest)

    with client.websocket_connect(PATH, headers=one) as first:
        assert round_trip(first) == 1
        with client.websocket_connect(PATH, headers=two) as other:
            assert round_trip(other) == 2
            monkeypatch.setattr("seokpan.api.presence.SESSION_CHECK_TIMEOUT_SECONDS", 0.05)
            monkeypatch.setattr(services.identity_api.sessions, "find", slow)
            with pytest.raises(WebSocketDisconnect) as ended:
                round_trip(other)
            assert ended.value.code == 1011


def test_inconsistent_identity_is_unknown_not_success(setup: Any, monkeypatch: Any) -> None:
    client, services = setup
    headers = guest(client)
    digest = digest_opaque_token(headers["Cookie"].split("=", 1)[1])
    assert client.portal is not None
    session = client.portal.call(services.identity_api.sessions.find, digest)
    with client.websocket_connect(PATH, headers=headers) as ws:
        assert round_trip(ws) == 1
        monkeypatch.setattr(
            services.identity_api.sessions,
            "find",
            AsyncMock(return_value=replace(session, actor_id="guest-other")),
        )
        with pytest.raises(WebSocketDisconnect) as ended:
            round_trip(ws)
        assert ended.value.code == 1011
