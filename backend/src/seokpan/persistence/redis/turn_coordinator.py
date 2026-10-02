"""Production due-turn discovery and durable tie selection coordination."""

from __future__ import annotations

import hashlib
import secrets

from seokpan.game.application import (
    DueTurn,
    GamePersistencePort,
    TieSelectionRecord,
    TurnFinalizationApproval,
)
from seokpan.persistence.redis.common import (
    LuaScriptRunner,
    RedisClient,
    RedisKeyspace,
    RedisProviderError,
    VersionedJsonCodec,
    VersionedLuaScript,
)
from seokpan.room.application import LobbyRoomRuntimePort
from seokpan.room.domain import RoomStatus
from seokpan.vote.application import VoteRuntimePort, VoteRuntimeSnapshot

_TIE_SELECTION = VersionedLuaScript(
    "tie-selection",
    1,
    r"""
local existing = redis.call('GET', KEYS[1])
if existing then
  local value = cjson.decode(existing)
  if value.fingerprint ~= ARGV[1] then
    return cjson.encode({ok = false, error = 'TIE_SELECTION_CONFLICT'})
  end
  return cjson.encode({ok = true, selected = value.selected})
end
redis.call('SET', KEYS[1], cjson.encode({fingerprint = ARGV[1], selected = ARGV[2]}),
           'PX', ARGV[3], 'NX')
local value = cjson.decode(redis.call('GET', KEYS[1]))
return cjson.encode({ok = true, selected = value.selected})
""",
)


class RedisTurnCoordinator:
    def __init__(
        self,
        client: RedisClient,
        rooms: LobbyRoomRuntimePort,
        votes: VoteRuntimePort,
        games: GamePersistencePort,
    ) -> None:
        self._scripts = LuaScriptRunner(client)
        self._rooms = rooms
        self._votes = votes
        self._games = games

    async def due_turns(self, *, now_ms: int, limit: int) -> tuple[DueTurn, ...]:
        if limit < 1:
            raise ValueError("INVALID_DUE_TURN_LIMIT")
        result: list[DueTurn] = []
        for room in await self._rooms.list_rooms():
            if room.status is not RoomStatus.PLAYING or room.game_id is None:
                continue
            vote = await self._votes.get(room.room_id)
            if (
                vote is not None
                and vote.game_id == room.game_id
                and (vote.deadline_ms is None or vote.deadline_ms <= now_ms)
            ):
                result.append(DueTurn(room.room_id, vote.game_id, vote.turn_no))
            if len(result) == limit:
                break
        return tuple(result)

    async def assess(
        self, *, due_turn: DueTurn, snapshot: VoteRuntimeSnapshot
    ) -> TurnFinalizationApproval:
        existing = await self._games.get_move(due_turn.game_id, due_turn.turn_no)
        if existing is None or existing.coordinate in snapshot.candidates:
            return TurnFinalizationApproval.ALLOWED
        return TurnFinalizationApproval.RECOVERY_REQUIRED

    async def select(self, *, game_id: str, turn_no: int, candidates: tuple[str, ...]) -> str:
        if len(candidates) < 2 or len(set(candidates)) != len(candidates):
            raise ValueError("TIE_CANDIDATES_REQUIRED")
        fingerprint = hashlib.sha256(
            "\n".join(candidates).encode("utf-8"), usedforsecurity=False
        ).hexdigest()
        selected = secrets.choice(candidates)
        result = await self._scripts.execute(
            _TIE_SELECTION,
            keys=(RedisKeyspace.tie_selection(game_id, turn_no),),
            args=(fingerprint, selected, 7 * 24 * 60 * 60 * 1000),
        )
        decoded = VersionedJsonCodec.decode(result) if isinstance(result, (bytes, str)) else None
        if decoded is None or decoded.get("ok") is not True:
            raise RedisProviderError("TIE_SELECTION_CONFLICT")
        value = decoded.get("selected")
        if not isinstance(value, str) or value not in candidates:
            raise RedisProviderError("REDIS_RESPONSE_INVALID")
        return value

    async def record(self, value: TieSelectionRecord) -> None:
        selected = await self.select(
            game_id=value.game_id,
            turn_no=value.turn_no,
            candidates=value.candidates,
        )
        if selected != value.selected_coordinate:
            raise RedisProviderError("TIE_SELECTION_CONFLICT")
