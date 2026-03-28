"""Tests for forge.engine module."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from forge import database as db
from forge.config import Settings
from forge.dispatcher import DispatchResult
from forge.engine import (
    PipelineEngine,
    _make_branch_name,
    _next_stage,
)
from forge.gate_runner import GateResult

# Save real methods before conftest's autouse fixture replaces them.
_real_start = PipelineEngine.start
_real_pause = PipelineEngine.pause


@pytest.fixture(autouse=True)
def _real_engine_methods(monkeypatch):
    """Undo the conftest no-op patches so engine tests use real start/pause."""
    monkeypatch.setattr(PipelineEngine, "start", _real_start)
    monkeypatch.setattr(PipelineEngine, "pause", _real_pause)


class _UnclosableConnection:
    """Wraps a sqlite3.Connection so that close() is a no-op.

    This prevents the engine loop from closing the shared test connection.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def close(self) -> None:
        pass  # no-op

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


@pytest.fixture
def conn() -> sqlite3.Connection:
    """In-memory database with schema applied."""
    c = db.get_connection(":memory:")
    db.migrate(c)
    return c


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def project_id(conn: sqlite3.Connection) -> str:
    return db.insert_project(
        conn, name="TestProject", repo_path="/tmp/repo", gate_dir="/tmp/repo/gates",
    )


@pytest.fixture
def active_task_with_queued_run(
    conn: sqlite3.Connection, project_id: str,
) -> tuple[str, str]:
    """Create an active task with a queued spec stage_run. Returns (task_id, stage_run_id)."""
    task_id = db.insert_task(
        conn, project_id=project_id, title="Test task", priority=10,
    )
    db.update_task(conn, task_id, status="active", current_stage="spec")
    sr_id = db.insert_stage_run(
        conn, task_id=task_id, stage="spec", attempt=1, status="queued",
    )
    return task_id, sr_id


# ---------------------------------------------------------------------------
# Unit tests for helper functions
# ---------------------------------------------------------------------------


class TestNextStage:
    def test_spec_to_plan(self) -> None:
        assert _next_stage("spec") == "plan"

    def test_plan_to_implement(self) -> None:
        assert _next_stage("plan") == "implement"

    def test_implement_to_review(self) -> None:
        assert _next_stage("implement") == "review"

    def test_review_to_none(self) -> None:
        assert _next_stage("review") is None

    def test_unknown_stage(self) -> None:
        assert _next_stage("unknown") is None


class TestMakeBranchName:
    def test_basic(self) -> None:
        name = _make_branch_name("abcd1234-5678-9abc-def0-123456789abc", "Add login page")
        assert name.startswith("forge/abcd1234-")
        assert "add-login-page" in name

    def test_special_chars(self) -> None:
        name = _make_branch_name("abcd1234-xxxx", "Hello, World! @#$%")
        assert name.startswith("forge/abcd1234-")
        # Special chars become hyphens
        assert "@" not in name

    def test_long_title_truncated(self) -> None:
        name = _make_branch_name("abcd1234-xxxx", "a" * 100)
        # The full branch name minus "forge/abcd1234-" prefix
        parts = name.split("forge/abcd1234-")
        assert len(parts[1]) <= 40


# ---------------------------------------------------------------------------
# Engine: advance_task
# ---------------------------------------------------------------------------


class TestAdvanceTask:
    @pytest.mark.asyncio
    async def test_spec_to_plan(
        self, conn: sqlite3.Connection, settings: Settings, project_id: str,
    ) -> None:
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(conn, project_id=project_id, title="T", priority=1)
        db.update_task(conn, task_id, status="active", current_stage="spec")

        await engine.advance_task(conn, task_id, "spec")

        task = db.get_task(conn, task_id)
        assert task["current_stage"] == "plan"
        runs = db.list_stage_runs(conn, task_id=task_id, stage="plan", status="queued")
        assert len(runs) == 1
        assert runs[0]["attempt"] == 1

    @pytest.mark.asyncio
    async def test_plan_to_implement(
        self, conn: sqlite3.Connection, settings: Settings, project_id: str,
    ) -> None:
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(conn, project_id=project_id, title="T", priority=1)
        db.update_task(conn, task_id, status="active", current_stage="plan")

        await engine.advance_task(conn, task_id, "plan")

        task = db.get_task(conn, task_id)
        assert task["current_stage"] == "implement"

    @pytest.mark.asyncio
    async def test_review_to_done(
        self, conn: sqlite3.Connection, settings: Settings, project_id: str,
    ) -> None:
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(conn, project_id=project_id, title="T", priority=1)
        db.update_task(conn, task_id, status="active", current_stage="review")

        await engine.advance_task(conn, task_id, "review")

        task = db.get_task(conn, task_id)
        assert task["status"] == "done"
        assert task["completed_at"] is not None
        # No more stage runs queued
        queued = db.list_stage_runs(conn, task_id=task_id, status="queued")
        assert len(queued) == 0


# ---------------------------------------------------------------------------
# Engine: bounce_task
# ---------------------------------------------------------------------------


class TestBounceTask:
    @pytest.mark.asyncio
    async def test_retry_on_bounce(
        self, conn: sqlite3.Connection, settings: Settings, project_id: str,
    ) -> None:
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn, project_id=project_id, title="T", priority=1, max_retries=3,
        )
        db.update_task(conn, task_id, status="active", current_stage="spec")
        # First attempt bounced
        db.insert_stage_run(
            conn, task_id=task_id, stage="spec", attempt=1, status="bounced",
        )

        task = dict(db.get_task(conn, task_id))
        gate_result = GateResult(
            passed=False, exit_code=1, stdout="", stderr="spec too short",
            gate_name="post-spec.sh", duration_seconds=1.0,
        )

        await engine.bounce_task(conn, task, "spec", gate_result)

        # Should have a new queued stage_run
        queued = db.list_stage_runs(conn, task_id=task_id, stage="spec", status="queued")
        assert len(queued) == 1
        assert queued[0]["attempt"] > 1

    @pytest.mark.asyncio
    async def test_needs_human_after_max_retries(
        self, conn: sqlite3.Connection, settings: Settings, project_id: str,
    ) -> None:
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn, project_id=project_id, title="T", priority=1, max_retries=2,
        )
        db.update_task(conn, task_id, status="active", current_stage="spec")
        # Simulate 2 bounced attempts (meeting max_retries)
        db.insert_stage_run(
            conn, task_id=task_id, stage="spec", attempt=1, status="bounced",
        )
        db.insert_stage_run(
            conn, task_id=task_id, stage="spec", attempt=2, status="bounced",
        )

        task = dict(db.get_task(conn, task_id))
        gate_result = GateResult(
            passed=False, exit_code=1, stdout="", stderr="still failing",
            gate_name="post-spec.sh", duration_seconds=1.0,
        )

        await engine.bounce_task(conn, task, "spec", gate_result)

        task = db.get_task(conn, task_id)
        assert task["status"] == "needs_human"


# ---------------------------------------------------------------------------
# Engine: timeout detection
# ---------------------------------------------------------------------------


class TestTimeoutDetection:
    def test_timeout_marks_error(
        self, conn: sqlite3.Connection, settings: Settings, project_id: str,
    ) -> None:
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn, project_id=project_id, title="T", priority=1, max_retries=3,
        )
        db.update_task(conn, task_id, status="active", current_stage="spec")

        # Create a running stage_run that started long ago
        old_time = (
            datetime.now(timezone.utc) - timedelta(seconds=9999)
        ).isoformat()
        sr_id = db.insert_stage_run(
            conn, task_id=task_id, stage="spec", attempt=1, status="running",
        )
        db.update_stage_run(conn, sr_id, started_at=old_time)

        engine._check_timeouts(conn, timeout_seconds=600)

        sr = db.get_stage_run(conn, sr_id)
        assert sr["status"] == "error"
        assert "timed out" in sr["error_message"]

    def test_no_timeout_for_recent_run(
        self, conn: sqlite3.Connection, settings: Settings, project_id: str,
    ) -> None:
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn, project_id=project_id, title="T", priority=1,
        )
        db.update_task(conn, task_id, status="active", current_stage="spec")

        recent_time = datetime.now(timezone.utc).isoformat()
        sr_id = db.insert_stage_run(
            conn, task_id=task_id, stage="spec", attempt=1, status="running",
        )
        db.update_stage_run(conn, sr_id, started_at=recent_time)

        engine._check_timeouts(conn, timeout_seconds=600)

        sr = db.get_stage_run(conn, sr_id)
        assert sr["status"] == "running"


# ---------------------------------------------------------------------------
# Engine: pause state
# ---------------------------------------------------------------------------


class TestEnginePause:
    @pytest.mark.asyncio
    async def test_pause_stops_loop(self, settings: Settings) -> None:
        engine = PipelineEngine(settings, ":memory:")
        engine.running = False
        # run_loop should exit immediately when running is False
        # We run it with a timeout to ensure it doesn't hang
        try:
            await asyncio.wait_for(engine.run_loop(), timeout=1.0)
        except asyncio.TimeoutError:
            pytest.fail("run_loop did not exit when running=False")

    @pytest.mark.asyncio
    async def test_start_sets_running(self, settings: Settings) -> None:
        engine = PipelineEngine(settings, ":memory:")
        # Set up a minimal DB for logging
        conn = db.get_connection(":memory:")
        db.migrate(conn)
        conn.close()

        # Patch get_connection to use in-memory DB
        with patch("forge.engine.database.get_connection") as mock_conn:
            mock_c = db.get_connection(":memory:")
            db.migrate(mock_c)
            mock_conn.return_value = mock_c
            # Start then immediately pause
            await engine.start()
            assert engine.running is True
            await engine.pause()
            assert engine.running is False
            # Give the loop a moment to exit
            if engine._loop_task:
                try:
                    await asyncio.wait_for(engine._loop_task, timeout=2.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    engine._loop_task.cancel()


# ---------------------------------------------------------------------------
# Engine: get_status and get_stats
# ---------------------------------------------------------------------------


class TestEngineStatus:
    def test_get_status(
        self, conn: sqlite3.Connection, settings: Settings, project_id: str,
    ) -> None:
        engine = PipelineEngine(settings, ":memory:")
        engine.running = True
        engine.current_task_id = "abc123"

        with patch("forge.engine.database.get_connection", return_value=conn):
            status = engine.get_status()
        assert status["running"] is True
        assert status["current_task_id"] == "abc123"
        assert status["queue_depth"] == 0

    def test_get_status_with_queued(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        active_task_with_queued_run: tuple[str, str],
    ) -> None:
        engine = PipelineEngine(settings, ":memory:")
        with patch("forge.engine.database.get_connection", return_value=conn):
            status = engine.get_status()
        assert status["queue_depth"] == 1

    def test_get_stats_empty(
        self, conn: sqlite3.Connection, settings: Settings,
    ) -> None:
        engine = PipelineEngine(settings, ":memory:")
        with patch("forge.engine.database.get_connection", return_value=conn):
            stats = engine.get_stats()
        assert stats["total_tasks"] == 0
        assert stats["total_stage_runs"] == 0
        assert stats["avg_stage_duration_seconds"] is None

    def test_get_stats_with_data(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        active_task_with_queued_run: tuple[str, str],
    ) -> None:
        task_id, sr_id = active_task_with_queued_run
        db.update_stage_run(conn, sr_id, duration_seconds=10.0, status="passed")

        engine = PipelineEngine(settings, ":memory:")
        with patch("forge.engine.database.get_connection", return_value=conn):
            stats = engine.get_stats()
        assert stats["total_tasks"] == 1
        assert stats["total_stage_runs"] == 1
        assert stats["avg_stage_duration_seconds"] == 10.0


# ---------------------------------------------------------------------------
# Engine: run_loop integration (mocked dispatcher + gate)
# ---------------------------------------------------------------------------


class TestRunLoopIntegration:
    async def _run_one_iteration(self, engine: PipelineEngine) -> None:
        """Run one engine loop iteration then stop."""
        engine.running = True
        loop_task = asyncio.create_task(engine.run_loop())
        await asyncio.sleep(0.5)
        engine.running = False
        try:
            await asyncio.wait_for(loop_task, timeout=3.0)
        except asyncio.TimeoutError:
            loop_task.cancel()

    @pytest.mark.asyncio
    async def test_full_stage_pass(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        active_task_with_queued_run: tuple[str, str],
    ) -> None:
        """Test that the engine picks up a task, dispatches, runs gate, and advances."""
        task_id, sr_id = active_task_with_queued_run
        db.update_task(conn, task_id, branch_name="forge/test-branch")

        dispatch_result = DispatchResult(
            output="spec content here",
            exit_code=0,
            duration_seconds=5.0,
            tokens_used=100,
        )
        gate_result = GateResult(
            passed=True, exit_code=0, stdout="ok", stderr="",
            gate_name="post-spec.sh", duration_seconds=1.0,
        )

        safe_conn = _UnclosableConnection(conn)
        engine = PipelineEngine(settings, ":memory:")

        with (
            patch("forge.engine.database.get_connection", return_value=safe_conn),
            patch("forge.engine.dispatch_claude", new_callable=AsyncMock, return_value=dispatch_result),
            patch("forge.engine.run_gate", new_callable=AsyncMock, return_value=gate_result),
            patch("forge.engine.build_prompt", return_value="test prompt"),
        ):
            await self._run_one_iteration(engine)

        # Verify: stage_run should be passed
        sr = db.get_stage_run(conn, sr_id)
        assert sr["status"] == "passed"

        # Task should have advanced to plan with a new queued stage_run
        task = db.get_task(conn, task_id)
        assert task["current_stage"] == "plan"
        plan_runs = db.list_stage_runs(conn, task_id=task_id, stage="plan", status="queued")
        assert len(plan_runs) == 1

    @pytest.mark.asyncio
    async def test_gate_failure_triggers_bounce(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        active_task_with_queued_run: tuple[str, str],
    ) -> None:
        """Test that a gate failure bounces the task."""
        task_id, sr_id = active_task_with_queued_run
        db.update_task(conn, task_id, branch_name="forge/test-branch")

        dispatch_result = DispatchResult(
            output="bad spec",
            exit_code=0,
            duration_seconds=3.0,
            tokens_used=50,
        )
        gate_result = GateResult(
            passed=False, exit_code=1, stdout="", stderr="spec missing sections",
            gate_name="post-spec.sh", duration_seconds=0.5,
        )

        safe_conn = _UnclosableConnection(conn)
        engine = PipelineEngine(settings, ":memory:")

        with (
            patch("forge.engine.database.get_connection", return_value=safe_conn),
            patch("forge.engine.dispatch_claude", new_callable=AsyncMock, return_value=dispatch_result),
            patch("forge.engine.run_gate", new_callable=AsyncMock, return_value=gate_result),
            patch("forge.engine.build_prompt", return_value="test prompt"),
        ):
            await self._run_one_iteration(engine)

        # Original stage_run should be bounced
        sr = db.get_stage_run(conn, sr_id)
        assert sr["status"] == "bounced"

        # A new queued stage_run for spec should exist
        queued = db.list_stage_runs(conn, task_id=task_id, stage="spec", status="queued")
        assert len(queued) == 1

    @pytest.mark.asyncio
    async def test_dispatch_error_retries(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        active_task_with_queued_run: tuple[str, str],
    ) -> None:
        """Test that a dispatch error triggers retry."""
        task_id, sr_id = active_task_with_queued_run
        db.update_task(conn, task_id, branch_name="forge/test-branch")

        dispatch_result = DispatchResult(
            output="",
            exit_code=1,
            duration_seconds=1.0,
            error="claude CLI not found in PATH",
        )

        safe_conn = _UnclosableConnection(conn)
        engine = PipelineEngine(settings, ":memory:")

        call_count = 0

        async def dispatch_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                engine.running = False  # stop after first dispatch
            return dispatch_result

        with (
            patch("forge.engine.database.get_connection", return_value=safe_conn),
            patch("forge.engine.dispatch_claude", side_effect=dispatch_side_effect),
            patch("forge.engine.build_prompt", return_value="test prompt"),
        ):
            engine.running = True
            loop_task = asyncio.create_task(engine.run_loop())
            try:
                await asyncio.wait_for(loop_task, timeout=3.0)
            except asyncio.TimeoutError:
                loop_task.cancel()

        # Original stage_run should be error
        sr = db.get_stage_run(conn, sr_id)
        assert sr["status"] == "error"

        # A retry should be queued
        queued = db.list_stage_runs(conn, task_id=task_id, stage="spec", status="queued")
        assert len(queued) == 1


# ---------------------------------------------------------------------------
# Engine: picks highest-priority task
# ---------------------------------------------------------------------------


class TestTaskPriority:
    def test_highest_priority_picked(
        self, conn: sqlite3.Connection, project_id: str,
    ) -> None:
        """Verify get_next_queued_task returns highest-priority task."""
        t1 = db.insert_task(
            conn, project_id=project_id, title="Low", priority=1,
        )
        db.update_task(conn, t1, status="active", current_stage="spec")
        db.insert_stage_run(conn, task_id=t1, stage="spec", attempt=1, status="queued")

        t2 = db.insert_task(
            conn, project_id=project_id, title="High", priority=10,
        )
        db.update_task(conn, t2, status="active", current_stage="spec")
        db.insert_stage_run(conn, task_id=t2, stage="spec", attempt=1, status="queued")

        picked = db.get_next_queued_task(conn)
        assert picked is not None
        assert picked["id"] == t2
