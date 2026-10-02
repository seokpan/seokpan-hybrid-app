"""Both service roots must compose the complete lifecycle on their existing resources."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from seokpan.app import build_headless_services, build_production_services
from seokpan.game_lifecycle import (
    GameLifecycleBindings,
    build_memory_game_lifecycle,
    build_redis_game_lifecycle,
)
from seokpan.production import ProductionProviders
from seokpan.settings import Settings


@pytest.fixture(autouse=True)
def isolated_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEOKPAN_GAME_LIFECYCLE_MODE", raising=False)


def test_default_preserves_legacy_until_operator_rollout() -> None:
    assert Settings().game_lifecycle_mode == "legacy"


@pytest.mark.parametrize("mode", ["legacy", "captured"])
def test_mode_is_read_from_server_environment(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setenv("SEOKPAN_GAME_LIFECYCLE_MODE", mode)
    assert Settings().game_lifecycle_mode == mode


@pytest.mark.parametrize("mode", ["", "CAPTURED", "capture", "true", "unsafe", None, True])
def test_invalid_mode_is_not_silently_treated_as_legacy(mode) -> None:
    with pytest.raises(ValidationError):
        Settings(game_lifecycle_mode=mode)


@pytest.mark.parametrize("mask", range(1, 7))
def test_partial_lifecycle_binding_is_rejected(mask: int) -> None:
    values = [object() if mask & (1 << bit) else None for bit in range(3)]
    with pytest.raises(ValueError, match="INCOMPLETE_GAME_LIFECYCLE"):
        GameLifecycleBindings(*values)


def test_empty_and_complete_binding_are_allowed() -> None:
    assert GameLifecycleBindings().startup is None
    a, b, c = object(), object(), object()
    assert GameLifecycleBindings(a, b, c).completion is c


def production_providers() -> Mock:
    # Composition must not perform DB/Redis I/O; provider calls fail the test.
    providers = Mock(spec=ProductionProviders)
    for name in (
        "rooms",
        "votes",
        "games",
        "identities",
        "sessions",
        "statistics",
        "presence",
        "chat",
        "realtime",
        "room_passwords",
    ):
        setattr(providers, name, Mock(name=name))
    providers.redis_client = Mock()
    providers.redis_client.get = AsyncMock(side_effect=AssertionError("unexpected Redis I/O"))
    providers.redis_client.evalsha = AsyncMock(side_effect=AssertionError("unexpected Redis I/O"))
    providers.redis_client.scan_iter = Mock(side_effect=AssertionError("unexpected Redis scan"))
    providers.passwords = Mock()
    providers.passwords.hash.return_value = "dummy-password-hash"
    providers.tokens = Mock()
    providers.tokens.issue.return_value = "test-token"
    providers.clock = SimpleNamespace(now_ms=1000)
    return providers


def services_for(kind: str, mode: str):
    if kind == "memory":
        return build_headless_services(Settings(environment="test", game_lifecycle_mode=mode)), None
    providers = production_providers()
    return (
        build_production_services(
            Settings(environment="production", game_lifecycle_mode=mode),
            providers,
        ),
        providers,
    )


@pytest.mark.parametrize("kind", ["memory", "redis"])
def test_legacy_composes_no_captured_coordinators(kind: str) -> None:
    services, _ = services_for(kind, "legacy")
    game = services.game_api.games
    runner = services.turn_resolution
    assert game._captured_startup is None
    assert runner._captured_invalidation is None
    assert runner._captured_completion is None


@pytest.mark.parametrize("kind", ["memory", "redis"])
def test_captured_composes_all_coordinators_on_existing_resources(kind: str) -> None:
    services, providers = services_for(kind, "captured")
    game = services.game_api.games
    runner = services.turn_resolution
    start = game._captured_startup
    invalidation = runner._captured_invalidation
    completion = runner._captured_completion
    assert start is not None and invalidation is not None and completion is not None
    assert start._runtime is runner._rooms is completion._rooms
    assert start._votes is game._votes is runner._votes
    assert start._games is game._games is runner._games is invalidation._games is completion._games
    assert start._rooms is game._rooms
    assert start._clock is game._clock is runner._clock
    if kind == "memory":
        for adapter in (start._initializer, invalidation._closures, completion._records):
            assert adapter._rooms is runner._rooms
            assert adapter._votes is runner._votes
    else:
        assert start._runtime is providers.rooms
        assert start._initializer._votes is providers.votes
        for adapter in (start._initializer, invalidation._closures, completion._records):
            assert adapter._scripts._client is providers.redis_client
        providers.redis_client.get.assert_not_called()
        providers.redis_client.evalsha.assert_not_called()
        providers.redis_client.scan_iter.assert_not_called()


def test_separate_memory_apps_do_not_share_lifecycle_state() -> None:
    a, _ = services_for("memory", "captured")
    b, _ = services_for("memory", "captured")
    assert a.turn_resolution._rooms is not b.turn_resolution._rooms
    assert a.turn_resolution._votes is not b.turn_resolution._votes


@pytest.mark.parametrize("kind", ["memory", "redis"])
def test_constructor_failure_does_not_fall_back_to_legacy(monkeypatch, kind: str) -> None:
    path = f"seokpan.persistence.{kind}.start_completion"
    name = "InMemoryCapturedCompletionStore" if kind == "memory" else "RedisCapturedCompletionStore"
    monkeypatch.setattr(f"{path}.{name}", Mock(side_effect=RuntimeError("constructor failed")))
    with pytest.raises(RuntimeError, match="constructor failed"):
        services_for(kind, "captured")


@pytest.mark.parametrize("builder", [build_memory_game_lifecycle, build_redis_game_lifecycle])
def test_factory_rejects_invalid_mode_even_when_settings_validation_is_bypassed(builder) -> None:
    if builder is build_memory_game_lifecycle:
        kwargs = dict(rooms=None, votes=None, games=None, room_service=None, clock=None)
    else:
        kwargs = dict(providers=None, room_service=None)
    with pytest.raises(ValueError, match="INVALID_GAME_LIFECYCLE_MODE"):
        builder(mode="not-a-mode", **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["memory", "redis"])
async def test_service_calls_selected_startup_not_the_legacy_room_mutation(kind: str) -> None:
    services, _ = services_for(kind, "captured")
    game = services.game_api.games
    expected = object()
    game._captured_startup.start_game = AsyncMock(
        return_value=SimpleNamespace(
            snapshot=expected,
            initialized_now=False,
        )
    )
    game._rooms.start_game = AsyncMock(side_effect=AssertionError("legacy path called"))
    result = await game.start_game(
        session=object(),
        room_id="room",
        request_id="request",
        expected_state_version=1,
    )
    assert result is expected
    game._captured_startup.start_game.assert_awaited_once()
    game._rooms.start_game.assert_not_awaited()


@pytest.mark.asyncio
async def test_composed_runner_reconciles_without_playing_rooms() -> None:
    services, _ = services_for("memory", "captured")
    runner = services.turn_resolution
    runner._captured_completion.reconcile = AsyncMock(return_value=0)
    assert await runner.run_once() == ()
    runner._captured_completion.reconcile.assert_awaited_once_with(limit=100)


@pytest.mark.asyncio
async def test_composed_runner_preserves_reconciliation_cancellation() -> None:
    services, _ = services_for("memory", "captured")
    runner = services.turn_resolution
    runner._captured_completion.reconcile = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await runner.run_once()
