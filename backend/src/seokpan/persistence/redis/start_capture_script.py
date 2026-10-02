"""Atomic Room admission plus immutable intent. Enabled only by the new command."""

from seokpan.persistence.redis.common import VersionedLuaScript
from seokpan.persistence.redis.room_scripts import _SNAPSHOT
from seokpan.room.application.start_capture import validate_intent_lookup


def start_intent_key(room_id: str, game_id: str) -> str:
    validate_intent_lookup(room_id, game_id)
    return f"stone:v1:room:{{{room_id}}}:start-intent:{game_id}"


def start_phase_key(room_id: str, game_id: str) -> str:
    validate_intent_lookup(room_id, game_id)
    return f"stone:v1:room:{{{room_id}}}:start-phase:{game_id}"


ROOM_START_CAPTURE = VersionedLuaScript(
    name="room-start-capture",
    version=1,
    source=_SNAPSHOT
    + r"""
local function reject(code)
  return cjson.encode({ok=false, error=code})
end
local function exact_json(value)
  if value == cjson.null then return 'null' end
  if type(value) == 'number' then
    if value ~= math.floor(value) or math.abs(value) > 9007199254740991 then
      error('START_CAPTURE_NUMBER_INVALID')
    end
    return string.format('%.0f', value)
  end
  if type(value) ~= 'table' then return cjson.encode(value) end
  local parts = {}
  if #value > 0 then
    for _, item in ipairs(value) do table.insert(parts, exact_json(item)) end
    return '[' .. table.concat(parts, ',') .. ']'
  end
  local names = {}
  for key, _ in pairs(value) do table.insert(names, key) end
  table.sort(names)
  for _, key in ipairs(names) do
    table.insert(parts, cjson.encode(key) .. ':' .. exact_json(value[key]))
  end
  return '{' .. table.concat(parts, ',') .. '}'
end
-- Validate key types before any write; script atomicity is not error rollback.
local types = {'hash','hash','set','hash','hash','zset','string','string','string'}
for index, kind in ipairs(types) do
  local actual = redis.call('TYPE', KEYS[index]).ok
  if actual ~= 'none' and actual ~= kind then return reject('START_CAPTURE_KEY_TYPE') end
end
local current = redis.call('TIME')
local now = tonumber(current[1]) * 1000 + math.floor(tonumber(current[2]) / 1000)
local version = tonumber(ARGV[5])
local ttl = tonumber(ARGV[7])
if not version or version < 1 or version >= 9007199254740991
    or version ~= math.floor(version) or not ttl or ttl <= 0 then
  return reject('START_INTENT_INVALID')
end
if redis.call('EXISTS', KEYS[1]) == 0 then return reject('ROOM_NOT_FOUND') end
if tonumber(redis.call('HGET', KEYS[1], 'schema_version')) ~= tonumber(ARGV[8]) then
  return reject('ROOM_SCHEMA_VERSION_MISMATCH')
end
if redis.call('EXISTS', KEYS[7]) == 1 then return reject('ROOM_RECENTLY_CLOSED') end
local fingerprint = redis.sha1hex('capture-start\n' .. ARGV[3] .. '\n' .. ARGV[4]
    .. '\n' .. ARGV[5] .. '\n' .. ARGV[6])
local cached = redis.call('HGET', KEYS[5], ARGV[2])
local expiry = tonumber(redis.call('ZSCORE', KEYS[6], ARGV[2]))
if cached and expiry and expiry > now then
  local entry = cjson.decode(cached)
  if entry.fingerprint ~= fingerprint then return reject('REQUEST_ID_CONFLICT') end
  if redis.call('HGET', KEYS[1], 'game_id') ~= ARGV[3]
      or redis.call('GET', KEYS[8]) == false
      or redis.call('GET', KEYS[9]) ~= 'PENDING' then
    return reject('GAME_START_RECOVERY_REQUIRED')
  end
  entry.result.replayed = true
  return exact_json(entry.result)
end
if redis.call('EXISTS', KEYS[8]) == 1 or redis.call('EXISTS', KEYS[9]) == 1 then
  return reject('START_INTENT_ALREADY_EXISTS')
end
local room = snapshot()
if room.status ~= 'WAITING' then return reject('ROOM_NOT_WAITING') end
if room.state_version ~= version then return reject('STATE_VERSION_CONFLICT') end
if room.owner_id ~= ARGV[4] then return reject('OWNER_REQUIRED') end
local ok, players = pcall(cjson.decode, ARGV[6])
if not ok or type(players) ~= 'table' or #players < 2 or #players > 100 then
  return reject('START_INTENT_INVALID')
end
local ready, count, roster = {}, 0, {}
for _, value in ipairs(room.participants) do
  if value.ready then ready[value.participant_id] = value; count = count + 1 end
  table.insert(roster, {
    participant_id=value.participant_id,
    team=value.ready and value.team or 'NONE',
    role=value.ready and 'PLAYER' or 'SPECTATOR'
  })
end
if count < room.config.minimum_ready then return reject('MINIMUM_READY_NOT_MET') end
if count ~= #players then return reject('START_INTENT_ROSTER_CHANGED') end
local seen, members, guests, teams = {}, {}, {}, {}
for _, player in ipairs(players) do
  if type(player) ~= 'table' then return reject('START_INTENT_INVALID') end
  local participant = ready[player.participant_id]
  if not participant or seen[player.participant_id] or participant.team ~= player.team then
    return reject('START_INTENT_ROSTER_CHANGED')
  end
  if player.team ~= 'BLACK' and player.team ~= 'WHITE' then
    return reject('START_INTENT_INVALID')
  end
  seen[player.participant_id] = true; teams[player.team] = true
  if player.member_id ~= cjson.null and player.member_id ~= nil then
    if type(player.member_id) ~= 'string' or not string.match(player.member_id, '^[1-9]%d*$')
        or #player.member_id > 20 or members[player.member_id]
        or player.guest_label ~= cjson.null or participant.actor_type ~= 'MEMBER' then
      return reject('START_INTENT_ROSTER_CHANGED')
    end
    members[player.member_id] = true
  else
    if type(player.guest_label) ~= 'string'
        or not string.match(player.guest_label, '^Guest%-%d%d%d%d$')
        or guests[player.guest_label] or participant.actor_type ~= 'GUEST' then
      return reject('START_INTENT_ROSTER_CHANGED')
    end
    guests[player.guest_label] = true
  end
end
if not teams.BLACK or not teams.WHITE then return reject('BOTH_TEAMS_REQUIRED') end
if room.config.vote_seconds ~= 5 and room.config.vote_seconds ~= 10
    and room.config.vote_seconds ~= 15 and room.config.vote_seconds ~= 30 then
  return reject('INVALID_VOTE_SECONDS')
end
-- Member IDs stay strings: cjson must never round a 64-bit database identifier.
-- Room-admitted timestamp/version replace only these provider-owned fields.
local intent = {
  schema_version=1, room_id=ARGV[1], game_id=ARGV[3], original_request_id=ARGV[2],
  owner_id=ARGV[4], accepted_state_version=version+1, started_at_ms=now,
  vote_seconds=room.config.vote_seconds, players=players,
  previous_game_id=room.last_game_id, previous_turn_no=room.last_game_turn_no
}
if (room.last_game_id == cjson.null) ~= (room.last_game_turn_no == cjson.null)
    or room.last_game_id == ARGV[3] then return reject('START_INTENT_INVALID') end
-- Encode everything before the first write. No post-transition snapshot read is needed.
local intent_json = exact_json(intent)
room.status = 'PLAYING'; room.game_id = ARGV[3]; room.state_version = version+1
local result = {
  ok=true, error=cjson.null, replayed=false, snapshot=room,
  start_roster=roster, operation_at_ms=now
}
local encoded_result = exact_json(result)
local cached_result = exact_json({fingerprint=fingerprint, result=result})
redis.call('SET', KEYS[8], intent_json)
redis.call('SET', KEYS[9], 'PENDING')
redis.call('HSET', KEYS[1], 'status', 'PLAYING', 'game_id', ARGV[3], 'state_version', version+1)
redis.call('HSET', KEYS[5], ARGV[2], cached_result)
redis.call('ZADD', KEYS[6], now + ttl, ARGV[2])
redis.call('PEXPIRE', KEYS[5], ttl)
redis.call('PEXPIRE', KEYS[6], ttl)
return encoded_result
""",
)
