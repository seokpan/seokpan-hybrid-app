"""First Runtime and initialized witness share one script; no production activation."""

from seokpan.persistence.redis.common import VersionedLuaScript

VOTE_START_INITIALIZE = VersionedLuaScript(
    name="vote-start-initialize",
    version=1,
    source=r"""
local function reject(code) return cjson.encode({ok=false, error=code}) end
local function exact_json(value)
  if value == cjson.null then return 'null' end
  if type(value) == 'number' then
    if value ~= math.floor(value) or math.abs(value) > 9007199254740991 then
      error('START_INITIALIZATION_NUMBER_INVALID')
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
local types = {'hash','hash','string','string','string','string','hash',
  'hash','hash','string','hash','hash','string'}
if #KEYS ~= 13 and #KEYS ~= 16 then return reject('START_INITIALIZE_KEYS_INVALID') end
for i=14,#KEYS do types[i] = i == 16 and 'string' or 'hash' end
for i, kind in ipairs(types) do
  local actual = redis.call('TYPE', KEYS[i]).ok
  if actual ~= 'none' and actual ~= kind then return reject('START_INITIALIZE_KEY_TYPE') end
end
local valid, intent = pcall(cjson.decode, ARGV[1])
if not valid or type(intent) ~= 'table' or intent.schema_version ~= 1 then
  return reject('START_INTENT_INVALID')
end
-- Input is the Python-validated canonical codec; compare exact stored bytes.
-- A corrupt/changed intent is never repaired by accepting a newer caller value.
if redis.call('GET', KEYS[4]) ~= ARGV[1] then return reject('START_INTENT_INVALID') end
if redis.call('EXISTS', KEYS[1]) == 0 then return reject('ROOM_NOT_FOUND') end
if redis.call('EXISTS', KEYS[3]) == 1 then return reject('GAME_NOT_IN_CURRENT_ROOM') end
if redis.call('HGET', KEYS[1], 'schema_version') ~= '3' then
  return reject('ROOM_SCHEMA_VERSION_MISMATCH')
end
local last_id = redis.call('HGET', KEYS[1], 'last_game_id')
local last_turn = tonumber(redis.call('HGET', KEYS[1], 'last_game_turn_no'))
if redis.call('HGET', KEYS[1], 'status') ~= 'PLAYING'
    or redis.call('HGET', KEYS[1], 'game_id') ~= intent.game_id
    or (last_id and last_id ~= '' and last_id or cjson.null) ~= intent.previous_game_id
    or (last_turn or cjson.null) ~= intent.previous_turn_no then
  return reject('GAME_NOT_IN_CURRENT_ROOM')
end
if redis.call('HGET', KEYS[1], 'owner_id') ~= ARGV[2] then return reject('OWNER_REQUIRED') end
local expected = tonumber(ARGV[3])
if not expected or expected ~= math.floor(expected)
    or expected < intent.accepted_state_version or expected >= 9007199254740992
    or tonumber(redis.call('HGET', KEYS[1], 'state_version')) ~= expected then
  return reject('STATE_VERSION_CONFLICT')
end
local phase = redis.call('GET', KEYS[5])
local raw_game = redis.call('GET', KEYS[6])
local old = nil
if raw_game then
  local ok, value = pcall(cjson.decode, raw_game)
  if not ok or type(value) ~= 'table' or value.schema_version ~= 3 then
    return reject('VOTE_SCHEMA_VERSION_MISMATCH')
  end
  old = value
end
if phase ~= 'PENDING' then
  local ok, witness = pcall(cjson.decode, phase or '')
  if not ok or type(witness) ~= 'table' or witness.schema_version ~= 1
      or witness.phase ~= 'INITIALIZED' or witness.game_id ~= intent.game_id
      or witness.intent_fingerprint ~= ARGV[4]
      or type(witness.initialized_at_ms) ~= 'number'
      or type(witness.first_deadline_ms) ~= 'number'
      or witness.initialized_at_ms < intent.started_at_ms
      or witness.initialized_at_ms ~= math.floor(witness.initialized_at_ms)
      or witness.first_deadline_ms >= 9007199254740992
      or witness.first_deadline_ms ~= witness.initialized_at_ms + intent.vote_seconds*1000
      or not old or old.game_id ~= intent.game_id then
    return reject('GAME_START_RECOVERY_REQUIRED')
  end
  -- The adapter reads the current turn's snapshot; never return a stale turn-one cache.
  return cjson.encode({ok=true, replayed=true})
end
if old then
  if old.game_id == intent.game_id then return reject('GAME_START_RECOVERY_REQUIRED') end
  if (old.game_status ~= 'FINISHED' and old.game_status ~= 'SYSTEM_INVALID')
      or old.game_id ~= intent.previous_game_id or old.turn_no ~= intent.previous_turn_no then
    return reject('STALE_GAME')
  end
elseif intent.previous_game_id ~= cjson.null then
  return reject('GAME_RUNTIME_NOT_FOUND')
end
if (intent.previous_game_id ~= cjson.null) ~= (#KEYS == 16) then
  return reject('START_INITIALIZE_KEYS_INVALID')
end
local participants = {}
for _, player in ipairs(intent.players) do
  local raw = redis.call('HGET', KEYS[2], player.participant_id)
  if not raw then return reject('GAME_START_RECOVERY_REQUIRED') end
  local ok, current = pcall(cjson.decode, raw)
  if not ok or type(current) ~= 'table' or type(current.connected) ~= 'boolean' then
    return reject('START_INITIALIZE_PARTICIPANT_INVALID')
  end
  table.insert(participants, {participant_id=player.participant_id, team=player.team,
    role='PLAYER', connected=current.connected})
end
local timestamp = redis.call('TIME')
local now = tonumber(timestamp[1])*1000 + math.floor(tonumber(timestamp[2])/1000)
local deadline = now + intent.vote_seconds*1000
if now < intent.started_at_ms or deadline >= 9007199254740992 then
  return reject('INVALID_DEADLINE')
end
local game = {schema_version=3, game_id=intent.game_id, state_version=2, turn_no=1,
  turn_status='VOTING', current_team='BLACK', deadline_ms=deadline, consecutive_passes=0,
  move_no=0, last_move=cjson.null, game_status='ACTIVE', end_reason=cjson.null,
  valid_voter_count=cjson.null, participants=participants, candidates={}}
local snapshot = {}
for key, value in pairs(game) do snapshot[key] = value end
snapshot.room_id=intent.room_id; snapshot.votes={}; snapshot.tally={}
snapshot.occupied_cells={}; snapshot.resolver=cjson.null
local encoded_game = exact_json(game)
local witness = exact_json({schema_version=1, phase='INITIALIZED', game_id=intent.game_id,
  intent_fingerprint=ARGV[4], initialized_at_ms=now, first_deadline_ms=deadline})
local result = exact_json({ok=true, replayed=false, snapshot=snapshot})
-- Finish validation/encoding before writes. Redis script errors are not rollback.
-- Witness first: unexpected later failure must not make a retry look PENDING.
redis.call('SET', KEYS[5], witness)
for i=7,#KEYS do redis.call('DEL', KEYS[i]) end
redis.call('SET', KEYS[6], encoded_game)
return result
""",
)
