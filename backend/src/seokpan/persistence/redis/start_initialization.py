"""Opt-in initializer using existing Redis Vote decoding and current-snapshot reads."""

from redis.exceptions import RedisError

from seokpan.persistence.redis.common import (
    LuaScriptRunner,
    RedisClient,
    RedisKeyspace,
    RedisProviderError,
)
from seokpan.persistence.redis.start_capture_script import start_intent_key, start_phase_key
from seokpan.persistence.redis.start_initialization_script import VOTE_START_INITIALIZE
from seokpan.persistence.redis.vote_adapter import RedisVoteRuntimeAdapter
from seokpan.room.application.start_intent import RoomGameStartIntent
from seokpan.vote.application.runtime import VoteMutationResult
from seokpan.vote.application.start_initialization import InitializeCapturedGame
from seokpan.vote.domain import VoteRuleViolation


class RedisCapturedVoteInitializer:
    def __init__(self, client: RedisClient, votes: RedisVoteRuntimeAdapter) -> None:
        self._client = client
        self._scripts = LuaScriptRunner(client)
        self._votes = votes

    async def get_phase(self, intent: RoomGameStartIntent) -> str | None:
        try:
            raw = await self._client.get(start_phase_key(intent.room_id, intent.game_id))
        except RedisError as error:
            raise RedisProviderError() from error
        if raw is None:
            return None
        try:
            value = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        except UnicodeDecodeError as error:
            raise RedisProviderError("START_INTENT_INVALID") from error
        if not isinstance(value, str):
            raise RedisProviderError("START_INTENT_INVALID")
        return value

    async def initialize(self, command: InitializeCapturedGame) -> VoteMutationResult:
        intent = command.intent
        room_id = intent.room_id
        keys: tuple[str, ...] = (
            RedisKeyspace.room_meta(room_id),
            RedisKeyspace.room_participants(room_id),
            RedisKeyspace.room_closed(room_id),
            start_intent_key(room_id, intent.game_id),
            start_phase_key(room_id, intent.game_id),
            RedisKeyspace.room_game(room_id),
            RedisKeyspace.room_board(room_id),
            RedisKeyspace.room_votes(room_id, 1),
            RedisKeyspace.room_vote_tally(room_id, 1),
            RedisKeyspace.room_resolver(room_id, 1),
            RedisKeyspace.room_votes(room_id, 2),
            RedisKeyspace.room_vote_tally(room_id, 2),
            RedisKeyspace.room_resolver(room_id, 2),
        )
        if intent.previous_turn_no is not None:
            keys += (
                RedisKeyspace.room_votes(room_id, intent.previous_turn_no),
                RedisKeyspace.room_vote_tally(room_id, intent.previous_turn_no),
                RedisKeyspace.room_resolver(room_id, intent.previous_turn_no),
            )
        raw = await self._scripts.execute(
            VOTE_START_INITIALIZE,
            keys=keys,
            args=(
                intent.to_json(),
                command.actor_id,
                command.expected_room_version,
                intent.fingerprint,
            ),
        )
        result = self._votes._result(raw)
        self._votes._raise_rejection(result)
        if result.get("replayed") is True:
            # Re-read the live turn, not the initial response cached at first creation.
            snapshot = await self._votes.get(room_id)
            if snapshot is None or snapshot.game_id != intent.game_id:
                raise VoteRuleViolation("GAME_START_RECOVERY_REQUIRED")
            return VoteMutationResult(snapshot, replayed=True)
        return self._votes._mutation_result(result)
