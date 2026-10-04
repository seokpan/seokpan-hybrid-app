"""Same-slot closure evidence and terminal ACK; neither script touches Game results."""

from seokpan.persistence.redis.common import VersionedLuaScript

CLOSED_START_READ = VersionedLuaScript(
    name="closed-start-read",
    version=1,
    source=r"""
local function reject(code) return cjson.encode({ok=false,error=code}) end
local types = {'hash','string','string','string'}
for i, expected in ipairs(types) do
  local actual = redis.call('TYPE', KEYS[i]).ok
  if actual ~= 'none' and actual ~= expected then return reject('START_CLOSURE_KEY_TYPE') end
end
if redis.call('EXISTS', KEYS[1]) ~= 0 then return reject('GAME_CLOSURE_UNCONFIRMED') end
local marker = redis.call('GET', KEYS[2])
if not marker then return reject('GAME_CLOSURE_UNCONFIRMED') end
-- Return original strings: no CJSON numeric precision loss in marker/intent/witness.
return cjson.encode({ok=true,marker=marker,
  intent=redis.call('GET',KEYS[3]) or cjson.null,
  phase=redis.call('GET',KEYS[4]) or cjson.null})
""",
)

CLOSED_START_ACK = VersionedLuaScript(
    name="closed-start-ack",
    version=1,
    source=r"""
local function reject(code) return cjson.encode({ok=false,error=code}) end
local types = {'hash','string','string','string','string'}
for i, expected in ipairs(types) do
  local actual = redis.call('TYPE', KEYS[i]).ok
  if actual ~= 'none' and actual ~= expected then return reject('START_CLOSURE_KEY_TYPE') end
end
if redis.call('EXISTS', KEYS[1]) ~= 0 or redis.call('EXISTS', KEYS[5]) ~= 0 then
  return reject('GAME_CLOSURE_UNCONFIRMED')
end
local raw = redis.call('GET',KEYS[2])
local ok, marker = pcall(cjson.decode, raw or '')
local closed = tonumber(ARGV[3])
local ttl = tonumber(ARGV[7])
if not ok or type(marker) ~= 'table' or marker.room_id ~= ARGV[1]
    or marker.terminated_game_id ~= ARGV[2] or marker.closed_at_ms ~= closed
    or type(marker.invalidation_pending) ~= 'boolean' then
  return reject('GAME_CLOSURE_UNCONFIRMED')
end
if not ttl or ttl <= 0 or ttl ~= math.floor(ttl) then return reject('START_CLOSURE_INVALID') end
if redis.call('GET',KEYS[3]) ~= ARGV[4] then return reject('START_CLOSURE_CHANGED') end
local phase = redis.call('GET',KEYS[4])
local replayed = not marker.invalidation_pending
local retain_until
if replayed then
  if phase ~= ARGV[6] or type(marker.retain_until_ms) ~= 'number' then
    return reject('START_CLOSURE_CHANGED')
  end
  retain_until = marker.retain_until_ms
else
  if phase ~= ARGV[5] and phase ~= ARGV[6] then return reject('START_CLOSURE_CHANGED') end
  local terminal_ok, terminal = pcall(cjson.decode, ARGV[6])
  if not terminal_ok or type(terminal) ~= 'table' or terminal.phase ~= 'FINALIZED'
      or terminal.game_id ~= ARGV[2] or terminal.closed_at_ms ~= closed then
    return reject('START_CLOSURE_INVALID')
  end
  local now = redis.call('TIME')
  retain_until = tonumber(now[1])*1000 + math.floor(tonumber(now[2])/1000) + ttl
end
if retain_until < 0 or retain_until >= 9007199254740992
    or retain_until ~= math.floor(retain_until) then
  return reject('START_CLOSURE_INVALID')
end
if not replayed then
  local receipt = '{"closed_at_ms":' .. string.format('%.0f',closed)
    .. ',"invalidation_pending":false,"retain_until_ms":' .. string.format('%.0f',retain_until)
    .. ',"room_id":' .. cjson.encode(ARGV[1])
    .. ',"terminated_game_id":' .. cjson.encode(ARGV[2]) .. '}'
  -- Before ACK no record receives a TTL. A failure leaves retryable evidence.
  redis.call('SET',KEYS[4],ARGV[6])
  redis.call('SET',KEYS[2],receipt)
end
-- After ACK a failure may leave extra data, never incomplete work with an expiry.
-- Retrying converges TTLs to the same deadline; it never extends retention.
redis.call('PEXPIREAT',KEYS[3],retain_until)
redis.call('PEXPIREAT',KEYS[4],retain_until)
redis.call('PEXPIREAT',KEYS[2],retain_until)
return cjson.encode({ok=true,replayed=replayed})
""",
)
