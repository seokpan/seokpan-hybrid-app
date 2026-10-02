from dataclasses import replace
from datetime import UTC, datetime

import pytest

from seokpan.game.application.history import replay_completed_game, replay_game_history
from seokpan.game.application.persistence import (
    GameParticipantRecord,
    GamePersistenceSnapshot,
    OfficialMoveRecord,
    PersistenceRuleViolation,
    StartGameCommand,
)
from seokpan.game.domain import Coordinate, EndReason, GameStatus, Stone

GAME_ID = "00000000-0000-4000-8000-000000000001"
OTHER_ID = "00000000-0000-4000-8000-000000000002"
NOW = datetime(2026, 9, 7, tzinfo=UTC)


def history(*moves: tuple[int, Stone, str]) -> GamePersistenceSnapshot:
    start = StartGameCommand(
        GAME_ID,
        OTHER_ID,
        10,
        NOW,
        (
            GameParticipantRecord(
                "00000000-0000-4000-8000-000000000003",
                Stone.BLACK,
                guest_label="Guest-0001",
            ),
            GameParticipantRecord(
                "00000000-0000-4000-8000-000000000004",
                Stone.WHITE,
                guest_label="Guest-0002",
            ),
        ),
    )
    return GamePersistenceSnapshot(
        start,
        (),
        tuple(
            OfficialMoveRecord(GAME_ID, turn, index, team, Coordinate.parse(pos), 1, 1, NOW)
            for index, (turn, team, pos) in enumerate(moves, 1)
        ),
    )


def black_win_history() -> GamePersistenceSnapshot:
    # First BLACK Pass; WHITE places once, then passes between BLACK moves.
    return history(
        (2, Stone.WHITE, "O15"),
        *((3 + index * 2, Stone.BLACK, f"{column}1") for index, column in enumerate("ABCDE")),
    )


def test_replay_preserves_pass_move_numbers_and_does_not_mutate_history() -> None:
    stored = black_win_history()
    before = stored.moves
    game = replay_game_history(stored)
    assert game.move_no == 6
    assert game.status is GameStatus.FINISHED
    assert game.winner is Stone.BLACK
    assert game.stone_at("O15") is Stone.WHITE
    assert stored.moves == before
    assert game.winning_line == tuple(Coordinate.parse(f"{column}1") for column in "ABCDE")


def test_replay_empty_history_does_not_invent_a_pass_or_end_turn() -> None:
    game = replay_game_history(history())
    assert game.move_no == 0
    assert game.current_team is Stone.BLACK
    assert game.status is GameStatus.ACTIVE


@pytest.mark.parametrize("field,value", [("game_id", OTHER_ID), ("move_no", 2)])
def test_replay_rejects_wrong_game_or_move_sequence(field: str, value: str | int) -> None:
    stored = history((1, Stone.BLACK, "A1"))
    invalid = replace(stored.moves[0], **{field: value})
    with pytest.raises(PersistenceRuleViolation, match="MOVE_SEQUENCE_CONFLICT"):
        replay_game_history(replace(stored, moves=(invalid,)))


@pytest.mark.parametrize(
    "moves",
    [
        ((1, Stone.BLACK, "A1"), (1, Stone.WHITE, "A2")),
        ((2, Stone.WHITE, "A1"), (1, Stone.BLACK, "A2")),
        ((3, Stone.BLACK, "A1"),),
        ((1, Stone.BLACK, "A1"), (4, Stone.WHITE, "A2")),
    ],
)
def test_replay_rejects_turn_regression_or_two_missing_turns(
    moves: tuple[tuple[int, Stone, str], ...],
) -> None:
    with pytest.raises(PersistenceRuleViolation, match="MOVE_SEQUENCE_CONFLICT"):
        replay_game_history(history(*moves))


@pytest.mark.parametrize(
    "moves",
    [
        ((1, Stone.WHITE, "A1"),),
        ((1, Stone.BLACK, "A1"), (2, Stone.WHITE, "A1")),
    ],
)
def test_replay_rejects_wrong_team_or_occupied_coordinate(
    moves: tuple[tuple[int, Stone, str], ...],
) -> None:
    with pytest.raises(PersistenceRuleViolation, match="GAME_HISTORY_INVALID"):
        replay_game_history(history(*moves))


def test_replay_rejects_a_move_after_a_winning_move() -> None:
    stored = black_win_history()
    extra = OfficialMoveRecord(GAME_ID, 12, 7, Stone.WHITE, Coordinate.parse("O14"), 1, 1, NOW)
    with pytest.raises(PersistenceRuleViolation, match="GAME_HISTORY_INVALID"):
        replay_game_history(replace(stored, moves=(*stored.moves, extra)))


def test_completed_replay_verifies_normal_win_against_board() -> None:
    game = replay_completed_game(
        black_win_history(), GameStatus.FINISHED, EndReason.BLACK_WIN, Stone.BLACK
    )
    assert game.end_reason is EndReason.BLACK_WIN
    assert len(game.winning_line) == 5


@pytest.mark.parametrize(
    "status,reason,winner",
    [
        (GameStatus.FINISHED, EndReason.WHITE_WIN, Stone.WHITE),
        (GameStatus.FINISHED, EndReason.BLACK_WIN, Stone.WHITE),
        (GameStatus.SYSTEM_INVALID, EndReason.BLACK_WIN, Stone.BLACK),
        (GameStatus.FINISHED, EndReason.DRAW, Stone.EMPTY),
    ],
)
def test_completed_replay_rejects_result_board_disagreement(
    status: GameStatus, reason: EndReason, winner: Stone
) -> None:
    with pytest.raises(PersistenceRuleViolation, match="GAME_RESULT_HISTORY_MISMATCH"):
        replay_completed_game(black_win_history(), status, reason, winner)


@pytest.mark.parametrize(
    "status,reason,winner",
    [
        (GameStatus.FINISHED, EndReason.FORFEIT, Stone.BLACK),
        (GameStatus.FINISHED, EndReason.FORFEIT, Stone.WHITE),
        (GameStatus.FINISHED, EndReason.JOINT_LOSS, Stone.EMPTY),
        (GameStatus.SYSTEM_INVALID, EndReason.SYSTEM_INVALID, Stone.EMPTY),
    ],
)
def test_non_board_result_uses_stored_reason_without_inventing_winning_line(
    status: GameStatus, reason: EndReason, winner: Stone
) -> None:
    game = replay_completed_game(history((2, Stone.WHITE, "H8")), status, reason, winner)
    assert (game.status, game.end_reason, game.winner) == (status, reason, winner)
    assert game.move_no == 1
    assert {cell.coordinate.canonical: cell.stone for cell in game.occupied_cells} == {
        "H8": Stone.WHITE
    }
    assert game.winning_line == ()


@pytest.mark.parametrize(
    "status,reason,winner",
    [
        (GameStatus.ACTIVE, EndReason.JOINT_LOSS, Stone.EMPTY),
        (GameStatus.FINISHED, EndReason.SYSTEM_INVALID, Stone.EMPTY),
        (GameStatus.SYSTEM_INVALID, EndReason.JOINT_LOSS, Stone.EMPTY),
        (GameStatus.FINISHED, EndReason.JOINT_LOSS, Stone.BLACK),
        (GameStatus.FINISHED, EndReason.FORFEIT, Stone.EMPTY),
        (GameStatus.FINISHED, EndReason.BLACK_WIN, Stone.BLACK),
    ],
)
def test_completed_replay_rejects_inconsistent_stored_conclusion(
    status: GameStatus, reason: EndReason, winner: Stone
) -> None:
    with pytest.raises(PersistenceRuleViolation, match="GAME_RESULT_HISTORY_MISMATCH"):
        replay_completed_game(history(), status, reason, winner)


def test_stored_system_invalid_is_not_replaced_with_a_board_win() -> None:
    result = replay_completed_game(
        black_win_history(), GameStatus.SYSTEM_INVALID, EndReason.SYSTEM_INVALID, Stone.EMPTY
    )
    assert result.status is GameStatus.SYSTEM_INVALID
    assert result.winner is Stone.EMPTY
    assert result.winning_line == ()
    assert result.move_no == 6


def test_white_win_after_initial_black_pass() -> None:
    stored = history(
        *((2 + index * 2, Stone.WHITE, f"{col}1") for index, col in enumerate("ABCDE"))
    )
    result = replay_completed_game(stored, GameStatus.FINISHED, EndReason.WHITE_WIN, Stone.WHITE)
    assert len(result.winning_line) == 5
    assert result.move_no == 5


def test_replay_full_board_draw() -> None:
    black: list[str] = []
    white: list[str] = []
    for row in range(1, 16):
        for column in range(1, 16):
            positions = black if (column - 1 + 2 * (row - 1)) % 4 < 2 else white
            positions.append(Coordinate(column, row).canonical)
    moves = [
        (
            turn,
            Stone.BLACK if turn % 2 else Stone.WHITE,
            (black if turn % 2 else white)[(turn - 1) // 2],
        )
        for turn in range(1, 226)
    ]
    result = replay_completed_game(
        history(*moves), GameStatus.FINISHED, EndReason.DRAW, Stone.EMPTY
    )
    assert result.move_no == len(result.occupied_cells) == 225
    assert result.winning_line == ()


@pytest.mark.parametrize(
    "positions",
    [
        ("E8", "F8", "G8", "H8", "J8", "I8"),
        ("F8", "G8", "I8", "H6", "H7", "H9", "H8"),
        ("G8", "I8", "H7", "H9", "H8"),
    ],
)
def test_replay_rejects_black_forbidden_moves(positions: tuple[str, ...]) -> None:
    stored = history(*((1 + index * 2, Stone.BLACK, pos) for index, pos in enumerate(positions)))
    with pytest.raises(PersistenceRuleViolation, match="GAME_HISTORY_INVALID"):
        replay_game_history(stored)
