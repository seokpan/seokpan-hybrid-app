from dataclasses import replace

import pytest

from seokpan.room.application import (
    ChangeRoomTeam,
    ConnectRoomParticipant,
    DisconnectRoomParticipant,
    ExpireRoomDisconnect,
    KickRoomParticipant,
    SetRoomReady,
    StartRoomGame,
)
from seokpan.room.application.runtime import ROOM_DISCONNECT_LEASE_MS
from seokpan.room.domain import RoomRuleViolation, RoomStatus, Team

from .conftest import RoomRuntimeHarness, create_room, digest, join_guest, join_member


@pytest.mark.asyncio
@pytest.mark.parametrize("guest", (False, True))
async def test_kick_replay_and_changed_target_conflict(
    room_harness: RoomRuntimeHarness, guest: bool
) -> None:
    adapter = room_harness.adapter
    await adapter.create(create_room())
    join = (
        join_guest("target", request_id="join")
        if guest
        else join_member("target", request_id="join", session_character="b")
    )
    await adapter.join(join)
    command = KickRoomParticipant("room-1", "kick", "member-1", "target", 2)
    result = await adapter.kick(command)
    assert result.snapshot is not None
    assert result.snapshot.state_version == 3
    assert [p.participant_id for p in result.snapshot.participants] == ["member-1"]
    assert not result.vote_removed
    assert result.departure is not None and not result.departure.room_closed
    assert (await adapter.kick(command)).replayed
    with pytest.raises(RoomRuleViolation, match="REQUEST_ID_CONFLICT"):
        await adapter.kick(replace(command, target_id="member-1"))
    assert await adapter.get("room-1") == result.snapshot
    # Old connection callbacks cannot recreate a removed participant.
    with pytest.raises(RoomRuleViolation, match="PARTICIPANT_NOT_FOUND"):
        await adapter.connect(
            ConnectRoomParticipant("room-1", "reconnect", "target", digest("b"), 3)
        )


@pytest.mark.asyncio
async def test_kick_removes_disconnect_lease_without_affecting_others(
    room_harness: RoomRuntimeHarness,
) -> None:
    adapter = room_harness.adapter
    await adapter.create(create_room())
    await adapter.join(join_member("target", request_id="join", session_character="b"))
    await adapter.change_team(ChangeRoomTeam("room-1", "team", "member-1", Team.BLACK, 2))
    await adapter.set_ready(SetRoomReady("room-1", "ready", "member-1", True, 3))
    await adapter.disconnect(DisconnectRoomParticipant("room-1", "disconnect", "target", 1, 4))
    result = await adapter.kick(KickRoomParticipant("room-1", "kick", "member-1", "target", 5))
    assert result.snapshot is not None
    assert result.snapshot.participants[0].ready
    assert result.snapshot.state_version == 6
    room_harness.clock.advance(30_001)
    with pytest.raises(RoomRuleViolation, match="CONNECTION_NOT_FOUND"):
        await adapter.expire_disconnect(ExpireRoomDisconnect("room-1", "expire", "target", 1, 6))
    assert await adapter.get("room-1") == result.snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("actor", "target", "version", "code"),
    [
        ("target", "member-1", 2, "OWNER_REQUIRED"),
        ("missing-actor", "target", 2, "OWNER_REQUIRED"),
        ("member-1", "member-1", 2, "CANNOT_KICK_SELF"),
        ("member-1", "missing", 2, "PARTICIPANT_NOT_FOUND"),
        ("member-1", "target", 1, "STATE_VERSION_CONFLICT"),
    ],
)
async def test_rejected_kick_preserves_runtime(
    room_harness: RoomRuntimeHarness, actor: str, target: str, version: int, code: str
) -> None:
    adapter = room_harness.adapter
    await adapter.create(create_room())
    await adapter.join(join_member("target", request_id="join", session_character="b"))
    before = await adapter.get("room-1")
    with pytest.raises(RoomRuleViolation, match=code):
        await adapter.kick(KickRoomParticipant("room-1", "kick", actor, target, version))
    assert await adapter.get("room-1") == before


@pytest.mark.asyncio
@pytest.mark.parametrize("start_first", (True, False))
async def test_start_and_kick_only_one_can_use_the_same_version(
    room_harness: RoomRuntimeHarness, start_first: bool
) -> None:
    adapter = room_harness.adapter
    await adapter.create(create_room(minimum_ready=2))
    await adapter.join(join_member("target", request_id="join", session_character="b"))
    await adapter.change_team(ChangeRoomTeam("room-1", "team-owner", "member-1", Team.BLACK, 2))
    await adapter.set_ready(SetRoomReady("room-1", "ready-owner", "member-1", True, 3))
    await adapter.change_team(ChangeRoomTeam("room-1", "team-target", "target", Team.WHITE, 4))
    await adapter.set_ready(SetRoomReady("room-1", "ready-target", "target", True, 5))
    kick = KickRoomParticipant("room-1", "kick", "member-1", "target", 6)
    start = StartRoomGame("room-1", "start", "member-1", "game-1", 6)
    if start_first:
        result = await adapter.start_game(start)
        with pytest.raises(RoomRuleViolation, match="STATE_VERSION_CONFLICT"):
            await adapter.kick(kick)
        with pytest.raises(RoomRuleViolation, match="ROOM_NOT_WAITING"):
            await adapter.kick(replace(kick, expected_state_version=7))
        assert result.snapshot is not None and result.snapshot.status is RoomStatus.PLAYING
    else:
        result = await adapter.kick(kick)
        with pytest.raises(RoomRuleViolation, match="STATE_VERSION_CONFLICT"):
            await adapter.start_game(start)
        with pytest.raises(RoomRuleViolation, match="MINIMUM_READY_NOT_MET"):
            await adapter.start_game(replace(start, expected_state_version=7))
        assert result.snapshot is not None and result.snapshot.game_id is None
    assert await adapter.get("room-1") == result.snapshot


@pytest.mark.asyncio
async def test_old_kick_replay_does_not_remove_new_participation(
    room_harness: RoomRuntimeHarness,
) -> None:
    adapter = room_harness.adapter
    await adapter.create(create_room())
    await adapter.join(join_member("old-id", request_id="join", session_character="b"))
    command = KickRoomParticipant("room-1", "kick", "member-1", "old-id", 2)
    await adapter.kick(command)
    joined = await adapter.join(
        join_member(
            "new-id", request_id="new-join", session_character="b", expected_state_version=3
        )
    )
    assert (await adapter.kick(command)).replayed
    assert await adapter.get("room-1") == joined.snapshot


@pytest.mark.asyncio
async def test_disconnect_grace_delays_owner_handoff_until_expiry(
    room_harness: RoomRuntimeHarness,
) -> None:
    adapter = room_harness.adapter
    await adapter.create(create_room())
    await adapter.join(join_member("target", request_id="join", session_character="b"))

    await adapter.disconnect(DisconnectRoomParticipant("room-1", "disconnect", "member-1", 1, 2))

    during_grace = await adapter.get("room-1")
    assert during_grace is not None
    assert during_grace.owner_id == "member-1"
    owner = next(item for item in during_grace.participants if item.participant_id == "member-1")
    assert owner.connected is False

    with pytest.raises(RoomRuleViolation, match="STATE_VERSION_CONFLICT"):
        await adapter.kick(KickRoomParticipant("room-1", "kick", "member-1", "target", 2))

    room_harness.clock.advance(ROOM_DISCONNECT_LEASE_MS)
    expired = await adapter.expire_disconnect(
        ExpireRoomDisconnect("room-1", "expire-owner", "member-1", 1, 3)
    )

    assert expired.snapshot is not None
    assert expired.snapshot.owner_id == "target"
    assert [item.participant_id for item in expired.snapshot.participants] == ["target"]

    with pytest.raises(RoomRuleViolation, match="OWNER_REQUIRED"):
        await adapter.kick(KickRoomParticipant("room-1", "old-owner-kick", "member-1", "target", 4))


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("room_id", "", "INVALID_ROOM_ID"),
        ("request_id", "", "INVALID_REQUEST_ID"),
        ("actor_id", "", "INVALID_PARTICIPANT_ID"),
        ("target_id", "", "INVALID_PARTICIPANT_ID"),
        ("expected_state_version", 0, "INVALID_STATE_VERSION"),
    ],
)
def test_kick_command_validates_identifiers_and_version(
    field: str, value: object, code: str
) -> None:
    with pytest.raises(RoomRuleViolation, match=code):
        replace(KickRoomParticipant("room-1", "kick", "member-1", "target", 2), **{field: value})
