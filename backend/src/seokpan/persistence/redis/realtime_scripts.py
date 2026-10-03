"""Atomic versioning, idempotency and Pub/Sub notification for state events."""

from seokpan.persistence.redis.common import VersionedLuaScript

PUBLISH_REALTIME = VersionedLuaScript(
    name="publish-realtime",
    version=1,
    source="""
local function response(ok, value, error_code)
  return cjson.encode({ok = ok, value = value, error = error_code})
end

local current = redis.call('TIME')
local current_ms = current[1] * 1000 + math.floor(current[2] / 1000)
local expired = redis.call('ZRANGEBYSCORE', KEYS[3], '-inf', current_ms)
for _, event_key in ipairs(expired) do
  redis.call('HDEL', KEYS[2], event_key)
  redis.call('ZREM', KEYS[3], event_key)
end

local event_key = ARGV[1]
if event_key ~= '' then
  local previous = redis.call('HGET', KEYS[2], event_key)
  if previous then
    redis.call('PUBLISH', KEYS[4], previous)
    return response(true, cjson.decode(previous), cjson.null)
  end
  if redis.call('HLEN', KEYS[2]) >= tonumber(ARGV[2]) then
    return response(false, cjson.null, 'REALTIME_IDEMPOTENCY_CAPACITY_REACHED')
  end
end

redis.call('SETNX', KEYS[1], 1)
local version = redis.call('INCR', KEYS[1])
local event = cjson.decode(ARGV[4])
event.state_version = version
local encoded = cjson.encode(event)
if event_key ~= '' then
  redis.call('HSET', KEYS[2], event_key, encoded)
  redis.call('ZADD', KEYS[3], current_ms + tonumber(ARGV[3]), event_key)
end
redis.call('PUBLISH', KEYS[4], encoded)
return response(true, event, cjson.null)
""",
)
