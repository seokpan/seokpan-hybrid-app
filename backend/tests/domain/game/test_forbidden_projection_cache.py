"""Regression checks for immutable board projection reuse; no rule changes."""

import pytest

from seokpan.game.domain.model import (
    BoardCell,
    Coordinate,
    ForbiddenReason,
    Game,
    GameRuleViolation,
    Stone,
)


def board(*coordinates: str) -> tuple[BoardCell, ...]:
    return tuple(BoardCell(Coordinate.parse(value), Stone.BLACK) for value in coordinates)


def test_equal_boards_reuse_results_and_changed_stones_recalculate() -> None:
    cells = board("G8", "I8", "H7", "H9")
    expected = Game.black_forbidden_coordinates(cells)
    assert Coordinate.parse("H8") in expected
    # Fresh cell objects equal the same immutable snapshot, independent of actors.
    assert Game.black_forbidden_coordinates(board("G8", "I8", "H7", "H9")) is expected
    changed = (*cells[:-1], BoardCell(Coordinate.parse("H9"), Stone.WHITE))
    assert Coordinate.parse("H8") not in Game.black_forbidden_coordinates(changed)
    assert Game.black_forbidden_coordinates(cells) is expected


@pytest.mark.parametrize("duplicate", [False, True])
def test_invalid_snapshots_still_rejected_on_repeated_reads(duplicate: bool) -> None:
    cells = board("H8", "H8") if duplicate else (BoardCell(Coordinate.parse("H8"), Stone.EMPTY),)
    for _ in range(2):
        with pytest.raises(GameRuleViolation, match="INVALID_BOARD_SNAPSHOT"):
            Game.black_forbidden_coordinates(cells)


def test_cache_evicts_old_boards_and_preserves_class_dispatch() -> None:
    class CountingGame(Game):
        calls = 0

        @classmethod
        def _black_forbidden_reason(
            cls, proposed: dict[Coordinate, Stone], coordinate: Coordinate
        ) -> ForbiddenReason | None:
            cls.calls += 1
            return None

    first = board("A1")
    CountingGame.black_forbidden_coordinates(first)
    calls = CountingGame.calls
    CountingGame.black_forbidden_coordinates(board("A1"))
    assert CountingGame.calls == calls
    # 128 distinct later boards force eviction of the earliest entry.
    for index in range(1, 129):
        coordinate = Coordinate(column=index % 15 + 1, row=index // 15 + 1)
        CountingGame.black_forbidden_coordinates((BoardCell(coordinate, Stone.BLACK),))
    calls = CountingGame.calls
    CountingGame.black_forbidden_coordinates(first)
    assert CountingGame.calls > calls
    # A subclass result must not substitute for the real rules.
    assert Coordinate.parse("H8") in Game.black_forbidden_coordinates(board("G8", "I8", "H7", "H9"))
