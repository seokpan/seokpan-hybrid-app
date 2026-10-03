"""Atomic admission and idempotency for transient Redis chat delivery."""

from seokpan.persistence.redis.common import VersionedLuaScript

PUBLISH_CHAT = VersionedLuaScript(
    name="publish-chat",
    version=1,
    source="""
local function response(ok, value, error_code)
  return cjson.encode({ok = ok, value = value, error = error_code})
end

local current = redis.call('TIME')
local current_ms = current[1] * 1000 + math.floor(current[2] / 1000)
local expired = redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', current_ms)
for _, request_key in ipairs(expired) do
  redis.call('HDEL', KEYS[1], request_key)
  redis.call('ZREM', KEYS[2], request_key)
end

local request_key = ARGV[1]
local fingerprint = ARGV[2]
local previous_raw = redis.call('HGET', KEYS[1], request_key)
if previous_raw then
  local previous = cjson.decode(previous_raw)
  if previous.fingerprint ~= fingerprint then
    return response(false, cjson.null, 'REQUEST_ID_REUSED')
  end
  return response(true, {
    message_id = previous.message_id,
    occurred_at = previous.occurred_at,
    replayed = true
  }, cjson.null)
end

if redis.call('HLEN', KEYS[1]) >= tonumber(ARGV[3]) then
  return response(false, cjson.null, 'CHAT_RETRY_CAPACITY_REACHED')
end

local receipt = {
  fingerprint = fingerprint,
  message_id = ARGV[4],
  occurred_at = ARGV[5]
}
redis.call('HSET', KEYS[1], request_key, cjson.encode(receipt))
redis.call('ZADD', KEYS[2], current_ms + tonumber(ARGV[6]), request_key)
redis.call('PUBLISH', KEYS[3], ARGV[7])
return response(true, {
  message_id = ARGV[4],
  occurred_at = ARGV[5],
  replayed = false
}, cjson.null)
""",
)
