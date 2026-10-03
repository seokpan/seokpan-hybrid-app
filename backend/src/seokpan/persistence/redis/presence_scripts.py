"""Atomic Redis scripts for process-independent user presence leases."""

from seokpan.persistence.redis.common import VersionedLuaScript

_COMMON = """
local function now_ms()
  local current = redis.call('TIME')
  return current[1] * 1000 + math.floor(current[2] / 1000)
end

local function cleanup(current_ms)
  local expired = redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', current_ms)
  for _, lease_id in ipairs(expired) do
    redis.call('HDEL', KEYS[1], lease_id)
    redis.call('ZREM', KEYS[2], lease_id)
  end
end

local function response(ok, value, error_code)
  return cjson.encode({ok = ok, value = value, error = error_code})
end
"""

PRESENCE_OPEN = VersionedLuaScript(
    name="presence-open",
    version=1,
    source=_COMMON
    + """
local current_ms = now_ms()
cleanup(current_ms)
local lease_id = ARGV[1]
local actor_type = ARGV[2]
local actor_id = ARGV[3]
local session_digest = ARGV[4]
local lease_ms = tonumber(ARGV[5])
local capacity = tonumber(ARGV[6])

if redis.call('HEXISTS', KEYS[1], lease_id) == 1 then
  return response(false, cjson.null, 'PRESENCE_LEASE_COLLISION')
end
if redis.call('HLEN', KEYS[1]) >= capacity then
  return response(false, cjson.null, 'PRESENCE_CAPACITY_REACHED')
end
for _, raw in ipairs(redis.call('HVALS', KEYS[1])) do
  local binding = cjson.decode(raw)
  if binding.session_digest == session_digest
    and (binding.actor_type ~= actor_type or binding.actor_id ~= actor_id) then
    return response(false, cjson.null, 'PRESENCE_SESSION_IDENTITY_MISMATCH')
  end
end
local expires_at_ms = current_ms + lease_ms
local binding = {
  lease_id = lease_id,
  actor_type = actor_type,
  actor_id = actor_id,
  session_digest = session_digest,
  expires_at_ms = expires_at_ms
}
redis.call('HSET', KEYS[1], lease_id, cjson.encode(binding))
redis.call('ZADD', KEYS[2], expires_at_ms, lease_id)
return response(true, binding, cjson.null)
""",
)

PRESENCE_RENEW = VersionedLuaScript(
    name="presence-renew",
    version=1,
    source=_COMMON
    + """
local current_ms = now_ms()
cleanup(current_ms)
local lease_id = ARGV[1]
local lease_ms = tonumber(ARGV[2])
local raw = redis.call('HGET', KEYS[1], lease_id)
if not raw then
  return response(false, cjson.null, 'PRESENCE_LEASE_ENDED')
end
local binding = cjson.decode(raw)
binding.expires_at_ms = current_ms + lease_ms
redis.call('HSET', KEYS[1], lease_id, cjson.encode(binding))
redis.call('ZADD', KEYS[2], binding.expires_at_ms, lease_id)
return response(true, binding, cjson.null)
""",
)

PRESENCE_CLOSE = VersionedLuaScript(
    name="presence-close",
    version=1,
    source=_COMMON
    + """
local current_ms = now_ms()
cleanup(current_ms)
redis.call('HDEL', KEYS[1], ARGV[1])
redis.call('ZREM', KEYS[2], ARGV[1])
return response(true, true, cjson.null)
""",
)

PRESENCE_INVALIDATE_SESSION = VersionedLuaScript(
    name="presence-invalidate-session",
    version=1,
    source=_COMMON
    + """
local current_ms = now_ms()
cleanup(current_ms)
local removed = 0
local values = redis.call('HGETALL', KEYS[1])
for index = 1, #values, 2 do
  local lease_id = values[index]
  local binding = cjson.decode(values[index + 1])
  if binding.session_digest == ARGV[1] then
    redis.call('HDEL', KEYS[1], lease_id)
    redis.call('ZREM', KEYS[2], lease_id)
    removed = removed + 1
  end
end
return response(true, removed, cjson.null)
""",
)

PRESENCE_READ = VersionedLuaScript(
    name="presence-read",
    version=1,
    source=_COMMON
    + """
local current_ms = now_ms()
cleanup(current_ms)
local values = redis.call('HVALS', KEYS[1])
local connections = {}
local identities = {}
local online_users = 0
for _, raw in ipairs(values) do
  local binding = cjson.decode(raw)
  table.insert(connections, binding)
  local identity_key = binding.actor_type .. ':' .. binding.actor_id
  if not identities[identity_key] then
    identities[identity_key] = true
    online_users = online_users + 1
  end
end
table.sort(connections, function(left, right) return left.lease_id < right.lease_id end)
return response(true, {connections = connections, online_users = online_users}, cjson.null)
""",
)
