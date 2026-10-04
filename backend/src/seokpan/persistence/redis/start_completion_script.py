"""Same-Room normal completion, preserving Runtime and fixed terminal retention."""

from seokpan.persistence.redis.common import VersionedLuaScript


def completion_pending_key(room_id: str, game_id: str) -> str:
    return f"stone:v1:room:{{{room_id}}}:normal-completion-pending:{game_id}"


START_COMPLETION_READ = VersionedLuaScript(
    name="normal-start-proof-read",
    version=1,
    source=r"""
for _, key in ipairs(KEYS) do
  local t = redis.call('TYPE', key).ok
  if t ~= 'none' and t ~= 'string' then
    return cjson.encode({ok=false,error='START_COMPLETION_INVALID'})
  end
end
return cjson.encode({ok=true,error=cjson.null,
  intent=redis.call('GET',KEYS[1]) or cjson.null,
  phase=redis.call('GET',KEYS[2]) or cjson.null})
""",
)

NORMAL_START_COMPLETE = VersionedLuaScript(
    name="normal-start-complete",
    version=2,
    source=r"""
local function reject(code) return cjson.encode({ok=false,error=code}) end
local function integer(n,min) return type(n)=='number' and n==math.floor(n)
  and n>=(min or 0) and n<9007199254740992 end
local function decode(s)
  if type(s)~='string' or #s>65536 then return nil end
  local ok,v=pcall(cjson.decode,s)
  if not ok or type(v)~='table' then return nil end
  return v
end
local function number(n) return string.format('%.0f',n) end
local room_id,game_id,intent_wire,expected_phase,fp=ARGV[1],ARGV[2],ARGV[3],ARGV[4],ARGV[5]
local expected_version,turn,ended=tonumber(ARGV[6]),tonumber(ARGV[7]),tonumber(ARGV[9])
local reason,retention=ARGV[8],tonumber(ARGV[10])
local types={'hash','set','string','string','string','string','string'}
if #KEYS~=7 then return reject('START_COMPLETION_INVALID') end
for i,key in ipairs(KEYS) do
  local t=redis.call('TYPE',key).ok
  if t~='none' and t~=types[i] then return reject('START_COMPLETION_INVALID') end
end
local task_raw=redis.call('GET',KEYS[7])
local expected_task=ARGV[11] or ''
if expected_task~='' then
  if not task_raw then return cjson.encode({ok=true,error=cjson.null,changed=false}) end
  if task_raw~=expected_task then return reject('START_COMPLETION_CHANGED') end
end
local task=task_raw and decode(task_raw) or nil
if task_raw and (not task or task.schema_version~=1 or type(task.released)~='boolean'
    or task.intent~=intent_wire or type(task.phase)~='string') then
  return reject('START_COMPLETION_INVALID')
end
-- Once the matching F15 closure is ACKed, it owns retention. Remove only
-- this obsolete normal task; never rewrite F15's terminal phase or its TTL.
if task and redis.call('EXISTS',KEYS[1])==0 then
  local m=decode(redis.call('GET',KEYS[6]))
  local p=decode(redis.call('GET',KEYS[5]))
  if m and m.room_id==room_id and m.terminated_game_id==game_id
      and m.invalidation_pending==false and integer(m.closed_at_ms)
      and p and p.schema_version==1 and p.phase=='FINALIZED' and p.game_id==game_id
      and p.intent_fingerprint==fp and p.closed_at_ms==m.closed_at_ms
      and redis.call('GET',KEYS[4])==intent_wire and redis.call('EXISTS',KEYS[3])==0 then
    redis.call('DEL',KEYS[7])
    return cjson.encode({ok=true,error=cjson.null,changed=false})
  end
end
-- RELEASED is a repair task, not permission to release any live Game again.
-- No original proof is recreated if its fixed retention has already elapsed.
if task and task.released then
  local p=decode(task.phase)
  local i=decode(task.intent)
  local c=p and p.normal_completion
  if not p or not i or i.room_id~=room_id or i.game_id~=game_id
      or p.schema_version~=1 or p.phase~='INITIALIZED' or p.game_id~=game_id
      or p.intent_fingerprint~=fp or not integer(p.initialized_at_ms)
      or not integer(p.first_deadline_ms) or p.initialized_at_ms<i.started_at_ms
      or p.first_deadline_ms~=p.initialized_at_ms+i.vote_seconds*1000
      or type(c)~='table' or c.schema_version~=1
      or not integer(c.final_turn_no,1) or c.final_turn_no~=turn
      or c.end_reason~=reason or not integer(c.ended_at_ms) or c.ended_at_ms~=ended
      or not integer(c.recorded_at_ms) or c.recorded_at_ms<ended
      or not integer(c.retain_until_ms) or retention~=86400000
      or c.retain_until_ms~=c.recorded_at_ms+retention then
    return reject('START_COMPLETION_INVALID')
  end
  if redis.call('HGET',KEYS[1],'game_id')==game_id then
    return reject('GAME_COMPLETION_UNCONFIRMED')
  end
  local closure=redis.call('GET',KEYS[6])
  if closure then
    local m=decode(closure)
    if not m or (m.terminated_game_id==game_id and m.invalidation_pending) then
      return reject('GAME_COMPLETION_UNCONFIRMED')
    end
  end
  local t=redis.call('TIME')
  local now=tonumber(t[1])*1000+math.floor(tonumber(t[2])/1000)
  local saved_intent=redis.call('GET',KEYS[4])
  local saved_phase=redis.call('GET',KEYS[5])
  if (saved_intent and saved_intent~=task.intent) or (saved_phase and saved_phase~=task.phase)
      or (now<c.retain_until_ms and (not saved_intent or not saved_phase)) then
    return reject('START_COMPLETION_CHANGED')
  end
  redis.call('PEXPIREAT',KEYS[4],number(c.retain_until_ms))
  redis.call('PEXPIREAT',KEYS[5],number(c.retain_until_ms))
  redis.call('DEL',KEYS[7])
  return cjson.encode({ok=true,error=cjson.null,changed=false})
end
if redis.call('EXISTS',KEYS[1])==0 or redis.call('EXISTS',KEYS[6])==1 then
  return reject('GAME_COMPLETION_UNCONFIRMED')
end
if tonumber(redis.call('HGET',KEYS[1],'schema_version'))~=3 then
  return reject('ROOM_SCHEMA_VERSION_MISMATCH')
end
if redis.call('GET',KEYS[4])~=intent_wire then return reject('START_COMPLETION_CHANGED') end
local raw=redis.call('GET',KEYS[5])
local phase,intent=decode(raw),decode(intent_wire)
if not phase or not intent or phase.schema_version~=1 or phase.phase~='INITIALIZED'
    or phase.game_id~=game_id or phase.intent_fingerprint~=fp
    or intent.room_id~=room_id or intent.game_id~=game_id
    or not integer(phase.initialized_at_ms) or not integer(phase.first_deadline_ms)
    or phase.initialized_at_ms<intent.started_at_ms
    or phase.first_deadline_ms~=phase.initialized_at_ms+intent.vote_seconds*1000
    or not integer(expected_version,1) or not integer(turn,1) or not integer(ended)
    or ended<intent.started_at_ms or retention~=86400000 then
  return reject('START_COMPLETION_INVALID')
end
if reason~='BLACK_WIN' and reason~='WHITE_WIN' and reason~='DRAW'
    and reason~='FORFEIT' and reason~='JOINT_LOSS' then
  return reject('START_COMPLETION_INVALID')
end
local time=redis.call('TIME')
local now=tonumber(time[1])*1000+math.floor(tonumber(time[2])/1000)
if task and not task.released then
  local prepared=decode(task.phase)
  if not prepared or prepared.schema_version~=phase.schema_version
      or prepared.phase~=phase.phase or prepared.game_id~=phase.game_id
      or prepared.intent_fingerprint~=phase.intent_fingerprint
      or prepared.initialized_at_ms~=phase.initialized_at_ms
      or prepared.first_deadline_ms~=phase.first_deadline_ms then
    return reject('START_COMPLETION_CHANGED')
  end
  -- Reuse the first durable receipt even if SET phase failed immediately after SET task.
  if phase.normal_completion==nil then phase=prepared end
end
local receipt=phase.normal_completion
local replay=receipt~=nil
if replay then
  if type(receipt)~='table' or receipt.schema_version~=1
      or not integer(receipt.final_turn_no,1) or receipt.final_turn_no~=turn
      or not integer(receipt.ended_at_ms) or receipt.ended_at_ms~=ended
      or receipt.end_reason~=reason or not integer(receipt.recorded_at_ms)
      or receipt.recorded_at_ms<ended or not integer(receipt.retain_until_ms)
      or receipt.retain_until_ms~=receipt.recorded_at_ms+retention then
    return reject('START_COMPLETION_INVALID')
  end
  local count=0;for k,v in pairs(receipt) do count=count+1 end
  if count~=6 then return reject('START_COMPLETION_INVALID') end
else
  if raw~=expected_phase then return reject('START_COMPLETION_CHANGED') end
  if now<ended or not integer(now+retention) then return reject('START_COMPLETION_INVALID') end
  receipt={recorded_at_ms=now,retain_until_ms=now+retention}
end
local status=redis.call('HGET',KEYS[1],'status')
local current=redis.call('HGET',KEYS[1],'game_id')
local releasing=status=='PLAYING' and current==game_id
local waiting=status=='WAITING' and (not current or current=='')
  and redis.call('HGET',KEYS[1],'last_game_id')==game_id
  and tonumber(redis.call('HGET',KEYS[1],'last_game_turn_no'))==turn
if not releasing and not waiting and not replay then
  return reject('GAME_COMPLETION_UNCONFIRMED')
end
if status~='PLAYING' and status~='WAITING' then
  return reject('GAME_COMPLETION_UNCONFIRMED')
end
if releasing or not replay then
  local game=decode(redis.call('GET',KEYS[3]))
  if not game or game.schema_version~=3 or game.game_id~=game_id
      or game.game_status~='FINISHED' or game.turn_no~=turn or game.end_reason~=reason then
    return reject('GAME_COMPLETION_UNCONFIRMED')
  end
end
local version=tonumber(redis.call('HGET',KEYS[1],'state_version'))
if releasing and (version~=expected_version or not integer(version+1,1)) then
  return reject('STATE_VERSION_CONFLICT')
end
-- Encode all numbers as exact integer literals, not CJSON's default precision.
local wire=task and not task.released and task.phase or raw
if not replay then
  wire='{"schema_version":1,"phase":"INITIALIZED","game_id":'..cjson.encode(game_id)
    ..',"intent_fingerprint":'..cjson.encode(fp)
    ..',"initialized_at_ms":'..number(phase.initialized_at_ms)
    ..',"first_deadline_ms":'..number(phase.first_deadline_ms)
    ..',"normal_completion":{"schema_version":1,"final_turn_no":'..number(turn)
    ..',"end_reason":'..cjson.encode(reason)..',"ended_at_ms":'..number(ended)
    ..',"recorded_at_ms":'..number(receipt.recorded_at_ms)
    ..',"retain_until_ms":'..number(receipt.retain_until_ms)..'}}'
end
-- The receipt is not release authority by itself. No TTL before Room release.
-- An interrupted write is not rolled back; retries repair these ordered cuts.
local function task_wire(released)
  return '{"schema_version":1,"intent":'..cjson.encode(intent_wire)
    ..',"phase":'..cjson.encode(wire)..',"released":'..(released and 'true' or 'false')..'}'
end
-- Persist rediscoverable work before any Room/phase/expiry mutation.
redis.call('SET',KEYS[7],task_wire(false))
if raw~=wire then redis.call('SET',KEYS[5],wire) end
if releasing then
  redis.call('DEL',KEYS[2])
  redis.call('HSET',KEYS[1],'status','WAITING','game_id','',
    'last_game_id',game_id,'last_game_turn_no',number(turn),'state_version',number(version+1))
end
redis.call('SET',KEYS[7],task_wire(true))
redis.call('PEXPIREAT',KEYS[4],number(receipt.retain_until_ms))
redis.call('PEXPIREAT',KEYS[5],number(receipt.retain_until_ms))
redis.call('DEL',KEYS[7])
return cjson.encode({ok=true,error=cjson.null,changed=releasing})
""",
)
