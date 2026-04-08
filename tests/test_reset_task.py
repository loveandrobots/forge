"""Tests for the task reset feature (database, API, CLI, dashboard)."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from forge import database
from forge.cli import main as cli_main
from forge.main import app


@pytest.fixture()
def client():
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture()
def project_id(client: TestClient, tmp_path) -> str:
    resp = client.post(
        "/api/projects",
        json={"name": "TestProject", "repo_path": str(tmp_path)},
    )
    return resp.json()["id"]


@pytest.fixture()
def task_id(client: TestClient, project_id: str) -> str:
    resp = client.post(
        "/api/tasks",
        json={"project_id": project_id, "title": "Test task", "priority": 5},
    )
    return resp.json()["id"]


def _activate(client: TestClient, task_id: str) -> None:
    client.post(f"/api/tasks/{task_id}/activate")


def _pause(client: TestClient, task_id: str) -> None:
    _activate(client, task_id)
    client.post(f"/api/tasks/{task_id}/pause")


def _make_needs_human(client: TestClient, task_id: str, tmp_path) -> None:
    """Activate a task then set it to needs_human via direct DB update."""
    _activate(client, task_id)
    conn = database.get_connection(str(tmp_path / "test.db"))
    try:
        database.update_task(conn, task_id, status="needs_human")
    finally:
        conn.close()


def _make_failed(client: TestClient, task_id: str, tmp_path) -> None:
    """Activate a task then set it to failed via direct DB update."""
    _activate(client, task_id)
    conn = database.get_connection(str(tmp_path / "test.db"))
    try:
        database.update_task(conn, task_id, status="failed")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Database-level tests
# ---------------------------------------------------------------------------


class TestResetTaskDatabase:
    def test_reset_task_deletes_all_stage_runs(self, client, project_id, task_id, tmp_path):
        _pause(client, task_id)
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            # Add extra stage_runs
            database.insert_stage_run(conn, task_id=task_id, stage="plan", attempt=1, status="passed")
            database.insert_stage_run(conn, task_id=task_id, stage="implement", attempt=1, status="bounced")
            runs_before = database.list_stage_runs(conn, task_id=task_id)
            assert len(runs_before) >= 2

            database.reset_task(conn, task_id, "spec", "Test task")

            runs_after = database.list_stage_runs(conn, task_id=task_id)
            assert len(runs_after) == 1
            assert runs_after[0]["status"] == "queued"
            assert runs_after[0]["stage"] == "spec"
        finally:
            conn.close()

    def test_reset_task_creates_correct_stage_run(self, client, project_id, task_id, tmp_path):
        _pause(client, task_id)
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            for stage in ["spec", "plan", "implement", "review"]:
                database.reset_task(conn, task_id, stage, "Test task")
                runs = database.list_stage_runs(conn, task_id=task_id)
                assert len(runs) == 1
                assert runs[0]["stage"] == stage
                assert runs[0]["attempt"] == 1
                assert runs[0]["status"] == "queued"
        finally:
            conn.close()

    def test_reset_task_updates_task_status_and_stage(self, client, project_id, task_id, tmp_path):
        _make_needs_human(client, task_id, tmp_path)
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            database.reset_task(conn, task_id, "plan", "Test task")
            task = database.get_task(conn, task_id)
            assert task["status"] == "active"
            assert task["current_stage"] == "plan"
        finally:
            conn.close()

    def test_reset_task_logs_entry(self, client, project_id, task_id, tmp_path):
        _pause(client, task_id)
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            database.reset_task(conn, task_id, "spec", "Test task")
            logs = database.get_logs(conn, task_id=task_id)
            assert any(
                "reset to spec stage" in log["message"]
                and "Previous stage_run history cleared" in log["message"]
                for log in logs
            )
        finally:
            conn.close()

    def test_reset_task_is_atomic(self, client, project_id, task_id, tmp_path):
        _pause(client, task_id)
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            runs_before = database.list_stage_runs(conn, task_id=task_id)
            task_before = database.get_task(conn, task_id)

            # Patch _new_id to raise after DELETE and UPDATE have executed
            # but before the INSERT stage_run completes
            original_new_id = database._new_id
            call_count = 0

            def _failing_new_id():
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise RuntimeError("simulated failure")
                return original_new_id()

            with patch("forge.database._new_id", side_effect=_failing_new_id):
                with pytest.raises(RuntimeError, match="simulated failure"):
                    database.reset_task(conn, task_id, "spec", "Test task")

            # Verify nothing changed after rollback
            runs_after = database.list_stage_runs(conn, task_id=task_id)
            task_after = database.get_task(conn, task_id)
            assert len(runs_after) == len(runs_before)
            assert task_after["status"] == task_before["status"]
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# API-level tests
# ---------------------------------------------------------------------------


class TestResetTaskAPI:
    def test_reset_api_success(self, client, project_id, task_id):
        _pause(client, task_id)
        resp = client.post(
            f"/api/tasks/{task_id}/reset",
            json={"from_stage": "implement"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "active"
        assert data["current_stage"] == "implement"

    def test_reset_api_default_stage(self, client, project_id, task_id):
        _pause(client, task_id)
        resp = client.post(f"/api/tasks/{task_id}/reset")
        assert resp.status_code == 200
        data = resp.json()
        assert data["current_stage"] == "spec"

    def test_reset_api_needs_human_task(self, client, project_id, task_id, tmp_path):
        _make_needs_human(client, task_id, tmp_path)
        resp = client.post(f"/api/tasks/{task_id}/reset")
        assert resp.status_code == 200
        assert resp.json()["status"] == "active"

    def test_reset_api_active_task_rejected(self, client, project_id, task_id):
        _activate(client, task_id)
        resp = client.post(f"/api/tasks/{task_id}/reset")
        assert resp.status_code == 400
        assert "Cannot reset" in resp.json()["detail"]

    def test_reset_api_backlog_task_rejected(self, client, project_id, task_id):
        resp = client.post(f"/api/tasks/{task_id}/reset")
        assert resp.status_code == 400
        assert "Cannot reset" in resp.json()["detail"]

    def test_reset_api_done_task_rejected(self, client, project_id, task_id, tmp_path):
        _activate(client, task_id)
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            database.update_task(conn, task_id, status="done")
        finally:
            conn.close()
        resp = client.post(f"/api/tasks/{task_id}/reset")
        assert resp.status_code == 400

    def test_reset_api_cancelled_task_rejected(self, client, project_id, task_id, tmp_path):
        _activate(client, task_id)
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            database.update_task(conn, task_id, status="cancelled")
        finally:
            conn.close()
        resp = client.post(f"/api/tasks/{task_id}/reset")
        assert resp.status_code == 400

    def test_reset_api_invalid_stage(self, client, project_id, task_id):
        _pause(client, task_id)
        resp = client.post(
            f"/api/tasks/{task_id}/reset",
            json={"from_stage": "invalid"},
        )
        assert resp.status_code == 400
        assert "Invalid stage" in resp.json()["detail"]

    def test_reset_api_task_not_found(self, client):
        resp = client.post("/api/tasks/nonexistent/reset")
        assert resp.status_code == 404

    def test_reset_api_empty_body_standard_flow(self, client, project_id, task_id):
        """Empty body {} on standard-flow task defaults to 'spec'."""
        _pause(client, task_id)
        resp = client.post(f"/api/tasks/{task_id}/reset", json={})
        assert resp.status_code == 200
        assert resp.json()["current_stage"] == "spec"

    def test_reset_api_empty_body_quick_flow(self, client, project_id):
        """Empty body {} on quick-flow task defaults to 'implement' (not 400)."""
        resp = client.post(
            "/api/tasks",
            json={"project_id": project_id, "title": "Quick reset", "priority": 1, "flow": "quick"},
        )
        qid = resp.json()["id"]
        client.post(f"/api/tasks/{qid}/activate")
        client.post(f"/api/tasks/{qid}/pause")
        resp = client.post(f"/api/tasks/{qid}/reset", json={})
        assert resp.status_code == 200
        assert resp.json()["current_stage"] == "implement"

    def test_reset_api_empty_body_epic_flow(self, client, project_id):
        """Empty body {} on epic-flow task defaults to 'spec'."""
        resp = client.post(
            "/api/tasks",
            json={"project_id": project_id, "title": "Epic reset", "priority": 1, "flow": "epic"},
        )
        eid = resp.json()["id"]
        client.post(f"/api/tasks/{eid}/activate")
        client.post(f"/api/tasks/{eid}/pause")
        resp = client.post(f"/api/tasks/{eid}/reset", json={})
        assert resp.status_code == 200
        assert resp.json()["current_stage"] == "spec"

    def test_reset_api_no_body_quick_flow(self, client, project_id):
        """No body at all on quick-flow task defaults to 'implement'."""
        resp = client.post(
            "/api/tasks",
            json={"project_id": project_id, "title": "Quick no body", "priority": 1, "flow": "quick"},
        )
        qid = resp.json()["id"]
        client.post(f"/api/tasks/{qid}/activate")
        client.post(f"/api/tasks/{qid}/pause")
        resp = client.post(f"/api/tasks/{qid}/reset")
        assert resp.status_code == 200
        assert resp.json()["current_stage"] == "implement"


# ---------------------------------------------------------------------------
# CLI-level tests
# ---------------------------------------------------------------------------


class TestResetTaskCLI:
    def test_cli_reset_task_success(self, client, project_id, task_id, tmp_path):
        _pause(client, task_id)
        cli_main(["reset-task", task_id, "--from-stage", "plan"])
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            task = database.get_task(conn, task_id)
            assert task["status"] == "active"
            assert task["current_stage"] == "plan"
            runs = database.list_stage_runs(conn, task_id=task_id)
            assert len(runs) == 1
            assert runs[0]["stage"] == "plan"
            assert runs[0]["status"] == "queued"
        finally:
            conn.close()

    def test_cli_reset_task_default_stage(self, client, project_id, task_id, tmp_path):
        _pause(client, task_id)
        cli_main(["reset-task", task_id])
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            task = database.get_task(conn, task_id)
            assert task["current_stage"] == "spec"
        finally:
            conn.close()

    def test_cli_reset_task_invalid_id(self):
        with pytest.raises(SystemExit, match="1"):
            cli_main(["reset-task", "nonexistent-id"])

    def test_cli_reset_task_wrong_status(self, client, project_id, task_id):
        # task_id is in backlog status
        with pytest.raises(SystemExit, match="1"):
            cli_main(["reset-task", task_id])

    def test_cli_reset_quick_flow_default_stage(self, client, project_id, tmp_path):
        """Quick-flow task defaults to 'implement' (first stage of quick flow)."""
        resp = client.post(
            "/api/tasks",
            json={"project_id": project_id, "title": "Quick task", "priority": 1, "flow": "quick"},
        )
        qid = resp.json()["id"]
        client.post(f"/api/tasks/{qid}/activate")
        client.post(f"/api/tasks/{qid}/pause")
        cli_main(["reset-task", qid])
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            task = database.get_task(conn, qid)
            assert task["current_stage"] == "implement"
            runs = database.list_stage_runs(conn, task_id=qid)
            assert len(runs) == 1
            assert runs[0]["stage"] == "implement"
        finally:
            conn.close()

    def test_cli_reset_quick_flow_rejects_spec_stage(self, client, project_id):
        """Quick-flow task rejects --from-stage spec (not in quick flow)."""
        resp = client.post(
            "/api/tasks",
            json={"project_id": project_id, "title": "Quick task 2", "priority": 1, "flow": "quick"},
        )
        qid = resp.json()["id"]
        client.post(f"/api/tasks/{qid}/activate")
        client.post(f"/api/tasks/{qid}/pause")
        with pytest.raises(SystemExit, match="1"):
            cli_main(["reset-task", qid, "--from-stage", "spec"])

    def test_cli_reset_quick_flow_rejects_plan_stage(self, client, project_id):
        """Quick-flow task rejects --from-stage plan (not in quick flow)."""
        resp = client.post(
            "/api/tasks",
            json={"project_id": project_id, "title": "Quick task 3", "priority": 1, "flow": "quick"},
        )
        qid = resp.json()["id"]
        client.post(f"/api/tasks/{qid}/activate")
        client.post(f"/api/tasks/{qid}/pause")
        with pytest.raises(SystemExit, match="1"):
            cli_main(["reset-task", qid, "--from-stage", "plan"])


# ---------------------------------------------------------------------------
# Dashboard template tests
# ---------------------------------------------------------------------------


class TestResetTaskDashboard:
    def test_task_detail_shows_reset_button_for_needs_human(
        self, client, project_id, task_id, tmp_path
    ):
        _make_needs_human(client, task_id, tmp_path)
        resp = client.get(f"/tasks/{task_id}")
        assert resp.status_code == 200
        html = resp.text
        assert "Reset" in html
        assert "reset-stage-" in html

    def test_task_detail_hides_reset_button_for_active(
        self, client, project_id, task_id
    ):
        _activate(client, task_id)
        resp = client.get(f"/tasks/{task_id}")
        assert resp.status_code == 200
        assert "reset-stage-" not in resp.text

    def test_task_detail_hides_reset_button_for_backlog(
        self, client, project_id, task_id
    ):
        resp = client.get(f"/tasks/{task_id}")
        assert resp.status_code == 200
        assert "reset-stage-" not in resp.text

    def test_task_detail_reset_button_has_json_enc(
        self, client, project_id, task_id, tmp_path
    ):
        _make_needs_human(client, task_id, tmp_path)
        resp = client.get(f"/tasks/{task_id}")
        assert resp.status_code == 200
        assert 'hx-ext="json-enc"' in resp.text

    def test_task_detail_json_enc_extension_loaded(
        self, client, project_id, task_id, tmp_path
    ):
        _make_needs_human(client, task_id, tmp_path)
        resp = client.get(f"/tasks/{task_id}")
        assert resp.status_code == 200
        assert "htmx-ext-json-enc" in resp.text

    def test_task_detail_action_buttons_success_only_reload(
        self, client, project_id, task_id, tmp_path
    ):
        """All action buttons use event.detail.successful before reloading."""
        _make_needs_human(client, task_id, tmp_path)
        resp = client.get(f"/tasks/{task_id}")
        assert resp.status_code == 200
        html = resp.text
        # No bare window.location.reload() without the success check
        assert 'hx-on::after-request="window.location.reload()"' not in html
        # All after-request handlers should check successful
        assert "event.detail.successful" in html


# ---------------------------------------------------------------------------
# JSON body end-to-end API tests
# ---------------------------------------------------------------------------


class TestResetTaskJSONBody:
    def test_reset_api_json_body_paused_task(self, client, project_id, task_id, tmp_path):
        """POST with JSON body from_stage resets a paused task and verifies full state."""
        _pause(client, task_id)
        resp = client.post(
            f"/api/tasks/{task_id}/reset",
            json={"from_stage": "spec"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "active"
        assert data["current_stage"] == "spec"

        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            runs = database.list_stage_runs(conn, task_id=task_id)
            assert len(runs) == 1
            assert runs[0]["stage"] == "spec"
            assert runs[0]["status"] == "queued"
            assert runs[0]["attempt"] == 1
        finally:
            conn.close()

    def test_reset_api_json_body_failed_task(self, client, project_id, task_id, tmp_path):
        """POST with JSON body from_stage resets a failed task and verifies full state."""
        _make_failed(client, task_id, tmp_path)
        resp = client.post(
            f"/api/tasks/{task_id}/reset",
            json={"from_stage": "spec"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "active"
        assert data["current_stage"] == "spec"

        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            runs = database.list_stage_runs(conn, task_id=task_id)
            assert len(runs) == 1
            assert runs[0]["stage"] == "spec"
            assert runs[0]["status"] == "queued"
            assert runs[0]["attempt"] == 1
        finally:
            conn.close()

    def test_reset_api_json_body_needs_human_task(self, client, project_id, task_id, tmp_path):
        """POST with JSON body from_stage resets a needs_human task and verifies full state."""
        _make_needs_human(client, task_id, tmp_path)
        resp = client.post(
            f"/api/tasks/{task_id}/reset",
            json={"from_stage": "spec"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "active"
        assert data["current_stage"] == "spec"

        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            runs = database.list_stage_runs(conn, task_id=task_id)
            assert len(runs) == 1
            assert runs[0]["stage"] == "spec"
            assert runs[0]["status"] == "queued"
            assert runs[0]["attempt"] == 1
        finally:
            conn.close()
