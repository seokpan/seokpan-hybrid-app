import re

from fastapi.testclient import TestClient
from prometheus_client import CONTENT_TYPE_LATEST

from seokpan.app import create_app


def test_metrics_expose_process_and_http_metrics() -> None:
    with TestClient(create_app()) as client:
        assert client.get("/health/live").status_code == 200

        response = client.get("/metrics")

        assert response.status_code == 200
        assert response.headers["content-type"] == CONTENT_TYPE_LATEST

        body = response.text

        assert "python_info" in body
        assert "process_cpu_seconds_total" in body

        assert re.search(
            r"seokpan_http_requests_total\{"
            r'[^}]*method="GET"'
            r'[^}]*route="/health/live"'
            r'[^}]*status="200"'
            r"[^}]*\}",
            body,
        )

        assert re.search(
            r"seokpan_http_request_duration_seconds_count\{"
            r'[^}]*method="GET"'
            r'[^}]*route="/health/live"'
            r'[^}]*status="200"'
            r"[^}]*\}",
            body,
        )


def test_metrics_use_route_templates_and_do_not_leak_request_values() -> None:
    game_id = "9a1c3b3e-53f6-4d2b-92f1-6b1af1e95231"
    secret_value = "do-not-export-this-value"

    with TestClient(create_app()) as client:
        response = client.get(
            f"/api/v1/games/{game_id}",
            params={"token": secret_value},
        )

        assert response.status_code == 401

        metrics = client.get("/metrics").text

        assert 'route="/api/v1/games/{game_id}"' in metrics
        assert game_id not in metrics
        assert secret_value not in metrics


def test_metrics_endpoint_is_not_self_instrumented_or_public_openapi() -> None:
    with TestClient(create_app()) as client:
        client.get("/metrics")
        client.get("/metrics")

        metrics = client.get("/metrics").text
        openapi = client.get("/api/openapi.json").json()

        assert 'route="/metrics"' not in metrics
        assert "/metrics" not in openapi["paths"]
