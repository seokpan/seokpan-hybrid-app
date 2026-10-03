from collections.abc import Callable
from dataclasses import fields

import pytest

from seokpan.room.domain import (
    ActorType,
    DisconnectReason,
    GameTermination,
    Participant,
    ParticipantRole,
    Room,
    RoomConfig,
    RoomRuleViolation,
    RoomStatus,
    RoomVisibility,
    Team,
)


def create_room(
    *,
    minimum_ready: int = 2,
    max_participants: int = 100,
    visibility: RoomVisibility = RoomVisibility.PUBLIC,
    room_password: str | None = None,
) -> Room:
    return Room.create(
        config=RoomConfig(
            name="  MVP Room  ",
            visibility=visibility,
            minimum_ready=minimum_ready,
            max_participants=max_participants,
        ),
        owner_id="member-owner",
        owner_type=ActorType.MEMBER,
        room_password=room_password,
    )


def join_ready_player(room: Room, participant_id: str, team: Team) -> None:
    room.join(participant_id=participant_id, actor_type=ActorType.MEMBER)
    room.change_team(participant_id=participant_id, team=team)
    room.set_ready(participant_id=participant_id, ready=True)


def assert_rejected_without_mutation(
    room: Room,
    expected_code: str,
    command: Callable[[], object],
) -> None:
    version_before = room.state_version
    participants_before = room.participants
    status_before = room.status
    owner_before = room.owner_id

    with pytest.raises(RoomRuleViolation, match=expected_code) as error:
        command()

    assert error.value.code == expected_code
    assert room.state_version == version_before
    assert room.participants == participants_before
    assert room.status is status_before
    assert room.owner_id == owner_before


@pytest.mark.parametrize(
    ("config", "code"),
    [
        (RoomConfig, "INVALID_ROOM_NAME"),
        (lambda: RoomConfig(name="room", max_participants=1), "INVALID_MAX_PARTICIPANTS"),
        (
            lambda: RoomConfig(name="room", max_participants=2, minimum_ready=3),
            "INVALID_MINIMUM_READY",
        ),
        (lambda: RoomConfig(name="room", vote_seconds=20), "INVALID_VOTE_SECONDS"),
    ],
)
def test_room_config_rejects_values_outside_mvp_bounds(
    config: Callable[..., RoomConfig],
    code: str,
) -> None:
    with pytest.raises(RoomRuleViolation, match=code):
        config(name="") if config is RoomConfig else config()


def test_room_creation_requires_member_and_normalizes_name() -> None:
    with pytest.raises(RoomRuleViolation, match="MEMBER_REQUIRED_TO_CREATE_ROOM"):
        Room.create(
            config=RoomConfig(name="room", minimum_ready=2),
            owner_id="guest-owner",
            owner_type=ActorType.GUEST,
        )

    room = create_room()

    assert room.config.name == "MVP Room"
    assert room.owner_id == "member-owner"
    assert room.state_version == 1


def test_public_room_is_the_default_and_does_not_accept_a_password() -> None:
    room = create_room()

    assert room.config.visibility is RoomVisibility.PUBLIC
    assert room.config.password_required is False

    with pytest.raises(RoomRuleViolation, match="INVALID_ROOM_PASSWORD"):
        create_room(room_password="public-password")


@pytest.mark.parametrize("length", [4, 20])
def test_private_room_accepts_password_at_mvp_length_boundaries(length: int) -> None:
    password = "p" * length

    room = create_room(visibility=RoomVisibility.PRIVATE, room_password=password)

    assert room.config.visibility is RoomVisibility.PRIVATE
    assert room.config.password_required is True
    assert "password" not in {field.name for field in fields(room.config)}
    assert password not in repr(room.config)
    assert password not in repr(room.__dict__)


@pytest.mark.parametrize("room_password", [None, "p" * 3, "p" * 21])
def test_private_room_rejects_missing_or_out_of_range_password(
    room_password: str | None,
) -> None:
    with pytest.raises(RoomRuleViolation, match="INVALID_ROOM_PASSWORD"):
        create_room(
            visibility=RoomVisibility.PRIVATE,
            room_password=room_password,
        )


def test_private_room_join_requires_verified_access_without_mutating_on_rejection() -> None:
    room = create_room(
        visibility=RoomVisibility.PRIVATE,
        room_password="private-password",
    )

    assert_rejected_without_mutation(
        room,
        "ROOM_PASSWORD_INVALID",
        lambda: room.join(participant_id="member-2", actor_type=ActorType.MEMBER),
    )
    assert_rejected_without_mutation(
        room,
        "ROOM_PASSWORD_INVALID",
        lambda: room.join(
            participant_id="member-2",
            actor_type=ActorType.MEMBER,
            private_access_verified=False,
        ),
    )

    joined = room.join(
        participant_id="member-2",
        actor_type=ActorType.MEMBER,
        private_access_verified=True,
    )

    assert joined.participant_id == "member-2"
    assert room.state_version == 2


def test_public_room_join_does_not_require_private_access_verification() -> None:
    room = create_room()

    joined = room.join(participant_id="guest-1", actor_type=ActorType.GUEST)

    assert joined.participant_id == "guest-1"


@pytest.mark.parametrize(
    ("owner", "code"),
    [
        (
            Participant(
                participant_id="guest-owner",
                actor_type=ActorType.GUEST,
                joined_order=1,
            ),
            "MEMBER_REQUIRED_TO_CREATE_ROOM",
        ),
        (
            Participant(
                participant_id="member-owner",
                actor_type=ActorType.MEMBER,
                joined_order=2,
            ),
            "INVALID_INITIAL_OWNER",
        ),
        (
            Participant(
                participant_id="member-owner",
                actor_type=ActorType.MEMBER,
                joined_order=1,
                connected=False,
            ),
            "INVALID_INITIAL_OWNER",
        ),
        (
            Participant(
                participant_id="member-owner",
                actor_type=ActorType.MEMBER,
                joined_order=1,
                team=Team.BLACK,
            ),
            "INVALID_INITIAL_OWNER",
        ),
        (
            Participant(
                participant_id="member-owner",
                actor_type=ActorType.MEMBER,
                joined_order=1,
                ready=True,
            ),
            "INVALID_INITIAL_OWNER",
        ),
    ],
)
def test_direct_room_construction_cannot_bypass_initial_owner_invariants(
    owner: Participant,
    code: str,
) -> None:
    with pytest.raises(RoomRuleViolation, match=code):
        Room(config=RoomConfig(name="room", minimum_ready=2), owner=owner)


def test_join_assigns_stable_order_and_enforces_capacity_and_identity() -> None:
    room = create_room(max_participants=2)
    joined = room.join(participant_id="guest-1", actor_type=ActorType.GUEST)

    assert joined.joined_order == 2
    assert room.state_version == 2
    assert_rejected_without_mutation(
        room,
        "PARTICIPANT_ALREADY_JOINED",
        lambda: room.join(participant_id="guest-1", actor_type=ActorType.GUEST),
    )
    assert_rejected_without_mutation(
        room,
        "ROOM_CAPACITY_REACHED",
        lambda: room.join(participant_id="member-2", actor_type=ActorType.MEMBER),
    )


def test_invalid_or_unknown_participant_is_rejected() -> None:
    with pytest.raises(RoomRuleViolation, match="INVALID_PARTICIPANT_ID"):
        Room.create(
            config=RoomConfig(name="room", minimum_ready=2),
            owner_id="",
            owner_type=ActorType.MEMBER,
        )

    room = create_room()
    assert_rejected_without_mutation(
        room,
        "PARTICIPANT_NOT_FOUND",
        lambda: room.set_ready(participant_id="unknown", ready=False),
    )


def test_team_change_resets_only_that_participants_ready() -> None:
    room = create_room()
    join_ready_player(room, "member-2", Team.WHITE)
    room.change_team(participant_id="member-owner", team=Team.BLACK)
    room.set_ready(participant_id="member-owner", ready=True)
    version_before = room.state_version

    room.change_team(participant_id="member-owner", team=Team.WHITE)

    assert room.participant("member-owner").ready is False
    assert room.participant("member-2").ready is True
    assert room.state_version == version_before + 1


def test_repeating_team_ready_and_vote_time_values_is_a_no_op() -> None:
    room = create_room()
    room.change_team(participant_id="member-owner", team=Team.BLACK)
    room.set_ready(participant_id="member-owner", ready=True)
    version_before = room.state_version

    room.change_team(participant_id="member-owner", team=Team.BLACK)
    room.set_ready(participant_id="member-owner", ready=True)
    room.change_vote_seconds(actor_id="member-owner", vote_seconds=15)

    assert room.state_version == version_before
    assert room.participant("member-owner").ready is True


def test_ready_requires_team_and_rejected_command_does_not_mutate() -> None:
    room = create_room()

    assert_rejected_without_mutation(
        room,
        "TEAM_REQUIRED_TO_READY",
        lambda: room.set_ready(participant_id="member-owner", ready=True),
    )


def test_vote_time_change_requires_owner_and_resets_every_ready_once() -> None:
    room = create_room()
    join_ready_player(room, "member-2", Team.WHITE)
    room.change_team(participant_id="member-owner", team=Team.BLACK)
    room.set_ready(participant_id="member-owner", ready=True)
    version_before = room.state_version

    assert_rejected_without_mutation(
        room,
        "OWNER_REQUIRED",
        lambda: room.change_vote_seconds(actor_id="member-2", vote_seconds=30),
    )
    room.change_vote_seconds(actor_id="member-owner", vote_seconds=30)

    assert room.config.vote_seconds == 30
    assert all(not participant.ready for participant in room.participants)
    assert room.state_version == version_before + 1


def test_game_start_requires_minimum_ready_and_both_teams() -> None:
    room = create_room(minimum_ready=2)
    room.change_team(participant_id="member-owner", team=Team.BLACK)
    room.set_ready(participant_id="member-owner", ready=True)
    assert_rejected_without_mutation(
        room,
        "MINIMUM_READY_NOT_MET",
        lambda: room.start_game(actor_id="member-owner", game_id="game-1"),
    )

    join_ready_player(room, "member-2", Team.BLACK)
    assert_rejected_without_mutation(
        room,
        "BOTH_TEAMS_REQUIRED",
        lambda: room.start_game(actor_id="member-owner", game_id="game-1"),
    )


def test_game_start_freezes_player_and_spectator_roster() -> None:
    room = create_room(minimum_ready=2)
    room.change_team(participant_id="member-owner", team=Team.BLACK)
    room.set_ready(participant_id="member-owner", ready=True)
    join_ready_player(room, "member-2", Team.WHITE)
    room.join(participant_id="guest-spectator", actor_type=ActorType.GUEST)
    version_before = room.state_version

    roster = room.start_game(actor_id="member-owner", game_id="game-1")

    assert room.status is RoomStatus.PLAYING
    assert roster.player_ids == ("member-owner", "member-2")
    assert roster.entries[-1].role is ParticipantRole.SPECTATOR
    assert roster.entries[-1].team is Team.NONE
    assert room.state_version == version_before + 1


def test_owner_disconnect_keeps_owner_and_ready_state_during_reconnect_lease() -> None:
    room = create_room()
    room.join(participant_id="guest-first", actor_type=ActorType.GUEST)
    room.join(participant_id="member-earliest", actor_type=ActorType.MEMBER)
    room.join(participant_id="member-later", actor_type=ActorType.MEMBER)
    room.change_team(participant_id="member-owner", team=Team.BLACK)
    room.set_ready(participant_id="member-owner", ready=True)
    room.change_team(participant_id="member-earliest", team=Team.WHITE)
    room.set_ready(participant_id="member-earliest", ready=True)
    version_before = room.state_version

    result = room.disconnect(
        participant_id="member-owner",
        reason=DisconnectReason.PARTICIPANT_CONNECTION_LOST,
    )

    assert result.new_owner_id == "member-owner"
    assert result.room_closed is False
    assert room.owner_id == "member-owner"
    assert room.participant("member-owner").connected is False
    assert room.participant("member-owner").ready is True
    assert room.participant("member-earliest").ready is True
    assert room.state_version == version_before + 1


def test_owner_reconnect_within_lease_preserves_ownership_and_ready_state() -> None:
    room = create_room()
    room.join(participant_id="member-2", actor_type=ActorType.MEMBER)
    room.change_team(participant_id="member-owner", team=Team.BLACK)
    room.set_ready(participant_id="member-owner", ready=True)
    room.disconnect(
        participant_id="member-owner",
        reason=DisconnectReason.PARTICIPANT_CONNECTION_LOST,
    )
    version_before = room.state_version

    room.reconnect(participant_id="member-owner")

    assert room.owner_id == "member-owner"
    assert room.participant("member-owner").connected is True
    assert room.participant("member-owner").ready is True
    assert room.state_version == version_before + 1

    room.reconnect(participant_id="member-owner")
    assert room.state_version == version_before + 1


def test_non_owner_disconnect_preserves_ready_during_reconnect_lease() -> None:
    room = create_room()
    join_ready_player(room, "member-2", Team.WHITE)
    version_before = room.state_version

    first_result = room.disconnect(
        participant_id="member-2",
        reason=DisconnectReason.PARTICIPANT_CONNECTION_LOST,
    )
    second_result = room.disconnect(
        participant_id="member-2",
        reason=DisconnectReason.PARTICIPANT_CONNECTION_LOST,
    )

    assert first_result.new_owner_id == "member-owner"
    assert second_result.new_owner_id == "member-owner"
    assert room.participant("member-2").ready is True
    assert room.participant("member-2").connected is False
    assert room.state_version == version_before + 1


def test_disconnected_ready_participant_is_not_eligible_to_start() -> None:
    room = create_room(minimum_ready=2)
    room.change_team(participant_id="member-owner", team=Team.BLACK)
    room.set_ready(participant_id="member-owner", ready=True)
    join_ready_player(room, "member-2", Team.WHITE)
    room.disconnect(
        participant_id="member-2",
        reason=DisconnectReason.PARTICIPANT_CONNECTION_LOST,
    )

    assert room.participant("member-2").ready is True
    assert room.participant("member-2").connected is False
    assert_rejected_without_mutation(
        room,
        "MINIMUM_READY_NOT_MET",
        lambda: room.start_game(actor_id="member-owner", game_id="game-1"),
    )


def test_disconnected_owner_cannot_start_during_reconnect_lease() -> None:
    room = create_room(minimum_ready=2)
    room.change_team(participant_id="member-owner", team=Team.BLACK)
    room.set_ready(participant_id="member-owner", ready=True)
    join_ready_player(room, "member-2", Team.WHITE)
    room.disconnect(
        participant_id="member-owner",
        reason=DisconnectReason.PARTICIPANT_CONNECTION_LOST,
    )

    assert_rejected_without_mutation(
        room,
        "PARTICIPANT_DISCONNECTED",
        lambda: room.start_game(actor_id="member-owner", game_id="game-1"),
    )


def test_waiting_room_closes_without_game_action_when_no_successor_exists() -> None:
    room = create_room()
    room.join(participant_id="guest-1", actor_type=ActorType.GUEST)
    version_before = room.state_version

    result = room.leave(participant_id="member-owner")

    assert result.room_closed is True
    assert result.game_termination is GameTermination.NONE
    assert room.status is RoomStatus.CLOSED
    assert room.owner_id is None
    assert room.state_version == version_before + 1


def test_playing_room_requests_system_invalid_only_when_room_must_close() -> None:
    room = create_room()
    room.change_team(participant_id="member-owner", team=Team.BLACK)
    room.set_ready(participant_id="member-owner", ready=True)
    room.join(participant_id="guest-player", actor_type=ActorType.GUEST)
    room.change_team(participant_id="guest-player", team=Team.WHITE)
    room.set_ready(participant_id="guest-player", ready=True)
    room.start_game(actor_id="member-owner", game_id="game-1")

    result = room.leave(participant_id="member-owner")

    assert result.room_closed is True
    assert result.game_termination is GameTermination.SYSTEM_INVALID
    assert result.terminated_game_id == "game-1"


def test_non_owner_departure_keeps_owner_and_room_open() -> None:
    room = create_room()
    room.join(participant_id="guest-1", actor_type=ActorType.GUEST)
    version_before = room.state_version

    result = room.leave(participant_id="guest-1")

    assert result.new_owner_id == "member-owner"
    assert result.room_closed is False
    assert room.status is RoomStatus.WAITING
    assert room.state_version == version_before + 1


def test_guest_identity_promotion_preserves_participant_state() -> None:
    room = create_room()
    room.join(participant_id="guest-1", actor_type=ActorType.GUEST)
    room.change_team(participant_id="guest-1", team=Team.WHITE)
    room.set_ready(participant_id="guest-1", ready=True)
    version_before = room.state_version

    room.change_identity(participant_id="guest-1", actor_type=ActorType.MEMBER)

    promoted = room.participant("guest-1")
    assert promoted.actor_type is ActorType.MEMBER
    assert promoted.team is Team.WHITE
    assert promoted.ready is True
    assert room.state_version == version_before + 1
    promoted_version = room.state_version
    room.change_identity(participant_id="guest-1", actor_type=ActorType.MEMBER)
    assert room.participant("guest-1") == promoted
    assert room.state_version == promoted_version
    assert_rejected_without_mutation(
        room,
        "ROOM_IDENTITY_CHANGE_NOT_ALLOWED",
        lambda: room.change_identity(participant_id="guest-1", actor_type=ActorType.GUEST),
    )


def test_empty_game_id_and_completion_in_wrong_state_preserve_room() -> None:
    room = create_room()
    assert_rejected_without_mutation(
        room,
        "INVALID_GAME_ID",
        lambda: room.start_game(actor_id="member-owner", game_id=""),
    )
    assert_rejected_without_mutation(
        room, "ROOM_NOT_PLAYING", lambda: room.complete_game(game_id="game-1")
    )
    join_ready_player(room, "black", Team.BLACK)
    join_ready_player(room, "white", Team.WHITE)
    room.start_game(actor_id="member-owner", game_id="game-1")
    assert_rejected_without_mutation(
        room, "STALE_GAME", lambda: room.complete_game(game_id="game-old")
    )
    assert room.game_id == "game-1"


def test_platform_failure_cannot_be_recorded_as_participant_disconnect() -> None:
    room = create_room()

    assert_rejected_without_mutation(
        room,
        "PLATFORM_FAILURE_IS_NOT_PARTICIPANT_DISCONNECT",
        lambda: room.disconnect(
            participant_id="member-owner",
            reason=DisconnectReason.PLATFORM_FAILURE,
        ),
    )


def test_room_commands_are_rejected_after_room_closes() -> None:
    room = create_room()
    room.leave(participant_id="member-owner")

    assert_rejected_without_mutation(
        room,
        "ROOM_CLOSED",
        lambda: room.join(participant_id="member-2", actor_type=ActorType.MEMBER),
    )


def test_waiting_only_commands_are_rejected_while_playing() -> None:
    room = create_room()
    room.change_team(participant_id="member-owner", team=Team.BLACK)
    room.set_ready(participant_id="member-owner", ready=True)
    join_ready_player(room, "member-2", Team.WHITE)
    room.start_game(actor_id="member-owner", game_id="game-1")

    assert_rejected_without_mutation(
        room,
        "ROOM_NOT_WAITING",
        lambda: room.set_ready(participant_id="member-owner", ready=False),
    )
