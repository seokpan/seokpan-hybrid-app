"""The Fake must enforce the same turn/move identity as durable history."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from seokpan.game.application import (
    GameParticipantRecord,
    OfficialMoveRecord,
    PersistenceOutcome,
    PersistenceRuleViolation,
    StartGameCommand,
)
from seokpan.game.domain import Coordinate, Stone
from seokpan.persistence.memory import InMemoryGamePersistenceAdapter

GAME_ID = "11111111-1111-4111-8111-111111111111"
OTHER_GAME_ID = "22222222-2222-4222-8222-222222222222"
ROOM_ID = "33333333-3333-4333-8333-333333333333"
PLAYER_ID = "44444444-4444-4444-8444-444444444444"
NOW = datetime(2026, 9, 7, tzinfo=UTC)


def start() -> StartGameCommand:
    return StartGameCommand(
        GAME_ID,
        ROOM_ID,
        5,
        NOW,
        (GameParticipantRecord(PLAYER_ID, Stone.WHITE, guest_label="Guest-0001"),),
    )


def after_pass() -> OfficialMoveRecord:
    return OfficialMoveRecord(GAME_ID, 2, 1, Stone.WHITE, Coordinate.parse("H8"), 1, 1, NOW)


@pytest.mark.asyncio
async def test_lookup_uses_turn_number_after_pass() -> None:
    adapter = InMemoryGamePersistenceAdapter()
    await adapter.start_game(start())
    command = after_pass()
    assert await adapter.append_move(command) is PersistenceOutcome.CREATED
    assert await adapter.get_move(GAME_ID, 1) is None
    assert await adapter.get_move(GAME_ID, 2) == command
    assert await adapter.get_move(OTHER_GAME_ID, 2) is None
    assert await adapter.append_move(command) is PersistenceOutcome.UNCHANGED
    assert len(adapter.moves) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "conflict",
    [
        replace(after_pass(), move_no=2),
        replace(after_pass(), turn_no=3),
        replace(after_pass(), coordinate=Coordinate.parse("I8")),
    ],
    ids=["same-turn-other-move", "same-move-other-turn", "same-identity-other-content"],
)
async def test_conflicting_move_does_not_change_history(conflict: OfficialMoveRecord) -> None:
    adapter = InMemoryGamePersistenceAdapter()
    await adapter.start_game(start())
    command = after_pass()
    await adapter.append_move(command)
    before = dict(adapter.moves)
    with pytest.raises(PersistenceRuleViolation, match="^MOVE_SEQUENCE_CONFLICT$"):
        await adapter.append_move(conflict)
    assert adapter.moves == before


@pytest.mark.asyncio
async def test_move_identity_is_scoped_to_game_and_missing_game_is_rejected() -> None:
    adapter = InMemoryGamePersistenceAdapter()
    command = after_pass()
    with pytest.raises(PersistenceRuleViolation, match="^GAME_NOT_FOUND$"):
        await adapter.append_move(command)
    assert adapter.moves == {}
    await adapter.start_game(start())
    await adapter.start_game(replace(start(), game_id=OTHER_GAME_ID))
    await adapter.append_move(command)
    other = replace(command, game_id=OTHER_GAME_ID)
    assert await adapter.append_move(other) is PersistenceOutcome.CREATED
    assert await adapter.get_move(GAME_ID, 2) == command
    assert await adapter.get_move(OTHER_GAME_ID, 2) == other
    history = await adapter.load_game(GAME_ID)
    assert history is not None
    assert history.moves == (command,)
    assert history.participants[0].rating is None
    assert history.participants[0].member_id is None
