from collections.abc import Callable

import pytest

from seokpan.room.domain import (
    ActorType,
    DisconnectReason,
    GameTermination,
    Room,
    RoomConfig,
    RoomRuleViolation,
    RoomStatus,
    Team,
)


def create_room() -> Room:
    return Room.create(
        config=RoomConfig(name="Kick test", minimum_ready=2),
        owner_id="member-owner",
        owner_type=ActorType.MEMBER,
    )


def join_ready_player(room: Room, participant_id: str, team: Team) -> None:
    room.join(participant_id=participant_id, actor_type=ActorType.MEMBER)
    room.change_team(participant_id=participant_id, team=team)
    room.set_ready(participant_id=participant_id, ready=True)


def assert_rejected_without_mutation(room: Room, code: str, command: Callable[[], object]) -> None:
    before = room.state_version, room.participants, room.status, room.owner_id, room.game_id
    with pytest.raises(RoomRuleViolation, match=code):
        command()
    assert (
        room.state_version,
        room.participants,
        room.status,
        room.owner_id,
        room.game_id,
    ) == before


@pytest.mark.parametrize("actor_type", (ActorType.MEMBER, ActorType.GUEST))
@pytest.mark.parametrize("connected", (True, False))
def test_owner_kick_removes_only_target(actor_type: ActorType, connected: bool) -> None:
    room = create_room()
    join_ready_player(room, "other", Team.BLACK)
    room.join(participant_id="target", actor_type=actor_type)
    room.change_team(participant_id="target", team=Team.WHITE)
    room.set_ready(participant_id="target", ready=True)
    if not connected:
        room.disconnect(
            participant_id="target", reason=DisconnectReason.PARTICIPANT_CONNECTION_LOST
        )
    others = tuple(p for p in room.participants if p.participant_id != "target")
    version = room.state_version

    departure = room.kick(actor_id="member-owner", target_id="target")

    assert room.participants == others
    assert room.state_version == version + 1
    assert room.status is RoomStatus.WAITING
    assert room.owner_id == "member-owner"
    assert departure.new_owner_id == departure.previous_owner_id == "member-owner"
    assert not departure.room_closed
    assert departure.game_termination is GameTermination.NONE
    assert room.game_id is None


@pytest.mark.parametrize(
    ("actor", "target", "code"),
    [
        ("other", "target", "OWNER_REQUIRED"),
        ("guest", "target", "OWNER_REQUIRED"),
        ("member-owner", "member-owner", "CANNOT_KICK_SELF"),
        ("member-owner", "missing", "PARTICIPANT_NOT_FOUND"),
    ],
)
def test_invalid_kick_does_not_change_room(actor: str, target: str, code: str) -> None:
    room = create_room()
    join_ready_player(room, "other", Team.BLACK)
    join_ready_player(room, "target", Team.WHITE)
    room.join(participant_id="guest", actor_type=ActorType.GUEST)
    assert_rejected_without_mutation(
        room, code, lambda: room.kick(actor_id=actor, target_id=target)
    )


def test_playing_kick_is_rejected_without_changing_roster_or_ready() -> None:
    room = create_room()
    join_ready_player(room, "black", Team.BLACK)
    join_ready_player(room, "white", Team.WHITE)
    room.start_game(actor_id="member-owner", game_id="game-1")
    assert_rejected_without_mutation(
        room, "ROOM_NOT_WAITING", lambda: room.kick(actor_id="member-owner", target_id="black")
    )
    assert room.game_id == "game-1"


def test_owner_handoff_occurs_on_departure_not_disconnect() -> None:
    room = create_room()
    join_ready_player(room, "successor", Team.BLACK)
    join_ready_player(room, "target", Team.WHITE)

    room.disconnect(
        participant_id="member-owner", reason=DisconnectReason.PARTICIPANT_CONNECTION_LOST
    )

    assert room.owner_id == "member-owner"
    assert room.participant("member-owner").connected is False
    assert_rejected_without_mutation(
        room, "OWNER_REQUIRED", lambda: room.kick(actor_id="successor", target_id="target")
    )

    departure = room.leave(participant_id="member-owner")

    assert departure.previous_owner_id == "member-owner"
    assert departure.new_owner_id == "successor"
    assert room.owner_id == "successor"
    assert_rejected_without_mutation(
        room, "OWNER_REQUIRED", lambda: room.kick(actor_id="member-owner", target_id="target")
    )


def test_closed_room_cannot_be_reopened_by_kick() -> None:
    room = create_room()
    room.leave(participant_id="member-owner")
    assert_rejected_without_mutation(
        room, "ROOM_NOT_WAITING", lambda: room.kick(actor_id="member-owner", target_id="target")
    )
