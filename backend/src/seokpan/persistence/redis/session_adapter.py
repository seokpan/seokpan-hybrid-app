"""redis.asyncio-backed server-side Session adapter."""

from __future__ import annotations

from typing import cast

from redis.exceptions import RedisError

from seokpan.identity.application.session import (
    SESSION_ABSOLUTE_TTL_MS,
    SESSION_IDLE_TTL_MS,
    SESSION_SCHEMA_VERSION,
    CreateSession,
    SessionActorType,
    SessionRecord,
    SessionRuleViolation,
    validate_digest,
)
from seokpan.persistence.redis.common import (
    LuaScriptRunner,
    RedisClient,
    RedisKeyspace,
    RedisProviderError,
    VersionedJsonCodec,
    VersionedLuaScript,
)
from seokpan.persistence.redis.session_scripts import (
    CREATE_SESSION,
    RESTORE_SESSION,
    REVOKE_SESSION,
    ROTATE_SESSION,
    TOUCH_SESSION,
)


class RedisSessionAdapter:
    def __init__(self, client: RedisClient) -> None:
        self._client = client
        self._scripts = LuaScriptRunner(client)

    async def create(self, command: CreateSession) -> SessionRecord:
        result = await self._scripts.execute(
            CREATE_SESSION,
            keys=(
                RedisKeyspace.session(command.session_digest),
                RedisKeyspace.session_index(command.actor_type, command.actor_id),
            ),
            args=(
                command.session_digest,
                command.actor_type.value,
                command.actor_id,
                command.csrf_digest,
                SESSION_SCHEMA_VERSION,
                SESSION_IDLE_TTL_MS,
                SESSION_ABSOLUTE_TTL_MS,
                command.csrf_token,
            ),
        )
        return self._session_result(result, command.session_digest)

    async def get(self, session_digest: str) -> SessionRecord | None:
        validate_digest(session_digest)
        try:
            raw = await self._client.get(RedisKeyspace.session(session_digest))
        except RedisError as error:
            raise RedisProviderError() from error
        if raw is None:
            return None
        return self._record(VersionedJsonCodec.decode(raw), session_digest)

    async def touch(self, session_digest: str) -> SessionRecord | None:
        validate_digest(session_digest)
        result = await self._execute_verified(
            TOUCH_SESSION,
            session_digest=session_digest,
            keys=(RedisKeyspace.session(session_digest),),
            args=(session_digest, SESSION_IDLE_TTL_MS),
        )
        if result is None:
            return None
        decoded = self._result(result)
        self._raise_rejection(decoded)
        session = decoded.get("session")
        if session is None:
            return None
        return self._record_mapping(session, session_digest)

    async def rotate(
        self,
        *,
        previous_session_digest: str,
        replacement: CreateSession,
    ) -> SessionRecord:
        validate_digest(previous_session_digest)
        result = await self._execute_verified(
            ROTATE_SESSION,
            session_digest=previous_session_digest,
            keys=(
                RedisKeyspace.session(previous_session_digest),
                RedisKeyspace.session(replacement.session_digest),
                RedisKeyspace.session_index(replacement.actor_type, replacement.actor_id),
            ),
            args=(
                previous_session_digest,
                replacement.session_digest,
                replacement.actor_type.value,
                replacement.actor_id,
                replacement.csrf_digest,
                SESSION_SCHEMA_VERSION,
                SESSION_IDLE_TTL_MS,
                SESSION_ABSOLUTE_TTL_MS,
                replacement.csrf_token,
            ),
        )
        if result is None:
            raise SessionRuleViolation("SESSION_NOT_FOUND")
        return self._session_result(result, replacement.session_digest)

    async def revoke(self, session_digest: str) -> bool:
        validate_digest(session_digest)
        result = await self._execute_verified(
            REVOKE_SESSION,
            session_digest=session_digest,
            keys=(RedisKeyspace.session(session_digest),),
            args=(session_digest,),
        )
        if result is None:
            return False
        decoded = self._result(result)
        self._raise_rejection(decoded)
        revoked = decoded.get("revoked")
        if not isinstance(revoked, bool):
            raise RedisProviderError("REDIS_RESPONSE_INVALID")
        return revoked

    async def restore_after_failed_rotation(
        self,
        *,
        failed_replacement_digest: str,
        previous: SessionRecord,
    ) -> SessionRecord:
        validate_digest(failed_replacement_digest)
        result = await self._execute_verified(
            RESTORE_SESSION,
            session_digest=failed_replacement_digest,
            keys=(
                RedisKeyspace.session(failed_replacement_digest),
                RedisKeyspace.session(previous.session_digest),
                RedisKeyspace.session_index(previous.actor_type, previous.actor_id),
            ),
            args=(
                failed_replacement_digest,
                previous.session_digest,
                previous.actor_type.value,
                previous.actor_id,
                previous.csrf_digest,
                previous.schema_version,
                previous.created_at_ms,
                previous.last_activity_at_ms,
                previous.absolute_expires_at_ms,
                SESSION_IDLE_TTL_MS,
                previous.csrf_token,
            ),
        )
        if result is None:
            raise SessionRuleViolation("SESSION_NOT_FOUND")
        return self._session_result(result, previous.session_digest)

    async def _execute_verified(
        self,
        script: VersionedLuaScript,
        *,
        session_digest: str,
        keys: tuple[str, ...],
        args: tuple[object, ...],
    ) -> object | None:
        """Validate bytes, then require the same bytes before any Lua write.

        Only a confirmed no-write conflict may be retried. Transport failures
        are never replayed here because the previous write outcome is unknown.
        """
        for _ in range(3):
            try:
                raw = await self._client.get(RedisKeyspace.session(session_digest))
            except RedisError as error:
                raise RedisProviderError() from error
            if raw is None:
                return None
            self._record(VersionedJsonCodec.decode(raw), session_digest)
            result = await self._scripts.execute(script, keys=keys, args=(*args, raw))
            decoded = self._result(result)
            if decoded["ok"] is False and decoded.get("error") == "SESSION_STATE_CHANGED":
                continue
            self._raise_rejection(decoded)
            return result
        raise RedisProviderError("SESSION_STATE_CHANGED")

    @classmethod
    def _session_result(cls, result: object, session_digest: str) -> SessionRecord:
        decoded = cls._result(result)
        cls._raise_rejection(decoded)
        return cls._record_mapping(decoded.get("session"), session_digest)

    @staticmethod
    def _result(result: object) -> dict[str, object]:
        if not isinstance(result, (bytes, str)):
            raise RedisProviderError("REDIS_RESPONSE_INVALID")
        decoded = VersionedJsonCodec.decode(result)
        if not isinstance(decoded.get("ok"), bool):
            raise RedisProviderError("REDIS_RESPONSE_INVALID")
        return decoded

    @staticmethod
    def _raise_rejection(result: dict[str, object]) -> None:
        if result["ok"] is True:
            return
        error = result.get("error")
        if not isinstance(error, str):
            raise RedisProviderError("REDIS_RESPONSE_INVALID")
        raise SessionRuleViolation(error)

    @classmethod
    def _record_mapping(cls, value: object, session_digest: str) -> SessionRecord:
        if not isinstance(value, dict):
            raise RedisProviderError("REDIS_RESPONSE_INVALID")
        return cls._record(cast(dict[str, object], value), session_digest)

    @staticmethod
    def _record(value: dict[str, object], session_digest: str) -> SessionRecord:
        try:
            return SessionRecord(
                session_digest=session_digest,
                actor_type=SessionActorType(_required_string(value, "actor_type")),
                actor_id=_required_string(value, "actor_id"),
                csrf_digest=_required_string(value, "csrf_digest"),
                csrf_token=_required_string(value, "csrf_token"),
                created_at_ms=_required_integer(value, "created_at_ms"),
                last_activity_at_ms=_required_integer(value, "last_activity_at_ms"),
                absolute_expires_at_ms=_required_integer(value, "absolute_expires_at_ms"),
                schema_version=_required_integer(value, "schema_version"),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RedisProviderError("REDIS_RESPONSE_INVALID") from error


def _required_string(value: dict[str, object], key: str) -> str:
    item = value[key]
    if not isinstance(item, str):
        raise TypeError(key)
    return item


def _required_integer(value: dict[str, object], key: str) -> int:
    item = value[key]
    if type(item) is not int:
        raise TypeError(key)
    return item
