"""Internal proof for normal completion; no result/Rating writes or Runtime deletion."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol, TypeGuard, cast

from seokpan.room.application.start_intent import RoomGameStartIntent
from seokpan.room.domain import RoomRuleViolation

NORMAL_END_REASONS = frozenset({"BLACK_WIN", "WHITE_WIN", "DRAW", "FORFEIT", "JOINT_LOSS"})


def integer(value: object, minimum: int = 0) -> TypeGuard[int]:
    return type(value) is int and minimum <= value < 2**53


def initialized_phase(intent: RoomGameStartIntent, raw: str) -> dict[str, object]:
    """Keep INITIALIZED history intact, including for concurrent F15 readers."""

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        if not isinstance(raw, str) or len(raw) > 65536:
            raise ValueError("phase")
        phase = json.loads(raw, object_pairs_hook=unique)
        if not isinstance(phase, dict):
            raise ValueError("phase")
        stamp, deadline = phase.get("initialized_at_ms"), phase.get("first_deadline_ms")
        if (
            type(phase.get("schema_version")) is not int
            or phase["schema_version"] != 1
            or phase.get("phase") != "INITIALIZED"
            or phase.get("game_id") != intent.game_id
            or phase.get("intent_fingerprint") != intent.fingerprint
            or not integer(stamp)
            or not integer(deadline)
            or stamp < intent.started_at_ms
            or deadline != stamp + intent.vote_seconds * 1000
        ):
            raise ValueError("phase")
        return cast(dict[str, object], phase)
    except (ValueError, TypeError, RecursionError) as error:
        raise RoomRuleViolation("START_COMPLETION_INVALID") from error


@dataclass(frozen=True, slots=True)
class CompleteCapturedGame:
    intent: RoomGameStartIntent
    phase_wire: str
    expected_room_version: int
    final_turn_no: int
    end_reason: str
    ended_at_ms: int
    pending_wire: str | None = None

    def __post_init__(self) -> None:
        initialized_phase(self.intent, self.phase_wire)
        if (
            not integer(self.expected_room_version, 1)
            or not integer(self.final_turn_no, 1)
            or not integer(self.ended_at_ms)
            or self.ended_at_ms < self.intent.started_at_ms
            or self.end_reason not in NORMAL_END_REASONS
        ):
            raise RoomRuleViolation("START_COMPLETION_INVALID")

    def receipt(
        self,
        phase: dict[str, object],
        *,
        now_ms: int,
        retention_ms: int,
    ) -> tuple[str, int, bool]:
        """Return immutable completion evidence, fixed expiry, and existing-receipt flag."""
        replay = "normal_completion" in phase
        prior = phase.get("normal_completion")
        if "normal_completion" in phase:
            if (
                not isinstance(prior, dict)
                or set(prior)
                != {
                    "schema_version",
                    "final_turn_no",
                    "end_reason",
                    "ended_at_ms",
                    "recorded_at_ms",
                    "retain_until_ms",
                }
                or type(prior.get("schema_version")) is not int
                or prior["schema_version"] != 1
                or not integer(prior.get("final_turn_no"), 1)
                or not integer(prior.get("ended_at_ms"))
                or prior["final_turn_no"] != self.final_turn_no
                or prior["end_reason"] != self.end_reason
                or prior["ended_at_ms"] != self.ended_at_ms
                or not integer(prior.get("recorded_at_ms"))
                or not integer(prior.get("retain_until_ms"))
                or prior["recorded_at_ms"] < self.ended_at_ms
                or prior["retain_until_ms"] != prior["recorded_at_ms"] + retention_ms
            ):
                raise RoomRuleViolation("START_COMPLETION_INVALID")
            expiry = cast(int, prior["retain_until_ms"])
        else:
            if (
                not integer(now_ms)
                or now_ms < self.ended_at_ms
                or not integer(now_ms + retention_ms)
            ):
                raise RoomRuleViolation("START_COMPLETION_INVALID")
            expiry = now_ms + retention_ms
            phase = {
                **phase,
                "normal_completion": {
                    "schema_version": 1,
                    "final_turn_no": self.final_turn_no,
                    "end_reason": self.end_reason,
                    "ended_at_ms": self.ended_at_ms,
                    "recorded_at_ms": now_ms,
                    "retain_until_ms": expiry,
                },
            }
        return json.dumps(phase, sort_keys=True, separators=(",", ":")), expiry, replay


@dataclass(frozen=True, slots=True)
class PendingCapturedCompletion:
    command: CompleteCapturedGame
    released: bool
    wire: str

    @classmethod
    def from_json(cls, raw: str) -> PendingCapturedCompletion:
        def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
            value: dict[str, object] = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("duplicate key")
                value[key] = item
            return value

        try:
            if not isinstance(raw, str) or len(raw) > 131072:
                raise ValueError("pending")
            obj = json.loads(raw, object_pairs_hook=unique)
            if (
                not isinstance(obj, dict)
                or set(obj) != {"schema_version", "intent", "phase", "released"}
                or type(obj["schema_version"]) is not int
                or obj["schema_version"] != 1
                or type(obj["released"]) is not bool
            ):
                raise ValueError("pending")
            intent = RoomGameStartIntent.from_json(obj["intent"])
            phase = initialized_phase(intent, obj["phase"])
            receipt = phase["normal_completion"]
            if not isinstance(receipt, dict):
                raise ValueError("receipt")
            command = CompleteCapturedGame(
                intent,
                obj["phase"],
                1,
                receipt["final_turn_no"],
                receipt["end_reason"],
                receipt["ended_at_ms"],
                pending_wire=raw,
            )
            command.receipt(phase, now_ms=0, retention_ms=86400000)
            return cls(command, obj["released"], raw)
        except (ValueError, TypeError, KeyError, RecursionError) as error:
            raise RoomRuleViolation("START_COMPLETION_INVALID") from error


def pending_completion_wire(intent: RoomGameStartIntent, phase: str, *, released: bool) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "intent": intent.to_json(),
            "phase": phase,
            "released": released,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


class CapturedCompletionPort(Protocol):
    async def pending(self, *, limit: int) -> tuple[tuple[str, str], ...]: ...

    async def read_pending(
        self,
        room_id: str,
        game_id: str,
    ) -> PendingCapturedCompletion | None: ...

    async def read_start(
        self,
        room_id: str,
        game_id: str,
    ) -> tuple[RoomGameStartIntent, str] | None: ...

    async def complete(self, command: CompleteCapturedGame) -> bool:
        """Release Room and retain terminal proof. True only for a new Room transition."""
        ...
