"""Provider-neutral Vote, Turn, and resolver runtime contract."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from seokpan.game.domain import AppliedMove, BoardCell, Coordinate, EndReason, GameStatus, Stone
from seokpan.vote.domain import (
    TurnClosure,
    TurnResolution,
    TurnResultKind,
    TurnStatus,
    Vote,
    Voter,
    VoteRuleViolation,
    VoteTally,
)

VOTE_RUNTIME_SCHEMA_VERSION = 3
RESOLVER_LEASE_MS = 5 * 1000
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _identifier(value: str, *, code: str) -> None:
    if _SAFE_ID.fullmatch(value) is None:
        raise VoteRuleViolation(code)


def _positive(value: int, *, code: str) -> None:
    if value < 1:
        raise VoteRuleViolation(code)


def _base(room_id: str, request_id: str, game_id: str) -> None:
    _identifier(room_id, code="INVALID_ROOM_ID")
    _identifier(request_id, code="INVALID_REQUEST_ID")
    _identifier(game_id, code="INVALID_GAME_ID")


@dataclass(frozen=True, slots=True)
class InitializeVoteRuntime:
    room_id: str
    request_id: str
    game_id: str
    participants: tuple[Voter, ...]
    deadline_ms: int
    expected_state_version: int
    previous_game_id: str | None = None
    previous_turn_no: int | None = None

    def __post_init__(self) -> None:
        _base(self.room_id, self.request_id, self.game_id)
        _positive(self.expected_state_version, code="INVALID_STATE_VERSION")
        if self.deadline_ms < 0:
            raise VoteRuleViolation("INVALID_DEADLINE")
        if (self.previous_game_id is None) != (self.previous_turn_no is None):
            raise VoteRuleViolation("INVALID_PREVIOUS_GAME")
        if self.previous_game_id is not None:
            _identifier(self.previous_game_id, code="INVALID_PREVIOUS_GAME")
            if self.previous_game_id == self.game_id:
                raise VoteRuleViolation("INVALID_PREVIOUS_GAME")
        if self.previous_turn_no is not None:
            _positive(self.previous_turn_no, code="INVALID_TURN_NUMBER")


@dataclass(frozen=True, slots=True)
class CastRuntimeVote:
    room_id: str
    request_id: str
    game_id: str
    turn_no: int
    participant_id: str
    coordinate: Coordinate
    expected_state_version: int

    def __post_init__(self) -> None:
        _base(self.room_id, self.request_id, self.game_id)
        _positive(self.turn_no, code="INVALID_TURN_NUMBER")
        _identifier(self.participant_id, code="INVALID_PARTICIPANT_ID")
        _positive(self.expected_state_version, code="INVALID_STATE_VERSION")


@dataclass(frozen=True, slots=True)
class RemoveRuntimeVote:
    room_id: str
    request_id: str
    game_id: str
    turn_no: int
    participant_id: str
    expected_state_version: int

    def __post_init__(self) -> None:
        _base(self.room_id, self.request_id, self.game_id)
        _positive(self.turn_no, code="INVALID_TURN_NUMBER")
        _identifier(self.participant_id, code="INVALID_PARTICIPANT_ID")
        _positive(self.expected_state_version, code="INVALID_STATE_VERSION")


@dataclass(frozen=True, slots=True)
class CloseRuntimeTurn:
    room_id: str
    request_id: str
    game_id: str
    turn_no: int
    expected_state_version: int
    next_deadline_ms: int | None = None

    def __post_init__(self) -> None:
        _base(self.room_id, self.request_id, self.game_id)
        _positive(self.turn_no, code="INVALID_TURN_NUMBER")
        _positive(self.expected_state_version, code="INVALID_STATE_VERSION")


@dataclass(frozen=True, slots=True)
class AcquireRuntimeResolver:
    room_id: str
    request_id: str
    game_id: str
    turn_no: int
    resolution_id: str
    expected_state_version: int

    def __post_init__(self) -> None:
        _base(self.room_id, self.request_id, self.game_id)
        _positive(self.turn_no, code="INVALID_TURN_NUMBER")
        _identifier(self.resolution_id, code="INVALID_RESOLUTION_ID")
        _positive(self.expected_state_version, code="INVALID_STATE_VERSION")


@dataclass(frozen=True, slots=True)
class ApplyRuntimeResolution:
    room_id: str
    request_id: str
    game_id: str
    turn_no: int
    resolution_id: str
    resolution: TurnResolution
    expected_state_version: int
    persistence_confirmed: bool
    next_deadline_ms: int | None = None

    def __post_init__(self) -> None:
        _base(self.room_id, self.request_id, self.game_id)
        _positive(self.turn_no, code="INVALID_TURN_NUMBER")
        _identifier(self.resolution_id, code="INVALID_RESOLUTION_ID")
        _positive(self.expected_state_version, code="INVALID_STATE_VERSION")
        if not self.persistence_confirmed:
            raise VoteRuleViolation("PERSISTENCE_CONFIRMATION_REQUIRED")
        if self.resolution.game_id != self.game_id or self.resolution.turn_no != self.turn_no:
            raise VoteRuleViolation("RESOLUTION_MISMATCH")
        move = self.resolution.applied_move
        move_resolution = (
            self.resolution.result is TurnResultKind.MOVE_APPLIED
            and self.resolution.status is TurnStatus.MOVE_APPLIED
            and self.resolution.selected_coordinate is not None
            and move is not None
            and move.team is self.resolution.team
            and move.coordinate == self.resolution.selected_coordinate
        )
        joint_loss_resolution = (
            self.resolution.result is TurnResultKind.JOINT_LOSS
            and self.resolution.status is TurnStatus.PASSED
            and self.resolution.selected_coordinate is None
            and move is None
            and self.resolution.end_reason is EndReason.JOINT_LOSS
        )
        if not move_resolution and not joint_loss_resolution:
            raise VoteRuleViolation("RESOLUTION_MISMATCH")
        if self.resolution.end_reason is None and self.next_deadline_ms is None:
            raise VoteRuleViolation("INVALID_NEXT_DEADLINE")


@dataclass(frozen=True, slots=True)
class FinalizeRuntimeGame:
    room_id: str
    request_id: str
    game_id: str
    turn_no: int
    expected_state_version: int
    end_reason: EndReason
    winner: Stone

    def __post_init__(self) -> None:
        _base(self.room_id, self.request_id, self.game_id)
        _positive(self.turn_no, code="INVALID_TURN_NUMBER")
        _positive(self.expected_state_version, code="INVALID_STATE_VERSION")
        valid = (
            self.end_reason is EndReason.FORFEIT and self.winner in {Stone.BLACK, Stone.WHITE}
        ) or (
            self.end_reason in {EndReason.JOINT_LOSS, EndReason.SYSTEM_INVALID}
            and self.winner is Stone.EMPTY
        )
        if not valid:
            raise VoteRuleViolation("INVALID_EXTERNAL_GAME_RESULT")


@dataclass(frozen=True, slots=True)
class ResolverLease:
    resolution_id: str
    expires_at_ms: int


@dataclass(frozen=True, slots=True)
class VoteRuntimeSnapshot:
    room_id: str
    game_id: str
    state_version: int
    turn_no: int
    turn_status: TurnStatus
    current_team: Stone
    deadline_ms: int | None
    consecutive_passes: int
    move_no: int
    game_status: GameStatus
    end_reason: EndReason | None
    participants: tuple[Voter, ...]
    votes: tuple[Vote, ...]
    tally: tuple[VoteTally, ...]
    candidates: tuple[Coordinate, ...]
    occupied_cells: tuple[BoardCell, ...]
    resolver: ResolverLease | None
    last_move: AppliedMove | None = None
    valid_voter_count: int | None = None
    schema_version: int = VOTE_RUNTIME_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class VoteMutationResult:
    snapshot: VoteRuntimeSnapshot
    replayed: bool = False
    closure: TurnClosure | None = None
    resolution: TurnResolution | None = None
    valid_voter_count: int | None = None


class VoteRuntimePort(Protocol):
    async def initialize(self, command: InitializeVoteRuntime) -> VoteMutationResult: ...

    async def get(self, room_id: str) -> VoteRuntimeSnapshot | None: ...

    async def cast_vote(self, command: CastRuntimeVote) -> VoteMutationResult: ...

    async def remove_vote(self, command: RemoveRuntimeVote) -> VoteMutationResult: ...

    async def close_turn(self, command: CloseRuntimeTurn) -> VoteMutationResult: ...

    async def acquire_resolver(self, command: AcquireRuntimeResolver) -> VoteMutationResult: ...

    async def apply_resolution(self, command: ApplyRuntimeResolution) -> VoteMutationResult: ...

    async def finalize_game(self, command: FinalizeRuntimeGame) -> VoteMutationResult: ...

    async def discard_game(self, room_id: str, game_id: str) -> None: ...
