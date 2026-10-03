from __future__ import annotations

import pytest

from seokpan.room.application import (
    ROOM_CLOSED_TOMBSTONE_TTL_MS,
    ROOM_DISCONNECT_LEASE_MS,
    ROOM_REQUEST_DEDUPE_TTL_MS,
    ChangeRoomIdentity,
    ChangeRoomTeam,
    ChangeRoomVoteSeconds,
    CompleteRoomGame,
    ConnectRoomParticipant,
    DisconnectRoomParticipant,
    ExpireRoomDisconnect,
    LeaveRoomRuntime,
    SetRoomReady,
    StartRoomGame,
)
from seokpan.room.domain import (
    ActorType,
    GameTermination,
    ParticipantRole,
    RoomRuleViolation,
    RoomStatus,
    RoomVisibility,
    Team,
)

from .conftest import RoomRuntimeHarness, create_room, digest, join_guest, join_member


@pytest.mark.asyncio
async def test_start_game_returns_stable_roster_and_game_id(
    room_harness: RoomRuntimeHarness,
) -> None:
    await room_harness.adapter.create(create_room(minimum_ready=2))
    await room_harness.adapter.join(
        join_member("member-2", request_id="join-start", session_character="b")
    )
    await room_harness.adapter.change_team(
        ChangeRoomTeam("room-1", "team-1", "member-1", Team.BLACK, 2)
    )
    await room_harness.adapter.set_ready(SetRoomReady("room-1", "ready-1", "member-1", True, 3))
    await room_harness.adapter.change_team(
        ChangeRoomTeam("room-1", "team-2", "member-2", Team.WHITE, 4)
    )
    await room_harness.adapter.set_ready(SetRoomReady("room-1", "ready-2", "member-2", True, 5))
    command = StartRoomGame("room-1", "start-1", "member-1", "game-1", 6)

    first = await room_harness.adapter.start_game(command)
    replay = await room_harness.adapter.start_game(command)

    assert first.snapshot is not None
    assert first.snapshot.game_id == "game-1"
    assert first.snapshot.state_version == 7
    assert first.start_roster is not None
    assert all(item.role is ParticipantRole.PLAYER for item in first.start_roster.entries)
    assert replay.replayed is True
    assert replay.start_roster == first.start_roster


@pytest.mark.asyncio
async def test_complete_game_returns_room_to_waiting_and_clears_ready(
    room_harness: RoomRuntimeHarness,
) -> None:
    await room_harness.adapter.create(create_room(minimum_ready=2))
    await room_harness.adapter.join(
        join_member("member-2", request_id="join-complete", session_character="b")
    )
    await room_harness.adapter.change_team(
        ChangeRoomTeam("room-1", "team-1", "member-1", Team.BLACK, 2)
    )
    await room_harness.adapter.set_ready(SetRoomReady("room-1", "ready-1", "member-1", True, 3))
    await room_harness.adapter.change_team(
        ChangeRoomTeam("room-1", "team-2", "member-2", Team.WHITE, 4)
    )
    await room_harness.adapter.set_ready(SetRoomReady("room-1", "ready-2", "member-2", True, 5))
    await room_harness.adapter.start_game(
        StartRoomGame("room-1", "start-complete", "member-1", "game-1", 6)
    )
    command = CompleteRoomGame("room-1", "complete-1", "game-1", 7, 13)

    result = await room_harness.adapter.complete_game(command)
    replay = await room_harness.adapter.complete_game(command)

    assert result.snapshot is not None
    assert result.snapshot.status is RoomStatus.WAITING
    assert result.snapshot.game_id is None
    assert result.snapshot.last_game_id == "game-1"
    assert result.snapshot.last_game_turn_no == 13
    assert result.snapshot.state_version == 8
    assert all(not item.ready for item in result.snapshot.participants)
    assert replay.replayed is True
    await room_harness.adapter.set_ready(
        SetRoomReady("room-1", "next-ready-1", "member-1", True, 8)
    )
    await room_harness.adapter.set_ready(
        SetRoomReady("room-1", "next-ready-2", "member-2", True, 9)
    )
    next_game = await room_harness.adapter.start_game(
        StartRoomGame("room-1", "next-game", "member-1", "game-2", 10)
    )
    assert next_game.snapshot is not None
    assert next_game.snapshot.game_id == "game-2"
    assert next_game.snapshot.last_game_id == "game-1"
    assert next_game.snapshot.last_game_turn_no == 13


def test_room_completion_requires_actual_positive_final_turn() -> None:
    with pytest.raises(RoomRuleViolation, match="INVALID_TURN_NUMBER"):
        CompleteRoomGame("room-1", "complete", "game-1", 7, 0)


@pytest.mark.asyncio
async def test_create_and_read_snapshot_without_password_hash(
    room_harness: RoomRuntimeHarness,
) -> None:
    result = await room_harness.adapter.create(create_room(visibility=RoomVisibility.PRIVATE))

    assert result.connection_generation == 1
    assert result.snapshot is not None
    assert result.snapshot.password_required is True
    assert "argon2" not in repr(result.snapshot)
    assert digest("a") not in repr(result.snapshot)
    assert await room_harness.adapter.get("room-1") == result.snapshot
    assert (await room_harness.adapter.get_private_access_hash("room-1")) is not None


@pytest.mark.asyncio
async def test_private_join_requires_preverified_access_without_mutation(
    room_harness: RoomRuntimeHarness,
) -> None:
    created = await room_harness.adapter.create(create_room(visibility=RoomVisibility.PRIVATE))

    with pytest.raises(RoomRuleViolation, match="ROOM_PASSWORD_INVALID"):
        await room_harness.adapter.join(
            join_member("member-2", request_id="join-1", session_character="b")
        )

    assert await room_harness.adapter.get("room-1") == created.snapshot

    joined = await room_harness.adapter.join(
        join_member(
            "member-2",
            request_id="join-2",
            session_character="b",
            private_access_verified=True,
        )
    )
    assert joined.snapshot is not None
    assert tuple(item.participant_id for item in joined.snapshot.participants) == (
        "member-1",
        "member-2",
    )


@pytest.mark.asyncio
async def test_team_ready_and_owner_setting_follow_domain_versioning(
    room_harness: RoomRuntimeHarness,
) -> None:
    await room_harness.adapter.create(create_room())
    await room_harness.adapter.join(
        join_member("member-2", request_id="join-1", session_character="b")
    )
    teamed = await room_harness.adapter.change_team(
        ChangeRoomTeam("room-1", "team-1", "member-2", Team.BLACK, 2)
    )
    readied = await room_harness.adapter.set_ready(
        SetRoomReady("room-1", "ready-1", "member-2", True, 3)
    )
    changed = await room_harness.adapter.change_vote_seconds(
        ChangeRoomVoteSeconds("room-1", "setting-1", "member-1", 30, 4)
    )

    assert teamed.snapshot is not None
    assert readied.snapshot is not None
    assert changed.snapshot is not None
    assert teamed.snapshot.state_version + 1 == readied.snapshot.state_version
    assert readied.snapshot.state_version + 1 == changed.snapshot.state_version
    assert all(not item.ready for item in changed.snapshot.participants)


@pytest.mark.asyncio
async def test_guest_identity_promotion_preserves_room_participant_state(
    room_harness: RoomRuntimeHarness,
) -> None:
    await room_harness.adapter.create(create_room())
    await room_harness.adapter.join(join_guest("guest-1", request_id="join-guest"))
    teamed = await room_harness.adapter.change_team(
        ChangeRoomTeam("room-1", "team-guest", "guest-1", Team.WHITE, 2)
    )
    assert teamed.snapshot is not None

    promoted = await room_harness.adapter.change_identity(
        ChangeRoomIdentity("room-1", "identity-1", "guest-1", ActorType.MEMBER, digest("b"), 3)
    )

    assert promoted.snapshot is not None
    participant = promoted.snapshot.participants[1]
    assert participant.participant_id == "guest-1"
    assert participant.actor_type is ActorType.MEMBER
    assert participant.team is Team.WHITE
    assert promoted.snapshot.state_version == 4


@pytest.mark.asyncio
async def test_new_generation_supersedes_old_and_stale_disconnect_is_ignored(
    room_harness: RoomRuntimeHarness,
) -> None:
    await room_harness.adapter.create(create_room())
    connected = await room_harness.adapter.connect(
        ConnectRoomParticipant("room-1", "connect-1", "member-1", digest("b"), 1)
    )
    assert connected.connection_generation == 2
    assert connected.snapshot is not None
    assert connected.snapshot.state_version == 1

    stale = await room_harness.adapter.disconnect(
        DisconnectRoomParticipant("room-1", "disconnect-old", "member-1", 1, 1)
    )

    assert stale.stale_connection is True
    assert stale.snapshot is not None
    assert stale.snapshot.owner_id == "member-1"
    assert stale.snapshot.participants[0].connected is True


@pytest.mark.asyncio
async def test_owner_disconnect_lease_preserves_owner_until_expiry(
    room_harness: RoomRuntimeHarness,
) -> None:
    await room_harness.adapter.create(create_room())
    await room_harness.adapter.join(
        join_member("member-2", request_id="join-1", session_character="b")
    )
    await room_harness.adapter.join(
        join_member(
            "member-3",
            request_id="join-2",
            session_character="c",
            expected_state_version=2,
        )
    )
    expected_version = 3
    for index, participant_id in enumerate(("member-1", "member-2", "member-3"), 1):
        await room_harness.adapter.change_team(
            ChangeRoomTeam(
                "room-1",
                f"team-{index}",
                participant_id,
                Team.BLACK,
                expected_version,
            )
        )
        expected_version += 1
        await room_harness.adapter.set_ready(
            SetRoomReady(
                "room-1",
                f"ready-{index}",
                participant_id,
                True,
                expected_version,
            )
        )
        expected_version += 1

    result = await room_harness.adapter.disconnect(
        DisconnectRoomParticipant("room-1", "disconnect-1", "member-1", 1, expected_version)
    )

    assert result.snapshot is not None
    assert result.snapshot.owner_id == "member-1"
    assert result.snapshot.participants[0].connected is False
    assert all(participant.ready for participant in result.snapshot.participants)
    assert result.disconnect_expires_at_ms == 1_000 + ROOM_DISCONNECT_LEASE_MS

    room_harness.clock.advance(ROOM_DISCONNECT_LEASE_MS)
    expired = await room_harness.adapter.expire_disconnect(
        ExpireRoomDisconnect(
            "room-1",
            "expire-owner",
            "member-1",
            1,
            expected_version + 1,
        )
    )
    assert expired.snapshot is not None
    assert expired.snapshot.owner_id == "member-2"
    assert all(not participant.ready for participant in expired.snapshot.participants)


@pytest.mark.asyncio
async def test_disconnected_ready_participant_does_not_count_for_game_start(
    room_harness: RoomRuntimeHarness,
) -> None:
    await room_harness.adapter.create(create_room(minimum_ready=2))
    await room_harness.adapter.join(
        join_member("member-2", request_id="join-start-disconnected", session_character="b")
    )
    await room_harness.adapter.change_team(
        ChangeRoomTeam("room-1", "team-owner", "member-1", Team.BLACK, 2)
    )
    await room_harness.adapter.set_ready(SetRoomReady("room-1", "ready-owner", "member-1", True, 3))
    await room_harness.adapter.change_team(
        ChangeRoomTeam("room-1", "team-member", "member-2", Team.WHITE, 4)
    )
    await room_harness.adapter.set_ready(
        SetRoomReady("room-1", "ready-member", "member-2", True, 5)
    )
    disconnected = await room_harness.adapter.disconnect(
        DisconnectRoomParticipant("room-1", "disconnect-member", "member-2", 1, 6)
    )
    assert disconnected.snapshot is not None
    assert disconnected.snapshot.participants[1].ready is True
    assert disconnected.snapshot.participants[1].connected is False

    with pytest.raises(RoomRuleViolation, match="MINIMUM_READY_NOT_MET"):
        await room_harness.adapter.start_game(
            StartRoomGame(
                "room-1",
                "start-with-disconnected",
                "member-1",
                "game-1",
                disconnected.snapshot.state_version,
            )
        )


@pytest.mark.asyncio
async def test_guest_disconnect_uses_lease_and_expiry_removes_participant(
    room_harness: RoomRuntimeHarness,
) -> None:
    await room_harness.adapter.create(create_room())
    await room_harness.adapter.join(join_guest("guest-1", request_id="join-guest"))
    disconnected = await room_harness.adapter.disconnect(
        DisconnectRoomParticipant("room-1", "disconnect-1", "guest-1", 1, 2)
    )
    assert disconnected.snapshot is not None
    assert disconnected.snapshot.participants[1].connected is False

    with pytest.raises(RoomRuleViolation, match="DISCONNECT_LEASE_ACTIVE"):
        await room_harness.adapter.expire_disconnect(
            ExpireRoomDisconnect("room-1", "expire-early", "guest-1", 1, 3)
        )

    room_harness.clock.advance(ROOM_DISCONNECT_LEASE_MS)
    expired = await room_harness.adapter.expire_disconnect(
        ExpireRoomDisconnect("room-1", "expire-ok", "guest-1", 1, 3)
    )
    assert expired.snapshot is not None
    assert tuple(item.participant_id for item in expired.snapshot.participants) == ("member-1",)


@pytest.mark.asyncio
async def test_disconnect_removes_only_current_vote_through_atomic_seam(
    room_harness: RoomRuntimeHarness,
) -> None:
    await room_harness.adapter.create(create_room())
    await room_harness.adapter.join(
        join_member("member-2", request_id="join-1", session_character="b")
    )
    room_harness.observable.seed_current_vote("room-1", 3, "member-2")
    room_harness.observable.seed_current_vote("room-1", 3, "member-1")

    result = await room_harness.adapter.disconnect(
        DisconnectRoomParticipant("room-1", "disconnect-1", "member-2", 1, 2, 3)
    )

    assert result.vote_removed is True
    assert not room_harness.observable.has_current_vote("room-1", 3, "member-2")
    assert room_harness.observable.has_current_vote("room-1", 3, "member-1")


@pytest.mark.asyncio
async def test_last_member_departure_closes_room_with_tombstone_without_game_loss(
    room_harness: RoomRuntimeHarness,
) -> None:
    await room_harness.adapter.create(create_room())
    result = await room_harness.adapter.leave(LeaveRoomRuntime("room-1", "leave-1", "member-1", 1))

    assert result.room_closed is True
    assert result.snapshot is None
    assert result.game_termination is GameTermination.NONE
    assert await room_harness.adapter.get("room-1") is None
    assert room_harness.observable.has_tombstone("room-1")

    with pytest.raises(RoomRuleViolation, match="ROOM_RECENTLY_CLOSED"):
        await room_harness.adapter.create(create_room(request_id="create-2"))

    room_harness.clock.advance(ROOM_CLOSED_TOMBSTONE_TTL_MS)
    recreated = await room_harness.adapter.create(create_room(request_id="create-3"))
    assert recreated.snapshot is not None


@pytest.mark.asyncio
async def test_playing_room_closure_records_retryable_game_invalidation(
    room_harness: RoomRuntimeHarness,
) -> None:
    await room_harness.adapter.create(create_room(minimum_ready=2))
    await room_harness.adapter.join(
        join_guest("guest-1", request_id="join-1", expected_state_version=1)
    )
    await room_harness.adapter.change_team(
        ChangeRoomTeam("room-1", "black", "member-1", Team.BLACK, 2)
    )
    await room_harness.adapter.set_ready(SetRoomReady("room-1", "ready-black", "member-1", True, 3))
    await room_harness.adapter.change_team(
        ChangeRoomTeam("room-1", "white", "guest-1", Team.WHITE, 4)
    )
    await room_harness.adapter.set_ready(SetRoomReady("room-1", "ready-white", "guest-1", True, 5))
    await room_harness.adapter.start_game(StartRoomGame("room-1", "start", "member-1", "game-1", 6))

    closed = await room_harness.adapter.leave(
        LeaveRoomRuntime("room-1", "leave-owner", "member-1", 7)
    )

    assert closed.room_closed is True
    assert closed.game_termination is GameTermination.SYSTEM_INVALID
    assert closed.terminated_game_id == "game-1"
    assert closed.operation_at_ms == 1_000
    pending = await room_harness.adapter.pending_game_invalidations(limit=10)
    assert [(item.room_id, item.game_id, item.closed_at_ms) for item in pending] == [
        ("room-1", "game-1", 1_000)
    ]

    # The correctness marker outlives the ordinary 10-minute anti-reuse tombstone.
    room_harness.clock.advance(ROOM_CLOSED_TOMBSTONE_TTL_MS)
    assert await room_harness.adapter.pending_game_invalidations(limit=10) == pending

    await room_harness.adapter.complete_game_invalidation("room-1", "game-1")
    assert await room_harness.adapter.pending_game_invalidations(limit=10) == ()

    # The closure marker still follows the request-dedupe horizon for bounded retention.
    room_harness.clock.advance(ROOM_REQUEST_DEDUPE_TTL_MS)
    assert await room_harness.adapter.pending_game_invalidations(limit=10) == ()


@pytest.mark.asyncio
async def test_request_id_replay_returns_original_result_without_second_mutation(
    room_harness: RoomRuntimeHarness,
) -> None:
    await room_harness.adapter.create(create_room())
    command = join_member("member-2", request_id="join-1", session_character="b")
    first = await room_harness.adapter.join(command)
    replay = await room_harness.adapter.join(command)

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.snapshot == first.snapshot


@pytest.mark.asyncio
async def test_request_id_reuse_with_different_command_is_rejected(
    room_harness: RoomRuntimeHarness,
) -> None:
    await room_harness.adapter.create(create_room())
    await room_harness.adapter.join(
        join_member("member-2", request_id="same-request", session_character="b")
    )

    with pytest.raises(RoomRuleViolation, match="REQUEST_ID_CONFLICT"):
        await room_harness.adapter.join(
            join_member("member-3", request_id="same-request", session_character="c")
        )

    snapshot = await room_harness.adapter.get("room-1")
    assert snapshot is not None
    assert tuple(item.participant_id for item in snapshot.participants) == (
        "member-1",
        "member-2",
    )


@pytest.mark.asyncio
async def test_join_uses_current_server_state_when_lobby_version_is_stale(
    room_harness: RoomRuntimeHarness,
) -> None:
    created = await room_harness.adapter.create(create_room())
    assert created.snapshot is not None

    joined = await room_harness.adapter.join(
        join_member(
            "member-2",
            request_id="join-stale-observation",
            session_character="b",
            expected_state_version=created.snapshot.state_version + 10,
        )
    )

    assert joined.snapshot is not None
    assert tuple(item.participant_id for item in joined.snapshot.participants) == (
        "member-1",
        "member-2",
    )
    assert joined.snapshot.state_version == created.snapshot.state_version + 1


@pytest.mark.asyncio
async def test_stale_version_still_rejects_versioned_room_mutations(
    room_harness: RoomRuntimeHarness,
) -> None:
    created = await room_harness.adapter.create(create_room())
    assert created.snapshot is not None

    with pytest.raises(RoomRuleViolation, match="STATE_VERSION_CONFLICT"):
        await room_harness.adapter.change_team(
            ChangeRoomTeam(
                "room-1",
                "team-stale",
                "member-1",
                Team.BLACK,
                created.snapshot.state_version + 10,
            )
        )

    assert await room_harness.adapter.get("room-1") == created.snapshot
