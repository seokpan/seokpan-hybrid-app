from __future__ import annotations

from dataclasses import replace

import pytest

from seokpan.identity.application.session import (
    SESSION_ABSOLUTE_TTL_MS,
    SESSION_IDLE_TTL_MS,
    CreateSession,
    SessionActorType,
    SessionRuleViolation,
    digest_opaque_token,
)

from .conftest import SessionHarness, digest, guest_command, member_command


@pytest.mark.asyncio
async def test_guest_session_lifecycle_uses_server_side_timestamps(
    session_harness: SessionHarness,
) -> None:
    record = await session_harness.adapter.create(guest_command())

    assert record.created_at_ms == session_harness.clock.now_ms
    assert record.last_activity_at_ms == session_harness.clock.now_ms
    assert record.absolute_expires_at_ms == 1_000 + SESSION_ABSOLUTE_TTL_MS
    assert await session_harness.adapter.get(digest("a")) == record


@pytest.mark.asyncio
async def test_idle_expiry_removes_authentication_authority(
    session_harness: SessionHarness,
) -> None:
    await session_harness.adapter.create(guest_command())
    session_harness.clock.advance(SESSION_IDLE_TTL_MS)

    assert await session_harness.adapter.get(digest("a")) is None
    assert await session_harness.adapter.touch(digest("a")) is None


@pytest.mark.asyncio
async def test_touch_extends_idle_but_never_absolute_expiry(
    session_harness: SessionHarness,
) -> None:
    initial = await session_harness.adapter.create(guest_command())
    session_harness.clock.advance(SESSION_IDLE_TTL_MS - 1)
    touched = await session_harness.adapter.touch(digest("a"))

    assert touched is not None
    assert touched.last_activity_at_ms == session_harness.clock.now_ms
    assert touched.absolute_expires_at_ms == initial.absolute_expires_at_ms

    session_harness.clock.advance(SESSION_ABSOLUTE_TTL_MS - (SESSION_IDLE_TTL_MS - 1))
    assert await session_harness.adapter.get(digest("a")) is None


@pytest.mark.asyncio
async def test_member_session_index_is_created_and_removed(
    session_harness: SessionHarness,
) -> None:
    await session_harness.adapter.create(member_command())
    assert session_harness.member_sessions("42") == (digest("b"),)

    assert await session_harness.adapter.revoke(digest("b")) is True
    assert await session_harness.adapter.revoke(digest("b")) is False
    assert session_harness.member_sessions("42") == ()


@pytest.mark.asyncio
async def test_rotation_replaces_guest_with_member_atomically(
    session_harness: SessionHarness,
) -> None:
    await session_harness.adapter.create(guest_command())
    replacement = member_command()

    rotated = await session_harness.adapter.rotate(
        previous_session_digest=digest("a"),
        replacement=replacement,
    )

    assert rotated.actor_type is SessionActorType.MEMBER
    assert await session_harness.adapter.get(digest("a")) is None
    assert await session_harness.adapter.get(digest("b")) == rotated
    assert session_harness.member_sessions("42") == (digest("b"),)


@pytest.mark.asyncio
async def test_failed_follow_up_can_restore_the_exact_previous_session(
    session_harness: SessionHarness,
) -> None:
    previous = await session_harness.adapter.create(guest_command())
    await session_harness.adapter.rotate(
        previous_session_digest=previous.session_digest,
        replacement=member_command(),
    )

    restored = await session_harness.adapter.restore_after_failed_rotation(
        failed_replacement_digest=digest("b"),
        previous=previous,
    )

    assert restored == previous
    assert await session_harness.adapter.get(digest("a")) == previous
    assert await session_harness.adapter.get(digest("b")) is None
    assert session_harness.member_sessions("42") == ()


@pytest.mark.asyncio
async def test_duplicate_create_and_invalid_rotation_do_not_replace_state(
    session_harness: SessionHarness,
) -> None:
    initial = await session_harness.adapter.create(guest_command())

    with pytest.raises(SessionRuleViolation, match="SESSION_ALREADY_EXISTS"):
        await session_harness.adapter.create(guest_command())
    with pytest.raises(SessionRuleViolation, match="SESSION_ROTATION_REQUIRES_NEW_DIGEST"):
        await session_harness.adapter.rotate(
            previous_session_digest=digest("a"),
            replacement=guest_command(),
        )

    assert await session_harness.adapter.get(digest("a")) == initial


@pytest.mark.asyncio
async def test_rotation_requires_a_live_previous_session(
    session_harness: SessionHarness,
) -> None:
    with pytest.raises(SessionRuleViolation, match="SESSION_NOT_FOUND"):
        await session_harness.adapter.rotate(
            previous_session_digest=digest("a"),
            replacement=member_command(),
        )

    assert await session_harness.adapter.get(digest("b")) is None


def test_token_digest_is_stable_and_plaintext_is_not_returned() -> None:
    token = "high-entropy-opaque-session-token"
    token_digest = digest_opaque_token(token)

    assert len(token_digest) == 64
    assert token not in token_digest
    assert digest_opaque_token(token) == token_digest


def test_invalid_digest_is_rejected_before_provider_access() -> None:
    with pytest.raises(SessionRuleViolation, match="INVALID_SESSION_DIGEST"):
        CreateSession(
            session_digest="raw-token",
            actor_type=SessionActorType.GUEST,
            actor_id="guest-1",
            csrf_digest=digest("f"),
            csrf_token="f" * 64,
        )


@pytest.mark.parametrize("token", ["", "wrong-csrf"])
def test_csrf_token_must_match_the_stored_digest(token: str) -> None:
    with pytest.raises(SessionRuleViolation, match="INVALID_CSRF_TOKEN"):
        replace(guest_command(), csrf_token=token)


@pytest.mark.asyncio
async def test_session_secrets_survive_touch_and_failed_rotation(
    session_harness: SessionHarness,
) -> None:
    command = guest_command()
    initial = await session_harness.adapter.create(command)
    assert initial.csrf_token == command.csrf_token
    assert command.csrf_token not in repr(command)
    assert initial.csrf_token not in repr(initial)
    session_harness.clock.advance(1234)
    touched = await session_harness.adapter.touch(command.session_digest)
    assert touched is not None and touched.csrf_token == initial.csrf_token
    with pytest.raises(SessionRuleViolation, match="UNSUPPORTED_SESSION_SCHEMA"):
        replace(touched, schema_version=1)
    with pytest.raises(SessionRuleViolation, match="INVALID_CSRF_TOKEN"):
        replace(touched, csrf_token="wrong-token")
    replacement = member_command()
    await session_harness.adapter.rotate(
        previous_session_digest=command.session_digest,
        replacement=replacement,
    )
    restored = await session_harness.adapter.restore_after_failed_rotation(
        failed_replacement_digest=replacement.session_digest,
        previous=touched,
    )
    assert restored == touched
    assert restored.csrf_token == initial.csrf_token != replacement.csrf_token
