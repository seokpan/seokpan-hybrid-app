from typing import Literal

from pydantic import Field

from seokpan.database_pool import RuntimePoolInputs


class Settings(RuntimePoolInputs):
    environment: Literal["local", "test", "development", "production"] = "local"
    # Switch only after the captured-lifecycle rollout gates; never per request.
    game_lifecycle_mode: Literal["legacy", "captured"] = "legacy"
    log_level: str = "INFO"
    instance_id: str = "local"
    identity_database_url: str | None = Field(default=None, repr=False)
    game_database_url: str | None = Field(default=None, repr=False)
    database_ca_file: str | None = Field(default=None, repr=False)
    redis_url: str | None = Field(default=None, repr=False)
    redis_expected_host: str | None = Field(default=None, repr=False)
    redis_expected_port: int | None = None
    redis_expected_database: int | None = None
    redis_auth_token: str | None = Field(default=None, repr=False)
    redis_ca_file: str | None = Field(default=None, repr=False)
    allowed_origins: tuple[str, ...] = ("http://localhost:5173",)
