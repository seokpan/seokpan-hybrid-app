from collections.abc import Iterable

import pytest

from seokpan.game.domain import (
    BoardCell,
    Coordinate,
    EndReason,
    ForbiddenReason,
    Game,
    GameRuleViolation,
    GameStatus,
    Stone,
)


@pytest.mark.parametrize("duplicate", [False, True])
def test_forbidden_snapshot_rejects_empty_or_duplicate_cells(duplicate: bool) -> None:
    coordinate = Coordinate.parse("H8")
    cells = (
        (
            BoardCell(coordinate=coordinate, stone=Stone.BLACK),
            BoardCell(coordinate=coordinate, stone=Stone.WHITE),
        )
        if duplicate
        else (BoardCell(coordinate=coordinate, stone=Stone.EMPTY),)
    )
    with pytest.raises(GameRuleViolation, match="INVALID_BOARD_SNAPSHOT") as error:
        Game.black_forbidden_coordinates(cells)
    assert error.value.code == "INVALID_BOARD_SNAPSHOT"


def snapshot(game: Game) -> tuple[object, ...]:
    return (
        game.status,
        game.current_team,
        game.move_no,
        game.end_reason,
        game.winner,
        game.conclusion,
        game.winning_line,
        game.moves,
        game.occupied_cells,
    )


def assert_rejected_without_mutation(
    game: Game,
    expected_code: str,
    command: object,
) -> None:
    before = snapshot(game)
    with pytest.raises(GameRuleViolation, match=expected_code) as error:
        assert callable(command)
        command()
    assert error.value.code == expected_code
    assert snapshot(game) == before


def play_positions(
    game: Game,
    *,
    black: Iterable[str],
    white: Iterable[str],
) -> None:
    black_positions = iter(black)
    white_positions = iter(white)
    while game.status is GameStatus.ACTIVE:
        try:
            coordinate = next(
                black_positions if game.current_team is Stone.BLACK else white_positions
            )
        except StopIteration:
            break
        game.apply_move(team=game.current_team, coordinate=coordinate)


@pytest.mark.parametrize(
    ("value", "canonical"),
    [
        ("A1", "A1"),
        ("A15", "A15"),
        ("O1", "O1"),
        ("o15", "O15"),
        ("  h8  ", "H8"),
    ],
)
def test_coordinate_parses_mvp_boundaries_and_normalizes_canonical_form(
    value: str,
    canonical: str,
) -> None:
    coordinate = Coordinate.parse(value)
    assert coordinate.canonical == canonical


@pytest.mark.parametrize("value", ["", "A0", "A16", "P1", "H08", "8H"])
def test_coordinate_rejects_values_outside_canonical_mvp_range(value: str) -> None:
    with pytest.raises(GameRuleViolation, match="INVALID_COORDINATE"):
        Coordinate.parse(value)

    with pytest.raises(GameRuleViolation, match="INVALID_COORDINATE"):
        Coordinate(column=0, row=1)


def test_game_starts_with_empty_board_black_turn_and_zero_moves() -> None:
    game = Game()
    assert game.status is GameStatus.ACTIVE
    assert game.current_team is Stone.BLACK
    assert game.move_no == 0
    assert game.end_reason is None
    assert game.winner is Stone.EMPTY
    assert game.conclusion is None
    assert game.winning_line == ()
    assert game.moves == ()
    assert game.occupied_cells == ()
    assert game.stone_at("H8") is Stone.EMPTY
    assert Game.black_forbidden_coordinates(game.occupied_cells) == ()


def test_valid_moves_change_board_turn_and_move_number_once() -> None:
    game = Game()
    first = game.apply_move(team=Stone.BLACK, coordinate="h8")
    second = game.apply_move(team=Stone.WHITE, coordinate=Coordinate.parse("H9"))

    assert first.move.move_no == 1
    assert first.move.coordinate.canonical == "H8"
    assert first.current_team is Stone.WHITE
    assert second.move.move_no == 2
    assert second.current_team is Stone.BLACK
    assert game.stone_at("H8") is Stone.BLACK
    assert game.stone_at("H9") is Stone.WHITE
    assert [cell.coordinate.canonical for cell in game.occupied_cells] == ["H8", "H9"]


def test_pass_turn_changes_only_the_current_team() -> None:
    game = Game()
    before = (game.status, game.move_no, game.moves, game.occupied_cells)

    game.pass_turn()
    assert game.current_team is Stone.WHITE
    game.pass_turn()
    assert game.current_team is Stone.BLACK
    assert (game.status, game.move_no, game.moves, game.occupied_cells) == before


def test_invalid_team_turn_and_occupied_position_do_not_mutate_game() -> None:
    game = Game()
    assert_rejected_without_mutation(
        game,
        "INVALID_COORDINATE",
        lambda: game.apply_move(team=Stone.BLACK, coordinate="P1"),
    )
    assert_rejected_without_mutation(
        game,
        "INVALID_MOVE_TEAM",
        lambda: game.apply_move(team=Stone.EMPTY, coordinate="H8"),
    )
    assert_rejected_without_mutation(
        game,
        "NOT_CURRENT_TEAM",
        lambda: game.apply_move(team=Stone.WHITE, coordinate="H8"),
    )
    game.apply_move(team=Stone.BLACK, coordinate="H8")
    assert_rejected_without_mutation(
        game,
        "POSITION_OCCUPIED",
        lambda: game.apply_move(team=Stone.WHITE, coordinate="H8"),
    )
    assert_rejected_without_mutation(
        game,
        "POSITION_OCCUPIED",
        lambda: game.black_forbidden_reason("H8"),
    )


@pytest.mark.parametrize(
    "black",
    [
        ["E8", "F8", "G8", "H8", "I8"],
        ["H5", "H6", "H7", "H8", "H9"],
        ["E5", "F6", "G7", "H8", "I9"],
        ["E11", "F10", "G9", "H8", "I7"],
    ],
)
def test_black_wins_with_exactly_five_in_every_direction(black: list[str]) -> None:
    game = Game()
    play_positions(game, black=black, white=["A1", "C1", "E1", "G1"])

    assert game.status is GameStatus.FINISHED
    assert game.end_reason is EndReason.BLACK_WIN
    assert game.winner is Stone.BLACK
    assert game.conclusion is not None
    assert game.current_team is Stone.EMPTY
    assert len(game.winning_line) == 5
    assert_rejected_without_mutation(
        game,
        "GAME_NOT_ACTIVE",
        lambda: game.apply_move(team=Stone.WHITE, coordinate="O15"),
    )
    assert_rejected_without_mutation(game, "GAME_NOT_ACTIVE", game.pass_turn)
    assert_rejected_without_mutation(
        game,
        "GAME_RESULT_ALREADY_FINALIZED",
        game.finish_joint_loss,
    )


@pytest.mark.parametrize(
    "white",
    [
        ["E8", "F8", "G8", "H8", "I8"],
        ["H5", "H6", "H7", "H8", "H9"],
        ["E5", "F6", "G7", "H8", "I9"],
        ["E11", "F10", "G9", "H8", "I7"],
        ["E8", "F8", "G8", "H8", "J8", "I8"],
    ],
)
def test_white_wins_with_five_or_more_in_every_direction(white: list[str]) -> None:
    game = Game()
    play_positions(
        game,
        black=["A1", "C1", "E1", "G1", "I1", "K1"],
        white=white,
    )

    assert game.status is GameStatus.FINISHED
    assert game.end_reason is EndReason.WHITE_WIN
    assert game.winner is Stone.WHITE
    assert len(game.winning_line) == len(white)


@pytest.mark.parametrize(
    ("black", "candidate", "reason"),
    [
        (
            ["E8", "F8", "G8", "H8", "J8"],
            "I8",
            ForbiddenReason.OVERLINE,
        ),
        (
            ["F8", "G8", "I8", "H6", "H7", "H9"],
            "H8",
            ForbiddenReason.DOUBLE_FOUR,
        ),
        (
            ["E8", "F8", "I8", "H5", "H6", "H9"],
            "H8",
            ForbiddenReason.DOUBLE_FOUR,
        ),
        (
            ["G8", "I8", "H7", "H9"],
            "H8",
            ForbiddenReason.DOUBLE_THREE,
        ),
        (
            ["G8", "J8", "H7", "H10"],
            "H8",
            ForbiddenReason.DOUBLE_THREE,
        ),
    ],
)
def test_black_forbidden_moves_are_derived_and_rejected_without_mutation(
    black: list[str],
    candidate: str,
    reason: ForbiddenReason,
) -> None:
    game = Game()
    white = ["A1", "C1", "E1", "G1", "I1", "K1"]
    play_positions(game, black=black, white=white)
    before = snapshot(game)

    assert game.black_forbidden_reason(candidate) is reason
    assert Coordinate.parse(candidate) in Game.black_forbidden_coordinates(game.occupied_cells)
    assert snapshot(game) == before
    assert_rejected_without_mutation(
        game,
        f"BLACK_{reason.value}",
        lambda: game.apply_move(team=Stone.BLACK, coordinate=candidate),
    )


def test_board_edge_does_not_count_blocked_threes_as_open_double_three() -> None:
    game = Game()
    play_positions(
        game,
        black=["B1", "C1", "A2", "A3"],
        white=["O15", "M15", "K15", "I15"],
    )

    assert game.black_forbidden_reason("A1") is None
    outcome = game.apply_move(team=Stone.BLACK, coordinate="A1")
    assert outcome.status is GameStatus.ACTIVE


def test_project_mvp_rejects_overline_even_when_same_move_would_make_exact_five() -> None:
    game = Game()
    play_positions(
        game,
        black=["F8", "G8", "I8", "J8", "H5", "H6", "H7", "H9", "H10"],
        white=["A1", "C1", "E1", "G1", "I1", "K1", "M1", "O1", "A3"],
    )

    assert game.black_forbidden_reason("H8") is ForbiddenReason.OVERLINE
    assert_rejected_without_mutation(
        game,
        "BLACK_OVERLINE",
        lambda: game.apply_move(team=Stone.BLACK, coordinate="H8"),
    )


def test_full_board_without_five_is_a_draw() -> None:
    game = Game()
    black: list[str] = []
    white: list[str] = []
    for row in range(1, 16):
        for column in range(1, 16):
            coordinate = Coordinate(column=column, row=row).canonical
            if (column - 1 + 2 * (row - 1)) % 4 < 2:
                black.append(coordinate)
            else:
                white.append(coordinate)

    play_positions(game, black=black, white=white)

    assert game.move_no == 225
    assert game.status is GameStatus.FINISHED
    assert game.end_reason is EndReason.DRAW
    assert game.winner is Stone.EMPTY
    assert game.winning_line == ()
