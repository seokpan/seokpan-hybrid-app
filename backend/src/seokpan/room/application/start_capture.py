"""Internal atomic-start command; identity inputs must come from a trusted resolver.

The existing HTTP path deliberately stays on StartRoomGame until the Vote witness,
F09/F15 consumers and record retention are wired and verified as one rollout unit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

from seokpan.room.application.runtime import RoomRuntimeSnapshot, StartRoomGame
from seokpan.room.application.start_intent import RoomGameStartIntent, StartIntentPlayer
from seokpan.room.domain import ActorType, RoomRuleViolation, RoomStatus


@dataclass(frozen=True, slots=True, kw_only=True)
class CaptureRoomGameStart(StartRoomGame):
    """A subtype accepted by both existing Room adapters, not a public request DTO."""

    players: tuple[StartIntentPlayer, ...]

    def __post_init__(self) -> None:
        StartRoomGame.__post_init__(self)
        # Validate identifiers/roster before either Provider can write anything.
        template = RoomGameStartIntent(
            room_id=self.room_id,
            game_id=self.game_id,
            original_request_id=self.request_id,
            owner_id=self.actor_id,
            accepted_state_version=self.expected_state_version + 1,
            started_at_ms=0,
            vote_seconds=15,
            players=self.players,
        )
        object.__setattr__(self, "players", template.players)

    def players_json(self) -> str:
        return json.dumps(
            [item.to_value() for item in self.players],
            sort_keys=True,
            separators=(",", ":"),
        )


def accepted_start_intent(
    command: CaptureRoomGameStart,
    room: RoomRuntimeSnapshot,
    accepted_at_ms: int,
) -> RoomGameStartIntent:
    """Validate against the Provider's snapshot, before its WAITING transition."""
    if room.status is not RoomStatus.WAITING:
        raise RoomRuleViolation("ROOM_NOT_WAITING")
    if room.room_id != command.room_id:
        raise RoomRuleViolation("GAME_NOT_IN_CURRENT_ROOM")
    if room.state_version != command.expected_state_version:
        raise RoomRuleViolation("STATE_VERSION_CONFLICT")
    if room.owner_id != command.actor_id:
        raise RoomRuleViolation("OWNER_REQUIRED")
    ready = {item.participant_id: item for item in room.participants if item.ready}
    if len(ready) < room.config.minimum_ready:
        raise RoomRuleViolation("MINIMUM_READY_NOT_MET")
    if set(ready) != {item.participant_id for item in command.players}:
        raise RoomRuleViolation("START_INTENT_ROSTER_CHANGED")
    for item in command.players:
        current = ready[item.participant_id]
        expected_actor = ActorType.MEMBER if item.member_id is not None else ActorType.GUEST
        if current.team.value != item.team or current.actor_type is not expected_actor:
            raise RoomRuleViolation("START_INTENT_ROSTER_CHANGED")
    return RoomGameStartIntent(
        room_id=command.room_id,
        game_id=command.game_id,
        original_request_id=command.request_id,
        owner_id=command.actor_id,
        accepted_state_version=command.expected_state_version + 1,
        started_at_ms=accepted_at_ms,
        vote_seconds=room.config.vote_seconds,
        players=command.players,
        previous_game_id=room.last_game_id,
        previous_turn_no=room.last_game_turn_no,
    )


def validate_intent_lookup(room_id: str, game_id: str) -> None:
    # Use the canonical UUID validation without constructing a fake accepted intent.
    for value in (room_id, game_id):
        try:
            parsed = UUID(value)
        except (ValueError, TypeError, AttributeError):
            raise RoomRuleViolation("INVALID_GAME_ID") from None
        if parsed.version != 4 or str(parsed) != value:
            raise RoomRuleViolation("INVALID_GAME_ID")
