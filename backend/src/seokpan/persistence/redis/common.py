"""Shared Redis key, JSON and versioned Lua boundaries."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, cast

from redis.exceptions import NoScriptError, RedisError

from seokpan.identity.application.session import SessionActorType, SessionRuleViolation

REDIS_KEY_PREFIX = "stone:v1:"


class RedisProviderError(RuntimeError):
    """A sanitized provider failure which never includes keys or credentials."""

    def __init__(self, code: str = "REDIS_PROVIDER_UNAVAILABLE") -> None:
        self.code = code
        super().__init__(code)


class RedisClient(Protocol):
    async def get(self, key: str) -> bytes | str | None: ...

    def scan_iter(self, *, match: str, count: int) -> AsyncIterator[bytes | str]: ...

    async def hgetall(self, key: str) -> Mapping[bytes | str, bytes | str]: ...

    async def evalsha(
        self,
        sha: str,
        numkeys: int,
        *keys_and_args: object,
    ) -> object: ...

    async def script_load(self, script: str) -> bytes | str: ...


class RedisKeyspace:
    @staticmethod
    def session(session_digest: str) -> str:
        return f"{REDIS_KEY_PREFIX}session:{session_digest}"

    @staticmethod
    def member_sessions(member_id: str) -> str:
        if not member_id.isdecimal():
            raise SessionRuleViolation("INVALID_MEMBER_ID")
        return f"{REDIS_KEY_PREFIX}identity:member:{member_id}:sessions"

    @staticmethod
    def no_member_index() -> str:
        """An unused declared key keeps each script signature stable."""
        return f"{REDIS_KEY_PREFIX}identity:none:sessions"

    @classmethod
    def session_index(cls, actor_type: SessionActorType, actor_id: str) -> str:
        if actor_type is SessionActorType.MEMBER:
            return cls.member_sessions(actor_id)
        return cls.no_member_index()

    @staticmethod
    def room_meta(room_id: str) -> str:
        return f"{REDIS_KEY_PREFIX}room:{{{room_id}}}:meta"

    @staticmethod
    def room_participants(room_id: str) -> str:
        return f"{REDIS_KEY_PREFIX}room:{{{room_id}}}:participants"

    @staticmethod
    def room_ready(room_id: str) -> str:
        return f"{REDIS_KEY_PREFIX}room:{{{room_id}}}:ready"

    @staticmethod
    def room_connections(room_id: str) -> str:
        return f"{REDIS_KEY_PREFIX}room:{{{room_id}}}:connections"

    @staticmethod
    def room_requests(room_id: str) -> str:
        return f"{REDIS_KEY_PREFIX}room:{{{room_id}}}:requests"

    @staticmethod
    def room_request_expiries(room_id: str) -> str:
        return f"{REDIS_KEY_PREFIX}room:{{{room_id}}}:request-expiries"

    @staticmethod
    def room_closed(room_id: str) -> str:
        return f"{REDIS_KEY_PREFIX}room:{{{room_id}}}:closed"

    @staticmethod
    def room_votes(room_id: str, turn_no: int | None) -> str:
        suffix = "none" if turn_no is None else str(turn_no)
        return f"{REDIS_KEY_PREFIX}room:{{{room_id}}}:votes:{suffix}"

    @staticmethod
    def room_game(room_id: str) -> str:
        return f"{REDIS_KEY_PREFIX}room:{{{room_id}}}:game"

    @staticmethod
    def room_board(room_id: str) -> str:
        return f"{REDIS_KEY_PREFIX}room:{{{room_id}}}:board"

    @staticmethod
    def room_vote_tally(room_id: str, turn_no: int | None) -> str:
        suffix = "none" if turn_no is None else str(turn_no)
        return f"{REDIS_KEY_PREFIX}room:{{{room_id}}}:vote-tally:{suffix}"

    @staticmethod
    def room_resolver(room_id: str, turn_no: int) -> str:
        return f"{REDIS_KEY_PREFIX}room:{{{room_id}}}:resolver:{turn_no}"

    @staticmethod
    def presence_bindings() -> str:
        return f"{REDIS_KEY_PREFIX}presence:bindings"

    @staticmethod
    def presence_expiries() -> str:
        return f"{REDIS_KEY_PREFIX}presence:expiries"

    @staticmethod
    def chat_receipts(scope: str) -> str:
        return f"{REDIS_KEY_PREFIX}chat:{{{scope}}}:receipts"

    @staticmethod
    def chat_receipt_expiries(scope: str) -> str:
        return f"{REDIS_KEY_PREFIX}chat:{{{scope}}}:receipt-expiries"

    @staticmethod
    def chat_channel(scope: str) -> str:
        return f"{REDIS_KEY_PREFIX}chat:{{{scope}}}:messages"

    @staticmethod
    def realtime_version(scope: str) -> str:
        return f"{REDIS_KEY_PREFIX}realtime:{{{scope}}}:version"

    @staticmethod
    def realtime_events(scope: str) -> str:
        return f"{REDIS_KEY_PREFIX}realtime:{{{scope}}}:events"

    @staticmethod
    def realtime_event_expiries(scope: str) -> str:
        return f"{REDIS_KEY_PREFIX}realtime:{{{scope}}}:event-expiries"

    @staticmethod
    def realtime_channel(scope: str) -> str:
        return f"{REDIS_KEY_PREFIX}realtime:{{{scope}}}:channel"

    @staticmethod
    def tie_selection(game_id: str, turn_no: int) -> str:
        return f"{REDIS_KEY_PREFIX}game:{{{game_id}}}:tie:{turn_no}"


class VersionedJsonCodec:
    @staticmethod
    def encode(value: Mapping[str, object]) -> str:
        return json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def decode(value: bytes | str) -> dict[str, object]:
        try:
            text = value.decode("utf-8") if isinstance(value, bytes) else value
            decoded = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RedisProviderError("REDIS_RESPONSE_INVALID") from error
        if not isinstance(decoded, dict):
            raise RedisProviderError("REDIS_RESPONSE_INVALID")
        return cast(dict[str, object], decoded)


@dataclass(frozen=True, slots=True)
class VersionedLuaScript:
    name: str
    version: int
    source: str

    @property
    def sha(self) -> str:
        return hashlib.sha1(self.source.encode("utf-8"), usedforsecurity=False).hexdigest()


class LuaScriptRunner:
    def __init__(self, client: RedisClient) -> None:
        self._client = client

    async def execute(
        self,
        script: VersionedLuaScript,
        *,
        keys: Sequence[str],
        args: Sequence[object],
    ) -> object:
        try:
            return await self._client.evalsha(script.sha, len(keys), *keys, *args)
        except NoScriptError:
            try:
                loaded_sha = await self._client.script_load(script.source)
                normalized_sha = (
                    loaded_sha.decode("ascii") if isinstance(loaded_sha, bytes) else loaded_sha
                )
                if normalized_sha != script.sha:
                    raise RedisProviderError("REDIS_SCRIPT_DIGEST_MISMATCH")
                return await self._client.evalsha(script.sha, len(keys), *keys, *args)
            except RedisProviderError:
                raise
            except RedisError as error:
                raise RedisProviderError() from error
        except RedisError as error:
            raise RedisProviderError() from error
