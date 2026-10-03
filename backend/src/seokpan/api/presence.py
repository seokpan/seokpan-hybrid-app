"""Authenticated presence stream, independent of Room and chat connections."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from dataclasses import dataclass, field
from uuid import uuid4

import anyio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from seokpan.api.identity import SESSION_COOKIE, IdentityApiServices, require_current_session
from seokpan.api.problems import ApiProblem
from seokpan.api.realtime import ActiveWebSocketRegistry, _safe_close
from seokpan.api.stream_access import SESSION_CHECK_TIMEOUT_SECONDS
from seokpan.identity.application import SessionRecord
from seokpan.presence import (
    PresenceIdentity,
    PresenceLease,
    PresencePort,
    PresenceSnapshot,
    PresenceUnavailable,
)

# Development/Fake transport timings; not a Room grace or production SLA.
PRESENCE_INTERVAL_SECONDS = 2.0
PRESENCE_REPLY_TIMEOUT_SECONDS = 5.0


@dataclass(slots=True)
class PresenceApiServices:
    identity: IdentityApiServices
    presence: PresencePort
    registry: ActiveWebSocketRegistry
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def open(self, current: SessionRecord) -> PresenceLease:
        async with asyncio.timeout(SESSION_CHECK_TIMEOUT_SECONDS), self._lock:
            return await self.presence.open(
                PresenceIdentity(current.actor_type, current.actor_id), current.session_digest
            )

    async def close(self, lease: PresenceLease) -> None:
        async with asyncio.timeout(SESSION_CHECK_TIMEOUT_SECONDS), self._lock:
            await self.presence.close(lease)

    async def checked_snapshot(self, lease: PresenceLease) -> PresenceSnapshot:
        # Recheck every registered Session, not only the requesting browser.
        # This lock coordinates only this Fake process; not Redis replicas.
        async with asyncio.timeout(SESSION_CHECK_TIMEOUT_SECONDS), self._lock:
            if self.registry.shutting_down:
                raise PresenceUnavailable("PRESENCE_SHUTTING_DOWN")
            connections = await self.presence.connections()
            sessions = {item.session_digest: item.identity for item in connections}
            for digest, identity in sessions.items():
                current = await self.identity.sessions.find(digest)
                if current is None:
                    await self.presence.invalidate_session(digest)
                elif (current.actor_type, current.actor_id) != (
                    identity.actor_type,
                    identity.actor_id,
                ):
                    raise PresenceUnavailable("PRESENCE_IDENTITY_CHANGED")
            if self.registry.shutting_down:
                raise PresenceUnavailable("PRESENCE_SHUTTING_DOWN")
            if not any(item.lease == lease for item in await self.presence.connections()):
                raise ApiProblem(401, "AUTH_REQUIRED", "Presence connection ended")
            # Called only after a matching response to this connection's ping.
            await self.presence.renew(lease)
            return await self.presence.snapshot()


def presence_router(services: PresenceApiServices) -> APIRouter:
    router = APIRouter(tags=["presence"])

    @router.websocket("/ws/v1/presence")
    async def socket(websocket: WebSocket) -> None:
        lease: PresenceLease | None = None
        try:
            if (
                websocket.query_params
                or websocket.headers.get("origin") not in services.identity.settings.allowed_origins
            ):
                await websocket.close(code=4403)
                return
            async with asyncio.timeout(SESSION_CHECK_TIMEOUT_SECONDS):
                # Unlike game connection acquisition, automatic presence connects
                # must not keep an otherwise idle authenticated Session alive.
                current = await require_current_session(
                    services.identity, websocket.cookies.get(SESSION_COOKIE), touch=False
                )
            await websocket.accept()
            while not services.registry.shutting_down:
                challenge = str(uuid4())
                async with asyncio.timeout(PRESENCE_REPLY_TIMEOUT_SECONDS):
                    await websocket.send_json(
                        {"schema_version": 1, "event_type": "presence.ping", "challenge": challenge}
                    )
                    raw = await websocket.receive_text()
                if len(raw) > 150:
                    await _safe_close(websocket, 4400)
                    return
                try:
                    pong = json.loads(raw)
                except ValueError:
                    pong = None
                if pong != {"event_type": "presence.pong", "challenge": challenge}:
                    await _safe_close(websocket, 4400)
                    return
                # A connection is counted only once a round trip succeeds.
                if lease is None:
                    lease = await services.open(current)
                snapshot = await services.checked_snapshot(lease)
                async with asyncio.timeout(PRESENCE_REPLY_TIMEOUT_SECONDS):
                    await websocket.send_json(
                        {
                            "schema_version": 1,
                            "event_type": "presence.snapshot",
                            "challenge": challenge,
                            "online_users": snapshot.online_users,
                        }
                    )
                # Detect explicit disconnect without waiting for the next ping.
                try:
                    async with asyncio.timeout(PRESENCE_INTERVAL_SECONDS):
                        incoming = await websocket.receive()
                    if incoming["type"] != "websocket.disconnect":
                        await _safe_close(websocket, 4400)
                    return
                except TimeoutError:
                    pass
            await _safe_close(websocket, 1012)
        except ApiProblem as error:
            await _safe_close(websocket, 4401 if error.status == 401 else 1011)
        except WebSocketDisconnect:
            pass
        except Exception:
            await _safe_close(websocket, 1011)
        finally:
            if lease is not None:
                with anyio.CancelScope(shield=True):
                    with suppress(Exception):
                        await services.close(lease)

    return router
