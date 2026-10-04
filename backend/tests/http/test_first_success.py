"""Issue #53: HTTP + WebSocket + the composed Runner, with Fake Providers only."""

from uuid import uuid4

from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession
from test_game_http import ORIGIN, _guest, _member, _mutation, _ready_room

from seokpan.app import build_headless_services, create_app
from seokpan.game.application import DueTurn, TurnProcessingStatus
from seokpan.settings import Settings


def _headers(client: TestClient) -> dict[str, str]:
    return {"Origin": ORIGIN, "Cookie": f"seokpan_session={client.cookies['seokpan_session']}"}


def _until(socket: WebSocketTestSession, event_type: str) -> list[dict[str, object]]:
    events = []
    for _ in range(30):
        event = socket.receive_json()
        events.append(event)
        if event["event_type"] == event_type:
            return events
    raise AssertionError(f"Missing {event_type}: {events}")


def test_first_success_normal_win_result_reconnect_and_second_game() -> None:
    settings = Settings(environment="test", allowed_origins=(ORIGIN,))
    services = build_headless_services(settings)
    app = create_app(settings=settings, services=services)
    clock, runner, due = (
        services.headless_clock,
        services.turn_resolution,
        services.headless_due_turns,
    )
    assert clock is not None and runner is not None and due is not None
    assert services.realtime_api is not None
    with (
        TestClient(app, base_url=ORIGIN) as black,
        TestClient(app, base_url=ORIGIN) as white,
        TestClient(app, base_url=ORIGIN) as guest,
    ):
        black_csrf, white_csrf = _member(black, "e2eb"), _member(white, "e2ew")
        guest_csrf = _guest(guest)
        room = _ready_room(black, black_csrf, white, white_csrf)
        room_id = str(room["room_id"])
        joined = guest.post(
            f"/api/v1/rooms/{room_id}/joins",
            headers={
                "Origin": ORIGIN,
                "X-CSRF-Token": guest_csrf,
            },
            json={"request_id": str(uuid4()), "expected_state_version": room["state_version"]},
        )
        assert joined.status_code == 201, joined.text
        room = joined.json()
        socket_path = f"/ws/v1/rooms/{room_id}"
        with black.websocket_connect(socket_path, headers=_headers(white)) as white_ws:
            assert white_ws.receive_json()["event_type"] == "room.snapshot"
            with black.websocket_connect(socket_path, headers=_headers(black)) as black_ws:
                initial_snapshot = black_ws.receive_json()
                black_id = initial_snapshot["payload"]["room"]["owner_id"]
                room = black.get(f"/api/v1/rooms/{room_id}/snapshot").json()
                start_body = {
                    "request_id": str(uuid4()),
                    "expected_state_version": room["state_version"],
                }
                started = black.post(
                    f"/api/v1/rooms/{room_id}/games",
                    headers={
                        "Origin": ORIGIN,
                        "X-CSRF-Token": black_csrf,
                    },
                    json=start_body,
                )
                assert started.status_code == 201, started.text
                game_id = started.json()["game_id"]
                _until(white_ws, "game.started")
                game = black.get(f"/api/v1/games/{game_id}").json()
                voted = _mutation(
                    black,
                    black_csrf,
                    "PUT",
                    f"/api/v1/games/{game_id}/turns/1/vote",
                    game["state_version"],
                    coordinate="H8",
                )
                assert voted["my_vote"] == "H8"
                _until(white_ws, "vote.tally_changed")
            _until(white_ws, "vote.tally_changed")
            disconnected = white.get(f"/api/v1/rooms/{room_id}/snapshot").json()
            assert disconnected["owner_id"] == black_id
            disconnected_owner = next(
                p for p in disconnected["participants"] if p["participant_id"] == black_id
            )
            assert disconnected_owner["connected"] is False

            clock.advance(1000)
            with black.websocket_connect(socket_path, headers=_headers(black)) as reconnected:
                restored = reconnected.receive_json()
                assert restored["event_type"] == "room.snapshot"
                participant = next(
                    p
                    for p in restored["payload"]["room"]["participants"]
                    if p["participant_id"] == black_id
                )
                assert participant["team"] == "BLACK"
                assert participant["connected"] is True
                assert restored["payload"]["game"]["can_vote"] is True
                assert restored["payload"]["game"]["my_vote"] is None
                assert restored["payload"]["room"]["owner_id"] == black_id
                game = black.get(f"/api/v1/games/{game_id}").json()
                assert game["my_vote"] is None and game["vote_aggregation"] == []
                assert black.portal is not None
                all_events = []
                # t1 BLACK Pass; t2 WHITE Move; then BLACK's five stones with WHITE Passes.
                for turn in range(1, 12):
                    if turn == 2 or (turn >= 3 and turn % 2 == 1):
                        actor, csrf = (white, white_csrf) if turn == 2 else (black, black_csrf)
                        coordinate = "O15" if turn == 2 else f"{chr(ord('A') + (turn - 3) // 2)}1"
                        game = actor.get(f"/api/v1/games/{game_id}").json()
                        _mutation(
                            actor,
                            csrf,
                            "PUT",
                            f"/api/v1/games/{game_id}/turns/{turn}/vote",
                            game["state_version"],
                            coordinate=coordinate,
                        )
                    due.values = (DueTurn(room_id, game_id, turn),)
                    clock.advance(turn * 15000 - clock.now_ms)
                    processed = black.portal.call(runner.run_once)[0]
                    expected = (
                        TurnProcessingStatus.GAME_ENDED
                        if turn == 11
                        else TurnProcessingStatus.MOVE
                        if turn == 2 or turn % 2 == 1 and turn > 1
                        else TurnProcessingStatus.PASS
                    )
                    assert processed.status is expected
                    event_type = (
                        "game.finished"
                        if turn == 11
                        else (
                            "game.move_applied"
                            if expected is TurnProcessingStatus.MOVE
                            else "turn.passed"
                        )
                    )
                    all_events.extend(_until(white_ws, event_type))
                    if turn in (1, 2):
                        game = black.get(f"/api/v1/games/{game_id}").json()
                        assert game["turn_no"] == turn + 1
                        assert game["move_no"] == turn - 1
                        assert game["last_move"] == (
                            None
                            if turn == 1
                            else {"move_no": 1, "team": "WHITE", "coordinate": "O15"}
                        )
                    if turn == 4:
                        assert black.get(f"/api/v1/games/{game_id}").json()["last_move"] == {
                            "move_no": 2,
                            "team": "BLACK",
                            "coordinate": "A1",
                        }
                versions = [e["state_version"] for e in all_events]
                assert versions == sorted(set(versions))
                assert [e["event_type"] for e in all_events].count("game.finished") == 1
                waiting = black.get(f"/api/v1/rooms/{room_id}/snapshot").json()
                assert waiting["status"] == "WAITING"
                assert waiting["last_game_id"] == game_id
                assert not any(p["ready"] for p in waiting["participants"])
                black_result = black.get(f"/api/v1/games/{game_id}/result").json()
                white_result = white.get(f"/api/v1/games/{game_id}/result").json()
                assert black_result["winner"] == "BLACK"
                assert black_result["turn_no"] == 11 and black_result["move_no"] == 6
                assert len(black_result["board"]) == 6
                assert black_result["winning_line"] == ["A1", "B1", "C1", "D1", "E1"]
                assert black_result["my_rating"]["rating_after"] == 1016
                assert white_result["my_rating"]["rating_after"] == 984
                assert services.game_api is not None
                persistence = services.game_api.games._games
                history = black.portal.call(persistence.load_game, game_id)
                assert history is not None
                assert [move.turn_no for move in history.moves] == [2, 3, 5, 7, 9, 11]
                assert [move.move_no for move in history.moves] == [1, 2, 3, 4, 5, 6]
                # Guest missed every live event; connecting after finish still locates Result.
                with black.websocket_connect(socket_path, headers=_headers(guest)) as guest_ws:
                    recovered = guest_ws.receive_json()
                    assert recovered["payload"]["room"]["last_game_id"] == game_id
                    guest_result = guest.get(f"/api/v1/games/{game_id}/result").json()
                    assert guest_result == {**black_result, "my_rating": None}
                    before_events = services.realtime_api.events.room_version(room_id)
                    assert (
                        black.portal.call(runner.run_once)[0].status
                        is TurnProcessingStatus.GAME_ENDED
                    )
                    assert services.realtime_api.events.room_version(room_id) == before_events
                    assert black.get(f"/api/v1/games/{game_id}/result").json() == black_result
                    assert black.portal.call(persistence.load_game, game_id) == history
                    for member_id, rating in ((1, 1016), (2, 984)):
                        member = black.portal.call(
                            services.identity_api.members.find_member, member_id
                        )
                        assert member is not None and member.rating == rating
                    for actor, csrf in ((black, black_csrf), (white, white_csrf)):
                        room = actor.get(f"/api/v1/rooms/{room_id}/snapshot").json()
                        room = _mutation(
                            actor,
                            csrf,
                            "PUT",
                            f"/api/v1/rooms/{room_id}/participants/me/ready",
                            room["state_version"],
                            ready=True,
                        )
                    second_body = {
                        "request_id": str(uuid4()),
                        "expected_state_version": room["state_version"],
                    }
                    second = black.post(
                        f"/api/v1/rooms/{room_id}/games",
                        headers={
                            "Origin": ORIGIN,
                            "X-CSRF-Token": black_csrf,
                        },
                        json=second_body,
                    )
                    assert second.status_code == 201, second.text
                    fresh = second.json()
                    repeated_start = black.post(
                        f"/api/v1/rooms/{room_id}/games",
                        headers={"Origin": ORIGIN, "X-CSRF-Token": black_csrf},
                        json=second_body,
                    )
                    assert repeated_start.status_code == 201, repeated_start.text
                    assert repeated_start.json()["game_id"] == fresh["game_id"]
                    assert repeated_start.json()["replayed"] is True
                    assert fresh["game_id"] != game_id and fresh["game_status"] == "ACTIVE"
                    assert fresh["move_no"] == 0 and fresh["turn_no"] == 1 and fresh["board"] == []
                    assert fresh["last_move"] is None
                    assert black_result["last_move"] == {
                        "move_no": 6,
                        "team": "BLACK",
                        "coordinate": "E1",
                    }
                    assert fresh["vote_aggregation"] == []
                    assert black.get(f"/api/v1/games/{game_id}/result").json() == black_result
                    assert black.portal.call(persistence.load_game, game_id) == history
                    stale = black.post(
                        f"/api/v1/rooms/{room_id}/games",
                        headers={
                            "Origin": ORIGIN,
                            "X-CSRF-Token": black_csrf,
                        },
                        json=start_body,
                    )
                    assert stale.status_code == 403
                    assert stale.json()["code"] == "GAME_NOT_IN_CURRENT_ROOM"
                    assert (
                        black.portal.call(runner.run_once)[0].status is TurnProcessingStatus.STALE
                    )
                    assert white.get(f"/api/v1/games/{fresh['game_id']}").json()["move_no"] == 0
