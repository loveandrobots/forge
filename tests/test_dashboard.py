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

    def test_pipeline_shows_quick_flow_badge(
        self,
        tmp_path,
        client: TestClient,
        sample_project: dict,
    ) -> None:
        """AC2: Kanban cards show a Quick badge for quick-flow tasks."""
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            tid_quick = database.insert_task(
                conn,
                project_id=sample_project["id"],
                title="QuickFlowTask",
                priority=1,
                flow="quick",
            )
            database.update_task(
                conn, tid_quick, status="active", current_stage="implement"
            )
            tid_standard = database.insert_task(
                conn,
                project_id=sample_project["id"],
                title="StandardFlowTask",
                priority=1,
                flow="standard",
            )
            database.update_task(
                conn, tid_standard, status="active", current_stage="spec"
            )
        finally:
            conn.close()
        resp = client.get("/")
        html = resp.text
        assert "QuickFlowTask" in html
        assert "StandardFlowTask" in html
        # Only the quick-flow task should have the badge
        assert html.count("badge-flow-quick") == 1


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

    def test_shows_resume_button_for_paused(
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
                title="Paused task",
                priority=1,
            )
            database.update_task(conn, tid, status="paused", current_stage="spec")
        finally:
            conn.close()
        resp = client.get(f"/tasks/{tid}")
        assert "Resume" in resp.text

    def test_task_detail_shows_flow(
        self,
        tmp_path,
        client: TestClient,
        sample_project: dict,
    ) -> None:
        """AC3: Task detail page shows the flow type in metadata."""
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            tid = database.insert_task(
                conn,
                project_id=sample_project["id"],
                title="Quick Detail",
                priority=1,
                flow="quick",
            )
        finally:
            conn.close()
        resp = client.get(f"/tasks/{tid}")
        assert "Quick Flow" in resp.text
        assert "badge-flow-quick" in resp.text

    def test_task_detail_shows_standard_flow(
        self,
        tmp_path,
        client: TestClient,
        sample_project: dict,
    ) -> None:
        """AC3: Task detail page shows Standard Flow for standard tasks."""
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            tid = database.insert_task(
                conn,
                project_id=sample_project["id"],
                title="Standard Detail",
                priority=1,
                flow="standard",
            )
        finally:
            conn.close()
        resp = client.get(f"/tasks/{tid}")
        assert "Standard Flow" in resp.text

    def test_task_detail_reset_dropdown_quick_flow(
        self,
        tmp_path,
        client: TestClient,
        sample_project: dict,
    ) -> None:
        """AC4: Quick-flow tasks only show implement and review in reset dropdown."""
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            tid = database.insert_task(
                conn,
                project_id=sample_project["id"],
                title="Quick Reset",
                priority=1,
                flow="quick",
            )
            database.update_task(
                conn, tid, status="needs_human", current_stage="implement"
            )
        finally:
            conn.close()
        resp = client.get(f"/tasks/{tid}")
        html = resp.text
        assert 'value="implement"' in html
        assert 'value="review"' in html
        assert 'value="spec"' not in html
        assert 'value="plan"' not in html

    def test_task_detail_reset_dropdown_standard_flow(
        self,
        tmp_path,
        client: TestClient,
        sample_project: dict,
    ) -> None:
        """AC4: Standard-flow tasks show all four stages in reset dropdown."""
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            tid = database.insert_task(
                conn,
                project_id=sample_project["id"],
                title="Standard Reset",
                priority=1,
                flow="standard",
            )
            database.update_task(
                conn, tid, status="needs_human", current_stage="spec"
            )
        finally:
            conn.close()
        resp = client.get(f"/tasks/{tid}")
        html = resp.text
        assert 'value="spec"' in html
        assert 'value="plan"' in html
        assert 'value="implement"' in html
        assert 'value="review"' in html

    def test_description_preserves_newlines(
        self, tmp_path, client: TestClient, sample_project
    ) -> None:
        """Newlines in task descriptions are present in rendered HTML."""
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            tid = database.insert_task(
                conn,
                project_id=sample_project["id"],
                title="Multiline Task",
                description="Line one\nLine two\nLine three",
                priority=5,
            )
        finally:
            conn.close()
        resp = client.get(f"/tasks/{tid}")
        html = resp.text
        assert "Line one\nLine two\nLine three" in html
        assert "<p>" not in html.split("task-description")[1].split("</div>")[0]
        # Verify no leading/trailing whitespace inside the pre-wrap div
        desc_block = html.split('class="task-description"')[1].split("</div>")[0]
        assert desc_block.startswith(">Line one")

    def test_description_html_escaped(
        self, tmp_path, client: TestClient, sample_project
    ) -> None:
        """HTML in descriptions is escaped — no XSS risk."""
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            tid = database.insert_task(
                conn,
                project_id=sample_project["id"],
                title="XSS Task",
                description="<script>alert('xss')</script>\nSafe line",
                priority=5,
            )
        finally:
            conn.close()
        resp = client.get(f"/tasks/{tid}")
        html = resp.text
        assert "&lt;script&gt;" in html
        assert "<script>alert" not in html

    def test_empty_description_hidden(
        self, tmp_path, client: TestClient, sample_project
    ) -> None:
        """Empty description does not render the .task-description div."""
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            tid = database.insert_task(
                conn,
                project_id=sample_project["id"],
                title="No Desc Task",
                description="",
                priority=5,
            )
        finally:
            conn.close()
        resp = client.get(f"/tasks/{tid}")
        assert "task-description" not in resp.text

    def test_single_line_description_no_p_tag(
        self, tmp_path, client: TestClient, sample_project
    ) -> None:
        """Single-line descriptions render without a <p> wrapper."""
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            tid = database.insert_task(
                conn,
                project_id=sample_project["id"],
                title="Single Line Task",
                description="Just one line",
                priority=5,
            )
        finally:
            conn.close()
        resp = client.get(f"/tasks/{tid}")
        desc_section = resp.text.split("task-description")[1].split("</div>")[0]
        assert "Just one line" in desc_section
        assert "<p>" not in desc_section

    def test_shows_cancel_button_for_backlog(
        self,
        client: TestClient,
        sample_task: str,
    ) -> None:
        """Cancel button appears for backlog tasks."""
        resp = client.get(f"/tasks/{sample_task}")
        assert "Cancel" in resp.text
        assert f"/api/tasks/{sample_task}/cancel" in resp.text

    def test_shows_cancel_button_for_active(
        self,
        client: TestClient,
        active_task_with_runs: str,
    ) -> None:
        """Cancel button appears for active tasks."""
        resp = client.get(f"/tasks/{active_task_with_runs}")
        assert "Cancel" in resp.text
        assert f"/api/tasks/{active_task_with_runs}/cancel" in resp.text

    def test_shows_cancel_button_for_paused(
        self,
        tmp_path,
        client: TestClient,
        sample_project: dict,
    ) -> None:
        """Cancel button appears for paused tasks."""
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            tid = database.insert_task(
                conn,
                project_id=sample_project["id"],
                title="Paused Task",
                priority=1,
            )
            database.update_task(conn, tid, status="paused", current_stage="spec")
        finally:
            conn.close()
        resp = client.get(f"/tasks/{tid}")
        assert "Cancel" in resp.text
        assert f"/api/tasks/{tid}/cancel" in resp.text

    def test_shows_cancel_button_for_needs_human(
        self,
        tmp_path,
        client: TestClient,
        sample_project: dict,
    ) -> None:
        """Cancel button appears for needs_human tasks."""
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            tid = database.insert_task(
                conn,
                project_id=sample_project["id"],
                title="Stuck Task",
                priority=1,
            )
            database.update_task(conn, tid, status="needs_human", current_stage="spec")
        finally:
            conn.close()
        resp = client.get(f"/tasks/{tid}")
        assert "Cancel" in resp.text
        assert f"/api/tasks/{tid}/cancel" in resp.text

    def test_no_cancel_button_for_done(
        self,
        tmp_path,
        client: TestClient,
        sample_project: dict,
    ) -> None:
        """Cancel button does not appear for done tasks."""
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            tid = database.insert_task(
                conn,
                project_id=sample_project["id"],
                title="Done Task",
                priority=1,
            )
            database.update_task(conn, tid, status="done", current_stage="review")
        finally:
            conn.close()
        resp = client.get(f"/tasks/{tid}")
        assert f"/api/tasks/{tid}/cancel" not in resp.text

    def test_no_cancel_button_for_cancelled(
        self,
        tmp_path,
        client: TestClient,
        sample_project: dict,
    ) -> None:
        """Cancel button does not appear for already-cancelled tasks."""
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            tid = database.insert_task(
                conn,
                project_id=sample_project["id"],
                title="Cancelled Task",
                priority=1,
            )
            database.update_task(conn, tid, status="cancelled")
        finally:
            conn.close()
        resp = client.get(f"/tasks/{tid}")
        assert f"/api/tasks/{tid}/cancel" not in resp.text

    def test_no_cancel_button_for_failed(
        self,
        tmp_path,
        client: TestClient,
        sample_project: dict,
    ) -> None:
        """Cancel button does not appear for failed tasks."""
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            tid = database.insert_task(
                conn,
                project_id=sample_project["id"],
                title="Failed Task",
                priority=1,
            )
            database.update_task(conn, tid, status="failed", current_stage="spec")
        finally:
            conn.close()
        resp = client.get(f"/tasks/{tid}")
        assert f"/api/tasks/{tid}/cancel" not in resp.text

    def test_cancel_button_uses_btn_danger_class(
        self,
        client: TestClient,
        sample_task: str,
    ) -> None:
        """Cancel button uses btn-danger styling."""
        resp = client.get(f"/tasks/{sample_task}")
        assert "btn btn-danger" in resp.text

    def test_css_has_pre_wrap(self) -> None:
        """The .task-description CSS rule includes white-space: pre-wrap."""
        import pathlib

        css = pathlib.Path("static/styles.css").read_text()
        # Find the .task-description block and check for pre-wrap
        idx = css.index(".task-description")
        block = css[idx : css.index("}", idx) + 1]
        assert "white-space: pre-wrap" in block


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

    def test_backlog_page_has_flow_selector(self, client: TestClient) -> None:
        """AC1: The backlog form includes a flow selector with standard and quick options."""
        resp = client.get("/backlog")
        html = resp.text
        assert 'name="flow"' in html
        assert 'value="standard"' in html
        assert 'value="quick"' in html
        # Standard should be selected by default
        assert 'value="standard" selected' in html

    def test_create_task_with_quick_flow_via_form(
        self,
        client: TestClient,
        sample_project: dict,
    ) -> None:
        """AC1: Creating a task with flow='quick' via the API persists correctly."""
        resp = client.post(
            "/api/tasks",
            json={
                "project_id": sample_project["id"],
                "title": "Quick Task",
                "description": "A quick flow task",
                "priority": 1,
                "flow": "quick",
            },
        )
        assert resp.status_code == 201
        task_id = resp.json()["id"]
        detail = client.get(f"/api/tasks/{task_id}")
        assert detail.json()["flow"] == "quick"

    def test_form_resets_on_success(self, client: TestClient) -> None:
        """After successful submission, the form script calls form.reset()."""
        resp = client.get("/backlog")
        html = resp.text
        # form.reset() must appear inside the resp.ok branch
        assert "form.reset()" in html
        # Verify it's gated behind resp.ok (success only)
        ok_pos = html.index("resp.ok")
        reset_pos = html.index("form.reset()")
        assert reset_pos > ok_pos

    def test_form_reset_preserves_project_id(self, client: TestClient) -> None:
        """The project_id selection is saved before reset and restored after."""
        resp = client.get("/backlog")
        html = resp.text
        # Script saves project_id before reset and restores after
        save_pos = html.index("savedProjectId = form.project_id.value")
        reset_pos = html.index("form.reset()")
        restore_pos = html.index("form.project_id.value = savedProjectId")
        assert save_pos < reset_pos < restore_pos

    def test_form_reset_only_on_success(self, client: TestClient) -> None:
        """form.reset() only appears inside the if (resp.ok) block, not unconditionally."""
        resp = client.get("/backlog")
        html = resp.text
        # form.reset() should appear exactly once in the entire page
        assert html.count("form.reset()") == 1
        # And it must be inside the resp.ok conditional
        ok_idx = html.index("resp.ok")
        reset_idx = html.index("form.reset()")
        assert reset_idx > ok_idx


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


# ---------------------------------------------------------------------------
# Escalation badges
# ---------------------------------------------------------------------------


class TestEscalationBadges:
    def test_pipeline_shows_escalated_badge(
        self,
        tmp_path,
        client: TestClient,
        sample_project: dict,
    ) -> None:
        """Pipeline view shows 'Escalated' badge for escalated tasks."""
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            tid = database.insert_task(
                conn,
                project_id=sample_project["id"],
                title="Escalated Task",
                flow="standard",
            )
            database.update_task(
                conn, tid, status="active", current_stage="spec", escalated_from_quick=1,
            )
        finally:
            conn.close()
        resp = client.get("/")
        assert resp.status_code == 200
        assert "Escalated" in resp.text

    def test_task_detail_shows_escalated_indicator(
        self,
        tmp_path,
        client: TestClient,
        sample_project: dict,
    ) -> None:
        """Task detail page shows 'Escalated from Quick Flow' indicator."""
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            tid = database.insert_task(
                conn,
                project_id=sample_project["id"],
                title="Escalated Detail Task",
                flow="standard",
            )
            database.update_task(
                conn, tid, status="active", current_stage="spec", escalated_from_quick=1,
            )
        finally:
            conn.close()
        resp = client.get(f"/tasks/{tid}")
        assert resp.status_code == 200
        assert "Escalated from Quick Flow" in resp.text
