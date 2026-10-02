"""Internal initialization contract; not an HTTP DTO or an activation switch."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from seokpan.room.application.start_intent import RoomGameStartIntent
from seokpan.vote.application.runtime import VoteMutationResult
from seokpan.vote.domain import VoteRuleViolation


@dataclass(frozen=True, slots=True)
class InitializeCapturedGame:
    """Ensure one captured Game is initialized; callers cannot extend its timer."""

    intent: RoomGameStartIntent
    request_id: str
    actor_id: str
    expected_room_version: int

    def __post_init__(self) -> None:
        if not isinstance(self.intent, RoomGameStartIntent):
            raise VoteRuleViolation("START_INTENT_INVALID")
        self.intent.to_json()
        if (
            not isinstance(self.request_id, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.request_id) is None
        ):
            raise VoteRuleViolation("INVALID_REQUEST_ID")
        if (
            not isinstance(self.actor_id, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.actor_id) is None
        ):
            raise VoteRuleViolation("INVALID_PARTICIPANT_ID")
        if (
            type(self.expected_room_version) is not int
            or not self.intent.accepted_state_version <= self.expected_room_version < 2**53
        ):
            raise VoteRuleViolation("STATE_VERSION_CONFLICT")


class CapturedVoteInitializationPort(Protocol):
    async def get_phase(self, intent: RoomGameStartIntent) -> str | None: ...

    async def initialize(self, command: InitializeCapturedGame) -> VoteMutationResult: ...
