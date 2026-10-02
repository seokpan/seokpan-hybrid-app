"""Room aggregate with no framework or provider dependencies."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum


class ActorType(StrEnum):
    MEMBER = "MEMBER"
    GUEST = "GUEST"


class Team(StrEnum):
    BLACK = "BLACK"
    WHITE = "WHITE"
    NONE = "NONE"


class RoomStatus(StrEnum):
    WAITING = "WAITING"
    PLAYING = "PLAYING"
    CLOSED = "CLOSED"


class RoomVisibility(StrEnum):
    PUBLIC = "PUBLIC"
    PRIVATE = "PRIVATE"


class ParticipantRole(StrEnum):
    PLAYER = "PLAYER"
    SPECTATOR = "SPECTATOR"


class DisconnectReason(StrEnum):
    PARTICIPANT_CONNECTION_LOST = "PARTICIPANT_CONNECTION_LOST"
    PLATFORM_FAILURE = "PLATFORM_FAILURE"


class GameTermination(StrEnum):
    NONE = "NONE"
    SYSTEM_INVALID = "SYSTEM_INVALID"


class RoomRuleViolation(ValueError):
    """A stable domain rejection which must not mutate Room state."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class RoomConfig:
    name: str
    visibility: RoomVisibility = RoomVisibility.PUBLIC
    max_participants: int = 100
    minimum_ready: int = 4
    vote_seconds: int = 15

    def __post_init__(self) -> None:
        normalized_name = self.name.strip()
        if not 1 <= len(normalized_name) <= 30:
            raise RoomRuleViolation("INVALID_ROOM_NAME")
        if not 2 <= self.max_participants <= 100:
            raise RoomRuleViolation("INVALID_MAX_PARTICIPANTS")
        if not 2 <= self.minimum_ready <= self.max_participants:
            raise RoomRuleViolation("INVALID_MINIMUM_READY")
        if self.vote_seconds not in {5, 10, 15, 30}:
            raise RoomRuleViolation("INVALID_VOTE_SECONDS")
        object.__setattr__(self, "name", normalized_name)

    @property
    def password_required(self) -> bool:
        return self.visibility is RoomVisibility.PRIVATE


@dataclass(frozen=True, slots=True)
class Participant:
    participant_id: str
    actor_type: ActorType
    joined_order: int
    connected: bool = True
    team: Team = Team.NONE
    ready: bool = False

    def __post_init__(self) -> None:
        if not self.participant_id:
            raise RoomRuleViolation("INVALID_PARTICIPANT_ID")


@dataclass(frozen=True, slots=True)
class RosterEntry:
    participant_id: str
    team: Team
    role: ParticipantRole


@dataclass(frozen=True, slots=True)
class StartRoster:
    entries: tuple[RosterEntry, ...]

    @property
    def player_ids(self) -> tuple[str, ...]:
        return tuple(
            entry.participant_id for entry in self.entries if entry.role is ParticipantRole.PLAYER
        )


@dataclass(frozen=True, slots=True)
class DepartureResult:
    previous_owner_id: str | None
    new_owner_id: str | None
    room_closed: bool
    game_termination: GameTermination
    terminated_game_id: str | None = None


class Room:
    """The authority for Room lifecycle, ownership, team and Ready rules."""

    def __init__(
        self,
        *,
        config: RoomConfig,
        owner: Participant,
    ) -> None:
        if owner.actor_type is not ActorType.MEMBER:
            raise RoomRuleViolation("MEMBER_REQUIRED_TO_CREATE_ROOM")
        if (
            owner.joined_order != 1
            or not owner.connected
            or owner.team is not Team.NONE
            or owner.ready
        ):
            raise RoomRuleViolation("INVALID_INITIAL_OWNER")
        self.config = config
        self.status = RoomStatus.WAITING
        self.owner_id: str | None = owner.participant_id
        self.game_id: str | None = None
        self.state_version = 1
        self._participants = {owner.participant_id: owner}
        self._next_joined_order = owner.joined_order + 1

    @classmethod
    def create(
        cls,
        *,
        config: RoomConfig,
        owner_id: str,
        owner_type: ActorType,
        room_password: str | None = None,
    ) -> Room:
        if owner_type is not ActorType.MEMBER:
            raise RoomRuleViolation("MEMBER_REQUIRED_TO_CREATE_ROOM")
        if config.visibility is RoomVisibility.PUBLIC and room_password is not None:
            raise RoomRuleViolation("INVALID_ROOM_PASSWORD")
        if config.visibility is RoomVisibility.PRIVATE and (
            room_password is None or not 4 <= len(room_password) <= 20
        ):
            raise RoomRuleViolation("INVALID_ROOM_PASSWORD")
        owner = Participant(
            participant_id=owner_id,
            actor_type=owner_type,
            joined_order=1,
        )
        return cls(config=config, owner=owner)

    @property
    def participants(self) -> tuple[Participant, ...]:
        return tuple(sorted(self._participants.values(), key=lambda item: item.joined_order))

    def participant(self, participant_id: str) -> Participant:
        try:
            return self._participants[participant_id]
        except KeyError as error:
            raise RoomRuleViolation("PARTICIPANT_NOT_FOUND") from error

    def join(
        self,
        *,
        participant_id: str,
        actor_type: ActorType,
        private_access_verified: bool = False,
    ) -> Participant:
        self._require_not_closed()
        if self.config.password_required and not private_access_verified:
            raise RoomRuleViolation("ROOM_PASSWORD_INVALID")
        if participant_id in self._participants:
            raise RoomRuleViolation("PARTICIPANT_ALREADY_JOINED")
        if len(self._participants) >= self.config.max_participants:
            raise RoomRuleViolation("ROOM_CAPACITY_REACHED")

        participant = Participant(
            participant_id=participant_id,
            actor_type=actor_type,
            joined_order=self._next_joined_order,
        )
        self._participants[participant_id] = participant
        self._next_joined_order += 1
        self._advance_version()
        return participant

    def change_team(self, *, participant_id: str, team: Team) -> None:
        self._require_waiting()
        participant = self.participant(participant_id)
        if participant.team is team:
            return
        self._participants[participant_id] = replace(participant, team=team, ready=False)
        self._advance_version()

    def change_identity(self, *, participant_id: str, actor_type: ActorType) -> None:
        """Promote a Guest participant without changing its Room identity or state."""
        participant = self.participant(participant_id)
        if participant.actor_type is actor_type:
            return
        if participant.actor_type is not ActorType.GUEST or actor_type is not ActorType.MEMBER:
            raise RoomRuleViolation("ROOM_IDENTITY_CHANGE_NOT_ALLOWED")
        self._participants[participant_id] = replace(participant, actor_type=actor_type)
        self._advance_version()

    def set_ready(self, *, participant_id: str, ready: bool) -> None:
        self._require_waiting()
        participant = self.participant(participant_id)
        if ready and participant.team is Team.NONE:
            raise RoomRuleViolation("TEAM_REQUIRED_TO_READY")
        if participant.ready is ready:
            return
        self._participants[participant_id] = replace(participant, ready=ready)
        self._advance_version()

    def change_vote_seconds(self, *, actor_id: str, vote_seconds: int) -> None:
        self._require_waiting()
        self._require_owner(actor_id)
        updated_config = replace(self.config, vote_seconds=vote_seconds)
        if updated_config == self.config:
            return
        self.config = updated_config
        self._reset_all_ready()
        self._advance_version()

    def start_game(self, *, actor_id: str, game_id: str) -> StartRoster:
        self._require_waiting()
        self._require_owner(actor_id)
        owner = self.participant(actor_id)
        if not owner.connected:
            raise RoomRuleViolation("PARTICIPANT_DISCONNECTED")
        if not game_id:
            raise RoomRuleViolation("INVALID_GAME_ID")
        ready_participants = tuple(
            item for item in self.participants if item.ready and item.connected
        )
        if len(ready_participants) < self.config.minimum_ready:
            raise RoomRuleViolation("MINIMUM_READY_NOT_MET")
        ready_teams = {item.team for item in ready_participants}
        if Team.BLACK not in ready_teams or Team.WHITE not in ready_teams:
            raise RoomRuleViolation("BOTH_TEAMS_REQUIRED")

        roster = StartRoster(
            entries=tuple(
                RosterEntry(
                    participant_id=item.participant_id,
                    team=item.team if item.ready and item.connected else Team.NONE,
                    role=(
                        ParticipantRole.PLAYER
                        if item.ready and item.connected
                        else ParticipantRole.SPECTATOR
                    ),
                )
                for item in self.participants
            )
        )
        self.status = RoomStatus.PLAYING
        self.game_id = game_id
        self._advance_version()
        return roster

    def complete_game(self, *, game_id: str) -> None:
        """Return a completed Game's Room to its reusable waiting state."""
        if self.status is not RoomStatus.PLAYING:
            raise RoomRuleViolation("ROOM_NOT_PLAYING")
        if self.game_id != game_id:
            raise RoomRuleViolation("STALE_GAME")
        self.status = RoomStatus.WAITING
        self.game_id = None
        self._reset_all_ready()
        self._advance_version()

    def disconnect(
        self,
        *,
        participant_id: str,
        reason: DisconnectReason,
    ) -> DepartureResult:
        self._require_not_closed()
        if reason is DisconnectReason.PLATFORM_FAILURE:
            raise RoomRuleViolation("PLATFORM_FAILURE_IS_NOT_PARTICIPANT_DISCONNECT")
        participant = self.participant(participant_id)
        if not participant.connected:
            return self._unchanged_departure_result()

        previous_owner_id = self.owner_id
        self._participants[participant_id] = replace(participant, connected=False)
        self._advance_version()
        return DepartureResult(
            previous_owner_id=previous_owner_id,
            new_owner_id=previous_owner_id,
            room_closed=False,
            game_termination=GameTermination.NONE,
        )

    def reconnect(self, *, participant_id: str) -> None:
        self._require_not_closed()
        participant = self.participant(participant_id)
        if participant.connected:
            return
        self._participants[participant_id] = replace(participant, connected=True)
        self._advance_version()

    def kick(self, *, actor_id: str, target_id: str) -> DepartureResult:
        self._require_waiting()
        self._require_owner(actor_id)
        if actor_id == target_id:
            raise RoomRuleViolation("CANNOT_KICK_SELF")
        return self.leave(participant_id=target_id)

    def leave(self, *, participant_id: str) -> DepartureResult:
        self._require_not_closed()
        participant = self.participant(participant_id)
        previous_owner_id = self.owner_id
        del self._participants[participant.participant_id]
        result = self._resolve_owner_departure(
            departed_id=participant_id,
            previous_owner_id=previous_owner_id,
        )
        self._advance_version()
        return result

    def _resolve_owner_departure(
        self,
        *,
        departed_id: str,
        previous_owner_id: str | None,
    ) -> DepartureResult:
        if departed_id != previous_owner_id:
            return DepartureResult(
                previous_owner_id=previous_owner_id,
                new_owner_id=previous_owner_id,
                room_closed=False,
                game_termination=GameTermination.NONE,
            )

        candidates = sorted(
            (
                item
                for item in self._participants.values()
                if item.participant_id != departed_id
                and item.connected
                and item.actor_type is ActorType.MEMBER
            ),
            key=lambda item: item.joined_order,
        )
        self._reset_all_ready()
        if candidates:
            self.owner_id = candidates[0].participant_id
            return DepartureResult(
                previous_owner_id=previous_owner_id,
                new_owner_id=self.owner_id,
                room_closed=False,
                game_termination=GameTermination.NONE,
            )

        game_termination = (
            GameTermination.SYSTEM_INVALID
            if self.status is RoomStatus.PLAYING
            else GameTermination.NONE
        )
        terminated_game_id = (
            self.game_id if game_termination is GameTermination.SYSTEM_INVALID else None
        )
        self.owner_id = None
        self.status = RoomStatus.CLOSED
        return DepartureResult(
            previous_owner_id=previous_owner_id,
            new_owner_id=None,
            room_closed=True,
            game_termination=game_termination,
            terminated_game_id=terminated_game_id,
        )

    def _unchanged_departure_result(self) -> DepartureResult:
        return DepartureResult(
            previous_owner_id=self.owner_id,
            new_owner_id=self.owner_id,
            room_closed=False,
            game_termination=GameTermination.NONE,
        )

    def _reset_all_ready(self) -> None:
        self._participants = {
            participant_id: replace(participant, ready=False)
            for participant_id, participant in self._participants.items()
        }

    def _require_owner(self, actor_id: str) -> None:
        if actor_id != self.owner_id:
            raise RoomRuleViolation("OWNER_REQUIRED")

    def _require_waiting(self) -> None:
        if self.status is not RoomStatus.WAITING:
            raise RoomRuleViolation("ROOM_NOT_WAITING")

    def _require_not_closed(self) -> None:
        if self.status is RoomStatus.CLOSED:
            raise RoomRuleViolation("ROOM_CLOSED")

    def _advance_version(self) -> None:
        self.state_version += 1
