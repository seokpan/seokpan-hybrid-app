"""Immutable start-intent wire contract; Provider/Application wiring is separate.

This value object is not proof that a Room accepted a start or that Vote runtime
was initialized. Only trusted atomic Provider transitions can establish those
facts. Member identifiers remain decimal strings at the JSON/Lua boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

START_INTENT_SCHEMA_VERSION = 1
_MAX_JSON_BYTES = 65_536
_MAX_SAFE_INTEGER = (1 << 53) - 1
_MAX_TIMESTAMP_MS = 253_402_300_799_999
_MAX_MEMBER_ID = (1 << 64) - 1
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_MEMBER_ID = re.compile(r"[1-9][0-9]{0,19}")
_GUEST_LABEL = re.compile(r"Guest-[0-9]{4}")
_PLAYER_FIELDS = {"participant_id", "team", "member_id", "guest_label"}
_INTENT_FIELDS = {
    "schema_version",
    "room_id",
    "game_id",
    "original_request_id",
    "owner_id",
    "accepted_state_version",
    "started_at_ms",
    "vote_seconds",
    "players",
    "previous_game_id",
    "previous_turn_no",
}


class StartIntentViolation(ValueError):
    """Stable internal rejection without serialized identity or secret values."""

    def __init__(self, code: str = "START_INTENT_INVALID") -> None:
        self.code = code
        super().__init__(code)


def _uuid4(value: object) -> None:
    if not isinstance(value, str):
        raise StartIntentViolation()
    try:
        parsed = UUID(value)
    except ValueError:
        raise StartIntentViolation() from None
    if parsed.version != 4 or str(parsed) != value:
        raise StartIntentViolation()


def _integer(value: object, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise StartIntentViolation()
    return value


def _object(value: object, fields: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != fields:
        raise StartIntentViolation()
    return cast(dict[str, object], value)


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise StartIntentViolation()
    return value


def _optional_text(value: object) -> str | None:
    return None if value is None else _text(value)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise StartIntentViolation()
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class StartIntentPlayer:
    """Accepted PLAYER identity, not live Session state or a display fallback."""

    participant_id: str
    team: str
    member_id: str | None = None
    guest_label: str | None = None

    def __post_init__(self) -> None:
        _uuid4(self.participant_id)
        if not isinstance(self.team, str) or self.team not in {"BLACK", "WHITE"}:
            raise StartIntentViolation()
        if self.member_id is not None and (
            not isinstance(self.member_id, str)
            or _MEMBER_ID.fullmatch(self.member_id) is None
            or int(self.member_id) > _MAX_MEMBER_ID
            or self.guest_label is not None
        ):
            raise StartIntentViolation()
        if self.member_id is None and (
            not isinstance(self.guest_label, str)
            or _GUEST_LABEL.fullmatch(self.guest_label) is None
        ):
            raise StartIntentViolation()

    def to_value(self) -> dict[str, object]:
        return {
            "participant_id": self.participant_id,
            "team": self.team,
            "member_id": self.member_id,
            "guest_label": self.guest_label,
        }

    @classmethod
    def from_value(cls, value: object) -> StartIntentPlayer:
        data = _object(value, _PLAYER_FIELDS)
        return cls(
            participant_id=_text(data["participant_id"]),
            team=_text(data["team"]),
            member_id=_optional_text(data["member_id"]),
            guest_label=_optional_text(data["guest_label"]),
        )


@dataclass(frozen=True, slots=True)
class RoomGameStartIntent:
    """Original accepted start facts; retry must not replace them with live data.

    The owner may be a SPECTATOR, so owner membership in players is not required.
    Provider admission must still validate ownership, version and the exact Ready
    roster. Successful decoding alone does not authorize a start or recovery.
    """

    room_id: str
    game_id: str
    original_request_id: str
    owner_id: str
    accepted_state_version: int
    started_at_ms: int
    vote_seconds: int
    players: tuple[StartIntentPlayer, ...]
    previous_game_id: str | None = None
    previous_turn_no: int | None = None
    schema_version: int = START_INTENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != START_INTENT_SCHEMA_VERSION
        ):
            raise StartIntentViolation("START_INTENT_SCHEMA_UNSUPPORTED")
        for value in (self.room_id, self.game_id, self.owner_id):
            _uuid4(value)
        if (
            not isinstance(self.original_request_id, str)
            or _REQUEST_ID.fullmatch(self.original_request_id) is None
        ):
            raise StartIntentViolation()
        _integer(self.accepted_state_version, 2, _MAX_SAFE_INTEGER)
        _integer(self.started_at_ms, 0, _MAX_TIMESTAMP_MS)
        _integer(self.vote_seconds, 5, 30)
        if self.vote_seconds not in {5, 10, 15, 30}:
            raise StartIntentViolation()
        if type(self.players) is not tuple or not 2 <= len(self.players) <= 100:
            raise StartIntentViolation()
        if any(type(player) is not StartIntentPlayer for player in self.players):
            raise StartIntentViolation()
        if {player.team for player in self.players} != {"BLACK", "WHITE"}:
            raise StartIntentViolation()
        for values in (
            [player.participant_id for player in self.players],
            [player.member_id for player in self.players if player.member_id is not None],
            [player.guest_label for player in self.players if player.guest_label is not None],
        ):
            if len(values) != len(set(values)):
                raise StartIntentViolation()
        if self.previous_game_id is None and self.previous_turn_no is not None:
            raise StartIntentViolation()
        if self.previous_game_id is not None:
            _uuid4(self.previous_game_id)
            if self.previous_game_id == self.game_id:
                raise StartIntentViolation()
            _integer(self.previous_turn_no, 1, _MAX_SAFE_INTEGER)
        ordered = tuple(sorted(self.players, key=lambda player: player.participant_id))
        object.__setattr__(self, "players", ordered)

    @property
    def started_at(self) -> datetime:
        """Recover the accepted UTC millisecond exactly, without float rounding."""
        return _EPOCH + timedelta(milliseconds=self.started_at_ms)

    def to_json(self) -> str:
        value = {
            "schema_version": self.schema_version,
            "room_id": self.room_id,
            "game_id": self.game_id,
            "original_request_id": self.original_request_id,
            "owner_id": self.owner_id,
            "accepted_state_version": self.accepted_state_version,
            "started_at_ms": self.started_at_ms,
            "vote_seconds": self.vote_seconds,
            "players": [player.to_value() for player in self.players],
            "previous_game_id": self.previous_game_id,
            "previous_turn_no": self.previous_turn_no,
        }
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    @property
    def fingerprint(self) -> str:
        """Content equality token, not authentication, authorization, or a MAC."""
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()

    @classmethod
    def from_json(cls, payload: str) -> RoomGameStartIntent:
        if not isinstance(payload, str):
            raise StartIntentViolation()
        try:
            if len(payload.encode("utf-8")) > _MAX_JSON_BYTES:
                raise StartIntentViolation()
            data = _object(json.loads(payload, object_pairs_hook=_unique_object), _INTENT_FIELDS)
        except (ValueError, RecursionError):
            raise StartIntentViolation() from None
        players = data["players"]
        if not isinstance(players, list) or not 2 <= len(players) <= 100:
            raise StartIntentViolation()
        previous_turn = data["previous_turn_no"]
        return cls(
            room_id=_text(data["room_id"]),
            game_id=_text(data["game_id"]),
            original_request_id=_text(data["original_request_id"]),
            owner_id=_text(data["owner_id"]),
            accepted_state_version=_integer(data["accepted_state_version"], 2, _MAX_SAFE_INTEGER),
            started_at_ms=_integer(data["started_at_ms"], 0, _MAX_TIMESTAMP_MS),
            vote_seconds=_integer(data["vote_seconds"], 5, 30),
            players=tuple(StartIntentPlayer.from_value(player) for player in players),
            previous_game_id=_optional_text(data["previous_game_id"]),
            previous_turn_no=(
                None if previous_turn is None else _integer(previous_turn, 1, _MAX_SAFE_INTEGER)
            ),
            schema_version=_integer(data["schema_version"], 1, _MAX_SAFE_INTEGER),
        )
