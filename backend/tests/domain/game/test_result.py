from collections.abc import Iterable
from decimal import Decimal

import pytest

from seokpan.game.domain import (
    EndReason,
    Game,
    GameParticipantRole,
    GameParticipantSnapshot,
    GameResultRuleViolation,
    GameResultService,
    GameRuleViolation,
    GameStatus,
    MemberOutcome,
    Stone,
    round_rating_delta,
)


def participant(
    participant_id: str,
    team: Stone,
    *,
    member_id: int | None,
    rating: int | None,
    role: GameParticipantRole = GameParticipantRole.PLAYER,
) -> GameParticipantSnapshot:
    return GameParticipantSnapshot(
        participant_id=participant_id,
        team=team,
        role=role,
        member_id=member_id,
        rating=rating,
    )


def roster(
    *,
    black_rating: int = 1000,
    white_rating: int = 1000,
    with_guests_and_spectator: bool = False,
) -> tuple[GameParticipantSnapshot, ...]:
    entries = [
        participant("black-member", Stone.BLACK, member_id=1, rating=black_rating),
        participant("white-member", Stone.WHITE, member_id=2, rating=white_rating),
    ]
    if with_guests_and_spectator:
        entries.extend(
            [
                participant("black-guest", Stone.BLACK, member_id=None, rating=None),
                participant("white-guest", Stone.WHITE, member_id=None, rating=None),
                participant(
                    "spectator-member",
                    Stone.BLACK,
                    member_id=3,
                    rating=1700,
                    role=GameParticipantRole.SPECTATOR,
                ),
            ]
        )
    return tuple(entries)


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


def finished_draw_game() -> Game:
    game = Game()
    black: list[str] = []
    white: list[str] = []
    for row in range(1, 16):
        for column in range(1, 16):
            coordinate = f"{chr(ord('A') + column - 1)}{row}"
            if (column - 1 + 2 * (row - 1)) % 4 < 2:
                black.append(coordinate)
            else:
                white.append(coordinate)
    play_positions(game, black=black, white=white)
    assert game.status is GameStatus.FINISHED
    assert game.end_reason is EndReason.DRAW
    return game


def service(
    *,
    game: Game | None = None,
    participants: tuple[GameParticipantSnapshot, ...] | None = None,
) -> GameResultService:
    return GameResultService(
        game_id="game-1",
        game=game if game is not None else Game(),
        participants=participants if participants is not None else roster(),
    )


def result_snapshot(subject: GameResultService) -> tuple[object, ...]:
    return (
        subject.game.status,
        subject.game.end_reason,
        subject.game.winner,
        subject.game.conclusion,
        subject.result,
    )


def assert_rejected_without_mutation(
    subject: GameResultService,
    expected_code: str,
    command: object,
) -> None:
    before = result_snapshot(subject)
    with pytest.raises(GameResultRuleViolation, match=expected_code) as error:
        assert callable(command)
        command()
    assert error.value.code == expected_code
    assert result_snapshot(subject) == before


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        (
            {
                "participant_id": "",
                "team": Stone.BLACK,
                "role": GameParticipantRole.PLAYER,
                "member_id": 1,
                "rating": 1000,
            },
            "INVALID_PARTICIPANT_ID",
        ),
        (
            {
                "participant_id": "player",
                "team": Stone.EMPTY,
                "role": GameParticipantRole.PLAYER,
                "member_id": 1,
                "rating": 1000,
            },
            "INVALID_PLAYER_TEAM",
        ),
        (
            {
                "participant_id": "guest",
                "team": Stone.BLACK,
                "role": GameParticipantRole.PLAYER,
                "member_id": None,
                "rating": 1000,
            },
            "GUEST_RATING_NOT_ALLOWED",
        ),
        (
            {
                "participant_id": "member",
                "team": Stone.BLACK,
                "role": GameParticipantRole.PLAYER,
                "member_id": 0,
                "rating": 1000,
            },
            "INVALID_MEMBER_ID",
        ),
        (
            {
                "participant_id": "member",
                "team": Stone.BLACK,
                "role": GameParticipantRole.PLAYER,
                "member_id": 1,
                "rating": -1,
            },
            "INVALID_MEMBER_RATING",
        ),
    ],
)
def test_participant_snapshot_rejects_invalid_identity_and_rating(
    kwargs: dict[str, object],
    code: str,
) -> None:
    with pytest.raises(GameResultRuleViolation, match=code) as error:
        GameParticipantSnapshot(**kwargs)  # type: ignore[arg-type]
    assert error.value.code == code


@pytest.mark.parametrize(
    ("game_id", "participants", "code"),
    [
        ("", roster(), "INVALID_GAME_ID"),
        ("game-1", (), "PARTICIPANTS_REQUIRED"),
        (
            "game-1",
            roster() + (participant("black-member", Stone.BLACK, member_id=3, rating=1000),),
            "DUPLICATE_PARTICIPANT",
        ),
        (
            "game-1",
            roster() + (participant("black-other", Stone.BLACK, member_id=1, rating=1000),),
            "DUPLICATE_MEMBER",
        ),
        (
            "game-1",
            (participant("black", Stone.BLACK, member_id=1, rating=1000),),
            "BOTH_PLAYER_TEAMS_REQUIRED",
        ),
    ],
)
def test_service_rejects_invalid_game_roster(
    game_id: str,
    participants: tuple[GameParticipantSnapshot, ...],
    code: str,
) -> None:
    with pytest.raises(GameResultRuleViolation, match=code) as error:
        GameResultService(game_id=game_id, game=Game(), participants=participants)
    assert error.value.code == code


def test_normal_win_uses_game_conclusion_and_is_idempotent() -> None:
    game = Game()
    play_positions(
        game,
        black=["E8", "F8", "G8", "H8", "I8"],
        white=["A1", "C1", "E1", "G1"],
    )
    subject = service(game=game)

    result = subject.finalize_completed_game()
    replayed = subject.finalize_completed_game()

    assert replayed is result
    assert result.status is GameStatus.FINISHED
    assert result.end_reason is EndReason.BLACK_WIN
    assert result.winner is Stone.BLACK
    assert len(result.winning_line) == 5
    assert result.stats_eligible
    assert [
        (item.member_id, item.outcome, item.rating_delta) for item in result.rating_adjustments
    ] == [
        (1, MemberOutcome.WIN, 16),
        (2, MemberOutcome.LOSS, -16),
    ]


def test_active_game_cannot_be_finalized_without_a_termination_fact() -> None:
    subject = service()
    assert_rejected_without_mutation(
        subject,
        "GAME_NOT_FINISHED",
        subject.finalize_completed_game,
    )


def test_forfeit_rejects_an_empty_team_without_mutating_game() -> None:
    game = Game()
    before = (game.status, game.end_reason, game.winner, game.conclusion)
    with pytest.raises(GameRuleViolation, match="INVALID_FORFEIT_TEAM") as error:
        game.finish_forfeit(losing_team=Stone.EMPTY)
    assert error.value.code == "INVALID_FORFEIT_TEAM"
    assert (game.status, game.end_reason, game.winner, game.conclusion) == before


def test_confirmed_single_team_departure_is_forfeit_and_includes_departed_member() -> None:
    subject = service(participants=roster(with_guests_and_spectator=True))

    result = subject.finalize_confirmed_departures(
        departed_participant_ids=frozenset({"black-member", "black-guest"}),
    )
    replayed = subject.finalize_confirmed_departures(
        departed_participant_ids=frozenset({"black-member", "black-guest"}),
    )

    assert replayed is result
    assert result.end_reason is EndReason.FORFEIT
    assert result.winner is Stone.WHITE
    assert [
        (item.participant_id, item.outcome, item.rating_delta) for item in result.rating_adjustments
    ] == [
        ("black-member", MemberOutcome.LOSS, -16),
        ("white-member", MemberOutcome.WIN, 16),
    ]
    assert all(item.member_id != 3 for item in result.rating_adjustments)


def test_confirmed_both_team_departure_is_joint_loss() -> None:
    subject = service()
    result = subject.finalize_confirmed_departures(
        departed_participant_ids=frozenset({"black-member", "white-member"}),
    )

    assert result.end_reason is EndReason.JOINT_LOSS
    assert result.winner is Stone.EMPTY
    assert [item.outcome for item in result.rating_adjustments] == [
        MemberOutcome.LOSS,
        MemberOutcome.LOSS,
    ]
    assert [item.rating_delta for item in result.rating_adjustments] == [-16, -16]


def test_partial_or_unknown_departure_does_not_finish_the_game() -> None:
    subject = service(participants=roster(with_guests_and_spectator=True))
    assert_rejected_without_mutation(
        subject,
        "FORFEIT_NOT_CONFIRMED",
        lambda: subject.finalize_confirmed_departures(
            departed_participant_ids=frozenset({"black-member"}),
        ),
    )
    assert_rejected_without_mutation(
        subject,
        "PARTICIPANT_NOT_FOUND",
        lambda: subject.finalize_confirmed_departures(
            departed_participant_ids=frozenset({"unknown"}),
        ),
    )


def test_system_invalid_creates_no_stats_or_rating_plan_and_is_idempotent() -> None:
    subject = service()

    result = subject.finalize_system_invalid()
    replayed = subject.finalize_system_invalid()

    assert replayed is result
    assert result.status is GameStatus.SYSTEM_INVALID
    assert result.end_reason is EndReason.SYSTEM_INVALID
    assert result.winner is Stone.EMPTY
    assert not result.stats_eligible
    assert result.rating_adjustments == ()


def test_conflicting_termination_cannot_replace_an_existing_result() -> None:
    subject = service()
    subject.finalize_confirmed_departures(
        departed_participant_ids=frozenset({"black-member"}),
    )

    assert_rejected_without_mutation(
        subject,
        "GAME_RESULT_ALREADY_FINALIZED",
        subject.finalize_system_invalid,
    )
    assert_rejected_without_mutation(
        subject,
        "GAME_RESULT_ALREADY_FINALIZED",
        lambda: subject.finalize_confirmed_departures(
            departed_participant_ids=frozenset({"black-member", "white-member"}),
        ),
    )


def test_guests_contribute_rating_1000_to_team_average_but_receive_no_adjustment() -> None:
    participants = (
        participant("black-member", Stone.BLACK, member_id=1, rating=1200),
        participant("black-guest", Stone.BLACK, member_id=None, rating=None),
        participant("white-member", Stone.WHITE, member_id=2, rating=1000),
    )
    subject = service(participants=participants)
    result = subject.finalize_confirmed_departures(
        departed_participant_ids=frozenset({"white-member"}),
    )

    assert [(item.member_id, item.rating_delta) for item in result.rating_adjustments] == [
        (1, 12),
        (2, -12),
    ]


def test_draw_uses_half_score_and_rating_floor_never_goes_below_zero() -> None:
    draw = service(
        game=finished_draw_game(),
        participants=roster(black_rating=1200, white_rating=1000),
    )
    result = draw.finalize_completed_game()

    assert [item.outcome for item in result.rating_adjustments] == [
        MemberOutcome.DRAW,
        MemberOutcome.DRAW,
    ]
    assert result.rating_adjustments[0].rating_delta < 0
    assert result.rating_adjustments[1].rating_delta > 0

    floor = service(
        participants=(
            participant("black-member", Stone.BLACK, member_id=1, rating=1000),
            participant("white-low", Stone.WHITE, member_id=2, rating=1),
            participant("white-high", Stone.WHITE, member_id=3, rating=2000),
        ),
    )
    floor_result = floor.finalize_confirmed_departures(
        departed_participant_ids=frozenset({"white-low", "white-high"}),
    )
    white = next(item for item in floor_result.rating_adjustments if item.member_id == 2)
    assert white.rating_delta < 0
    assert white.rating_after == 0


def test_rating_delta_rounds_half_away_from_zero() -> None:
    assert round_rating_delta(Decimal("0.5")) == 1
    assert round_rating_delta(Decimal("-0.5")) == -1
