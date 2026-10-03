import pytest

from seokpan.settings import Settings


def test_settings_use_seokpan_environment_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEOKPAN_ENVIRONMENT", "test")
    monkeypatch.setenv("SEOKPAN_INSTANCE_ID", "test-instance")
    monkeypatch.setenv("SEOKPAN_IDENTITY_DATABASE_URL", "mysql+asyncmy://identity@db/stone_game")
    monkeypatch.setenv("SEOKPAN_GAME_DATABASE_URL", "mysql+asyncmy://game@db/stone_game")
    monkeypatch.setenv("SEOKPAN_REDIS_URL", "redis://redis.platform.svc.cluster.local:6379/0")

    settings = Settings()

    assert settings.environment == "test"
    assert settings.instance_id == "test-instance"
    assert settings.identity_database_url == "mysql+asyncmy://identity@db/stone_game"
    assert settings.game_database_url == "mysql+asyncmy://game@db/stone_game"
    assert settings.redis_url == "redis://redis.platform.svc.cluster.local:6379/0"


def test_runtime_settings_do_not_consume_migration_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SEOKPAN_MIGRATION_DATABASE_URL", "mysql+asyncmy://admin@db/stone_game")

    settings = Settings()

    assert "migration_database_url" not in type(settings).model_fields
