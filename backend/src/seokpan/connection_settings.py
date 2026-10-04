"""Shared target contract for application and one-shot Migration settings."""

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseTargetSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SEOKPAN_",
        extra="ignore",
        hide_input_in_errors=True,
    )

    connection_profile: Literal["legacy", "cloud", "lab", "recovery"] = "legacy"
    database_expected_host: str | None = Field(default=None, repr=False)
    database_expected_port: int | None = None
    database_expected_name: str | None = Field(default=None, repr=False)
