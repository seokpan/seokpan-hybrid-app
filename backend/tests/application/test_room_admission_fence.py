"""Admission coordinator boundaries; real Redis Lua is a separate Provider gate."""

import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from seokpan.identity.application.session import SessionRuleViolation
from seokpan.persistence.memory.room_admission import SessionAdmissionMemoryRoomAdapter
from seokpan.persistence.redis.room_admission import (
    ACQUIRE_ADMISSION,
    ADMITTED_ROOM_MUTATION,
    RELEASE_ADMISSION,
    SessionAdmissionRedisRoomAdapter,
    admission_key,
)
from seokpan.persistence.redis.room_scripts import ROOM_MUTATION
from seokpan.room.domain import RoomRuleViolation

DIGEST = "a" * 64
OPERATIONS = ("create", "join", "change_identity", "connect")


def payload(operation):
    result = {"session_digest": DIGEST, "actor_type": "MEMBER"}
    result["owner_id" if operation == "create" else "participant_id"] = "p1"
    return result


def adapter(*, existing=None, acquired=1, mutation_error=None, release_error=None):
    value = object.__new__(SessionAdmissionRedisRoomAdapter)
    calls = []

    async def execute(script, *, keys, args):
        calls.append((script, keys, args))
        if script is ACQUIRE_ADMISSION:
            return acquired
        if script is RELEASE_ADMISSION:
            if release_error is not None:
                raise release_error
            return 1
        if isinstance(mutation_error, BaseException):
            raise mutation_error
        if mutation_error is not None:
            return json.dumps({"ok": False, "error": mutation_error})
        return json.dumps({"ok": True, "snapshot": "fixture"})

    value._scripts = SimpleNamespace(execute=AsyncMock(side_effect=execute))
    value._find_binding = AsyncMock(return_value=existing)
    value._mutation_keys = Mock(return_value=tuple(f"room-key-{n}" for n in range(10)))
    value._result = json.loads
    value._mutation_result = lambda result: result
    return value, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OPERATIONS)
async def test_success_declares_session_and_lease_without_changing_request_fingerprint(operation):
    value, calls = adapter()
    supplied = payload(operation)
    result = await value._mutate("r1", "request", operation, supplied)
    assert result["ok"]
    assert [call[0] for call in calls] == [
        ACQUIRE_ADMISSION,
        ADMITTED_ROOM_MUTATION,
        RELEASE_ADMISSION,
    ]
    lease = admission_key(DIGEST)
    token = calls[0][2][0]
    assert len(token) == 32
    assert calls[1][1][-2:] == (lease, f"stone:v1:session:{DIGEST}")
    assert len(calls[1][1]) == 12
    assert calls[1][2][-1] == token
    encoded_payload = json.loads(calls[1][2][6])
    assert "admission_token" not in encoded_payload
    assert encoded_payload["session_digest"] == DIGEST
    assert calls[2][1:] == ((lease,), (token,))
    assert "schema_version" not in supplied


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OPERATIONS)
async def test_busy_lease_cannot_read_or_write_membership(operation):
    value, calls = adapter(acquired=0)
    with pytest.raises(SessionRuleViolation, match="ROOM_ADMISSION_BUSY"):
        await value._mutate("r1", "request", operation, payload(operation))
    value._find_binding.assert_not_awaited()
    assert len(calls) == 1  # Never release a lease which this request did not acquire.


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OPERATIONS)
async def test_another_room_membership_prevents_room_write(operation):
    value, calls = adapter(existing=SimpleNamespace(room_id="r2", participant_id="p2"))
    with pytest.raises(RoomRuleViolation, match="SESSION_ALREADY_IN_ROOM"):
        await value._mutate("r1", "request", operation, payload(operation))
    assert [call[0] for call in calls] == [ACQUIRE_ADMISSION, RELEASE_ADMISSION]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OPERATIONS)
async def test_same_target_can_reach_underlying_idempotency_check(operation):
    value, calls = adapter(existing=SimpleNamespace(room_id="r1", participant_id="p1"))
    await value._mutate("r1", "request", operation, payload(operation))
    assert calls[1][0] is ADMITTED_ROOM_MUTATION


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        "ROOM_ADMISSION_EXPIRED",
        "ROOM_ADMISSION_SESSION_INVALID",
        "SESSION_NOT_FOUND",
    ],
)
async def test_atomic_fence_and_session_rejections_are_not_retried_as_legacy(code):
    value, calls = adapter(mutation_error=code)
    with pytest.raises(SessionRuleViolation, match=code):
        await value._mutate("r1", "request", "join", payload("join"))
    assert len(calls) == 3
    assert calls[1][0] is ADMITTED_ROOM_MUTATION


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        "ROOM_CAPACITY_REACHED",
        "ROOM_PASSWORD_INVALID",
        "STATE_VERSION_CONFLICT",
    ],
)
async def test_room_domain_rejections_remain_domain_rejections(code):
    value, calls = adapter(mutation_error=code)
    with pytest.raises(RoomRuleViolation, match=code):
        await value._mutate("r1", "request", "join", payload("join"))
    assert calls[-1][0] is RELEASE_ADMISSION


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["read", "write"])
async def test_uncertain_provider_failure_is_not_replayed_or_hidden(stage):
    failure = RuntimeError("synthetic unavailable")
    value, calls = adapter(mutation_error=failure if stage == "write" else None)
    if stage == "read":
        value._find_binding.side_effect = failure
    with pytest.raises(RuntimeError, match="synthetic unavailable"):
        await value._mutate("r1", "request", "join", payload("join"))
    assert calls[-1][0] is RELEASE_ADMISSION
    assert len([call for call in calls if call[0] is ADMITTED_ROOM_MUTATION]) <= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["read", "write", "release"])
async def test_cancellation_propagates(stage):
    value, calls = adapter(
        mutation_error=asyncio.CancelledError() if stage == "write" else None,
        release_error=asyncio.CancelledError() if stage == "release" else None,
    )
    if stage == "read":
        value._find_binding.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await value._mutate("r1", "request", "join", payload("join"))
    assert calls[-1][0] is RELEASE_ADMISSION


@pytest.mark.asyncio
async def test_release_failure_does_not_mask_a_committed_result():
    value, _ = adapter(release_error=RuntimeError("synthetic release failure"))
    assert (await value._mutate("r1", "request", "join", payload("join")))["ok"]


@pytest.mark.asyncio
async def test_release_failure_does_not_mask_original_rejection():
    value, _ = adapter(
        mutation_error="ROOM_CAPACITY_REACHED",
        release_error=RuntimeError("release"),
    )
    with pytest.raises(RoomRuleViolation, match="ROOM_CAPACITY_REACHED"):
        await value._mutate("r1", "request", "join", payload("join"))


def test_admitted_script_inherits_current_room_transitions():
    assert ADMITTED_ROOM_MUTATION.source.endswith(ROOM_MUTATION.source)
    assert "ARGV[8]" in ADMITTED_ROOM_MUTATION.source
    assert ADMITTED_ROOM_MUTATION.source.index("ROOM_ADMISSION_EXPIRED") < (
        ADMITTED_ROOM_MUTATION.source.index(ROOM_MUTATION.source)
    )


@pytest.mark.parametrize("bad", ["", "a" * 63, "A" * 64, "{" + "a" * 63])
def test_invalid_session_digest_is_not_used_as_a_key(bad):
    with pytest.raises(SessionRuleViolation):
        admission_key(bad)


@pytest.mark.parametrize("connected", [True, False])
def test_memory_counts_disconnected_bindings_until_actual_removal(connected):
    value = object.__new__(SessionAdmissionMemoryRoomAdapter)
    value._purge_expired = lambda: None
    value._rooms = {
        "r1": SimpleNamespace(
            connections={
                "p1": SimpleNamespace(session_digest=DIGEST, connected=connected),
            }
        )
    }
    with pytest.raises(RoomRuleViolation, match="SESSION_ALREADY_IN_ROOM"):
        value._require_single_binding(DIGEST, "r2", "p2")
    # Leave/kick/expiry removes the real connection; there is no second index.
    value._rooms["r1"].connections.clear()
    value._require_single_binding(DIGEST, "r2", "p2")


def test_memory_closure_needs_no_separate_reservation_release():
    value = object.__new__(SessionAdmissionMemoryRoomAdapter)
    value._purge_expired = lambda: None
    value._rooms = {
        "r1": SimpleNamespace(
            connections={
                "p1": SimpleNamespace(session_digest=DIGEST),
            }
        )
    }
    value._rooms.clear()
    value._require_single_binding(DIGEST, "r2", "p2")


def test_service_factories_select_guarded_adapters_without_new_resource_ownership():
    root = Path(__file__).resolve().parents[2] / "src" / "seokpan"
    for path, module, name, alias in (
        (
            root / "app.py",
            "seokpan.persistence.memory.room_admission",
            "SessionAdmissionMemoryRoomAdapter",
            "InMemoryRoomRuntimeAdapter",
        ),
        (
            root / "production.py",
            "seokpan.persistence.redis.room_admission",
            "SessionAdmissionRedisRoomAdapter",
            "RedisRoomRuntimeAdapter",
        ),
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = [node for node in tree.body if isinstance(node, ast.ImportFrom)]
        assert any(
            node.module == module
            and any(item.name == name and item.asname == alias for item in node.names)
            for node in imports
        )
        assert any(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == alias
            for node in ast.walk(tree)
        )
