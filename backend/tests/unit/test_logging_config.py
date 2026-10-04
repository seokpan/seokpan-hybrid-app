from __future__ import annotations

import json
import logging

import pytest

from seokpan.logging_config import (
    _source_path,
    _SuccessfulProbeAccessFilter,
    configure_logging,
)
from seokpan.settings import Settings


def _settings(*, level: str = "INFO", instance_id: str = "test-backend") -> Settings:
    return Settings(
        environment="test",
        log_level=level,
        instance_id=instance_id,
    )


def test_configured_application_log_is_structured(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(_settings())

    logging.getLogger("seokpan.test").error(
        "Turn resolution failed",
        extra={
            "event": "turn.runner.iteration_failed",
            "room_id": "room-1",
            "game_id": "game-1",
            "turn_no": 7,
            "error_code": "TEST_FAILURE",
            "provider_cause": "redis_connection",
        },
    )

    output = capsys.readouterr().out.strip()
    payload = json.loads(output)

    assert payload["level"] == "ERROR"
    assert payload["event"] == "turn.runner.iteration_failed"
    assert payload["logger"] == "seokpan.test"
    assert payload["file"] == "test_logging_config.py"
    assert isinstance(payload["line"], int)
    assert payload["function"] == "test_configured_application_log_is_structured"
    assert payload["instance_id"] == "test-backend"
    assert payload["room_id"] == "room-1"
    assert payload["game_id"] == "game-1"
    assert payload["turn_no"] == 7
    assert payload["error_code"] == "TEST_FAILURE"
    assert payload["provider_cause"] == "redis_connection"


def test_exception_records_type_and_frames_without_exception_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(_settings())

    try:
        raise RuntimeError("credential-like-secret-value")
    except RuntimeError:
        logging.getLogger("seokpan.test").exception(
            "Background operation failed",
            extra={"event": "background.operation.failed"},
        )

    payload = json.loads(capsys.readouterr().out.strip())

    assert payload["message"] == "Background operation failed"
    assert payload["exception"]["type"] == "RuntimeError"
    assert payload["exception"]["frames"]
    assert "credential-like-secret-value" not in json.dumps(payload)


def test_log_level_controls_application_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(_settings(level="ERROR"))
    logger = logging.getLogger("seokpan.test")

    logger.warning("hidden warning")
    assert capsys.readouterr().out == ""

    logger.error(
        "visible error",
        extra={"event": "test.visible_error"},
    )
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["level"] == "ERROR"


def test_instance_id_falls_back_to_hostname(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HOSTNAME", "backend-pod-123")
    configure_logging(_settings(instance_id=""))

    logging.getLogger("seokpan.test").info(
        "Started",
        extra={"event": "application.started"},
    )

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["instance_id"] == "backend-pod-123"


@pytest.mark.parametrize(
    ("path", "status_code", "expected"),
    [
        ("/health/live", 200, False),
        ("/health/ready", 200, False),
        ("/metrics", 200, False),
        ("/health/ready", 503, True),
        ("/api/v1/rooms", 200, True),
    ],
)
def test_successful_probe_access_filter(
    path: str,
    status_code: int,
    expected: bool,
) -> None:
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:1234", "GET", path, "1.1", status_code),
        exc_info=None,
    )

    assert _SuccessfulProbeAccessFilter().filter(record) is expected


def test_invalid_log_level_is_rejected() -> None:
    with pytest.raises(ValueError, match="INVALID_LOG_LEVEL"):
        configure_logging(_settings(level="LOUD"))


def test_source_path_prefers_package_relative_path() -> None:
    assert (
        _source_path("/opt/app/src/seokpan/game/application/service.py")
        == "seokpan/game/application/service.py"
    )
    assert _source_path("/tmp/test_logging_config.py") == "test_logging_config.py"
