from collections.abc import Callable, Iterable

import pytest

from seokpan.game.domain import (
    Coordinate,
    EndReason,
    Game,
    GameRuleViolation,
    GameStatus,
    Stone,
)
from seokpan.vote.domain import (
    ParticipantRole,
    TurnResultKind,
    TurnStatus,
    Voter,
    VoteRuleViolation,
    VoteTurnGame,
)


def participants() -> tuple[Voter, ...]:
    return (
        Voter("black-1", Stone.BLACK),
        Voter("black-2", Stone.BLACK),
        Voter("white-1", Stone.WHITE),
        Voter("white-2", Stone.WHITE),
        Voter("spectator", Stone.BLACK, role=ParticipantRole.SPECTATOR),
    )


def voting_game(*, game: Game | None = None, deadline_ms: int = 1_000) -> VoteTurnGame:
    return VoteTurnGame(
        game_id="game-1",
        participants=participants(),
        deadline_ms=deadline_ms,
        game=game,
    )


def snapshot(subject: VoteTurnGame) -> tuple[object, ...]:
    return (
        subject.turn_no,
        subject.turn_status,
        subject.current_team,
        subject.deadline_ms,
        subject.consecutive_passes,
        subject.votes,
        subject.participants,
        subject.game.status,
        subject.game.move_no,
        subject.game.end_reason,
        subject.game.moves,
        subject.game.occupied_cells,
    )


def assert_rejected_without_mutation(
    subject: VoteTurnGame,
    expected_code: str,
    command: Callable[[], object],
) -> None:
    before = snapshot(subject)
    with pytest.raises(VoteRuleViolation, match=expected_code) as error:
        command()
    assert error.value.code == expected_code
    assert snapshot(subject) == before


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


def cast(
    subject: VoteTurnGame,
    participant_id: str,
    coordinate: str,
    *,
    now_ms: int = 999,
) -> None:
    subject.cast_vote(
        game_id="game-1",
        turn_no=subject.turn_no,
        participant_id=participant_id,
        coordinate=coordinate,
        now_ms=now_ms,
    )


def test_vote_turn_game_starts_with_black_voting_turn_and_frozen_roster() -> None:
    subject = voting_game()

    assert subject.turn_no == 1
    assert subject.turn_status is TurnStatus.VOTING
    assert subject.current_team is Stone.BLACK
    assert subject.deadline_ms == 1_000
    assert subject.consecutive_passes == 0
    assert subject.votes == ()
    assert [item.participant_id for item in subject.participants] == [
        "black-1",
        "black-2",
        "spectator",
        "white-1",
        "white-2",
    ]


@pytest.mark.parametrize(
    ("command", "code"),
    [
        (lambda: Voter("", Stone.BLACK), "INVALID_PARTICIPANT_ID"),
        (lambda: Voter("empty", Stone.EMPTY), "INVALID_PARTICIPANT_TEAM"),
        (
            lambda: VoteTurnGame(game_id="", participants=participants(), deadline_ms=1),
            "INVALID_GAME_ID",
        ),
        (
            lambda: VoteTurnGame(game_id="g", participants=participants(), deadline_ms=-1),
            "INVALID_DEADLINE",
        ),
        (
            lambda: VoteTurnGame(game_id="g", participants=(), deadline_ms=1),
            "PARTICIPANTS_REQUIRED",
        ),
        (
            lambda: VoteTurnGame(
                game_id="g",
                participants=(Voter("same", Stone.BLACK), Voter("same", Stone.WHITE)),
                deadline_ms=1,
            ),
            "DUPLICATE_PARTICIPANT",
        ),
        (
            lambda: VoteTurnGame(
                game_id="g",
                participants=(Voter("black", Stone.BLACK),),
                deadline_ms=1,
            ),
            "BOTH_PLAYER_TEAMS_REQUIRED",
        ),
    ],
)
def test_constructor_rejects_invalid_identity_roster_and_deadline(
    command: Callable[[], object],
    code: str,
) -> None:
    with pytest.raises(VoteRuleViolation, match=code) as error:
        command()
    assert error.value.code == code


def test_constructor_rejects_a_finished_game() -> None:
    game = Game()
    play_positions(
        game,
        black=["E8", "F8", "G8", "H8", "I8"],
        white=["A1", "C1", "E1", "G1"],
    )

    with pytest.raises(VoteRuleViolation, match="GAME_NOT_ACTIVE"):
        voting_game(game=game)


def test_vote_is_created_replaced_and_deleted_as_one_final_vote_per_player() -> None:
    subject = voting_game()

    first = subject.cast_vote(
        game_id="game-1",
        turn_no=1,
        participant_id="black-1",
        coordinate="h8",
        now_ms=100,
    )
    replacement = subject.cast_vote(
        game_id="game-1",
        turn_no=1,
        participant_id="black-1",
        coordinate=Coordinate.parse("I8"),
        now_ms=200,
    )
    cast(subject, "black-2", "H8")

    assert first.coordinate.canonical == "H8"
    assert replacement.coordinate.canonical == "I8"
    assert [(vote.participant_id, vote.coordinate.canonical) for vote in subject.votes] == [
        ("black-1", "I8"),
        ("black-2", "H8"),
    ]

    subject.remove_vote(
        game_id="game-1",
        turn_no=1,
        participant_id="black-1",
        now_ms=300,
    )
    subject.remove_vote(
        game_id="game-1",
        turn_no=1,
        participant_id="black-1",
        now_ms=400,
    )
    assert [(vote.participant_id, vote.coordinate.canonical) for vote in subject.votes] == [
        ("black-2", "H8")
    ]


def test_stale_deadline_role_team_and_connection_rejections_do_not_mutate() -> None:
    subject = voting_game()
    subject.disconnect(participant_id="black-2", now_ms=100)

    cases: list[tuple[str, Callable[[], object]]] = [
        (
            "STALE_GAME",
            lambda: subject.cast_vote(
                game_id="other",
                turn_no=1,
                participant_id="black-1",
                coordinate="H8",
                now_ms=100,
            ),
        ),
        (
            "STALE_TURN",
            lambda: subject.cast_vote(
                game_id="game-1",
                turn_no=2,
                participant_id="black-1",
                coordinate="H8",
                now_ms=100,
            ),
        ),
        (
            "TURN_DEADLINE_REACHED",
            lambda: subject.cast_vote(
                game_id="game-1",
                turn_no=1,
                participant_id="black-1",
                coordinate="H8",
                now_ms=1_000,
            ),
        ),
        (
            "PARTICIPANT_NOT_FOUND",
            lambda: subject.cast_vote(
                game_id="game-1",
                turn_no=1,
                participant_id="missing",
                coordinate="H8",
                now_ms=100,
            ),
        ),
        (
            "PLAYER_REQUIRED",
            lambda: subject.cast_vote(
                game_id="game-1",
                turn_no=1,
                participant_id="spectator",
                coordinate="H8",
                now_ms=100,
            ),
        ),
        (
            "PARTICIPANT_DISCONNECTED",
            lambda: subject.cast_vote(
                game_id="game-1",
                turn_no=1,
                participant_id="black-2",
                coordinate="H8",
                now_ms=100,
            ),
        ),
        (
            "CURRENT_TEAM_REQUIRED",
            lambda: subject.cast_vote(
                game_id="game-1",
                turn_no=1,
                participant_id="white-1",
                coordinate="H8",
                now_ms=100,
            ),
        ),
        (
            "INVALID_COORDINATE",
            lambda: subject.cast_vote(
                game_id="game-1",
                turn_no=1,
                participant_id="black-1",
                coordinate="P1",
                now_ms=100,
            ),
        ),
    ]

    for code, command in cases:
        assert_rejected_without_mutation(subject, code, command)


def test_used_and_black_forbidden_coordinates_are_rejected_without_mutation() -> None:
    occupied_game = Game()
    occupied_game.apply_move(team=Stone.BLACK, coordinate="A1")
    occupied_game.apply_move(team=Stone.WHITE, coordinate="O15")
    occupied = voting_game(game=occupied_game)
    assert_rejected_without_mutation(
        occupied,
        "POSITION_OCCUPIED",
        lambda: occupied.cast_vote(
            game_id="game-1",
            turn_no=1,
            participant_id="black-1",
            coordinate="A1",
            now_ms=100,
        ),
    )

    forbidden_game = Game()
    play_positions(
        forbidden_game,
        black=["G8", "I8", "H7", "H9"],
        white=["A1", "C1", "E1", "G1"],
    )
    forbidden = voting_game(game=forbidden_game)
    assert_rejected_without_mutation(
        forbidden,
        "BLACK_DOUBLE_THREE",
        lambda: forbidden.cast_vote(
            game_id="game-1",
            turn_no=1,
            participant_id="black-1",
            coordinate="H8",
            now_ms=100,
        ),
    )


def test_disconnect_before_deadline_removes_vote_and_reconnect_does_not_restore_it() -> None:
    subject = voting_game()
    cast(subject, "black-1", "H8")

    subject.disconnect(participant_id="black-1", now_ms=500)
    subject.disconnect(participant_id="black-1", now_ms=600)
    assert subject.votes == ()
    assert not next(
        item for item in subject.participants if item.participant_id == "black-1"
    ).connected

    subject.reconnect(participant_id="black-1")
    subject.reconnect(participant_id="black-1")
    assert subject.votes == ()
    assert next(item for item in subject.participants if item.participant_id == "black-1").connected


def test_disconnect_at_or_after_deadline_does_not_change_the_frozen_vote() -> None:
    subject = voting_game()
    cast(subject, "black-1", "H8")

    subject.disconnect(participant_id="black-1", now_ms=1_000)
    assert [vote.coordinate.canonical for vote in subject.votes] == ["H8"]


def test_close_rejects_early_wrong_or_non_voting_turn_without_mutation() -> None:
    subject = voting_game()
    cast(subject, "black-1", "H8")

    assert_rejected_without_mutation(
        subject,
        "STALE_GAME",
        lambda: subject.close_voting(game_id="other", turn_no=1, now_ms=1_000),
    )
    assert_rejected_without_mutation(
        subject,
        "STALE_TURN",
        lambda: subject.close_voting(game_id="game-1", turn_no=2, now_ms=1_000),
    )
    assert_rejected_without_mutation(
        subject,
        "TURN_DEADLINE_NOT_REACHED",
        lambda: subject.close_voting(game_id="game-1", turn_no=1, now_ms=999),
    )
    subject.close_voting(game_id="game-1", turn_no=1, now_ms=1_000)
    assert_rejected_without_mutation(
        subject,
        "TURN_NOT_VOTING",
        lambda: subject.cast_vote(
            game_id="game-1",
            turn_no=1,
            participant_id="black-1",
            coordinate="I8",
            now_ms=999,
        ),
    )


def test_single_highest_vote_closes_then_applies_exactly_one_move_idempotently() -> None:
    subject = voting_game()
    cast(subject, "black-1", "H8")
    cast(subject, "black-2", "H8")

    closure = subject.close_voting(game_id="game-1", turn_no=1, now_ms=1_000)
    replayed_closure = subject.close_voting(game_id="game-1", turn_no=1, now_ms=1_500)

    assert replayed_closure == closure
    assert closure.result is TurnResultKind.RESOLUTION_REQUIRED
    assert closure.status is TurnStatus.RESOLVING
    assert [(item.coordinate.canonical, item.count) for item in closure.tally] == [("H8", 2)]
    assert [item.canonical for item in closure.candidates] == ["H8"]

    resolution = subject.resolve_move(
        game_id="game-1",
        turn_no=1,
        selected_coordinate=None,
        next_deadline_ms=2_000,
    )
    replayed = subject.resolve_move(
        game_id="game-1",
        turn_no=1,
        selected_coordinate="H8",
        next_deadline_ms=9_000,
    )
    replayed_without_selection = subject.resolve_move(
        game_id="game-1",
        turn_no=1,
        selected_coordinate=None,
        next_deadline_ms=9_000,
    )

    assert replayed == resolution
    assert replayed_without_selection == resolution
    assert resolution.result is TurnResultKind.MOVE_APPLIED
    assert resolution.status is TurnStatus.MOVE_APPLIED
    assert resolution.selected_coordinate == Coordinate.parse("H8")
    assert resolution.applied_move is not None
    assert resolution.applied_move.move_no == 1
    assert resolution.end_reason is None
    assert subject.game.move_no == 1
    assert subject.turn_no == 2
    assert subject.turn_status is TurnStatus.VOTING
    assert subject.current_team is Stone.WHITE
    assert subject.deadline_ms == 2_000
    assert subject.votes == ()


def test_tally_order_and_tie_selection_are_explicit_and_validated() -> None:
    subject = voting_game()
    cast(subject, "black-1", "I8")
    cast(subject, "black-2", "H8")
    closure = subject.close_voting(game_id="game-1", turn_no=1, now_ms=1_000)

    assert [(item.coordinate.canonical, item.count) for item in closure.tally] == [
        ("H8", 1),
        ("I8", 1),
    ]
    assert [item.canonical for item in closure.candidates] == ["H8", "I8"]
    assert_rejected_without_mutation(
        subject,
        "TIE_SELECTION_REQUIRED",
        lambda: subject.resolve_move(
            game_id="game-1",
            turn_no=1,
            selected_coordinate=None,
            next_deadline_ms=2_000,
        ),
    )
    assert_rejected_without_mutation(
        subject,
        "INVALID_COORDINATE",
        lambda: subject.resolve_move(
            game_id="game-1",
            turn_no=1,
            selected_coordinate="P1",
            next_deadline_ms=2_000,
        ),
    )
    assert_rejected_without_mutation(
        subject,
        "INVALID_RESOLUTION_CANDIDATE",
        lambda: subject.resolve_move(
            game_id="game-1",
            turn_no=1,
            selected_coordinate="J8",
            next_deadline_ms=2_000,
        ),
    )

    result = subject.resolve_move(
        game_id="game-1",
        turn_no=1,
        selected_coordinate=Coordinate.parse("I8"),
        next_deadline_ms=2_000,
    )
    assert result.selected_coordinate == Coordinate.parse("I8")

    assert_rejected_without_mutation(
        subject,
        "RESOLUTION_ALREADY_APPLIED",
        lambda: subject.resolve_move(
            game_id="game-1",
            turn_no=1,
            selected_coordinate="H8",
            next_deadline_ms=3_000,
        ),
    )
    assert_rejected_without_mutation(
        subject,
        "INVALID_COORDINATE",
        lambda: subject.resolve_move(
            game_id="game-1",
            turn_no=1,
            selected_coordinate="P1",
            next_deadline_ms=3_000,
        ),
    )


def test_single_candidate_rejects_a_different_explicit_selection() -> None:
    subject = voting_game()
    cast(subject, "black-1", "H8")
    subject.close_voting(game_id="game-1", turn_no=1, now_ms=1_000)

    assert_rejected_without_mutation(
        subject,
        "INVALID_RESOLUTION_CANDIDATE",
        lambda: subject.resolve_move(
            game_id="game-1",
            turn_no=1,
            selected_coordinate="I8",
            next_deadline_ms=2_000,
        ),
    )
    assert_rejected_without_mutation(
        subject,
        "INVALID_COORDINATE",
        lambda: subject.resolve_move(
            game_id="game-1",
            turn_no=1,
            selected_coordinate="P1",
            next_deadline_ms=2_000,
        ),
    )


def test_resolve_rejects_wrong_state_stale_identity_and_invalid_next_deadline() -> None:
    subject = voting_game()
    assert_rejected_without_mutation(
        subject,
        "TURN_NOT_RESOLVING",
        lambda: subject.resolve_move(
            game_id="game-1",
            turn_no=1,
            selected_coordinate="H8",
            next_deadline_ms=2_000,
        ),
    )
    cast(subject, "black-1", "H8")
    subject.close_voting(game_id="game-1", turn_no=1, now_ms=1_000)
    assert_rejected_without_mutation(
        subject,
        "STALE_GAME",
        lambda: subject.resolve_move(
            game_id="other",
            turn_no=1,
            selected_coordinate="H8",
            next_deadline_ms=2_000,
        ),
    )
    assert_rejected_without_mutation(
        subject,
        "STALE_TURN",
        lambda: subject.resolve_move(
            game_id="game-1",
            turn_no=2,
            selected_coordinate="H8",
            next_deadline_ms=2_000,
        ),
    )
    assert_rejected_without_mutation(
        subject,
        "INVALID_NEXT_DEADLINE",
        lambda: subject.resolve_move(
            game_id="game-1",
            turn_no=1,
            selected_coordinate="H8",
            next_deadline_ms=1_000,
        ),
    )


def test_external_game_state_drift_is_rejected_as_a_vote_domain_error() -> None:
    subject = voting_game()
    cast(subject, "black-1", "H8")
    subject.close_voting(game_id="game-1", turn_no=1, now_ms=1_000)
    subject.game.apply_move(team=Stone.BLACK, coordinate="H8")

    assert_rejected_without_mutation(
        subject,
        "NOT_CURRENT_TEAM",
        lambda: subject.resolve_move(
            game_id="game-1",
            turn_no=1,
            selected_coordinate=None,
            next_deadline_ms=2_000,
        ),
    )


def test_vote_request_rejects_when_the_wrapped_game_was_ended_externally() -> None:
    subject = voting_game()
    subject.game.finish_joint_loss()

    assert_rejected_without_mutation(
        subject,
        "GAME_NOT_ACTIVE",
        lambda: subject.cast_vote(
            game_id="game-1",
            turn_no=1,
            participant_id="black-1",
            coordinate="H8",
            now_ms=100,
        ),
    )
    assert_rejected_without_mutation(
        subject,
        "GAME_NOT_ACTIVE",
        lambda: subject.close_voting(
            game_id="game-1",
            turn_no=1,
            now_ms=1_000,
            next_deadline_ms=2_000,
        ),
    )


@pytest.mark.parametrize(
    ("end_reason", "winner", "expected_status"),
    [
        (EndReason.JOINT_LOSS, Stone.EMPTY, GameStatus.FINISHED),
        (EndReason.SYSTEM_INVALID, Stone.EMPTY, GameStatus.SYSTEM_INVALID),
    ],
)
def test_external_result_applies_supported_non_board_results(
    end_reason: EndReason,
    winner: Stone,
    expected_status: GameStatus,
) -> None:
    subject = voting_game()
    cast(subject, "black-1", "H8")

    subject.finalize_external_result(end_reason=end_reason, winner=winner)

    assert subject.game.status is expected_status
    assert subject.game.end_reason is end_reason
    assert subject.game.winner is winner
    assert subject.votes == ()
    assert subject.turn_status is TurnStatus.PASSED
    assert subject.deadline_ms is None


@pytest.mark.parametrize(
    ("end_reason", "winner"),
    [
        (EndReason.FORFEIT, Stone.EMPTY),
        (EndReason.JOINT_LOSS, Stone.BLACK),
        (EndReason.SYSTEM_INVALID, Stone.WHITE),
        (EndReason.BLACK_WIN, Stone.BLACK),
    ],
)
def test_external_result_rejects_invalid_reason_winner_combinations(
    end_reason: EndReason,
    winner: Stone,
) -> None:
    subject = voting_game()

    assert_rejected_without_mutation(
        subject,
        "INVALID_EXTERNAL_GAME_RESULT",
        lambda: subject.finalize_external_result(end_reason=end_reason, winner=winner),
    )


def test_external_result_is_idempotent_only_for_the_same_finished_result() -> None:
    subject = voting_game()

    subject.finalize_external_result(
        end_reason=EndReason.SYSTEM_INVALID,
        winner=Stone.EMPTY,
    )
    finished = snapshot(subject)

    subject.finalize_external_result(
        end_reason=EndReason.SYSTEM_INVALID,
        winner=Stone.EMPTY,
    )
    assert snapshot(subject) == finished

    assert_rejected_without_mutation(
        subject,
        "GAME_ALREADY_FINISHED",
        lambda: subject.finalize_external_result(
            end_reason=EndReason.JOINT_LOSS,
            winner=Stone.EMPTY,
        ),
    )


def test_external_result_translates_game_domain_failure() -> None:
    class FailingExternalGame(Game):
        def finish_system_invalid(self):
            raise GameRuleViolation("GAME_RESULT_ALREADY_FINALIZED")

    subject = voting_game(game=FailingExternalGame())

    assert_rejected_without_mutation(
        subject,
        "GAME_RESULT_ALREADY_FINALIZED",
        lambda: subject.finalize_external_result(
            end_reason=EndReason.SYSTEM_INVALID,
            winner=Stone.EMPTY,
        ),
    )


@pytest.mark.parametrize(
    ("winner", "expected_loser"),
    [
        (Stone.BLACK, Stone.WHITE),
        (Stone.WHITE, Stone.BLACK),
    ],
)
def test_external_forfeit_finishes_with_the_confirmed_winner(
    winner: Stone,
    expected_loser: Stone,
) -> None:
    subject = voting_game()
    cast(subject, "black-1", "H8")

    subject.finalize_external_result(
        end_reason=EndReason.FORFEIT,
        winner=winner,
    )

    assert subject.game.status is GameStatus.FINISHED
    assert subject.game.end_reason is EndReason.FORFEIT
    assert subject.game.winner is winner
    assert subject.game.conclusion is not None
    assert subject.game.conclusion.winner is not expected_loser
    assert subject.votes == ()
    assert subject.turn_status is TurnStatus.PASSED
    assert subject.deadline_ms is None


def test_public_candidate_selector_delegates_to_domain_selection_rule() -> None:
    candidates = (
        Coordinate.parse("H8"),
        Coordinate.parse("I8"),
    )

    assert VoteTurnGame.select_candidate(candidates, "I8") == Coordinate.parse("I8")


def test_first_zero_vote_pass_advances_and_second_consecutive_pass_is_joint_loss() -> None:
    subject = voting_game()

    first = subject.close_voting(
        game_id="game-1",
        turn_no=1,
        now_ms=1_000,
        next_deadline_ms=2_000,
    )
    replayed_first = subject.close_voting(
        game_id="game-1",
        turn_no=1,
        now_ms=9_000,
    )
    assert replayed_first == first
    assert first.result is TurnResultKind.PASSED
    assert first.status is TurnStatus.PASSED
    assert first.team is Stone.BLACK
    assert first.tally == ()
    assert first.candidates == ()
    assert subject.turn_no == 2
    assert subject.current_team is Stone.WHITE
    assert subject.consecutive_passes == 1
    assert subject.game.move_no == 0

    second = subject.close_voting(game_id="game-1", turn_no=2, now_ms=2_000)
    replayed_second = subject.close_voting(game_id="game-1", turn_no=2, now_ms=3_000)
    assert replayed_second == second
    assert second.result is TurnResultKind.JOINT_LOSS
    assert second.status is TurnStatus.RESOLVING
    assert second.team is Stone.WHITE
    assert subject.game.status is GameStatus.ACTIVE
    assert subject.consecutive_passes == 1

    resolution = subject.resolve_joint_loss(game_id="game-1", turn_no=2)
    finished_snapshot = snapshot(subject)
    assert subject.resolve_joint_loss(game_id="game-1", turn_no=2) == resolution
    assert snapshot(subject) == finished_snapshot
    assert resolution.result is TurnResultKind.JOINT_LOSS
    assert resolution.status is TurnStatus.PASSED
    assert subject.game.status is GameStatus.FINISHED
    assert subject.game.end_reason is EndReason.JOINT_LOSS
    assert subject.game.current_team is Stone.EMPTY
    assert subject.game.move_no == 0
    assert subject.deadline_ms is None
    assert subject.consecutive_passes == 2


def test_joint_loss_rejects_voting_move_closure_and_committed_move() -> None:
    subject = voting_game()
    cast(subject, "black-1", "H8")
    assert_rejected_without_mutation(
        subject,
        "TURN_NOT_RESOLVING",
        lambda: subject.resolve_joint_loss(game_id="game-1", turn_no=1),
    )
    subject.close_voting(game_id="game-1", turn_no=1, now_ms=1_000)
    assert_rejected_without_mutation(
        subject,
        "RESOLUTION_MISMATCH",
        lambda: subject.resolve_joint_loss(game_id="game-1", turn_no=1),
    )
    subject.resolve_move(
        game_id="game-1",
        turn_no=1,
        selected_coordinate=None,
        next_deadline_ms=2_000,
    )
    assert_rejected_without_mutation(
        subject,
        "RESOLUTION_ALREADY_APPLIED",
        lambda: subject.resolve_joint_loss(game_id="game-1", turn_no=1),
    )


def test_first_pass_requires_a_future_deadline_without_mutation() -> None:
    subject = voting_game()
    assert_rejected_without_mutation(
        subject,
        "INVALID_NEXT_DEADLINE",
        lambda: subject.close_voting(
            game_id="game-1",
            turn_no=1,
            now_ms=1_000,
            next_deadline_ms=None,
        ),
    )


def test_applied_move_after_a_pass_resets_the_consecutive_pass_count() -> None:
    subject = voting_game()
    subject.close_voting(
        game_id="game-1",
        turn_no=1,
        now_ms=1_000,
        next_deadline_ms=2_000,
    )
    cast(subject, "white-1", "H8", now_ms=1_500)
    subject.close_voting(game_id="game-1", turn_no=2, now_ms=2_000)
    subject.resolve_move(
        game_id="game-1",
        turn_no=2,
        selected_coordinate=None,
        next_deadline_ms=3_000,
    )

    assert subject.turn_no == 3
    assert subject.current_team is Stone.BLACK
    assert subject.game.move_no == 1
    assert subject.consecutive_passes == 0


def test_winning_resolution_finishes_without_opening_another_turn() -> None:
    game = Game()
    play_positions(
        game,
        black=["E8", "F8", "G8", "H8"],
        white=["A1", "C1", "E1", "G1"],
    )
    subject = voting_game(game=game)
    cast(subject, "black-1", "I8")
    subject.close_voting(game_id="game-1", turn_no=1, now_ms=1_000)

    result = subject.resolve_move(
        game_id="game-1",
        turn_no=1,
        selected_coordinate=None,
        next_deadline_ms=2_000,
    )

    assert result.end_reason is EndReason.BLACK_WIN
    assert subject.game.status is GameStatus.FINISHED
    assert subject.turn_no == 1
    assert subject.turn_status is TurnStatus.MOVE_APPLIED
    assert subject.deadline_ms is None
