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

    def test_migrate_adds_pause_after_completion_column(self) -> None:
        """Migration adds pause_after_completion to an existing DB without the column."""
        c = db.get_connection(":memory:")
        # Create old schema without the column
        c.executescript("""
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                repo_path TEXT NOT NULL,
                default_branch TEXT NOT NULL DEFAULT 'main',
                gate_dir TEXT NOT NULL DEFAULT 'gates',
                skill_refs TEXT,
                created_at TEXT NOT NULL,
                config TEXT
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id),
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                priority INTEGER NOT NULL DEFAULT 0,
                current_stage TEXT,
                status TEXT NOT NULL DEFAULT 'backlog',
                branch_name TEXT,
                spec_path TEXT,
                plan_path TEXT,
                review_path TEXT,
                skill_overrides TEXT,
                max_retries INTEGER NOT NULL DEFAULT 3,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS stage_runs (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(id),
                stage TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                prompt_sent TEXT,
                started_at TEXT,
                finished_at TEXT,
                duration_seconds REAL,
                claude_output TEXT,
                artifacts_produced TEXT,
                gate_name TEXT,
                gate_exit_code INTEGER,
                gate_stdout TEXT,
                gate_stderr TEXT,
                tokens_used INTEGER,
                error_message TEXT
            );
            CREATE TABLE IF NOT EXISTS task_links (
                id TEXT PRIMARY KEY,
                source_task_id TEXT NOT NULL REFERENCES tasks(id),
                target_task_id TEXT NOT NULL REFERENCES tasks(id),
                link_type TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS run_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                task_id TEXT,
                stage_run_id TEXT,
                metadata TEXT
            );
        """)
        # Insert a project with old schema
        c.execute(
            "INSERT INTO projects (id, name, repo_path, created_at) VALUES ('p1', 'Old', '/tmp', '2025-01-01')"
        )
        c.commit()
        # Run migrate — should add the column
        db.migrate(c)
        row = c.execute(
            "SELECT pause_after_completion FROM projects WHERE id = 'p1'"
        ).fetchone()
        assert row[0] == 0


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

    def test_insert_project_default_pause_after_completion(
        self, conn: sqlite3.Connection
    ) -> None:
        pid = db.insert_project(conn, name="NoPause", repo_path="/tmp")
        row = db.get_project(conn, pid)
        assert row["pause_after_completion"] == 0

    def test_insert_project_with_pause_after_completion(
        self, conn: sqlite3.Connection
    ) -> None:
        pid = db.insert_project(
            conn, name="WithPause", repo_path="/tmp", pause_after_completion=True
        )
        row = db.get_project(conn, pid)
        assert row["pause_after_completion"] == 1

    def test_update_project_pause_after_completion(
        self, conn: sqlite3.Connection
    ) -> None:
        pid = db.insert_project(conn, name="Toggle", repo_path="/tmp")
        row = db.get_project(conn, pid)
        assert row["pause_after_completion"] == 0
        # Enable
        db.update_project(conn, pid, pause_after_completion=True)
        row = db.get_project(conn, pid)
        assert row["pause_after_completion"] == 1
        # Disable
        db.update_project(conn, pid, pause_after_completion=False)
        row = db.get_project(conn, pid)
        assert row["pause_after_completion"] == 0

    def test_skill_refs_and_config(self, conn: sqlite3.Connection) -> None:
        pid = db.insert_project(
            conn,
            name="WithJSON",
            repo_path="/j",
            skill_refs=["a", "b"],
            config={"key": "val"},
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
        tid = db.insert_task(
            conn, project_id=project_id, title="T1", description="desc", priority=3
        )
        row = db.get_task(conn, tid)
        assert row is not None
        assert row["title"] == "T1"
        assert row["status"] == "backlog"
        assert row["priority"] == 3
        assert row["current_stage"] is None

    def test_list_tasks_ordered(
        self, conn: sqlite3.Connection, project_id: str
    ) -> None:
        db.insert_task(conn, project_id=project_id, title="Low", priority=1)
        db.insert_task(conn, project_id=project_id, title="High", priority=10)
        rows = db.list_tasks(conn, project_id=project_id)
        assert rows[0]["title"] == "High"
        assert rows[1]["title"] == "Low"

    def test_list_tasks_filter_status(
        self, conn: sqlite3.Connection, project_id: str
    ) -> None:
        tid = db.insert_task(conn, project_id=project_id, title="A")
        db.update_task(conn, tid, status="active")
        db.insert_task(conn, project_id=project_id, title="B")
        rows = db.list_tasks(conn, status="backlog")
        assert len(rows) == 1
        assert rows[0]["title"] == "B"

    def test_list_tasks_filter_priority_gte(
        self, conn: sqlite3.Connection, project_id: str
    ) -> None:
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

    def test_delete_non_backlog_task(
        self, conn: sqlite3.Connection, task_id: str
    ) -> None:
        db.update_task(conn, task_id, status="active")
        assert db.delete_task(conn, task_id) is False
        assert db.get_task(conn, task_id) is not None

    def test_foreign_key_constraint(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            db.insert_task(conn, project_id="nonexistent", title="Bad")

    def test_get_next_queued_task(
        self, conn: sqlite3.Connection, project_id: str
    ) -> None:
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

    def test_list_stage_runs_filter(
        self, conn: sqlite3.Connection, task_id: str
    ) -> None:
        db.insert_stage_run(
            conn, task_id=task_id, stage="spec", attempt=1, status="queued"
        )
        db.insert_stage_run(
            conn, task_id=task_id, stage="spec", attempt=2, status="passed"
        )
        rows = db.list_stage_runs(conn, task_id=task_id, status="passed")
        assert len(rows) == 1

    def test_update_stage_run(self, conn: sqlite3.Connection, task_id: str) -> None:
        sr_id = db.insert_stage_run(conn, task_id=task_id, stage="spec", attempt=1)
        ok = db.update_stage_run(
            conn, sr_id, status="running", started_at="2025-01-01T00:00:00Z"
        )
        assert ok is True
        row = db.get_stage_run(conn, sr_id)
        assert row["status"] == "running"
        assert row["started_at"] == "2025-01-01T00:00:00Z"

    def test_update_stage_run_no_fields(
        self, conn: sqlite3.Connection, task_id: str
    ) -> None:
        sr_id = db.insert_stage_run(conn, task_id=task_id, stage="spec", attempt=1)
        assert db.update_stage_run(conn, sr_id) is False

    def test_get_retry_count(self, conn: sqlite3.Connection, task_id: str) -> None:
        db.insert_stage_run(
            conn, task_id=task_id, stage="spec", attempt=1, status="bounced"
        )
        db.insert_stage_run(
            conn, task_id=task_id, stage="spec", attempt=2, status="error"
        )
        db.insert_stage_run(
            conn, task_id=task_id, stage="spec", attempt=3, status="passed"
        )
        assert db.get_retry_count(conn, task_id, "spec") == 2

    def test_get_implement_review_retry_count(
        self, conn: sqlite3.Connection, task_id: str
    ) -> None:
        # Bounced implement and review runs should be counted
        db.insert_stage_run(
            conn, task_id=task_id, stage="implement", attempt=1, status="bounced"
        )
        db.insert_stage_run(
            conn, task_id=task_id, stage="review", attempt=1, status="bounced"
        )
        # Error runs should also be counted
        db.insert_stage_run(
            conn, task_id=task_id, stage="review", attempt=2, status="error"
        )
        # Non-bounced/error runs should not be counted
        db.insert_stage_run(
            conn, task_id=task_id, stage="implement", attempt=2, status="passed"
        )
        # Different stage should not be counted
        db.insert_stage_run(
            conn, task_id=task_id, stage="spec", attempt=1, status="bounced"
        )
        assert db.get_implement_review_retry_count(conn, task_id) == 3

    def test_get_stage_run_count(
        self, conn: sqlite3.Connection, task_id: str
    ) -> None:
        # Returns 0 when no stage_runs exist
        assert db.get_stage_run_count(conn, task_id, "implement") == 0

        # Counts all statuses
        db.insert_stage_run(
            conn, task_id=task_id, stage="implement", attempt=1, status="passed"
        )
        db.insert_stage_run(
            conn, task_id=task_id, stage="implement", attempt=2, status="bounced"
        )
        db.insert_stage_run(
            conn, task_id=task_id, stage="implement", attempt=3, status="error"
        )
        db.insert_stage_run(
            conn, task_id=task_id, stage="implement", attempt=4, status="queued"
        )
        assert db.get_stage_run_count(conn, task_id, "implement") == 4

        # Does not count different stages
        db.insert_stage_run(
            conn, task_id=task_id, stage="review", attempt=1, status="passed"
        )
        assert db.get_stage_run_count(conn, task_id, "implement") == 4
        assert db.get_stage_run_count(conn, task_id, "review") == 1

    def test_update_with_artifacts(
        self, conn: sqlite3.Connection, task_id: str
    ) -> None:
        sr_id = db.insert_stage_run(conn, task_id=task_id, stage="spec", attempt=1)
        db.update_stage_run(conn, sr_id, artifacts_produced=["spec.md", "notes.md"])
        row = db.get_stage_run(conn, sr_id)
        import json

        assert json.loads(row["artifacts_produced"]) == ["spec.md", "notes.md"]


# ---------------------------------------------------------------------------
# Task links
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Stats query functions
# ---------------------------------------------------------------------------


class TestCountTasksByExactStatus:
    def test_counts_done(self, conn: sqlite3.Connection, project_id: str) -> None:
        db.insert_task(conn, project_id=project_id, title="T1")
        t2 = db.insert_task(conn, project_id=project_id, title="T2")
        t3 = db.insert_task(conn, project_id=project_id, title="T3")
        db.update_task(conn, t2, status="done")
        db.update_task(conn, t3, status="done")
        assert db.count_tasks_by_exact_status(conn, "done") == 2

    def test_counts_active(self, conn: sqlite3.Connection, project_id: str) -> None:
        t1 = db.insert_task(conn, project_id=project_id, title="T1")
        db.update_task(conn, t1, status="active")
        assert db.count_tasks_by_exact_status(conn, "active") == 1

    def test_returns_zero_when_empty(self, conn: sqlite3.Connection) -> None:
        assert db.count_tasks_by_exact_status(conn, "done") == 0


class TestGetAvgDurationByStage:
    def test_with_data(self, conn: sqlite3.Connection, task_id: str) -> None:
        sr1 = db.insert_stage_run(conn, task_id=task_id, stage="spec", attempt=1)
        db.update_stage_run(conn, sr1, status="passed", duration_seconds=10.0)
        sr2 = db.insert_stage_run(conn, task_id=task_id, stage="spec", attempt=2)
        db.update_stage_run(conn, sr2, status="passed", duration_seconds=20.0)
        sr3 = db.insert_stage_run(conn, task_id=task_id, stage="plan", attempt=1)
        db.update_stage_run(conn, sr3, status="passed", duration_seconds=30.0)
        # Bounced run with duration should also be included (finished run)
        sr4 = db.insert_stage_run(conn, task_id=task_id, stage="spec", attempt=3)
        db.update_stage_run(conn, sr4, status="bounced", duration_seconds=6.0)

        result = db.get_avg_duration_by_stage(conn)
        assert result["plan"] == 30.0
        # spec: (10 + 20 + 6) / 3 = 12.0
        assert abs(result["spec"] - 12.0) < 0.01

    def test_empty(self, conn: sqlite3.Connection) -> None:
        assert db.get_avg_duration_by_stage(conn) == {}

    def test_excludes_null_duration(
        self, conn: sqlite3.Connection, task_id: str
    ) -> None:
        sr1 = db.insert_stage_run(conn, task_id=task_id, stage="spec", attempt=1)
        db.update_stage_run(conn, sr1, status="passed", duration_seconds=10.0)
        # Queued run with no duration
        db.insert_stage_run(conn, task_id=task_id, stage="spec", attempt=2)
        result = db.get_avg_duration_by_stage(conn)
        assert result["spec"] == 10.0


class TestGetBounceRateByStage:
    def test_with_data(self, conn: sqlite3.Connection, task_id: str) -> None:
        sr1 = db.insert_stage_run(conn, task_id=task_id, stage="implement", attempt=1)
        db.update_stage_run(conn, sr1, status="passed")
        sr2 = db.insert_stage_run(conn, task_id=task_id, stage="implement", attempt=2)
        db.update_stage_run(conn, sr2, status="passed")
        sr3 = db.insert_stage_run(conn, task_id=task_id, stage="implement", attempt=3)
        db.update_stage_run(conn, sr3, status="bounced")
        sr4 = db.insert_stage_run(conn, task_id=task_id, stage="implement", attempt=4)
        db.update_stage_run(conn, sr4, status="error")
        result = db.get_bounce_rate_by_stage(conn)
        assert abs(result["implement"] - 0.25) < 0.01

    def test_empty(self, conn: sqlite3.Connection) -> None:
        assert db.get_bounce_rate_by_stage(conn) == {}

    def test_no_bounces(self, conn: sqlite3.Connection, task_id: str) -> None:
        for i in range(3):
            sr = db.insert_stage_run(conn, task_id=task_id, stage="spec", attempt=i + 1)
            db.update_stage_run(conn, sr, status="passed")
        result = db.get_bounce_rate_by_stage(conn)
        assert result["spec"] == 0.0


class TestTaskLinks:
    def test_insert_and_get(self, conn: sqlite3.Connection, project_id: str) -> None:
        t1 = db.insert_task(conn, project_id=project_id, title="A")
        t2 = db.insert_task(conn, project_id=project_id, title="B")
        link_id = db.insert_task_link(
            conn, source_task_id=t1, target_task_id=t2, link_type="blocks"
        )
        links = db.get_task_links(conn, t1)
        assert len(links) == 1
        assert links[0]["id"] == link_id
        assert links[0]["link_type"] == "blocks"

    def test_get_links_as_target(
        self, conn: sqlite3.Connection, project_id: str
    ) -> None:
        t1 = db.insert_task(conn, project_id=project_id, title="A")
        t2 = db.insert_task(conn, project_id=project_id, title="B")
        db.insert_task_link(
            conn, source_task_id=t1, target_task_id=t2, link_type="blocks"
        )
        links = db.get_task_links(conn, t2)
        assert len(links) == 1

    def test_invalid_link_type(self, conn: sqlite3.Connection, project_id: str) -> None:
        t1 = db.insert_task(conn, project_id=project_id, title="A")
        t2 = db.insert_task(conn, project_id=project_id, title="B")
        with pytest.raises(ValueError, match="Invalid link_type"):
            db.insert_task_link(
                conn, source_task_id=t1, target_task_id=t2, link_type="invalid"
            )


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

    def test_filter_by_project_id(
        self, conn: sqlite3.Connection, project_id: str, task_id: str
    ) -> None:
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


# ---------------------------------------------------------------------------
# Flow field
# ---------------------------------------------------------------------------


class TestFlowField:
    def test_migrate_adds_flow_column(self) -> None:
        """Migration adds flow column to an existing DB without it."""
        c = db.get_connection(":memory:")
        # Create old schema without the flow column
        c.executescript("""
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                repo_path TEXT NOT NULL,
                default_branch TEXT NOT NULL DEFAULT 'main',
                gate_dir TEXT NOT NULL DEFAULT 'gates',
                skill_refs TEXT,
                created_at TEXT NOT NULL,
                config TEXT,
                pause_after_completion INTEGER NOT NULL DEFAULT 0,
                stage_timeouts TEXT
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id),
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                priority INTEGER NOT NULL DEFAULT 0,
                current_stage TEXT,
                status TEXT NOT NULL DEFAULT 'backlog',
                branch_name TEXT,
                spec_path TEXT,
                plan_path TEXT,
                review_path TEXT,
                skill_overrides TEXT,
                max_retries INTEGER NOT NULL DEFAULT 3,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS stage_runs (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(id),
                stage TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                prompt_sent TEXT,
                started_at TEXT,
                finished_at TEXT,
                duration_seconds REAL,
                claude_output TEXT,
                artifacts_produced TEXT,
                gate_name TEXT,
                gate_exit_code INTEGER,
                gate_stdout TEXT,
                gate_stderr TEXT,
                tokens_used INTEGER,
                error_message TEXT
            );
            CREATE TABLE IF NOT EXISTS task_links (
                id TEXT PRIMARY KEY,
                source_task_id TEXT NOT NULL REFERENCES tasks(id),
                target_task_id TEXT NOT NULL REFERENCES tasks(id),
                link_type TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS run_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                task_id TEXT,
                stage_run_id TEXT,
                metadata TEXT
            );
        """)
        # Insert a project and task before migration
        c.execute(
            "INSERT INTO projects (id, name, repo_path, created_at) VALUES ('p1', 'Old', '/tmp', '2025-01-01')"
        )
        c.execute(
            "INSERT INTO tasks (id, project_id, title, created_at, updated_at) VALUES ('t1', 'p1', 'Old task', '2025-01-01', '2025-01-01')"
        )
        c.commit()
        # Run migrate — should add the flow column
        db.migrate(c)
        row = c.execute("SELECT flow FROM tasks WHERE id = 't1'").fetchone()
        assert row[0] == "standard"

    def test_insert_task_default_flow(
        self, conn: sqlite3.Connection, project_id: str
    ) -> None:
        """insert_task without flow param defaults to 'standard'."""
        tid = db.insert_task(conn, project_id=project_id, title="Default flow")
        row = db.get_task(conn, tid)
        assert row["flow"] == "standard"

    def test_insert_task_with_flow_standard(
        self, conn: sqlite3.Connection, project_id: str
    ) -> None:
        tid = db.insert_task(
            conn, project_id=project_id, title="Std", flow="standard"
        )
        row = db.get_task(conn, tid)
        assert row["flow"] == "standard"

    def test_insert_task_with_flow_quick(
        self, conn: sqlite3.Connection, project_id: str
    ) -> None:
        tid = db.insert_task(
            conn, project_id=project_id, title="Quick", flow="quick"
        )
        row = db.get_task(conn, tid)
        assert row["flow"] == "quick"

    def test_insert_task_invalid_flow_rejected(
        self, conn: sqlite3.Connection, project_id: str
    ) -> None:
        with pytest.raises(ValueError, match="Invalid flow"):
            db.insert_task(
                conn, project_id=project_id, title="Bad", flow="invalid"
            )


# ---------------------------------------------------------------------------
# Escalated from quick field
# ---------------------------------------------------------------------------


class TestEscalatedFromQuick:
    def test_insert_task_escalated_from_quick_default(
        self, conn: sqlite3.Connection, project_id: str
    ) -> None:
        """Insert a task without specifying escalated_from_quick — defaults to 0."""
        tid = db.insert_task(conn, project_id=project_id, title="Default escalated")
        row = db.get_task(conn, tid)
        assert row["escalated_from_quick"] == 0

    def test_update_task_escalated_from_quick(
        self, conn: sqlite3.Connection, project_id: str
    ) -> None:
        """Update escalated_from_quick to 1 and verify."""
        tid = db.insert_task(conn, project_id=project_id, title="Escalate me")
        db.update_task(conn, tid, escalated_from_quick=1)
        row = db.get_task(conn, tid)
        assert row["escalated_from_quick"] == 1

    def test_task_response_includes_escalated_from_quick(
        self, conn: sqlite3.Connection, project_id: str
    ) -> None:
        """The escalated_from_quick column is present in task rows."""
        tid = db.insert_task(conn, project_id=project_id, title="Check column")
        row = db.get_task(conn, tid)
        assert "escalated_from_quick" in row.keys()
        assert row["escalated_from_quick"] == 0
