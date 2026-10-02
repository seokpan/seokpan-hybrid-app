"""Shared Fake rating state is not a substitute for MariaDB transactions."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from seokpan.game.application import (
    FinalizeGameCommand,
    GameParticipantRecord,
    PersistenceOutcome,
    PersistenceRuleViolation,
    StartGameCommand,
    StoredGameResult,
)
from seokpan.game.domain import (
    EndReason,
    GameResult,
    GameStatus,
    MemberOutcome,
    RatingAdjustment,
    Stone,
)
from seokpan.identity.application import CreateMember, IdentityRuleViolation
from seokpan.persistence.memory import InMemoryGamePersistenceAdapter, InMemoryIdentityAdapter

GAME_ID = "11111111-1111-4111-8111-111111111111"
ROOM_ID = "22222222-2222-4222-8222-222222222222"
BLACK_ID = "33333333-3333-4333-8333-333333333333"
WHITE_ID = "44444444-4444-4444-8444-444444444444"
NOW = datetime(2026, 9, 7, tzinfo=UTC)


def start() -> StartGameCommand:
    return StartGameCommand(
        GAME_ID,
        ROOM_ID,
        5,
        NOW,
        (
            GameParticipantRecord(BLACK_ID, Stone.BLACK, member_id=1),
            GameParticipantRecord(WHITE_ID, Stone.WHITE, member_id=2),
        ),
    )


def finish() -> FinalizeGameCommand:
    return FinalizeGameCommand(
        GameResult(
            GAME_ID,
            GameStatus.FINISHED,
            EndReason.FORFEIT,
            Stone.BLACK,
            (),
            True,
            (
                RatingAdjustment(BLACK_ID, 1, Stone.BLACK, MemberOutcome.WIN, 1000, 16, 1016),
                RatingAdjustment(WHITE_ID, 2, Stone.WHITE, MemberOutcome.LOSS, 1000, -16, 984),
            ),
        ),
        NOW,
    )


@pytest.mark.asyncio
async def test_completed_result_retains_history_after_current_rating_changes() -> None:
    games = InMemoryGamePersistenceAdapter({1: 1000, 2: 1000})
    assert await games.load_result(GAME_ID) is None
    await games.start_game(start())
    assert await games.load_result(GAME_ID) is None
    await games.finalize_game(finish())
    games.member_ratings.update({1: 1300, 2: 1200})
    expected = StoredGameResult(
        GAME_ID,
        ROOM_ID,
        GameStatus.FINISHED,
        EndReason.FORFEIT,
        Stone.BLACK,
        NOW,
        finish().result.rating_adjustments,
    )
    assert await games.load_result(GAME_ID) == expected
    assert await games.load_result(GAME_ID) == expected
    assert games.member_ratings == {1: 1300, 2: 1200}


@pytest.mark.asyncio
async def test_completed_invalid_result_does_not_invent_rating_history() -> None:
    games = InMemoryGamePersistenceAdapter({1: 1000, 2: 1000})
    await games.start_game(start())
    command = replace(
        finish(),
        result=replace(
            finish().result,
            status=GameStatus.SYSTEM_INVALID,
            end_reason=EndReason.SYSTEM_INVALID,
            winner=Stone.EMPTY,
            stats_eligible=False,
            rating_adjustments=(),
        ),
    )
    await games.finalize_game(command)
    stored = await games.load_result(GAME_ID)
    assert stored is not None
    assert not stored.stats_eligible
    assert stored.rating_adjustments == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", ["missing_start", "game_id", "missing_rating", "participant"])
async def test_completed_result_rejects_incomplete_fake_history(broken: str) -> None:
    games = InMemoryGamePersistenceAdapter({1: 1000, 2: 1000})
    await games.start_game(start())
    await games.finalize_game(finish())
    result = finish().result
    if broken == "missing_start":
        del games.games[GAME_ID]
    elif broken == "game_id":
        result = replace(result, game_id=ROOM_ID)
    elif broken == "missing_rating":
        result = replace(result, rating_adjustments=result.rating_adjustments[:1])
    else:
        result = replace(
            result,
            rating_adjustments=(
                replace(result.rating_adjustments[0], participant_id=WHITE_ID),
                result.rating_adjustments[1],
            ),
        )
    games.results[GAME_ID] = replace(finish(), result=result)
    with pytest.raises(PersistenceRuleViolation, match="GAME_RESULT_INCOMPLETE"):
        await games.load_result(GAME_ID)


@pytest.mark.parametrize(
    "broken",
    [
        "status",
        "system_invalid_rating",
        "duplicate_member",
        "duplicate_participant",
        "member_id",
        "team",
        "before",
        "after",
        "outcome",
    ],
)
def test_stored_result_rejects_inconsistent_history(broken: str) -> None:
    result = StoredGameResult(
        GAME_ID,
        ROOM_ID,
        GameStatus.FINISHED,
        EndReason.FORFEIT,
        Stone.BLACK,
        NOW,
        finish().result.rating_adjustments,
    )
    first, second = result.rating_adjustments
    with pytest.raises(PersistenceRuleViolation, match="GAME_RESULT_INCOMPLETE"):
        if broken == "status":
            replace(result, status=GameStatus.ACTIVE)
        elif broken == "system_invalid_rating":
            replace(
                result,
                status=GameStatus.SYSTEM_INVALID,
                end_reason=EndReason.SYSTEM_INVALID,
                winner=Stone.EMPTY,
            )
        elif broken == "duplicate_member":
            replace(result, rating_adjustments=(first, replace(second, member_id=1)))
        elif broken == "duplicate_participant":
            replace(result, rating_adjustments=(first, replace(second, participant_id=BLACK_ID)))
        else:
            replacement = {
                "member_id": replace(first, member_id=0),
                "team": replace(first, team=Stone.EMPTY),
                "before": replace(first, rating_before=-1),
                "after": replace(first, rating_after=1000),
                "outcome": replace(first, outcome=MemberOutcome.LOSS),
            }[broken]
            replace(result, rating_adjustments=(replacement, second))


def test_stored_result_preserves_zero_floor_and_draw_outcome() -> None:
    first = replace(
        finish().result.rating_adjustments[0],
        outcome=MemberOutcome.DRAW,
        rating_before=0,
        rating_delta=-16,
        rating_after=0,
    )
    result = StoredGameResult(
        GAME_ID,
        ROOM_ID,
        GameStatus.FINISHED,
        EndReason.DRAW,
        Stone.EMPTY,
        NOW,
        (first,),
    )
    assert result.rating_adjustments[0].rating_delta == -16
    assert result.outcome_for(Stone.WHITE) is MemberOutcome.DRAW


@pytest.mark.asyncio
async def test_registered_ratings_and_finished_ratings_share_one_fake_state() -> None:
    ratings: dict[int, int] = {}
    identity = InMemoryIdentityAdapter(member_ratings=ratings)
    games = InMemoryGamePersistenceAdapter(ratings)
    await identity.create(CreateMember("black", "Black", "fake-hash"))
    await identity.create(CreateMember("white", "White", "fake-hash"))
    await games.start_game(start())
    before = await games.load_game(GAME_ID)
    assert before is not None
    assert tuple(p.rating for p in before.participants) == (1000, 1000)
    assert await games.finalize_game(finish()) is PersistenceOutcome.CREATED
    assert await games.finalize_game(finish()) is PersistenceOutcome.UNCHANGED
    assert ratings == {1: 1016, 2: 984}
    for stored in (
        await identity.find_by_login_id("black"),
        await identity.find_by_nickname("Black"),
        await identity.find_by_member_id(1),
    ):
        assert stored is not None
        assert stored.member.rating == 1016
        assert stored.password_hash == "fake-hash"
    # Rechecking the first result must retain its pre-result ratings.
    assert await games.load_game(GAME_ID) == before
    next_start = replace(start(), game_id="55555555-5555-4555-8555-555555555555")
    await games.start_game(next_start)
    next_game = await games.load_game(next_start.game_id)
    assert next_game is not None
    assert tuple(p.rating for p in next_game.participants) == (1016, 984)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ratings", "error"),
    [({1: 1000}, "MEMBER_NOT_FOUND"), ({1: 1000, 2: 999}, "STALE_MEMBER_RATING")],
)
async def test_invalid_later_member_leaves_all_ratings_and_result_unchanged(
    ratings: dict[int, int], error: str
) -> None:
    games = InMemoryGamePersistenceAdapter(ratings)
    await games.start_game(start())
    before = dict(ratings)
    with pytest.raises(PersistenceRuleViolation, match=f"^{error}$"):
        await games.finalize_game(finish())
    assert ratings == before
    assert games.results == {}


@pytest.mark.asyncio
async def test_missing_member_rating_is_not_silently_recreated() -> None:
    ratings: dict[int, int] = {}
    identity = InMemoryIdentityAdapter(member_ratings=ratings)
    games = InMemoryGamePersistenceAdapter(ratings)
    await identity.create(CreateMember("black", "Black", "fake-hash"))
    del ratings[1]
    with pytest.raises(IdentityRuleViolation, match="^IDENTITY_PROVIDER_UNAVAILABLE$"):
        await identity.find_by_member_id(1)
    await games.start_game(start())
    with pytest.raises(PersistenceRuleViolation, match="^MEMBER_RATING_NOT_FOUND$"):
        await games.load_game(GAME_ID)
    assert ratings == {}
    assert await identity.find_by_member_id(999) is None


@pytest.mark.asyncio
async def test_system_invalid_does_not_change_ratings() -> None:
    ratings = {1: 1000, 2: 1000}
    games = InMemoryGamePersistenceAdapter(ratings)
    await games.start_game(start())
    command = FinalizeGameCommand(
        GameResult(
            GAME_ID, GameStatus.SYSTEM_INVALID, EndReason.SYSTEM_INVALID, Stone.EMPTY, (), False, ()
        ),
        NOW,
    )
    assert await games.finalize_game(command) is PersistenceOutcome.CREATED
    assert await games.finalize_game(command) is PersistenceOutcome.UNCHANGED
    assert ratings == {1: 1000, 2: 1000}


@pytest.mark.asyncio
async def test_repeated_history_commands_reject_different_start_or_result() -> None:
    ratings = {1: 1000, 2: 1000}
    games = InMemoryGamePersistenceAdapter(ratings)
    assert await games.load_game(GAME_ID) is None
    assert await games.result_matches(finish()) is False
    assert await games.game_is_finalized(GAME_ID) is False
    with pytest.raises(PersistenceRuleViolation, match="^GAME_NOT_FOUND$"):
        await games.finalize_game(finish())
    await games.start_game(start())
    assert await games.start_game(start()) is PersistenceOutcome.UNCHANGED
    with pytest.raises(PersistenceRuleViolation, match="^GAME_START_CONFLICT$"):
        await games.start_game(replace(start(), voting_time_seconds=10))
    await games.finalize_game(finish())
    assert await games.result_matches(finish()) is True
    assert await games.game_is_finalized(GAME_ID) is True
    changed = replace(finish(), ended_at=datetime(2026, 9, 8, tzinfo=UTC))
    with pytest.raises(PersistenceRuleViolation, match="^GAME_RESULT_CONFLICT$"):
        await games.finalize_game(changed)
    with pytest.raises(PersistenceRuleViolation, match="^GAME_RESULT_CONFLICT$"):
        await games.result_matches(changed)
    assert games.results == {GAME_ID: finish()}
    assert ratings == {1: 1016, 2: 984}


@pytest.mark.asyncio
async def test_duplicate_registration_cannot_overwrite_changed_rating() -> None:
    with pytest.raises(ValueError, match="first_member_id must be positive"):
        InMemoryIdentityAdapter(first_member_id=0)
    ratings: dict[int, int] = {}
    identity = InMemoryIdentityAdapter(member_ratings=ratings)
    command = CreateMember("black", "Black", "fake-hash")
    await identity.create(command)
    ratings[1] = 1016
    with pytest.raises(IdentityRuleViolation, match="^LOGIN_ID_ALREADY_EXISTS$"):
        await identity.create(command)
    with pytest.raises(IdentityRuleViolation, match="^NICKNAME_ALREADY_EXISTS$"):
        await identity.create(replace(command, login_id="other"))
    assert ratings == {1: 1016}
