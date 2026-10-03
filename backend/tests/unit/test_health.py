from fastapi.testclient import TestClient

from seokpan.app import create_app
from seokpan.health import RuntimeReadiness


def test_health_endpoints_report_initial_process_state() -> None:
    with TestClient(create_app()) as client:
        assert client.get("/health/startup").json() == {"status": "started"}
        assert client.get("/health/live").json() == {"status": "alive"}
        assert client.get("/health/ready").json() == {"status": "ready"}


def test_provider_readiness_is_fail_closed_and_recovers() -> None:
    readiness = RuntimeReadiness()
    with TestClient(create_app(readiness=readiness)) as client:
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.headers["Cache-Control"] == "no-store"
        assert response.json() == {"status": "not_ready"}

        readiness.mark_ready()
        assert client.get("/health/ready").json() == {"status": "ready"}

        readiness.mark_not_ready()
        assert client.get("/health/ready").status_code == 503
