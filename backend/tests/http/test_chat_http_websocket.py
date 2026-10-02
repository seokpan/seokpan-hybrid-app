from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid1, uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from seokpan.app import ApplicationServices, build_headless_services, create_app
from seokpan.chat import ChatDeliveryUnavailable
from seokpan.settings import Settings

ORIGIN = "http://localhost:5173"
LOBBY = "/api/v1/chat/lobby"
LOBBY_WS = "/ws/v1/chat/lobby"


@dataclass(frozen=True)
class Actor:
    headers: dict[str, str] = field(repr=False)
    display_name: str


@pytest.fixture
def setup() -> Iterator[tuple[TestClient, ApplicationServices]]:
    settings = Settings(environment="test", allowed_origins=(ORIGIN,))
    services = build_headless_services(settings)
    with TestClient(create_app(settings=settings, services=services), base_url=ORIGIN) as client:
        yield client, services


def actor(client: TestClient, name: str | None = None) -> Actor:
    client.cookies.clear()
    if name is None:
        response = client.post("/api/v1/sessions/guest", headers={"Origin": ORIGIN})
        assert response.status_code == 201
    else:
        registered = client.post(
            "/api/v1/members",
            headers={"Origin": ORIGIN},
            json={
                "login_id": name,
                "nickname": name,
                "password": "correct-pass",
            },
        )
        assert registered.status_code == 201, registered.text
        response = client.post(
            "/api/v1/sessions/member",
            headers={"Origin": ORIGIN},
            json={
                "login_id": name,
                "password": "correct-pass",
            },
        )
        assert response.status_code == 200
    headers = {
        "Origin": ORIGIN,
        "X-CSRF-Token": response.json()["csrf_token"],
        "Cookie": f"seokpan_session={client.cookies.get('seokpan_session')}",
    }
    client.cookies.clear()
    return Actor(headers, response.json()["display_name"])


def send(
    client: TestClient,
    who: Actor,
    text: str = "반갑습니다",
    *,
    path: str = LOBBY,
    request_id: str | None = None,
) -> Any:
    return client.post(
        path, headers=who.headers, json={"request_id": request_id or str(uuid4()), "text": text}
    )


def room(client: TestClient, owner: Actor) -> dict[str, Any]:
    response = client.post(
        "/api/v1/rooms",
        headers=owner.headers,
        json={
            "request_id": str(uuid4()),
            "name": "채팅방",
            "minimum_ready": 2,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def join(client: TestClient, who: Actor, target: dict[str, Any]) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/rooms/{target['room_id']}/joins",
        headers=who.headers,
        json={"request_id": str(uuid4()), "expected_state_version": target["state_version"]},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_lobby_member_guest_send_plain_text_and_retry_once(setup: Any) -> None:
    client, _services = setup
    member, guest = actor(client, "chatmember"), actor(client)
    assert send(client, member, "과거 대화").status_code == 200
    with client.websocket_connect(LOBBY_WS, headers=guest.headers) as ws:
        assert ws.receive_json()["event_type"] == "chat.ready"
        request_id = str(uuid4())
        first = send(client, member, "  <b>안녕</b> 😀  ", request_id=request_id)
        assert first.status_code == 200
        assert first.headers["cache-control"] == "no-store"
        event = ws.receive_json()
        assert event["event_id"] == first.json()["message_id"]
        assert event["payload"] == {
            "actor_type": "MEMBER",
            "display_name": "chatmember",
            "text": "<b>안녕</b> 😀",
        }
        assert event["scope"] == "LOBBY" and event["room_id"] is None
        assert set(event) == {
            "event_type",
            "schema_version",
            "event_id",
            "occurred_at",
            "scope",
            "room_id",
            "payload",
        }
        retried = send(client, member, "<b>안녕</b> 😀", request_id=request_id)
        assert retried.json() == {**first.json(), "replayed": True}
        conflict = send(client, member, "다른 내용", request_id=request_id)
        assert conflict.status_code == 409 and conflict.json()["code"] == "REQUEST_ID_REUSED"
        assert send(client, guest, "확인").status_code == 200
        # No duplicate was left in the queue before the second sender's message.
        assert ws.receive_json()["payload"] == {
            "actor_type": "GUEST",
            "display_name": guest.display_name,
            "text": "확인",
        }
        for secret in ("csrf", "digest", "login_id", "password", "session"):
            assert secret not in str(event)


@pytest.mark.parametrize(
    "missing,status", [("Cookie", 401), ("Origin", 403), ("X-CSRF-Token", 403)]
)
def test_send_requires_session_origin_and_csrf(setup: Any, missing: str, status: int) -> None:
    client, _ = setup
    who = actor(client)
    headers = {key: value for key, value in who.headers.items() if key != missing}
    response = client.post(
        LOBBY, headers=headers, json={"request_id": str(uuid4()), "text": "test"}
    )
    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "body",
    [
        {"text": ""},
        {"text": " \n "},
        {"text": "가" * 201},
        {"text": "😀" * 201},
        {"text": 123},
        {"text": "test", "sender": "fake"},
        {"text": "test", "room_id": str(uuid4())},
        {"text": "test", "request_id": str(uuid1())},
    ],
)
def test_invalid_body_cannot_select_sender_or_scope(setup: Any, body: dict[str, Any]) -> None:
    client, _ = setup
    who = actor(client)
    response = client.post(LOBBY, headers=who.headers, json={"request_id": str(uuid4()), **body})
    assert response.status_code == 422


def test_exact_unicode_limit_and_missing_history_endpoint(setup: Any) -> None:
    client, _ = setup
    who = actor(client)
    assert send(client, who, " 😀" + "😀" * 199 + " ").status_code == 200
    assert client.get(LOBBY, headers=who.headers).status_code == 405
    assert client.get(f"{LOBBY}/history", headers=who.headers).status_code == 404


def test_room_access_scope_and_chat_disconnect_preserve_game_connection(setup: Any) -> None:
    client, services = setup
    owner, guest, outsider = actor(client, "roomowner"), actor(client), actor(client)
    created = room(client, owner)
    joined = join(client, guest, created)
    room_id = joined["room_id"]
    path, socket_path = f"/api/v1/chat/rooms/{room_id}", f"/ws/v1/chat/rooms/{room_id}"
    assert send(client, guest, path=path).status_code == 403  # no game stream yet
    assert send(client, outsider, path=path).status_code == 403
    assert send(client, guest).status_code == 403  # cannot send to Lobby from a Room
    with client.websocket_connect(f"/ws/v1/rooms/{room_id}", headers=guest.headers) as game:
        before = game.receive_json()["payload"]["room"]
        with client.websocket_connect(socket_path, headers=guest.headers) as chat:
            assert chat.receive_json()["event_type"] == "chat.ready"
            assert send(client, guest, path=path).status_code == 200
            assert chat.receive_json()["room_id"] == room_id
            assert send(client, guest, path=f"/api/v1/chat/rooms/{uuid4()}").status_code == 403
        after = client.get(f"/api/v1/rooms/{room_id}/state", headers=guest.headers).json()["room"]
        assert before == after
        assert (
            services.realtime_api.registry.connection_generation(
                room_id,
                client.get("/api/v1/session", headers=guest.headers).json()["participant_id"],
            )
            is not None
        )


def test_room_chat_accepts_shared_generation_without_local_game_registry() -> None:
    settings = Settings(environment="test", allowed_origins=(ORIGIN,))
    services = build_headless_services(settings)
    assert services.room_api is not None
    assert services.realtime_api is not None
    assert services.chat_api is not None
    chat_registry = type(services.realtime_api.registry)()
    original_resolve = services.room_api.rooms.resolve_participation

    async def resolve_with_shared_generation(session_digest: str) -> Any:
        participation = await original_resolve(session_digest)
        if participation is None:
            return None
        generation = services.realtime_api.registry.connection_generation(
            participation.room_id, participation.participant_id
        )
        return replace(
            participation,
            connection_generation=generation,
            connected=generation is not None,
        )

    services = replace(services, chat_api=replace(services.chat_api, registry=chat_registry))
    with TestClient(create_app(settings=settings, services=services), base_url=ORIGIN) as client:
        owner, guest = actor(client, "crosspod"), actor(client)
        target = join(client, guest, room(client, owner))
        room_id = target["room_id"]
        with client.websocket_connect(f"/ws/v1/rooms/{room_id}", headers=guest.headers) as game:
            game.receive_json()
            services.room_api.rooms.resolve_participation = resolve_with_shared_generation  # type: ignore[method-assign]
            assert (
                chat_registry.connection_generation(
                    room_id,
                    client.get("/api/v1/session", headers=guest.headers).json()["participant_id"],
                )
                is None
            )
            with client.websocket_connect(
                f"/ws/v1/chat/rooms/{room_id}", headers=guest.headers
            ) as chat:
                assert chat.receive_json()["event_type"] == "chat.ready"
                assert send(client, guest, path=f"/api/v1/chat/rooms/{room_id}").status_code == 200


def test_lobby_socket_cannot_survive_a_brief_room_visit(setup: Any) -> None:
    client, _ = setup
    owner, observer = actor(client, "lobbyowner"), actor(client)
    with client.websocket_connect(LOBBY_WS, headers=owner.headers) as ws:
        ws.receive_json()
        created = room(client, owner)
        response = client.request(
            "DELETE",
            f"/api/v1/rooms/{created['room_id']}/participants/me",
            headers=owner.headers,
            json={"request_id": str(uuid4()), "expected_state_version": created["state_version"]},
        )
        assert response.status_code in (200, 204), response.text
        assert send(client, observer).status_code == 200
        with pytest.raises(WebSocketDisconnect) as ended:
            ws.receive_json()
        assert ended.value.code == 4403
    with client.websocket_connect(LOBBY_WS, headers=owner.headers) as renewed:
        assert renewed.receive_json()["event_type"] == "chat.ready"
        assert send(client, observer, "새 연결 이후").status_code == 200
        assert renewed.receive_json()["payload"]["text"] == "새 연결 이후"


def test_logout_ends_chat_before_another_message(setup: Any) -> None:
    client, _ = setup
    reader, sender = actor(client), actor(client)
    with client.websocket_connect(LOBBY_WS, headers=reader.headers) as ws:
        ws.receive_json()
        assert client.delete("/api/v1/session", headers=reader.headers).status_code == 204
        assert send(client, sender).status_code == 200
        with pytest.raises(WebSocketDisconnect) as ended:
            ws.receive_json()
        assert ended.value.code == 4401


@pytest.mark.parametrize("suffix", ["?token=x", "?actor_id=1"])
def test_socket_rejects_query_credentials_and_identity(setup: Any, suffix: str) -> None:
    client, _ = setup
    who = actor(client)
    with (
        pytest.raises(WebSocketDisconnect) as error,
        client.websocket_connect(LOBBY_WS + suffix, headers=who.headers),
    ):
        pass
    assert error.value.code == 4403


def test_socket_rejects_wrong_origin_and_client_commands(setup: Any) -> None:
    client, _ = setup
    who = actor(client)
    with (
        pytest.raises(WebSocketDisconnect) as error,
        client.websocket_connect(LOBBY_WS, headers={**who.headers, "Origin": "https://other"}),
    ):
        pass
    assert error.value.code == 4403
    with client.websocket_connect(LOBBY_WS, headers=who.headers) as ws:
        ws.receive_json()
        ws.send_json({"text": "not an HTTP command"})
        with pytest.raises(WebSocketDisconnect) as ended:
            ws.receive_json()
        assert ended.value.code == 1008


def test_delivery_failure_is_not_success_and_hides_internal_details() -> None:
    settings = Settings(environment="test", allowed_origins=(ORIGIN,))
    services = build_headless_services(settings)
    assert services.chat_api is not None
    delivery = AsyncMock()
    delivery.publish.side_effect = ChatDeliveryUnavailable("PRIVATE backend address")
    services = replace(services, chat_api=replace(services.chat_api, delivery=delivery))
    with TestClient(create_app(settings=settings, services=services), base_url=ORIGIN) as client:
        response = send(client, actor(client))
        assert response.status_code == 503
        assert response.json()["code"] == "CHAT_UNAVAILABLE"
        assert "PRIVATE" not in response.text


def test_kick_blocks_cached_send_and_further_receiving(setup: Any) -> None:
    client, _ = setup
    owner, guest = actor(client, "kickchat"), actor(client)
    joined = join(client, guest, room(client, owner))
    room_id = joined["room_id"]
    path = f"/api/v1/chat/rooms/{room_id}"
    guest_id = client.get("/api/v1/session", headers=guest.headers).json()["participant_id"]
    with (
        client.websocket_connect(f"/ws/v1/rooms/{room_id}", headers=owner.headers) as owner_game,
        client.websocket_connect(f"/ws/v1/rooms/{room_id}", headers=guest.headers) as guest_game,
    ):
        owner_game.receive_json()
        guest_game.receive_json()
        with client.websocket_connect(
            f"/ws/v1/chat/rooms/{room_id}", headers=guest.headers
        ) as chat:
            chat.receive_json()
            request_id = str(uuid4())
            assert send(client, guest, path=path, request_id=request_id).status_code == 200
            chat.receive_json()
            latest = client.get(f"/api/v1/rooms/{room_id}/state", headers=owner.headers).json()[
                "room"
            ]
            kicked = client.post(
                f"/api/v1/rooms/{room_id}/participants/{guest_id}/kick",
                headers=owner.headers,
                json={
                    "request_id": str(uuid4()),
                    "expected_state_version": latest["state_version"],
                },
            )
            assert kicked.status_code == 200
            assert send(client, guest, path=path, request_id=request_id).status_code == 403
            assert send(client, owner, "퇴장 이후 비공개 대화", path=path).status_code == 200
            with pytest.raises(WebSocketDisconnect) as ended:
                chat.receive_json()
            assert ended.value.code == 4403


def test_new_game_connection_invalidates_old_chat_without_history(setup: Any) -> None:
    client, _ = setup
    owner, guest = actor(client, "renewchat"), actor(client)
    target = join(client, guest, room(client, owner))
    room_id = target["room_id"]
    game_path, chat_path = f"/ws/v1/rooms/{room_id}", f"/ws/v1/chat/rooms/{room_id}"
    with client.websocket_connect(game_path, headers=guest.headers) as old_game:
        old_game.receive_json()
        with client.websocket_connect(chat_path, headers=guest.headers) as old_chat:
            old_chat.receive_json()
            with client.websocket_connect(game_path, headers=guest.headers) as new_game:
                new_game.receive_json()
                assert send(client, guest, path=f"/api/v1/chat/rooms/{room_id}").status_code == 200
                with pytest.raises(WebSocketDisconnect) as ended:
                    old_chat.receive_json()
                assert ended.value.code == 4403
                with client.websocket_connect(chat_path, headers=guest.headers) as new_chat:
                    assert new_chat.receive_json()["event_type"] == "chat.ready"
                    assert (
                        send(
                            client, guest, "새 대화", path=f"/api/v1/chat/rooms/{room_id}"
                        ).status_code
                        == 200
                    )
                    assert new_chat.receive_json()["payload"]["text"] == "새 대화"


def test_idle_session_expiry_and_shutdown_close_only_chat(setup: Any) -> None:
    client, services = setup
    who = actor(client)
    with client.websocket_connect(LOBBY_WS, headers=who.headers) as chat:
        chat.receive_json()
        services.headless_clock.advance(2 * 60 * 60 * 1000 + 1)
        with pytest.raises(WebSocketDisconnect) as ended:
            chat.receive_json()
        assert ended.value.code == 4401
    who = actor(client)
    with client.websocket_connect(LOBBY_WS, headers=who.headers) as chat:
        chat.receive_json()
        client.portal.call(services.realtime_api.registry.end_runtime)
        with pytest.raises(WebSocketDisconnect) as ended:
            chat.receive_json()
        assert ended.value.code == 1012


def test_sender_lookup_race_cannot_publish_after_room_entry(setup: Any, monkeypatch: Any) -> None:
    client, services = setup
    who = actor(client, "chatrace")
    original = services.identity_api.members.find_member

    async def lookup(member_id: int) -> Any:
        from seokpan.identity.application import digest_opaque_token
        from seokpan.room.domain import RoomConfig

        member = await original(member_id)
        digest = digest_opaque_token(who.headers["Cookie"].split("=", 1)[1])
        current = await services.identity_api.sessions.find(digest)
        await services.room_api.rooms.create_room(
            session=current,
            request_id=str(uuid4()),
            config=RoomConfig(name="race"),
            password=None,
        )
        return member

    monkeypatch.setattr(services.identity_api.members, "find_member", lookup)
    response = send(client, who)
    assert response.status_code == 403
    assert response.json()["code"] == "CHAT_SCOPE_FORBIDDEN"


def test_delayed_session_read_rechecks_kicked_binding(setup: Any, monkeypatch: Any) -> None:
    client, services = setup
    owner, guest = actor(client, "readrace"), actor(client)
    target = join(client, guest, room(client, owner))
    room_id = target["room_id"]
    with client.websocket_connect(f"/ws/v1/rooms/{room_id}", headers=guest.headers) as game:
        game.receive_json()
        original = services.identity_api.sessions.find
        kicked = [False]

        async def delayed(digest: str) -> Any:
            from seokpan.identity.application import digest_opaque_token

            current = await original(digest)
            if not kicked[0]:
                kicked[0] = True
                owner_record = await original(
                    digest_opaque_token(owner.headers["Cookie"].split("=", 1)[1])
                )
                snapshot = await services.room_api.rooms.get(room_id)
                participation = services.room_api.rooms.participation(digest)
                await services.room_api.rooms.kick_participant(
                    session=owner_record,
                    target_id=participation.participant_id,
                    request_id=str(uuid4()),
                    expected_state_version=snapshot.state_version,
                )
            return current

        # Keep the game stream's periodic check from being the race initiator:
        # this command runs immediately in the same ASGI loop.
        monkeypatch.setattr(services.identity_api.sessions, "find", delayed)
        response = send(client, guest, path=f"/api/v1/chat/rooms/{room_id}")
        assert response.status_code == 403


def test_playing_member_guest_and_spectator_chat_do_not_change_game(setup: Any) -> None:
    client, _ = setup
    owner, guest, spectator = actor(client, "playingchat"), actor(client), actor(client)
    target = join(client, guest, room(client, owner))
    room_id = target["room_id"]
    with (
        client.websocket_connect(f"/ws/v1/rooms/{room_id}", headers=owner.headers) as black,
        client.websocket_connect(f"/ws/v1/rooms/{room_id}", headers=guest.headers) as white,
    ):
        black.receive_json()
        white.receive_json()
        for who, team in ((owner, "BLACK"), (guest, "WHITE")):
            for action, value in (("team", {"team": team}), ("ready", {"ready": True})):
                version = client.get(f"/api/v1/rooms/{room_id}/state", headers=who.headers).json()[
                    "room"
                ]["state_version"]
                result = client.put(
                    f"/api/v1/rooms/{room_id}/participants/me/{action}",
                    headers=who.headers,
                    json={"request_id": str(uuid4()), "expected_state_version": version, **value},
                )
                assert result.status_code == 200, result.text
        state = client.get(f"/api/v1/rooms/{room_id}/state", headers=owner.headers).json()["room"]
        started = client.post(
            f"/api/v1/rooms/{room_id}/games",
            headers=owner.headers,
            json={"request_id": str(uuid4()), "expected_state_version": state["state_version"]},
        )
        assert started.status_code == 201, started.text
        state = client.get(f"/api/v1/rooms/{room_id}/state", headers=owner.headers).json()["room"]
        join(client, spectator, state)
        with client.websocket_connect(
            f"/ws/v1/rooms/{room_id}", headers=spectator.headers
        ) as watching:
            watching.receive_json()
            with client.websocket_connect(
                f"/ws/v1/chat/rooms/{room_id}", headers=spectator.headers
            ) as chat:
                chat.receive_json()
                before = client.get(f"/api/v1/rooms/{room_id}/state", headers=owner.headers).json()
                for who in (owner, guest, spectator):
                    assert (
                        send(client, who, path=f"/api/v1/chat/rooms/{room_id}").status_code == 200
                    )
                    assert chat.receive_json()["payload"]["display_name"] == who.display_name
                after = client.get(f"/api/v1/rooms/{room_id}/state", headers=owner.headers).json()
                assert before == after


@pytest.mark.parametrize(
    "failure,close_code", [("slow", 1013), ("wrong_scope", 1011), ("provider", 1011)]
)
def test_chat_delivery_failure_never_sends_wrong_data(
    setup: Any, monkeypatch: Any, failure: str, close_code: int
) -> None:
    from seokpan.chat import (
        ChatMessage,
        ChatScope,
        ChatScopeType,
        ChatSender,
        ChatSubscriptionClosed,
    )
    from seokpan.identity.application import SessionActorType

    client, services = setup
    who = actor(client)
    subscription = AsyncMock()
    if failure == "slow":
        subscription.receive.side_effect = ChatSubscriptionClosed("SLOW_CONSUMER")
    elif failure == "provider":
        subscription.receive.side_effect = RuntimeError("PRIVATE internal endpoint")
    else:
        subscription.receive.return_value = ChatMessage(
            str(uuid4()),
            "2026-09-08T00:00:00Z",
            ChatScope(ChatScopeType.ROOM, str(uuid4())),
            ChatSender(SessionActorType.MEMBER, "someone"),
            "PRIVATE other Room text",
        )
    monkeypatch.setattr(
        services.chat_api.delivery, "subscribe", AsyncMock(return_value=subscription)
    )
    with client.websocket_connect(LOBBY_WS, headers=who.headers) as chat:
        assert chat.receive_json()["event_type"] == "chat.ready"
        with pytest.raises(WebSocketDisconnect) as ended:
            chat.receive_json()
        assert ended.value.code == close_code
    subscription.close.assert_awaited_once()


def test_participation_watch_releases_without_retaining_session(setup: Any) -> None:
    import gc
    import weakref

    _client, services = setup
    watch = services.room_api.rooms.watch_participation("temporary-test-digest")
    assert watch is services.room_api.rooms.watch_participation("temporary-test-digest")
    reference = weakref.ref(watch)
    del watch
    gc.collect()
    assert reference() is None
