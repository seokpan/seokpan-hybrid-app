"""Provider-neutral Room runtime-state contract."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from seokpan.room.domain import (
    ActorType,
    DepartureResult,
    GameTermination,
    RoomConfig,
    RoomRuleViolation,
    RoomStatus,
    RoomVisibility,
    StartRoster,
    Team,
)

ROOM_RUNTIME_SCHEMA_VERSION = 3
ROOM_DISCONNECT_LEASE_MS = 10 * 1000
ROOM_CLOSED_TOMBSTONE_TTL_MS = 10 * 60 * 1000
ROOM_REQUEST_DEDUPE_TTL_MS = 24 * 60 * 60 * 1000
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_ARGON2ID_PREFIX = "$argon2id$"


def _validate_identifier(value: str, *, code: str) -> None:
    if _SAFE_ID.fullmatch(value) is None:
        raise RoomRuleViolation(code)


def _validate_request(room_id: str, request_id: str) -> None:
    validate_room_id(room_id)
    _validate_identifier(request_id, code="INVALID_REQUEST_ID")


def validate_room_id(room_id: str) -> None:
    _validate_identifier(room_id, code="INVALID_ROOM_ID")


def _validate_session_digest(value: str) -> None:
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise RoomRuleViolation("INVALID_SESSION_DIGEST")


def _validate_state_version(value: int) -> None:
    if value < 1:
        raise RoomRuleViolation("INVALID_STATE_VERSION")


@dataclass(frozen=True, slots=True)
class CreateRoomRuntime:
    room_id: str
    request_id: str
    config: RoomConfig
    owner_id: str
    owner_session_digest: str
    encoded_password: str | None = None

    def __post_init__(self) -> None:
        _validate_request(self.room_id, self.request_id)
        _validate_identifier(self.owner_id, code="INVALID_PARTICIPANT_ID")
        _validate_session_digest(self.owner_session_digest)
        if self.config.visibility is RoomVisibility.PUBLIC and self.encoded_password is not None:
            raise RoomRuleViolation("INVALID_ROOM_PASSWORD_HASH")
        if self.config.visibility is RoomVisibility.PRIVATE and (
            self.encoded_password is None or not self.encoded_password.startswith(_ARGON2ID_PREFIX)
        ):
            raise RoomRuleViolation("INVALID_ROOM_PASSWORD_HASH")


@dataclass(frozen=True, slots=True)
class JoinRoomRuntime:
    room_id: str
    request_id: str
    participant_id: str
    actor_type: ActorType
    session_digest: str
    expected_state_version: int
    private_access_verified: bool = False

    def __post_init__(self) -> None:
        _validate_request(self.room_id, self.request_id)
        _validate_identifier(self.participant_id, code="INVALID_PARTICIPANT_ID")
        _validate_session_digest(self.session_digest)
        _validate_state_version(self.expected_state_version)


@dataclass(frozen=True, slots=True)
class ChangeRoomTeam:
    room_id: str
    request_id: str
    participant_id: str
    team: Team
    expected_state_version: int

    def __post_init__(self) -> None:
        _validate_request(self.room_id, self.request_id)
        _validate_identifier(self.participant_id, code="INVALID_PARTICIPANT_ID")
        _validate_state_version(self.expected_state_version)


@dataclass(frozen=True, slots=True)
class ChangeRoomIdentity:
    room_id: str
    request_id: str
    participant_id: str
    actor_type: ActorType
    session_digest: str
    expected_state_version: int

    def __post_init__(self) -> None:
        _validate_request(self.room_id, self.request_id)
        _validate_identifier(self.participant_id, code="INVALID_PARTICIPANT_ID")
        _validate_session_digest(self.session_digest)
        _validate_state_version(self.expected_state_version)


@dataclass(frozen=True, slots=True)
class SetRoomReady:
    room_id: str
    request_id: str
    participant_id: str
    ready: bool
    expected_state_version: int

    def __post_init__(self) -> None:
        _validate_request(self.room_id, self.request_id)
        _validate_identifier(self.participant_id, code="INVALID_PARTICIPANT_ID")
        _validate_state_version(self.expected_state_version)


@dataclass(frozen=True, slots=True)
class ChangeRoomVoteSeconds:
    room_id: str
    request_id: str
    actor_id: str
    vote_seconds: int
    expected_state_version: int

    def __post_init__(self) -> None:
        _validate_request(self.room_id, self.request_id)
        _validate_identifier(self.actor_id, code="INVALID_PARTICIPANT_ID")
        if self.vote_seconds not in {5, 10, 15, 30}:
            raise RoomRuleViolation("INVALID_VOTE_SECONDS")
        _validate_state_version(self.expected_state_version)


@dataclass(frozen=True, slots=True)
class StartRoomGame:
    room_id: str
    request_id: str
    actor_id: str
    game_id: str
    expected_state_version: int

    def __post_init__(self) -> None:
        _validate_request(self.room_id, self.request_id)
        _validate_identifier(self.actor_id, code="INVALID_PARTICIPANT_ID")
        _validate_identifier(self.game_id, code="INVALID_GAME_ID")
        _validate_state_version(self.expected_state_version)


@dataclass(frozen=True, slots=True)
class CompleteRoomGame:
    room_id: str
    request_id: str
    game_id: str
    expected_state_version: int
    final_turn_no: int

    def __post_init__(self) -> None:
        _validate_request(self.room_id, self.request_id)
        _validate_identifier(self.game_id, code="INVALID_GAME_ID")
        _validate_state_version(self.expected_state_version)
        if self.final_turn_no < 1:
            raise RoomRuleViolation("INVALID_TURN_NUMBER")


@dataclass(frozen=True, slots=True)
class ConnectRoomParticipant:
    room_id: str
    request_id: str
    participant_id: str
    session_digest: str
    expected_state_version: int

    def __post_init__(self) -> None:
        _validate_request(self.room_id, self.request_id)
        _validate_identifier(self.participant_id, code="INVALID_PARTICIPANT_ID")
        _validate_session_digest(self.session_digest)
        _validate_state_version(self.expected_state_version)


@dataclass(frozen=True, slots=True)
class DisconnectRoomParticipant:
    room_id: str
    request_id: str
    participant_id: str
    connection_generation: int
    expected_state_version: int
    active_vote_turn: int | None = None

    def __post_init__(self) -> None:
        _validate_request(self.room_id, self.request_id)
        _validate_identifier(self.participant_id, code="INVALID_PARTICIPANT_ID")
        if self.connection_generation < 1:
            raise RoomRuleViolation("INVALID_CONNECTION_GENERATION")
        _validate_state_version(self.expected_state_version)
        if self.active_vote_turn is not None and self.active_vote_turn < 1:
            raise RoomRuleViolation("INVALID_TURN_NUMBER")


@dataclass(frozen=True, slots=True)
class ExpireRoomDisconnect:
    room_id: str
    request_id: str
    participant_id: str
    connection_generation: int
    expected_state_version: int
    active_vote_turn: int | None = None

    def __post_init__(self) -> None:
        _validate_request(self.room_id, self.request_id)
        _validate_identifier(self.participant_id, code="INVALID_PARTICIPANT_ID")
        if self.connection_generation < 1:
            raise RoomRuleViolation("INVALID_CONNECTION_GENERATION")
        _validate_state_version(self.expected_state_version)
        if self.active_vote_turn is not None and self.active_vote_turn < 1:
            raise RoomRuleViolation("INVALID_TURN_NUMBER")


@dataclass(frozen=True, slots=True)
class KickRoomParticipant:
    room_id: str
    request_id: str
    actor_id: str
    target_id: str
    expected_state_version: int

    def __post_init__(self) -> None:
        _validate_request(self.room_id, self.request_id)
        _validate_identifier(self.actor_id, code="INVALID_PARTICIPANT_ID")
        _validate_identifier(self.target_id, code="INVALID_PARTICIPANT_ID")
        _validate_state_version(self.expected_state_version)


@dataclass(frozen=True, slots=True)
class LeaveRoomRuntime:
    room_id: str
    request_id: str
    participant_id: str
    expected_state_version: int
    active_vote_turn: int | None = None

    def __post_init__(self) -> None:
        _validate_request(self.room_id, self.request_id)
        _validate_identifier(self.participant_id, code="INVALID_PARTICIPANT_ID")
        _validate_state_version(self.expected_state_version)
        if self.active_vote_turn is not None and self.active_vote_turn < 1:
            raise RoomRuleViolation("INVALID_TURN_NUMBER")


@dataclass(frozen=True, slots=True)
class RoomRuntimeParticipant:
    participant_id: str
    actor_type: ActorType
    joined_order: int
    connected: bool
    team: Team
    ready: bool


@dataclass(frozen=True, slots=True)
class RoomConnection:
    participant_id: str
    session_digest: str
    generation: int
    connected: bool
    disconnect_expires_at_ms: int | None


@dataclass(frozen=True, slots=True)
class DueRoomDisconnect:
    room_id: str
    participant_id: str
    connection_generation: int
    expires_at_ms: int


@dataclass(frozen=True, slots=True)
class PendingGameInvalidation:
    room_id: str
    game_id: str
    closed_at_ms: int


@dataclass(frozen=True, slots=True)
class RoomRuntimeSnapshot:
    room_id: str
    config: RoomConfig
    status: RoomStatus
    owner_id: str | None
    state_version: int
    participants: tuple[RoomRuntimeParticipant, ...]
    game_id: str | None = None
    schema_version: int = ROOM_RUNTIME_SCHEMA_VERSION
    last_game_id: str | None = None
    last_game_turn_no: int | None = None

    @property
    def password_required(self) -> bool:
        return self.config.password_required


@dataclass(frozen=True, slots=True)
class RoomMutationResult:
    snapshot: RoomRuntimeSnapshot | None
    replayed: bool = False
    connection_generation: int | None = None
    disconnect_expires_at_ms: int | None = None
    stale_connection: bool = False
    vote_removed: bool = False
    departure: DepartureResult | None = None
    start_roster: StartRoster | None = None
    operation_at_ms: int | None = None

    @property
    def room_closed(self) -> bool:
        return self.snapshot is None and self.departure is not None and self.departure.room_closed

    @property
    def game_termination(self) -> GameTermination:
        if self.departure is None:
            return GameTermination.NONE
        return self.departure.game_termination

    @property
    def terminated_game_id(self) -> str | None:
        if self.departure is None:
            return None
        return self.departure.terminated_game_id


@dataclass(frozen=True, slots=True)
class RoomSessionBinding:
    room_id: str
    participant_id: str
    session_digest: str
    actor_type: ActorType
    connection_generation: int | None = None
    connected: bool = True

    def __post_init__(self) -> None:
        validate_room_id(self.room_id)
        _validate_identifier(self.participant_id, code="INVALID_PARTICIPANT_ID")
        _validate_session_digest(self.session_digest)
        if self.connection_generation is not None and self.connection_generation < 1:
            raise RoomRuleViolation("INVALID_CONNECTION_GENERATION")


class RoomRuntimePort(Protocol):
    async def create(self, command: CreateRoomRuntime) -> RoomMutationResult: ...

    async def get(self, room_id: str) -> RoomRuntimeSnapshot | None: ...

    async def get_private_access_hash(self, room_id: str) -> str | None: ...

    async def join(self, command: JoinRoomRuntime) -> RoomMutationResult: ...

    async def change_identity(self, command: ChangeRoomIdentity) -> RoomMutationResult: ...

    async def change_team(self, command: ChangeRoomTeam) -> RoomMutationResult: ...

    async def set_ready(self, command: SetRoomReady) -> RoomMutationResult: ...

    async def change_vote_seconds(self, command: ChangeRoomVoteSeconds) -> RoomMutationResult: ...

    async def start_game(self, command: StartRoomGame) -> RoomMutationResult: ...

    async def complete_game(self, command: CompleteRoomGame) -> RoomMutationResult: ...

    async def connect(self, command: ConnectRoomParticipant) -> RoomMutationResult: ...

    async def disconnect(self, command: DisconnectRoomParticipant) -> RoomMutationResult: ...

    async def expire_disconnect(self, command: ExpireRoomDisconnect) -> RoomMutationResult: ...

    async def leave(self, command: LeaveRoomRuntime) -> RoomMutationResult: ...

    async def kick(self, command: KickRoomParticipant) -> RoomMutationResult: ...

    async def pending_game_invalidations(
        self,
        *,
        limit: int,
    ) -> tuple[PendingGameInvalidation, ...]: ...

    async def complete_game_invalidation(self, room_id: str, game_id: str) -> None: ...


class DueRoomDisconnectSource(Protocol):
    async def due_disconnects(
        self,
        *,
        now_ms: int,
        limit: int,
    ) -> tuple[DueRoomDisconnect, ...]: ...


class RoomPasswordPort(Protocol):
    """Argon2id provider boundary; raw passwords never enter a runtime adapter."""

    async def encode(self, raw_password: str) -> str: ...

    async def verify(self, encoded_password: str, candidate_password: str) -> bool: ...
