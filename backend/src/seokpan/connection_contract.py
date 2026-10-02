"""Exact hybrid destinations and CA validation without network access."""

from __future__ import annotations

import os
import re
import ssl
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


class ConnectionContractError(ValueError):
    """Failures contain no caller URL, endpoint, secret or filesystem path."""


def _host(value: str | None) -> str:
    if (
        not value
        or len(value) > 253
        or any(
            not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            for label in value.split(".")
        )
    ):
        raise ConnectionContractError("approved host is missing or invalid")
    return value.lower()


def _port(value: int | None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ConnectionContractError("approved port is missing or invalid")
    return value


@dataclass(frozen=True, slots=True)
class DatabaseTarget:
    host: str = field(repr=False)
    port: int
    database: str = field(repr=False)

    @classmethod
    def from_fields(
        cls, host: str | None, port: int | None, database: str | None
    ) -> DatabaseTarget:
        if not database or len(database) > 64 or not re.fullmatch(r"[A-Za-z0-9_]+", database):
            raise ConnectionContractError("approved database name is missing or invalid")
        return cls(_host(host), _port(port), database)


@dataclass(frozen=True, slots=True)
class RedisTarget:
    host: str = field(repr=False)
    port: int
    database: int


def parse_hybrid_redis_url(
    raw: str | None, host: str | None, port: int | None, database: int | None
) -> RedisTarget:
    approved_host, approved_port = _host(host), _port(port)
    if isinstance(database, bool) or not isinstance(database, int) or database != 0:
        raise ConnectionContractError("approved Redis database must be zero")
    if not raw or raw != raw.strip() or any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise ConnectionContractError("Redis URL is missing or malformed")
    try:
        parts = urlsplit(raw)
        actual_port = 6379 if parts.port is None else parts.port
    except ValueError:
        raise ConnectionContractError("Redis URL is malformed") from None
    if (
        parts.scheme != "rediss"
        or parts.hostname != approved_host
        or actual_port != approved_port
        or parts.path != "/0"
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise ConnectionContractError("Redis URL must match the approved TLS endpoint without auth")
    return RedisTarget(approved_host, approved_port, database)


def validated_redis_auth_token(value: str | None) -> str:
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ConnectionContractError("a separate Redis AUTH token is missing or invalid")
    return value


def strict_ca_context(ca_file: str | None) -> ssl.SSLContext:
    if "SSLKEYLOGFILE" in os.environ:
        raise ConnectionContractError("TLS key logging is not permitted")
    if not ca_file or not ca_file.strip():
        raise ConnectionContractError("an explicit Redis CA file is required")
    try:
        if not Path(ca_file).is_file():
            raise OSError
        # cafile prevents OS trust-store fallback; strict defaults include hostname checks.
        return ssl.create_default_context(cafile=ca_file)
    except (OSError, ValueError):
        raise ConnectionContractError("Redis CA file is unreadable or invalid") from None
