"""Tests for forge.routers.dashboard — dashboard page routes."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from forge import database
from forge.main import app


@pytest.fixture()
def client():
    """TestClient that skips the lifespan (no engine needed for dashboard tests)."""
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture()
def sample_project(tmp_path):
    """Insert a project directly and return its id + data."""
    conn = database.get_connection(str(tmp_path / "test.db"))
    try:
        pid = database.insert_project(
            conn,
            name="TestProject",
            repo_path=str(tmp_path),
        )
        return {"id": pid, "name": "TestProject", "repo_path": str(tmp_path)}
    finally:
        conn.close()


@pytest.fixture()
def sample_task(tmp_path, sample_project):
    """Insert a backlog task and return its id."""
    conn = database.get_connection(str(tmp_path / "test.db"))
    try:
        tid = database.insert_task(
            conn,
            project_id=sample_project["id"],
            title="Test Task",
            description="A task for testing",
            priority=5,
        )
        return tid
    finally:
        conn.close()


@pytest.fixture()
def active_task_with_runs(tmp_path, sample_project):
    """Insert an active task with stage runs."""
    conn = database.get_connection(str(tmp_path / "test.db"))
    try:
        tid = database.insert_task(
            conn,
            project_id=sample_project["id"],
            title="Active Task",
            description="A task in the pipeline",
            priority=3,
        )
        database.update_task(conn, tid, status="active", current_stage="plan")
        sr1 = database.insert_stage_run(
            conn,
            task_id=tid,
            stage="spec",
            attempt=1,
            status="passed",
        )
        database.update_stage_run(
            conn,
            sr1,
            started_at="2026-01-01T00:00:00+00:00",
            finished_at="2026-01-01T00:01:00+00:00",
            duration_seconds=60.0,
            gate_exit_code=0,
        )
        database.insert_stage_run(
            conn,
            task_id=tid,
            stage="plan",
            attempt=1,
            status="queued",
        )
        return tid
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Pipeline view
# ---------------------------------------------------------------------------


class TestPipelineView:
    def test_returns_200(self, client: TestClient) -> None:
        resp = client.get("/")
        assert resp.status_code == 200

    def test_contains_kanban_columns(self, client: TestClient) -> None:
        resp = client.get("/")
        html = resp.text
        for col in ["Backlog", "Spec", "Plan", "Implement", "Review", "Done"]:
            assert col in html

    def test_shows_task_in_backlog(self, client: TestClient, sample_task: str) -> None:
        resp = client.get("/")
        assert "Test Task" in resp.text

    def test_shows_active_task_in_stage_column(
        self,
        client: TestClient,
        active_task_with_runs: str,
    ) -> None:
        resp = client.get("/")
        assert "Active Task" in resp.text

    def test_project_filter(
        self,
        client: TestClient,
        sample_project: dict,
        sample_task: str,
    ) -> None:
        resp = client.get(f"/?project_id={sample_project['id']}")
        assert resp.status_code == 200
        assert "Test Task" in resp.text

    def test_project_filter_empty(self, client: TestClient) -> None:
        resp = client.get("/?project_id=nonexistent")
        assert resp.status_code == 200

    def test_needs_human_indicator(
        self, tmp_path, client: TestClient, sample_project: dict
    ) -> None:
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            tid = database.insert_task(
                conn,
                project_id=sample_project["id"],
                title="Stuck Task",
                priority=1,
            )
            database.update_task(
                conn, tid, status="needs_human", current_stage="implement"
            )
        finally:
            conn.close()
        resp = client.get("/")
        assert "Needs Human" in resp.text

    def test_htmx_polling_attribute(self, client: TestClient) -> None:
        resp = client.get("/")
        assert "hx-trigger" in resp.text
        assert "every 5s" in resp.text

    def test_retry_indicator_shown_for_attempt_gt_1(
        self,
        tmp_path,
        client: TestClient,
        sample_project: dict,
    ) -> None:
        """AC 1: Cards with attempt > 1 show 'Attempt N/M'."""
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            tid = database.insert_task(
                conn,
                project_id=sample_project["id"],
                title="Retry Task",
                priority=1,
                max_retries=3,
            )
            database.update_task(conn, tid, status="active", current_stage="implement")
            database.insert_stage_run(
                conn, task_id=tid, stage="implement", attempt=2, status="running"
            )
        finally:
            conn.close()
        resp = client.get("/")
        assert "Attempt 2/3" in resp.text

    def test_no_retry_indicator_for_first_attempt(
        self,
        client: TestClient,
        active_task_with_runs: str,
    ) -> None:
        """AC 2: Cards on first attempt do not show retry indicator."""
        resp = client.get("/")
        assert "Attempt 1/" not in resp.text
        assert "Attempt 1" not in resp.text

    def test_no_retry_indicator_for_backlog_or_done(
        self,
        tmp_path,
        client: TestClient,
        sample_project: dict,
    ) -> None:
        """AC 3: Backlog and done tasks show no retry indicator."""
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            database.insert_task(
                conn,
                project_id=sample_project["id"],
                title="Backlog Task",
                priority=1,
            )
            tid_done = database.insert_task(
                conn,
                project_id=sample_project["id"],
                title="Done Task",
                priority=1,
            )
            database.update_task(conn, tid_done, status="done", current_stage="review")
        finally:
            conn.close()
        resp = client.get("/")
        assert "Attempt" not in resp.text

    def test_max_retries_from_database(
        self,
        tmp_path,
        client: TestClient,
        sample_project: dict,
    ) -> None:
        """AC 4: max_retries comes from the task's database value, not hardcoded."""
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            tid = database.insert_task(
                conn,
                project_id=sample_project["id"],
                title="Custom Retries",
                priority=1,
                max_retries=5,
            )
            database.update_task(conn, tid, status="active", current_stage="spec")
            database.insert_stage_run(
                conn, task_id=tid, stage="spec", attempt=2, status="running"
            )
        finally:
            conn.close()
        resp = client.get("/")
        assert "Attempt 2/5" in resp.text

    def test_retry_indicator_uses_attempt_css_class(
        self,
        tmp_path,
        client: TestClient,
        sample_project: dict,
    ) -> None:
        """AC 5: Indicator uses the .attempt CSS class for small, plain text."""
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            tid = database.insert_task(
                conn,
                project_id=sample_project["id"],
                title="Styled Task",
                priority=1,
                max_retries=3,
            )
            database.update_task(conn, tid, status="active", current_stage="plan")
            database.insert_stage_run(
                conn, task_id=tid, stage="plan", attempt=2, status="running"
            )
        finally:
            conn.close()
        resp = client.get("/")
        assert 'class="attempt"' in resp.text
        assert "Attempt 2/3" in resp.text


# ---------------------------------------------------------------------------
# Task detail
# ---------------------------------------------------------------------------


class TestTaskDetail:
    def test_returns_200(self, client: TestClient, sample_task: str) -> None:
        resp = client.get(f"/tasks/{sample_task}")
        assert resp.status_code == 200

    def test_shows_task_title(self, client: TestClient, sample_task: str) -> None:
        resp = client.get(f"/tasks/{sample_task}")
        assert "Test Task" in resp.text

    def test_shows_stage_run_history(
        self,
        client: TestClient,
        active_task_with_runs: str,
    ) -> None:
        resp = client.get(f"/tasks/{active_task_with_runs}")
        assert "spec" in resp.text
        assert "plan" in resp.text
        assert "Attempt" in resp.text

    def test_not_found(self, client: TestClient) -> None:
        resp = client.get("/tasks/nonexistent-id")
        assert resp.status_code == 404

    def test_shows_resume_button_for_needs_human(
        self,
        tmp_path,
        client: TestClient,
        sample_project: dict,
    ) -> None:
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            tid = database.insert_task(
                conn,
                project_id=sample_project["id"],
                title="Stuck",
                priority=1,
            )
            database.update_task(conn, tid, status="needs_human", current_stage="spec")
        finally:
            conn.close()
        resp = client.get(f"/tasks/{tid}")
        assert "Resume" in resp.text


# ---------------------------------------------------------------------------
# Backlog
# ---------------------------------------------------------------------------


class TestBacklog:
    def test_returns_200(self, client: TestClient) -> None:
        resp = client.get("/backlog")
        assert resp.status_code == 200

    def test_shows_backlog_tasks(self, client: TestClient, sample_task: str) -> None:
        resp = client.get("/backlog")
        assert "Test Task" in resp.text
        assert "TestProject" in resp.text

    def test_shows_create_form(self, client: TestClient) -> None:
        resp = client.get("/backlog")
        assert "Create Task" in resp.text
        assert "<form" in resp.text

    def test_empty_backlog(self, client: TestClient) -> None:
        resp = client.get("/backlog")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class TestSettings:
    def test_returns_200(self, client: TestClient) -> None:
        resp = client.get("/settings")
        assert resp.status_code == 200

    def test_shows_engine_settings(self, client: TestClient) -> None:
        resp = client.get("/settings")
        assert "Poll interval" in resp.text
        assert "Stage timeout" in resp.text

    def test_shows_claude_settings(self, client: TestClient) -> None:
        resp = client.get("/settings")
        assert "Default model" in resp.text
        assert "opus" in resp.text

    def test_shows_projects(self, client: TestClient, sample_project: dict) -> None:
        resp = client.get("/settings")
        assert "TestProject" in resp.text


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------


class TestLogs:
    def test_returns_200(self, client: TestClient) -> None:
        resp = client.get("/logs")
        assert resp.status_code == 200

    def test_shows_log_entries(self, tmp_path, client: TestClient) -> None:
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            database.insert_log(conn, level="info", message="Test log message")
        finally:
            conn.close()
        resp = client.get("/logs")
        assert "Test log message" in resp.text

    def test_filter_by_level(self, tmp_path, client: TestClient) -> None:
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            database.insert_log(conn, level="info", message="Info msg")
            database.insert_log(conn, level="error", message="Error msg")
        finally:
            conn.close()
        resp = client.get("/logs?level=error")
        assert "Error msg" in resp.text

    def test_empty_logs(self, client: TestClient) -> None:
        resp = client.get("/logs")
        assert "No log entries" in resp.text

    def test_shows_filter_form(self, client: TestClient) -> None:
        resp = client.get("/logs")
        assert "All levels" in resp.text


# ---------------------------------------------------------------------------
# Static assets
# ---------------------------------------------------------------------------


class TestStaticAssets:
    def test_css_served(self, client: TestClient) -> None:
        resp = client.get("/static/styles.css")
        assert resp.status_code == 200
        assert "text/css" in resp.headers["content-type"]

    def test_js_served(self, client: TestClient) -> None:
        resp = client.get("/static/app.js")
        assert resp.status_code == 200
        assert "javascript" in resp.headers["content-type"]
