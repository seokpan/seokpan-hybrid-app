from __future__ import annotations

import asyncio
import math
from dataclasses import asdict
from typing import cast
from uuid import UUID, uuid4

import pytest

from seokpan.identity.application import SessionActorType
from seokpan.persistence.memory.presence_adapter import InMemoryPresenceAdapter
from seokpan.presence import (
    PresenceIdentity,
    PresenceLease,
    PresencePort,
    PresenceRuleViolation,
    PresenceUnavailable,
)

MEMBER = PresenceIdentity(SessionActorType.MEMBER, "123")
GUEST = PresenceIdentity(SessionActorType.GUEST, "guest-1")
FIRST = "a" * 64
SECOND = "b" * 64


class Clock:
    value = 0.0

    def __call__(self) -> float:
        return self.value


def adapter(clock: Clock | None = None, limit: int = 100) -> InMemoryPresenceAdapter:
    # Deliberately not the 30-second Room lease or authentication TTL.
    return InMemoryPresenceAdapter(
        lease_seconds=7.0, max_connections=limit, monotonic=clock or Clock()
    )


@pytest.mark.asyncio
async def test_member_tabs_and_devices_are_one_user_until_last_connection_closes() -> None:
    port: PresencePort = adapter()
    assert (await port.snapshot()).online_users == 0
    one = await port.open(MEMBER, FIRST)
    two = await port.open(MEMBER, FIRST)
    device = await port.open(MEMBER, SECOND)
    assert one != two != device
    assert (await port.snapshot()).online_users == 1
    await port.close(one)
    await port.close(two)
    assert (await port.snapshot()).online_users == 1
    await port.close(device)
    assert (await port.snapshot()).online_users == 0


@pytest.mark.asyncio
async def test_guest_is_deduplicated_by_temporary_identity_not_by_display_name() -> None:
    port = adapter()
    await port.open(GUEST, FIRST)
    await port.open(GUEST, SECOND)
    await port.open(PresenceIdentity(SessionActorType.GUEST, "guest-2"), "c" * 64)
    # The same string across actor types must not alias a Member and a Guest.
    await port.open(PresenceIdentity(SessionActorType.GUEST, "123"), "d" * 64)
    await port.open(MEMBER, "e" * 64)
    assert (await port.snapshot()).online_users == 4


@pytest.mark.asyncio
async def test_old_close_cannot_remove_a_reconnected_or_upgraded_identity() -> None:
    port = adapter()
    old = await port.open(GUEST, FIRST)
    replacement = await port.open(GUEST, FIRST)
    await port.close(old)
    await port.close(old)
    assert (await port.snapshot()).online_users == 1
    member = await port.open(MEMBER, SECOND)
    await port.invalidate_session(FIRST)
    await port.close(replacement)
    await port.close(old)
    await port.renew(member)
    assert (await port.snapshot()).online_users == 1
    for ended in [old, replacement]:
        with pytest.raises(PresenceRuleViolation, match="PRESENCE_LEASE_ENDED"):
            await port.renew(ended)


@pytest.mark.asyncio
async def test_revoking_one_session_keeps_other_device_but_removes_all_its_tabs() -> None:
    port = adapter()
    one = await port.open(MEMBER, FIRST)
    two = await port.open(MEMBER, FIRST)
    device = await port.open(MEMBER, SECOND)
    await port.invalidate_session(FIRST)
    await port.invalidate_session(FIRST)
    assert (await port.snapshot()).online_users == 1
    for lease in [one, two]:
        with pytest.raises(PresenceRuleViolation):
            await port.renew(lease)
    await port.close(device)
    assert (await port.snapshot()).online_users == 0


@pytest.mark.asyncio
async def test_expiry_boundary_and_late_renewal_do_not_resurrect_a_connection() -> None:
    clock = Clock()
    port = adapter(clock)
    old = await port.open(MEMBER, FIRST)
    clock.value = 6.0
    new = await port.open(MEMBER, FIRST)
    clock.value = 7.0
    assert (await port.snapshot()).online_users == 1
    with pytest.raises(PresenceRuleViolation, match="PRESENCE_LEASE_ENDED"):
        await port.renew(old)
    await port.close(old)
    clock.value = 12.0
    await port.renew(new)
    clock.value = 18.999
    assert (await port.snapshot()).online_users == 1
    clock.value = 19.0
    assert (await port.snapshot()).online_users == 0


@pytest.mark.asyncio
async def test_reading_the_count_never_renews_lease() -> None:
    clock = Clock()
    port = adapter(clock)
    await port.open(MEMBER, FIRST)
    for step in range(7):
        clock.value = float(step)
        assert (await port.snapshot()).online_users == 1
    clock.value = 7.0
    assert (await port.snapshot()).online_users == 0


@pytest.mark.asyncio
async def test_capacity_does_not_evict_live_connections_and_recovers_after_expiry() -> None:
    clock = Clock()
    port = adapter(clock, limit=1)
    await port.open(MEMBER, FIRST)
    with pytest.raises(PresenceUnavailable, match="PRESENCE_CAPACITY_REACHED"):
        await port.open(GUEST, SECOND)
    assert (await port.snapshot()).online_users == 1
    clock.value = 7.0
    guest = await port.open(GUEST, SECOND)
    await port.close(guest)
    assert (await port.snapshot()).online_users == 0


@pytest.mark.asyncio
async def test_session_identity_mismatch_is_rejected_without_reassigning_existing_user() -> None:
    port = adapter()
    member = await port.open(MEMBER, FIRST)
    with pytest.raises(PresenceRuleViolation, match="PRESENCE_SESSION_IDENTITY_MISMATCH"):
        await port.open(GUEST, FIRST)
    assert (await port.snapshot()).online_users == 1
    await port.renew(member)


@pytest.mark.asyncio
async def test_many_simultaneous_tabs_and_duplicate_closes_match_unique_users() -> None:
    port = adapter()
    leases = await asyncio.gather(*(port.open(MEMBER, FIRST) for _ in range(40)))
    guests = await asyncio.gather(
        *(
            port.open(PresenceIdentity(SessionActorType.GUEST, str(i)), f"{i:064x}")
            for i in range(20)
        )
    )
    assert (await port.snapshot()).online_users == 21
    await asyncio.gather(*(port.close(lease) for lease in leases[:-1] * 2))
    assert (await port.snapshot()).online_users == 21
    await asyncio.gather(*(port.close(lease) for lease in [leases[-1], *guests]))
    assert (await port.snapshot()).online_users == 0


@pytest.mark.asyncio
async def test_snapshot_does_not_expose_identity_session_or_connection_ids() -> None:
    port = adapter()
    lease = await port.open(MEMBER, FIRST)
    value = await port.snapshot()
    assert asdict(value) == {"online_users": 1}
    assert FIRST not in repr(port)
    assert MEMBER.actor_id not in repr(MEMBER)
    assert lease.lease_id not in repr(lease)


@pytest.mark.parametrize("value", [math.nan, math.inf, -1.0, True])
@pytest.mark.asyncio
async def test_bad_clock_fails_instead_of_claiming_zero_users(value: float) -> None:
    clock = Clock()
    port = adapter(clock)
    await port.open(MEMBER, FIRST)
    clock.value = value
    with pytest.raises(PresenceUnavailable, match="PRESENCE_CLOCK_UNAVAILABLE"):
        await port.snapshot()
    clock.value = 1.0
    assert (await port.snapshot()).online_users == 1


@pytest.mark.asyncio
async def test_clock_failure_and_backwards_time_are_not_exposed_as_success() -> None:
    clock = Clock()
    port = adapter(clock)
    await port.open(MEMBER, FIRST)
    clock.value = 2.0
    await port.snapshot()
    clock.value = 1.0
    with pytest.raises(PresenceUnavailable):
        await port.snapshot()

    def broken() -> float:
        raise RuntimeError("sensitive provider detail")

    unavailable = InMemoryPresenceAdapter(lease_seconds=1, max_connections=1, monotonic=broken)
    with pytest.raises(PresenceUnavailable, match="^PRESENCE_CLOCK_UNAVAILABLE$"):
        await unavailable.snapshot()


def test_cross_event_loop_use_is_rejected() -> None:
    port = adapter()
    asyncio.run(port.open(MEMBER, FIRST))
    with pytest.raises(PresenceUnavailable, match="PRESENCE_LOOP_MISMATCH"):
        asyncio.run(port.snapshot())


@pytest.mark.parametrize("duration", [0.0, -1.0, math.inf, math.nan, True])
def test_invalid_duration(duration: float) -> None:
    with pytest.raises(PresenceRuleViolation, match="INVALID_PRESENCE_CONFIGURATION"):
        InMemoryPresenceAdapter(lease_seconds=duration, max_connections=1)


@pytest.mark.parametrize("limit", [0, -1, True])
def test_invalid_capacity(limit: int) -> None:
    with pytest.raises(PresenceRuleViolation, match="INVALID_PRESENCE_CONFIGURATION"):
        adapter(limit=limit)


@pytest.mark.parametrize("actor_id", ["", "0", "01", "١", "-1", "1" * 129])
def test_invalid_member_identity(actor_id: str) -> None:
    with pytest.raises(PresenceRuleViolation, match="INVALID_PRESENCE_IDENTITY"):
        PresenceIdentity(SessionActorType.MEMBER, actor_id)


@pytest.mark.parametrize("lease_id", ["secret", str(UUID(int=0)), str(uuid4()).upper()])
def test_invalid_handle(lease_id: str) -> None:
    with pytest.raises(PresenceRuleViolation, match="INVALID_PRESENCE_LEASE"):
        PresenceLease(lease_id)


@pytest.mark.asyncio
async def test_invalid_runtime_inputs_leave_existing_entries_unchanged() -> None:
    port = adapter()
    await port.open(MEMBER, FIRST)
    with pytest.raises(PresenceRuleViolation):
        await port.open(cast(PresenceIdentity, "raw identity"), SECOND)
    for invalid in ["raw token", "", "A" * 64]:
        with pytest.raises(PresenceRuleViolation):
            await port.open(GUEST, invalid)
        with pytest.raises(PresenceRuleViolation):
            await port.invalidate_session(invalid)
    with pytest.raises(PresenceRuleViolation):
        await port.close(cast(PresenceLease, "raw lease"))
    with pytest.raises(PresenceRuleViolation):
        PresenceIdentity(cast(SessionActorType, "MEMBER"), "123")
    assert (await port.snapshot()).online_users == 1


@pytest.mark.asyncio
async def test_live_lease_collision_does_not_overwrite_other_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed = uuid4()
    monkeypatch.setattr("seokpan.persistence.memory.presence_adapter.uuid4", lambda: fixed)
    port = adapter()
    await port.open(MEMBER, FIRST)
    with pytest.raises(PresenceUnavailable, match="PRESENCE_LEASE_COLLISION"):
        await port.open(GUEST, SECOND)
    assert (await port.snapshot()).online_users == 1
