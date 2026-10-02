"""Pure Game result, forfeit and Elo rating rules."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from seokpan.game.domain.model import (
    Coordinate,
    EndReason,
    Game,
    GameConclusion,
    GameRuleViolation,
    GameStatus,
    Stone,
)

INITIAL_RATING = 1000
ELO_K_FACTOR = 32


def round_rating_delta(value: Decimal) -> int:
    """Round an Elo delta to the nearest integer with ties away from zero."""
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


class GameParticipantRole(StrEnum):
    PLAYER = "PLAYER"
    SPECTATOR = "SPECTATOR"


class MemberOutcome(StrEnum):
    WIN = "WIN"
    DRAW = "DRAW"
    LOSS = "LOSS"


class GameResultRuleViolation(ValueError):
    """A stable result rejection which must not mutate Game state."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class GameParticipantSnapshot:
    participant_id: str
    team: Stone
    role: GameParticipantRole
    member_id: int | None = None
    rating: int | None = None

    def __post_init__(self) -> None:
        if not self.participant_id:
            raise GameResultRuleViolation("INVALID_PARTICIPANT_ID")
        if self.role is GameParticipantRole.PLAYER and self.team is Stone.EMPTY:
            raise GameResultRuleViolation("INVALID_PLAYER_TEAM")
        if self.member_id is None:
            if self.rating is not None:
                raise GameResultRuleViolation("GUEST_RATING_NOT_ALLOWED")
            return
        if self.member_id <= 0:
            raise GameResultRuleViolation("INVALID_MEMBER_ID")
        if self.rating is None or self.rating < 0:
            raise GameResultRuleViolation("INVALID_MEMBER_RATING")


@dataclass(frozen=True, slots=True)
class RatingAdjustment:
    participant_id: str
    member_id: int
    team: Stone
    outcome: MemberOutcome
    rating_before: int
    rating_delta: int
    rating_after: int


@dataclass(frozen=True, slots=True)
class GameResult:
    game_id: str
    status: GameStatus
    end_reason: EndReason
    winner: Stone
    winning_line: tuple[Coordinate, ...]
    stats_eligible: bool
    rating_adjustments: tuple[RatingAdjustment, ...]


class GameResultService:
    """Adjudicate a Game from provider-confirmed facts and plan Member updates."""

    def __init__(
        self,
        *,
        game_id: str,
        game: Game,
        participants: tuple[GameParticipantSnapshot, ...],
    ) -> None:
        if not game_id:
            raise GameResultRuleViolation("INVALID_GAME_ID")
        if not participants:
            raise GameResultRuleViolation("PARTICIPANTS_REQUIRED")
        participant_ids = tuple(item.participant_id for item in participants)
        if len(participant_ids) != len(set(participant_ids)):
            raise GameResultRuleViolation("DUPLICATE_PARTICIPANT")
        member_ids = tuple(item.member_id for item in participants if item.member_id is not None)
        if len(member_ids) != len(set(member_ids)):
            raise GameResultRuleViolation("DUPLICATE_MEMBER")
        player_teams = {
            item.team for item in participants if item.role is GameParticipantRole.PLAYER
        }
        if Stone.BLACK not in player_teams or Stone.WHITE not in player_teams:
            raise GameResultRuleViolation("BOTH_PLAYER_TEAMS_REQUIRED")

        self.game_id = game_id
        self.game = game
        self._participants = participants
        self._result: GameResult | None = None

    @property
    def result(self) -> GameResult | None:
        return self._result

    def finalize_completed_game(self) -> GameResult:
        """Build the result of a win, draw or joint loss already decided by Game."""
        if self.game.status is GameStatus.ACTIVE:
            raise GameResultRuleViolation("GAME_NOT_FINISHED")
        return self._finalize_current_conclusion()

    def finalize_confirmed_departures(
        self,
        *,
        departed_participant_ids: frozenset[str],
    ) -> GameResult:
        """Finish only after the provider has confirmed participant departures."""
        known_ids = {item.participant_id for item in self._participants}
        if not departed_participant_ids <= known_ids:
            raise GameResultRuleViolation("PARTICIPANT_NOT_FOUND")

        black_departed = self._all_players_departed(
            team=Stone.BLACK,
            departed_participant_ids=departed_participant_ids,
        )
        white_departed = self._all_players_departed(
            team=Stone.WHITE,
            departed_participant_ids=departed_participant_ids,
        )
        try:
            if black_departed and white_departed:
                self.game.finish_joint_loss()
            elif black_departed:
                self.game.finish_forfeit(losing_team=Stone.BLACK)
            elif white_departed:
                self.game.finish_forfeit(losing_team=Stone.WHITE)
            else:
                raise GameResultRuleViolation("FORFEIT_NOT_CONFIRMED")
        except GameRuleViolation as error:
            raise GameResultRuleViolation(error.code) from error
        return self._finalize_current_conclusion()

    def finalize_system_invalid(self) -> GameResult:
        """Finish without stats after a provider confirms recovery is impossible."""
        try:
            self.game.finish_system_invalid()
        except GameRuleViolation as error:
            raise GameResultRuleViolation(error.code) from error
        return self._finalize_current_conclusion()

    def _all_players_departed(
        self,
        *,
        team: Stone,
        departed_participant_ids: frozenset[str],
    ) -> bool:
        player_ids = {
            item.participant_id
            for item in self._participants
            if item.role is GameParticipantRole.PLAYER and item.team is team
        }
        return player_ids <= departed_participant_ids

    def _finalize_current_conclusion(self) -> GameResult:
        conclusion = self.game.conclusion
        assert conclusion is not None
        if self._result is not None:
            return self._result

        adjustments = self._rating_adjustments(conclusion)
        self._result = GameResult(
            game_id=self.game_id,
            status=conclusion.status,
            end_reason=conclusion.end_reason,
            winner=conclusion.winner,
            winning_line=conclusion.winning_line,
            stats_eligible=conclusion.status is not GameStatus.SYSTEM_INVALID,
            rating_adjustments=adjustments,
        )
        return self._result

    def _rating_adjustments(
        self,
        conclusion: GameConclusion,
    ) -> tuple[RatingAdjustment, ...]:
        if conclusion.status is GameStatus.SYSTEM_INVALID:
            return ()

        team_averages = {
            Stone.BLACK: self._team_average(Stone.BLACK),
            Stone.WHITE: self._team_average(Stone.WHITE),
        }
        adjustments: list[RatingAdjustment] = []
        for participant in sorted(self._participants, key=lambda item: item.participant_id):
            if (
                participant.role is not GameParticipantRole.PLAYER
                or participant.member_id is None
                or participant.rating is None
            ):
                continue
            score = self._score(conclusion, participant.team)
            opponent = Stone.WHITE if participant.team is Stone.BLACK else Stone.BLACK
            expected = Decimal(1) / (
                Decimal(1)
                + Decimal(10)
                ** ((team_averages[opponent] - team_averages[participant.team]) / Decimal(400))
            )
            rating_delta = round_rating_delta(Decimal(ELO_K_FACTOR) * (score - expected))
            rating_after = max(0, participant.rating + rating_delta)
            adjustments.append(
                RatingAdjustment(
                    participant_id=participant.participant_id,
                    member_id=participant.member_id,
                    team=participant.team,
                    outcome=self._member_outcome(conclusion, participant.team),
                    rating_before=participant.rating,
                    rating_delta=rating_delta,
                    rating_after=rating_after,
                )
            )
        return tuple(adjustments)

    def _team_average(self, team: Stone) -> Decimal:
        ratings = [
            Decimal(item.rating if item.rating is not None else INITIAL_RATING)
            for item in self._participants
            if item.role is GameParticipantRole.PLAYER and item.team is team
        ]
        return sum(ratings, Decimal(0)) / Decimal(len(ratings))

    @staticmethod
    def _score(conclusion: GameConclusion, team: Stone) -> Decimal:
        if conclusion.end_reason is EndReason.DRAW:
            return Decimal("0.5")
        if conclusion.end_reason is EndReason.JOINT_LOSS:
            return Decimal(0)
        return Decimal(1) if conclusion.winner is team else Decimal(0)

    @staticmethod
    def _member_outcome(conclusion: GameConclusion, team: Stone) -> MemberOutcome:
        if conclusion.end_reason is EndReason.DRAW:
            return MemberOutcome.DRAW
        if conclusion.winner is team:
            return MemberOutcome.WIN
        return MemberOutcome.LOSS
