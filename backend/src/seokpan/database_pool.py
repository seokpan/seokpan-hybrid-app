"""Explicit, bounded runtime pool inputs; no adopted operating values or database I/O."""

import math
import re
from typing import Self

from pydantic import BaseModel, field_validator, model_validator

from seokpan.connection_settings import DatabaseTargetSettings


class RuntimePoolOptions(BaseModel):
    """Validate only supplied pool values, without reading process environment."""

    database_pool_size: int | None = None
    database_max_overflow: int | None = None
    database_pool_timeout_seconds: float | None = None

    @field_validator("database_pool_size", "database_max_overflow", mode="before")
    @classmethod
    def bounded_integer(cls, value: object) -> int | None:
        if value is None:
            return None
        if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
            try:
                value = int(value)
            except ValueError:
                raise ValueError("database pool integer is invalid") from None
        if type(value) is not int or value < 0:
            raise ValueError("database pool integer must be nonnegative")
        return value

    @field_validator("database_pool_timeout_seconds", mode="before")
    @classmethod
    def finite_timeout(cls, value: object) -> float | None:
        if value is None:
            return None
        if isinstance(value, str) and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
            value = float(value)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("database pool timeout must be finite and positive")
        try:
            timeout = float(value)
        except (ValueError, OverflowError):
            raise ValueError("database pool timeout must be finite and positive") from None
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("database pool timeout must be finite and positive")
        return timeout

    @model_validator(mode="after")
    def explicit_bundle(self) -> Self:
        values = (
            self.database_pool_size,
            self.database_max_overflow,
            self.database_pool_timeout_seconds,
        )
        if all(value is None for value in values):
            return self
        if any(value is None for value in values):
            raise ValueError("database pool size, overflow and timeout must be supplied together")
        if self.database_pool_size == 0:
            raise ValueError("database pool size must be positive; unbounded pools are not allowed")
        return self

    def engine_options(self) -> dict[str, int | float]:
        if self.database_pool_size is None:
            return {}
        # explicit_bundle has validated the complete tuple.
        assert self.database_max_overflow is not None
        assert self.database_pool_timeout_seconds is not None
        return {
            "pool_size": self.database_pool_size,
            "max_overflow": self.database_max_overflow,
            "pool_timeout": self.database_pool_timeout_seconds,
        }


class RuntimePoolInputs(DatabaseTargetSettings, RuntimePoolOptions):
    """Load optional pool inputs with the existing SEOKPAN_ settings contract."""
