from __future__ import annotations

import pytest

from seokpan.identity.application import (
    CreateSession,
    SessionRecord,
    SessionTransitionUnavailable,
)
from seokpan.persistence.memory import (
    InMemorySessionAdapter,
    InMemorySessionWorkflow,
    ManualClock,
)

from .conftest import digest, guest_command, member_command


class FailingParticipantTransitions:
    def __init__(self) -> None:
        self.identity_attempts = 0
        self.leave_attempts = 0
        self.fail_identity_change = False

    async def change_identity(
        self,
        previous: SessionRecord,
        replacement: CreateSession,
    ) -> None:
        del previous, replacement
        self.identity_attempts += 1
        if self.fail_identity_change:
            raise SessionTransitionUnavailable

    async def leave(self, current: SessionRecord) -> None:
        del current
        self.leave_attempts += 1


class FailOnceRevokeSessionAdapter(InMemorySessionAdapter):
    def __init__(self, clock: ManualClock) -> None:
        super().__init__(clock)
        self.fail_next_revoke = True

    async def revoke(self, session_digest: str) -> bool:
        if self.fail_next_revoke:
            self.fail_next_revoke = False
            raise SessionTransitionUnavailable
        return await super().revoke(session_digest)


class FailOnceRotateSessionAdapter(InMemorySessionAdapter):
    def __init__(self, clock: ManualClock) -> None:
        super().__init__(clock)
        self.fail_next_rotate = True

    async def rotate(
        self,
        *,
        previous_session_digest: str,
        replacement: CreateSession,
    ) -> SessionRecord:
        if self.fail_next_rotate:
            self.fail_next_rotate = False
            raise SessionTransitionUnavailable
        return await super().rotate(
            previous_session_digest=previous_session_digest,
            replacement=replacement,
        )


@pytest.mark.asyncio
async def test_session_rotation_failure_does_not_change_room_identity() -> None:
    sessions = FailOnceRotateSessionAdapter(ManualClock(now_ms=1_000))
    participants = FailingParticipantTransitions()
    workflow = InMemorySessionWorkflow(sessions, participants)
    previous = await workflow.create(guest_command())

    with pytest.raises(SessionTransitionUnavailable):
        await workflow.rotate_identity(previous=previous, replacement=member_command())

    assert participants.identity_attempts == 0
    assert await sessions.get(digest("a")) == previous
    assert await sessions.get(digest("b")) is None


@pytest.mark.asyncio
async def test_room_identity_failure_restores_previous_session() -> None:
    sessions = InMemorySessionAdapter(ManualClock(now_ms=1_000))
    participants = FailingParticipantTransitions()
    workflow = InMemorySessionWorkflow(sessions, participants)
    previous = await workflow.create(guest_command())
    participants.fail_identity_change = True

    with pytest.raises(SessionTransitionUnavailable):
        await workflow.rotate_identity(previous=previous, replacement=member_command())

    assert participants.identity_attempts == 1
    assert await sessions.get(digest("a")) == previous
    assert await sessions.get(digest("b")) is None
    assert sessions.member_session_digests("42") == ()


@pytest.mark.asyncio
async def test_logout_can_retry_after_room_leave_succeeds_and_revoke_fails() -> None:
    sessions = FailOnceRevokeSessionAdapter(ManualClock(now_ms=1_000))
    participants = FailingParticipantTransitions()
    workflow = InMemorySessionWorkflow(sessions, participants)
    current = await workflow.create(guest_command())

    with pytest.raises(SessionTransitionUnavailable):
        await workflow.logout(current)

    assert participants.leave_attempts == 1
    assert await sessions.get(current.session_digest) == current

    assert await workflow.logout(current) is True
    assert participants.leave_attempts == 2
    assert await sessions.get(current.session_digest) is None
