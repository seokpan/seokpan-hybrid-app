from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest

from seokpan.api.identity import SessionActorType
from seokpan.api.room import RoomApiServices, room_snapshot_response
from seokpan.room.application import RoomRuntimeParticipant, RoomRuntimeSnapshot
from seokpan.room.domain import ActorType, RoomConfig, RoomStatus, Team


def snapshot() -> RoomRuntimeSnapshot:
    return RoomRuntimeSnapshot(
        room_id="room-1",
        config=RoomConfig(name="표시 검증", minimum_ready=2),
        status=RoomStatus.WAITING,
        owner_id="participant-internal-1",
        state_version=1,
        participants=(
            RoomRuntimeParticipant(
                participant_id="participant-internal-1",
                actor_type=ActorType.MEMBER,
                joined_order=1,
                connected=True,
                team=Team.NONE,
                ready=False,
            ),
        ),
    )


@pytest.mark.asyncio
async def test_room_presentation_never_exposes_participant_id_when_identity_is_missing() -> None:
    services = SimpleNamespace(
        rooms=SimpleNamespace(resolve_participant_identity=AsyncMock(return_value=None)),
        identity=SimpleNamespace(
            members=SimpleNamespace(find_member=AsyncMock(return_value=None)),
        ),
    )

    response = await room_snapshot_response(cast(RoomApiServices, services), snapshot())

    assert response.participants[0].display_name == "참가자"
    assert response.participants[0].display_name != "participant-internal-1"


@pytest.mark.asyncio
async def test_room_presentation_uses_neutral_fallback_when_member_lookup_misses() -> None:
    services = SimpleNamespace(
        rooms=SimpleNamespace(
            resolve_participant_identity=AsyncMock(
                return_value=SimpleNamespace(
                    actor_type=SessionActorType.MEMBER,
                    actor_id="42",
                )
            )
        ),
        identity=SimpleNamespace(
            members=SimpleNamespace(find_member=AsyncMock(return_value=None)),
        ),
    )

    response = await room_snapshot_response(cast(RoomApiServices, services), snapshot())

    assert response.participants[0].display_name == "참가자"
    assert response.participants[0].display_name != "participant-internal-1"
