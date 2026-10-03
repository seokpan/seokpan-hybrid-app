"""Lobby and Room WebSocket snapshot and event delivery."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, cast
from uuid import uuid4

import anyio
from fastapi import APIRouter, Cookie, Response, WebSocket, WebSocketDisconnect

from seokpan.api.game import GameApiServices
from seokpan.api.identity import SESSION_COOKIE, IdentityApiServices, require_current_session
from seokpan.api.problems import ApiProblem, room_problem_responses
from seokpan.api.room import LobbyResponse, RoomApiServices
from seokpan.api.snapshots import LobbyRecoveryResponse, RoomRecoveryResponse, SnapshotReader
from seokpan.api.stream_access import (
    SESSION_CHECK_INTERVAL_SECONDS,
    SESSION_CHECK_TIMEOUT_SECONDS,
    StreamAccess,
    StreamAccessState,
)
from seokpan.identity.application import SessionRecord
from seokpan.room.application import (
    RealtimeEvent,
    RealtimeEventPort,
    RealtimeSubscription,
    RoomConnectionCoordinator,
)
from seokpan.room.domain import RoomRuleViolation


@dataclass(frozen=True, slots=True)
class RealtimeApiServices:
    identity: IdentityApiServices
    rooms: RoomApiServices
    games: GameApiServices | None
    events: RealtimeEventPort
    connections: RoomConnectionCoordinator
    registry: ActiveWebSocketRegistry


class StreamEnd(StrEnum):
    CLIENT_DISCONNECT = "CLIENT_DISCONNECT"
    SETUP_FAILED = "SETUP_FAILED"
    REPLACED = "REPLACED"
    ROOM_ACCESS_ENDED = "ROOM_ACCESS_ENDED"
    SHUTDOWN = "SHUTDOWN"
    SESSION_EXPIRED = "SESSION_EXPIRED"


class ActiveWebSocketRegistry:
    """Process-local socket ownership; Redis/multi-Replica ownership is a later gate."""

    def __init__(self) -> None:
        self._active: dict[tuple[str, str], tuple[int, asyncio.Event]] = {}
        self._shutdown = asyncio.Event()
        self._runtime_users = 0

    def begin_runtime(self) -> None:
        if self._runtime_users == 0:
            self._shutdown = asyncio.Event()
        self._runtime_users += 1

    def end_runtime(self) -> None:
        if self._runtime_users > 0:
            self._runtime_users -= 1
        if self._runtime_users == 0:
            self._shutdown.set()

    def register(self, room_id: str, participant_id: str, generation: int) -> asyncio.Event:
        key = (room_id, participant_id)
        previous = self._active.get(key)
        if previous is not None:
            previous[1].set()
        replaced = asyncio.Event()
        self._active[key] = (generation, replaced)
        return replaced

    def unregister(self, room_id: str, participant_id: str, generation: int) -> None:
        key = (room_id, participant_id)
        current = self._active.get(key)
        if current is not None and current[0] == generation:
            self._active.pop(key, None)

    async def wait_for_shutdown(self) -> None:
        await self._shutdown.wait()

    def connection_generation(self, room_id: str, participant_id: str) -> int | None:
        current = self._active.get((room_id, participant_id))
        return None if current is None else current[0]

    @property
    def shutting_down(self) -> bool:
        return self._shutdown.is_set()


def realtime_router(services: RealtimeApiServices) -> APIRouter:
    router = APIRouter(tags=["realtime"])
    snapshots = SnapshotReader(services.rooms, services.games, services.events)

    @router.get(
        "/api/v1/lobby/snapshot",
        response_model=LobbyRecoveryResponse,
        responses=room_problem_responses(401, 503),
    )
    async def lobby_recovery(
        response: Response,
        session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    ) -> LobbyRecoveryResponse:
        await require_current_session(services.identity, session_cookie, touch=True)
        response.headers["Cache-Control"] = "no-store"
        return await snapshots.lobby()

    @router.get(
        "/api/v1/rooms/{room_id}/state",
        response_model=RoomRecoveryResponse,
        responses=room_problem_responses(401, 403, 404, 503),
    )
    async def room_recovery(
        room_id: str,
        response: Response,
        session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    ) -> RoomRecoveryResponse:
        current = await require_current_session(services.identity, session_cookie, touch=True)
        response.headers["Cache-Control"] = "no-store"
        return await snapshots.room(current, room_id)

    @router.websocket("/ws/v1/lobby")
    async def lobby_socket(websocket: WebSocket) -> None:
        current = await _websocket_session(websocket, services.identity)
        if current is None:
            return
        subscription: RealtimeSubscription | None = None
        try:
            subscription = await services.events.subscribe_lobby()
            snapshot = await snapshots.lobby()
            snapshot_version = snapshot.stream_version
            payload = LobbyResponse(rooms=snapshot.rooms).model_dump(mode="json")
            access = StreamAccess(services.identity, current)
            if await _check_access(websocket, access) is not None:
                return
            await websocket.accept()
            await websocket.send_json(
                _snapshot_envelope(
                    "lobby.snapshot",
                    snapshot_version,
                    payload,
                )
            )
            await _stream_events(
                websocket,
                subscription,
                services.registry,
                access=access,
                snapshot_version=snapshot_version,
            )
        except WebSocketDisconnect:
            return
        except Exception:
            await _safe_close(websocket, 1011)
        finally:
            if subscription is not None:
                await subscription.close()

    @router.websocket("/ws/v1/rooms/{room_id}")
    async def room_socket(websocket: WebSocket, room_id: str) -> None:
        current = await _websocket_session(websocket, services.identity)
        if current is None:
            return
        participation = await services.rooms.rooms.resolve_participation(current.session_digest)
        if participation is None or participation.room_id != room_id:
            await websocket.close(code=4403)
            return
        if await services.rooms.rooms.get(room_id) is None:
            await websocket.close(code=4404)
            return

        subscription: RealtimeSubscription | None = None
        generation: int | None = None
        replaced: asyncio.Event | None = None
        established = False
        initial_snapshot_sent = False
        end = StreamEnd.SHUTDOWN
        try:
            subscription = await services.events.subscribe_room(room_id)
            await websocket.accept()
            generation, _connected_state_version = await services.connections.connect(
                session=current,
                room_id=room_id,
            )
            replaced = services.registry.register(
                room_id,
                participation.participant_id,
                generation,
            )
            established = True
            snapshot = await snapshots.room(current, room_id)
            snapshot_version = snapshot.stream_version
            access = StreamAccess(
                services.identity,
                current,
                rooms=services.rooms.rooms,
                room_id=room_id,
                participant_id=participation.participant_id,
                connection_generation=generation,
            )
            access_state = await access.check()
            if services.registry.shutting_down:
                await _safe_close(websocket, 1012)
                return
            if replaced.is_set():
                end = StreamEnd.REPLACED
                await _safe_close(websocket, 4001)
                return
            access_end = await _end_access(
                websocket,
                access_state,
                room_id=room_id,
                state_version=services.events.room_version(room_id),
            )
            if access_end is not None:
                end = access_end
                return
            await websocket.send_json(
                _snapshot_envelope(
                    "room.snapshot",
                    snapshot_version,
                    {
                        "room": snapshot.room.model_dump(mode="json"),
                        "game": (
                            None if snapshot.game is None else snapshot.game.model_dump(mode="json")
                        ),
                    },
                    room_id=room_id,
                    game_id=snapshot.room.game_id,
                )
            )
            initial_snapshot_sent = True
            end = await _stream_events(
                websocket,
                subscription,
                services.registry,
                access=access,
                replaced=replaced,
                room_id=room_id,
                participant_id=participation.participant_id,
                state_version=lambda: services.events.room_version(room_id),
                snapshot_version=snapshot_version,
            )
        except WebSocketDisconnect:
            end = StreamEnd.CLIENT_DISCONNECT
        except Exception:
            if established and not initial_snapshot_sent:
                end = StreamEnd.SETUP_FAILED
            await _safe_close(websocket, 1011)
        finally:
            if subscription is not None:
                await subscription.close()
            if generation is not None:
                services.registry.unregister(room_id, participation.participant_id, generation)
            if (
                established
                and generation is not None
                and end
                in (
                    StreamEnd.CLIENT_DISCONNECT,
                    StreamEnd.SESSION_EXPIRED,
                    StreamEnd.SETUP_FAILED,
                )
            ):
                with suppress(ApiProblem, RoomRuleViolation):
                    await services.connections.disconnect(
                        room_id=room_id,
                        participant_id=participation.participant_id,
                        connection_generation=generation,
                    )

    return router


async def _websocket_session(
    websocket: WebSocket,
    identity: IdentityApiServices,
) -> SessionRecord | None:
    if websocket.headers.get("origin") not in identity.settings.allowed_origins:
        await websocket.close(code=4403)
        return None
    prohibited = {"token", "access_token", "session", SESSION_COOKIE}
    if prohibited.intersection(websocket.query_params):
        await websocket.close(code=4403)
        return None
    try:
        async with asyncio.timeout(SESSION_CHECK_TIMEOUT_SECONDS):
            return await require_current_session(
                identity,
                websocket.cookies.get(SESSION_COOKIE),
                touch=True,
            )
    except ApiProblem as error:
        await websocket.close(code=4401 if error.status == 401 else 1011)
        return None
    except Exception:
        await _safe_close(websocket, 1011)
        return None


async def _stream_events(
    websocket: WebSocket,
    subscription: RealtimeSubscription,
    registry: ActiveWebSocketRegistry,
    *,
    access: StreamAccess,
    replaced: asyncio.Event | None = None,
    room_id: str | None = None,
    participant_id: str | None = None,
    state_version: Callable[[], int] | None = None,
    snapshot_version: int = 0,
) -> StreamEnd:
    receive_task = asyncio.create_task(websocket.receive())
    shutdown_task = asyncio.create_task(registry.wait_for_shutdown())
    replaced_task = None if replaced is None else asyncio.create_task(replaced.wait())
    event_task = asyncio.create_task(subscription.receive())
    try:
        while True:
            receive_wait = cast(asyncio.Future[object], receive_task)
            shutdown_wait = cast(asyncio.Future[object], shutdown_task)
            event_wait = cast(asyncio.Future[object], event_task)
            tasks = {receive_wait, shutdown_wait, event_wait}
            if replaced_task is not None:
                tasks.add(cast(asyncio.Future[object], replaced_task))
            done, _pending = await asyncio.wait(
                tasks,
                timeout=SESSION_CHECK_INTERVAL_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if shutdown_wait in done:
                event_task.cancel()
                await _safe_close(websocket, 1012)
                return StreamEnd.SHUTDOWN
            if replaced_task is not None and cast(asyncio.Future[object], replaced_task) in done:
                event_task.cancel()
                await websocket.send_json(
                    _snapshot_envelope(
                        "connection.reconnect_required",
                        1 if state_version is None else state_version(),
                        {"reason": "CONNECTION_REPLACED"},
                        room_id=room_id,
                    )
                )
                await _safe_close(websocket, 4001)
                return StreamEnd.REPLACED
            if receive_wait in done:
                event_task.cancel()
                message = receive_task.result()
                if message["type"] != "websocket.disconnect":
                    await _safe_close(websocket, 1008)
                return StreamEnd.CLIENT_DISCONNECT
            # A terminal notice may be sent after logout removed the binding. No
            # further room/game data is delivered once that participation ends.
            if event_wait in done:
                terminal = event_task.result()
                if terminal.event_type == "room.closed" or (
                    terminal.event_type == "room.participant_left"
                    and terminal.payload.get("participant_id") == participant_id
                ):
                    await websocket.send_json(_event_value(terminal))
                    await _safe_close(websocket, 1000)
                    return StreamEnd.ROOM_ACCESS_ENDED
            access_state = await access.check()
            # Recheck transport ownership after an awaited storage read.
            if (
                registry.shutting_down
                or receive_task.done()
                or (replaced is not None and replaced.is_set())
            ):
                continue
            access_end = await _end_access(
                websocket,
                access_state,
                room_id=room_id,
                state_version=(snapshot_version if state_version is None else state_version()),
            )
            if access_end is not None:
                return access_end
            if event_wait not in done:
                continue
            event = event_task.result()
            event_task = asyncio.create_task(subscription.receive())
            if event.state_version <= snapshot_version:
                continue
            await websocket.send_json(_event_value(event))
            if (
                event.event_type == "snapshot.required"
                and event.payload.get("reason") == "SLOW_CONSUMER"
            ):
                await _safe_close(websocket, 1013)
                return StreamEnd.CLIENT_DISCONNECT
    finally:
        owned_tasks = (receive_task, shutdown_task, replaced_task, event_task)
        for task in owned_tasks:
            if task is not None and not task.done():
                task.cancel()
        with anyio.CancelScope(shield=True):
            await asyncio.gather(
                *(task for task in owned_tasks if task is not None), return_exceptions=True
            )


async def _check_access(websocket: WebSocket, access: StreamAccess) -> StreamEnd | None:
    return await _end_access(websocket, await access.check())


async def _end_access(
    websocket: WebSocket,
    state: StreamAccessState,
    *,
    room_id: str | None = None,
    state_version: int = 1,
) -> StreamEnd | None:
    if state is StreamAccessState.EXPIRED:
        await _safe_close(websocket, 4401)
        return StreamEnd.SESSION_EXPIRED
    if state is StreamAccessState.LEFT:
        await _safe_close(websocket, 1000)
        return StreamEnd.ROOM_ACCESS_ENDED
    if state is StreamAccessState.REPLACED:
        await websocket.send_json(
            _snapshot_envelope(
                "connection.reconnect_required",
                state_version,
                {"reason": "CONNECTION_REPLACED"},
                room_id=room_id,
            )
        )
        await _safe_close(websocket, 4001)
        return StreamEnd.REPLACED
    return None


def _event_value(event: RealtimeEvent) -> dict[str, object]:
    return {
        "event_type": event.event_type,
        "schema_version": event.schema_version,
        "event_id": event.event_id,
        "occurred_at": event.occurred_at,
        "state_version": event.state_version,
        "room_id": event.room_id,
        "game_id": event.game_id,
        "turn_no": event.turn_no,
        "payload": dict(event.payload),
    }


def _snapshot_envelope(
    event_type: str,
    state_version: int,
    payload: dict[str, object],
    *,
    room_id: str | None = None,
    game_id: str | None = None,
) -> dict[str, object]:
    return _event_value(
        RealtimeEvent(
            event_type=event_type,
            event_id=str(uuid4()),
            occurred_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            state_version=state_version,
            room_id=room_id,
            game_id=game_id,
            payload=payload,
        )
    )


async def _safe_close(websocket: WebSocket, code: int) -> None:
    try:
        await websocket.close(code=code)
    except RuntimeError:
        return
