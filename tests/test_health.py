"""Health endpoint smoke test."""

from __future__ import annotations

from fastapi.testclient import TestClient

from main import app


def test_health_returns_phase_0() -> None:
    client = TestClient(app)
    resp = client.get("/api/core/health")
    assert resp.status_code == 200
    assert resp.json() == {"phase": 0}
