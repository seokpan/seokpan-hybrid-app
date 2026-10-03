from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from redis.exceptions import NoScriptError

from seokpan.persistence.memory import InMemoryRoomRuntimeAdapter, ManualClock
from seokpan.persistence.redis.common import VersionedJsonCodec
from seokpan.persistence.redis.room_adapter import RedisRoomRuntimeAdapter
from seokpan.persistence.redis.room_scripts import (
    ROOM_INVALIDATION_ACK,
    ROOM_MUTATION,
    ROOM_PRIVATE_HASH_READ,
    ROOM_READ,
)
from seokpan.room.application import (
    ChangeRoomIdentity,
    ChangeRoomTeam,
    ChangeRoomVoteSeconds,
    CompleteRoomGame,
    ConnectRoomParticipant,
    CreateRoomRuntime,
    DisconnectRoomParticipant,
    ExpireRoomDisconnect,
    JoinRoomRuntime,
    KickRoomParticipant,
    LeaveRoomRuntime,
    RoomMutationResult,
    RoomRuntimePort,
    RoomRuntimeSnapshot,
    SetRoomReady,
    StartRoomGame,
)
from seokpan.room.domain import ActorType, RoomConfig, RoomRuleViolation, RoomVisibility, Team


@dataclass(slots=True)
class RoomRuntimeHarness:
    adapter: RoomRuntimePort
    clock: ManualClock
    observable: InMemoryRoomRuntimeAdapter


class EmulatedRoomRedisClient:
    """Lua command-boundary emulator; it is not actual Redis evidence."""

    def __init__(self, clock: ManualClock, *, scripts_loaded: bool = True) -> None:
        self.store = InMemoryRoomRuntimeAdapter(clock)
        self.loaded = (
            {
                ROOM_MUTATION.sha,
                ROOM_READ.sha,
                ROOM_PRIVATE_HASH_READ.sha,
                ROOM_INVALIDATION_ACK.sha,
            }
            if scripts_loaded
            else set()
        )
        self.evalsha_calls: list[tuple[str, int, tuple[object, ...]]] = []
        self.script_load_calls: list[str] = []

    async def get(self, key: str) -> bytes | None:
        if key.endswith(":closed"):
            room_id = self._room_id(key)
            pending = self.store._pending_game_invalidations.get(room_id)
            if pending is None:
                return None
            return self._encode(
                {
                    "room_id": pending.room_id,
                    "terminated_game_id": pending.game_id,
                    "closed_at_ms": pending.closed_at_ms,
                    "invalidation_pending": True,
                }
            )
        return None

    async def scan_iter(self, *, match: str, count: int) -> AsyncIterator[bytes]:
        assert match in {
            "stone:v1:room:{*}:meta",
            "stone:v1:room:{*}:connections",
            "stone:v1:room:{*}:closed",
        }
        assert count == 100
        if match.endswith(":closed"):
            for pending in await self.store.pending_game_invalidations(limit=100):
                yield f"stone:v1:room:{{{pending.room_id}}}:closed".encode()
            return
        suffix = "meta" if match.endswith(":meta") else "connections"
        for room in await self.store.list_rooms():
            yield f"stone:v1:room:{{{room.room_id}}}:{suffix}".encode()

    async def hgetall(self, key: str) -> dict[bytes, bytes]:
        room_id = self._room_id(key)
        state = self.store._rooms.get(room_id)
        if state is None:
            return {}
        return {
            participant_id.encode(): self._encode(
                {
                    "session_digest": connection.session_digest,
                    "generation": connection.generation,
                    "connected": connection.connected,
                    "disconnect_expires_at_ms": connection.disconnect_expires_at_ms,
                }
            )
            for participant_id, connection in state.connections.items()
        }

    async def evalsha(
        self,
        sha: str,
        numkeys: int,
        *keys_and_args: object,
    ) -> bytes:
        self.evalsha_calls.append((sha, numkeys, keys_and_args))
        if sha not in self.loaded:
            raise NoScriptError("script cache miss")
        keys = tuple(str(item) for item in keys_and_args[:numkeys])
        args = keys_and_args[numkeys:]
        room_id = self._room_id(keys[0])
        if sha == ROOM_READ.sha:
            return self._response(snapshot=await self.store.get(room_id))
        if sha == ROOM_PRIVATE_HASH_READ.sha:
            return self._encode(
                {
                    "ok": True,
                    "encoded_password": await self.store.get_private_access_hash(room_id),
                    "error": None,
                }
            )
        if sha == ROOM_INVALIDATION_ACK.sha:
            game_id = str(args[0])
            pending = self.store._pending_game_invalidations.get(room_id)
            if pending is None:
                return self._encode({"ok": True, "missing": True, "error": None})
            try:
                await self.store.complete_game_invalidation(room_id, game_id)
            except RoomRuleViolation as error:
                return self._encode({"ok": False, "error": error.code})
            return self._encode({"ok": True, "missing": False, "error": None})
        if sha != ROOM_MUTATION.sha:
            raise AssertionError("unknown script")

        operation = str(args[1])
        request_id = str(args[2])
        payload = VersionedJsonCodec.decode(str(args[6]))
        try:
            result = await self._mutate(room_id, request_id, operation, payload)
        except RoomRuleViolation as error:
            return self._encode({"ok": False, "error": error.code})
        return self._response(result=result)

    async def script_load(self, script: str) -> str:
        self.script_load_calls.append(script)
        for candidate in (ROOM_MUTATION, ROOM_READ, ROOM_PRIVATE_HASH_READ, ROOM_INVALIDATION_ACK):
            if candidate.source == script:
                self.loaded.add(candidate.sha)
                return candidate.sha
        raise AssertionError("unknown script source")

    async def _mutate(
        self,
        room_id: str,
        request_id: str,
        operation: str,
        payload: dict[str, object],
    ) -> RoomMutationResult:
        if operation == "create":
            return await self.store.create(
                CreateRoomRuntime(
                    room_id=room_id,
                    request_id=request_id,
                    config=RoomConfig(
                        name=str(payload["name"]),
                        visibility=RoomVisibility(str(payload["visibility"])),
                        max_participants=int(str(payload["max_participants"])),
                        minimum_ready=int(str(payload["minimum_ready"])),
                        vote_seconds=int(str(payload["vote_seconds"])),
                    ),
                    owner_id=str(payload["owner_id"]),
                    owner_session_digest=str(payload["session_digest"]),
                    encoded_password=str(payload["encoded_password"]) or None,
                )
            )
        if operation == "join":
            return await self.store.join(
                JoinRoomRuntime(
                    room_id=room_id,
                    request_id=request_id,
                    participant_id=str(payload["participant_id"]),
                    actor_type=ActorType(str(payload["actor_type"])),
                    session_digest=str(payload["session_digest"]),
                    expected_state_version=int(str(payload["expected_state_version"])),
                    private_access_verified=bool(payload["private_access_verified"]),
                )
            )
        if operation == "kick":
            return await self.store.kick(
                KickRoomParticipant(
                    room_id,
                    request_id,
                    str(payload["actor_id"]),
                    str(payload["target_id"]),
                    int(str(payload["expected_state_version"])),
                )
            )
        if operation == "change_team":
            return await self.store.change_team(
                ChangeRoomTeam(
                    room_id,
                    request_id,
                    str(payload["participant_id"]),
                    Team(str(payload["team"])),
                    int(str(payload["expected_state_version"])),
                )
            )
        if operation == "change_identity":
            return await self.store.change_identity(
                ChangeRoomIdentity(
                    room_id,
                    request_id,
                    str(payload["participant_id"]),
                    ActorType(str(payload["actor_type"])),
                    str(payload["session_digest"]),
                    int(str(payload["expected_state_version"])),
                )
            )
        if operation == "set_ready":
            return await self.store.set_ready(
                SetRoomReady(
                    room_id,
                    request_id,
                    str(payload["participant_id"]),
                    bool(payload["ready"]),
                    int(str(payload["expected_state_version"])),
                )
            )
        if operation == "change_vote_seconds":
            return await self.store.change_vote_seconds(
                ChangeRoomVoteSeconds(
                    room_id,
                    request_id,
                    str(payload["actor_id"]),
                    int(str(payload["vote_seconds"])),
                    int(str(payload["expected_state_version"])),
                )
            )
        if operation == "start_game":
            return await self.store.start_game(
                StartRoomGame(
                    room_id,
                    request_id,
                    str(payload["actor_id"]),
                    str(payload["game_id"]),
                    int(str(payload["expected_state_version"])),
                )
            )
        if operation == "complete_game":
            return await self.store.complete_game(
                CompleteRoomGame(
                    room_id,
                    request_id,
                    str(payload["game_id"]),
                    int(str(payload["expected_state_version"])),
                    int(str(payload["final_turn_no"])),
                )
            )
        if operation == "connect":
            return await self.store.connect(
                ConnectRoomParticipant(
                    room_id,
                    request_id,
                    str(payload["participant_id"]),
                    str(payload["session_digest"]),
                    int(str(payload["expected_state_version"])),
                )
            )
        active_vote_turn = payload.get("active_vote_turn")
        normalized_turn = None if active_vote_turn is None else int(str(active_vote_turn))
        if operation == "disconnect":
            return await self.store.disconnect(
                DisconnectRoomParticipant(
                    room_id,
                    request_id,
                    str(payload["participant_id"]),
                    int(str(payload["connection_generation"])),
                    int(str(payload["expected_state_version"])),
                    normalized_turn,
                )
            )
        if operation == "expire_disconnect":
            return await self.store.expire_disconnect(
                ExpireRoomDisconnect(
                    room_id,
                    request_id,
                    str(payload["participant_id"]),
                    int(str(payload["connection_generation"])),
                    int(str(payload["expected_state_version"])),
                    normalized_turn,
                )
            )
        if operation == "leave":
            return await self.store.leave(
                LeaveRoomRuntime(
                    room_id,
                    request_id,
                    str(payload["participant_id"]),
                    int(str(payload["expected_state_version"])),
                    normalized_turn,
                )
            )
        raise AssertionError("unknown operation")

    @classmethod
    def _response(
        cls,
        *,
        result: RoomMutationResult | None = None,
        snapshot: RoomRuntimeSnapshot | None = None,
    ) -> bytes:
        value: dict[str, object] = {
            "ok": True,
            "error": None,
            "snapshot": cls._snapshot(snapshot if result is None else result.snapshot),
        }
        if result is not None:
            value.update(
                {
                    "replayed": result.replayed,
                    "connection_generation": result.connection_generation,
                    "disconnect_expires_at_ms": result.disconnect_expires_at_ms,
                    "stale_connection": result.stale_connection,
                    "vote_removed": result.vote_removed,
                    "operation_at_ms": result.operation_at_ms,
                    "departure": (
                        None
                        if result.departure is None
                        else {
                            "previous_owner_id": result.departure.previous_owner_id,
                            "new_owner_id": result.departure.new_owner_id,
                            "room_closed": result.departure.room_closed,
                            "game_termination": result.departure.game_termination.value,
                            "terminated_game_id": result.departure.terminated_game_id,
                        }
                    ),
                    "start_roster": (
                        None
                        if result.start_roster is None
                        else [
                            {
                                "participant_id": item.participant_id,
                                "team": item.team.value,
                                "role": item.role.value,
                            }
                            for item in result.start_roster.entries
                        ]
                    ),
                }
            )
        return cls._encode(value)

    @staticmethod
    def _snapshot(snapshot: RoomRuntimeSnapshot | None) -> dict[str, object] | None:
        if snapshot is None:
            return None
        return {
            "schema_version": snapshot.schema_version,
            "last_game_id": snapshot.last_game_id,
            "last_game_turn_no": snapshot.last_game_turn_no,
            "room_id": snapshot.room_id,
            "config": {
                "name": snapshot.config.name,
                "visibility": snapshot.config.visibility.value,
                "max_participants": snapshot.config.max_participants,
                "minimum_ready": snapshot.config.minimum_ready,
                "vote_seconds": snapshot.config.vote_seconds,
            },
            "status": snapshot.status.value,
            "owner_id": snapshot.owner_id,
            "state_version": snapshot.state_version,
            "participants": [
                {
                    "participant_id": item.participant_id,
                    "actor_type": item.actor_type.value,
                    "joined_order": item.joined_order,
                    "connected": item.connected,
                    "team": item.team.value,
                    "ready": item.ready,
                }
                for item in snapshot.participants
            ],
            "game_id": snapshot.game_id,
        }

    @staticmethod
    def _encode(value: dict[str, object]) -> bytes:
        return VersionedJsonCodec.encode(value).encode()

    @staticmethod
    def _room_id(key: str) -> str:
        return key.split("{", maxsplit=1)[1].split("}", maxsplit=1)[0]


@pytest.fixture(params=("memory", "redis-boundary"))
def room_harness(request: pytest.FixtureRequest) -> RoomRuntimeHarness:
    clock = ManualClock(now_ms=1_000)
    observable = InMemoryRoomRuntimeAdapter(clock)
    if request.param == "memory":
        return RoomRuntimeHarness(adapter=observable, clock=clock, observable=observable)
    client = EmulatedRoomRedisClient(clock)
    return RoomRuntimeHarness(
        adapter=RedisRoomRuntimeAdapter(client),
        clock=clock,
        observable=client.store,
    )


def digest(character: str) -> str:
    return character * 64


def create_room(
    *,
    request_id: str = "create-1",
    visibility: RoomVisibility = RoomVisibility.PUBLIC,
    minimum_ready: int = 4,
) -> CreateRoomRuntime:
    return CreateRoomRuntime(
        room_id="room-1",
        request_id=request_id,
        config=RoomConfig(
            name="MVP Room",
            visibility=visibility,
            minimum_ready=minimum_ready,
        ),
        owner_id="member-1",
        owner_session_digest=digest("a"),
        encoded_password=(
            "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA"
            if visibility is RoomVisibility.PRIVATE
            else None
        ),
    )


def join_member(
    participant_id: str,
    *,
    request_id: str,
    session_character: str,
    expected_state_version: int = 1,
    private_access_verified: bool = False,
) -> JoinRoomRuntime:
    return JoinRoomRuntime(
        room_id="room-1",
        request_id=request_id,
        participant_id=participant_id,
        actor_type=ActorType.MEMBER,
        session_digest=digest(session_character),
        expected_state_version=expected_state_version,
        private_access_verified=private_access_verified,
    )


def join_guest(
    participant_id: str,
    *,
    request_id: str,
    expected_state_version: int = 1,
) -> JoinRoomRuntime:
    return JoinRoomRuntime(
        room_id="room-1",
        request_id=request_id,
        participant_id=participant_id,
        actor_type=ActorType.GUEST,
        session_digest=digest("f"),
        expected_state_version=expected_state_version,
    )
