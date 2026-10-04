"""Versioned Lua sources for atomic Room runtime transitions."""

from seokpan.persistence.redis.common import VersionedLuaScript

_SNAPSHOT = r"""
local function snapshot()
  if redis.call('EXISTS', KEYS[1]) == 0 then return cjson.null end
  local meta_values = redis.call(
    'HMGET', KEYS[1], 'schema_version', 'name', 'visibility',
    'max_participants', 'minimum_ready', 'vote_seconds', 'status',
    'owner_id', 'state_version', 'game_id', 'last_game_id', 'last_game_turn_no'
  )
  local participant_values = redis.call('HGETALL', KEYS[2])
  local participants = {}
  for index = 1, #participant_values, 2 do
    local participant_id = participant_values[index]
    local participant = cjson.decode(participant_values[index + 1])
    participant.participant_id = participant_id
    participant.ready = redis.call('SISMEMBER', KEYS[3], participant_id) == 1
    table.insert(participants, participant)
  end
  table.sort(participants, function(left, right)
    return left.joined_order < right.joined_order
  end)
  return {
    schema_version = tonumber(meta_values[1]),
    room_id = ARGV[1],
    config = {
      name = meta_values[2],
      visibility = meta_values[3],
      max_participants = tonumber(meta_values[4]),
      minimum_ready = tonumber(meta_values[5]),
      vote_seconds = tonumber(meta_values[6])
    },
    status = meta_values[7],
    owner_id = meta_values[8] == '' and cjson.null or meta_values[8],
    state_version = tonumber(meta_values[9]),
    game_id = (not meta_values[10] or meta_values[10] == '') and cjson.null or meta_values[10],
    last_game_id = (not meta_values[11] or meta_values[11] == '') and cjson.null or meta_values[11],
    last_game_turn_no = tonumber(meta_values[12]) or cjson.null,
    participants = participants
  }
end
"""

_MUTATION_COMMON = r"""
local function now_ms()
  local current = redis.call('TIME')
  return current[1] * 1000 + math.floor(current[2] / 1000)
end

local function rejection(code)
  return cjson.encode({ok = false, error = code})
end

local current_ms = now_ms()
local operation = ARGV[2]
local request_id = ARGV[3]
local request_ttl_ms = tonumber(ARGV[4])
local disconnect_lease_ms = tonumber(ARGV[5])
local tombstone_ttl_ms = tonumber(ARGV[6])
local payload = cjson.decode(ARGV[7])
local fingerprint = redis.sha1hex(operation .. '\n' .. ARGV[7])

-- Reject old state before request-cache cleanup or any other write.
if redis.call('EXISTS', KEYS[1]) == 1 and
   tonumber(redis.call('HGET', KEYS[1], 'schema_version')) ~= payload.schema_version then
  return rejection('ROOM_SCHEMA_VERSION_MISMATCH')
end

local expired = redis.call('ZRANGEBYSCORE', KEYS[6], '-inf', current_ms)
if #expired > 0 then
  redis.call('HDEL', KEYS[5], unpack(expired))
  redis.call('ZREM', KEYS[6], unpack(expired))
end
local cached = redis.call('HGET', KEYS[5], request_id)
if cached then
  local entry = cjson.decode(cached)
  if entry.fingerprint ~= fingerprint then
    return rejection('REQUEST_ID_CONFLICT')
  end
  local replay = entry.result
  replay.replayed = true
  return cjson.encode(replay)
end

local function save(result)
  result.ok = true
  result.error = cjson.null
  result.replayed = false
  result.operation_at_ms = current_ms
  redis.call('HSET', KEYS[5], request_id, cjson.encode({
    fingerprint = fingerprint,
    result = result
  }))
  redis.call('ZADD', KEYS[6], current_ms + request_ttl_ms, request_id)
  redis.call('PEXPIRE', KEYS[5], request_ttl_ms)
  redis.call('PEXPIRE', KEYS[6], request_ttl_ms)
  return cjson.encode(result)
end

local function participant(participant_id)
  local raw = redis.call('HGET', KEYS[2], participant_id)
  if not raw then return nil end
  return cjson.decode(raw)
end

local function store_participant(participant_id, value)
  redis.call('HSET', KEYS[2], participant_id, cjson.encode(value))
end

local function advance_version()
  return redis.call('HINCRBY', KEYS[1], 'state_version', 1)
end

local function expected_version_matches()
  local current = tonumber(redis.call('HGET', KEYS[1], 'state_version'))
  return current == payload.expected_state_version
end

local function remove_vote(participant_id)
  if payload.active_vote_turn == nil or payload.active_vote_turn == cjson.null then
    return false
  end
  local raw_game = redis.call('GET', KEYS[10])
  if not raw_game then return false end
  local game = cjson.decode(raw_game)
  if game.turn_no ~= payload.active_vote_turn
      or game.turn_status ~= 'VOTING'
      or current_ms >= game.deadline_ms then
    return false
  end
  local coordinate = redis.call('HGET', KEYS[8], participant_id)
  if not coordinate then return false end
  redis.call('HDEL', KEYS[8], participant_id)
  local remaining = redis.call('HINCRBY', KEYS[9], coordinate, -1)
  if remaining <= 0 then redis.call('HDEL', KEYS[9], coordinate) end
  return true
end

local function update_game_player(participant_id, connected, vote_removed)
  local raw = redis.call('GET', KEYS[10])
  if not raw then return false end
  local game = cjson.decode(raw)
  local changed = vote_removed
  for _, item in ipairs(game.participants or {}) do
    if item.participant_id == participant_id and item.role == 'PLAYER' then
      if item.connected ~= connected then
        item.connected = connected
        changed = true
      end
      break
    end
  end
  if not changed then return false end
  game.state_version = tonumber(game.state_version or 1) + 1
  redis.call('SET', KEYS[10], cjson.encode(game))
  return true
end

local function departure(
  previous_owner_id, new_owner_id, room_closed, termination, terminated_game_id
)
  return {
    previous_owner_id = previous_owner_id == nil and cjson.null or previous_owner_id,
    new_owner_id = new_owner_id == nil and cjson.null or new_owner_id,
    room_closed = room_closed,
    game_termination = termination,
    terminated_game_id = terminated_game_id == nil and cjson.null or terminated_game_id
  }
end

local function owner_departure(departed_id, previous_owner_id)
  if departed_id ~= previous_owner_id then
    return departure(previous_owner_id, previous_owner_id, false, 'NONE')
  end
  local values = redis.call('HGETALL', KEYS[2])
  local candidates = {}
  for index = 1, #values, 2 do
    local participant_id = values[index]
    local value = cjson.decode(values[index + 1])
    if participant_id ~= departed_id and value.connected and value.actor_type == 'MEMBER' then
      table.insert(candidates, {id = participant_id, joined_order = value.joined_order})
    end
  end
  table.sort(candidates, function(left, right)
    return left.joined_order < right.joined_order
  end)
  redis.call('DEL', KEYS[3])
  if #candidates > 0 then
    redis.call('HSET', KEYS[1], 'owner_id', candidates[1].id)
    return departure(previous_owner_id, candidates[1].id, false, 'NONE')
  end
  local status = redis.call('HGET', KEYS[1], 'status')
  local termination = status == 'PLAYING' and 'SYSTEM_INVALID' or 'NONE'
  local game_id = redis.call('HGET', KEYS[1], 'game_id')
  local terminated_game_id = termination == 'SYSTEM_INVALID' and game_id ~= '' and game_id or nil
  local marker = cjson.encode({
    room_id = ARGV[1],
    terminated_game_id = terminated_game_id == nil and cjson.null or terminated_game_id,
    closed_at_ms = current_ms,
    invalidation_pending = termination == 'SYSTEM_INVALID'
  })
  if termination == 'SYSTEM_INVALID' then
    -- Unfinished durable work is not completed by a TTL expiry.
    redis.call('SET', KEYS[7], marker)
  else
    redis.call('SET', KEYS[7], marker, 'PX', tombstone_ttl_ms)
  end
  redis.call('DEL', KEYS[1], KEYS[2], KEYS[3], KEYS[4], KEYS[8])
  return departure(previous_owner_id, nil, true, termination, terminated_game_id)
end
"""

ROOM_MUTATION = VersionedLuaScript(
    name="room-runtime-mutation",
    version=15,
    source=_SNAPSHOT
    + _MUTATION_COMMON
    + r"""
if operation == 'create' then
  if redis.call('EXISTS', KEYS[7]) == 1 then
    return rejection('ROOM_RECENTLY_CLOSED')
  end
  if redis.call('EXISTS', KEYS[1]) == 1 then
    return rejection('ROOM_ALREADY_EXISTS')
  end
  redis.call('HSET', KEYS[1],
    'schema_version', payload.schema_version,
    'name', payload.name,
    'visibility', payload.visibility,
    'password_hash', payload.encoded_password or '',
    'max_participants', payload.max_participants,
    'minimum_ready', payload.minimum_ready,
    'vote_seconds', payload.vote_seconds,
    'status', 'WAITING',
    'owner_id', payload.owner_id,
    'state_version', 1,
    'next_joined_order', 2,
    'game_id', '', 'last_game_id', '', 'last_game_turn_no', ''
  )
  store_participant(payload.owner_id, {
    actor_type = 'MEMBER', joined_order = 1, connected = true, team = 'NONE'
  })
  redis.call('HSET', KEYS[4], payload.owner_id, cjson.encode({
    session_digest = payload.session_digest,
    generation = 1,
    connected = true,
    disconnect_expires_at_ms = cjson.null
  }))
  return save({snapshot = snapshot(), connection_generation = 1})
end

if redis.call('EXISTS', KEYS[1]) == 0 then
  return rejection('ROOM_NOT_FOUND')
end

if operation == 'join' then
  -- Join is evaluated against current server state. Lobby snapshots intentionally
  -- do not advance for Ready/team-only changes, so their Room state_version is
  -- an observation rather than an admission precondition.
  if redis.call('HEXISTS', KEYS[2], payload.participant_id) == 1 then
    return rejection('PARTICIPANT_ALREADY_JOINED')
  end
  if redis.call('HLEN', KEYS[2]) >= tonumber(redis.call('HGET', KEYS[1], 'max_participants')) then
    return rejection('ROOM_CAPACITY_REACHED')
  end
  local private_room = redis.call('HGET', KEYS[1], 'visibility') == 'PRIVATE'
  if private_room and not payload.private_access_verified then
    return rejection('ROOM_PASSWORD_INVALID')
  end
  local joined_order = tonumber(redis.call('HGET', KEYS[1], 'next_joined_order'))
  store_participant(payload.participant_id, {
    actor_type = payload.actor_type,
    joined_order = joined_order,
    connected = true,
    team = 'NONE'
  })
  redis.call('HSET', KEYS[1], 'next_joined_order', joined_order + 1)
  redis.call('HSET', KEYS[4], payload.participant_id, cjson.encode({
    session_digest = payload.session_digest,
    generation = 1,
    connected = true,
    disconnect_expires_at_ms = cjson.null
  }))
  advance_version()
  return save({snapshot = snapshot(), connection_generation = 1})
end

local current_participant_id = payload.participant_id or payload.actor_id
local current_participant = current_participant_id and participant(current_participant_id) or nil
if not current_participant and operation ~= 'kick' and operation ~= 'complete_game' then
  if operation == 'disconnect' or operation == 'expire_disconnect' then
    return rejection('CONNECTION_NOT_FOUND')
  end
  return rejection('PARTICIPANT_NOT_FOUND')
end
if operation ~= 'disconnect' and operation ~= 'expire_disconnect' then
  if not expected_version_matches() then return rejection('STATE_VERSION_CONFLICT') end
end

if operation == 'change_team' then
  if redis.call('HGET', KEYS[1], 'status') ~= 'WAITING' then
    return rejection('ROOM_NOT_WAITING')
  end
  if current_participant.team ~= payload.team then
    current_participant.team = payload.team
    store_participant(payload.participant_id, current_participant)
    redis.call('SREM', KEYS[3], payload.participant_id)
    advance_version()
  end
  return save({snapshot = snapshot()})
end

if operation == 'change_identity' then
  local raw_connection = redis.call('HGET', KEYS[4], payload.participant_id)
  if not raw_connection then return rejection('CONNECTION_NOT_FOUND') end
  local connection = cjson.decode(raw_connection)
  if current_participant.actor_type ~= payload.actor_type and
     (current_participant.actor_type ~= 'GUEST' or payload.actor_type ~= 'MEMBER') then
    return rejection('ROOM_IDENTITY_CHANGE_NOT_ALLOWED')
  end
  local actor_changed = current_participant.actor_type ~= payload.actor_type
  current_participant.actor_type = payload.actor_type
  store_participant(payload.participant_id, current_participant)
  local changed = connection.session_digest ~= payload.session_digest
  connection.session_digest = payload.session_digest
  redis.call('HSET', KEYS[4], payload.participant_id, cjson.encode(connection))
  if changed or actor_changed then advance_version() end
  return save({snapshot = snapshot()})
end

if operation == 'set_ready' then
  if redis.call('HGET', KEYS[1], 'status') ~= 'WAITING' then
    return rejection('ROOM_NOT_WAITING')
  end
  if payload.ready and current_participant.team == 'NONE' then
    return rejection('TEAM_REQUIRED_TO_READY')
  end
  local existing = redis.call('SISMEMBER', KEYS[3], payload.participant_id) == 1
  if existing ~= payload.ready then
    if payload.ready then redis.call('SADD', KEYS[3], payload.participant_id)
    else redis.call('SREM', KEYS[3], payload.participant_id) end
    advance_version()
  end
  return save({snapshot = snapshot()})
end

if operation == 'change_vote_seconds' then
  if redis.call('HGET', KEYS[1], 'status') ~= 'WAITING' then
    return rejection('ROOM_NOT_WAITING')
  end
  if redis.call('HGET', KEYS[1], 'owner_id') ~= payload.actor_id then
    return rejection('OWNER_REQUIRED')
  end
  local previous = tonumber(redis.call('HGET', KEYS[1], 'vote_seconds'))
  if previous ~= payload.vote_seconds then
    redis.call('HSET', KEYS[1], 'vote_seconds', payload.vote_seconds)
    redis.call('DEL', KEYS[3])
    advance_version()
  end
  return save({snapshot = snapshot()})
end

if operation == 'start_game' then
  if redis.call('HGET', KEYS[1], 'status') ~= 'WAITING' then
    return rejection('ROOM_NOT_WAITING')
  end
  if redis.call('HGET', KEYS[1], 'owner_id') ~= payload.actor_id then
    return rejection('OWNER_REQUIRED')
  end
  if not current_participant.connected then
    return rejection('PARTICIPANT_DISCONNECTED')
  end
  local minimum_ready = tonumber(redis.call('HGET', KEYS[1], 'minimum_ready'))
  local connected_ready_count = 0
  local has_black = false
  local has_white = false
  local values = redis.call('HGETALL', KEYS[2])
  local roster = {}
  for index = 1, #values, 2 do
    local participant_id = values[index]
    local value = cjson.decode(values[index + 1])
    local ready = redis.call('SISMEMBER', KEYS[3], participant_id) == 1 and value.connected
    if ready then connected_ready_count = connected_ready_count + 1 end
    if ready and value.team == 'BLACK' then has_black = true end
    if ready and value.team == 'WHITE' then has_white = true end
    table.insert(roster, {
      participant_id = participant_id,
      team = ready and value.team or 'NONE',
      role = ready and 'PLAYER' or 'SPECTATOR',
      joined_order = value.joined_order
    })
  end
  if connected_ready_count < minimum_ready then
    return rejection('MINIMUM_READY_NOT_MET')
  end
  if not has_black or not has_white then return rejection('BOTH_TEAMS_REQUIRED') end
  table.sort(roster, function(left, right) return left.joined_order < right.joined_order end)
  for _, item in ipairs(roster) do item.joined_order = nil end
  redis.call('HSET', KEYS[1], 'status', 'PLAYING', 'game_id', payload.game_id)
  advance_version()
  return save({snapshot = snapshot(), start_roster = roster})
end

if operation == 'complete_game' then
  if not expected_version_matches() then return rejection('STATE_VERSION_CONFLICT') end
  if redis.call('HGET', KEYS[1], 'status') ~= 'PLAYING' then
    return rejection('ROOM_NOT_PLAYING')
  end
  if redis.call('HGET', KEYS[1], 'game_id') ~= payload.game_id then
    return rejection('STALE_GAME')
  end
  redis.call('HSET', KEYS[1], 'status', 'WAITING', 'game_id', '',
    'last_game_id', payload.game_id, 'last_game_turn_no', payload.final_turn_no)
  redis.call('DEL', KEYS[3])
  advance_version()
  return save({snapshot = snapshot()})
end

if operation == 'connect' then
  local raw = redis.call('HGET', KEYS[4], payload.participant_id)
  local generation = 1
  if raw then generation = cjson.decode(raw).generation + 1 end
  if not current_participant.connected then
    current_participant.connected = true
    store_participant(payload.participant_id, current_participant)
    advance_version()
  end
  redis.call('HSET', KEYS[4], payload.participant_id, cjson.encode({
    session_digest = payload.session_digest,
    generation = generation,
    connected = true,
    disconnect_expires_at_ms = cjson.null
  }))
  update_game_player(payload.participant_id, true, false)
  return save({snapshot = snapshot(), connection_generation = generation})
end

if operation == 'disconnect' then
  local raw = redis.call('HGET', KEYS[4], payload.participant_id)
  if not raw then return rejection('CONNECTION_NOT_FOUND') end
  local connection = cjson.decode(raw)
  if connection.generation ~= payload.connection_generation then
    return save({snapshot = snapshot(), stale_connection = true})
  end
  if not expected_version_matches() then return rejection('STATE_VERSION_CONFLICT') end
  if not connection.connected then
    return save({
      snapshot = snapshot(),
      disconnect_expires_at_ms = connection.disconnect_expires_at_ms
    })
  end
  local previous_owner_id = redis.call('HGET', KEYS[1], 'owner_id')
  current_participant.connected = false
  store_participant(payload.participant_id, current_participant)
  connection.connected = false
  connection.disconnect_expires_at_ms = current_ms + disconnect_lease_ms
  redis.call('HSET', KEYS[4], payload.participant_id, cjson.encode(connection))
  local vote_removed = remove_vote(payload.participant_id)
  update_game_player(payload.participant_id, false, vote_removed)
  advance_version()
  return save({
    snapshot = snapshot(),
    disconnect_expires_at_ms = connection.disconnect_expires_at_ms,
    vote_removed = vote_removed,
    departure = departure(previous_owner_id, previous_owner_id, false, 'NONE')
  })
end

if operation == 'expire_disconnect' then
  local raw = redis.call('HGET', KEYS[4], payload.participant_id)
  if not raw then return rejection('CONNECTION_NOT_FOUND') end
  local connection = cjson.decode(raw)
  if connection.generation ~= payload.connection_generation or connection.connected then
    return save({snapshot = snapshot(), stale_connection = true})
  end
  if not expected_version_matches() then return rejection('STATE_VERSION_CONFLICT') end
  if current_ms < connection.disconnect_expires_at_ms then
    return rejection('DISCONNECT_LEASE_ACTIVE')
  end
  local previous_owner_id = redis.call('HGET', KEYS[1], 'owner_id')
  redis.call('HDEL', KEYS[2], payload.participant_id)
  redis.call('SREM', KEYS[3], payload.participant_id)
  redis.call('HDEL', KEYS[4], payload.participant_id)
  local vote_removed = remove_vote(payload.participant_id)
  update_game_player(payload.participant_id, false, vote_removed)
  local resolved = owner_departure(payload.participant_id, previous_owner_id)
  if not resolved.room_closed then advance_version() end
  return save({snapshot = snapshot(), vote_removed = vote_removed, departure = resolved})
end

if operation == 'kick' then
  if redis.call('HGET', KEYS[1], 'status') ~= 'WAITING' then
    return rejection('ROOM_NOT_WAITING')
  end
  local owner_id = redis.call('HGET', KEYS[1], 'owner_id')
  if owner_id ~= payload.actor_id then return rejection('OWNER_REQUIRED') end
  if payload.actor_id == payload.target_id then return rejection('CANNOT_KICK_SELF') end
  if not participant(payload.target_id) then return rejection('PARTICIPANT_NOT_FOUND') end
  redis.call('HDEL', KEYS[2], payload.target_id)
  redis.call('SREM', KEYS[3], payload.target_id)
  redis.call('HDEL', KEYS[4], payload.target_id)
  advance_version()
  return save({snapshot = snapshot(), departure = {
    previous_owner_id = owner_id, new_owner_id = owner_id,
    room_closed = false, game_termination = 'NONE'
  }})
end

if operation == 'leave' then
  local previous_owner_id = redis.call('HGET', KEYS[1], 'owner_id')
  redis.call('HDEL', KEYS[2], payload.participant_id)
  redis.call('SREM', KEYS[3], payload.participant_id)
  redis.call('HDEL', KEYS[4], payload.participant_id)
  local vote_removed = remove_vote(payload.participant_id)
  update_game_player(payload.participant_id, false, vote_removed)
  local resolved = owner_departure(payload.participant_id, previous_owner_id)
  if not resolved.room_closed then advance_version() end
  return save({snapshot = snapshot(), vote_removed = vote_removed, departure = resolved})
end

return rejection('ROOM_OPERATION_INVALID')
""",
)

_READ_RESPONSE = r"""
if redis.call('EXISTS', KEYS[1]) == 1 and
   tonumber(redis.call('HGET', KEYS[1], 'schema_version')) ~= tonumber(ARGV[2]) then
  return cjson.encode({ok = false, error = 'ROOM_SCHEMA_VERSION_MISMATCH'})
end
return cjson.encode({ok = true, snapshot = snapshot(), error = cjson.null})
"""

ROOM_READ = VersionedLuaScript(
    name="room-runtime-read",
    version=2,
    source=_SNAPSHOT + _READ_RESPONSE,
)

ROOM_PRIVATE_HASH_READ = VersionedLuaScript(
    name="room-private-hash-read",
    version=1,
    source=r"""
local value = redis.call('HGET', KEYS[1], 'password_hash')
if not value or value == '' then value = cjson.null end
return cjson.encode({ok = true, encoded_password = value, error = cjson.null})
""",
)


ROOM_INVALIDATION_ACK = VersionedLuaScript(
    name="room-invalidation-ack",
    version=2,
    source=r"""
local raw = redis.call('GET', KEYS[1])
if not raw then
  return cjson.encode({ok = true, missing = true})
end
local value = cjson.decode(raw)
if value.terminated_game_id ~= ARGV[1] then
  return cjson.encode({ok = false, error = 'STALE_GAME'})
end
if value.invalidation_pending == false then
  return cjson.encode({ok = true, missing = false})
end
local ttl = tonumber(ARGV[2])
if not ttl or ttl <= 0 or ttl ~= math.floor(ttl) then
  return cjson.encode({ok = false, error = 'INVALID_INVALIDATION_RETENTION'})
end
value.invalidation_pending = false
redis.call('SET', KEYS[1], cjson.encode(value), 'PX', ttl)
return cjson.encode({ok = true, missing = false})
""",
)
