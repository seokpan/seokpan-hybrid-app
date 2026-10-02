"""Actual loopback TCP/HTTP/WebSocket smoke, separate from ASGI TestClient tests."""

import asyncio
import json
import socket

import httpx2
import pytest
import uvicorn
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from seokpan.development import create_development_app
from seokpan.settings import Settings

ORIGIN = "http://localhost:5173"


async def check_connection(base_url: str, ws_url: str) -> None:
    async with httpx2.AsyncClient(base_url=base_url, trust_env=False, timeout=5) as client:
        guest = await client.post("/api/v1/sessions/guest", headers={"Origin": ORIGIN})
        assert guest.status_code == 201
        assert guest.headers["Cache-Control"] == "no-store"
        recovered = await client.post(
            "/api/v1/session/csrf",
            headers={"Origin": ORIGIN, "X-CSRF-Bootstrap": "1"},
            json={},
        )
        assert recovered.status_code == 200
        assert recovered.json()["csrf_token"] == guest.json()["csrf_token"]
        denied = await client.post(
            "/api/v1/session/csrf",
            headers={"Origin": "https://untrusted.example", "X-CSRF-Bootstrap": "1"},
            json={},
        )
        assert denied.status_code == 403
        cookie = {"Cookie": f"seokpan_session={client.cookies['seokpan_session']}"}
        async with connect(ws_url, origin=ORIGIN, additional_headers=cookie, proxy=None) as ws:
            snapshot = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert snapshot["event_type"] == "lobby.snapshot"
            pong = await ws.ping()
            await asyncio.wait_for(pong, timeout=5)
            assert (await client.get("/api/v1/lobby/snapshot")).status_code == 200
        with pytest.raises(InvalidStatus) as failure:
            async with connect(
                ws_url, origin="https://untrusted.example", additional_headers=cookie, proxy=None
            ):
                pass
        assert failure.value.response.status_code == 403


@pytest.mark.asyncio
async def test_real_uvicorn_http_websocket_and_origin() -> None:
    app = create_development_app(settings=Settings(environment="test"))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False, ws="auto"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                    raise AssertionError("server did not start")
                await asyncio.sleep(0.01)
        await check_connection(f"http://127.0.0.1:{port}", f"ws://127.0.0.1:{port}/ws/v1/lobby")
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=5)
        finally:
            listener.close()
    assert not app.state.development_runner.running
