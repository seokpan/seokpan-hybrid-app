"""Opt-in real Lua on an owned, empty Redis 7.2.4 loopback subprocess.

No TLS/ElastiCache/MariaDB/Cloud3 acceptance. The binary is supplied outside
the repository; normal unit runs do not collect these cases.
"""

import asyncio
import json
import os
import socket
import subprocess
import tempfile
from uuid import uuid4

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from seokpan.game.application import DueTurn
from seokpan.game.domain import AppliedMove, Coordinate, EndReason, Stone
from seokpan.persistence.memory import InMemoryGamePersistenceAdapter
from seokpan.persistence.redis.common import RedisKeyspace
from seokpan.persistence.redis.room_adapter import RedisRoomRuntimeAdapter
from seokpan.persistence.redis.turn_coordinator import RedisTurnCoordinator
from seokpan.persistence.redis.vote_adapter import RedisVoteRuntimeAdapter
from seokpan.room.application import (
    ChangeRoomTeam,
    CreateRoomRuntime,
    JoinRoomRuntime,
    LeaveRoomRuntime,
    SetRoomReady,
    StartRoomGame,
)
from seokpan.room.domain import ActorType, RoomConfig, Team
from seokpan.vote.application import (
    AcquireRuntimeDeparture,
    AcquireRuntimeResolver,
    ApplyRuntimeResolution,
    CastRuntimeVote,
    CloseRuntimeTurn,
    FinalizeRuntimeGame,
    InitializeVoteRuntime,
)
from seokpan.vote.domain import TurnResolution, TurnResultKind, TurnStatus, Voter, VoteRuleViolation


@pytest_asyncio.fixture
async def server():
    binary = os.environ.get("SEOKPAN_REDIS_TEST_SERVER")
    if not binary:
        pytest.fail("Set SEOKPAN_REDIS_TEST_SERVER for the owned local Redis 7.2.4 Lua tests")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="seokpan-lua-") as directory:
        process = subprocess.Popen(
            [
                binary,
                "--bind",
                "127.0.0.1",
                "--port",
                str(port),
                "--protected-mode",
                "yes",
                "--save",
                "",
                "--appendonly",
                "no",
                "--daemonize",
                "no",
                "--dir",
                directory,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
        client = Redis(host="127.0.0.1", port=port, db=0, socket_timeout=1)
        try:
            for _ in range(100):
                if process.poll() is not None:
                    pytest.fail("Owned Redis test subprocess failed to start")
                try:
                    await client.ping()
                    break
                except RedisConnectionError:
                    await asyncio.sleep(0.01)
            else:
                pytest.fail("Owned Redis test subprocess did not become ready")
            assert (await client.info("server"))["redis_version"] == "7.2.4"
            assert await client.dbsize() == 0
            yield client
        finally:
            await client.aclose()
            process.terminate()
            process.wait(timeout=5)
            assert process.returncode == 0
            with socket.socket() as probe:
                probe.settimeout(0.2)
                assert probe.connect_ex(("127.0.0.1", port)) != 0


async def setup(client):
    room_id, game_id, black, white = (str(uuid4()) for _ in range(4))
    rooms, votes = RedisRoomRuntimeAdapter(client), RedisVoteRuntimeAdapter(client)
    await rooms.create(
        CreateRoomRuntime(
            room_id=room_id,
            request_id="create",
            owner_id=black,
            owner_session_digest="a" * 64,
            config=RoomConfig(name="synthetic", minimum_ready=2, vote_seconds=5),
        )
    )
    await rooms.join(JoinRoomRuntime(room_id, "join", white, ActorType.MEMBER, "b" * 64, 1))
    for participant, team in ((black, Team.BLACK), (white, Team.WHITE)):
        room = await rooms.get(room_id)
        await rooms.change_team(
            ChangeRoomTeam(room_id, f"team-{team}", participant, team, room.state_version)
        )
        room = await rooms.get(room_id)
        await rooms.set_ready(
            SetRoomReady(room_id, f"ready-{team}", participant, True, room.state_version)
        )
    room = await rooms.get(room_id)
    await rooms.start_game(StartRoomGame(room_id, "start", black, game_id, room.state_version))
    seconds, micros = await client.time()
    now_ms = seconds * 1000 + micros // 1000
    await votes.initialize(
        InitializeVoteRuntime(
            room_id,
            "initialize",
            game_id,
            (Voter(black, Stone.BLACK), Voter(white, Stone.WHITE)),
            now_ms + 60000,
            1,
        )
    )
    return rooms, votes, room_id, game_id, black, white


async def leave(rooms, room_id, participant):
    room = await rooms.get(room_id)
    return await rooms.leave(
        LeaveRoomRuntime(
            room_id, f"leave-{participant}", participant, room.state_version, active_vote_turn=None
        )
    )


async def claim(rooms, votes, room_id, game_id, owner, *, winner=Stone.WHITE):
    room, state = await rooms.get(room_id), await votes.get(room_id)
    return await votes.acquire_departure(
        AcquireRuntimeDeparture(
            room_id,
            f"claim-{owner}",
            game_id,
            state.turn_no,
            owner,
            state.state_version,
            EndReason.FORFEIT,
            winner,
            1 if room is None else room.state_version,
        )
    )


@pytest.mark.asyncio
async def test_real_lua_retains_decision_after_expiry_takeover_and_room_closure(server):
    rooms, votes, r, g, black, white = await setup(server)
    await leave(rooms, r, black)
    with pytest.raises(VoteRuleViolation, match="DEPARTURE_RESULT_CHANGED"):
        await claim(rooms, votes, r, g, "bad-owner", winner=Stone.BLACK)
    first = (await claim(rooms, votes, r, g, "owner-a")).snapshot
    assert first.resolver.departure.winner is Stone.WHITE
    with pytest.raises(VoteRuleViolation, match="RESOLVER_LEASE_HELD"):
        await claim(rooms, votes, r, g, "owner-busy")
    with pytest.raises(VoteRuleViolation, match="DEPARTURE_FINALIZATION_PENDING"):
        await votes.close_turn(CloseRuntimeTurn(r, "close", g, 1, first.state_version, 1))
    key = RedisKeyspace.room_resolver(r, 1)
    assert await server.pttl(key) == -1  # pending intent has no physical expiry
    lease = json.loads(await server.get(key))
    lease["expires_at_ms"] = 0  # controlled logical expiry; no sleeping past deadline
    await server.set(key, json.dumps(lease))
    await leave(rooms, r, white)
    assert await rooms.get(r) is None
    second = (await claim(rooms, votes, r, g, "owner-b")).snapshot
    assert second.resolver.departure == first.resolver.departure
    assert second.resolver.resolution_id != first.resolver.resolution_id
    for owner in ("owner-a", "owner-b"):
        command = FinalizeRuntimeGame(
            r, f"finish-{owner}", g, 1, second.state_version, EndReason.FORFEIT, Stone.WHITE, owner
        )
        if owner == "owner-a":
            with pytest.raises(VoteRuleViolation, match="RESOLVER_NOT_OWNER"):
                await votes.finalize_game(command)
        else:
            result = await votes.finalize_game(command)
            assert result.snapshot.end_reason is EndReason.FORFEIT


@pytest.mark.asyncio
async def test_real_lua_closure_before_claim_rejects_new_intent(server):
    rooms, votes, r, g, black, white = await setup(server)
    await leave(rooms, r, black)
    await leave(rooms, r, white)
    with pytest.raises(VoteRuleViolation, match="ROOM_NOT_FOUND"):
        await claim(rooms, votes, r, g, "late-owner")
    assert (await votes.get(r)).resolver is None


@pytest.mark.asyncio
async def test_real_lua_closed_turn_move_converges_before_departure_and_is_rediscovered(server):
    rooms, votes, r, g, black, _white = await setup(server)
    state = await votes.get(r)
    await votes.cast_vote(
        CastRuntimeVote(r, "vote", g, 1, black, Coordinate.parse("A1"), state.state_version)
    )
    raw = json.loads(await server.get(RedisKeyspace.room_game(r)))
    seconds, micros = await server.time()
    raw["deadline_ms"] = seconds * 1000 + micros // 1000 - 1
    await server.set(RedisKeyspace.room_game(r), json.dumps(raw))
    state = await votes.get(r)
    closed = await votes.close_turn(
        CloseRuntimeTurn(r, "close", g, 1, state.state_version, raw["deadline_ms"] + 5000)
    )
    await leave(rooms, r, black)
    with pytest.raises(VoteRuleViolation, match="TURN_NOT_VOTING"):
        await claim(rooms, votes, r, g, "departure")
    state = await votes.get(r)
    leased = await votes.acquire_resolver(
        AcquireRuntimeResolver(r, "lease", g, 1, "normal", state.state_version)
    )
    move = AppliedMove(1, Stone.BLACK, Coordinate.parse("A1"))
    resolution = TurnResolution(
        g,
        1,
        Stone.BLACK,
        TurnResultKind.MOVE_APPLIED,
        TurnStatus.MOVE_APPLIED,
        move.coordinate,
        move,
        None,
    )
    result = await votes.apply_resolution(
        ApplyRuntimeResolution(
            r,
            "apply",
            g,
            1,
            "normal",
            resolution,
            leased.snapshot.state_version,
            True,
            raw["deadline_ms"] + 5000,
        )
    )
    assert result.snapshot.move_no == 1 and result.snapshot.turn_no == 2
    assert result.snapshot.occupied_cells[0].coordinate == move.coordinate
    assert result.snapshot.resolver is None
    # A confirmed whole-team departure blocks close even before an intent exists.
    with pytest.raises(VoteRuleViolation, match="DEPARTURE_FINALIZATION_PENDING"):
        await votes.close_turn(
            CloseRuntimeTurn(
                r,
                "close-next",
                g,
                2,
                result.snapshot.state_version,
                raw["deadline_ms"] + 10000,
            )
        )
    coordinator = RedisTurnCoordinator(server, rooms, votes, InMemoryGamePersistenceAdapter())
    assert await coordinator.due_turns(now_ms=raw["deadline_ms"] + 1, limit=10) == (
        DueTurn(r, g, 2),
    )
    assert closed.snapshot.turn_status is TurnStatus.RESOLVING
