from __future__ import annotations

from typing import cast
from uuid import uuid4

import pytest
from redis.exceptions import RedisError

from seokpan.identity.application import SessionActorType
from seokpan.persistence.redis.common import VersionedJsonCodec
from seokpan.persistence.redis.presence_adapter import RedisPresenceAdapter
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
    PresenceUnavailable,
)


class ScriptedClient:
    def __init__(self) -> None:
        self.results: list[object] = []
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def get(self, key: str) -> bytes | str | None:
        raise AssertionError(key)

    async def evalsha(self, sha: str, numkeys: int, *values: object) -> object:
        self.calls.append((sha, values))
        return self.results.pop(0)

    async def script_load(self, script: str) -> bytes | str:
        raise AssertionError(script)


def result(*, ok: bool = True, value: object = None, error: object = None) -> str:
    return VersionedJsonCodec.encode({"ok": ok, "value": value, "error": error})


def binding(
    lease: PresenceLease,
    identity: PresenceIdentity,
    digest: str,
) -> dict[str, object]:
    return {
        "lease_id": lease.lease_id,
        "actor_type": identity.actor_type.value,
        "actor_id": identity.actor_id,
        "session_digest": digest,
        "expires_at_ms": 1234,
    }


@pytest.mark.asyncio
async def test_open_uses_shared_keys_and_server_script(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ScriptedClient()
    lease = PresenceLease(str(uuid4()))
    identity = PresenceIdentity(SessionActorType.MEMBER, "42")
    digest = "a" * 64
    client.results.append(result(value=binding(lease, identity, digest)))
    monkeypatch.setattr("seokpan.persistence.redis.presence_adapter.uuid4", lambda: lease.lease_id)

    opened = await RedisPresenceAdapter(client, lease_seconds=15, max_connections=1000).open(
        identity, digest
    )

    assert opened == lease
    sha, values = client.calls[0]
    assert sha == PRESENCE_OPEN.sha
    assert values[:2] == (
        "stone:v1:presence:bindings",
        "stone:v1:presence:expiries",
    )
    assert values[2:] == (lease.lease_id, "MEMBER", "42", digest, 15000, 1000)


@pytest.mark.asyncio
async def test_snapshot_deduplicates_users_and_connections_are_validated() -> None:
    client = ScriptedClient()
    identity = PresenceIdentity(SessionActorType.GUEST, str(uuid4()))
    digest = "b" * 64
    first = PresenceLease(str(uuid4()))
    second = PresenceLease(str(uuid4()))
    payload = {
        "online_users": 1,
        "connections": [binding(first, identity, digest), binding(second, identity, digest)],
    }
    client.results.extend((result(value=payload), result(value=payload)))
    adapter = RedisPresenceAdapter(client, lease_seconds=15, max_connections=1000)

    assert (await adapter.snapshot()).online_users == 1
    assert await adapter.connections() == (
        PresenceConnection(first, identity, digest),
        PresenceConnection(second, identity, digest),
    )
    assert [call[0] for call in client.calls] == [PRESENCE_READ.sha, PRESENCE_READ.sha]


@pytest.mark.asyncio
async def test_mutations_map_contract_errors() -> None:
    client = ScriptedClient()
    adapter = RedisPresenceAdapter(client, lease_seconds=15, max_connections=1000)
    lease = PresenceLease(str(uuid4()))
    identity = PresenceIdentity(SessionActorType.MEMBER, "42")
    digest = "c" * 64
    client.results.extend(
        (
            result(ok=False, error="PRESENCE_SESSION_IDENTITY_MISMATCH"),
            result(ok=False, error="PRESENCE_LEASE_ENDED"),
            result(value=True),
            result(value=2),
        )
    )

    with pytest.raises(PresenceRuleViolation, match="PRESENCE_SESSION_IDENTITY_MISMATCH"):
        await adapter.open(identity, digest)
    with pytest.raises(PresenceRuleViolation, match="PRESENCE_LEASE_ENDED"):
        await adapter.renew(lease)
    await adapter.close(lease)
    await adapter.invalidate_session(digest)

    assert [call[0] for call in client.calls] == [
        PRESENCE_OPEN.sha,
        PRESENCE_RENEW.sha,
        PRESENCE_CLOSE.sha,
        PRESENCE_INVALIDATE_SESSION.sha,
    ]


@pytest.mark.asyncio
async def test_provider_and_invalid_responses_fail_closed() -> None:
    class FailingClient(ScriptedClient):
        async def evalsha(self, sha: str, numkeys: int, *values: object) -> object:
            raise RedisError("secret-bearing provider detail")

    with pytest.raises(PresenceUnavailable, match="REDIS_PROVIDER_UNAVAILABLE"):
        await RedisPresenceAdapter(
            FailingClient(), lease_seconds=15, max_connections=1000
        ).snapshot()

    client = ScriptedClient()
    client.results.extend((b"not-json", result(value={"online_users": 1, "connections": "bad"})))
    adapter = RedisPresenceAdapter(client, lease_seconds=15, max_connections=1000)
    with pytest.raises(PresenceUnavailable, match="PRESENCE_RESPONSE_INVALID"):
        await adapter.snapshot()
    with pytest.raises(PresenceUnavailable, match="PRESENCE_RESPONSE_INVALID"):
        await adapter.connections()


@pytest.mark.parametrize(
    ("lease_seconds", "max_connections"),
    ((0, 1), (float("nan"), 1), (15, 0), (True, 1), (15, True)),
)
def test_configuration_is_strict(lease_seconds: object, max_connections: object) -> None:
    with pytest.raises(PresenceRuleViolation, match="INVALID_PRESENCE_CONFIGURATION"):
        RedisPresenceAdapter(
            ScriptedClient(),
            lease_seconds=cast(float, lease_seconds),
            max_connections=cast(int, max_connections),
        )
