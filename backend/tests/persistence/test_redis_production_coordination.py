from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from seokpan.game.application import (
    DueTurn,
    TieSelectionRecord,
    TurnFinalizationApproval,
)
from seokpan.identity.application import (
    CreateSession,
    SessionActorType,
    SessionRecord,
    SessionRuleViolation,
    SessionTransitionUnavailable,
    digest_opaque_token,
)
from seokpan.persistence.redis import (
    RedisRoomParticipationResolver,
    RedisSessionWorkflow,
    RedisTurnCoordinator,
)
from seokpan.persistence.redis.common import RedisKeyspace, VersionedJsonCodec
from seokpan.room.application.runtime import RoomSessionBinding
from seokpan.room.domain import ActorType


def session(
    character: str,
    *,
    actor_type: SessionActorType = SessionActorType.GUEST,
    actor_id: str = "guest-1",
) -> SessionRecord:
    csrf = f"csrf-{character}"
    return SessionRecord(
        character * 64,
        actor_type,
        actor_id,
        digest_opaque_token(csrf),
        0,
        0,
        1_000,
        csrf_token=csrf,
    )


@pytest.mark.asyncio
async def test_participation_resolution_requires_matching_shared_session() -> None:
    current = session("a")
    binding = RoomSessionBinding(
        "room-1",
        "participant-1",
        current.session_digest,
        ActorType.GUEST,
        connection_generation=7,
        connected=True,
    )
    rooms = SimpleNamespace(
        find_by_session=AsyncMock(return_value=binding),
        find_by_participant=AsyncMock(return_value=binding),
    )
    sessions = SimpleNamespace(get=AsyncMock(return_value=current))
    resolver = RedisRoomParticipationResolver(rooms, sessions)

    resolved = await resolver.by_session(current.session_digest)
    assert resolved is not None
    assert (resolved.room_id, resolved.participant_id, resolved.actor_id) == (
        "room-1",
        "participant-1",
        "guest-1",
    )
    assert resolved.connection_generation == 7
    assert resolved.connected is True

    sessions.get.return_value = None
    assert await resolver.by_participant("participant-1") is None

    sessions.get.return_value = session("a", actor_type=SessionActorType.MEMBER, actor_id="1")
    assert await resolver.by_session(current.session_digest) is None

    replacement = CreateSession(
        "b" * 64,
        SessionActorType.MEMBER,
        "1",
        digest_opaque_token("replacement-csrf"),
        csrf_token="replacement-csrf",
    )
    rooms.find_by_participant.return_value = RoomSessionBinding(
        "room-1", "participant-1", replacement.session_digest, ActorType.MEMBER
    )
    assert await resolver.identity_transition_applied(current, replacement, "participant-1") is True


@pytest.mark.asyncio
async def test_session_workflow_restores_previous_session_when_room_rotation_fails() -> None:
    previous = session("a")
    replacement = CreateSession(
        "b" * 64,
        SessionActorType.MEMBER,
        "1",
        digest_opaque_token("replacement-csrf"),
        csrf_token="replacement-csrf",
    )
    rotated = session("b", actor_type=SessionActorType.MEMBER, actor_id="1")
    sessions = SimpleNamespace(
        rotate=AsyncMock(return_value=rotated),
        restore_after_failed_rotation=AsyncMock(return_value=previous),
    )
    participants = SimpleNamespace(
        change_identity=AsyncMock(side_effect=RuntimeError("room unavailable"))
    )
    workflow = RedisSessionWorkflow(sessions, participants)

    with pytest.raises(RuntimeError, match="room unavailable"):
        await workflow.rotate_identity(previous=previous, replacement=replacement)

    sessions.restore_after_failed_rotation.assert_awaited_once_with(
        failed_replacement_digest=replacement.session_digest,
        previous=previous,
    )


@pytest.mark.asyncio
async def test_session_workflow_fails_closed_when_rotation_rollback_is_uncertain() -> None:
    previous = session("a")
    replacement = CreateSession(
        "b" * 64,
        SessionActorType.MEMBER,
        "1",
        digest_opaque_token("replacement-csrf"),
        csrf_token="replacement-csrf",
    )
    sessions = SimpleNamespace(
        rotate=AsyncMock(
            return_value=session("b", actor_type=SessionActorType.MEMBER, actor_id="1")
        ),
        restore_after_failed_rotation=AsyncMock(side_effect=OSError("redis unavailable")),
    )
    participants = SimpleNamespace(change_identity=AsyncMock(side_effect=RuntimeError("room")))

    with pytest.raises(SessionTransitionUnavailable):
        await RedisSessionWorkflow(sessions, participants).rotate_identity(
            previous=previous,
            replacement=replacement,
        )


@pytest.mark.asyncio
async def test_session_workflow_does_not_rollback_an_unconfirmed_room_write() -> None:
    previous = session("a")
    replacement = CreateSession(
        "b" * 64,
        SessionActorType.MEMBER,
        "1",
        digest_opaque_token("replacement-csrf"),
        csrf_token="replacement-csrf",
    )
    sessions = SimpleNamespace(
        rotate=AsyncMock(
            return_value=session("b", actor_type=SessionActorType.MEMBER, actor_id="1")
        ),
        restore_after_failed_rotation=AsyncMock(),
    )
    participants = SimpleNamespace(
        change_identity=AsyncMock(side_effect=SessionTransitionUnavailable())
    )

    with pytest.raises(SessionTransitionUnavailable):
        await RedisSessionWorkflow(sessions, participants).rotate_identity(
            previous=previous,
            replacement=replacement,
        )

    sessions.restore_after_failed_rotation.assert_not_awaited()


@pytest.mark.asyncio
async def test_logout_holds_session_admission_gate_until_revocation() -> None:
    current = session("a")
    order: list[str] = []

    async def acquire(_digest: str) -> str:
        order.append("acquire")
        return "lease-token"

    async def leave(_current: SessionRecord) -> None:
        order.append("leave")

    async def revoke(_digest: str) -> bool:
        order.append("revoke")
        return True

    async def release(_digest: str, _token: str) -> None:
        order.append("release")

    admissions = SimpleNamespace(
        acquire_session_admission=AsyncMock(side_effect=acquire),
        release_session_admission=AsyncMock(side_effect=release),
    )
    participants = SimpleNamespace(leave=AsyncMock(side_effect=leave))
    sessions = SimpleNamespace(revoke=AsyncMock(side_effect=revoke))

    assert await RedisSessionWorkflow(sessions, participants, admissions).logout(current) is True
    assert order == ["acquire", "leave", "revoke", "release"]


@pytest.mark.asyncio
async def test_logout_busy_admission_does_not_leave_or_revoke() -> None:
    current = session("a")
    admissions = SimpleNamespace(
        acquire_session_admission=AsyncMock(
            side_effect=SessionRuleViolation("ROOM_ADMISSION_BUSY")
        ),
        release_session_admission=AsyncMock(),
    )
    participants = SimpleNamespace(leave=AsyncMock())
    sessions = SimpleNamespace(revoke=AsyncMock())

    with pytest.raises(SessionRuleViolation, match="ROOM_ADMISSION_BUSY"):
        await RedisSessionWorkflow(sessions, participants, admissions).logout(current)

    participants.leave.assert_not_awaited()
    sessions.revoke.assert_not_awaited()
    admissions.release_session_admission.assert_not_awaited()


class TieRedisClient:
    def __init__(self) -> None:
        self.calls: list[tuple[int, tuple[object, ...]]] = []
        self.selected: str | None = None

    async def evalsha(self, sha: str, numkeys: int, *values: object) -> bytes:
        del sha
        self.calls.append((numkeys, values))
        candidates = ("A1", "B1")
        selected = str(values[numkeys + 1])
        assert selected in candidates
        if self.selected is None:
            self.selected = selected
        return VersionedJsonCodec.encode({"ok": True, "selected": self.selected}).encode()

    async def script_load(self, script: str) -> str:
        raise AssertionError(f"unexpected script load: {script}")


@pytest.mark.asyncio
async def test_turn_coordinator_uses_durable_tie_key_and_checks_official_move() -> None:
    client = TieRedisClient()
    games = SimpleNamespace(get_move=AsyncMock(return_value=None))
    coordinator = RedisTurnCoordinator(
        client,
        SimpleNamespace(list_rooms=AsyncMock(return_value=())),
        SimpleNamespace(get=AsyncMock(return_value=None)),
        games,
    )

    selected = await coordinator.select(game_id="game-1", turn_no=2, candidates=("A1", "B1"))
    numkeys, values = client.calls[0]
    assert numkeys == 1
    assert values[0] == RedisKeyspace.tie_selection("game-1", 2)
    assert selected in {"A1", "B1"}

    due = DueTurn("room-1", "game-1", 2)
    snapshot = SimpleNamespace(candidates=(SimpleNamespace(canonical="A1"),))
    assert (
        await coordinator.assess(due_turn=due, snapshot=snapshot)
        is TurnFinalizationApproval.ALLOWED
    )
    games.get_move.return_value = SimpleNamespace(coordinate=SimpleNamespace(canonical="B1"))
    assert (
        await coordinator.assess(due_turn=due, snapshot=snapshot)
        is TurnFinalizationApproval.RECOVERY_REQUIRED
    )

    await coordinator.record(TieSelectionRecord("game-1", 2, ("A1", "B1"), selected))
