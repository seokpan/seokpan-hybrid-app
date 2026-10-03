"""Bounded, single-event-loop presence Fake. No Redis or Session writes."""

from __future__ import annotations

import asyncio
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import uuid4

from seokpan.presence import (
    PresenceConnection,
    PresenceIdentity,
    PresenceLease,
    PresenceRuleViolation,
    PresenceSnapshot,
    PresenceUnavailable,
)


@dataclass(frozen=True, slots=True)
class _Binding:
    identity: PresenceIdentity
    session_digest: str = field(repr=False)
    expires_at: float


class InMemoryPresenceAdapter:
    """The caller explicitly selects a test lease duration and capacity.

    These are not production timing or a product user limit. All modifications
    are without awaits in one event loop. Real shared Providers must separately
    prove duplicate/expiry/revocation behavior across processes and replicas.
    """

    def __init__(
        self,
        *,
        lease_seconds: float,
        max_connections: int,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, (int, float))
            or not math.isfinite(lease_seconds)
            or lease_seconds <= 0
            or type(max_connections) is not int
            or max_connections < 1
        ):
            raise PresenceRuleViolation("INVALID_PRESENCE_CONFIGURATION")
        self._lease_seconds = lease_seconds
        self._limit = max_connections
        self._monotonic = monotonic
        self._last_time: float | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._bindings: dict[PresenceLease, _Binding] = {}

    def _current(self) -> float:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise PresenceUnavailable("PRESENCE_LOOP_MISMATCH")
        try:
            value = self._monotonic()
        except Exception:
            raise PresenceUnavailable("PRESENCE_CLOCK_UNAVAILABLE") from None
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not math.isfinite(value + self._lease_seconds)
            or value < 0
            or (self._last_time is not None and value < self._last_time)
        ):
            raise PresenceUnavailable("PRESENCE_CLOCK_UNAVAILABLE")
        self._last_time = value
        for lease, binding in tuple(self._bindings.items()):
            if binding.expires_at <= value:
                del self._bindings[lease]
        return value

    async def open(self, identity: PresenceIdentity, session_digest: str) -> PresenceLease:
        if not isinstance(identity, PresenceIdentity):
            raise PresenceRuleViolation("INVALID_PRESENCE_IDENTITY")
        _validate_session(session_digest)
        now = self._current()
        if any(
            binding.session_digest == session_digest and binding.identity != identity
            for binding in self._bindings.values()
        ):
            raise PresenceRuleViolation("PRESENCE_SESSION_IDENTITY_MISMATCH")
        if len(self._bindings) >= self._limit:
            # Never remove a live user's lease to make room for another.
            raise PresenceUnavailable("PRESENCE_CAPACITY_REACHED")
        lease = PresenceLease(str(uuid4()))
        if lease in self._bindings:
            raise PresenceUnavailable("PRESENCE_LEASE_COLLISION")
        self._bindings[lease] = _Binding(identity, session_digest, now + self._lease_seconds)
        return lease

    async def renew(self, lease: PresenceLease) -> None:
        _validate_lease(lease)
        now = self._current()
        previous = self._bindings.get(lease)
        if previous is None:
            raise PresenceRuleViolation("PRESENCE_LEASE_ENDED")
        self._bindings[lease] = _Binding(
            previous.identity, previous.session_digest, now + self._lease_seconds
        )

    async def close(self, lease: PresenceLease) -> None:
        _validate_lease(lease)
        self._current()
        self._bindings.pop(lease, None)

    async def invalidate_session(self, session_digest: str) -> None:
        _validate_session(session_digest)
        self._current()
        for lease, binding in tuple(self._bindings.items()):
            if binding.session_digest == session_digest:
                del self._bindings[lease]

    async def snapshot(self) -> PresenceSnapshot:
        self._current()
        return PresenceSnapshot(len({binding.identity for binding in self._bindings.values()}))

    async def connections(self) -> tuple[PresenceConnection, ...]:
        self._current()
        return tuple(
            PresenceConnection(lease, binding.identity, binding.session_digest)
            for lease, binding in self._bindings.items()
        )


def _validate_session(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise PresenceRuleViolation("INVALID_PRESENCE_SESSION")


def _validate_lease(value: PresenceLease) -> None:
    if not isinstance(value, PresenceLease):
        raise PresenceRuleViolation("INVALID_PRESENCE_LEASE")
