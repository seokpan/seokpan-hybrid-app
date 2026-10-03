"""Authorized HTTP chat commands and an independent receive-only chat stream."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

import anyio
from fastapi import APIRouter, Cookie, Header, Request, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field

from seokpan.api.identity import (
    SESSION_COOKIE,
    IdentityApiServices,
    guest_display_name,
    require_allowed_origin,
    require_csrf,
    require_current_session,
)
from seokpan.api.problems import ApiProblem, room_problem_responses
from seokpan.api.realtime import ActiveWebSocketRegistry, _safe_close, _websocket_session
from seokpan.api.stream_access import (
    SESSION_CHECK_INTERVAL_SECONDS,
    SESSION_CHECK_TIMEOUT_SECONDS,
)
from seokpan.chat import (
    ChatDeliveryPort,
    ChatDeliveryUnavailable,
    ChatReceipt,
    ChatRuleViolation,
    ChatScope,
    ChatScopeType,
    ChatSender,
    ChatSubscription,
    ChatSubscriptionClosed,
    SendChat,
)
from seokpan.identity.application import SessionActorType, SessionRecord
from seokpan.room.application import RoomApplicationService, RoomParticipation
from seokpan.room.domain import RoomStatus


@dataclass(frozen=True, slots=True)
class ChatApiServices:
    identity: IdentityApiServices
    rooms: RoomApplicationService
    delivery: ChatDeliveryPort
    registry: ActiveWebSocketRegistry


class SendChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    text: str = Field(repr=False)


class ChatReceiptResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)
    message_id: str
    occurred_at: str
    replayed: bool


class ChatAccess:
    """Bind a stream to one Session, participation and game connection generation.

    Chat never acquires a game connection or renews an idle Session. A changed
    identity/participation must open a fresh, empty chat subscription.
    """

    def __init__(
        self,
        services: ChatApiServices,
        current: SessionRecord,
        scope: ChatScope,
        binding: RoomParticipation | None,
    ):
        self.services = services
        self.current = current
        self.scope = scope
        self.watch = services.rooms.watch_participation(current.session_digest)
        self.binding = binding
        self.generation = (
            None
            if self.binding is None
            else (
                self.binding.connection_generation
                if self.binding.connection_generation is not None
                else services.registry.connection_generation(
                    self.binding.room_id, self.binding.participant_id
                )
            )
        )

    @classmethod
    async def create(
        cls, services: ChatApiServices, current: SessionRecord, scope: ChatScope
    ) -> ChatAccess:
        binding = await services.rooms.resolve_participation(current.session_digest)
        return cls(services, current, scope, binding)

    async def check_binding(self) -> None:
        binding = await self.services.rooms.resolve_participation(self.current.session_digest)
        if not self.watch.unchanged or binding != self.binding:
            raise ApiProblem(403, "CHAT_SCOPE_FORBIDDEN", "Chat access ended")
        if self.scope.kind is ChatScopeType.LOBBY:
            if binding is not None:
                raise ApiProblem(403, "CHAT_SCOPE_FORBIDDEN", "Lobby chat is unavailable in a Room")
        elif (
            binding is None
            or binding.room_id != self.scope.room_id
            or (binding.actor_type, binding.actor_id)
            != (self.current.actor_type, self.current.actor_id)
            or not binding.connected
            or self.generation is None
            or (
                binding.connection_generation is not None
                and binding.connection_generation != self.generation
            )
            or (
                binding.connection_generation is None
                and self.services.registry.connection_generation(
                    binding.room_id, binding.participant_id
                )
                != self.generation
            )
        ):
            raise ApiProblem(403, "CHAT_SCOPE_FORBIDDEN", "Current Room connection required")

    async def check(self) -> None:
        async with asyncio.timeout(SESSION_CHECK_TIMEOUT_SECONDS):
            await self.check_binding()
            if self.binding is not None:
                room = await self.services.rooms.get(self.binding.room_id)
                if (
                    room is None
                    or room.status is RoomStatus.CLOSED
                    or not any(
                        item.participant_id == self.binding.participant_id and item.connected
                        for item in room.participants
                    )
                ):
                    raise ApiProblem(
                        403, "CHAT_SCOPE_FORBIDDEN", "Current Room connection required"
                    )
            current = await self.services.identity.sessions.find(self.current.session_digest)
            if current is None:
                raise ApiProblem(401, "AUTH_REQUIRED", "Authentication required")
            if (current.actor_type, current.actor_id) != (
                self.current.actor_type,
                self.current.actor_id,
            ):
                raise ApiProblem(503, "CHAT_UNAVAILABLE", "Chat is unavailable")
            # Storage reads above may have yielded while a join/kick/logout or
            # connection replacement completed. Never trust the initial binding.
            await self.check_binding()
            if self.services.registry.shutting_down:
                raise ChatDeliveryUnavailable("CHAT_SHUTTING_DOWN")


async def _sender(services: ChatApiServices, current: SessionRecord) -> ChatSender:
    if current.actor_type is SessionActorType.GUEST:
        name = guest_display_name(current.actor_id)
    else:
        member = await services.identity.members.find_member(int(current.actor_id))
        if member is None:
            raise ApiProblem(401, "AUTH_REQUIRED", "Authentication required")
        name = member.nickname
    return ChatSender(current.actor_type, name)


def _scope(room_id: str | None) -> ChatScope:
    return ChatScope(ChatScopeType.LOBBY if room_id is None else ChatScopeType.ROOM, room_id)


def _rule_problem(error: ChatRuleViolation) -> ApiProblem:
    return ApiProblem(
        409 if error.code == "REQUEST_ID_REUSED" else 422,
        error.code,
        "Chat request rejected",
    )


def chat_router(services: ChatApiServices) -> APIRouter:
    router = APIRouter(tags=["chat"])

    async def send(
        payload: SendChatRequest,
        request: Request,
        response: Response,
        raw_session: str | None,
        csrf: str | None,
        room_id: str | None = None,
    ) -> ChatReceiptResponse:
        try:
            require_allowed_origin(services.identity.settings, request)
            current = await require_current_session(services.identity, raw_session)
            require_csrf(current, csrf)
            access = await ChatAccess.create(services, current, _scope(room_id))
            await access.check()
            sender = await _sender(services, current)
            command = SendChat(
                access.scope, sender, current.session_digest, str(payload.request_id), payload.text
            )
            await access.check()
            receipt: ChatReceipt = await services.delivery.publish(command)
        except ApiProblem:
            raise
        except ChatRuleViolation as error:
            raise _rule_problem(error) from None
        except Exception:
            raise ApiProblem(503, "CHAT_UNAVAILABLE", "Chat is unavailable") from None
        response.headers["Cache-Control"] = "no-store"
        return ChatReceiptResponse.model_validate(receipt)

    @router.post(
        "/api/v1/chat/lobby",
        response_model=ChatReceiptResponse,
        responses=room_problem_responses(401, 403, 409, 422, 503),
    )
    async def send_lobby(
        payload: SendChatRequest,
        request: Request,
        response: Response,
        session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
        csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    ) -> ChatReceiptResponse:
        return await send(payload, request, response, session_cookie, csrf_token)

    @router.post(
        "/api/v1/chat/rooms/{room_id}",
        response_model=ChatReceiptResponse,
        responses=room_problem_responses(401, 403, 409, 422, 503),
    )
    async def send_room(
        room_id: str,
        payload: SendChatRequest,
        request: Request,
        response: Response,
        session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
        csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    ) -> ChatReceiptResponse:
        return await send(payload, request, response, session_cookie, csrf_token, room_id)

    async def socket(websocket: WebSocket, room_id: str | None = None) -> None:
        subscription: ChatSubscription | None = None
        try:
            # No credentials or client-selected identity/scope in the query.
            if websocket.query_params:
                await websocket.close(code=4403)
                return
            current = await _websocket_session(websocket, services.identity)
            if current is None:
                return
            access = await ChatAccess.create(services, current, _scope(room_id))
            await access.check()
            subscription = await services.delivery.subscribe(access.scope)
            await access.check()
            await websocket.accept()
            await websocket.send_json(
                _envelope(
                    "chat.ready", access.scope, str(uuid4()), datetime.now(UTC).isoformat(), {}
                )
            )
            await _receive_chat(websocket, services, access, subscription)
        except ApiProblem as error:
            await _safe_close(
                websocket, 4401 if error.status == 401 else 4403 if error.status == 403 else 1011
            )
        except ChatRuleViolation:
            await _safe_close(websocket, 4403)
        except WebSocketDisconnect:
            pass
        except ChatSubscriptionClosed:
            await _safe_close(websocket, 1013)
        except Exception:
            await _safe_close(websocket, 1011)
        finally:
            if subscription is not None:
                with anyio.CancelScope(shield=True):
                    with suppress(Exception):
                        await subscription.close()

    @router.websocket("/ws/v1/chat/lobby")
    async def lobby_socket(websocket: WebSocket) -> None:
        await socket(websocket)

    @router.websocket("/ws/v1/chat/rooms/{room_id}")
    async def room_socket(websocket: WebSocket, room_id: str) -> None:
        await socket(websocket, room_id)

    return router


def _envelope(
    event_type: str, scope: ChatScope, event_id: str, occurred_at: str, payload: dict[str, object]
) -> dict[str, object]:
    # Transient messages have no Room/Game state version and no replay Snapshot.
    return {
        "event_type": event_type,
        "schema_version": 1,
        "event_id": event_id,
        "occurred_at": occurred_at.replace("+00:00", "Z"),
        "scope": scope.kind.value,
        "room_id": scope.room_id,
        "payload": payload,
    }


async def _receive_chat(
    websocket: WebSocket,
    services: ChatApiServices,
    access: ChatAccess,
    subscription: ChatSubscription,
) -> None:
    incoming = asyncio.create_task(websocket.receive())
    shutdown = asyncio.create_task(services.registry.wait_for_shutdown())
    message = asyncio.create_task(subscription.receive())
    try:
        while True:
            await asyncio.wait(
                {incoming, shutdown, message},
                timeout=SESSION_CHECK_INTERVAL_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if shutdown.done():
                await _safe_close(websocket, 1012)
                return
            if incoming.done():
                if incoming.result()["type"] != "websocket.disconnect":
                    await _safe_close(websocket, 1008)
                return
            await access.check()
            if shutdown.done() or incoming.done():
                continue
            if not message.done():
                continue
            received = message.result()
            if received.scope != access.scope:
                raise ChatDeliveryUnavailable("CHAT_SCOPE_MISMATCH")
            await websocket.send_json(
                _envelope(
                    "chat.message",
                    received.scope,
                    received.message_id,
                    received.occurred_at,
                    {
                        "actor_type": received.sender.actor_type.value,
                        "display_name": received.sender.display_name,
                        "text": received.text,
                    },
                )
            )
            message = asyncio.create_task(subscription.receive())
    finally:
        for task in (incoming, shutdown, message):
            if not task.done():
                task.cancel()
        with anyio.CancelScope(shield=True):
            await asyncio.gather(incoming, shutdown, message, return_exceptions=True)
