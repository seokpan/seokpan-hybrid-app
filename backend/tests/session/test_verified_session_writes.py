"""Verified-read/CAS boundary tests, not execution of real Redis Lua."""

from collections.abc import Awaitable, Callable

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from seokpan.identity.application import SessionRecord, SessionRuleViolation
from seokpan.persistence.memory import ManualClock
from seokpan.persistence.redis.common import RedisProviderError, VersionedJsonCodec
from seokpan.persistence.redis.session_adapter import RedisSessionAdapter
from seokpan.persistence.redis.session_scripts import (
    CREATE_SESSION,
    RESTORE_SESSION,
    REVOKE_SESSION,
    ROTATE_SESSION,
    TOUCH_SESSION,
)

from .conftest import EmulatedRedisClient, guest_command, member_command


def operation(
    adapter: RedisSessionAdapter, name: str, previous: SessionRecord
) -> Callable[[], Awaitable[object]]:
    if name == "touch":
        return lambda: adapter.touch(previous.session_digest)
    if name == "rotate":
        return lambda: adapter.rotate(
            previous_session_digest=previous.session_digest, replacement=member_command()
        )
    if name == "revoke":
        return lambda: adapter.revoke(previous.session_digest)
    return lambda: adapter.restore_after_failed_rotation(
        failed_replacement_digest=member_command().session_digest, previous=previous
    )


class CorruptReadClient(EmulatedRedisClient):
    broken = False

    async def get(self, key: str) -> bytes | None:
        raw = await super().get(key)
        if raw is None or not self.broken:
            return raw
        data = VersionedJsonCodec.decode(raw)
        data["csrf_token"] = "mismatched-test-value"
        return VersionedJsonCodec.encode(data).encode()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["touch", "rotate", "restore", "revoke"])
async def test_corrupt_session_is_rejected_before_any_lua_call(name: str) -> None:
    client = CorruptReadClient(ManualClock())
    adapter = RedisSessionAdapter(client)
    previous = await adapter.create(guest_command())
    if name == "restore":
        await adapter.rotate(
            previous_session_digest=previous.session_digest, replacement=member_command()
        )
    before = (
        await client.store.get(previous.session_digest),
        await client.store.get(member_command().session_digest),
        client.member_session_digests("42"),
        len(client.evalsha_calls),
    )
    client.broken = True
    with pytest.raises(RedisProviderError, match="REDIS_RESPONSE_INVALID"):
        await operation(adapter, name, previous)()
    assert before == (
        await client.store.get(previous.session_digest),
        await client.store.get(member_command().session_digest),
        client.member_session_digests("42"),
        len(client.evalsha_calls),
    )


class ConcurrentTouchClient(EmulatedRedisClient):
    remaining_conflicts = 0

    async def evalsha(self, sha: str, numkeys: int, *values: object) -> bytes:
        if sha != CREATE_SESSION.sha and self.remaining_conflicts:
            self.remaining_conflicts -= 1
            self.clock.advance(1)
            await self.store.touch(str(values[numkeys]))
        return await super().evalsha(sha, numkeys, *values)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["touch", "rotate", "restore", "revoke"])
async def test_confirmed_conflict_rereads_and_preserves_session_rules(name: str) -> None:
    client = ConcurrentTouchClient(ManualClock())
    adapter = RedisSessionAdapter(client)
    previous = await adapter.create(guest_command())
    if name == "restore":
        await adapter.rotate(
            previous_session_digest=previous.session_digest, replacement=member_command()
        )
    calls = len(client.evalsha_calls)
    client.remaining_conflicts = 1
    result = await operation(adapter, name, previous)()
    assert len(client.evalsha_calls) == calls + 2
    assert client.evalsha_calls[-1][2][-1] != client.evalsha_calls[-2][2][-1]
    if name == "revoke":
        assert result is True and await adapter.get(previous.session_digest) is None
    else:
        assert isinstance(result, SessionRecord)
        expected = member_command().csrf_token if name == "rotate" else previous.csrf_token
        assert result.csrf_token == expected
        if name == "restore":
            assert result == previous
            assert await adapter.get(member_command().session_digest) is None


@pytest.mark.asyncio
async def test_repeated_conflicts_stop_without_rotating_the_session() -> None:
    client = ConcurrentTouchClient(ManualClock())
    adapter = RedisSessionAdapter(client)
    previous = await adapter.create(guest_command())
    calls = len(client.evalsha_calls)
    client.remaining_conflicts = 5
    with pytest.raises(RedisProviderError, match="^SESSION_STATE_CHANGED$"):
        await operation(adapter, "rotate", previous)()
    assert len(client.evalsha_calls) == calls + 3
    assert await adapter.get(previous.session_digest) is not None
    assert await adapter.get(member_command().session_digest) is None


class UncertainWriteClient(EmulatedRedisClient):
    fail = False
    failed_attempts = 0

    async def evalsha(self, sha: str, numkeys: int, *values: object) -> bytes:
        if self.fail:
            self.failed_attempts += 1
            raise RedisConnectionError("test-only uncertain write")
        return await super().evalsha(sha, numkeys, *values)


@pytest.mark.asyncio
async def test_uncertain_transport_failure_is_not_replayed() -> None:
    client = UncertainWriteClient(ManualClock())
    adapter = RedisSessionAdapter(client)
    previous = await adapter.create(guest_command())
    client.fail = True
    with pytest.raises(RedisProviderError, match="^REDIS_PROVIDER_UNAVAILABLE$"):
        await adapter.touch(previous.session_digest)
    assert client.failed_attempts == 1


class DisappearingClient(EmulatedRedisClient):
    disappear = False

    async def evalsha(self, sha: str, numkeys: int, *values: object) -> bytes:
        if self.disappear:
            await self.store.revoke(str(values[numkeys]))
        return await super().evalsha(sha, numkeys, *values)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["touch", "rotate", "restore", "revoke"])
async def test_session_disappearing_after_read_is_not_recreated(name: str) -> None:
    client = DisappearingClient(ManualClock())
    adapter = RedisSessionAdapter(client)
    previous = await adapter.create(guest_command())
    if name == "restore":
        await adapter.rotate(
            previous_session_digest=previous.session_digest, replacement=member_command()
        )
    client.disappear = True
    if name in ("rotate", "restore"):
        with pytest.raises(SessionRuleViolation, match="SESSION_NOT_FOUND"):
            await operation(adapter, name, previous)()
    else:
        assert await operation(adapter, name, previous)() in (None, False)
    assert await adapter.get(previous.session_digest) is None
    assert await adapter.get(member_command().session_digest) is None


def test_exact_raw_guards_precede_every_script_write() -> None:
    for script, guard, first_write in (
        (TOUCH_SESSION, "if raw ~= ARGV[3]", "redis.call('DEL', session_key)"),
        (ROTATE_SESSION, "if previous_raw ~= ARGV[10]", "redis.call('DEL', previous_key)"),
        (RESTORE_SESSION, "if failed_raw ~= ARGV[12]", "redis.call('DEL', failed_key)"),
        (REVOKE_SESSION, "if raw ~= ARGV[2]", "redis.call('DEL', session_key)"),
    ):
        assert script.version == 3
        start = script.source.index(guard)
        end = script.source.index("end", start)
        assert "SESSION_STATE_CHANGED" in script.source[start:end]
        assert end < script.source.index(first_write)
