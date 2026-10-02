from __future__ import annotations

from dataclasses import dataclass

import pytest
from redis.exceptions import NoScriptError

from seokpan.game.domain import AppliedMove, Coordinate, EndReason, Stone
from seokpan.persistence.memory import InMemoryVoteRuntimeAdapter, ManualClock
from seokpan.persistence.redis.common import VersionedJsonCodec
from seokpan.persistence.redis.vote_adapter import RedisVoteRuntimeAdapter
from seokpan.persistence.redis.vote_scripts import VOTE_DISCARD, VOTE_MUTATION, VOTE_READ
from seokpan.vote.application import (
    AcquireRuntimeResolver,
    ApplyRuntimeResolution,
    CastRuntimeVote,
    CloseRuntimeTurn,
    FinalizeRuntimeGame,
    InitializeVoteRuntime,
    RemoveRuntimeVote,
    VoteMutationResult,
    VoteRuntimePort,
    VoteRuntimeSnapshot,
)
from seokpan.vote.domain import (
    ParticipantRole,
    TurnClosure,
    TurnResolution,
    TurnResultKind,
    TurnStatus,
    Voter,
    VoteRuleViolation,
)


@dataclass(slots=True)
class VoteRuntimeHarness:
    adapter: VoteRuntimePort
    clock: ManualClock
    observable: InMemoryVoteRuntimeAdapter


class EmulatedVoteRedisClient:
    """Lua command-boundary emulator; it is not actual Redis evidence."""

    def __init__(self, clock: ManualClock, *, scripts_loaded: bool = True) -> None:
        self.store = InMemoryVoteRuntimeAdapter(clock)
        self.loaded = (
            {VOTE_DISCARD.sha, VOTE_MUTATION.sha, VOTE_READ.sha} if scripts_loaded else set()
        )
        self.evalsha_calls: list[tuple[str, int, tuple[object, ...]]] = []
        self.script_load_calls: list[str] = []

    async def get(self, key: str) -> bytes | None:
        room_id = self._room_id(key)
        snapshot = await self.store.get(room_id)
        if snapshot is None:
            return None
        return VersionedJsonCodec.encode(
            {
                "game_id": snapshot.game_id,
                "turn_no": snapshot.turn_no,
                "schema_version": snapshot.schema_version,
            }
        ).encode()

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
        if sha == VOTE_READ.sha:
            return self._encode(
                {"ok": True, "error": None, "snapshot": _snapshot(await self.store.get(room_id))}
            )
        if sha == VOTE_DISCARD.sha:
            current = await self.store.get(room_id)
            if current is None:
                return self._encode({"ok": True, "missing": True, "error": None})
            if current.game_id != str(args[0]):
                return self._encode({"ok": False, "error": "STALE_GAME"})
            if current.turn_no != int(str(args[1])):
                return self._encode({"ok": False, "error": "REDIS_SNAPSHOT_CHANGED"})
            await self.store.discard_game(room_id, current.game_id)
            return self._encode({"ok": True, "missing": False, "error": None})
        if sha != VOTE_MUTATION.sha:
            raise AssertionError("unknown script")
        operation = str(args[0])
        request_id = str(args[1])
        payload = VersionedJsonCodec.decode(str(args[4]))
        try:
            result = await self._mutate(room_id, request_id, operation, payload)
        except VoteRuleViolation as error:
            return self._encode({"ok": False, "error": error.code})
        return self._encode(_result(result))

    async def script_load(self, script: str) -> str:
        self.script_load_calls.append(script)
        for candidate in (VOTE_DISCARD, VOTE_MUTATION, VOTE_READ):
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
    ) -> VoteMutationResult:
        game_id = str(payload["game_id"])
        expected = int(str(payload["expected_state_version"]))
        if operation == "initialize":
            return await self.store.initialize(
                InitializeVoteRuntime(
                    room_id=room_id,
                    request_id=request_id,
                    game_id=game_id,
                    participants=tuple(_voter(item) for item in _list(payload["participants"])),
                    deadline_ms=int(str(payload["deadline_ms"])),
                    expected_state_version=expected,
                    previous_game_id=(
                        None
                        if payload.get("previous_game_id") is None
                        else str(payload["previous_game_id"])
                    ),
                    previous_turn_no=(
                        None
                        if payload.get("previous_turn_no") is None
                        else int(str(payload["previous_turn_no"]))
                    ),
                )
            )
        turn_no = int(str(payload["turn_no"]))
        if operation == "cast_vote":
            return await self.store.cast_vote(
                CastRuntimeVote(
                    room_id,
                    request_id,
                    game_id,
                    turn_no,
                    str(payload["participant_id"]),
                    Coordinate.parse(str(payload["coordinate"])),
                    expected,
                )
            )
        if operation == "remove_vote":
            return await self.store.remove_vote(
                RemoveRuntimeVote(
                    room_id,
                    request_id,
                    game_id,
                    turn_no,
                    str(payload["participant_id"]),
                    expected,
                )
            )
        if operation == "close_turn":
            deadline = payload.get("next_deadline_ms")
            return await self.store.close_turn(
                CloseRuntimeTurn(
                    room_id,
                    request_id,
                    game_id,
                    turn_no,
                    expected,
                    next_deadline_ms=None if deadline is None else int(str(deadline)),
                )
            )
        if operation == "acquire_resolver":
            return await self.store.acquire_resolver(
                AcquireRuntimeResolver(
                    room_id,
                    request_id,
                    game_id,
                    turn_no,
                    str(payload["resolution_id"]),
                    expected,
                )
            )
        if operation == "apply_resolution":
            deadline = payload.get("next_deadline_ms")
            return await self.store.apply_resolution(
                ApplyRuntimeResolution(
                    room_id=room_id,
                    request_id=request_id,
                    game_id=game_id,
                    turn_no=turn_no,
                    resolution_id=str(payload["resolution_id"]),
                    resolution=_resolution(payload["resolution"]),
                    expected_state_version=expected,
                    persistence_confirmed=bool(payload["persistence_confirmed"]),
                    next_deadline_ms=None if deadline is None else int(str(deadline)),
                )
            )
        if operation == "finalize_game":
            return await self.store.finalize_game(
                FinalizeRuntimeGame(
                    room_id=room_id,
                    request_id=request_id,
                    game_id=game_id,
                    turn_no=turn_no,
                    expected_state_version=expected,
                    end_reason=EndReason(str(payload["end_reason"])),
                    winner=Stone(str(payload["winner"])),
                )
            )
        raise AssertionError(f"unexpected operation: {operation}")

    @staticmethod
    def _room_id(key: str) -> str:
        return key.split("{", 1)[1].split("}", 1)[0]

    @staticmethod
    def _encode(value: dict[str, object]) -> bytes:
        return VersionedJsonCodec.encode(value).encode()


def _list(value: object) -> list[object]:
    assert isinstance(value, list)
    return value


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value


def _voter(value: object) -> Voter:
    item = _mapping(value)
    return Voter(
        str(item["participant_id"]),
        Stone(str(item["team"])),
        ParticipantRole(str(item["role"])),
        bool(item["connected"]),
    )


def _snapshot(value: VoteRuntimeSnapshot | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "schema_version": value.schema_version,
        "room_id": value.room_id,
        "game_id": value.game_id,
        "state_version": value.state_version,
        "turn_no": value.turn_no,
        "turn_status": value.turn_status.value,
        "current_team": value.current_team.value,
        "deadline_ms": value.deadline_ms,
        "consecutive_passes": value.consecutive_passes,
        "move_no": value.move_no,
        "last_move": None
        if value.last_move is None
        else {
            "move_no": value.last_move.move_no,
            "team": value.last_move.team.value,
            "coordinate": value.last_move.coordinate.canonical,
        },
        "game_status": value.game_status.value,
        "end_reason": None if value.end_reason is None else value.end_reason.value,
        "participants": [
            {
                "participant_id": item.participant_id,
                "team": item.team.value,
                "role": item.role.value,
                "connected": item.connected,
            }
            for item in value.participants
        ],
        "votes": [
            {"participant_id": item.participant_id, "coordinate": item.coordinate.canonical}
            for item in value.votes
        ],
        "tally": [
            {"coordinate": item.coordinate.canonical, "count": item.count} for item in value.tally
        ],
        "candidates": [item.canonical for item in value.candidates],
        "occupied_cells": [
            {"coordinate": item.coordinate.canonical, "stone": item.stone.value}
            for item in value.occupied_cells
        ],
        "resolver": (
            None
            if value.resolver is None
            else {
                "resolution_id": value.resolver.resolution_id,
                "expires_at_ms": value.resolver.expires_at_ms,
            }
        ),
    }


def _closure(value: TurnClosure | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "game_id": value.game_id,
        "turn_no": value.turn_no,
        "team": value.team.value,
        "result": value.result.value,
        "status": value.status.value,
        "tally": [
            {"coordinate": item.coordinate.canonical, "count": item.count} for item in value.tally
        ],
        "candidates": [item.canonical for item in value.candidates],
    }


def _resolution_value(value: TurnResolution | None) -> dict[str, object] | None:
    if value is None:
        return None
    return RedisVoteRuntimeAdapter._resolution_value(value)


def _result(value: VoteMutationResult) -> dict[str, object]:
    return {
        "ok": True,
        "error": None,
        "snapshot": _snapshot(value.snapshot),
        "replayed": value.replayed,
        "closure": _closure(value.closure),
        "resolution": _resolution_value(value.resolution),
        "valid_voter_count": value.valid_voter_count,
    }


def _resolution(value: object) -> TurnResolution:
    item = _mapping(value)
    selected = item.get("selected_coordinate")
    move_value = item.get("applied_move")
    move = None
    if move_value is not None:
        move_item = _mapping(move_value)
        move = AppliedMove(
            int(str(move_item["move_no"])),
            Stone(str(move_item["team"])),
            Coordinate.parse(str(move_item["coordinate"])),
        )
    end_reason = item.get("end_reason")
    return TurnResolution(
        game_id=str(item["game_id"]),
        turn_no=int(str(item["turn_no"])),
        team=Stone(str(item["team"])),
        result=TurnResultKind(str(item["result"])),
        status=TurnStatus(str(item["status"])),
        selected_coordinate=None if selected is None else Coordinate.parse(str(selected)),
        applied_move=move,
        end_reason=None if end_reason is None else EndReason(str(end_reason)),
    )


@pytest.fixture(params=("memory", "redis"))
def vote_harness(request: pytest.FixtureRequest) -> VoteRuntimeHarness:
    clock = ManualClock()
    if request.param == "memory":
        adapter = InMemoryVoteRuntimeAdapter(clock)
        return VoteRuntimeHarness(adapter, clock, adapter)
    client = EmulatedVoteRedisClient(clock)
    return VoteRuntimeHarness(RedisVoteRuntimeAdapter(client), clock, client.store)
