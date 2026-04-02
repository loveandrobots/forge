"""Tests for forge.routers.dashboard."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from forge.main import app


@pytest.fixture()
def client():
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture()
def project_id(client: TestClient, tmp_path) -> str:
    resp = client.post(
        "/api/projects",
        json={
            "name": "TestProject",
            "repo_path": str(tmp_path),
        },
    )
    return resp.json()["id"]


@pytest.fixture()
def task_id(client: TestClient, project_id: str) -> str:
    resp = client.post(
        "/api/tasks",
        json={
            "project_id": project_id,
            "title": "Test task",
            "priority": 5,
        },
    )
    return resp.json()["id"]


class TestPipelineView:
    def test_pipeline_loads(self, client: TestClient) -> None:
        resp = client.get("/")
        assert resp.status_code == 200
        assert "Pipeline" in resp.text

    def test_cancelled_task_in_cancelled_column(
        self, client: TestClient, project_id: str, task_id: str
    ) -> None:
        # Cancel the task via the API
        resp = client.post(f"/api/tasks/{task_id}/cancel")
        assert resp.status_code == 200

        # Fetch the pipeline view
        resp = client.get("/")
        assert resp.status_code == 200
        html = resp.text

        # The "Cancelled" column header should exist
        assert "Cancelled" in html
        # The cancelled badge should be rendered
        assert "badge-cancelled" in html

    def test_cancelled_badge_rendering(
        self, client: TestClient, project_id: str, task_id: str
    ) -> None:
        # Cancel the task
        client.post(f"/api/tasks/{task_id}/cancel")

        resp = client.get("/")
        html = resp.text

        # Check that cancelled card and badge CSS classes are present
        assert "card-cancelled" in html
        assert "badge-cancelled" in html
        # The badge text should say "Cancelled"
        assert ">Cancelled</span>" in html
