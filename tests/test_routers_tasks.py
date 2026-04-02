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


class TestListTasks:
    def test_empty(self, client: TestClient) -> None:
        resp = client.get("/api/tasks")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_filter_by_project(
        self, client: TestClient, project_id: str, task_id: str
    ) -> None:
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
        resp = client.post(
            "/api/tasks",
            json={
                "project_id": project_id,
                "title": "New task",
                "description": "Details",
                "priority": 3,
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["title"] == "New task"
        assert data["status"] == "backlog"
        assert data["priority"] == 3

    def test_invalid_project(self, client: TestClient) -> None:
        resp = client.post(
            "/api/tasks",
            json={
                "project_id": "nonexistent",
                "title": "Bad task",
            },
        )
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

    def test_patch_status_rejected(self, client: TestClient, task_id: str) -> None:
        resp = client.patch(f"/api/tasks/{task_id}", json={"status": "active"})
        assert resp.status_code == 400
        assert "Use /activate" in resp.json()["detail"]


class TestActivateTask:
    def test_activate_backlog_task(self, client: TestClient, task_id: str) -> None:
        resp = client.post(f"/api/tasks/{task_id}/activate")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "active"
        assert data["current_stage"] == "spec"

    def test_activate_non_backlog_fails(self, client: TestClient, task_id: str) -> None:
        # First activate it
        client.post(f"/api/tasks/{task_id}/activate")
        # Try again — now it's active, not backlog
        resp = client.post(f"/api/tasks/{task_id}/activate")
        assert resp.status_code == 400

    def test_activate_not_found(self, client: TestClient) -> None:
        resp = client.post("/api/tasks/nonexistent/activate")
        assert resp.status_code == 404


class TestDeleteTask:
    def test_delete_backlog_task(self, client: TestClient, task_id: str) -> None:
        resp = client.delete(f"/api/tasks/{task_id}")
        assert resp.status_code == 204

        resp = client.get(f"/api/tasks/{task_id}")
        assert resp.status_code == 404

    def test_cannot_delete_non_backlog(
        self, client: TestClient, task_id: str, tmp_path
    ) -> None:
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
    def test_resume_needs_human(
        self, client: TestClient, task_id: str, tmp_path
    ) -> None:
        db_path = tmp_path / "test.db"
        conn = database.get_connection(str(db_path))
        try:
            database.update_task(
                conn, task_id, status="needs_human", current_stage="plan"
            )
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
            database.update_task(
                conn, task_id, status="active", current_stage="implement"
            )
        finally:
            conn.close()

        resp = client.post(f"/api/tasks/{task_id}/retry")
        assert resp.status_code == 200
        assert resp.json()["status"] == "active"

    def test_retry_backlog_fails(self, client: TestClient, task_id: str) -> None:
        resp = client.post(f"/api/tasks/{task_id}/retry")
        assert resp.status_code == 400


class TestCancelTask:
    """Tests for POST /api/tasks/{task_id}/cancel."""

    def _set_status(self, tmp_path, task_id: str, status: str, **kwargs) -> None:
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            database.update_task(conn, task_id, status=status, **kwargs)
        finally:
            conn.close()

    # --- AC 1 & 3: Successful cancellation from each cancellable state ---

    def test_cancel_task_from_backlog(
        self, client: TestClient, task_id: str
    ) -> None:
        resp = client.post(f"/api/tasks/{task_id}/cancel")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "cancelled"
        assert data["id"] == task_id

    def test_cancel_task_from_active(
        self, client: TestClient, task_id: str, tmp_path
    ) -> None:
        self._set_status(tmp_path, task_id, "active", current_stage="spec")
        resp = client.post(f"/api/tasks/{task_id}/cancel")
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"

    def test_cancel_task_from_paused(
        self, client: TestClient, task_id: str, tmp_path
    ) -> None:
        self._set_status(tmp_path, task_id, "paused", current_stage="spec")
        resp = client.post(f"/api/tasks/{task_id}/cancel")
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"

    def test_cancel_task_from_needs_human(
        self, client: TestClient, task_id: str, tmp_path
    ) -> None:
        self._set_status(tmp_path, task_id, "needs_human", current_stage="plan")
        resp = client.post(f"/api/tasks/{task_id}/cancel")
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"

    # --- AC 2: Optional reason field ---

    def test_cancel_task_with_reason(
        self, client: TestClient, task_id: str, tmp_path
    ) -> None:
        resp = client.post(
            f"/api/tasks/{task_id}/cancel",
            json={"reason": "No longer needed"},
        )
        assert resp.status_code == 200
        # Verify log contains the reason
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            logs = database.get_logs(conn, task_id=task_id)
            assert any(
                "No longer needed" in log["message"] for log in logs
            )
        finally:
            conn.close()

    def test_cancel_task_without_body(
        self, client: TestClient, task_id: str
    ) -> None:
        resp = client.post(f"/api/tasks/{task_id}/cancel")
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"

    def test_cancel_task_with_empty_body(
        self, client: TestClient, task_id: str
    ) -> None:
        resp = client.post(f"/api/tasks/{task_id}/cancel", json={})
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"

    # --- AC 4: Reject cancellation of terminal-state tasks ---

    def test_cancel_done_task_returns_400(
        self, client: TestClient, task_id: str, tmp_path
    ) -> None:
        self._set_status(tmp_path, task_id, "done")
        resp = client.post(f"/api/tasks/{task_id}/cancel")
        assert resp.status_code == 400
        assert "done" in resp.json()["detail"]

    def test_cancel_cancelled_task_returns_400(
        self, client: TestClient, task_id: str
    ) -> None:
        client.post(f"/api/tasks/{task_id}/cancel")
        resp = client.post(f"/api/tasks/{task_id}/cancel")
        assert resp.status_code == 400
        assert "cancelled" in resp.json()["detail"]

    def test_cancel_error_task_returns_400(
        self, client: TestClient, task_id: str, tmp_path
    ) -> None:
        self._set_status(tmp_path, task_id, "error")
        resp = client.post(f"/api/tasks/{task_id}/cancel")
        assert resp.status_code == 400

    # --- AC 5: Nonexistent task ---

    def test_cancel_nonexistent_task_returns_404(
        self, client: TestClient
    ) -> None:
        resp = client.post("/api/tasks/nonexistent-id/cancel")
        assert resp.status_code == 404

    # --- AC 6: Running stage run is marked as error ---

    def test_cancel_marks_running_stage_run_as_error(
        self, client: TestClient, task_id: str, tmp_path
    ) -> None:
        db_path = str(tmp_path / "test.db")
        conn = database.get_connection(db_path)
        try:
            database.update_task(conn, task_id, status="active", current_stage="spec")
            sr_id = database.insert_stage_run(
                conn, task_id=task_id, stage="spec", attempt=1, status="running"
            )
        finally:
            conn.close()

        resp = client.post(f"/api/tasks/{task_id}/cancel")
        assert resp.status_code == 200

        conn = database.get_connection(db_path)
        try:
            sr = database.get_stage_run(conn, sr_id)
            assert sr["status"] == "error"
            assert sr["error_message"] == "Task cancelled"
        finally:
            conn.close()

    # --- AC 7: Cancellation is logged ---

    def test_cancel_inserts_log_entry(
        self, client: TestClient, task_id: str, tmp_path
    ) -> None:
        client.post(f"/api/tasks/{task_id}/cancel")
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            logs = database.get_logs(conn, task_id=task_id)
            assert len(logs) >= 1
            log = logs[0]
            assert log["level"] == "info"
            assert "cancelled" in log["message"]
        finally:
            conn.close()

    def test_cancel_log_includes_reason(
        self, client: TestClient, task_id: str, tmp_path
    ) -> None:
        client.post(
            f"/api/tasks/{task_id}/cancel",
            json={"reason": "Duplicate work"},
        )
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            logs = database.get_logs(conn, task_id=task_id)
            assert any("Duplicate work" in log["message"] for log in logs)
        finally:
            conn.close()

    # --- AC 9: Engine skips cancelled tasks ---

    def test_get_next_queued_task_skips_cancelled(self, tmp_path, project_id: str) -> None:
        db_path = str(tmp_path / "test.db")
        conn = database.get_connection(db_path)
        try:
            tid = database.insert_task(
                conn, project_id=project_id, title="Cancelled task"
            )
            database.update_task(conn, tid, status="cancelled", current_stage="spec")
            database.insert_stage_run(
                conn, task_id=tid, stage="spec", attempt=1, status="queued"
            )
            result = database.get_next_queued_task(conn)
            assert result is None
        finally:
            conn.close()

    # --- AC 10: Engine does not activate cancelled tasks ---

    def test_activate_backlog_tasks_skips_cancelled(
        self, tmp_path, project_id: str
    ) -> None:
        from forge.config import Settings
        from forge.engine import PipelineEngine

        db_path = str(tmp_path / "test.db")
        conn = database.get_connection(db_path)
        try:
            tid = database.insert_task(
                conn, project_id=project_id, title="Was cancelled"
            )
            database.update_task(conn, tid, status="cancelled")

            engine = PipelineEngine(Settings(), db_path)
            engine._activate_backlog_tasks(conn)

            row = database.get_task(conn, tid)
            assert row["status"] == "cancelled"
        finally:
            conn.close()

    # --- AC 11: PATCH error message includes /cancel ---

    def test_patch_task_status_error_mentions_cancel(
        self, client: TestClient, task_id: str
    ) -> None:
        resp = client.patch(
            f"/api/tasks/{task_id}", json={"status": "cancelled"}
        )
        assert resp.status_code == 400
        assert "/cancel" in resp.json()["detail"]

    # --- AC 12: Resume and retry reject cancelled tasks ---

    def test_resume_cancelled_task_returns_400(
        self, client: TestClient, task_id: str
    ) -> None:
        client.post(f"/api/tasks/{task_id}/cancel")
        resp = client.post(f"/api/tasks/{task_id}/resume")
        assert resp.status_code == 400
        assert "cancelled" in resp.json()["detail"].lower()

    def test_retry_cancelled_task_returns_400(
        self, client: TestClient, task_id: str
    ) -> None:
        client.post(f"/api/tasks/{task_id}/cancel")
        resp = client.post(f"/api/tasks/{task_id}/retry")
        assert resp.status_code == 400
        assert "cancelled" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Flow field
# ---------------------------------------------------------------------------


class TestFlowField:
    def test_create_task_with_flow_quick(
        self, client: TestClient, project_id: str
    ) -> None:
        resp = client.post(
            "/api/tasks",
            json={
                "project_id": project_id,
                "title": "Quick task",
                "flow": "quick",
            },
        )
        assert resp.status_code == 201
        assert resp.json()["flow"] == "quick"

    def test_create_task_default_flow(
        self, client: TestClient, project_id: str
    ) -> None:
        resp = client.post(
            "/api/tasks",
            json={
                "project_id": project_id,
                "title": "Default flow task",
            },
        )
        assert resp.status_code == 201
        assert resp.json()["flow"] == "standard"

    def test_create_task_invalid_flow_returns_422(
        self, client: TestClient, project_id: str
    ) -> None:
        resp = client.post(
            "/api/tasks",
            json={
                "project_id": project_id,
                "title": "Bad flow",
                "flow": "invalid",
            },
        )
        assert resp.status_code == 422

    def test_activate_quick_flow_starts_at_implement(
        self, client: TestClient, project_id: str
    ) -> None:
        resp = client.post(
            "/api/tasks",
            json={
                "project_id": project_id,
                "title": "Quick activate",
                "flow": "quick",
            },
        )
        task_id = resp.json()["id"]
        resp = client.post(f"/api/tasks/{task_id}/activate")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "active"
        assert data["current_stage"] == "implement"

    def test_activate_standard_flow_starts_at_spec(
        self, client: TestClient, project_id: str
    ) -> None:
        resp = client.post(
            "/api/tasks",
            json={
                "project_id": project_id,
                "title": "Standard activate",
                "flow": "standard",
            },
        )
        task_id = resp.json()["id"]
        resp = client.post(f"/api/tasks/{task_id}/activate")
        assert resp.status_code == 200
        assert resp.json()["current_stage"] == "spec"


class TestBatchCreateTasks:
    def test_batch_create(self, client: TestClient, project_id: str) -> None:
        resp = client.post(
            "/api/tasks/batch",
            json={
                "tasks": [
                    {
                        "project_id": project_id,
                        "title": "Task A",
                        "flow": "standard",
                    },
                    {
                        "project_id": project_id,
                        "title": "Task B",
                        "flow": "quick",
                    },
                ]
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert len(data) == 2
        assert data[0]["title"] == "Task A"
        assert data[0]["flow"] == "standard"
        assert data[1]["title"] == "Task B"
        assert data[1]["flow"] == "quick"

    def test_batch_create_default_flow(
        self, client: TestClient, project_id: str
    ) -> None:
        resp = client.post(
            "/api/tasks/batch",
            json={
                "tasks": [
                    {
                        "project_id": project_id,
                        "title": "No flow specified",
                    },
                ]
            },
        )
        assert resp.status_code == 201
        assert resp.json()[0]["flow"] == "standard"

    def test_batch_create_invalid_flow(
        self, client: TestClient, project_id: str
    ) -> None:
        resp = client.post(
            "/api/tasks/batch",
            json={
                "tasks": [
                    {
                        "project_id": project_id,
                        "title": "Bad",
                        "flow": "bogus",
                    },
                ]
            },
        )
        assert resp.status_code == 422

    def test_batch_create_invalid_project(
        self, client: TestClient
    ) -> None:
        resp = client.post(
            "/api/tasks/batch",
            json={
                "tasks": [
                    {
                        "project_id": "nonexistent",
                        "title": "Bad project",
                    },
                ]
            },
        )
        assert resp.status_code == 404

    def test_batch_create_atomic_rollback(
        self, client: TestClient, project_id: str
    ) -> None:
        """If one task in a batch has an invalid project, no tasks are created."""
        resp = client.post(
            "/api/tasks/batch",
            json={
                "tasks": [
                    {
                        "project_id": project_id,
                        "title": "Good task",
                    },
                    {
                        "project_id": "nonexistent",
                        "title": "Bad project task",
                    },
                ]
            },
        )
        assert resp.status_code == 404
        # Verify the first task was NOT created (atomicity)
        all_tasks = client.get("/api/tasks").json()
        assert not any(t["title"] == "Good task" for t in all_tasks)


class TestUpdateTaskFlow:
    def test_update_flow_on_backlog_task(
        self, client: TestClient, project_id: str
    ) -> None:
        resp = client.post(
            "/api/tasks",
            json={
                "project_id": project_id,
                "title": "Backlog flow change",
                "flow": "standard",
            },
        )
        task_id = resp.json()["id"]
        resp = client.patch(
            f"/api/tasks/{task_id}",
            json={"flow": "quick"},
        )
        assert resp.status_code == 200
        assert resp.json()["flow"] == "quick"

    def test_update_flow_on_active_task_rejected(
        self, client: TestClient, project_id: str
    ) -> None:
        resp = client.post(
            "/api/tasks",
            json={
                "project_id": project_id,
                "title": "Active flow change",
                "flow": "standard",
            },
        )
        task_id = resp.json()["id"]
        client.post(f"/api/tasks/{task_id}/activate")
        resp = client.patch(
            f"/api/tasks/{task_id}",
            json={"flow": "quick"},
        )
        assert resp.status_code == 400
        assert "backlog" in resp.json()["detail"]

    def test_update_flow_invalid_value_rejected(
        self, client: TestClient, project_id: str
    ) -> None:
        resp = client.post(
            "/api/tasks",
            json={"project_id": project_id, "title": "Bad flow update"},
        )
        task_id = resp.json()["id"]
        resp = client.patch(
            f"/api/tasks/{task_id}",
            json={"flow": "bogus"},
        )
        assert resp.status_code == 422


class TestResetTaskFlowAware:
    def _make_quick_task_needs_human(
        self, client: TestClient, project_id: str, tmp_path
    ) -> str:
        resp = client.post(
            "/api/tasks",
            json={
                "project_id": project_id,
                "title": "Quick reset test",
                "flow": "quick",
            },
        )
        task_id = resp.json()["id"]
        client.post(f"/api/tasks/{task_id}/activate")
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            database.update_task(conn, task_id, status="needs_human")
        finally:
            conn.close()
        return task_id

    def test_reset_quick_task_rejects_spec_stage(
        self, client: TestClient, project_id: str, tmp_path
    ) -> None:
        task_id = self._make_quick_task_needs_human(client, project_id, tmp_path)
        resp = client.post(
            f"/api/tasks/{task_id}/reset",
            json={"from_stage": "spec"},
        )
        assert resp.status_code == 400
        assert "Invalid stage" in resp.json()["detail"]

    def test_reset_quick_task_rejects_plan_stage(
        self, client: TestClient, project_id: str, tmp_path
    ) -> None:
        task_id = self._make_quick_task_needs_human(client, project_id, tmp_path)
        resp = client.post(
            f"/api/tasks/{task_id}/reset",
            json={"from_stage": "plan"},
        )
        assert resp.status_code == 400
        assert "Invalid stage" in resp.json()["detail"]

    def test_reset_quick_task_allows_implement_stage(
        self, client: TestClient, project_id: str, tmp_path
    ) -> None:
        task_id = self._make_quick_task_needs_human(client, project_id, tmp_path)
        resp = client.post(
            f"/api/tasks/{task_id}/reset",
            json={"from_stage": "implement"},
        )
        assert resp.status_code == 200
        assert resp.json()["current_stage"] == "implement"

    def test_reset_quick_task_default_stage_is_implement(
        self, client: TestClient, project_id: str, tmp_path
    ) -> None:
        task_id = self._make_quick_task_needs_human(client, project_id, tmp_path)
        resp = client.post(f"/api/tasks/{task_id}/reset")
        assert resp.status_code == 200
        assert resp.json()["current_stage"] == "implement"

    def test_reset_standard_task_default_stage_is_spec(
        self, client: TestClient, project_id: str, tmp_path
    ) -> None:
        resp = client.post(
            "/api/tasks",
            json={
                "project_id": project_id,
                "title": "Standard reset test",
                "flow": "standard",
            },
        )
        task_id = resp.json()["id"]
        client.post(f"/api/tasks/{task_id}/activate")
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            database.update_task(conn, task_id, status="needs_human")
        finally:
            conn.close()
        resp = client.post(f"/api/tasks/{task_id}/reset")
        assert resp.status_code == 200
        assert resp.json()["current_stage"] == "spec"


# ---------------------------------------------------------------------------
# max_retries inherits from configured default
# ---------------------------------------------------------------------------


class TestMaxRetriesDefault:
    """New tasks should inherit max_retries from the configured default_max_retries."""

    @pytest.fixture()
    def _config_max_retries(self, tmp_path, monkeypatch):
        """Write a config.yaml with default_max_retries=6 and point CONFIG_PATH at it."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("engine:\n  default_max_retries: 6\n")
        monkeypatch.setattr("forge.routers.tasks.CONFIG_PATH", config_file)

    def test_create_task_uses_configured_default(
        self, client: TestClient, project_id: str, _config_max_retries
    ) -> None:
        resp = client.post(
            "/api/tasks",
            json={
                "project_id": project_id,
                "title": "Should get 6 retries",
            },
        )
        assert resp.status_code == 201
        assert resp.json()["max_retries"] == 6

    def test_create_task_explicit_max_retries_overrides_config(
        self, client: TestClient, project_id: str, _config_max_retries
    ) -> None:
        resp = client.post(
            "/api/tasks",
            json={
                "project_id": project_id,
                "title": "Explicit retries",
                "max_retries": 10,
            },
        )
        assert resp.status_code == 201
        assert resp.json()["max_retries"] == 10

    def test_batch_create_uses_configured_default(
        self, client: TestClient, project_id: str, _config_max_retries
    ) -> None:
        resp = client.post(
            "/api/tasks/batch",
            json={
                "tasks": [
                    {"project_id": project_id, "title": "Batch A"},
                    {"project_id": project_id, "title": "Batch B", "max_retries": 2},
                ]
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data[0]["max_retries"] == 6  # inherited from config
        assert data[1]["max_retries"] == 2  # explicit override

    def test_changing_config_affects_next_task(
        self, client: TestClient, project_id: str, tmp_path, monkeypatch
    ) -> None:
        """Changing the default and creating another task reflects the new value."""
        config_file = tmp_path / "config.yaml"
        # Start with default_max_retries=6
        config_file.write_text("engine:\n  default_max_retries: 6\n")
        monkeypatch.setattr("forge.routers.tasks.CONFIG_PATH", config_file)

        resp = client.post(
            "/api/tasks",
            json={"project_id": project_id, "title": "First task"},
        )
        assert resp.json()["max_retries"] == 6

        # Change the config to 9
        config_file.write_text("engine:\n  default_max_retries: 9\n")

        resp = client.post(
            "/api/tasks",
            json={"project_id": project_id, "title": "Second task"},
        )
        assert resp.json()["max_retries"] == 9
