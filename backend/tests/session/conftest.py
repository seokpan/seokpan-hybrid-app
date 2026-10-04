from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import pytest
from redis.exceptions import NoScriptError

from seokpan.identity.application.session import (
    CreateSession,
    SessionActorType,
    SessionPort,
    SessionRecord,
    SessionRuleViolation,
    digest_opaque_token,
)
from seokpan.persistence.memory import InMemorySessionAdapter, ManualClock
from seokpan.persistence.redis.common import RedisKeyspace, VersionedJsonCodec
from seokpan.persistence.redis.session_adapter import RedisSessionAdapter
from seokpan.persistence.redis.session_scripts import (
    CREATE_SESSION,
    RESTORE_SESSION,
    REVOKE_SESSION,
    ROTATE_SESSION,
    TOUCH_SESSION,
)


class ObservableSessionPort(SessionPort, Protocol):
    def member_session_digests(self, member_id: str) -> tuple[str, ...]: ...


@dataclass(slots=True)
class SessionHarness:
    adapter: SessionPort
    clock: ManualClock
    member_sessions: Callable[[str], tuple[str, ...]]


class EmulatedRedisClient:
    """Redis command boundary emulator; it is not actual Provider evidence."""

    def __init__(self, clock: ManualClock, *, scripts_loaded: bool = True) -> None:
        self.clock = clock
        self.store = InMemorySessionAdapter(clock)
        self.loaded = (
            {
                script.sha
                for script in (
                    CREATE_SESSION,
                    TOUCH_SESSION,
                    ROTATE_SESSION,
                    RESTORE_SESSION,
                    REVOKE_SESSION,
                )
            }
            if scripts_loaded
            else set()
        )
        self.evalsha_calls: list[tuple[str, int, tuple[object, ...]]] = []
        self.script_load_calls: list[str] = []
        self.get_keys: list[str] = []

    async def get(self, key: str) -> bytes | None:
        self.get_keys.append(key)
        digest = key.rsplit(":", maxsplit=1)[-1]
        record = await self.store.get(digest)
        if record is None:
            return None
        return VersionedJsonCodec.encode(self._payload(record)).encode()

    async def evalsha(
        self,
        sha: str,
        numkeys: int,
        *keys_and_args: object,
    ) -> bytes:
        self.evalsha_calls.append((sha, numkeys, keys_and_args))
        if sha not in self.loaded:
            raise NoScriptError("script cache miss")
        args = keys_and_args[numkeys:]
        if sha != CREATE_SESSION.sha:
            record = await self.store.get(str(args[0]))
            if record is not None:
                raw = VersionedJsonCodec.encode(self._payload(record)).encode()
                expected = args[-1]
                if isinstance(expected, str):
                    expected = expected.encode()
                if raw != expected:
                    return VersionedJsonCodec.encode(
                        {"ok": False, "session": None, "error": "SESSION_STATE_CHANGED"}
                    ).encode()
            args = args[:-1]
        try:
            if sha == CREATE_SESSION.sha:
                command = self._command(args, digest_index=0, actor_index=1)
                record = await self.store.create(command)
                return self._session_response(record)
            if sha == TOUCH_SESSION.sha:
                record = await self.store.touch(str(args[0]))
                return self._session_response(record)
            if sha == ROTATE_SESSION.sha:
                command = self._command(args, digest_index=1, actor_index=2)
                record = await self.store.rotate(
                    previous_session_digest=str(args[0]),
                    replacement=command,
                )
                return self._session_response(record)
            if sha == RESTORE_SESSION.sha:
                previous = SessionRecord(
                    session_digest=str(args[1]),
                    actor_type=SessionActorType(str(args[2])),
                    actor_id=str(args[3]),
                    csrf_digest=str(args[4]),
                    csrf_token=str(args[10]),
                    schema_version=int(str(args[5])),
                    created_at_ms=int(str(args[6])),
                    last_activity_at_ms=int(str(args[7])),
                    absolute_expires_at_ms=int(str(args[8])),
                )
                record = await self.store.restore_after_failed_rotation(
                    failed_replacement_digest=str(args[0]),
                    previous=previous,
                )
                return self._session_response(record)
            if sha == REVOKE_SESSION.sha:
                revoked = await self.store.revoke(str(args[0]))
                return VersionedJsonCodec.encode(
                    {"ok": True, "revoked": revoked, "error": None}
                ).encode()
        except SessionRuleViolation as error:
            return VersionedJsonCodec.encode(
                {"ok": False, "session": None, "error": error.code}
            ).encode()
        raise AssertionError("unknown script")

    async def script_load(self, script: str) -> str:
        self.script_load_calls.append(script)
        for candidate in (
            CREATE_SESSION,
            TOUCH_SESSION,
            ROTATE_SESSION,
            RESTORE_SESSION,
            REVOKE_SESSION,
        ):
            if candidate.source == script:
                self.loaded.add(candidate.sha)
                return candidate.sha
        raise AssertionError("unknown script source")

    def member_session_digests(self, member_id: str) -> tuple[str, ...]:
        return self.store.member_session_digests(member_id)

    @staticmethod
    def _command(
        args: tuple[object, ...],
        *,
        digest_index: int,
        actor_index: int,
    ) -> CreateSession:
        return CreateSession(
            session_digest=str(args[digest_index]),
            actor_type=SessionActorType(str(args[actor_index])),
            actor_id=str(args[actor_index + 1]),
            csrf_digest=str(args[actor_index + 2]),
            csrf_token=str(args[-1]),
        )

    @classmethod
    def _session_response(cls, record: SessionRecord | None) -> bytes:
        return VersionedJsonCodec.encode(
            {
                "ok": True,
                "session": None if record is None else cls._payload(record),
                "error": None,
            }
        ).encode()

    @staticmethod
    def _payload(record: SessionRecord) -> dict[str, object]:
        return {
            "schema_version": record.schema_version,
            "actor_type": record.actor_type.value,
            "actor_id": record.actor_id,
            "csrf_digest": record.csrf_digest,
            "csrf_token": record.csrf_token,
            "created_at_ms": record.created_at_ms,
            "last_activity_at_ms": record.last_activity_at_ms,
            "absolute_expires_at_ms": record.absolute_expires_at_ms,
        }


@pytest.fixture(params=("memory", "redis-boundary"))
def session_harness(request: pytest.FixtureRequest) -> SessionHarness:
    clock = ManualClock(now_ms=1_000)
    if request.param == "memory":
        adapter = InMemorySessionAdapter(clock)
        return SessionHarness(adapter, clock, adapter.member_session_digests)

    client = EmulatedRedisClient(clock)
    return SessionHarness(
        RedisSessionAdapter(client),
        clock,
        client.member_session_digests,
    )


@pytest.fixture
def emulated_redis_client() -> EmulatedRedisClient:
    return EmulatedRedisClient(ManualClock(now_ms=1_000))


def digest(character: str) -> str:
    return character * 64


def guest_command(character: str = "a") -> CreateSession:
    return CreateSession(
        session_digest=digest(character),
        actor_type=SessionActorType.GUEST,
        actor_id="guest-1",
        csrf_digest=digest_opaque_token("f" * 64),
        csrf_token="f" * 64,
    )


def member_command(character: str = "b", member_id: str = "42") -> CreateSession:
    return CreateSession(
        session_digest=digest(character),
        actor_type=SessionActorType.MEMBER,
        actor_id=member_id,
        csrf_digest=digest_opaque_token("e" * 64),
        csrf_token="e" * 64,
    )


def expected_session_key(character: str) -> str:
    return RedisKeyspace.session(digest(character))
