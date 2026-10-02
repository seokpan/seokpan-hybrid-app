"""Pure start-intent contract tests; no Provider/Room mutation is exercised."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime

import pytest

from seokpan.room.application.start_intent import (
    RoomGameStartIntent,
    StartIntentPlayer,
    StartIntentViolation,
)

ROOM = "00000000-0000-4000-8000-000000000001"
GAME = "00000000-0000-4000-8000-000000000002"
BLACK = "00000000-0000-4000-8000-000000000003"
WHITE = "00000000-0000-4000-8000-000000000004"
OTHER = "00000000-0000-4000-8000-000000000005"


@pytest.fixture
def intent() -> RoomGameStartIntent:
    return RoomGameStartIntent(
        room_id=ROOM,
        game_id=GAME,
        original_request_id="start-accepted",
        owner_id=BLACK,
        accepted_state_version=7,
        started_at_ms=1_234,
        vote_seconds=15,
        players=(
            StartIntentPlayer(BLACK, "BLACK", member_id="1"),
            StartIntentPlayer(WHITE, "WHITE", guest_label="Guest-0001"),
        ),
    )


def test_round_trip_preserves_original_start_facts(intent: RoomGameStartIntent) -> None:
    restored = RoomGameStartIntent.from_json(intent.to_json())
    assert restored == intent
    assert restored.started_at == datetime(1970, 1, 1, 0, 0, 1, 234000, tzinfo=UTC)
    assert restored.fingerprint == intent.fingerprint


def test_nested_facts_are_immutable_and_export_is_detached(intent: RoomGameStartIntent) -> None:
    with pytest.raises(FrozenInstanceError):
        intent.started_at_ms = 9_999
    with pytest.raises(FrozenInstanceError):
        intent.players[0].member_id = "2"
    value = intent.players[0].to_value()
    value["member_id"] = "999"
    assert intent.players[0].member_id == "1"


def test_roster_order_is_canonical_not_a_new_start(intent: RoomGameStartIntent) -> None:
    reordered = replace(intent, players=tuple(reversed(intent.players)))
    assert reordered == intent
    assert reordered.to_json() == intent.to_json()
    assert reordered.fingerprint == intent.fingerprint


def test_owner_may_be_non_ready_spectator(intent: RoomGameStartIntent) -> None:
    value = replace(intent, owner_id=OTHER)
    assert all(player.participant_id != value.owner_id for player in value.players)
    assert RoomGameStartIntent.from_json(value.to_json()) == value


@pytest.mark.parametrize("member_id", ["9007199254740993", "18446744073709551615"])
def test_member_id_keeps_exact_decimal_string_above_double_precision(
    intent: RoomGameStartIntent, member_id: str
) -> None:
    player = replace(intent.players[0], member_id=member_id)
    value = replace(intent, players=(player, intent.players[1]))
    assert json.loads(value.to_json())["players"][0]["member_id"] == member_id
    assert RoomGameStartIntent.from_json(value.to_json()).players[0].member_id == member_id


@pytest.mark.parametrize(
    "member_id",
    [0, True, 1, "0", "01", "-1", "+1", "1.0", "１", "18446744073709551616", "9" * 100],
)
def test_member_identifier_must_be_canonical_unsigned_bigint_string(member_id: object) -> None:
    with pytest.raises(StartIntentViolation):
        StartIntentPlayer.from_value(
            {"participant_id": BLACK, "team": "BLACK", "member_id": member_id, "guest_label": None}
        )


@pytest.mark.parametrize(
    ("member_id", "guest_label"),
    [
        (None, None),
        ("1", "Guest-0001"),
        (None, "참가자"),
        (None, "Guest-12345"),
        (None, "Guest-１２３４"),
    ],
)
def test_identity_is_not_guessed_or_replaced_by_ui_fallback(
    member_id: str | None, guest_label: str | None
) -> None:
    with pytest.raises(StartIntentViolation):
        StartIntentPlayer(BLACK, "BLACK", member_id=member_id, guest_label=guest_label)


@pytest.mark.parametrize("team", ["NONE", "EMPTY", "SPECTATOR", "black", 1, True, []])
def test_only_black_and_white_players_are_serialized(team: object) -> None:
    with pytest.raises(StartIntentViolation):
        StartIntentPlayer.from_value(
            {"participant_id": BLACK, "team": team, "member_id": "1", "guest_label": None}
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("accepted_state_version", 1),
        ("accepted_state_version", True),
        ("accepted_state_version", 7.0),
        ("accepted_state_version", 1 << 53),
        ("started_at_ms", -1),
        ("started_at_ms", True),
        ("started_at_ms", 1234.0),
        ("started_at_ms", 253_402_300_800_000),
        ("vote_seconds", 6),
        ("vote_seconds", 15.0),
        ("vote_seconds", "15"),
        ("room_id", "room-1"),
        ("game_id", None),
        ("owner_id", "00000000-0000-1000-8000-000000000001"),
        ("original_request_id", ""),
        ("original_request_id", "x" * 65),
        ("original_request_id", "new\nrequest"),
        ("previous_turn_no", 3),
        ("previous_game_id", OTHER),
    ],
)
def test_malformed_wire_fields_are_rejected(
    intent: RoomGameStartIntent, field: str, value: object
) -> None:
    data = json.loads(intent.to_json())
    data[field] = value
    with pytest.raises(StartIntentViolation):
        RoomGameStartIntent.from_json(json.dumps(data))


@pytest.mark.parametrize("version", [2, 999])
def test_unknown_schema_is_not_silently_upgraded(intent: RoomGameStartIntent, version: int) -> None:
    data = json.loads(intent.to_json())
    data["schema_version"] = version
    with pytest.raises(StartIntentViolation, match="START_INTENT_SCHEMA_UNSUPPORTED"):
        RoomGameStartIntent.from_json(json.dumps(data))


@pytest.mark.parametrize("fault", ["one_player", "same_team", "participant", "member", "guest"])
def test_invalid_or_duplicate_rosters_are_rejected(intent: RoomGameStartIntent, fault: str) -> None:
    first, second = intent.players
    if fault == "one_player":
        players = (first,)
    elif fault == "same_team":
        players = (first, replace(second, team="BLACK"))
    elif fault == "participant":
        players = (first, replace(second, participant_id=BLACK))
    elif fault == "member":
        players = (first, replace(second, member_id="1", guest_label=None))
    else:
        players = (replace(first, member_id=None, guest_label="Guest-0001"), second)
    with pytest.raises(StartIntentViolation):
        replace(intent, players=players)


def test_previous_game_reference_is_exact_and_never_self(intent: RoomGameStartIntent) -> None:
    value = replace(intent, previous_game_id=OTHER, previous_turn_no=17)
    assert RoomGameStartIntent.from_json(value.to_json()) == value
    with pytest.raises(StartIntentViolation):
        replace(value, previous_game_id=GAME)
    with pytest.raises(StartIntentViolation):
        replace(value, previous_turn_no=True)


@pytest.mark.parametrize("key", ["csrf_token", "session_digest", "password", "unexpected"])
def test_unknown_fields_are_not_kept_and_errors_do_not_echo_values(
    intent: RoomGameStartIntent, key: str
) -> None:
    data = json.loads(intent.to_json())
    data[key] = "secret-sentinel"
    with pytest.raises(StartIntentViolation) as error:
        RoomGameStartIntent.from_json(json.dumps(data))
    assert "secret-sentinel" not in str(error.value)
    assert key not in str(error.value)


@pytest.mark.parametrize("scope", ["intent", "player"])
def test_duplicate_json_keys_are_rejected(intent: RoomGameStartIntent, scope: str) -> None:
    payload = intent.to_json()
    if scope == "intent":
        payload = payload.replace('"vote_seconds":15', '"vote_seconds":5,"vote_seconds":15')
    else:
        payload = payload.replace('"member_id":"1"', '"member_id":"9","member_id":"1"')
    with pytest.raises(StartIntentViolation):
        RoomGameStartIntent.from_json(payload)


@pytest.mark.parametrize("payload", ["{}", "[]", "null", "{", " " * 65_537, "[" * 2000])
def test_invalid_or_oversized_json_never_creates_default_intent(payload: str) -> None:
    with pytest.raises(StartIntentViolation):
        RoomGameStartIntent.from_json(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("room_id", OTHER),
        ("game_id", OTHER),
        ("original_request_id", "other-request"),
        ("owner_id", OTHER),
        ("accepted_state_version", 8),
        ("started_at_ms", 1235),
        ("vote_seconds", 30),
    ],
)
def test_fingerprint_changes_with_each_original_start_fact(
    intent: RoomGameStartIntent, field: str, value: object
) -> None:
    data = json.loads(intent.to_json())
    data[field] = value
    changed = RoomGameStartIntent.from_json(json.dumps(data))
    assert changed.fingerprint != intent.fingerprint


def test_player_identity_and_team_are_in_fingerprint(intent: RoomGameStartIntent) -> None:
    changed = replace(
        intent, players=(replace(intent.players[0], member_id="2"), intent.players[1])
    )
    assert changed.fingerprint != intent.fingerprint
    swapped = replace(
        intent,
        players=(
            replace(intent.players[0], team="WHITE"),
            replace(intent.players[1], team="BLACK"),
        ),
    )
    assert swapped.fingerprint != intent.fingerprint


def test_maximum_timestamp_keeps_exact_milliseconds(intent: RoomGameStartIntent) -> None:
    value = replace(intent, started_at_ms=253_402_300_799_999)
    assert value.started_at == datetime(9999, 12, 31, 23, 59, 59, 999000, tzinfo=UTC)


def test_nested_unknown_field_is_rejected(intent: RoomGameStartIntent) -> None:
    data = json.loads(intent.to_json())
    data["players"][0]["session_digest"] = "not-part-of-the-intent"
    with pytest.raises(StartIntentViolation):
        RoomGameStartIntent.from_json(json.dumps(data))


def test_mutable_constructor_roster_is_rejected(intent: RoomGameStartIntent) -> None:
    with pytest.raises(StartIntentViolation):
        replace(intent, players=list(intent.players))


@pytest.mark.parametrize("count", [100, 101])
def test_roster_cardinality_matches_current_room_limit(
    intent: RoomGameStartIntent, count: int
) -> None:
    players = tuple(
        StartIntentPlayer(
            f"00000000-0000-4000-8000-{number:012d}",
            "BLACK" if number % 2 else "WHITE",
            member_id=str(number),
        )
        for number in range(1, count + 1)
    )
    if count == 101:
        with pytest.raises(StartIntentViolation):
            replace(intent, players=players)
    else:
        value = replace(intent, players=players)
        assert RoomGameStartIntent.from_json(value.to_json()) == value
