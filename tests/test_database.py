"""Tests for forge.database module."""

from __future__ import annotations

import sqlite3

import pytest

from forge import database as db


@pytest.fixture
def conn() -> sqlite3.Connection:
    """In-memory database with schema applied."""
    c = db.get_connection(":memory:")
    db.migrate(c)
    return c


@pytest.fixture
def project_id(conn: sqlite3.Connection) -> str:
    return db.insert_project(conn, name="TestProject", repo_path="/tmp/repo")


@pytest.fixture
def task_id(conn: sqlite3.Connection, project_id: str) -> str:
    return db.insert_task(conn, project_id=project_id, title="Test task", priority=5)


# ---------------------------------------------------------------------------
# Schema / migrate
# ---------------------------------------------------------------------------

class TestMigrate:
    def test_idempotent(self, conn: sqlite3.Connection) -> None:
        # Calling migrate a second time should not error
        db.migrate(conn)
        db.migrate(conn)

    def test_tables_exist(self, conn: sqlite3.Connection) -> None:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"projects", "tasks", "stage_runs", "task_links", "run_log"} <= tables

    def test_foreign_keys_enabled(self, conn: sqlite3.Connection) -> None:
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

class TestProjects:
    def test_insert_and_get(self, conn: sqlite3.Connection) -> None:
        pid = db.insert_project(conn, name="P1", repo_path="/r")
        row = db.get_project(conn, pid)
        assert row is not None
        assert row["name"] == "P1"
        assert row["repo_path"] == "/r"
        assert row["default_branch"] == "main"
        assert row["gate_dir"] == "gates"

    def test_get_by_name(self, conn: sqlite3.Connection) -> None:
        db.insert_project(conn, name="ByName", repo_path="/x")
        row = db.get_project_by_name(conn, "ByName")
        assert row is not None
        assert row["name"] == "ByName"

    def test_get_nonexistent(self, conn: sqlite3.Connection) -> None:
        assert db.get_project(conn, "no-such-id") is None
        assert db.get_project_by_name(conn, "no-such-name") is None

    def test_list_projects(self, conn: sqlite3.Connection) -> None:
        db.insert_project(conn, name="Bravo", repo_path="/b")
        db.insert_project(conn, name="Alpha", repo_path="/a")
        rows = db.list_projects(conn)
        assert [r["name"] for r in rows] == ["Alpha", "Bravo"]

    def test_update_project(self, conn: sqlite3.Connection) -> None:
        pid = db.insert_project(conn, name="Old", repo_path="/old")
        ok = db.update_project(conn, pid, name="New", repo_path="/new")
        assert ok is True
        row = db.get_project(conn, pid)
        assert row["name"] == "New"
        assert row["repo_path"] == "/new"

    def test_update_no_fields(self, conn: sqlite3.Connection) -> None:
        pid = db.insert_project(conn, name="NoOp", repo_path="/x")
        assert db.update_project(conn, pid) is False

    def test_update_nonexistent(self, conn: sqlite3.Connection) -> None:
        assert db.update_project(conn, "bad-id", name="X") is False

    def test_unique_name(self, conn: sqlite3.Connection) -> None:
        db.insert_project(conn, name="Dup", repo_path="/a")
        with pytest.raises(sqlite3.IntegrityError):
            db.insert_project(conn, name="Dup", repo_path="/b")

    def test_skill_refs_and_config(self, conn: sqlite3.Connection) -> None:
        pid = db.insert_project(
            conn, name="WithJSON", repo_path="/j",
            skill_refs=["a", "b"], config={"key": "val"},
        )
        row = db.get_project(conn, pid)
        import json
        assert json.loads(row["skill_refs"]) == ["a", "b"]
        assert json.loads(row["config"]) == {"key": "val"}


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

class TestTasks:
    def test_insert_and_get(self, conn: sqlite3.Connection, project_id: str) -> None:
        tid = db.insert_task(conn, project_id=project_id, title="T1", description="desc", priority=3)
        row = db.get_task(conn, tid)
        assert row is not None
        assert row["title"] == "T1"
        assert row["status"] == "backlog"
        assert row["priority"] == 3
        assert row["current_stage"] is None

    def test_list_tasks_ordered(self, conn: sqlite3.Connection, project_id: str) -> None:
        db.insert_task(conn, project_id=project_id, title="Low", priority=1)
        db.insert_task(conn, project_id=project_id, title="High", priority=10)
        rows = db.list_tasks(conn, project_id=project_id)
        assert rows[0]["title"] == "High"
        assert rows[1]["title"] == "Low"

    def test_list_tasks_filter_status(self, conn: sqlite3.Connection, project_id: str) -> None:
        tid = db.insert_task(conn, project_id=project_id, title="A")
        db.update_task(conn, tid, status="active")
        db.insert_task(conn, project_id=project_id, title="B")
        rows = db.list_tasks(conn, status="backlog")
        assert len(rows) == 1
        assert rows[0]["title"] == "B"

    def test_list_tasks_filter_priority_gte(self, conn: sqlite3.Connection, project_id: str) -> None:
        db.insert_task(conn, project_id=project_id, title="Low", priority=1)
        db.insert_task(conn, project_id=project_id, title="High", priority=5)
        rows = db.list_tasks(conn, priority_gte=5)
        assert len(rows) == 1
        assert rows[0]["title"] == "High"

    def test_update_task(self, conn: sqlite3.Connection, task_id: str) -> None:
        ok = db.update_task(conn, task_id, title="Updated", status="active")
        assert ok is True
        row = db.get_task(conn, task_id)
        assert row["title"] == "Updated"
        assert row["status"] == "active"
        assert row["updated_at"] is not None

    def test_delete_backlog_task(self, conn: sqlite3.Connection, task_id: str) -> None:
        assert db.delete_task(conn, task_id) is True
        assert db.get_task(conn, task_id) is None

    def test_delete_non_backlog_task(self, conn: sqlite3.Connection, task_id: str) -> None:
        db.update_task(conn, task_id, status="active")
        assert db.delete_task(conn, task_id) is False
        assert db.get_task(conn, task_id) is not None

    def test_foreign_key_constraint(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            db.insert_task(conn, project_id="nonexistent", title="Bad")

    def test_get_next_queued_task(self, conn: sqlite3.Connection, project_id: str) -> None:
        t1 = db.insert_task(conn, project_id=project_id, title="Low", priority=1)
        t2 = db.insert_task(conn, project_id=project_id, title="High", priority=10)
        db.update_task(conn, t1, status="active")
        db.update_task(conn, t2, status="active")
        db.insert_stage_run(conn, task_id=t1, stage="spec", attempt=1, status="queued")
        db.insert_stage_run(conn, task_id=t2, stage="spec", attempt=1, status="queued")
        row = db.get_next_queued_task(conn)
        assert row is not None
        assert row["id"] == t2  # highest priority

    def test_get_next_queued_task_none(self, conn: sqlite3.Connection) -> None:
        assert db.get_next_queued_task(conn) is None


# ---------------------------------------------------------------------------
# Stage runs
# ---------------------------------------------------------------------------

class TestStageRuns:
    def test_insert_and_get(self, conn: sqlite3.Connection, task_id: str) -> None:
        sr_id = db.insert_stage_run(conn, task_id=task_id, stage="spec", attempt=1)
        row = db.get_stage_run(conn, sr_id)
        assert row is not None
        assert row["stage"] == "spec"
        assert row["status"] == "queued"
        assert row["started_at"] is None

    def test_list_stage_runs(self, conn: sqlite3.Connection, task_id: str) -> None:
        db.insert_stage_run(conn, task_id=task_id, stage="spec", attempt=1)
        db.insert_stage_run(conn, task_id=task_id, stage="plan", attempt=1)
        rows = db.list_stage_runs(conn, task_id=task_id)
        assert len(rows) == 2

    def test_list_stage_runs_filter(self, conn: sqlite3.Connection, task_id: str) -> None:
        db.insert_stage_run(conn, task_id=task_id, stage="spec", attempt=1, status="queued")
        db.insert_stage_run(conn, task_id=task_id, stage="spec", attempt=2, status="passed")
        rows = db.list_stage_runs(conn, task_id=task_id, status="passed")
        assert len(rows) == 1

    def test_update_stage_run(self, conn: sqlite3.Connection, task_id: str) -> None:
        sr_id = db.insert_stage_run(conn, task_id=task_id, stage="spec", attempt=1)
        ok = db.update_stage_run(conn, sr_id, status="running", started_at="2025-01-01T00:00:00Z")
        assert ok is True
        row = db.get_stage_run(conn, sr_id)
        assert row["status"] == "running"
        assert row["started_at"] == "2025-01-01T00:00:00Z"

    def test_update_stage_run_no_fields(self, conn: sqlite3.Connection, task_id: str) -> None:
        sr_id = db.insert_stage_run(conn, task_id=task_id, stage="spec", attempt=1)
        assert db.update_stage_run(conn, sr_id) is False

    def test_get_retry_count(self, conn: sqlite3.Connection, task_id: str) -> None:
        db.insert_stage_run(conn, task_id=task_id, stage="spec", attempt=1, status="bounced")
        db.insert_stage_run(conn, task_id=task_id, stage="spec", attempt=2, status="error")
        db.insert_stage_run(conn, task_id=task_id, stage="spec", attempt=3, status="passed")
        assert db.get_retry_count(conn, task_id, "spec") == 2

    def test_update_with_artifacts(self, conn: sqlite3.Connection, task_id: str) -> None:
        sr_id = db.insert_stage_run(conn, task_id=task_id, stage="spec", attempt=1)
        db.update_stage_run(conn, sr_id, artifacts_produced=["spec.md", "notes.md"])
        row = db.get_stage_run(conn, sr_id)
        import json
        assert json.loads(row["artifacts_produced"]) == ["spec.md", "notes.md"]


# ---------------------------------------------------------------------------
# Task links
# ---------------------------------------------------------------------------

class TestTaskLinks:
    def test_insert_and_get(self, conn: sqlite3.Connection, project_id: str) -> None:
        t1 = db.insert_task(conn, project_id=project_id, title="A")
        t2 = db.insert_task(conn, project_id=project_id, title="B")
        link_id = db.insert_task_link(conn, source_task_id=t1, target_task_id=t2, link_type="blocks")
        links = db.get_task_links(conn, t1)
        assert len(links) == 1
        assert links[0]["id"] == link_id
        assert links[0]["link_type"] == "blocks"

    def test_get_links_as_target(self, conn: sqlite3.Connection, project_id: str) -> None:
        t1 = db.insert_task(conn, project_id=project_id, title="A")
        t2 = db.insert_task(conn, project_id=project_id, title="B")
        db.insert_task_link(conn, source_task_id=t1, target_task_id=t2, link_type="blocks")
        links = db.get_task_links(conn, t2)
        assert len(links) == 1

    def test_invalid_link_type(self, conn: sqlite3.Connection, project_id: str) -> None:
        t1 = db.insert_task(conn, project_id=project_id, title="A")
        t2 = db.insert_task(conn, project_id=project_id, title="B")
        with pytest.raises(ValueError, match="Invalid link_type"):
            db.insert_task_link(conn, source_task_id=t1, target_task_id=t2, link_type="invalid")


# ---------------------------------------------------------------------------
# Run log
# ---------------------------------------------------------------------------

class TestRunLog:
    def test_insert_and_get(self, conn: sqlite3.Connection) -> None:
        log_id = db.insert_log(conn, level="info", message="hello")
        assert log_id > 0
        rows = db.get_logs(conn)
        assert len(rows) == 1
        assert rows[0]["message"] == "hello"

    def test_invalid_level(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="Invalid log level"):
            db.insert_log(conn, level="debug", message="bad")

    def test_filter_by_level(self, conn: sqlite3.Connection) -> None:
        db.insert_log(conn, level="info", message="a")
        db.insert_log(conn, level="error", message="b")
        rows = db.get_logs(conn, level="error")
        assert len(rows) == 1
        assert rows[0]["message"] == "b"

    def test_filter_by_task_id(self, conn: sqlite3.Connection, task_id: str) -> None:
        db.insert_log(conn, level="info", message="with task", task_id=task_id)
        db.insert_log(conn, level="info", message="without task")
        rows = db.get_logs(conn, task_id=task_id)
        assert len(rows) == 1
        assert rows[0]["message"] == "with task"

    def test_filter_by_project_id(self, conn: sqlite3.Connection, project_id: str, task_id: str) -> None:
        db.insert_log(conn, level="info", message="proj log", task_id=task_id)
        db.insert_log(conn, level="info", message="no proj")
        rows = db.get_logs(conn, project_id=project_id)
        assert len(rows) == 1
        assert rows[0]["message"] == "proj log"

    def test_pagination(self, conn: sqlite3.Connection) -> None:
        for i in range(5):
            db.insert_log(conn, level="info", message=f"msg{i}")
        rows = db.get_logs(conn, limit=2, offset=0)
        assert len(rows) == 2
        rows2 = db.get_logs(conn, limit=2, offset=2)
        assert len(rows2) == 2

    def test_metadata(self, conn: sqlite3.Connection) -> None:
        db.insert_log(conn, level="info", message="meta", metadata={"key": "val"})
        rows = db.get_logs(conn)
        import json
        assert json.loads(rows[0]["metadata"]) == {"key": "val"}

    def test_order_desc(self, conn: sqlite3.Connection) -> None:
        db.insert_log(conn, level="info", message="first")
        db.insert_log(conn, level="info", message="second")
        rows = db.get_logs(conn)
        # Most recent first
        assert rows[0]["message"] == "second"
