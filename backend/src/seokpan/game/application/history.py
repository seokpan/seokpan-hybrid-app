"""Reconstruct a Board from stored Moves without trusting a Runtime snapshot."""

from __future__ import annotations

from dataclasses import dataclass

from seokpan.game.application.persistence import GamePersistenceSnapshot, PersistenceRuleViolation
from seokpan.game.domain import (
    AppliedMove,
    BoardCell,
    Coordinate,
    EndReason,
    Game,
    GameRuleViolation,
    GameStatus,
    Stone,
)


@dataclass(frozen=True, slots=True)
class CompletedGameReplay:
    occupied_cells: tuple[BoardCell, ...]
    move_no: int
    status: GameStatus
    end_reason: EndReason
    winner: Stone
    winning_line: tuple[Coordinate, ...]
    last_move: AppliedMove | None = None


def replay_game_history(history: GamePersistenceSnapshot) -> Game:
    """Replay ordered official Moves; gaps represent single zero-vote Passes.

    Two consecutive missing turns cannot precede another Move: they would
    already have ended the Game in joint loss. Trailing Passes and the final
    turn number are not recoverable from Move rows and are not invented here.
    """
    game = Game()
    next_turn = 1
    for move_no, move in enumerate(history.moves, 1):
        if (
            move.game_id != history.start.game_id
            or move.move_no != move_no
            or move.turn_no < next_turn
            or move.turn_no > next_turn + 1
        ):
            raise PersistenceRuleViolation("MOVE_SEQUENCE_CONFLICT")
        try:
            if move.turn_no > next_turn:
                game.pass_turn()
            game.apply_move(team=move.team, coordinate=move.coordinate)
        except GameRuleViolation as error:
            raise PersistenceRuleViolation("GAME_HISTORY_INVALID") from error
        next_turn = move.turn_no + 1
    return game


def replay_completed_game(
    history: GamePersistenceSnapshot,
    status: GameStatus,
    end_reason: EndReason,
    winner: Stone,
) -> CompletedGameReplay:
    """Validate Board-decided results and preserve externally decided reasons.

    Departures and unrecoverable failures are not inferred from missing Moves.
    The caller must supply a completed persistent result, not a client claim.
    This function neither calculates ratings nor authorizes result access.
    """
    game = replay_game_history(history)
    if end_reason in {EndReason.BLACK_WIN, EndReason.WHITE_WIN, EndReason.DRAW}:
        if (game.status, game.end_reason, game.winner) != (status, end_reason, winner):
            raise PersistenceRuleViolation("GAME_RESULT_HISTORY_MISMATCH")
        winning_line = game.winning_line
    elif (status, end_reason, winner) in {
        (GameStatus.SYSTEM_INVALID, EndReason.SYSTEM_INVALID, Stone.EMPTY),
        (GameStatus.FINISHED, EndReason.JOINT_LOSS, Stone.EMPTY),
        (GameStatus.FINISHED, EndReason.FORFEIT, Stone.BLACK),
        (GameStatus.FINISHED, EndReason.FORFEIT, Stone.WHITE),
    }:
        winning_line = ()
    else:
        raise PersistenceRuleViolation("GAME_RESULT_HISTORY_MISMATCH")
    return CompletedGameReplay(
        game.occupied_cells,
        game.move_no,
        status,
        end_reason,
        winner,
        winning_line,
        game.moves[-1] if game.moves else None,
    )
