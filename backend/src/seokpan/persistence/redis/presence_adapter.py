"""Redis-backed, process-independent user presence leases."""

from __future__ import annotations

import math
import re
from typing import cast
from uuid import uuid4

from seokpan.identity.application import SessionActorType
from seokpan.persistence.redis.common import (
    LuaScriptRunner,
    RedisClient,
    RedisKeyspace,
    RedisProviderError,
    VersionedJsonCodec,
    VersionedLuaScript,
)
from seokpan.persistence.redis.presence_scripts import (
    PRESENCE_CLOSE,
    PRESENCE_INVALIDATE_SESSION,
    PRESENCE_OPEN,
    PRESENCE_READ,
    PRESENCE_RENEW,
)
from seokpan.presence import (
    PresenceConnection,
    PresenceIdentity,
    PresenceLease,
    PresenceRuleViolation,
    PresenceSnapshot,
    PresenceUnavailable,
)


class RedisPresenceAdapter:
    """Use Redis server time and atomic scripts across Backend replicas."""

    def __init__(
        self,
        client: RedisClient,
        *,
        lease_seconds: float,
        max_connections: int,
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
        lease_ms = math.ceil(lease_seconds * 1000)
        if lease_ms > 2**53 - 1:
            raise PresenceRuleViolation("INVALID_PRESENCE_CONFIGURATION")
        self._scripts = LuaScriptRunner(client)
        self._lease_ms = lease_ms
        self._limit = max_connections

    async def open(self, identity: PresenceIdentity, session_digest: str) -> PresenceLease:
        if not isinstance(identity, PresenceIdentity):
            raise PresenceRuleViolation("INVALID_PRESENCE_IDENTITY")
        _validate_session(session_digest)
        lease = PresenceLease(str(uuid4()))
        result = await self._execute(
            PRESENCE_OPEN,
            args=(
                lease.lease_id,
                identity.actor_type.value,
                identity.actor_id,
                session_digest,
                self._lease_ms,
                self._limit,
            ),
        )
        decoded = self._result(result)
        self._raise_rejection(decoded)
        binding = self._binding(decoded.get("value"))
        if binding.lease != lease or binding.identity != identity:
            raise PresenceUnavailable("PRESENCE_RESPONSE_INVALID")
        if binding.session_digest != session_digest:
            raise PresenceUnavailable("PRESENCE_RESPONSE_INVALID")
        return lease

    async def renew(self, lease: PresenceLease) -> None:
        _validate_lease(lease)
        decoded = self._result(
            await self._execute(PRESENCE_RENEW, args=(lease.lease_id, self._lease_ms))
        )
        self._raise_rejection(decoded)
        binding = self._binding(decoded.get("value"))
        if binding.lease != lease:
            raise PresenceUnavailable("PRESENCE_RESPONSE_INVALID")

    async def close(self, lease: PresenceLease) -> None:
        _validate_lease(lease)
        decoded = self._result(await self._execute(PRESENCE_CLOSE, args=(lease.lease_id,)))
        self._raise_rejection(decoded)
        if decoded.get("value") is not True:
            raise PresenceUnavailable("PRESENCE_RESPONSE_INVALID")

    async def invalidate_session(self, session_digest: str) -> None:
        _validate_session(session_digest)
        decoded = self._result(
            await self._execute(PRESENCE_INVALIDATE_SESSION, args=(session_digest,))
        )
        self._raise_rejection(decoded)
        removed = decoded.get("value")
        if type(removed) is not int or removed < 0:
            raise PresenceUnavailable("PRESENCE_RESPONSE_INVALID")

    async def snapshot(self) -> PresenceSnapshot:
        value = self._read_value(await self._execute(PRESENCE_READ, args=()))
        online_users = value.get("online_users")
        if type(online_users) is not int or online_users < 0:
            raise PresenceUnavailable("PRESENCE_RESPONSE_INVALID")
        return PresenceSnapshot(online_users)

    async def connections(self) -> tuple[PresenceConnection, ...]:
        value = self._read_value(await self._execute(PRESENCE_READ, args=()))
        raw_connections = value.get("connections")
        if raw_connections == {}:  # Redis cjson encodes an empty Lua table as an object.
            raw_connections = []
        if not isinstance(raw_connections, list):
            raise PresenceUnavailable("PRESENCE_RESPONSE_INVALID")
        return tuple(self._binding(item) for item in raw_connections)

    async def _execute(self, script: VersionedLuaScript, *, args: tuple[object, ...]) -> object:
        try:
            return await self._scripts.execute(
                script,
                keys=(RedisKeyspace.presence_bindings(), RedisKeyspace.presence_expiries()),
                args=args,
            )
        except RedisProviderError as error:
            raise PresenceUnavailable(error.code) from None

    @classmethod
    def _read_value(cls, result: object) -> dict[str, object]:
        decoded = cls._result(result)
        cls._raise_rejection(decoded)
        value = decoded.get("value")
        if not isinstance(value, dict):
            raise PresenceUnavailable("PRESENCE_RESPONSE_INVALID")
        return cast(dict[str, object], value)

    @staticmethod
    def _result(result: object) -> dict[str, object]:
        if not isinstance(result, (bytes, str)):
            raise PresenceUnavailable("PRESENCE_RESPONSE_INVALID")
        try:
            decoded = VersionedJsonCodec.decode(result)
        except RedisProviderError:
            raise PresenceUnavailable("PRESENCE_RESPONSE_INVALID") from None
        if not isinstance(decoded.get("ok"), bool):
            raise PresenceUnavailable("PRESENCE_RESPONSE_INVALID")
        return decoded

    @staticmethod
    def _raise_rejection(result: dict[str, object]) -> None:
        if result["ok"] is True:
            return
        error = result.get("error")
        if not isinstance(error, str):
            raise PresenceUnavailable("PRESENCE_RESPONSE_INVALID")
        if error in {"PRESENCE_SESSION_IDENTITY_MISMATCH", "PRESENCE_LEASE_ENDED"}:
            raise PresenceRuleViolation(error)
        if error in {"PRESENCE_CAPACITY_REACHED", "PRESENCE_LEASE_COLLISION"}:
            raise PresenceUnavailable(error)
        raise PresenceUnavailable("PRESENCE_RESPONSE_INVALID")

    @staticmethod
    def _binding(value: object) -> PresenceConnection:
        if not isinstance(value, dict):
            raise PresenceUnavailable("PRESENCE_RESPONSE_INVALID")
        try:
            actor_type = value["actor_type"]
            actor_id = value["actor_id"]
            lease_id = value["lease_id"]
            session_digest = value["session_digest"]
            expires_at_ms = value["expires_at_ms"]
            string_values = (actor_type, actor_id, lease_id, session_digest)
            if not all(isinstance(item, str) for item in string_values):
                raise TypeError
            if type(expires_at_ms) is not int or expires_at_ms < 0:
                raise TypeError
            return PresenceConnection(
                PresenceLease(cast(str, lease_id)),
                PresenceIdentity(SessionActorType(cast(str, actor_type)), cast(str, actor_id)),
                cast(str, session_digest),
            )
        except (KeyError, TypeError, ValueError, PresenceRuleViolation):
            raise PresenceUnavailable("PRESENCE_RESPONSE_INVALID") from None


def _validate_session(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise PresenceRuleViolation("INVALID_PRESENCE_SESSION")


def _validate_lease(value: PresenceLease) -> None:
    if not isinstance(value, PresenceLease):
        raise PresenceRuleViolation("INVALID_PRESENCE_LEASE")
