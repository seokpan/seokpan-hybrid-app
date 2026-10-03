"""Validated closure evidence for captured Games; never inferred from a missing Room."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol, cast

from seokpan.room.application.start_intent import RoomGameStartIntent
from seokpan.room.domain import RoomRuleViolation


@dataclass(frozen=True, slots=True)
class ClosedStartIntent:
    intent: RoomGameStartIntent
    closed_at_ms: int
    initialized: bool
    acknowledged: bool
    phase_wire: str


class CapturedClosurePort(Protocol):
    async def read_closed_start(
        self,
        room_id: str,
        game_id: str,
        closed_at_ms: int,
    ) -> ClosedStartIntent | None: ...

    async def acknowledge(self, value: ClosedStartIntent) -> None: ...


def _object(raw: object) -> dict[str, object]:
    if not isinstance(raw, str) or len(raw) > 65536:
        raise RoomRuleViolation("START_CLOSURE_INVALID")

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise RoomRuleViolation("START_CLOSURE_INVALID")
            result[key] = value
        return result

    try:
        value: object = json.loads(raw, object_pairs_hook=unique)
    except (ValueError, RecursionError) as error:
        raise RoomRuleViolation("START_CLOSURE_INVALID") from error
    if not isinstance(value, dict):
        raise RoomRuleViolation("START_CLOSURE_INVALID")
    return value


def decode_closed_start(
    *,
    room_id: str,
    game_id: str,
    closed_at_ms: int,
    marker_wire: object,
    intent_wire: object,
    phase_wire: object,
) -> ClosedStartIntent | None:
    """Validate one atomic Provider read. Missing/corrupt evidence is not PENDING."""
    if type(closed_at_ms) is not int or not 0 <= closed_at_ms < 2**53:
        raise RoomRuleViolation("START_CLOSURE_INVALID")
    marker = _object(marker_wire)
    if (
        marker.get("room_id") != room_id
        or marker.get("terminated_game_id") != game_id
        or type(marker.get("closed_at_ms")) is not int
        or marker["closed_at_ms"] != closed_at_ms
        or type(marker.get("invalidation_pending")) is not bool
    ):
        raise RoomRuleViolation("GAME_CLOSURE_UNCONFIRMED")
    if intent_wire is None and phase_wire is None:
        # Legacy closure is valid, but it cannot invent missing Game history.
        return None
    if not isinstance(intent_wire, str):
        raise RoomRuleViolation("START_CLOSURE_INVALID")
    try:
        intent = RoomGameStartIntent.from_json(intent_wire)
    except (ValueError, TypeError) as error:
        raise RoomRuleViolation("START_CLOSURE_INVALID") from error
    if (
        intent.room_id != room_id
        or intent.game_id != game_id
        or intent.started_at_ms > closed_at_ms
        or not isinstance(phase_wire, str)
    ):
        raise RoomRuleViolation("START_CLOSURE_INVALID")
    acknowledged = not marker["invalidation_pending"]
    if phase_wire == "PENDING":
        if acknowledged:
            raise RoomRuleViolation("START_CLOSURE_INVALID")
        initialized = False
    else:
        phase = _object(phase_wire)
        if (
            type(phase.get("schema_version")) is not int
            or phase["schema_version"] != 1
            or phase.get("game_id") != game_id
            or phase.get("intent_fingerprint") != intent.fingerprint
        ):
            raise RoomRuleViolation("START_CLOSURE_INVALID")
        if phase.get("phase") == "INITIALIZED":
            stamp, deadline = phase.get("initialized_at_ms"), phase.get("first_deadline_ms")
            if (
                acknowledged
                or type(stamp) is not int
                or type(deadline) is not int
                or not intent.started_at_ms <= stamp <= closed_at_ms
                or not 0 <= deadline < 2**53
                or deadline != stamp + intent.vote_seconds * 1000
            ):
                raise RoomRuleViolation("START_CLOSURE_INVALID")
            initialized = True
        elif phase.get("phase") == "FINALIZED":
            # Also accepts the pre-ACK cut after terminal witness/TTL writes.
            if (
                type(phase.get("closed_at_ms")) is not int
                or phase["closed_at_ms"] != closed_at_ms
                or type(phase.get("initialized")) is not bool
            ):
                raise RoomRuleViolation("START_CLOSURE_INVALID")
            initialized = cast(bool, phase["initialized"])
        else:
            raise RoomRuleViolation("START_CLOSURE_INVALID")
    return ClosedStartIntent(intent, closed_at_ms, initialized, acknowledged, phase_wire)


def terminal_phase(value: ClosedStartIntent) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "phase": "FINALIZED",
            "game_id": value.intent.game_id,
            "intent_fingerprint": value.intent.fingerprint,
            "closed_at_ms": value.closed_at_ms,
            "initialized": value.initialized,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
