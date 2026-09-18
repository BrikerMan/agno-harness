"""Smoke-test the copied demo backend against the original frontend contract."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

DEMO_BACKEND = Path(__file__).resolve().parents[1] / "examples" / "demo" / "backend"

if not (DEMO_BACKEND / "app" / "main.py").is_file():
    pytest.skip(
        "examples/demo is local-only and not in the published tree", allow_module_level=True
    )


@pytest.fixture(scope="module")
def demo_client() -> TestClient:
    if str(DEMO_BACKEND) not in sys.path:
        sys.path.insert(0, str(DEMO_BACKEND))
    from app.main import app

    with TestClient(app) as client:
        yield client


def test_demo_health_contract(demo_client: TestClient) -> None:
    response = demo_client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert "resumeMode" in body
    assert "sequencerMode" in body


def test_demo_scenarios_catalog(demo_client: TestClient) -> None:
    response = demo_client.get("/scenarios")
    assert response.status_code == 200
    body = response.json()
    ids = {item["id"] for item in body["scenarios"]}
    assert {
        "plain",
        "reasoning",
        "single-tool",
        "parallel-tools",
        "hidden-tool",
        "subagent",
        "hitl-confirm",
        "hitl-input",
        "hitl-frontend-tool",
        "hitl-feedback",
    }.issubset(ids)
    assert "groups" in body


def test_demo_settings_and_agui_routes(demo_client: TestClient) -> None:
    settings = demo_client.get("/settings")
    assert settings.status_code == 200

    threads = demo_client.get("/threads", headers={"X-Demo-User": "demo-user"})
    assert threads.status_code == 200
