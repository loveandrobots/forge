"""Tests for per-stage timeout configuration."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from forge import database as db
from forge.config import EngineSettings, Settings, resolve_stage_timeout
from forge.engine import PipelineEngine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_reset_repo_state(monkeypatch):
    """Mock reset_repo_state so timeout tests don't need real git repos."""

    async def _noop_reset(repo_path: str, default_branch: str) -> dict:
        return {"success": True, "output": "mocked reset"}

    monkeypatch.setattr("forge.engine.reset_repo_state", _noop_reset)


@pytest.fixture
def conn() -> sqlite3.Connection:
    """In-memory database with schema applied."""
    c = db.get_connection(":memory:")
    db.migrate(c)
    return c


@pytest.fixture
def engine_settings() -> EngineSettings:
    return EngineSettings()


# ---------------------------------------------------------------------------
# Config-level tests (acceptance criteria 1, 2, 3, 8, 9)
# ---------------------------------------------------------------------------


class TestEngineSettingsDefaults:
    def test_default_stage_timeouts(self) -> None:
        """AC 1, 9: default stage_timeouts has implement=900."""
        es = EngineSettings()
        assert es.stage_timeouts == {"implement": 900}

    def test_default_stage_timeout_seconds(self) -> None:
        """AC 8: global default remains 600."""
        es = EngineSettings()
        assert es.stage_timeout_seconds == 600

    def test_engine_settings_from_dict(self) -> None:
        """AC 1: stage_timeouts loaded from config dict."""
        es = EngineSettings(
            stage_timeouts={"spec": 120, "implement": 1200},
        )
        assert es.stage_timeouts == {"spec": 120, "implement": 1200}


class TestResolveStageTimeout:
    def test_fallback_to_global_default(self, engine_settings: EngineSettings) -> None:
        """AC 2, 3: stages not in stage_timeouts fall back to stage_timeout_seconds."""
        assert resolve_stage_timeout("spec", None, engine_settings) == 600

    def test_per_stage_config(self, engine_settings: EngineSettings) -> None:
        """AC 1: implement uses per-stage config value."""
        assert resolve_stage_timeout("implement", None, engine_settings) == 900

    def test_empty_stage_timeouts(self) -> None:
        """AC 2: empty stage_timeouts dict falls back to global default."""
        es = EngineSettings(stage_timeouts={})
        assert resolve_stage_timeout("implement", None, es) == 600
        assert resolve_stage_timeout("spec", None, es) == 600

    def test_project_override_takes_precedence(self, engine_settings: EngineSettings) -> None:
        """AC 6, 7: project-level override wins over global per-stage config."""
        assert resolve_stage_timeout("implement", {"implement": 1200}, engine_settings) == 1200

    def test_full_fallback_chain(self) -> None:
        """AC 6, 7: three-tier resolution across all stages."""
        es = EngineSettings(stage_timeouts={"plan": 400, "implement": 900})
        project_timeouts = {"spec": 200}

        assert resolve_stage_timeout("spec", project_timeouts, es) == 200  # project
        assert resolve_stage_timeout("plan", project_timeouts, es) == 400  # global per-stage
        assert resolve_stage_timeout("implement", project_timeouts, es) == 900  # global per-stage
        assert resolve_stage_timeout("review", project_timeouts, es) == 600  # global default


# ---------------------------------------------------------------------------
# Database tests (acceptance criteria 4, 5, 10)
# ---------------------------------------------------------------------------


class TestDatabaseStageTimeouts:
    def test_migrate_adds_stage_timeouts_column(self) -> None:
        """AC 4, 5: migrate adds stage_timeouts column to existing databases."""
        c = db.get_connection(":memory:")
        # Create the table without stage_timeouts column
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
                pause_after_completion INTEGER NOT NULL DEFAULT 0
            );
        """)
        # Verify column doesn't exist yet
        cur = c.execute("PRAGMA table_info(projects)")
        columns = [row[1] for row in cur.fetchall()]
        assert "stage_timeouts" not in columns

        # Run migration
        db.migrate(c)

        # Verify column exists
        cur = c.execute("PRAGMA table_info(projects)")
        columns = [row[1] for row in cur.fetchall()]
        assert "stage_timeouts" in columns

    def test_insert_project_with_stage_timeouts(self, conn: sqlite3.Connection) -> None:
        """AC 10: stage_timeouts round-trips through insert/get."""
        pid = db.insert_project(
            conn,
            name="P1",
            repo_path="/tmp/r",
            stage_timeouts={"implement": 1200},
        )
        row = db.get_project(conn, pid)
        assert row is not None
        assert json.loads(row["stage_timeouts"]) == {"implement": 1200}

    def test_insert_project_without_stage_timeouts(self, conn: sqlite3.Connection) -> None:
        """AC 4: stage_timeouts is NULL when not provided."""
        pid = db.insert_project(
            conn,
            name="P2",
            repo_path="/tmp/r",
        )
        row = db.get_project(conn, pid)
        assert row is not None
        assert row["stage_timeouts"] is None

    def test_update_project_stage_timeouts(self, conn: sqlite3.Connection) -> None:
        """AC 10: stage_timeouts can be set via update."""
        pid = db.insert_project(conn, name="P3", repo_path="/tmp/r")
        db.update_project(conn, pid, stage_timeouts={"spec": 120})
        row = db.get_project(conn, pid)
        assert json.loads(row["stage_timeouts"]) == {"spec": 120}

    def test_update_project_clear_stage_timeouts(self, conn: sqlite3.Connection) -> None:
        """stage_timeouts can be cleared by setting to None."""
        pid = db.insert_project(
            conn, name="P4", repo_path="/tmp/r", stage_timeouts={"spec": 120}
        )
        db.update_project(conn, pid, stage_timeouts=None)
        row = db.get_project(conn, pid)
        assert row["stage_timeouts"] is None


# ---------------------------------------------------------------------------
# Engine integration tests (acceptance criteria 6, 7)
# ---------------------------------------------------------------------------


class TestCheckTimeoutsPerStage:
    async def test_implement_uses_longer_timeout(self, conn: sqlite3.Connection) -> None:
        """AC 7: implement stage at 700s should NOT time out (900s timeout)."""
        settings = Settings()
        engine = PipelineEngine(settings, ":memory:")

        pid = db.insert_project(conn, name="TP1", repo_path="/tmp/r")
        task_id = db.insert_task(conn, project_id=pid, title="T", max_retries=3)
        db.update_task(conn, task_id, status="active", current_stage="implement")

        old_time = (datetime.now(timezone.utc) - timedelta(seconds=700)).isoformat()
        sr_id = db.insert_stage_run(
            conn, task_id=task_id, stage="implement", attempt=1, status="running"
        )
        db.update_stage_run(conn, sr_id, started_at=old_time)

        await engine._check_timeouts(conn)

        sr = db.get_stage_run(conn, sr_id)
        assert sr["status"] == "running"  # 700 < 900, not timed out

    async def test_spec_times_out_at_default(self, conn: sqlite3.Connection) -> None:
        """AC 7: spec stage at 700s should time out (600s default timeout)."""
        settings = Settings()
        engine = PipelineEngine(settings, ":memory:")

        pid = db.insert_project(conn, name="TP2", repo_path="/tmp/r")
        task_id = db.insert_task(conn, project_id=pid, title="T", max_retries=3)
        db.update_task(conn, task_id, status="active", current_stage="spec")

        old_time = (datetime.now(timezone.utc) - timedelta(seconds=700)).isoformat()
        sr_id = db.insert_stage_run(
            conn, task_id=task_id, stage="spec", attempt=1, status="running"
        )
        db.update_stage_run(conn, sr_id, started_at=old_time)

        await engine._check_timeouts(conn)

        sr = db.get_stage_run(conn, sr_id)
        assert sr["status"] == "error"
        assert "timed out" in sr["error_message"]

    async def test_project_override_extends_timeout(self, conn: sqlite3.Connection) -> None:
        """AC 6, 7: project override of implement=1200 prevents timeout at 1000s."""
        settings = Settings()
        engine = PipelineEngine(settings, ":memory:")

        pid = db.insert_project(
            conn, name="TP3", repo_path="/tmp/r",
            stage_timeouts={"implement": 1200},
        )
        task_id = db.insert_task(conn, project_id=pid, title="T", max_retries=3)
        db.update_task(conn, task_id, status="active", current_stage="implement")

        old_time = (datetime.now(timezone.utc) - timedelta(seconds=1000)).isoformat()
        sr_id = db.insert_stage_run(
            conn, task_id=task_id, stage="implement", attempt=1, status="running"
        )
        db.update_stage_run(conn, sr_id, started_at=old_time)

        await engine._check_timeouts(conn)

        sr = db.get_stage_run(conn, sr_id)
        assert sr["status"] == "running"  # 1000 < 1200, not timed out
