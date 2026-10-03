"""Versioned Lua sources for atomic Session lifecycle transitions."""

from seokpan.persistence.redis.common import VersionedLuaScript

_COMMON = """
local function now_ms()
  local current = redis.call('TIME')
  return current[1] * 1000 + math.floor(current[2] / 1000)
end

local function member_index(actor_type, actor_id)
  if actor_type ~= 'MEMBER' then
    return nil
  end
  return 'stone:v1:identity:member:' .. actor_id .. ':sessions'
end

local function refresh_member_index(index_key, current_ms)
  if not index_key then return end
  redis.call('ZREMRANGEBYSCORE', index_key, '-inf', current_ms)
  local latest = redis.call('ZREVRANGE', index_key, 0, 0, 'WITHSCORES')
  if #latest == 0 then
    redis.call('DEL', index_key)
  else
    redis.call('PEXPIREAT', index_key, tonumber(latest[2]))
  end
end

local function response(ok, session, error_code)
  return cjson.encode({ok = ok, session = session, error = error_code})
end

local function valid_session(session)
  return type(session) == 'table' and session.schema_version == 2
    and type(session.csrf_token) == 'string' and #session.csrf_token > 0
    and type(session.csrf_digest) == 'string' and #session.csrf_digest == 64
    and type(session.actor_id) == 'string'
    and (session.actor_type == 'MEMBER' or session.actor_type == 'GUEST')
    and type(session.created_at_ms) == 'number'
    and type(session.last_activity_at_ms) == 'number'
    and type(session.absolute_expires_at_ms) == 'number'
    and session.created_at_ms <= session.last_activity_at_ms
    and session.last_activity_at_ms < session.absolute_expires_at_ms
end
"""

CREATE_SESSION = VersionedLuaScript(
    name="session-create",
    version=2,
    source=_COMMON
    + """
local session_key = KEYS[1]
local index_key = KEYS[2]
local digest = ARGV[1]
local actor_type = ARGV[2]
local actor_id = ARGV[3]
local csrf_digest = ARGV[4]
local schema_version = tonumber(ARGV[5])
local idle_ttl_ms = tonumber(ARGV[6])
local absolute_ttl_ms = tonumber(ARGV[7])
local csrf_token = ARGV[8]

if redis.call('EXISTS', session_key) == 1 then
  return response(false, cjson.null, 'SESSION_ALREADY_EXISTS')
end

local current_ms = now_ms()
local absolute_expires_at_ms = current_ms + absolute_ttl_ms
local idle_expires_at_ms = math.min(current_ms + idle_ttl_ms, absolute_expires_at_ms)
local session = {
  schema_version = schema_version,
  actor_type = actor_type,
  actor_id = actor_id,
  csrf_digest = csrf_digest,
  csrf_token = csrf_token,
  created_at_ms = current_ms,
  last_activity_at_ms = current_ms,
  absolute_expires_at_ms = absolute_expires_at_ms
}
redis.call('SET', session_key, cjson.encode(session), 'PXAT', idle_expires_at_ms)

if actor_type == 'MEMBER' then
  redis.call('ZADD', index_key, idle_expires_at_ms, digest)
  refresh_member_index(index_key, current_ms)
end

return response(true, session, cjson.null)
""",
)

TOUCH_SESSION = VersionedLuaScript(
    name="session-touch",
    version=3,
    source=_COMMON
    + """
local session_key = KEYS[1]
local digest = ARGV[1]
local idle_ttl_ms = tonumber(ARGV[2])
local raw = redis.call('GET', session_key)
if not raw then
  return response(true, cjson.null, cjson.null)
end
if raw ~= ARGV[3] then
  return response(false, cjson.null, 'SESSION_STATE_CHANGED')
end

local session = cjson.decode(raw)
if not valid_session(session) then
  return response(false, cjson.null, 'UNSUPPORTED_SESSION_SCHEMA')
end
local current_ms = now_ms()
if current_ms >= session.absolute_expires_at_ms then
  redis.call('DEL', session_key)
  local index_key = member_index(session.actor_type, session.actor_id)
  if index_key then
    redis.call('ZREM', index_key, digest)
    refresh_member_index(index_key, current_ms)
  end
  return response(true, cjson.null, cjson.null)
end

session.last_activity_at_ms = current_ms
local remaining_ms = session.absolute_expires_at_ms - current_ms
local ttl_ms = math.min(idle_ttl_ms, remaining_ms)
redis.call('SET', session_key, cjson.encode(session), 'PX', ttl_ms)
local index_key = member_index(session.actor_type, session.actor_id)
if index_key then
  redis.call('ZADD', index_key, current_ms + ttl_ms, digest)
  refresh_member_index(index_key, current_ms)
end
return response(true, session, cjson.null)
""",
)

ROTATE_SESSION = VersionedLuaScript(
    name="session-rotate",
    version=3,
    source=_COMMON
    + """
local previous_key = KEYS[1]
local replacement_key = KEYS[2]
local replacement_index_key = KEYS[3]
local previous_digest = ARGV[1]
local replacement_digest = ARGV[2]
local actor_type = ARGV[3]
local actor_id = ARGV[4]
local csrf_digest = ARGV[5]
local schema_version = tonumber(ARGV[6])
local idle_ttl_ms = tonumber(ARGV[7])
local absolute_ttl_ms = tonumber(ARGV[8])
local csrf_token = ARGV[9]

if previous_key == replacement_key then
  return response(false, cjson.null, 'SESSION_ROTATION_REQUIRES_NEW_DIGEST')
end
local previous_raw = redis.call('GET', previous_key)
if not previous_raw then
  return response(false, cjson.null, 'SESSION_NOT_FOUND')
end
if previous_raw ~= ARGV[10] then
  return response(false, cjson.null, 'SESSION_STATE_CHANGED')
end
if redis.call('EXISTS', replacement_key) == 1 then
  return response(false, cjson.null, 'SESSION_ALREADY_EXISTS')
end

local previous = cjson.decode(previous_raw)
if not valid_session(previous) then
  return response(false, cjson.null, 'UNSUPPORTED_SESSION_SCHEMA')
end
local previous_index_key = member_index(previous.actor_type, previous.actor_id)
local current_ms = now_ms()
if current_ms >= previous.absolute_expires_at_ms then
  return response(false, cjson.null, 'SESSION_NOT_FOUND')
end
redis.call('DEL', previous_key)
if previous_index_key then
  redis.call('ZREM', previous_index_key, previous_digest)
  refresh_member_index(previous_index_key, current_ms)
end

local absolute_expires_at_ms = current_ms + absolute_ttl_ms
local idle_expires_at_ms = math.min(current_ms + idle_ttl_ms, absolute_expires_at_ms)
local replacement = {
  schema_version = schema_version,
  actor_type = actor_type,
  actor_id = actor_id,
  csrf_digest = csrf_digest,
  csrf_token = csrf_token,
  created_at_ms = current_ms,
  last_activity_at_ms = current_ms,
  absolute_expires_at_ms = absolute_expires_at_ms
}
redis.call('SET', replacement_key, cjson.encode(replacement), 'PXAT', idle_expires_at_ms)
if actor_type == 'MEMBER' then
  redis.call('ZADD', replacement_index_key, idle_expires_at_ms, replacement_digest)
  refresh_member_index(replacement_index_key, current_ms)
end
return response(true, replacement, cjson.null)
""",
)

RESTORE_SESSION = VersionedLuaScript(
    name="session-restore-after-failed-rotation",
    version=3,
    source=_COMMON
    + """
local failed_key = KEYS[1]
local previous_key = KEYS[2]
local previous_index_key = KEYS[3]
local failed_digest = ARGV[1]
local previous_digest = ARGV[2]
local previous_actor_type = ARGV[3]
local previous_actor_id = ARGV[4]
local previous_csrf_digest = ARGV[5]
local previous_schema_version = tonumber(ARGV[6])
local previous_created_at_ms = tonumber(ARGV[7])
local previous_last_activity_at_ms = tonumber(ARGV[8])
local previous_absolute_expires_at_ms = tonumber(ARGV[9])
local idle_ttl_ms = tonumber(ARGV[10])
local previous_csrf_token = ARGV[11]

local failed_raw = redis.call('GET', failed_key)
if not failed_raw then
  return response(false, cjson.null, 'SESSION_NOT_FOUND')
end
if failed_raw ~= ARGV[12] then
  return response(false, cjson.null, 'SESSION_STATE_CHANGED')
end
if redis.call('EXISTS', previous_key) == 1 then
  return response(false, cjson.null, 'SESSION_ALREADY_EXISTS')
end

local current_ms = now_ms()
local idle_expires_at_ms = math.min(
  previous_last_activity_at_ms + idle_ttl_ms,
  previous_absolute_expires_at_ms
)
local failed = cjson.decode(failed_raw)
if not valid_session(failed) or previous_schema_version ~= 2 then
  return response(false, cjson.null, 'UNSUPPORTED_SESSION_SCHEMA')
end
local failed_index_key = member_index(failed.actor_type, failed.actor_id)
redis.call('DEL', failed_key)
if failed_index_key then
  redis.call('ZREM', failed_index_key, failed_digest)
  refresh_member_index(failed_index_key, current_ms)
end
if current_ms >= idle_expires_at_ms then
  return response(false, cjson.null, 'SESSION_ROLLBACK_EXPIRED')
end

local previous = {
  schema_version = previous_schema_version,
  actor_type = previous_actor_type,
  actor_id = previous_actor_id,
  csrf_digest = previous_csrf_digest,
  csrf_token = previous_csrf_token,
  created_at_ms = previous_created_at_ms,
  last_activity_at_ms = previous_last_activity_at_ms,
  absolute_expires_at_ms = previous_absolute_expires_at_ms
}
redis.call('SET', previous_key, cjson.encode(previous), 'PXAT', idle_expires_at_ms)
if previous_actor_type == 'MEMBER' then
  redis.call('ZADD', previous_index_key, idle_expires_at_ms, previous_digest)
  refresh_member_index(previous_index_key, current_ms)
end
return response(true, previous, cjson.null)
""",
)

REVOKE_SESSION = VersionedLuaScript(
    name="session-revoke",
    version=3,
    source=_COMMON
    + """
local session_key = KEYS[1]
local digest = ARGV[1]
local raw = redis.call('GET', session_key)
if not raw then
  return cjson.encode({ok = true, revoked = false, error = cjson.null})
end
if raw ~= ARGV[2] then
  return response(false, cjson.null, 'SESSION_STATE_CHANGED')
end
local session = cjson.decode(raw)
local current_ms = now_ms()
redis.call('DEL', session_key)
local index_key = member_index(session.actor_type, session.actor_id)
if index_key then
  redis.call('ZREM', index_key, digest)
  refresh_member_index(index_key, current_ms)
end
return cjson.encode({ok = true, revoked = true, error = cjson.null})
""",
)
