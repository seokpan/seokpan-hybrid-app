"""Vote and Turn rules without clock, random, framework or provider dependencies."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, replace
from enum import StrEnum

from seokpan.game.domain import (
    AppliedMove,
    Coordinate,
    EndReason,
    Game,
    GameRuleViolation,
    GameStatus,
    Stone,
)


class ParticipantRole(StrEnum):
    PLAYER = "PLAYER"
    SPECTATOR = "SPECTATOR"


class TurnStatus(StrEnum):
    VOTING = "VOTING"
    RESOLVING = "RESOLVING"
    MOVE_APPLIED = "MOVE_APPLIED"
    PASSED = "PASSED"


class TurnResultKind(StrEnum):
    RESOLUTION_REQUIRED = "RESOLUTION_REQUIRED"
    MOVE_APPLIED = "MOVE_APPLIED"
    PASSED = "PASSED"
    JOINT_LOSS = "JOINT_LOSS"


class VoteRuleViolation(ValueError):
    """A stable Vote/Turn rejection which must not mutate state."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class Voter:
    participant_id: str
    team: Stone
    role: ParticipantRole = ParticipantRole.PLAYER
    connected: bool = True

    def __post_init__(self) -> None:
        if not self.participant_id:
            raise VoteRuleViolation("INVALID_PARTICIPANT_ID")
        if self.team is Stone.EMPTY:
            raise VoteRuleViolation("INVALID_PARTICIPANT_TEAM")


@dataclass(frozen=True, slots=True)
class Vote:
    participant_id: str
    coordinate: Coordinate


@dataclass(frozen=True, slots=True)
class VoteTally:
    coordinate: Coordinate
    count: int


@dataclass(frozen=True, slots=True)
class TurnClosure:
    game_id: str
    turn_no: int
    team: Stone
    result: TurnResultKind
    status: TurnStatus
    tally: tuple[VoteTally, ...]
    candidates: tuple[Coordinate, ...]


@dataclass(frozen=True, slots=True)
class TurnResolution:
    game_id: str
    turn_no: int
    team: Stone
    result: TurnResultKind
    status: TurnStatus
    selected_coordinate: Coordinate | None
    applied_move: AppliedMove | None
    end_reason: EndReason | None


class VoteTurnGame:
    """Server-authoritative Vote and Turn orchestration around the Game domain."""

    def __init__(
        self,
        *,
        game_id: str,
        participants: tuple[Voter, ...],
        deadline_ms: int,
        game: Game | None = None,
    ) -> None:
        if not game_id:
            raise VoteRuleViolation("INVALID_GAME_ID")
        if deadline_ms < 0:
            raise VoteRuleViolation("INVALID_DEADLINE")
        if not participants:
            raise VoteRuleViolation("PARTICIPANTS_REQUIRED")
        participant_ids = tuple(item.participant_id for item in participants)
        if len(participant_ids) != len(set(participant_ids)):
            raise VoteRuleViolation("DUPLICATE_PARTICIPANT")
        player_teams = {item.team for item in participants if item.role is ParticipantRole.PLAYER}
        if Stone.BLACK not in player_teams or Stone.WHITE not in player_teams:
            raise VoteRuleViolation("BOTH_PLAYER_TEAMS_REQUIRED")

        self.game_id = game_id
        self.game = game if game is not None else Game()
        if self.game.status is not GameStatus.ACTIVE:
            raise VoteRuleViolation("GAME_NOT_ACTIVE")
        self._participants = {item.participant_id: item for item in participants}
        self._turn_no = 1
        self._turn_status = TurnStatus.VOTING
        self._deadline_ms = deadline_ms
        self._votes: dict[str, Coordinate] = {}
        self._consecutive_passes = 0
        self._closures: dict[int, TurnClosure] = {}
        self._resolutions: dict[int, TurnResolution] = {}

    @property
    def turn_no(self) -> int:
        return self._turn_no

    @property
    def turn_status(self) -> TurnStatus:
        return self._turn_status

    @property
    def current_team(self) -> Stone:
        return self.game.current_team

    @property
    def deadline_ms(self) -> int | None:
        if self.game.status is not GameStatus.ACTIVE:
            return None
        return self._deadline_ms

    @property
    def consecutive_passes(self) -> int:
        return self._consecutive_passes

    @property
    def votes(self) -> tuple[Vote, ...]:
        return tuple(
            Vote(participant_id=participant_id, coordinate=coordinate)
            for participant_id, coordinate in sorted(self._votes.items())
        )

    @property
    def participants(self) -> tuple[Voter, ...]:
        return tuple(sorted(self._participants.values(), key=lambda item: item.participant_id))

    def cast_vote(
        self,
        *,
        game_id: str,
        turn_no: int,
        participant_id: str,
        coordinate: Coordinate | str,
        now_ms: int,
    ) -> Vote:
        self._require_voting_request(game_id=game_id, turn_no=turn_no, now_ms=now_ms)
        participant = self._require_eligible_participant(participant_id)
        parsed = self._validate_coordinate(coordinate)
        vote = Vote(participant_id=participant.participant_id, coordinate=parsed)
        self._votes[participant.participant_id] = parsed
        return vote

    def remove_vote(
        self,
        *,
        game_id: str,
        turn_no: int,
        participant_id: str,
        now_ms: int,
    ) -> None:
        self._require_voting_request(game_id=game_id, turn_no=turn_no, now_ms=now_ms)
        participant = self._require_eligible_participant(participant_id)
        self._votes.pop(participant.participant_id, None)

    def disconnect(self, *, participant_id: str, now_ms: int) -> None:
        participant = self._participant(participant_id)
        if not participant.connected:
            return
        self._participants[participant_id] = replace(participant, connected=False)
        if self._turn_status is TurnStatus.VOTING and now_ms < self._deadline_ms:
            self._votes.pop(participant_id, None)

    def reconnect(self, *, participant_id: str) -> None:
        participant = self._participant(participant_id)
        if participant.connected:
            return
        self._participants[participant_id] = replace(participant, connected=True)

    def close_voting(
        self,
        *,
        game_id: str,
        turn_no: int,
        now_ms: int,
        next_deadline_ms: int | None = None,
    ) -> TurnClosure:
        self._require_game_id(game_id)
        existing = self._closures.get(turn_no)
        if existing is not None:
            return existing
        self._require_current_turn(turn_no)
        if self.game.status is not GameStatus.ACTIVE:
            raise VoteRuleViolation("GAME_NOT_ACTIVE")
        if now_ms < self._deadline_ms:
            raise VoteRuleViolation("TURN_DEADLINE_NOT_REACHED")

        tally = self._tally()
        if not tally:
            return self._close_as_pass(next_deadline_ms=next_deadline_ms)

        highest_count = tally[0].count
        candidates = tuple(item.coordinate for item in tally if item.count == highest_count)
        self._turn_status = TurnStatus.RESOLVING
        closure = TurnClosure(
            game_id=self.game_id,
            turn_no=self._turn_no,
            team=self.current_team,
            result=TurnResultKind.RESOLUTION_REQUIRED,
            status=TurnStatus.RESOLVING,
            tally=tally,
            candidates=candidates,
        )
        self._closures[self._turn_no] = closure
        return closure

    def resolve_move(
        self,
        *,
        game_id: str,
        turn_no: int,
        selected_coordinate: Coordinate | str | None,
        next_deadline_ms: int | None = None,
    ) -> TurnResolution:
        self._require_game_id(game_id)
        existing = self._resolutions.get(turn_no)
        if existing is not None:
            self._require_same_replayed_selection(existing, selected_coordinate)
            return existing
        self._require_current_turn(turn_no)
        if self._turn_status is not TurnStatus.RESOLVING:
            raise VoteRuleViolation("TURN_NOT_RESOLVING")
        closure = self._closures[self._turn_no]
        selected = self._select_candidate(closure.candidates, selected_coordinate)

        try:
            resolved_game = deepcopy(self.game)
            outcome = resolved_game.apply_move(team=closure.team, coordinate=selected)
        except GameRuleViolation as error:
            raise VoteRuleViolation(error.code) from error
        if outcome.status is GameStatus.ACTIVE:
            self._require_next_deadline(next_deadline_ms)

        self.game = resolved_game
        self._consecutive_passes = 0
        self._turn_status = TurnStatus.MOVE_APPLIED
        resolution = TurnResolution(
            game_id=self.game_id,
            turn_no=self._turn_no,
            team=closure.team,
            result=TurnResultKind.MOVE_APPLIED,
            status=TurnStatus.MOVE_APPLIED,
            selected_coordinate=selected,
            applied_move=outcome.move,
            end_reason=outcome.end_reason,
        )
        self._resolutions[self._turn_no] = resolution
        if outcome.status is GameStatus.ACTIVE:
            assert next_deadline_ms is not None
            self._advance_turn(next_deadline_ms)
        return resolution

    def resolve_joint_loss(self, *, game_id: str, turn_no: int) -> TurnResolution:
        """Confirm a consecutive zero-vote loss after its durable Result exists."""
        self._require_game_id(game_id)
        existing = self._resolutions.get(turn_no)
        if existing is not None:
            if existing.result is not TurnResultKind.JOINT_LOSS:
                raise VoteRuleViolation("RESOLUTION_ALREADY_APPLIED")
            return existing
        self._require_current_turn(turn_no)
        if self._turn_status is not TurnStatus.RESOLVING:
            raise VoteRuleViolation("TURN_NOT_RESOLVING")
        closure = self._closures[self._turn_no]
        if closure.result is not TurnResultKind.JOINT_LOSS:
            raise VoteRuleViolation("RESOLUTION_MISMATCH")

        self.game.finish_joint_loss()
        self._consecutive_passes = 2
        self._turn_status = TurnStatus.PASSED
        resolution = TurnResolution(
            game_id=self.game_id,
            turn_no=self._turn_no,
            team=closure.team,
            result=TurnResultKind.JOINT_LOSS,
            status=TurnStatus.PASSED,
            selected_coordinate=None,
            applied_move=None,
            end_reason=EndReason.JOINT_LOSS,
        )
        self._resolutions[self._turn_no] = resolution
        return resolution

    def finalize_external_result(self, *, end_reason: EndReason, winner: Stone) -> None:
        """Apply a provider-confirmed non-board result and stop accepting votes."""
        if self.game.status is not GameStatus.ACTIVE:
            conclusion = self.game.conclusion
            if (
                conclusion is not None
                and conclusion.end_reason is end_reason
                and conclusion.winner is winner
            ):
                return
            raise VoteRuleViolation("GAME_ALREADY_FINISHED")
        try:
            if end_reason is EndReason.FORFEIT:
                if winner not in {Stone.BLACK, Stone.WHITE}:
                    raise VoteRuleViolation("INVALID_EXTERNAL_GAME_RESULT")
                losing_team = Stone.WHITE if winner is Stone.BLACK else Stone.BLACK
                self.game.finish_forfeit(losing_team=losing_team)
            elif end_reason is EndReason.JOINT_LOSS and winner is Stone.EMPTY:
                self.game.finish_joint_loss()
            elif end_reason is EndReason.SYSTEM_INVALID and winner is Stone.EMPTY:
                self.game.finish_system_invalid()
            else:
                raise VoteRuleViolation("INVALID_EXTERNAL_GAME_RESULT")
        except GameRuleViolation as error:
            raise VoteRuleViolation(error.code) from error
        self._votes.clear()
        self._turn_status = TurnStatus.PASSED

    def _close_as_pass(self, *, next_deadline_ms: int | None) -> TurnClosure:
        next_passes = self._consecutive_passes + 1
        if next_passes < 2:
            self._require_next_deadline(next_deadline_ms)

        team = self.current_team
        result = TurnResultKind.JOINT_LOSS if next_passes == 2 else TurnResultKind.PASSED
        self._turn_status = (
            TurnStatus.RESOLVING if result is TurnResultKind.JOINT_LOSS else TurnStatus.PASSED
        )

        closure = TurnClosure(
            game_id=self.game_id,
            turn_no=self._turn_no,
            team=team,
            result=result,
            status=self._turn_status,
            tally=(),
            candidates=(),
        )
        self._closures[self._turn_no] = closure
        if result is TurnResultKind.PASSED:
            self._consecutive_passes = next_passes
            assert next_deadline_ms is not None
            self.game.pass_turn()
            self._advance_turn(next_deadline_ms)
        return closure

    @staticmethod
    def select_candidate(
        candidates: tuple[Coordinate, ...],
        selected_coordinate: Coordinate | str | None,
    ) -> Coordinate:
        """Validate a provider-selected tie candidate with the Domain rule."""
        return VoteTurnGame._select_candidate(candidates, selected_coordinate)

    def _require_voting_request(self, *, game_id: str, turn_no: int, now_ms: int) -> None:
        self._require_game_id(game_id)
        self._require_current_turn(turn_no)
        if self.game.status is not GameStatus.ACTIVE:
            raise VoteRuleViolation("GAME_NOT_ACTIVE")
        if self._turn_status is not TurnStatus.VOTING:
            raise VoteRuleViolation("TURN_NOT_VOTING")
        if now_ms >= self._deadline_ms:
            raise VoteRuleViolation("TURN_DEADLINE_REACHED")

    def _require_game_id(self, game_id: str) -> None:
        if game_id != self.game_id:
            raise VoteRuleViolation("STALE_GAME")

    def _require_current_turn(self, turn_no: int) -> None:
        if turn_no != self._turn_no:
            raise VoteRuleViolation("STALE_TURN")

    def _participant(self, participant_id: str) -> Voter:
        try:
            return self._participants[participant_id]
        except KeyError as error:
            raise VoteRuleViolation("PARTICIPANT_NOT_FOUND") from error

    def _require_eligible_participant(self, participant_id: str) -> Voter:
        participant = self._participant(participant_id)
        if participant.role is not ParticipantRole.PLAYER:
            raise VoteRuleViolation("PLAYER_REQUIRED")
        if not participant.connected:
            raise VoteRuleViolation("PARTICIPANT_DISCONNECTED")
        if participant.team is not self.current_team:
            raise VoteRuleViolation("CURRENT_TEAM_REQUIRED")
        return participant

    def _validate_coordinate(self, coordinate: Coordinate | str) -> Coordinate:
        try:
            parsed = (
                coordinate if isinstance(coordinate, Coordinate) else Coordinate.parse(coordinate)
            )
            if self.game.stone_at(parsed) is not Stone.EMPTY:
                raise VoteRuleViolation("POSITION_OCCUPIED")
            if self.current_team is Stone.BLACK:
                reason = self.game.black_forbidden_reason(parsed)
                if reason is not None:
                    raise VoteRuleViolation(f"BLACK_{reason.value}")
        except GameRuleViolation as error:
            raise VoteRuleViolation(error.code) from error
        return parsed

    def _tally(self) -> tuple[VoteTally, ...]:
        counts = Counter(self._votes.values())
        return tuple(
            VoteTally(coordinate=coordinate, count=count)
            for coordinate, count in sorted(
                counts.items(),
                key=lambda item: (-item[1], item[0].canonical),
            )
        )

    @staticmethod
    def _select_candidate(
        candidates: tuple[Coordinate, ...],
        selected_coordinate: Coordinate | str | None,
    ) -> Coordinate:
        if len(candidates) == 1:
            only = candidates[0]
            if selected_coordinate is None:
                return only
            selected = VoteTurnGame._parse_selected_coordinate(selected_coordinate)
            if selected != only:
                raise VoteRuleViolation("INVALID_RESOLUTION_CANDIDATE")
            return selected
        if selected_coordinate is None:
            raise VoteRuleViolation("TIE_SELECTION_REQUIRED")
        selected = VoteTurnGame._parse_selected_coordinate(selected_coordinate)
        if selected not in candidates:
            raise VoteRuleViolation("INVALID_RESOLUTION_CANDIDATE")
        return selected

    @staticmethod
    def _parse_selected_coordinate(
        selected_coordinate: Coordinate | str,
    ) -> Coordinate:
        try:
            return (
                selected_coordinate
                if isinstance(selected_coordinate, Coordinate)
                else Coordinate.parse(selected_coordinate)
            )
        except GameRuleViolation as error:
            raise VoteRuleViolation(error.code) from error

    def _require_next_deadline(self, next_deadline_ms: int | None) -> None:
        if next_deadline_ms is None or next_deadline_ms <= self._deadline_ms:
            raise VoteRuleViolation("INVALID_NEXT_DEADLINE")

    @staticmethod
    def _require_same_replayed_selection(
        existing: TurnResolution,
        selected_coordinate: Coordinate | str | None,
    ) -> None:
        if selected_coordinate is None:
            return
        try:
            selected = (
                selected_coordinate
                if isinstance(selected_coordinate, Coordinate)
                else Coordinate.parse(selected_coordinate)
            )
        except GameRuleViolation as error:
            raise VoteRuleViolation(error.code) from error
        if selected != existing.selected_coordinate:
            raise VoteRuleViolation("RESOLUTION_ALREADY_APPLIED")

    def _advance_turn(self, next_deadline_ms: int) -> None:
        self._turn_no += 1
        self._turn_status = TurnStatus.VOTING
        self._deadline_ms = next_deadline_ms
        self._votes = {}
