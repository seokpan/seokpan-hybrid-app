"""Fenced Session admission using the existing single Redis authority.

All participating writes must use this adapter. Redis Cluster is not supported:
Room keys and the Session lease live in different hash slots. The lease is not
membership; Room connections remain the authority and are re-read on every try.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from uuid import uuid4

from seokpan.identity.application.session import SessionRuleViolation, validate_digest
from seokpan.persistence.redis.common import RedisKeyspace, VersionedJsonCodec, VersionedLuaScript
from seokpan.persistence.redis.room_adapter import RedisRoomRuntimeAdapter
from seokpan.persistence.redis.room_scripts import ROOM_MUTATION
from seokpan.room.application.runtime import (
    ROOM_CLOSED_TOMBSTONE_TTL_MS,
    ROOM_DISCONNECT_LEASE_MS,
    ROOM_REQUEST_DEDUPE_TTL_MS,
    ROOM_RUNTIME_SCHEMA_VERSION,
    RoomMutationResult,
)
from seokpan.room.domain import RoomRuleViolation

_LOGGER = logging.getLogger(__name__)
_ADMISSIONS = frozenset({"create", "join", "change_identity", "connect"})
ADMISSION_LEASE_MS = 10_000

ACQUIRE_ADMISSION = VersionedLuaScript(
    "room-session-admission-acquire",
    1,
    "return redis.call('SET', KEYS[1], ARGV[1], 'NX', 'PX', ARGV[2]) and 1 or 0",
)
RELEASE_ADMISSION = VersionedLuaScript(
    "room-session-admission-release",
    1,
    """if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
""",
)

# Prepend to the actual current Room script; do not fork its Domain transitions.
# The token is ARGV[8], outside payload ARGV[7], so idempotency fingerprints stay
# stable across retries acquiring different leases. There is no write before the
# final lease and Session checks, not even request-cache expiry pruning.
_ADMISSION_FENCE = r"""
local function admission_rejection(code)
  return cjson.encode({ok=false, error=code})
end
if #KEYS ~= 12 or #ARGV ~= 8 then
  return admission_rejection('ROOM_ADMISSION_EXPIRED')
end
local op = ARGV[2]
if op ~= 'create' and op ~= 'join' and op ~= 'change_identity' and op ~= 'connect' then
  return admission_rejection('ROOM_ADMISSION_EXPIRED')
end
local token = ARGV[8]
if type(token) ~= 'string' or #token ~= 32
    or redis.call('GET', KEYS[11]) ~= token then
  return admission_rejection('ROOM_ADMISSION_EXPIRED')
end
local input = cjson.decode(ARGV[7])
local digest = input.session_digest
if type(digest) ~= 'string' or #digest ~= 64 or string.find(digest, '[^0-9a-f]')
    or KEYS[11] ~= 'stone:v1:room-admission:' .. digest
    or KEYS[12] ~= 'stone:v1:session:' .. digest then
  return admission_rejection('ROOM_ADMISSION_EXPIRED')
end
local raw_session = redis.call('GET', KEYS[12])
if not raw_session then return admission_rejection('SESSION_NOT_FOUND') end
local decoded, actor = pcall(cjson.decode, raw_session)
if not decoded or type(actor) ~= 'table' or actor.schema_version ~= 2
    or type(actor.actor_id) ~= 'string' or #actor.actor_id == 0
    or type(actor.absolute_expires_at_ms) ~= 'number' then
  return admission_rejection('ROOM_ADMISSION_SESSION_INVALID')
end
local now = redis.call('TIME')
local current_ms = now[1] * 1000 + math.floor(now[2] / 1000)
if actor.absolute_expires_at_ms <= current_ms then
  return admission_rejection('SESSION_NOT_FOUND')
end
local expected_actor = op == 'create' and 'MEMBER' or input.actor_type
if op == 'connect' then
  local raw_participant = redis.call('HGET', KEYS[2], input.participant_id)
  if not raw_participant then return admission_rejection('PARTICIPANT_NOT_FOUND') end
  local valid, participant = pcall(cjson.decode, raw_participant)
  if not valid or type(participant) ~= 'table' then
    return admission_rejection('ROOM_ADMISSION_SESSION_INVALID')
  end
  expected_actor = participant.actor_type
end
if (expected_actor ~= 'MEMBER' and expected_actor ~= 'GUEST')
    or actor.actor_type ~= expected_actor then
  return admission_rejection('ROOM_ADMISSION_SESSION_INVALID')
end
"""
ADMITTED_ROOM_MUTATION = VersionedLuaScript(
    "room-session-admitted-mutation",
    1,
    _ADMISSION_FENCE + ROOM_MUTATION.source,
)


def admission_key(digest: str) -> str:
    validate_digest(digest)
    return f"stone:v1:room-admission:{digest}"


class SessionAdmissionRedisRoomAdapter(RedisRoomRuntimeAdapter):
    """Serializes admission reads; the actual Room write checks the same lease."""

    async def acquire_session_admission(self, digest: str) -> str:
        key = admission_key(digest)
        token = uuid4().hex
        acquired = await self._scripts.execute(
            ACQUIRE_ADMISSION,
            keys=(key,),
            args=(token, ADMISSION_LEASE_MS),
        )
        if type(acquired) is not int or acquired != 1:
            raise SessionRuleViolation("ROOM_ADMISSION_BUSY")
        return token

    async def release_session_admission(self, digest: str, token: str) -> None:
        key = admission_key(digest)
        await self._scripts.execute(RELEASE_ADMISSION, keys=(key,), args=(token,))

    async def _mutate(
        self,
        room_id: str,
        request_id: str,
        operation: str,
        payload: Mapping[str, object],
        *,
        active_vote_turn: int | None = None,
    ) -> RoomMutationResult:
        if operation not in _ADMISSIONS:
            return await super()._mutate(
                room_id,
                request_id,
                operation,
                payload,
                active_vote_turn=active_vote_turn,
            )
        digest = payload.get("session_digest")
        participant_id = payload.get("owner_id" if operation == "create" else "participant_id")
        if not isinstance(digest, str) or not isinstance(participant_id, str):
            raise RoomRuleViolation("INVALID_PARTICIPANT_ID")
        key = admission_key(digest)
        token = await self.acquire_session_admission(digest)
        try:
            existing = await self._find_binding(session_digest=digest)
            if existing is not None and (
                existing.room_id,
                existing.participant_id,
            ) != (room_id, participant_id):
                raise RoomRuleViolation("SESSION_ALREADY_IN_ROOM")
            raw = await self._scripts.execute(
                ADMITTED_ROOM_MUTATION,
                keys=(
                    *self._mutation_keys(room_id, active_vote_turn),
                    key,
                    RedisKeyspace.session(digest),
                ),
                args=(
                    room_id,
                    operation,
                    request_id,
                    ROOM_REQUEST_DEDUPE_TTL_MS,
                    ROOM_DISCONNECT_LEASE_MS,
                    ROOM_CLOSED_TOMBSTONE_TTL_MS,
                    VersionedJsonCodec.encode(
                        {**payload, "schema_version": ROOM_RUNTIME_SCHEMA_VERSION}
                    ),
                    token,
                ),
            )
            decoded = self._result(raw)
            if decoded.get("ok") is False and decoded.get("error") in {
                "ROOM_ADMISSION_EXPIRED",
                "ROOM_ADMISSION_SESSION_INVALID",
                "SESSION_NOT_FOUND",
            }:
                raise SessionRuleViolation(str(decoded["error"]))
            self._raise_rejection(decoded)
            return self._mutation_result(decoded)
        finally:
            try:
                # A late owner must never remove a successor's lease. If a
                # queued Room write arrives after release, its fence rejects it.
                await self.release_session_admission(digest, token)
            except Exception:
                # The lease expires; do not turn a committed admission into an
                # error or hide the original failure. Cancellation propagates.
                _LOGGER.warning("Room admission lease release failed")
