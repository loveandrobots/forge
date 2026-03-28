"""Tests for forge.routers.tasks."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from forge import database
from forge.main import app


@pytest.fixture()
def client():
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture()
def project_id(client: TestClient, tmp_path) -> str:
    resp = client.post("/api/projects", json={
        "name": "TestProject",
        "repo_path": str(tmp_path),
    })
    return resp.json()["id"]


@pytest.fixture()
def task_id(client: TestClient, project_id: str) -> str:
    resp = client.post("/api/tasks", json={
        "project_id": project_id,
        "title": "Test task",
        "priority": 5,
    })
    return resp.json()["id"]


class TestListTasks:
    def test_empty(self, client: TestClient) -> None:
        resp = client.get("/api/tasks")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_filter_by_project(self, client: TestClient, project_id: str, task_id: str) -> None:
        resp = client.get(f"/api/tasks?project_id={project_id}")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_filter_by_status(self, client: TestClient, task_id: str) -> None:
        resp = client.get("/api/tasks?status=backlog")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

        resp = client.get("/api/tasks?status=active")
        assert resp.status_code == 200
        assert resp.json() == []


class TestCreateTask:
    def test_success(self, client: TestClient, project_id: str) -> None:
        resp = client.post("/api/tasks", json={
            "project_id": project_id,
            "title": "New task",
            "description": "Details",
            "priority": 3,
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["title"] == "New task"
        assert data["status"] == "backlog"
        assert data["priority"] == 3

    def test_invalid_project(self, client: TestClient) -> None:
        resp = client.post("/api/tasks", json={
            "project_id": "nonexistent",
            "title": "Bad task",
        })
        assert resp.status_code == 404


class TestGetTask:
    def test_found(self, client: TestClient, task_id: str) -> None:
        resp = client.get(f"/api/tasks/{task_id}")
        assert resp.status_code == 200
        assert resp.json()["title"] == "Test task"

    def test_not_found(self, client: TestClient) -> None:
        resp = client.get("/api/tasks/nonexistent")
        assert resp.status_code == 404


class TestUpdateTask:
    def test_update_title(self, client: TestClient, task_id: str) -> None:
        resp = client.patch(f"/api/tasks/{task_id}", json={"title": "Updated"})
        assert resp.status_code == 200
        assert resp.json()["title"] == "Updated"

    def test_not_found(self, client: TestClient) -> None:
        resp = client.patch("/api/tasks/nonexistent", json={"title": "X"})
        assert resp.status_code == 404


class TestDeleteTask:
    def test_delete_backlog_task(self, client: TestClient, task_id: str) -> None:
        resp = client.delete(f"/api/tasks/{task_id}")
        assert resp.status_code == 204

        resp = client.get(f"/api/tasks/{task_id}")
        assert resp.status_code == 404

    def test_cannot_delete_non_backlog(self, client: TestClient, task_id: str, tmp_path) -> None:
        # Manually set task to active status
        db_path = tmp_path / "test.db"
        conn = database.get_connection(str(db_path))
        try:
            database.update_task(conn, task_id, status="active")
        finally:
            conn.close()
        resp = client.delete(f"/api/tasks/{task_id}")
        assert resp.status_code == 400

    def test_delete_not_found(self, client: TestClient) -> None:
        resp = client.delete("/api/tasks/nonexistent")
        assert resp.status_code == 404


class TestResumeTask:
    def test_resume_needs_human(self, client: TestClient, task_id: str, tmp_path) -> None:
        db_path = tmp_path / "test.db"
        conn = database.get_connection(str(db_path))
        try:
            database.update_task(conn, task_id, status="needs_human", current_stage="plan")
        finally:
            conn.close()

        resp = client.post(f"/api/tasks/{task_id}/resume")
        assert resp.status_code == 200
        assert resp.json()["status"] == "active"

    def test_resume_non_needs_human(self, client: TestClient, task_id: str) -> None:
        resp = client.post(f"/api/tasks/{task_id}/resume")
        assert resp.status_code == 400


class TestPauseTask:
    def test_pause_active(self, client: TestClient, task_id: str, tmp_path) -> None:
        db_path = tmp_path / "test.db"
        conn = database.get_connection(str(db_path))
        try:
            database.update_task(conn, task_id, status="active")
        finally:
            conn.close()

        resp = client.post(f"/api/tasks/{task_id}/pause")
        assert resp.status_code == 200
        assert resp.json()["status"] == "paused"

    def test_pause_non_active(self, client: TestClient, task_id: str) -> None:
        resp = client.post(f"/api/tasks/{task_id}/pause")
        assert resp.status_code == 400


class TestRetryTask:
    def test_retry_active(self, client: TestClient, task_id: str, tmp_path) -> None:
        db_path = tmp_path / "test.db"
        conn = database.get_connection(str(db_path))
        try:
            database.update_task(conn, task_id, status="active", current_stage="implement")
        finally:
            conn.close()

        resp = client.post(f"/api/tasks/{task_id}/retry")
        assert resp.status_code == 200
        assert resp.json()["status"] == "active"

    def test_retry_backlog_fails(self, client: TestClient, task_id: str) -> None:
        resp = client.post(f"/api/tasks/{task_id}/retry")
        assert resp.status_code == 400
