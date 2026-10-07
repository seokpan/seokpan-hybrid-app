"""Apply only reviewed files to a separate checkout; never call service APIs."""
import hashlib
import json
from pathlib import Path
import shutil
import sys

phase, target = sys.argv[1:]
root = Path(target).resolve()
source = Path(__file__).resolve().parents[2]
preimages = {
    'backend/src/seokpan/persistence/redis/vote_scripts.py': '527f10141079ca17d8434013f49a039a12808086b70b39b25044bc3d0ee3e42a',
    'backend/tests/integration/test_departure_redis_lua.py': 'a925a36abdb4d99b9c3f2bd01fdf01b443b5f09997710e0c55d8e1df5a2c0674',
    'backend/tests/vote_runtime/test_legacy_initialization_guard.py': '81aa230f11d142b6c86ac5982d92428a6d5775f5f833223fec46c09a9379ee6a',
    'backend/tests/vote_runtime/test_redis_vote_adapter.py': '6734f320d508224df545f0e410e77f6413f09f4dd5d9da0ee2ac5e20a47a966d',
}
postimages = {
    'backend/src/seokpan/persistence/redis/vote_scripts.py': 'b3aa1f304b5901316d126a1ad470183cb6691a1d2c88e482c8b67c53d519b0f3',
    'backend/tests/integration/test_departure_redis_lua.py': '7e61903a241f0b1522a48b2f8cf7be008b53cab79ea8c1581f21667d729a4d47',
    'backend/tests/vote_runtime/test_legacy_initialization_guard.py': 'd54e2960f0023b458f624c576e86c040600d8507343d5516f3b31a23da8a15af',
    'backend/tests/vote_runtime/test_redis_vote_adapter.py': 'ac3e21546743f2d083e2fade04c28972d69fb1c30db7c73c5e40166af8047618',
    '.github/workflows/backend-source-validation.yml': '245ee413f1f574f46e935e71b897030e5d8f0cc6ec8faee9475bbd7db583cec7',
    'docs/HYBRID_LUA_REGRESSION.md': '27e99064310dcb40d83831728e3a5e8462d91df095a4ecdbdd29e7fac3a4b40e',
}
def verify(expected):
    for name, digest in expected.items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest, name

def replace(path, old, new, count=1):
    text = path.read_text()
    assert text.count(old) == count, str(path)
    path.write_text(text.replace(old, new))

if phase == 'tests':
    verify(preimages)
    path = root / 'backend/tests/integration/test_departure_redis_lua.py'
    replace(path, '@pytest.mark.parametrize("deadline_offset", [-1000, 0])', '@pytest.mark.parametrize("deadline_offset", [-1000, 0, None])')
    replace(path, '                raw["deadline_ms"] + deadline_offset,', '                None if deadline_offset is None else raw["deadline_ms"] + deadline_offset,')
    addition = '''\n\n@pytest.mark.asyncio
@pytest.mark.parametrize("deadline_offset", [-1000, 0, None])
async def test_real_lua_rejected_pass_deadline_preserves_state_and_retry(server, deadline_offset):
    _rooms, votes, r, g, _black, _white = await setup(server)
    raw = json.loads(await server.get(RedisKeyspace.room_game(r)))
    seconds, micros = await server.time()
    raw["deadline_ms"] = seconds * 1000 + micros // 1000 - 1
    await server.set(RedisKeyspace.room_game(r), json.dumps(raw))
    before = await votes.get(r)
    before_game = await server.get(RedisKeyspace.room_game(r))
    with pytest.raises(VoteRuleViolation, match="INVALID_NEXT_DEADLINE"):
        await votes.close_turn(
            CloseRuntimeTurn(
                r,
                "pass-invalid",
                g,
                1,
                before.state_version,
                None if deadline_offset is None else raw["deadline_ms"] + deadline_offset,
            )
        )
    assert await votes.get(r) == before, "rejected pass must not mutate runtime state"
    assert await server.get(RedisKeyspace.room_game(r)) == before_game
    result = await votes.close_turn(
        CloseRuntimeTurn(r, "pass-valid", g, 1, before.state_version, raw["deadline_ms"] + 5000)
    )
    assert result.snapshot.turn_no == 2
    assert result.snapshot.move_no == 0
    assert result.snapshot.turn_status is TurnStatus.VOTING
'''
    path.write_text(path.read_text() + addition)
    verify({str(path.relative_to(root)): postimages[str(path.relative_to(root))]})
elif phase == 'fix':
    path = root / 'backend/src/seokpan/persistence/redis/vote_scripts.py'
    replace(path, 'payload.next_deadline_ms == nil or payload.next_deadline_ms <= game.deadline_ms', 'payload.next_deadline_ms == nil or payload.next_deadline_ms == cjson.null\n          or payload.next_deadline_ms <= game.deadline_ms', 2)
    replace(path, 'version=10,', 'version=11,')
    for name in list(preimages)[2:]:
        replace(root / name, 'VOTE_MUTATION.version == 10', 'VOTE_MUTATION.version == 11')
    for name in list(postimages)[4:]:
        target = root / name
        assert not target.exists(), name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, target)
    verify(postimages)
    print(json.dumps(postimages, indent=2))
else:
    raise SystemExit('Unknown phase')
