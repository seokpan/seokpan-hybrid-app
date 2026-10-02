"""Completed-result access and corruption checks, without real Providers."""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from seokpan.api.game import game_result_response
from seokpan.api.problems import _room_status
from seokpan.api.room import room_snapshot_response
from seokpan.game.application import (
    FinalizeGameCommand,
    GameApplicationService,
    GameParticipantRecord,
    OfficialMoveRecord,
    PersistenceRuleViolation,
    StartGameCommand,
)
from seokpan.game.domain import (
    Coordinate,
    EndReason,
    GameResult,
    GameStatus,
    MemberOutcome,
    RatingAdjustment,
    Stone,
)
from seokpan.identity.application import SessionActorType, SessionRecord, digest_opaque_token
from seokpan.persistence.memory import InMemoryGamePersistenceAdapter, ManualClock
from seokpan.room.application import RoomApplicationService, RoomParticipation, RoomRuntimeSnapshot
from seokpan.room.application.runtime import RoomRuntimeParticipant
from seokpan.room.domain import ActorType, RoomConfig, RoomRuleViolation, RoomStatus, Team
from seokpan.vote.application import VoteRuntimePort, VoteRuntimeSnapshot
from seokpan.vote.domain import TurnStatus

GAME = "00000000-0000-4000-8000-000000000001"
ROOM = "00000000-0000-4000-8000-000000000002"
BLACK = "00000000-0000-4000-8000-000000000003"
WHITE = "00000000-0000-4000-8000-000000000004"
NOW = datetime(2026, 9, 7, tzinfo=UTC)
SESSION = SessionRecord(
    "a" * 64,
    SessionActorType.MEMBER,
    "1",
    digest_opaque_token("test-csrf"),
    0,
    0,
    1000,
    csrf_token="test-csrf",
)


@pytest.mark.asyncio
async def test_room_result_reference_serialization_keeps_missing_identity_fallback() -> None:
    _, rooms, _, _ = await setup_result()
    api = SimpleNamespace(rooms=rooms)
    with pytest.raises(RoomRuleViolation, match="ROOM_NOT_FOUND"):
        await room_snapshot_response(api, None)
    rooms.participant_identity.return_value = None
    response = await room_snapshot_response(api, rooms.get.return_value)
    assert response.last_game_id == GAME
    assert [p.display_name for p in response.participants] == ["참가자", "참가자"]
    assert _room_status("ROOM_NOT_FOUND")[0] == 404
    assert _room_status("INVALID_ROOM_NAME")[0] == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("room_missing,roster_missing", [(True, False), (False, True)])
async def test_start_requires_complete_room_result(
    room_missing: bool, roster_missing: bool
) -> None:
    service, rooms, _, _ = await setup_result()

    async def start_game(**values: object) -> object:
        if values["expected_state_version"] == 9:
            raise RoomRuleViolation("REQUEST_ID_CONFLICT")
        return SimpleNamespace(
            snapshot=None if room_missing else rooms.get.return_value,
            start_roster=None if roster_missing else object(),
        )

    rooms.start_game = AsyncMock(side_effect=start_game)
    with pytest.raises(RoomRuleViolation, match="GAME_START_RESULT_INVALID"):
        await service.start_game(
            session=SESSION, room_id=ROOM, request_id="start", expected_state_version=8
        )
    with pytest.raises(RoomRuleViolation, match="REQUEST_ID_CONFLICT"):
        await service.start_game(
            session=SESSION, room_id=ROOM, request_id="start", expected_state_version=9
        )
    with pytest.raises(RoomRuleViolation, match="SESSION_NOT_IN_ROOM"):
        await service.start_game(
            session=SESSION, room_id=GAME, request_id="other", expected_state_version=8
        )


@pytest.mark.asyncio
async def test_start_does_not_write_game_when_room_disappeared() -> None:
    service, rooms, games, votes = await setup_result()
    rooms.start_game = AsyncMock(
        return_value=SimpleNamespace(snapshot=rooms.get.return_value, start_roster=object())
    )
    rooms.get.return_value = None
    games.start_game = AsyncMock()
    with pytest.raises(RoomRuleViolation, match="GAME_NOT_IN_CURRENT_ROOM"):
        await service.start_game(
            session=SESSION, room_id=ROOM, request_id="start", expected_state_version=8
        )
    games.start_game.assert_not_called()
    votes.initialize.assert_not_called()


@pytest.mark.asyncio
async def test_state_reference_points_to_result_after_finish() -> None:
    service, rooms, _, votes = await setup_result()
    assert await service.current_state_reference(SESSION) == (8, f"/api/v1/games/{GAME}/result")
    votes.get.return_value = SimpleNamespace(game_id=GAME, state_version=55)
    assert await service.current_state_reference(SESSION) == (8, f"/api/v1/games/{GAME}/result")
    rooms.get.return_value = replace(rooms.get.return_value, game_id=GAME, last_game_id=None)
    assert await service.current_state_reference(SESSION) == (55, f"/api/v1/games/{GAME}")
    rooms.get.return_value = replace(rooms.get.return_value, game_id=WHITE)
    assert await service.current_state_reference(SESSION) == (8, None)
    rooms.get.return_value = None
    assert await service.current_state_reference(SESSION) == (None, None)


async def setup_result() -> tuple[
    GameApplicationService, Mock, InMemoryGamePersistenceAdapter, Mock
]:
    games = InMemoryGamePersistenceAdapter({1: 1000, 2: 1000})
    await games.start_game(
        StartGameCommand(
            GAME,
            ROOM,
            5,
            NOW,
            (
                GameParticipantRecord(BLACK, Stone.BLACK, member_id=1),
                GameParticipantRecord(WHITE, Stone.WHITE, member_id=2),
            ),
        )
    )
    await games.finalize_game(
        FinalizeGameCommand(
            GameResult(
                GAME,
                GameStatus.FINISHED,
                EndReason.JOINT_LOSS,
                Stone.EMPTY,
                (),
                True,
                (
                    RatingAdjustment(BLACK, 1, Stone.BLACK, MemberOutcome.LOSS, 1000, -16, 984),
                    RatingAdjustment(WHITE, 2, Stone.WHITE, MemberOutcome.LOSS, 1000, -16, 984),
                ),
            ),
            NOW,
        )
    )
    rooms = Mock(spec=RoomApplicationService)
    rooms.participation.return_value = RoomParticipation(
        SESSION.session_digest,
        ROOM,
        BLACK,
        SESSION.actor_type,
        SESSION.actor_id,
    )
    rooms.resolve_participation = AsyncMock(
        side_effect=lambda session_digest: rooms.participation(session_digest)
    )
    rooms.resolve_participant_identity = AsyncMock(
        side_effect=lambda participant_id: rooms.participant_identity(participant_id)
    )
    rooms.get = AsyncMock(
        return_value=RoomRuntimeSnapshot(
            ROOM,
            RoomConfig(name="Result test"),
            RoomStatus.WAITING,
            BLACK,
            8,
            (
                RoomRuntimeParticipant(BLACK, ActorType.MEMBER, 1, True, Team.BLACK, False),
                RoomRuntimeParticipant(WHITE, ActorType.MEMBER, 2, True, Team.WHITE, False),
            ),
            last_game_id=GAME,
            last_game_turn_no=2,
        )
    )
    votes = Mock(spec=VoteRuntimePort)
    votes.get = AsyncMock(return_value=None)
    return (
        GameApplicationService(rooms=rooms, games=games, votes=votes, clock=ManualClock()),
        rooms,
        games,
        votes,
    )


@pytest.mark.asyncio
async def test_last_result_survives_runtime_replacement_and_never_reads_new_rating() -> None:
    service, rooms, games, votes = await setup_result()
    rooms.get.return_value = replace(
        rooms.get.return_value, game_id=WHITE, status=RoomStatus.PLAYING
    )
    games.member_ratings.update({1: 1200, 2: 1400})
    result = await service.get_result(session=SESSION, game_id=GAME)
    public = game_result_response(result).model_dump(mode="json")
    assert result.turn_no == 2 and result.board.move_no == 0
    assert public["my_rating"] == {
        "outcome": "LOSS",
        "rating_before": 1000,
        "rating_delta": -16,
        "rating_after": 984,
    }
    assert public["winning_line"] is None
    assert set(public["my_rating"]) == {"outcome", "rating_before", "rating_delta", "rating_after"}
    votes.get.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("viewer", ["guest", "member_spectator", "changed_identity"])
async def test_result_does_not_return_another_participants_rating(viewer: str) -> None:
    service, rooms, _, _ = await setup_result()
    session = (
        replace(SESSION, actor_type=SessionActorType.GUEST, actor_id="guest")
        if viewer == "guest"
        else SESSION
    )
    if viewer == "member_spectator":
        room = rooms.get.return_value
        rooms.get.return_value = replace(
            room,
            participants=(
                *room.participants,
                replace(
                    room.participants[0],
                    participant_id=ROOM,
                    team=Team.NONE,
                ),
            ),
        )
        rooms.participation.return_value = replace(
            rooms.participation.return_value, participant_id=ROOM
        )
    elif viewer == "changed_identity":
        session = replace(SESSION, actor_id="2")
    result = await service.get_result(session=session, game_id=GAME)
    assert game_result_response(result).my_rating is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("broken", "code"),
    [
        ("no_session_room", "SESSION_NOT_IN_ROOM"),
        ("room_missing", "ROOM_NOT_FOUND"),
        ("participant_missing", "SESSION_NOT_IN_ROOM"),
        ("game_missing", "GAME_NOT_FOUND"),
        ("not_last_or_current", "GAME_NOT_IN_CURRENT_ROOM"),
        ("active", "GAME_NOT_FINISHED"),
        ("missing_result", "GAME_RESULT_INCOMPLETE"),
        ("foreign_room", "GAME_RESULT_INCOMPLETE"),
        ("missing_final_turn", "GAME_RESULT_INCOMPLETE"),
        ("invalid_final_turn", "GAME_RESULT_INCOMPLETE"),
        ("broken_history", "GAME_RESULT_INCOMPLETE"),
        ("invalid_moves", "GAME_RESULT_HISTORY_MISMATCH"),
    ],
)
async def test_result_rejects_missing_unauthorized_or_incomplete_state(
    broken: str, code: str
) -> None:
    service, rooms, games, _ = await setup_result()
    room = rooms.get.return_value
    if broken == "no_session_room":
        rooms.participation.return_value = None
    elif broken == "room_missing":
        rooms.get.return_value = None
    elif broken == "participant_missing":
        rooms.get.return_value = replace(room, participants=())
    elif broken == "game_missing":
        games.games.clear()
    elif broken == "not_last_or_current":
        rooms.get.return_value = replace(room, last_game_id=WHITE)
    elif broken == "active":
        games.results.clear()
        rooms.get.return_value = replace(room, game_id=GAME, last_game_id=None)
    elif broken == "missing_result":
        games.results.clear()
    elif broken == "foreign_room":
        games.games[GAME] = replace(games.games[GAME], room_id=WHITE)
    elif broken == "missing_final_turn":
        rooms.get.return_value = replace(room, last_game_turn_no=None)
    elif broken == "invalid_final_turn":
        rooms.get.return_value = replace(room, last_game_turn_no=0)
    elif broken == "broken_history":
        games.results.clear()
        games.member_ratings.clear()
    else:
        await games.append_move(
            OfficialMoveRecord(
                GAME,
                1,
                2,
                Stone.BLACK,
                Coordinate.parse("A1"),
                1,
                1,
                NOW,
            )
        )
    with pytest.raises((RoomRuleViolation, PersistenceRuleViolation), match=code):
        await service.get_result(session=SESSION, game_id=GAME)


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_state", ["completed", "missing", "wrong_game", "active"])
async def test_result_during_room_completion_requires_finished_matching_runtime(
    runtime_state: str,
) -> None:
    service, rooms, _, votes = await setup_result()
    rooms.get.return_value = replace(
        rooms.get.return_value,
        game_id=GAME,
        last_game_id=None,
        last_game_turn_no=None,
        status=RoomStatus.PLAYING,
    )
    runtime = VoteRuntimeSnapshot(
        ROOM,
        GAME,
        4,
        2,
        TurnStatus.PASSED,
        Stone.WHITE,
        None,
        2,
        0,
        GameStatus.FINISHED,
        EndReason.JOINT_LOSS,
        (),
        (),
        (),
        (),
        (),
        None,
    )
    if runtime_state == "missing":
        votes.get.return_value = None
    elif runtime_state == "wrong_game":
        votes.get.return_value = replace(runtime, game_id=WHITE)
    elif runtime_state == "active":
        votes.get.return_value = replace(runtime, game_status=GameStatus.ACTIVE)
    else:
        votes.get.return_value = runtime
    if runtime_state == "completed":
        assert (await service.get_result(session=SESSION, game_id=GAME)).turn_no == 2
    else:
        with pytest.raises(PersistenceRuleViolation, match="GAME_RESULT_INCOMPLETE"):
            await service.get_result(session=SESSION, game_id=GAME)


@pytest.mark.asyncio
@pytest.mark.parametrize("extra_turn", [False, True])
async def test_normal_win_replays_board_and_requires_exact_final_turn(extra_turn: bool) -> None:
    service, rooms, games, _ = await setup_result()
    for move_no, coordinate in enumerate(
        ("A1", "A15", "B1", "C15", "C1", "E15", "D1", "G15", "E1"), 1
    ):
        await games.append_move(
            OfficialMoveRecord(
                GAME,
                move_no,
                move_no,
                Stone.BLACK if move_no % 2 else Stone.WHITE,
                Coordinate.parse(coordinate),
                1,
                1,
                NOW,
            )
        )
    rooms.get.return_value = replace(rooms.get.return_value, last_game_turn_no=9 + int(extra_turn))
    command = games.results[GAME]
    games.results[GAME] = replace(
        command,
        result=replace(
            command.result,
            end_reason=EndReason.BLACK_WIN,
            winner=Stone.BLACK,
            rating_adjustments=(
                replace(
                    command.result.rating_adjustments[0],
                    outcome=MemberOutcome.WIN,
                    rating_delta=16,
                    rating_after=1016,
                ),
                command.result.rating_adjustments[1],
            ),
        ),
    )
    if extra_turn:
        with pytest.raises(PersistenceRuleViolation, match="GAME_RESULT_HISTORY_MISMATCH"):
            await service.get_result(session=SESSION, game_id=GAME)
    else:
        result = game_result_response(await service.get_result(session=SESSION, game_id=GAME))
        assert result.winning_line == ["A1", "B1", "C1", "D1", "E1"]
        assert result.move_no == 9
        assert len(result.board) == 9
