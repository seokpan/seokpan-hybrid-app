"""F09 safety regressions at application boundaries; not real Provider evidence."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from seokpan.game.application.persistence import PersistenceOutcome, PersistenceRuleViolation
from seokpan.game.application.service import GameApplicationService
from seokpan.game.domain import EndReason, GameParticipantRole, Stone
from seokpan.identity.application import SessionActorType
from seokpan.room.domain import RoomRuleViolation, RoomStatus, Team
from seokpan.vote.domain import VoteRuleViolation

ROOM_ID = "00000000-0000-4000-8000-000000000001"
GAME_ID = "00000000-0000-4000-8000-000000000002"
BLACK_ID = "00000000-0000-4000-8000-000000000003"
WHITE_ID = "00000000-0000-4000-8000-000000000004"


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    black = SimpleNamespace(participant_id=BLACK_ID, team=Team.BLACK, ready=True, connected=True)
    white = SimpleNamespace(participant_id=WHITE_ID, team=Team.WHITE, ready=True, connected=True)
    room = SimpleNamespace(
        room_id=ROOM_ID,
        game_id=GAME_ID,
        status=RoomStatus.PLAYING,
        owner_id=BLACK_ID,
        state_version=7,
        config=SimpleNamespace(vote_seconds=15, minimum_ready=2),
        participants=(black, white),
        last_game_id=None,
        last_game_turn_no=None,
    )
    roster = tuple(
        SimpleNamespace(
            participant_id=participant_id,
            team=team,
            role=GameParticipantRole.PLAYER,
            member_id=member_id,
            guest_label=None,
        )
        for participant_id, team, member_id in (
            (BLACK_ID, Stone.BLACK, 1),
            (WHITE_ID, Stone.WHITE, 2),
        )
    )
    history = SimpleNamespace(
        start=SimpleNamespace(
            game_id=GAME_ID,
            room_id=ROOM_ID,
            voting_time_seconds=15,
            participants=roster,
        ),
        participants=roster,
        moves=(),
    )
    rooms = Mock()
    rooms.resolve_participation = AsyncMock(
        return_value=SimpleNamespace(room_id=ROOM_ID, participant_id=BLACK_ID)
    )
    rooms.start_game = AsyncMock(side_effect=RoomRuleViolation("ROOM_NOT_WAITING"))
    rooms.get = AsyncMock(return_value=room)
    rooms.resolve_participant_identity = AsyncMock(
        side_effect=lambda participant_id: SimpleNamespace(
            actor_type=SessionActorType.MEMBER,
            actor_id="1" if participant_id == BLACK_ID else "2",
        )
    )
    games = Mock()
    games.load_game = AsyncMock(return_value=history)
    games.load_result = AsyncMock(return_value=None)
    games.start_game = AsyncMock(return_value=PersistenceOutcome.CREATED)
    runtime = SimpleNamespace(game_id=GAME_ID)
    votes = Mock()
    votes.get = AsyncMock(return_value=None)
    votes.initialize = AsyncMock(return_value=SimpleNamespace(snapshot=runtime, replayed=False))
    service = GameApplicationService(
        rooms=rooms,
        games=games,
        votes=votes,
        clock=SimpleNamespace(now_ms=4_000),
        events=Mock(),
    )
    emitted = AsyncMock()
    monkeypatch.setattr(service, "_game_started", emitted)
    return SimpleNamespace(
        service=service,
        rooms=rooms,
        games=games,
        votes=votes,
        room=room,
        history=history,
        runtime=runtime,
        emitted=emitted,
    )


async def recover(harness: SimpleNamespace) -> object:
    return await harness.service.start_game(
        session=SimpleNamespace(session_digest="a" * 64),
        room_id=ROOM_ID,
        request_id="new-recovery-request",
        expected_state_version=7,
    )


def no_initialize(harness: SimpleNamespace) -> None:
    harness.votes.initialize.assert_not_awaited()
    harness.emitted.assert_not_awaited()


@pytest.mark.asyncio
async def test_persisted_start_without_progress_can_initialize_missing_vote(
    harness: SimpleNamespace,
) -> None:
    result = await recover(harness)
    assert result.replayed is True
    assert result.game is harness.runtime
    harness.games.start_game.assert_not_awaited()
    harness.votes.initialize.assert_awaited_once()
    command = harness.votes.initialize.await_args.args[0]
    assert command.game_id == GAME_ID
    assert command.deadline_ms == 19_000
    assert tuple(item.participant_id for item in command.participants) == (BLACK_ID, WHITE_ID)
    harness.emitted.assert_awaited_once()


@pytest.mark.asyncio
async def test_durable_moves_never_restart_as_an_empty_first_turn(harness: SimpleNamespace) -> None:
    harness.history.moves = (SimpleNamespace(move_no=1, turn_no=1),)
    with pytest.raises(RoomRuleViolation, match="GAME_START_RECOVERY_REQUIRED"):
        await recover(harness)
    no_initialize(harness)
    harness.games.start_game.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", list(EndReason))
async def test_any_stored_result_blocks_startup_reinitialization(
    harness: SimpleNamespace, reason: EndReason
) -> None:
    harness.games.load_result.return_value = SimpleNamespace(end_reason=reason)
    with pytest.raises(RoomRuleViolation, match="GAME_START_RECOVERY_REQUIRED"):
        await recover(harness)
    no_initialize(harness)
    harness.games.start_game.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("game_id", "another-game"),
        ("room_id", "another-room"),
        ("voting_time_seconds", 30),
    ],
)
async def test_persistent_start_identity_and_config_must_match(
    harness: SimpleNamespace, field: str, value: object
) -> None:
    setattr(harness.history.start, field, value)
    with pytest.raises(PersistenceRuleViolation, match="GAME_START_CONFLICT"):
        await recover(harness)
    no_initialize(harness)


@pytest.mark.asyncio
async def test_departed_player_is_not_silently_recreated_or_a_stopiteration(
    harness: SimpleNamespace,
) -> None:
    harness.room.participants = harness.room.participants[:1]
    with pytest.raises(RoomRuleViolation, match="GAME_START_RECOVERY_REQUIRED"):
        await recover(harness)
    no_initialize(harness)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing", "closed", "waiting", "new_game"])
async def test_room_transition_during_provider_reads_blocks_initialization(
    harness: SimpleNamespace, change: str
) -> None:
    latest = SimpleNamespace(**vars(harness.room))
    if change == "missing":
        latest = None
    elif change == "closed":
        latest.status = RoomStatus.CLOSED
    elif change == "waiting":
        latest.status = RoomStatus.WAITING
    else:
        latest.game_id = "another-game"
    harness.rooms.get.side_effect = [harness.room, latest]
    with pytest.raises(RoomRuleViolation, match="GAME_NOT_IN_CURRENT_ROOM"):
        await recover(harness)
    no_initialize(harness)


@pytest.mark.asyncio
async def test_room_version_change_during_provider_reads_requires_resynchronization(
    harness: SimpleNamespace,
) -> None:
    latest = SimpleNamespace(**vars(harness.room))
    latest.state_version = 8
    harness.rooms.get.side_effect = [harness.room, latest]
    with pytest.raises(RoomRuleViolation, match="STATE_VERSION_CONFLICT"):
        await recover(harness)
    no_initialize(harness)


@pytest.mark.asyncio
async def test_corrupt_result_does_not_fall_back_to_new_runtime(harness: SimpleNamespace) -> None:
    harness.games.load_result.side_effect = PersistenceRuleViolation("GAME_RESULT_INCOMPLETE")
    with pytest.raises(PersistenceRuleViolation, match="GAME_RESULT_INCOMPLETE"):
        await recover(harness)
    no_initialize(harness)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("provider unavailable"), asyncio.CancelledError()])
async def test_result_read_failure_and_cancellation_propagate_without_initializing(
    harness: SimpleNamespace, error: BaseException
) -> None:
    harness.games.load_result.side_effect = error
    with pytest.raises(type(error)) as caught:
        await recover(harness)
    assert caught.value is error
    no_initialize(harness)


@pytest.mark.asyncio
async def test_normal_existing_runtime_is_not_reinitialized(harness: SimpleNamespace) -> None:
    harness.votes.get.return_value = harness.runtime
    with pytest.raises(RoomRuleViolation, match="ROOM_NOT_WAITING"):
        await recover(harness)
    no_initialize(harness)


@pytest.mark.asyncio
async def test_concurrent_vote_winner_is_reused_without_duplicate_started_event(
    harness: SimpleNamespace,
) -> None:
    harness.votes.get.side_effect = [None, harness.runtime]
    harness.votes.initialize.side_effect = VoteRuleViolation("GAME_RUNTIME_ALREADY_EXISTS")
    result = await recover(harness)
    assert result.game is harness.runtime
    harness.emitted.assert_not_awaited()


@pytest.mark.asyncio
async def test_wrong_concurrent_game_is_not_accepted(harness: SimpleNamespace) -> None:
    harness.votes.get.side_effect = [None, SimpleNamespace(game_id="another-game")]
    harness.votes.initialize.side_effect = VoteRuleViolation("GAME_RUNTIME_ALREADY_EXISTS")
    with pytest.raises(RoomRuleViolation, match="GAME_START_RECOVERY_REQUIRED"):
        await recover(harness)
    harness.emitted.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["member", "guest", "played", "unchanged"])
async def test_persistence_race_validates_identity_and_progress_not_only_team(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    intended = tuple(SimpleNamespace(**vars(item)) for item in harness.history.start.participants)
    monkeypatch.setattr(
        harness.service,
        "_persistence_participant",
        AsyncMock(
            side_effect=lambda participant_id, _team: next(
                item for item in intended if item.participant_id == participant_id
            )
        ),
    )
    if change == "member":
        harness.history.start.participants[0].member_id = 999
    elif change == "guest":
        harness.history.start.participants[0].member_id = None
        harness.history.start.participants[0].guest_label = "Guest-9999"
    elif change == "played":
        harness.history.moves = (SimpleNamespace(move_no=1, turn_no=1),)
    harness.games.load_game.side_effect = [None, harness.history]
    harness.games.start_game.side_effect = PersistenceRuleViolation("GAME_START_CONFLICT")
    if change == "unchanged":
        result = await recover(harness)
        assert result.game is harness.runtime
        harness.votes.initialize.assert_awaited_once()
    else:
        error_type = RoomRuleViolation if change == "played" else PersistenceRuleViolation
        code = "GAME_START_RECOVERY_REQUIRED" if change == "played" else "GAME_START_CONFLICT"
        with pytest.raises(error_type, match=code):
            await recover(harness)
        no_initialize(harness)
    harness.games.start_game.assert_awaited_once()
