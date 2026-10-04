from __future__ import annotations

from typing import cast
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from seokpan.persistence.redis.common import VersionedJsonCodec
from seokpan.persistence.redis.realtime_adapter import (
    RealtimeUnavailable,
    RedisRealtimeEventAdapter,
)
from seokpan.persistence.redis.realtime_scripts import PUBLISH_REALTIME


class ScriptedClient:
    def __init__(self) -> None:
        self.raw_version: object = None
        self.result: object = None
        self.call: tuple[str, int, tuple[object, ...]] | None = None

    async def get(self, key: str) -> object:
        assert key.startswith("stone:v1:realtime:")
        return self.raw_version

    async def evalsha(self, sha: str, numkeys: int, *values: object) -> object:
        self.call = (sha, numkeys, values)
        return self.result

    async def script_load(self, script: str) -> bytes | str:
        raise AssertionError(script)


def event(*, version: int = 2) -> dict[str, object]:
    return {
        "event_type": "lobby.rooms_changed",
        "schema_version": 1,
        "event_id": str(uuid4()),
        "occurred_at": "2026-09-14T00:00:00Z",
        "state_version": version,
        "room_id": None,
        "game_id": None,
        "turn_no": None,
        "payload": {"reason": "test"},
    }


def response(*, ok: bool, value: object = None, error: object = None) -> str:
    return VersionedJsonCodec.encode({"ok": ok, "value": value, "error": error})


@pytest.mark.asyncio
async def test_publish_uses_atomic_scope_and_updates_local_version() -> None:
    client = ScriptedClient()
    client.result = response(ok=True, value=event(version=7))
    adapter = RedisRealtimeEventAdapter(
        cast(Redis, client), max_idempotency_keys=10, idempotency_seconds=3
    )

    await adapter.lobby_rooms_changed({"reason": "test"}, event_key="request-1")

    assert adapter.lobby_version == 7
    assert client.call is not None
    sha, numkeys, values = client.call
    assert sha == PUBLISH_REALTIME.sha
    assert numkeys == 4
    assert values[:4] == (
        "stone:v1:realtime:{lobby}:version",
        "stone:v1:realtime:{lobby}:events",
        "stone:v1:realtime:{lobby}:event-expiries",
        "stone:v1:realtime:{lobby}:channel",
    )
    assert values[4:7] == ("request-1", 10, 3000)


@pytest.mark.asyncio
async def test_versions_are_read_from_redis_not_only_process_cache() -> None:
    client = ScriptedClient()
    adapter = RedisRealtimeEventAdapter(cast(Redis, client))
    client.raw_version = b"12"
    assert await adapter.current_lobby_version() == 12
    client.raw_version = None
    assert await adapter.current_room_version("room-1") == 1


@pytest.mark.asyncio
async def test_room_publish_tracks_independent_scope() -> None:
    client = ScriptedClient()
    value = event(version=4)
    value.update({"event_type": "room.changed", "room_id": "room-1"})
    client.result = response(ok=True, value=value)
    adapter = RedisRealtimeEventAdapter(cast(Redis, client))

    await adapter.room_changed(
        event_type="room.changed",
        room_id="room-1",
        payload={"reason": "test"},
    )

    assert adapter.room_version("room-1") == 4
    assert client.call is not None
    assert client.call[2][:4] == (
        "stone:v1:realtime:{room:room-1}:version",
        "stone:v1:realtime:{room:room-1}:events",
        "stone:v1:realtime:{room:room-1}:event-expiries",
        "stone:v1:realtime:{room:room-1}:channel",
    )


@pytest.mark.asyncio
async def test_provider_and_capacity_fail_closed() -> None:
    client = ScriptedClient()
    client.result = response(
        ok=False,
        error="REALTIME_IDEMPOTENCY_CAPACITY_REACHED",
    )
    with pytest.raises(RealtimeUnavailable, match="REALTIME_IDEMPOTENCY_CAPACITY_REACHED"):
        await RedisRealtimeEventAdapter(cast(Redis, client)).lobby_rooms_changed({})

    class FailingClient(ScriptedClient):
        async def get(self, key: str) -> object:
            raise RedisError("provider detail")

    with pytest.raises(RealtimeUnavailable, match="REALTIME_PROVIDER_UNAVAILABLE"):
        await RedisRealtimeEventAdapter(cast(Redis, FailingClient())).current_lobby_version()


@pytest.mark.parametrize(
    ("capacity", "seconds"),
    ((0, 1), (1, 0), (True, 1), (1, float("nan"))),
)
def test_configuration_is_strict(capacity: object, seconds: object) -> None:
    with pytest.raises(ValueError, match="INVALID_REALTIME_CONFIGURATION"):
        RedisRealtimeEventAdapter(
            cast(Redis, ScriptedClient()),
            max_idempotency_keys=cast(int, capacity),
            idempotency_seconds=cast(float, seconds),
        )
