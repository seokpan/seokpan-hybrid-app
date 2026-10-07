from __future__ import annotations

import pytest
from redis.exceptions import ConnectionError

from seokpan.game.domain import Stone
from seokpan.persistence.memory import ManualClock
from seokpan.persistence.redis.common import RedisKeyspace, RedisProviderError, VersionedJsonCodec
from seokpan.persistence.redis.vote_adapter import RedisVoteRuntimeAdapter, _list
from seokpan.persistence.redis.vote_scripts import VOTE_DISCARD, VOTE_MUTATION, VOTE_READ
from seokpan.vote.application import InitializeVoteRuntime
from seokpan.vote.domain import Voter

from .conftest import EmulatedVoteRedisClient, _snapshot


def test_vote_keyspace_uses_one_room_hash_tag() -> None:
    keys = (
        RedisKeyspace.room_game("00000000-0000-4000-8000-000000000101"),
        RedisKeyspace.room_board("00000000-0000-4000-8000-000000000101"),
        RedisKeyspace.room_votes("00000000-0000-4000-8000-000000000101", 1),
        RedisKeyspace.room_vote_tally("00000000-0000-4000-8000-000000000101", 1),
        RedisKeyspace.room_resolver("00000000-0000-4000-8000-000000000101", 1),
    )
    assert all("{00000000-0000-4000-8000-000000000101}" in key for key in keys)
    assert len(set(keys)) == len(keys)
    assert "local function response(value)" in VOTE_MUTATION.source
    assert "return value\nend" in VOTE_MUTATION.source
    assert "return remember(response({" in VOTE_MUTATION.source
    assert "state_version = payload.expected_state_version + 1" in VOTE_MUTATION.source
    assert "game.state_version = current_version(game) + 1" in VOTE_MUTATION.source
    assert "current_version(game) == payload.expected_state_version" in VOTE_MUTATION.source
    assert "payload.expected_room_state_version" in VOTE_MUTATION.source
    assert "'status') ~= 'PLAYING'" in VOTE_MUTATION.source
    assert "'game_id') ~= payload.game_id" in VOTE_MUTATION.source


@pytest.mark.asyncio
async def test_discard_game_uses_atomic_same_slot_cleanup() -> None:
    client = EmulatedVoteRedisClient(ManualClock())
    adapter = RedisVoteRuntimeAdapter(client)
    await adapter.initialize(
        InitializeVoteRuntime(
            "00000000-0000-4000-8000-000000000101",
            "init-discard",
            "00000000-0000-4000-8000-000000000102",
            (Voter("black-1", Stone.BLACK), Voter("white-1", Stone.WHITE)),
            1_000,
            1,
        )
    )

    await adapter.discard_game(
        "00000000-0000-4000-8000-000000000101", "00000000-0000-4000-8000-000000000102"
    )

    sha, count, values = client.evalsha_calls[-1]
    assert sha == VOTE_DISCARD.sha
    assert count == 10
    assert all("{00000000-0000-4000-8000-000000000101}" in str(key) for key in values[:count])
    assert "game.game_id ~= ARGV[1]" in VOTE_DISCARD.source
    assert "game.turn_no" in VOTE_DISCARD.source
    assert "redis.call('DEL', unpack(KEYS))" in VOTE_DISCARD.source
    assert await adapter.get("00000000-0000-4000-8000-000000000101") is None


@pytest.mark.asyncio
async def test_script_cache_miss_loads_exact_versioned_script() -> None:
    clock = ManualClock()
    client = EmulatedVoteRedisClient(clock, scripts_loaded=False)
    adapter = RedisVoteRuntimeAdapter(client)

    await adapter.initialize(
        InitializeVoteRuntime(
            "00000000-0000-4000-8000-000000000101",
            "initialize-1",
            "00000000-0000-4000-8000-000000000102",
            (Voter("black-1", Stone.BLACK), Voter("white-1", Stone.WHITE)),
            1_000,
            1,
        )
    )

    assert client.script_load_calls == [VOTE_MUTATION.source]
    assert len(client.evalsha_calls) == 2
    assert client.evalsha_calls[0][0] == VOTE_MUTATION.sha
    assert client.evalsha_calls[0][1] == 15


class FailingRedisClient:
    async def get(self, key: str) -> None:
        raise ConnectionError("redis://secret-host:6379")

    async def evalsha(self, sha: str, numkeys: int, *keys_and_args: object) -> object:
        raise ConnectionError("redis://secret-host:6379")

    async def script_load(self, script: str) -> str:
        raise ConnectionError("redis://secret-host:6379")


@pytest.mark.asyncio
async def test_provider_error_is_sanitized() -> None:
    adapter = RedisVoteRuntimeAdapter(FailingRedisClient())
    with pytest.raises(RedisProviderError) as raised:
        await adapter.get("00000000-0000-4000-8000-000000000101")
    assert str(raised.value) == "REDIS_PROVIDER_UNAVAILABLE"
    assert "secret-host" not in str(raised.value)


@pytest.mark.asyncio
async def test_replacement_declares_only_previous_turn_cleanup_keys() -> None:
    client = EmulatedVoteRedisClient(ManualClock())
    adapter = RedisVoteRuntimeAdapter(client)
    voters = (Voter("black-1", Stone.BLACK), Voter("white-1", Stone.WHITE))
    await adapter.initialize(
        InitializeVoteRuntime(
            "00000000-0000-4000-8000-000000000101",
            "start-1",
            "00000000-0000-4000-8000-000000000102",
            voters,
            1000,
            1,
        )
    )
    client.store._states["00000000-0000-4000-8000-000000000101"].game.game.finish_system_invalid()
    await adapter.initialize(
        InitializeVoteRuntime(
            "00000000-0000-4000-8000-000000000101",
            "start-2",
            "00000000-0000-4000-8000-000000000103",
            voters,
            2000,
            1,
            previous_game_id="00000000-0000-4000-8000-000000000102",
            previous_turn_no=1,
        )
    )
    _, count, values = client.evalsha_calls[-1]
    assert count == 18
    assert values[13:16] == (
        RedisKeyspace.room_votes("00000000-0000-4000-8000-000000000101", 1),
        RedisKeyspace.room_vote_tally("00000000-0000-4000-8000-000000000101", 1),
        RedisKeyspace.room_resolver("00000000-0000-4000-8000-000000000101", 1),
    )
    assert all("{00000000-0000-4000-8000-000000000101}" in str(key) for key in values[:count])
    # Static Lua checks, not a claim that a real Redis server executed the script.
    assert "local request_id = 'vote:' .. ARGV[2]" in VOTE_MUTATION.source
    assert "redis.call('DEL', KEYS[14], KEYS[15], KEYS[16])" in VOTE_MUTATION.source
    assert "redis.call('DEL', KEYS[12]" not in VOTE_MUTATION.source
    assert VOTE_MUTATION.source.index("'status') ~= 'PLAYING'") < VOTE_MUTATION.source.index(
        "if cached then"
    )


class ChangedSnapshotClient(EmulatedVoteRedisClient):
    read_attempts = 0

    async def evalsha(self, sha: str, numkeys: int, *keys_and_args: object) -> bytes:
        if sha == VOTE_READ.sha:
            self.read_attempts += 1
            payload = VersionedJsonCodec.decode(str(keys_and_args[numkeys]))
            assert payload == {
                "room_id": "00000000-0000-4000-8000-000000000101",
                "game_id": "00000000-0000-4000-8000-000000000102",
                "turn_no": 1,
            }
            return self._encode({"ok": False, "error": "REDIS_SNAPSHOT_CHANGED"})
        return await super().evalsha(sha, numkeys, *keys_and_args)


@pytest.mark.asyncio
async def test_read_does_not_mix_new_game_with_previous_turn_keys() -> None:
    client = ChangedSnapshotClient(ManualClock())
    adapter = RedisVoteRuntimeAdapter(client)
    await adapter.initialize(
        InitializeVoteRuntime(
            "00000000-0000-4000-8000-000000000101",
            "init",
            "00000000-0000-4000-8000-000000000102",
            (Voter("black-1", Stone.BLACK), Voter("white-1", Stone.WHITE)),
            1000,
            1,
        )
    )
    with pytest.raises(RedisProviderError, match="REDIS_SNAPSHOT_CHANGED"):
        await adapter.get("00000000-0000-4000-8000-000000000101")
    assert "game.game_id ~= payload.game_id or game.turn_no ~= payload.turn_no" in VOTE_READ.source
    assert client.read_attempts == 3


class StaleFirstGameReadClient(EmulatedVoteRedisClient):
    def __init__(self, clock: ManualClock) -> None:
        super().__init__(clock)
        self.game_reads = 0

    async def get(self, key: str) -> bytes | None:
        value = await super().get(key)
        if value is not None and self.game_reads == 0:
            self.game_reads += 1
            game = VersionedJsonCodec.decode(value)
            game["turn_no"] = 0
            return self._encode(game)
        self.game_reads += 1
        return value

    async def evalsha(self, sha: str, numkeys: int, *keys_and_args: object) -> bytes:
        if sha == VOTE_READ.sha:
            payload = VersionedJsonCodec.decode(str(keys_and_args[numkeys]))
            if payload["turn_no"] == 0:
                self.evalsha_calls.append((sha, numkeys, keys_and_args))
                return self._encode({"ok": False, "error": "REDIS_SNAPSHOT_CHANGED"})
        return await super().evalsha(sha, numkeys, *keys_and_args)


@pytest.mark.asyncio
async def test_read_retries_entire_snapshot_after_turn_change() -> None:
    client = StaleFirstGameReadClient(ManualClock())
    adapter = RedisVoteRuntimeAdapter(client)
    room_id = "00000000-0000-4000-8000-000000000101"
    await adapter.initialize(
        InitializeVoteRuntime(
            room_id,
            "init",
            "00000000-0000-4000-8000-000000000102",
            (Voter("black-1", Stone.BLACK), Voter("white-1", Stone.WHITE)),
            1000,
            1,
        )
    )

    snapshot = await adapter.get(room_id)

    assert snapshot is not None and snapshot.turn_no == 1
    assert client.game_reads == 2
    reads = [call for call in client.evalsha_calls if call[0] == VOTE_READ.sha]
    assert len(reads) == 2
    assert reads[0][2][4] == RedisKeyspace.room_votes(room_id, 0)
    assert reads[1][2][4] == RedisKeyspace.room_votes(room_id, 1)


def test_missing_rejection_code_is_provider_failure() -> None:
    with pytest.raises(RedisProviderError, match="REDIS_RESPONSE_INVALID"):
        RedisVoteRuntimeAdapter._raise_rejection({"ok": False, "error": None})


def test_empty_lua_object_is_accepted_only_as_empty_array() -> None:
    assert _list({}) == []
    with pytest.raises(RedisProviderError, match="REDIS_RESPONSE_INVALID"):
        _list({"unexpected": "value"})


@pytest.mark.parametrize("version", [1, 2, 4])
def test_old_or_future_vote_snapshot_is_not_interpreted(version: int) -> None:
    with pytest.raises(RedisProviderError, match="VOTE_SCHEMA_VERSION_MISMATCH"):
        RedisVoteRuntimeAdapter._optional_snapshot({"schema_version": version})


def test_last_move_lua_write_is_persistence_gated_and_readable() -> None:
    assert VOTE_MUTATION.version == 11
    assert VOTE_READ.version == 5
    assert VOTE_MUTATION.source.index("existing.schema_version ~= 3") < VOTE_MUTATION.source.index(
        "local expired"
    )
    assert "cached.result.snapshot.schema_version ~= 3" in VOTE_MUTATION.source
    assert VOTE_MUTATION.source.index(
        "if not payload.persistence_confirmed"
    ) < VOTE_MUTATION.source.index("game.last_move = resolution.applied_move")
    assert "last_move = cjson.null" in VOTE_MUTATION.source
    assert "last_move = game.last_move" in VOTE_READ.source
    assert "game.schema_version ~= 3" in VOTE_READ.source


@pytest.mark.parametrize(
    "value",
    [
        {"move_no": 0, "team": "BLACK", "coordinate": "H8"},
        {"move_no": 1, "team": "EMPTY", "coordinate": "H8"},
        {"move_no": 1, "team": "BLACK", "coordinate": "P1"},
        {"move_no": 1, "team": "WHITE", "coordinate": "h8"},
    ],
)
def test_invalid_last_move_is_provider_failure(value: dict[str, object]) -> None:
    with pytest.raises(RedisProviderError, match="REDIS_RESPONSE_INVALID"):
        RedisVoteRuntimeAdapter._last_move(value)


@pytest.mark.asyncio
async def test_last_move_must_match_snapshot_board_and_count() -> None:
    from dataclasses import replace

    from seokpan.game.domain import AppliedMove, BoardCell, Coordinate

    client = EmulatedVoteRedisClient(ManualClock())
    start = await client.store.initialize(
        InitializeVoteRuntime(
            "00000000-0000-4000-8000-000000000101",
            "init",
            "00000000-0000-4000-8000-000000000102",
            (Voter("b", Stone.BLACK), Voter("w", Stone.WHITE)),
            1000,
            1,
        )
    )
    move = AppliedMove(1, Stone.BLACK, Coordinate.parse("H8"))
    current = replace(
        start.snapshot,
        move_no=1,
        last_move=move,
        occupied_cells=(BoardCell(move.coordinate, Stone.BLACK),),
    )
    assert RedisVoteRuntimeAdapter._optional_snapshot(_snapshot(current)) == current
    for changes in (
        {"last_move": None},
        {"move_no": 0},
        {"last_move": replace(move, move_no=2)},
        {"last_move": replace(move, team=Stone.WHITE)},
    ):
        with pytest.raises(RedisProviderError, match="REDIS_RESPONSE_INVALID"):
            RedisVoteRuntimeAdapter._optional_snapshot(_snapshot(replace(current, **changes)))
    missing = _snapshot(start.snapshot)
    assert missing is not None
    del missing["last_move"]
    with pytest.raises(RedisProviderError, match="REDIS_RESPONSE_INVALID"):
        RedisVoteRuntimeAdapter._optional_snapshot(missing)


class OldVoteClient(EmulatedVoteRedisClient):
    async def get(self, key: str) -> bytes:
        return self._encode({"schema_version": 2})


@pytest.mark.asyncio
async def test_old_game_read_stops_before_script_execution() -> None:
    client = OldVoteClient(ManualClock())
    with pytest.raises(RedisProviderError, match="VOTE_SCHEMA_VERSION_MISMATCH"):
        await RedisVoteRuntimeAdapter(client).get("00000000-0000-4000-8000-000000000101")
    assert client.evalsha_calls == []


def test_external_finalization_lua_accepts_system_invalid_without_winner() -> None:
    source = VOTE_MUTATION.source
    assert "payload.end_reason == 'SYSTEM_INVALID'" in source
    assert "payload.winner == 'EMPTY'" in source
    assert "game.game_status = payload.end_reason == 'SYSTEM_INVALID'" in source
