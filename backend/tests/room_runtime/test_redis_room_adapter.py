from __future__ import annotations

import hashlib

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from seokpan.persistence.memory import ManualClock
from seokpan.persistence.redis.common import RedisKeyspace, RedisProviderError, VersionedJsonCodec
from seokpan.persistence.redis.room_adapter import RedisRoomRuntimeAdapter
from seokpan.persistence.redis.room_scripts import (
    ROOM_INVALIDATION_ACK,
    ROOM_MUTATION,
    ROOM_READ,
)
from seokpan.room.application import ChangeRoomIdentity, DisconnectRoomParticipant
from seokpan.room.application.runtime import (
    ROOM_DISCONNECT_LEASE_MS,
    ROOM_RUNTIME_SCHEMA_VERSION,
)
from seokpan.room.domain import ActorType

from .conftest import EmulatedRoomRedisClient, create_room, join_guest


def test_kick_lua_checks_rules_before_removing_only_target_room_state() -> None:
    # Source review only. The emulator does not execute this Lua in Redis.
    kick = ROOM_MUTATION.source.split("if operation == 'kick' then", 1)[1].split(
        "if operation == 'leave' then", 1
    )[0]
    first_write = kick.index("redis.call('HDEL'")
    for code in ("ROOM_NOT_WAITING", "OWNER_REQUIRED", "CANNOT_KICK_SELF", "PARTICIPANT_NOT_FOUND"):
        assert kick.index(f"rejection('{code}')") < first_write
    assert "HDEL', KEYS[2], payload.target_id" in kick
    assert "SREM', KEYS[3], payload.target_id" in kick
    assert "HDEL', KEYS[4], payload.target_id" in kick
    assert "advance_version()" in kick
    assert "remove_vote(" not in kick and "update_game_player(" not in kick
    assert ROOM_MUTATION.version == 15
    participant_guard = ROOM_MUTATION.source.split(
        "local current_participant_id = payload.participant_id or payload.actor_id", 1
    )[1].split("if operation ~= 'disconnect'", 1)[0]
    assert (
        "local current_participant = current_participant_id and "
        "participant(current_participant_id) or nil"
    ) in participant_guard
    assert "operation ~= 'complete_game'" in participant_guard
    assert "operation == 'disconnect' or operation == 'expire_disconnect'" in participant_guard
    assert "rejection('CONNECTION_NOT_FOUND')" in participant_guard


def test_start_game_uses_connected_ready_participants_only() -> None:
    source = ROOM_MUTATION.source
    start = source.split("if operation == 'start_game' then", 1)[1].split(
        "if operation == 'complete_game' then", 1
    )[0]

    assert "PARTICIPANT_DISCONNECTED" in start
    assert "and value.connected" in start
    assert "connected_ready_count" in start
    assert "MINIMUM_READY_NOT_MET" in start


def test_join_rechecks_current_state_without_full_room_version_gate() -> None:
    source = ROOM_MUTATION.source
    join = source.split("if operation == 'join' then", 1)[1].split(
        "local current_participant_id", 1
    )[0]
    assert "expected_version_matches()" not in join
    for current_check in (
        "ROOM_CAPACITY_REACHED",
        "ROOM_PASSWORD_INVALID",
        "PARTICIPANT_ALREADY_JOINED",
    ):
        assert current_check in join


def test_complete_game_is_not_blocked_by_participant_guard() -> None:
    source = ROOM_MUTATION.source
    guard = source.index(
        "local current_participant_id = payload.participant_id or payload.actor_id"
    )
    complete = source.index("if operation == 'complete_game' then")
    assert guard < complete
    participant_guard = source[guard:complete]
    assert (
        "local current_participant = current_participant_id and "
        "participant(current_participant_id) or nil"
    ) in participant_guard
    assert "operation ~= 'complete_game'" in participant_guard
    complete_block = source[complete : source.index("if operation == 'connect' then")]
    assert "expected_version_matches()" in complete_block
    assert "ROOM_NOT_PLAYING" in complete_block
    assert "STALE_GAME" in complete_block


@pytest.mark.asyncio
async def test_mutation_declares_one_hash_slot_and_separated_room_keys() -> None:
    client = EmulatedRoomRedisClient(ManualClock(now_ms=1_000))
    await RedisRoomRuntimeAdapter(client).create(create_room())

    _, numkeys, values = client.evalsha_calls[-1]
    keys = tuple(str(item) for item in values[:numkeys])
    assert numkeys == 10
    assert all("{room-1}" in key for key in keys)
    assert keys[:4] == (
        RedisKeyspace.room_meta("room-1"),
        RedisKeyspace.room_participants("room-1"),
        RedisKeyspace.room_ready("room-1"),
        RedisKeyspace.room_connections("room-1"),
    )
    assert keys[9] == RedisKeyspace.room_game("room-1")
    assert "redis.call('TIME')" in ROOM_MUTATION.source
    assert "expected_version_matches" in ROOM_MUTATION.source
    assert "REQUEST_ID_CONFLICT" in ROOM_MUTATION.source
    assert "HINCRBY', KEYS[9], coordinate, -1" in ROOM_MUTATION.source
    assert "local raw_game = redis.call('GET', KEYS[10])" in ROOM_MUTATION.source
    assert "game.state_version = tonumber(game.state_version or 1) + 1" in (ROOM_MUTATION.source)


@pytest.mark.asyncio
async def test_list_rooms_scans_meta_keys_without_a_second_authority_index() -> None:
    client = EmulatedRoomRedisClient(ManualClock(now_ms=1_000))
    adapter = RedisRoomRuntimeAdapter(client)
    await adapter.create(create_room())

    rooms = await adapter.list_rooms()

    assert tuple(room.room_id for room in rooms) == ("room-1",)


@pytest.mark.asyncio
async def test_shared_binding_lookup_tracks_identity_session_rotation() -> None:
    client = EmulatedRoomRedisClient(ManualClock(now_ms=1_000))
    adapter = RedisRoomRuntimeAdapter(client)
    await adapter.create(create_room())
    await adapter.join(join_guest("guest-1", request_id="join-1"))

    initial = await adapter.find_by_session("f" * 64)
    assert initial is not None
    assert (initial.room_id, initial.participant_id, initial.actor_type) == (
        "room-1",
        "guest-1",
        ActorType.GUEST,
    )
    assert initial.connection_generation == 1
    assert initial.connected is True

    await adapter.change_identity(
        ChangeRoomIdentity(
            room_id="room-1",
            request_id="rotate-1",
            participant_id="guest-1",
            actor_type=ActorType.MEMBER,
            session_digest="b" * 64,
            expected_state_version=2,
        )
    )

    assert await adapter.find_by_session("f" * 64) is None
    rotated = await adapter.find_by_participant("guest-1")
    assert rotated is not None
    assert rotated.session_digest == "b" * 64
    assert rotated.actor_type is ActorType.MEMBER


@pytest.mark.asyncio
async def test_due_disconnects_are_discovered_from_shared_connection_hashes() -> None:
    clock = ManualClock(now_ms=1_000)
    client = EmulatedRoomRedisClient(clock)
    adapter = RedisRoomRuntimeAdapter(client)
    await adapter.create(create_room())
    await adapter.join(join_guest("guest-1", request_id="join-1"))
    await adapter.disconnect(
        DisconnectRoomParticipant(
            room_id="room-1",
            request_id="disconnect-1",
            participant_id="guest-1",
            connection_generation=1,
            expected_state_version=2,
            active_vote_turn=None,
        )
    )

    assert await adapter.due_disconnects(now_ms=1_000, limit=10) == ()
    due = await adapter.due_disconnects(
        now_ms=1_000 + ROOM_DISCONNECT_LEASE_MS,
        limit=10,
    )
    assert len(due) == 1
    assert (
        due[0].room_id,
        due[0].participant_id,
        due[0].connection_generation,
        due[0].expires_at_ms,
    ) == ("room-1", "guest-1", 1, 1_000 + ROOM_DISCONNECT_LEASE_MS)


@pytest.mark.asyncio
async def test_script_cache_miss_loads_exact_room_source() -> None:
    client = EmulatedRoomRedisClient(ManualClock(now_ms=1_000), scripts_loaded=False)
    await RedisRoomRuntimeAdapter(client).create(create_room())

    assert client.script_load_calls == [ROOM_MUTATION.source]
    assert len(client.evalsha_calls) == 2
    assert (
        ROOM_MUTATION.sha
        == hashlib.sha1(ROOM_MUTATION.source.encode(), usedforsecurity=False).hexdigest()
    )


class FailingRoomClient(EmulatedRoomRedisClient):
    async def evalsha(
        self,
        sha: str,
        numkeys: int,
        *keys_and_args: object,
    ) -> bytes:
        raise RedisConnectionError(
            "redis://user:secret@redis.platform.svc/stone:v1:room:{room-1}:meta"
        )


@pytest.mark.asyncio
async def test_provider_error_is_sanitized() -> None:
    adapter = RedisRoomRuntimeAdapter(FailingRoomClient(ManualClock()))

    with pytest.raises(RedisProviderError) as caught:
        await adapter.create(create_room())

    assert str(caught.value) == "REDIS_PROVIDER_UNAVAILABLE"
    assert "secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_due_disconnect_scan_transport_failure_is_unavailable_not_invalid() -> None:
    class FailingScanClient(EmulatedRoomRedisClient):
        async def scan_iter(self, *, match: str, count: int):
            del match, count
            raise RedisConnectionError("redis://user:secret@redis.platform.svc")
            yield b""  # Keep this an async iterator.

    adapter = RedisRoomRuntimeAdapter(FailingScanClient(ManualClock()))
    with pytest.raises(RedisProviderError) as caught:
        await adapter.due_disconnects(now_ms=1_000, limit=1)
    assert caught.value.code == "REDIS_PROVIDER_UNAVAILABLE"
    assert "secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_schema_guard_is_sent_and_precedes_any_lua_state_write() -> None:
    client = EmulatedRoomRedisClient(ManualClock(now_ms=1_000))
    adapter = RedisRoomRuntimeAdapter(client)
    await adapter.create(create_room())
    _, numkeys, values = client.evalsha_calls[-1]
    payload = VersionedJsonCodec.decode(str(values[numkeys + 6]))
    assert payload["schema_version"] == ROOM_RUNTIME_SCHEMA_VERSION == 3
    await adapter.get("room-1")
    _, numkeys, values = client.evalsha_calls[-1]
    assert values[numkeys:] == ("room-1", 3)
    # Static wiring check, not proof of execution by a Redis server.
    assert ROOM_MUTATION.source.index("return rejection('ROOM_SCHEMA_VERSION_MISMATCH')") < (
        ROOM_MUTATION.source.index("local expired =")
    )
    assert "ROOM_SCHEMA_VERSION_MISMATCH" in ROOM_READ.source


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["old_version", "missing_turn", "missing_game", "zero_turn"])
async def test_result_reference_decoder_rejects_old_or_incomplete_room_state(bad: str) -> None:
    client = EmulatedRoomRedisClient(ManualClock(now_ms=1_000))
    created = await RedisRoomRuntimeAdapter(client).create(create_room())
    payload = client._snapshot(created.snapshot)
    assert payload is not None
    expected = "REDIS_RESPONSE_INVALID"
    if bad == "old_version":
        payload["schema_version"] = 2
        expected = "ROOM_SCHEMA_VERSION_MISMATCH"
    elif bad == "missing_turn":
        payload["last_game_id"] = "game-1"
    elif bad == "missing_game":
        payload["last_game_turn_no"] = 5
    else:
        payload.update(last_game_id="game-1", last_game_turn_no=0)
    with pytest.raises(RedisProviderError, match=expected):
        RedisRoomRuntimeAdapter._optional_snapshot(payload)


def test_lua_schema_rejection_is_a_provider_error_not_user_input_error() -> None:
    with pytest.raises(RedisProviderError, match="REDIS_RESPONSE_INVALID"):
        RedisRoomRuntimeAdapter._raise_rejection({"ok": False, "error": None})
    with pytest.raises(RedisProviderError, match="ROOM_SCHEMA_VERSION_MISMATCH"):
        RedisRoomRuntimeAdapter._raise_rejection(
            {"ok": False, "error": "ROOM_SCHEMA_VERSION_MISMATCH"}
        )


def test_system_invalid_tombstone_stays_pending_without_ttl_until_ack() -> None:
    source = ROOM_MUTATION.source
    assert "invalidation_pending = termination == 'SYSTEM_INVALID'" in source
    assert "terminated_game_id" in source
    assert "closed_at_ms = current_ms" in source

    pending = source.split("if termination == 'SYSTEM_INVALID' then", 1)[1].split("else", 1)[0]
    assert "redis.call('SET', KEYS[7], marker)" in pending
    assert "'PX'" not in pending

    assert ROOM_INVALIDATION_ACK.version == 2
    assert "value.invalidation_pending = false" in ROOM_INVALIDATION_ACK.source
