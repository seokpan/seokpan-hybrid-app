"""Local Browser composition; no real DB/Redis or browser is used here."""

import asyncio
import time
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from test_game_http import ORIGIN, _member, _ready_room

from seokpan.app import build_headless_services
from seokpan.development import (
    DevelopmentRunner,
    DevelopmentTieSelector,
    MonotonicDevelopmentClock,
    create_browser_e2e_app,
    create_development_app,
)
from seokpan.persistence.memory import ManualClock
from seokpan.settings import Settings


@pytest.mark.parametrize("environment", ["production", "development"])
def test_development_rejects_deployed_environments(environment: str) -> None:
    settings = Settings.model_validate({"environment": environment})
    with pytest.raises(RuntimeError, match="local/test"):
        create_development_app(settings=settings)


@pytest.mark.parametrize("name", ["identity_database_url", "game_database_url", "redis_url"])
def test_development_rejects_provider_configuration(name: str) -> None:
    settings = Settings.model_validate({"environment": "test", name: "not-a-real-url"})
    with pytest.raises(RuntimeError, match="real provider"):
        create_development_app(settings=settings)


def test_development_rejects_non_loopback_origin() -> None:
    with pytest.raises(RuntimeError, match="loopback"):
        create_development_app(settings=Settings(allowed_origins=("https://example.com",)))


def test_e2e_origin_is_separate_and_original_factory_remains_restricted() -> None:
    with pytest.raises(RuntimeError, match="loopback"):
        create_development_app(settings=Settings(allowed_origins=("http://localhost:5175",)))
    with TestClient(create_browser_e2e_app()) as client:
        assert (
            client.post(
                "/api/v1/sessions/guest", headers={"Origin": "http://localhost:5173"}
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/v1/sessions/guest", headers={"Origin": "http://localhost:5175"}
            ).status_code
            == 201
        )


@pytest.mark.parametrize("name", ["IDENTITY_DATABASE_URL", "GAME_DATABASE_URL", "REDIS_URL"])
def test_e2e_factory_refuses_inherited_provider_settings(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(f"SEOKPAN_{name}", "not-a-real-url")
    with pytest.raises(RuntimeError, match="real provider"):
        create_browser_e2e_app()


def test_clock_uses_monotonic_elapsed_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("seokpan.development.time.time_ns", lambda: 900_000_000)
    values = iter([100_000_000, 123_000_000, 124_000_000])
    monkeypatch.setattr("seokpan.development.time.monotonic_ns", lambda: next(values))
    clock = MonotonicDevelopmentClock()
    monkeypatch.setattr("seokpan.development.time.time_ns", lambda: 1)
    assert clock.now_ms == 923
    assert clock.now_ms == 924


@pytest.mark.asyncio
async def test_tie_selection_uses_server_random_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("seokpan.development.secrets.choice", lambda values: values[-1])
    selector = DevelopmentTieSelector()
    assert await selector.select(game_id="unused", turn_no=1, candidates=("A1", "B2")) == "B2"
    with pytest.raises(ValueError, match="TIE_CANDIDATES_REQUIRED"):
        await selector.select(game_id="unused", turn_no=1, candidates=())


def test_development_lifecycle_and_no_public_tick_route() -> None:
    app = create_development_app(settings=Settings(environment="test"))
    runner = app.state.development_runner
    assert not runner.running
    with TestClient(app) as client:
        assert runner.available()
        assert client.get("/health/ready").status_code == 200
        assert client.post("/api/v1/development/tick").status_code == 404
        assert app.state.services.headless_clock is None
    assert not runner.running and not runner.failed


def test_pause_blocks_requests_without_processing_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = ManualClock()
    app = create_development_app(settings=Settings(environment="test"), clock=clock)
    services = app.state.services
    turns = AsyncMock(return_value=())
    monkeypatch.setattr(services.turn_resolution, "run_once", turns)
    with TestClient(app) as client:
        calls = turns.await_count
        clock.advance(2001)
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["code"] == "DEVELOPMENT_RUNTIME_PAUSED"
        with (
            pytest.raises(WebSocketDisconnect) as error,
            client.websocket_connect("/ws/v1/lobby"),
        ):
            pass
        assert error.value.code == 1013
        assert turns.await_count == calls
        assert app.state.development_runner.failed


@pytest.mark.asyncio
async def test_runner_failure_stops_without_replaying_or_logging_details(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    clock = ManualClock()
    services = build_headless_services(Settings(environment="test"), clock=clock)
    assert services.turn_resolution is not None
    failure = AsyncMock(side_effect=RuntimeError("sensitive-placeholder"))
    monkeypatch.setattr(services.turn_resolution, "run_once", failure)
    runner = DevelopmentRunner(services, clock)
    await runner.run()
    assert runner.failed and not runner.running and failure.await_count == 1
    assert "sensitive-placeholder" not in caplog.text
    with pytest.raises(RuntimeError, match="NOT_RESTARTABLE"):
        await runner.run()


@pytest.mark.asyncio
async def test_runner_cancellation_is_clean() -> None:
    clock = ManualClock()
    services = build_headless_services(Settings(environment="test"), clock=clock)
    runner = DevelopmentRunner(services, clock)
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(0)
    assert runner.running
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not runner.running and not runner.failed


def test_auto_discovery_closes_passes_without_public_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Controlled clock jumps isolate automatic discovery; suspension refusal is tested above.
    monkeypatch.setattr("seokpan.development.MAX_PAUSE_MS", 60_000)
    clock = ManualClock()
    app = create_development_app(settings=Settings(environment="test"), clock=clock)
    with TestClient(app, base_url=ORIGIN) as black:
        white = TestClient(app, base_url=ORIGIN)
        try:
            black_csrf, white_csrf = _member(black, "devb"), _member(white, "devw")
            room = _ready_room(black, black_csrf, white, white_csrf)
            room_id = room["room_id"]
            started = black.post(
                f"/api/v1/rooms/{room_id}/games",
                headers={"Origin": ORIGIN, "X-CSRF-Token": black_csrf},
                json={"request_id": str(uuid4()), "expected_state_version": room["state_version"]},
            )
            assert started.status_code == 201
            game_id = started.json()["game_id"]
            clock.advance(15000)
            end = time.monotonic() + 3
            while time.monotonic() < end:
                state = black.get(f"/api/v1/games/{game_id}").json()
                if state.get("turn_no") == 2:
                    break
                time.sleep(0.02)
            assert state["turn_no"] == 2 and state["move_no"] == 0
            clock.advance(15000)
            end = time.monotonic() + 3
            while time.monotonic() < end:
                result = black.get(f"/api/v1/games/{game_id}/result")
                if result.status_code == 200:
                    break
                time.sleep(0.02)
            assert result.status_code == 200
            assert result.json()["end_reason"] == "JOINT_LOSS"
            recovered = black.get(f"/api/v1/rooms/{room_id}/state").json()
            assert recovered["room"]["status"] == "WAITING"
            assert recovered["game"] is None
            assert app.state.development_runner.available()
        finally:
            white.close()


def test_auto_expiry_removes_disconnected_participant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("seokpan.development.MAX_PAUSE_MS", 60_000)
    clock = ManualClock()
    app = create_development_app(settings=Settings(environment="test"), clock=clock)
    with TestClient(app, base_url=ORIGIN) as black:
        white = TestClient(app, base_url=ORIGIN)
        try:
            black_csrf, white_csrf = _member(black, "expb"), _member(white, "expw")
            room = _ready_room(black, black_csrf, white, white_csrf)
            room_id = room["room_id"]
            with black.websocket_connect(
                f"/ws/v1/rooms/{room_id}",
                headers={
                    "Origin": ORIGIN,
                    "Cookie": f"seokpan_session={white.cookies['seokpan_session']}",
                },
            ) as ws:
                assert ws.receive_json()["event_type"] == "room.snapshot"
            clock.advance(30000)
            end = time.monotonic() + 3
            while time.monotonic() < end:
                state = black.get(f"/api/v1/rooms/{room_id}/state").json()
                if len(state["room"]["participants"]) == 1:
                    break
                time.sleep(0.02)
            assert len(state["room"]["participants"]) == 1
            assert state["room"]["owner_id"] == room["owner_id"]
        finally:
            white.close()


def test_pause_closes_socket_without_owner_departure(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = ManualClock()
    app = create_development_app(settings=Settings(environment="test"), clock=clock)
    with TestClient(app, base_url=ORIGIN) as client:
        csrf = _member(client, "pause")
        created = client.post(
            "/api/v1/rooms",
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
            json={"request_id": str(uuid4()), "name": "일시중지 검사"},
        ).json()
        room_id = created["room_id"]
        with client.websocket_connect(
            f"/ws/v1/rooms/{room_id}",
            headers={
                "Origin": ORIGIN,
                "Cookie": f"seokpan_session={client.cookies['seokpan_session']}",
            },
        ) as ws:
            assert ws.receive_json()["event_type"] == "room.snapshot"
            clock.advance(2001)
            with pytest.raises(WebSocketDisconnect) as error:
                ws.receive_json()
            assert error.value.code == 1012
        assert client.portal is not None
        room = client.portal.call(app.state.services.room_api.rooms.get, room_id)
        assert room is not None and room.owner_id == created["owner_id"]
        assert room.participants[0].connected
