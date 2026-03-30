"""Tests for forge.routers.projects."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from forge import database
from forge.main import app


@pytest.fixture()
def client():
    """TestClient that skips the lifespan (no engine needed for router tests)."""
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture()
def sample_project(tmp_path) -> dict:
    return {
        "name": "TestProject",
        "repo_path": str(tmp_path),
        "default_branch": "main",
        "gate_dir": "gates",
    }


class TestListProjects:
    def test_empty(self, client: TestClient) -> None:
        resp = client.get("/api/projects")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_projects(self, client: TestClient, sample_project: dict) -> None:
        client.post("/api/projects", json=sample_project)
        resp = client.get("/api/projects")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "TestProject"


class TestCreateProject:
    def test_success(self, client: TestClient, sample_project: dict) -> None:
        resp = client.post("/api/projects", json=sample_project)
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "TestProject"
        assert "id" in data
        assert data["default_branch"] == "main"

    def test_duplicate_name(self, client: TestClient, sample_project: dict) -> None:
        client.post("/api/projects", json=sample_project)
        resp = client.post("/api/projects", json=sample_project)
        assert resp.status_code == 409

    def test_invalid_repo_path(self, client: TestClient) -> None:
        resp = client.post(
            "/api/projects",
            json={
                "name": "Bad",
                "repo_path": "/nonexistent/path/xyz",
            },
        )
        assert resp.status_code == 400


class TestGetProject:
    def test_found(self, client: TestClient, sample_project: dict) -> None:
        create_resp = client.post("/api/projects", json=sample_project)
        pid = create_resp.json()["id"]
        resp = client.get(f"/api/projects/{pid}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "TestProject"

    def test_not_found(self, client: TestClient) -> None:
        resp = client.get("/api/projects/nonexistent")
        assert resp.status_code == 404


class TestUpdateProject:
    def test_update_name(self, client: TestClient, sample_project: dict) -> None:
        create_resp = client.post("/api/projects", json=sample_project)
        pid = create_resp.json()["id"]
        resp = client.patch(f"/api/projects/{pid}", json={"name": "Renamed"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Renamed"

    def test_not_found(self, client: TestClient) -> None:
        resp = client.patch("/api/projects/nonexistent", json={"name": "X"})
        assert resp.status_code == 404

    def test_empty_update(self, client: TestClient, sample_project: dict) -> None:
        create_resp = client.post("/api/projects", json=sample_project)
        pid = create_resp.json()["id"]
        resp = client.patch(f"/api/projects/{pid}", json={})
        assert resp.status_code == 200
