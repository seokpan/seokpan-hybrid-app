"""Central application logging configuration."""

from __future__ import annotations

import json
import logging
import os
import sys
import traceback
from datetime import UTC, datetime
from typing import Any

from seokpan.settings import Settings

_CONTEXT_FIELDS = (
    "request_id",
    "room_id",
    "game_id",
    "turn_no",
    "participant_id",
    "error_code",
    "provider_cause",
    "status",
)

_QUIET_ACCESS_PATHS = {
    "/health/live",
    "/health/ready",
    "/metrics",
}


def _source_path(value: str) -> str:
    """Return a stable package-relative source path when possible."""

    normalized = value.replace("\\", "/")

    if normalized.startswith("seokpan/"):
        return normalized

    marker = "/seokpan/"
    if marker in normalized:
        return f"seokpan/{normalized.rsplit(marker, 1)[1]}"

    return normalized.rsplit("/", 1)[-1]


class _JsonFormatter(logging.Formatter):
    def __init__(self, *, instance_id: str) -> None:
        super().__init__()
        self._instance_id = instance_id

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": (
                datetime.fromtimestamp(record.created, tz=UTC)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            ),
            "level": record.levelname,
            "event": getattr(record, "event", "application.log"),
            "logger": record.name,
            "file": _source_path(record.pathname),
            "line": record.lineno,
            "function": record.funcName,
            "instance_id": getattr(record, "instance_id", self._instance_id),
            "message": record.getMessage(),
        }

        for field in _CONTEXT_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value

        if record.exc_info is not None:
            exc_type, _exc_value, exc_tb = record.exc_info
            frames = (
                []
                if exc_tb is None
                else [
                    {
                        "file": _source_path(frame.filename),
                        "line": frame.lineno,
                        "function": frame.name,
                    }
                    for frame in traceback.extract_tb(exc_tb)
                ]
            )
            payload["exception"] = {
                "type": None if exc_type is None else exc_type.__name__,
                "frames": frames,
            }

        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )


class _SuccessfulProbeAccessFilter(logging.Filter):
    """Hide successful probe noise while preserving failures."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) < 5:
            return True

        path = str(args[2]).split("?", maxsplit=1)[0]

        raw_status_code = args[4]
        if not isinstance(raw_status_code, int | str):
            return True

        try:
            status_code = int(raw_status_code)
        except ValueError:
            return True

        return not (path in _QUIET_ACCESS_PATHS and status_code < 400)


def _resolve_level(value: str) -> int:
    normalized = value.strip().upper()
    level = logging.getLevelNamesMapping().get(normalized)
    if not isinstance(level, int):
        raise ValueError("INVALID_LOG_LEVEL")
    return level


def configure_logging(settings: Settings) -> None:
    """Configure one structured stdout contract for Seokpan application logs."""

    instance_id = settings.instance_id.strip() or os.getenv("HOSTNAME", "").strip() or "unknown"
    level = _resolve_level(settings.log_level)

    application_logger = logging.getLogger("seokpan")
    application_logger.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter(instance_id=instance_id))
    handler.setLevel(level)

    application_logger.addHandler(handler)
    application_logger.setLevel(level)
    application_logger.propagate = False

    access_logger = logging.getLogger("uvicorn.access")
    for existing in tuple(access_logger.filters):
        if isinstance(existing, _SuccessfulProbeAccessFilter):
            access_logger.removeFilter(existing)
    access_logger.addFilter(_SuccessfulProbeAccessFilter())
