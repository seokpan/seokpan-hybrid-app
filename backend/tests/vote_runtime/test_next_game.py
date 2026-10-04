from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from seokpan.game.domain import Coordinate, Stone
from seokpan.persistence.memory import InMemoryVoteRuntimeAdapter, ManualClock
from seokpan.room.application import RoomRuntimeSnapshot
from seokpan.room.domain import RoomConfig, RoomStatus
from seokpan.vote.application import CastRuntimeVote, InitializeVoteRuntime
from seokpan.vote.domain import Voter, VoteRuleViolation

from .conftest import VoteRuntimeHarness


def initial() -> InitializeVoteRuntime:
    return InitializeVoteRuntime(
        "00000000-0000-4000-8000-000000000101",
        "init-1",
        "00000000-0000-4000-8000-000000000102",
        (Voter("black-1", Stone.BLACK), Voter("white-1", Stone.WHITE)),
        1000,
        1,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [False, True])
async def test_completed_runtime_can_be_replaced_without_old_request_replay(
    vote_harness: VoteRuntimeHarness,
    invalid: bool,
) -> None:
    first = initial()
    await vote_harness.adapter.initialize(first)
    vote = CastRuntimeVote(
        "00000000-0000-4000-8000-000000000101",
        "vote-1",
        "00000000-0000-4000-8000-000000000102",
        1,
        "black-1",
        Coordinate.parse("A1"),
        2,
    )
    await vote_harness.adapter.cast_vote(vote)
    # Seed a completed Provider state; the full HTTP E2E covers actual finalization.
    game = vote_harness.observable._states["00000000-0000-4000-8000-000000000101"].game.game
    if invalid:
        game.finish_system_invalid()
    else:
        game.finish_joint_loss()
    second = replace(
        first,
        game_id="00000000-0000-4000-8000-000000000103",
        request_id="init-2",
        previous_game_id="00000000-0000-4000-8000-000000000102",
        previous_turn_no=1,
    )
    result = await vote_harness.adapter.initialize(second)
    assert result.snapshot.game_id == "00000000-0000-4000-8000-000000000103"
    assert result.snapshot.turn_no == 1
    assert result.snapshot.move_no == 0
    assert result.snapshot.last_move is None
    assert result.snapshot.votes == result.snapshot.tally == result.snapshot.occupied_cells == ()
    assert result.snapshot.resolver is None
    assert (await vote_harness.adapter.initialize(second)).replayed
    with pytest.raises(VoteRuleViolation, match="STALE_GAME"):
        await vote_harness.adapter.initialize(first)
    with pytest.raises(VoteRuleViolation, match="STALE_GAME"):
        await vote_harness.adapter.cast_vote(vote)
    assert await vote_harness.adapter.get("00000000-0000-4000-8000-000000000101") == result.snapshot


@pytest.mark.asyncio
async def test_active_runtime_is_never_replaced(vote_harness: VoteRuntimeHarness) -> None:
    first = initial()
    before = await vote_harness.adapter.initialize(first)
    with pytest.raises(VoteRuleViolation, match="GAME_RUNTIME_ALREADY_EXISTS"):
        await vote_harness.adapter.initialize(
            replace(first, request_id="init-2", game_id="00000000-0000-4000-8000-000000000103")
        )
    assert await vote_harness.adapter.get("00000000-0000-4000-8000-000000000101") == before.snapshot


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"deadline_ms": -1}, "INVALID_DEADLINE"),
        ({"previous_game_id": "other"}, "INVALID_PREVIOUS_GAME"),
        ({"previous_turn_no": 1}, "INVALID_PREVIOUS_GAME"),
        ({"previous_game_id": "!", "previous_turn_no": 1}, "INVALID_PREVIOUS_GAME"),
        (
            {"previous_game_id": "00000000-0000-4000-8000-000000000102", "previous_turn_no": 1},
            "INVALID_PREVIOUS_GAME",
        ),
        ({"previous_game_id": "other", "previous_turn_no": 0}, "INVALID_TURN_NUMBER"),
    ],
)
def test_previous_game_reference_is_a_valid_pair(changes: dict[str, object], code: str) -> None:
    with pytest.raises(VoteRuleViolation, match=code):
        replace(initial(), **changes)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "previous_id,previous_turn",
    [(None, None), ("other", 1), ("00000000-0000-4000-8000-000000000102", 2)],
)
async def test_replacement_rejects_unmatched_previous_game(
    vote_harness: VoteRuntimeHarness,
    previous_id: str | None,
    previous_turn: int | None,
) -> None:
    await vote_harness.adapter.initialize(initial())
    vote_harness.observable._states[
        "00000000-0000-4000-8000-000000000101"
    ].game.game.finish_system_invalid()
    before = await vote_harness.adapter.get("00000000-0000-4000-8000-000000000101")
    with pytest.raises(VoteRuleViolation, match="STALE_GAME"):
        await vote_harness.adapter.initialize(
            replace(
                initial(),
                game_id="00000000-0000-4000-8000-000000000103",
                request_id="init-2",
                previous_game_id=previous_id,
                previous_turn_no=previous_turn,
            )
        )
    assert await vote_harness.adapter.get("00000000-0000-4000-8000-000000000101") == before


@pytest.mark.asyncio
async def test_missing_previous_runtime_and_wrong_initial_version_fail_closed(
    vote_harness: VoteRuntimeHarness,
) -> None:
    with pytest.raises(VoteRuleViolation, match="GAME_RUNTIME_NOT_FOUND"):
        await vote_harness.adapter.initialize(
            replace(initial(), previous_game_id="other", previous_turn_no=1)
        )
    with pytest.raises(VoteRuleViolation, match="STATE_VERSION_CONFLICT"):
        await vote_harness.adapter.initialize(replace(initial(), expected_state_version=2))
    assert await vote_harness.adapter.get("00000000-0000-4000-8000-000000000101") is None


@pytest.mark.asyncio
async def test_cached_request_cannot_restore_missing_runtime(
    vote_harness: VoteRuntimeHarness,
) -> None:
    await vote_harness.adapter.initialize(initial())
    vote_harness.observable._states.clear()
    with pytest.raises(VoteRuleViolation, match="STALE_GAME"):
        await vote_harness.adapter.initialize(initial())


@pytest.mark.asyncio
async def test_room_reference_is_checked_before_initializing_or_replaying() -> None:
    room = RoomRuntimeSnapshot(
        "00000000-0000-4000-8000-000000000101",
        RoomConfig(name="guard"),
        RoomStatus.PLAYING,
        "black-1",
        2,
        (),
        game_id="00000000-0000-4000-8000-000000000102",
    )
    lookup = AsyncMock(return_value=room)
    votes = InMemoryVoteRuntimeAdapter(ManualClock(), room_lookup=lookup)
    await votes.initialize(initial())
    assert (await votes.initialize(initial())).replayed
    for mismatch in (
        None,
        replace(room, status=RoomStatus.WAITING),
        replace(room, game_id="00000000-0000-4000-8000-000000000103"),
        replace(room, last_game_id="other"),
        replace(room, last_game_turn_no=1),
    ):
        lookup.return_value = mismatch
        with pytest.raises(VoteRuleViolation, match="ROOM_NOT_FOUND|GAME_NOT_IN_CURRENT_ROOM"):
            await votes.initialize(initial())
