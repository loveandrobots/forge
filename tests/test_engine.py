"""Tests for forge.engine module."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from forge import database as db
from forge.config import EngineSettings, Settings
from forge.dispatcher import DispatchResult, GitResult
from forge.engine import (
    PipelineEngine,
    _make_branch_name,
    _next_stage,
    _parse_stage_timeouts,
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


@pytest.fixture(autouse=True)
def _mock_reset_repo_state(monkeypatch):
    """Mock reset_repo_state so engine tests don't need real git repos."""

    async def _noop_reset(repo_path: str, default_branch: str) -> dict:
        return {"success": True, "output": "mocked reset"}

    monkeypatch.setattr("forge.engine.reset_repo_state", _noop_reset)


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
        conn,
        name="TestProject",
        repo_path="/tmp/repo",
        gate_dir="/tmp/repo/gates",
    )


@pytest.fixture
def active_task_with_queued_run(
    conn: sqlite3.Connection,
    project_id: str,
) -> tuple[str, str]:
    """Create an active task with a queued spec stage_run. Returns (task_id, stage_run_id)."""
    task_id = db.insert_task(
        conn,
        project_id=project_id,
        title="Test task",
        priority=10,
    )
    db.update_task(conn, task_id, status="active", current_stage="spec")
    sr_id = db.insert_stage_run(
        conn,
        task_id=task_id,
        stage="spec",
        attempt=1,
        status="queued",
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
        name = _make_branch_name(
            "abcd1234-5678-9abc-def0-123456789abc", "Add login page"
        )
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
    async def test_spec_to_plan(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
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

    async def test_plan_to_implement(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(conn, project_id=project_id, title="T", priority=1)
        db.update_task(conn, task_id, status="active", current_stage="plan")

        await engine.advance_task(conn, task_id, "plan")

        task = db.get_task(conn, task_id)
        assert task["current_stage"] == "implement"

    async def test_review_to_done(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
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
    async def test_retry_on_bounce(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn,
            project_id=project_id,
            title="T",
            priority=1,
            max_retries=3,
        )
        db.update_task(conn, task_id, status="active", current_stage="spec")
        # First attempt bounced
        db.insert_stage_run(
            conn,
            task_id=task_id,
            stage="spec",
            attempt=1,
            status="bounced",
        )

        task = dict(db.get_task(conn, task_id))
        gate_result = GateResult(
            passed=False,
            exit_code=1,
            stdout="",
            stderr="spec too short",
            gate_name="post-spec.sh",
            duration_seconds=1.0,
        )

        await engine.bounce_task(conn, task, "spec", gate_result)

        # Should have a new queued stage_run
        queued = db.list_stage_runs(
            conn, task_id=task_id, stage="spec", status="queued"
        )
        assert len(queued) == 1
        assert queued[0]["attempt"] > 1

    async def test_needs_human_after_max_retries(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn,
            project_id=project_id,
            title="T",
            priority=1,
            max_retries=2,
        )
        db.update_task(conn, task_id, status="active", current_stage="spec")
        # Simulate 2 bounced attempts (meeting max_retries)
        db.insert_stage_run(
            conn,
            task_id=task_id,
            stage="spec",
            attempt=1,
            status="bounced",
        )
        db.insert_stage_run(
            conn,
            task_id=task_id,
            stage="spec",
            attempt=2,
            status="bounced",
        )

        task = dict(db.get_task(conn, task_id))
        gate_result = GateResult(
            passed=False,
            exit_code=1,
            stdout="",
            stderr="still failing",
            gate_name="post-spec.sh",
            duration_seconds=1.0,
        )

        await engine.bounce_task(conn, task, "spec", gate_result)

        task = db.get_task(conn, task_id)
        assert task["status"] == "needs_human"

    async def test_bounce_attempt_number_sequential(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """After first bounce, next attempt should be 2 (not 3)."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn, project_id=project_id, title="T", priority=1, max_retries=5
        )
        db.update_task(conn, task_id, status="active", current_stage="plan")
        db.insert_stage_run(
            conn, task_id=task_id, stage="plan", attempt=1, status="bounced"
        )

        task = dict(db.get_task(conn, task_id))
        gate_result = GateResult(
            passed=False, exit_code=1, stdout="", stderr="issues",
            gate_name="post-plan.sh", duration_seconds=1.0,
        )

        await engine.bounce_task(conn, task, "plan", gate_result)

        queued = db.list_stage_runs(
            conn, task_id=task_id, stage="plan", status="queued"
        )
        assert len(queued) == 1
        assert queued[0]["attempt"] == 2

    async def test_bounce_attempt_number_after_multiple_retries(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """Sequential numbering across multiple bounces."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn, project_id=project_id, title="T", priority=1, max_retries=5
        )
        db.update_task(conn, task_id, status="active", current_stage="spec")
        db.insert_stage_run(
            conn, task_id=task_id, stage="spec", attempt=1, status="bounced"
        )
        db.insert_stage_run(
            conn, task_id=task_id, stage="spec", attempt=2, status="bounced"
        )

        task = dict(db.get_task(conn, task_id))
        gate_result = GateResult(
            passed=False, exit_code=1, stdout="", stderr="issues",
            gate_name="post-spec.sh", duration_seconds=1.0,
        )

        await engine.bounce_task(conn, task, "spec", gate_result)

        queued = db.list_stage_runs(
            conn, task_id=task_id, stage="spec", status="queued"
        )
        assert len(queued) == 1
        assert queued[0]["attempt"] == 3

    async def test_bounce_task_no_type_metadata_in_logs(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """Regression: bounce_task must not log Python type info (e.g. type=)."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn,
            project_id=project_id,
            title="T",
            priority=1,
            max_retries=3,
        )
        gate_result = GateResult(
            passed=False,
            exit_code=1,
            stdout="",
            stderr="issues",
            gate_name="post-spec.sh",
            duration_seconds=1.0,
        )
        logged_messages: list[str] = []
        original_log = engine._log

        def capture_log(level: str, msg: str, **kwargs: object) -> None:
            logged_messages.append(msg)
            original_log(level, msg, **kwargs)

        engine._log = capture_log  # type: ignore[assignment]

        # Exercise "spec" bounce path (non-review branch)
        db.update_task(conn, task_id, status="active", current_stage="spec")
        db.insert_stage_run(
            conn, task_id=task_id, stage="spec", attempt=1, status="bounced",
        )
        task = dict(db.get_task(conn, task_id))
        await engine.bounce_task(conn, task, "spec", gate_result)

        # Verify bounce_task created a queued stage_run for spec
        spec_queued = db.list_stage_runs(conn, task_id=task_id, stage="spec", status="queued")
        assert len(spec_queued) >= 1, "bounce_task should insert a queued spec stage_run"

        # Exercise "review" bounce path
        db.update_task(conn, task_id, current_stage="review")
        db.insert_stage_run(
            conn, task_id=task_id, stage="implement", attempt=1, status="done",
        )
        db.insert_stage_run(
            conn, task_id=task_id, stage="review", attempt=1, status="bounced",
        )
        task = dict(db.get_task(conn, task_id))
        await engine.bounce_task(conn, task, "review", gate_result)

        # Verify bounce_task created a queued stage_run for implement (review bounces back)
        impl_queued = db.list_stage_runs(conn, task_id=task_id, stage="implement", status="queued")
        assert len(impl_queued) >= 1, "bounce_task should insert a queued implement stage_run on review bounce"

        assert logged_messages, "Expected at least one log message from bounce_task"
        for msg in logged_messages:
            assert "type=" not in msg, f"Log message leaks type metadata: {msg}"
            assert "type(" not in msg, f"Log message leaks type metadata: {msg}"


class TestErrorRetryAttemptNumber:
    async def test_error_retry_attempt_number_sequential(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """After first error, retry attempt should be 2 (not 3)."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn, project_id=project_id, title="T", priority=1, max_retries=5
        )
        db.update_task(conn, task_id, status="active", current_stage="implement")
        sr_id = db.insert_stage_run(
            conn, task_id=task_id, stage="implement", attempt=1, status="error"
        )

        task = dict(db.get_task(conn, task_id))
        await engine._handle_error_retry(conn, task, "implement", sr_id)

        queued = db.list_stage_runs(
            conn, task_id=task_id, stage="implement", status="queued"
        )
        assert len(queued) == 1
        assert queued[0]["attempt"] == 2

    async def test_error_retry_attempt_number_after_multiple_errors(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """Sequential numbering across multiple error retries."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn, project_id=project_id, title="T", priority=1, max_retries=5
        )
        db.update_task(conn, task_id, status="active", current_stage="plan")
        db.insert_stage_run(
            conn, task_id=task_id, stage="plan", attempt=1, status="error"
        )
        sr_id = db.insert_stage_run(
            conn, task_id=task_id, stage="plan", attempt=2, status="error"
        )

        task = dict(db.get_task(conn, task_id))
        await engine._handle_error_retry(conn, task, "plan", sr_id)

        queued = db.list_stage_runs(
            conn, task_id=task_id, stage="plan", status="queued"
        )
        assert len(queued) == 1
        assert queued[0]["attempt"] == 3


# ---------------------------------------------------------------------------
# Engine: timeout detection
# ---------------------------------------------------------------------------


class TestTimeoutDetection:
    async def test_timeout_marks_error(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn,
            project_id=project_id,
            title="T",
            priority=1,
            max_retries=3,
        )
        db.update_task(conn, task_id, status="active", current_stage="spec")

        # Create a running stage_run that started long ago
        old_time = (datetime.now(timezone.utc) - timedelta(seconds=9999)).isoformat()
        sr_id = db.insert_stage_run(
            conn,
            task_id=task_id,
            stage="spec",
            attempt=1,
            status="running",
        )
        db.update_stage_run(conn, sr_id, started_at=old_time)

        await engine._check_timeouts(conn)

        sr = db.get_stage_run(conn, sr_id)
        assert sr["status"] == "error"
        assert "timed out" in sr["error_message"]

    async def test_no_timeout_for_recent_run(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn,
            project_id=project_id,
            title="T",
            priority=1,
        )
        db.update_task(conn, task_id, status="active", current_stage="spec")

        recent_time = datetime.now(timezone.utc).isoformat()
        sr_id = db.insert_stage_run(
            conn,
            task_id=task_id,
            stage="spec",
            attempt=1,
            status="running",
        )
        db.update_stage_run(conn, sr_id, started_at=recent_time)

        await engine._check_timeouts(conn)

        sr = db.get_stage_run(conn, sr_id)
        assert sr["status"] == "running"


# ---------------------------------------------------------------------------
# Engine: pause state
# ---------------------------------------------------------------------------


class TestEnginePause:
    async def test_pause_stops_loop(self, settings: Settings) -> None:
        engine = PipelineEngine(settings, ":memory:")
        engine.running = False
        # run_loop should exit immediately when running is False
        # We run it with a timeout to ensure it doesn't hang
        try:
            await asyncio.wait_for(engine.run_loop(), timeout=1.0)
        except asyncio.TimeoutError:
            pytest.fail("run_loop did not exit when running=False")

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
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
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
        self,
        conn: sqlite3.Connection,
        settings: Settings,
    ) -> None:
        engine = PipelineEngine(settings, ":memory:")
        with patch("forge.engine.database.get_connection", return_value=conn):
            stats = engine.get_stats()
        assert stats["total_tasks"] == 0
        assert stats["total_stage_runs"] == 0
        assert stats["avg_stage_duration_seconds"] is None
        assert stats["total_completed"] == 0
        assert stats["total_active"] == 0
        assert stats["avg_duration_by_stage"] == {}
        assert stats["bounce_rate_by_stage"] == {}

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
        assert stats["total_completed"] == 0
        assert stats["total_active"] == 1
        assert stats["avg_duration_by_stage"] == {"spec": 10.0}
        assert stats["bounce_rate_by_stage"] == {"spec": 0.0}

    def test_get_stats_with_completed_and_bounced(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        # Create a done task and an active task
        t1 = db.insert_task(conn, project_id=project_id, title="Done task", priority=1)
        db.update_task(conn, t1, status="done")
        t2 = db.insert_task(
            conn, project_id=project_id, title="Active task", priority=2
        )
        db.update_task(conn, t2, status="active")

        # Create stage runs: 1 passed, 1 bounced for "spec"
        sr1 = db.insert_stage_run(conn, task_id=t1, stage="spec", attempt=1)
        db.update_stage_run(conn, sr1, status="passed", duration_seconds=10.0)
        sr2 = db.insert_stage_run(conn, task_id=t2, stage="spec", attempt=1)
        db.update_stage_run(conn, sr2, status="bounced", duration_seconds=5.0)

        engine = PipelineEngine(settings, ":memory:")
        with patch("forge.engine.database.get_connection", return_value=conn):
            stats = engine.get_stats()
        assert stats["total_completed"] == 1
        assert stats["total_active"] == 1
        assert abs(stats["avg_duration_by_stage"]["spec"] - 7.5) < 0.01
        assert abs(stats["bounce_rate_by_stage"]["spec"] - 0.5) < 0.01


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
            passed=True,
            exit_code=0,
            stdout="ok",
            stderr="",
            gate_name="post-spec.sh",
            duration_seconds=1.0,
        )

        safe_conn = _UnclosableConnection(conn)
        engine = PipelineEngine(settings, ":memory:")

        with (
            patch("forge.engine.database.get_connection", return_value=safe_conn),
            patch(
                "forge.engine.dispatch_claude",
                new_callable=AsyncMock,
                return_value=dispatch_result,
            ),
            patch(
                "forge.engine.run_gate",
                new_callable=AsyncMock,
                return_value=gate_result,
            ),
            patch("forge.engine.build_prompt", return_value="test prompt"),
        ):
            await self._run_one_iteration(engine)

        # Verify: stage_run should be passed
        sr = db.get_stage_run(conn, sr_id)
        assert sr["status"] == "passed"

        # Task should have advanced to plan with a new queued stage_run
        task = db.get_task(conn, task_id)
        assert task["current_stage"] == "plan"
        plan_runs = db.list_stage_runs(
            conn, task_id=task_id, stage="plan", status="queued"
        )
        assert len(plan_runs) == 1

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
            passed=False,
            exit_code=1,
            stdout="",
            stderr="spec missing sections",
            gate_name="post-spec.sh",
            duration_seconds=0.5,
        )

        safe_conn = _UnclosableConnection(conn)
        engine = PipelineEngine(settings, ":memory:")

        with (
            patch("forge.engine.database.get_connection", return_value=safe_conn),
            patch(
                "forge.engine.dispatch_claude",
                new_callable=AsyncMock,
                return_value=dispatch_result,
            ),
            patch(
                "forge.engine.run_gate",
                new_callable=AsyncMock,
                return_value=gate_result,
            ),
            patch("forge.engine.build_prompt", return_value="test prompt"),
        ):
            await self._run_one_iteration(engine)

        # Original stage_run should be bounced
        sr = db.get_stage_run(conn, sr_id)
        assert sr["status"] == "bounced"

        # A new queued stage_run for spec should exist
        queued = db.list_stage_runs(
            conn, task_id=task_id, stage="spec", status="queued"
        )
        assert len(queued) == 1

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
        queued = db.list_stage_runs(
            conn, task_id=task_id, stage="spec", status="queued"
        )
        assert len(queued) == 1


# ---------------------------------------------------------------------------
# Engine: picks highest-priority task
# ---------------------------------------------------------------------------


class TestActivateBacklogTasks:
    def test_activates_backlog_task(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """Backlog task gets activated with a queued stage_run for the first stage."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn,
            project_id=project_id,
            title="Backlog task",
            priority=5,
        )

        safe_conn = _UnclosableConnection(conn)
        with patch("forge.engine.database.get_connection", return_value=safe_conn):
            engine._activate_backlog_tasks(conn)

        task = db.get_task(conn, task_id)
        assert task["status"] == "active"
        assert task["current_stage"] == "spec"
        runs = db.list_stage_runs(conn, task_id=task_id, stage="spec", status="queued")
        assert len(runs) == 1
        assert runs[0]["attempt"] == 1

    def test_respects_concurrency_limit(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """Only activates up to max_concurrent_tasks backlog tasks."""
        settings.engine.max_concurrent_tasks = 2
        engine = PipelineEngine(settings, ":memory:")

        task_ids = []
        for i in range(4):
            tid = db.insert_task(
                conn,
                project_id=project_id,
                title=f"Task {i}",
                priority=i,
            )
            task_ids.append(tid)

        safe_conn = _UnclosableConnection(conn)
        with patch("forge.engine.database.get_connection", return_value=safe_conn):
            engine._activate_backlog_tasks(conn)

        active = db.list_tasks(conn, status="active")
        backlog = db.list_tasks(conn, status="backlog")
        assert len(active) == 2
        assert len(backlog) == 2

    def test_skips_when_at_capacity(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """Does not activate backlog tasks when active count meets concurrency limit."""
        settings.engine.max_concurrent_tasks = 1
        engine = PipelineEngine(settings, ":memory:")

        # Create one already-active task
        active_id = db.insert_task(
            conn,
            project_id=project_id,
            title="Active",
            priority=10,
        )
        db.update_task(conn, active_id, status="active", current_stage="spec")

        # Create a backlog task
        backlog_id = db.insert_task(
            conn,
            project_id=project_id,
            title="Waiting",
            priority=5,
        )

        safe_conn = _UnclosableConnection(conn)
        with patch("forge.engine.database.get_connection", return_value=safe_conn):
            engine._activate_backlog_tasks(conn)

        task = db.get_task(conn, backlog_id)
        assert task["status"] == "backlog"

    async def test_engine_loop_picks_up_backlog_task(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """Full engine loop activates a backlog task and dispatches it."""
        task_id = db.insert_task(
            conn,
            project_id=project_id,
            title="From backlog",
            priority=5,
        )

        dispatch_result = DispatchResult(
            output="spec content",
            exit_code=0,
            duration_seconds=2.0,
            tokens_used=50,
        )
        gate_result = GateResult(
            passed=True,
            exit_code=0,
            stdout="ok",
            stderr="",
            gate_name="post-spec.sh",
            duration_seconds=0.5,
        )

        safe_conn = _UnclosableConnection(conn)
        engine = PipelineEngine(settings, ":memory:")

        with (
            patch("forge.engine.database.get_connection", return_value=safe_conn),
            patch(
                "forge.engine.dispatch_claude",
                new_callable=AsyncMock,
                return_value=dispatch_result,
            ),
            patch(
                "forge.engine.run_gate",
                new_callable=AsyncMock,
                return_value=gate_result,
            ),
            patch("forge.engine.build_prompt", return_value="test prompt"),
            patch(
                "forge.engine.create_branch", new_callable=AsyncMock, return_value=GitResult(success=True)
            ),
        ):
            engine.running = True
            loop_task = asyncio.create_task(engine.run_loop())
            await asyncio.sleep(0.5)
            engine.running = False
            try:
                await asyncio.wait_for(loop_task, timeout=3.0)
            except asyncio.TimeoutError:
                loop_task.cancel()

        task = db.get_task(conn, task_id)
        assert task["status"] == "active"
        assert task["current_stage"] == "plan"
        # The spec stage_run should be passed
        spec_runs = db.list_stage_runs(
            conn, task_id=task_id, stage="spec", status="passed"
        )
        assert len(spec_runs) == 1

    def test_activate_quick_flow_starts_at_implement(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """Quick flow backlog task starts at implement, not spec."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn,
            project_id=project_id,
            title="Quick task",
            priority=5,
            flow="quick",
        )

        safe_conn = _UnclosableConnection(conn)
        with patch("forge.engine.database.get_connection", return_value=safe_conn):
            engine._activate_backlog_tasks(conn)

        task = db.get_task(conn, task_id)
        assert task["status"] == "active"
        assert task["current_stage"] == "implement"
        runs = db.list_stage_runs(conn, task_id=task_id, stage="implement", status="queued")
        assert len(runs) == 1
        # No spec or plan stage_runs should exist
        spec_runs = db.list_stage_runs(conn, task_id=task_id, stage="spec")
        plan_runs = db.list_stage_runs(conn, task_id=task_id, stage="plan")
        assert len(spec_runs) == 0
        assert len(plan_runs) == 0


# ---------------------------------------------------------------------------
# _next_stage with flow parameter
# ---------------------------------------------------------------------------


class TestNextStageFlow:
    def test_standard_flow_spec_to_plan(self) -> None:
        assert _next_stage("spec", "standard") == "plan"

    def test_standard_flow_plan_to_implement(self) -> None:
        assert _next_stage("plan", "standard") == "implement"

    def test_standard_flow_implement_to_review(self) -> None:
        assert _next_stage("implement", "standard") == "review"

    def test_standard_flow_review_to_none(self) -> None:
        assert _next_stage("review", "standard") is None

    def test_quick_flow_implement_to_review(self) -> None:
        assert _next_stage("implement", "quick") == "review"

    def test_quick_flow_review_to_none(self) -> None:
        assert _next_stage("review", "quick") is None

    def test_quick_flow_spec_returns_none(self) -> None:
        """spec is not in quick flow, so _next_stage returns None."""
        assert _next_stage("spec", "quick") is None

    def test_quick_flow_plan_returns_none(self) -> None:
        """plan is not in quick flow, so _next_stage returns None."""
        assert _next_stage("plan", "quick") is None

    def test_default_flow_param_is_standard(self) -> None:
        """Calling without flow param behaves as standard."""
        assert _next_stage("spec") == "plan"


# ---------------------------------------------------------------------------
# advance_task with flow
# ---------------------------------------------------------------------------


class TestAdvanceTaskFlow:
    async def test_quick_flow_implement_to_review(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn, project_id=project_id, title="Quick", priority=1, flow="quick"
        )
        db.update_task(conn, task_id, status="active", current_stage="implement")

        await engine.advance_task(conn, task_id, "implement")

        task = db.get_task(conn, task_id)
        assert task["current_stage"] == "review"
        runs = db.list_stage_runs(conn, task_id=task_id, stage="review", status="queued")
        assert len(runs) == 1

    async def test_quick_flow_review_to_done(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn, project_id=project_id, title="Quick", priority=1, flow="quick"
        )
        db.update_task(conn, task_id, status="active", current_stage="review")

        await engine.advance_task(conn, task_id, "review")

        task = db.get_task(conn, task_id)
        assert task["status"] == "done"
        assert task["completed_at"] is not None

    async def test_quick_flow_no_spec_plan_runs(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """Full quick flow progression creates no spec or plan runs."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn, project_id=project_id, title="Quick", priority=1, flow="quick"
        )
        db.update_task(conn, task_id, status="active", current_stage="implement")

        # Advance implement -> review
        await engine.advance_task(conn, task_id, "implement")
        # Advance review -> done
        await engine.advance_task(conn, task_id, "review")

        all_runs = db.list_stage_runs(conn, task_id=task_id)
        stages = {r["stage"] for r in all_runs}
        assert "spec" not in stages
        assert "plan" not in stages


# ---------------------------------------------------------------------------
# Engine: auto-pause after task completion
# ---------------------------------------------------------------------------


class TestAutoPause:
    @pytest.fixture
    def pause_project_id(self, conn: sqlite3.Connection) -> str:
        return db.insert_project(
            conn,
            name="PauseProject",
            repo_path="/tmp/repo",
            gate_dir="/tmp/repo/gates",
            pause_after_completion=True,
        )

    @pytest.fixture
    def no_pause_project_id(self, conn: sqlite3.Connection) -> str:
        return db.insert_project(
            conn,
            name="NoPauseProject",
            repo_path="/tmp/repo",
            gate_dir="/tmp/repo/gates",
            pause_after_completion=False,
        )

    async def test_auto_pause_on_task_done(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        pause_project_id: str,
    ) -> None:
        """Engine pauses when a task completes for a pause-enabled project."""
        engine = PipelineEngine(settings, ":memory:")
        engine.running = True
        task_id = db.insert_task(
            conn,
            project_id=pause_project_id,
            title="My Task",
            priority=1,
        )
        db.update_task(conn, task_id, status="active", current_stage="review")

        project = dict(db.get_project(conn, pause_project_id))
        await engine.advance_task(conn, task_id, "review", project=project)

        task = db.get_task(conn, task_id)
        assert task["status"] == "done"
        assert engine.running is False

    async def test_no_auto_pause_when_flag_is_false(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        no_pause_project_id: str,
    ) -> None:
        """Engine continues when a task completes for a non-pause project."""
        engine = PipelineEngine(settings, ":memory:")
        engine.running = True
        task_id = db.insert_task(
            conn,
            project_id=no_pause_project_id,
            title="My Task",
            priority=1,
        )
        db.update_task(conn, task_id, status="active", current_stage="review")

        project = dict(db.get_project(conn, no_pause_project_id))
        await engine.advance_task(conn, task_id, "review", project=project)

        task = db.get_task(conn, task_id)
        assert task["status"] == "done"
        assert engine.running is True

    async def test_auto_pause_on_needs_human_from_bounce(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        pause_project_id: str,
    ) -> None:
        """Engine pauses when bounce_task marks needs_human for a pause-enabled project."""
        engine = PipelineEngine(settings, ":memory:")
        engine.running = True
        task_id = db.insert_task(
            conn,
            project_id=pause_project_id,
            title="Bounced Task",
            priority=1,
            max_retries=1,
        )
        db.update_task(conn, task_id, status="active", current_stage="spec")
        # Create enough bounced runs to exceed max_retries
        db.insert_stage_run(
            conn, task_id=task_id, stage="spec", attempt=1, status="bounced"
        )

        task = dict(db.get_task(conn, task_id))
        project = dict(db.get_project(conn, pause_project_id))
        gate_result = GateResult(
            passed=False,
            exit_code=1,
            stdout="",
            stderr="fail",
            gate_name="post-spec.sh",
            duration_seconds=1.0,
        )
        await engine.bounce_task(conn, task, "spec", gate_result, project=project)

        task = db.get_task(conn, task_id)
        assert task["status"] == "needs_human"
        assert engine.running is False

    async def test_auto_pause_on_needs_human_from_error_retry(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        pause_project_id: str,
    ) -> None:
        """Engine pauses when _handle_error_retry marks needs_human for a pause-enabled project."""
        engine = PipelineEngine(settings, ":memory:")
        engine.running = True
        task_id = db.insert_task(
            conn,
            project_id=pause_project_id,
            title="Error Task",
            priority=1,
            max_retries=1,
        )
        db.update_task(conn, task_id, status="active", current_stage="spec")
        sr_id = db.insert_stage_run(
            conn, task_id=task_id, stage="spec", attempt=1, status="error"
        )
        # One error run already meets max_retries=1

        task = dict(db.get_task(conn, task_id))
        project = dict(db.get_project(conn, pause_project_id))
        await engine._handle_error_retry(conn, task, "spec", sr_id, project=project)

        task = db.get_task(conn, task_id)
        assert task["status"] == "needs_human"
        assert engine.running is False

    async def test_auto_pause_message_format(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        pause_project_id: str,
    ) -> None:
        """Auto-pause log message matches the exact format from the spec."""
        engine = PipelineEngine(settings, ":memory:")
        engine.running = True
        safe_conn = _UnclosableConnection(conn)
        with patch("forge.engine.database.get_connection", return_value=safe_conn):
            task_id = db.insert_task(
                conn,
                project_id=pause_project_id,
                title="Special Task",
                priority=1,
            )
            db.update_task(conn, task_id, status="active", current_stage="review")
            project = dict(db.get_project(conn, pause_project_id))

            await engine.advance_task(conn, task_id, "review", project=project)

        logs = db.get_logs(conn, task_id=task_id)
        auto_pause_logs = [row for row in logs if "auto-paused" in row["message"]]
        assert len(auto_pause_logs) == 1
        expected = (
            "Engine auto-paused after completing task 'Special Task' for project "
            "'PauseProject'. Restart the service and unpause to continue."
        )
        assert auto_pause_logs[0]["message"] == expected


# ---------------------------------------------------------------------------
# Engine: review bounce to implement
# ---------------------------------------------------------------------------


class TestReviewBounceToImplement:
    """Tests for the review→implement bounce behavior (AC 1, 2, 3, 4, 13, 14, 15, 16, 18, 20, 21)."""

    async def test_review_bounce_creates_implement_stage_run(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """AC 1, 2, 16: Review ISSUES verdict bounces to implement, not review."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn, project_id=project_id, title="T", priority=1, max_retries=3
        )
        db.update_task(conn, task_id, status="active", current_stage="review")
        # Original implement run passed
        db.insert_stage_run(
            conn, task_id=task_id, stage="implement", attempt=1, status="passed"
        )
        # Review attempt 1 bounced
        db.insert_stage_run(
            conn, task_id=task_id, stage="review", attempt=1, status="bounced"
        )

        task = dict(db.get_task(conn, task_id))
        gate_result = GateResult(
            passed=False, exit_code=1, stdout="", stderr="ISSUES found",
            gate_name="post-review.sh", duration_seconds=1.0,
        )

        await engine.bounce_task(conn, task, "review", gate_result)

        # Task current_stage should be implement
        task = db.get_task(conn, task_id)
        assert task["current_stage"] == "implement"

        # New implement stage_run should be queued
        queued_implement = db.list_stage_runs(
            conn, task_id=task_id, stage="implement", status="queued"
        )
        assert len(queued_implement) == 1

        # No new review stage_run should be queued
        queued_review = db.list_stage_runs(
            conn, task_id=task_id, stage="review", status="queued"
        )
        assert len(queued_review) == 0

    async def test_implement_attempt_increments_after_review_bounce(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """AC 3: Implement attempt number is based on prior implement runs."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn, project_id=project_id, title="T", priority=1, max_retries=5
        )
        db.update_task(conn, task_id, status="active", current_stage="review")
        # Original implement passed (attempt 1)
        db.insert_stage_run(
            conn, task_id=task_id, stage="implement", attempt=1, status="passed"
        )
        # Review bounced
        db.insert_stage_run(
            conn, task_id=task_id, stage="review", attempt=1, status="bounced"
        )

        task = dict(db.get_task(conn, task_id))
        gate_result = GateResult(
            passed=False, exit_code=1, stdout="", stderr="ISSUES",
            gate_name="post-review.sh", duration_seconds=1.0,
        )

        await engine.bounce_task(conn, task, "review", gate_result)

        queued = db.list_stage_runs(
            conn, task_id=task_id, stage="implement", status="queued"
        )
        assert len(queued) == 1
        # 1 prior implement run (passed), so new_attempt = 1+1 = 2
        assert queued[0]["attempt"] == 2

    async def test_spec_plan_implement_bounces_stay_same_stage(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """AC 4, 21: Non-review bounces stay on the same stage."""
        engine = PipelineEngine(settings, ":memory:")
        gate_result = GateResult(
            passed=False, exit_code=1, stdout="", stderr="fail",
            gate_name="gate.sh", duration_seconds=1.0,
        )

        for stage in ("spec", "plan", "implement"):
            task_id = db.insert_task(
                conn, project_id=project_id, title=f"T-{stage}", priority=1, max_retries=3
            )
            db.update_task(conn, task_id, status="active", current_stage=stage)
            db.insert_stage_run(
                conn, task_id=task_id, stage=stage, attempt=1, status="bounced"
            )

            task = dict(db.get_task(conn, task_id))
            await engine.bounce_task(conn, task, stage, gate_result)

            queued = db.list_stage_runs(
                conn, task_id=task_id, stage=stage, status="queued"
            )
            assert len(queued) == 1, f"{stage} should bounce to same stage"

    async def test_successful_implement_after_review_bounce_advances_to_review(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """AC 15, 18: Successful implement after review bounce advances to review with attempt=1."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn, project_id=project_id, title="T", priority=1
        )
        db.update_task(conn, task_id, status="active", current_stage="implement")
        # Prior bounced review exists
        db.insert_stage_run(
            conn, task_id=task_id, stage="review", attempt=1, status="bounced"
        )

        await engine.advance_task(conn, task_id, "implement")

        task = db.get_task(conn, task_id)
        assert task["current_stage"] == "review"
        review_runs = db.list_stage_runs(
            conn, task_id=task_id, stage="review", status="queued"
        )
        assert len(review_runs) == 1
        assert review_runs[0]["attempt"] == 1

    async def test_max_retries_respected_across_implement_review_loop(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """AC 13, 14, 20: max_retries is shared across implement→review loop."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn, project_id=project_id, title="T", priority=1, max_retries=2
        )
        db.update_task(conn, task_id, status="active", current_stage="review")
        # 2 bounced runs across implement+review
        db.insert_stage_run(
            conn, task_id=task_id, stage="implement", attempt=1, status="bounced"
        )
        db.insert_stage_run(
            conn, task_id=task_id, stage="review", attempt=1, status="bounced"
        )

        task = dict(db.get_task(conn, task_id))
        gate_result = GateResult(
            passed=False, exit_code=1, stdout="", stderr="ISSUES",
            gate_name="post-review.sh", duration_seconds=1.0,
        )

        await engine.bounce_task(conn, task, "review", gate_result)

        task = db.get_task(conn, task_id)
        assert task["status"] == "needs_human"

    async def test_implement_attempt_sequential_across_multiple_review_bounces(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """Implement attempt numbers increase monotonically across repeated review bounces."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn, project_id=project_id, title="T", priority=1, max_retries=10
        )
        gate_result = GateResult(
            passed=False, exit_code=1, stdout="", stderr="ISSUES",
            gate_name="post-review.sh", duration_seconds=1.0,
        )

        # --- Cycle 1: implement attempt=1 passes → review bounces ---
        db.update_task(conn, task_id, status="active", current_stage="review")
        db.insert_stage_run(
            conn, task_id=task_id, stage="implement", attempt=1, status="passed"
        )
        db.insert_stage_run(
            conn, task_id=task_id, stage="review", attempt=1, status="bounced"
        )

        task = dict(db.get_task(conn, task_id))
        await engine.bounce_task(conn, task, "review", gate_result)

        queued = db.list_stage_runs(
            conn, task_id=task_id, stage="implement", status="queued"
        )
        assert len(queued) == 1
        assert queued[0]["attempt"] == 2

        # --- Cycle 2: implement attempt=2 passes → review bounces again ---
        # Mark implement attempt=2 as passed
        conn.execute(
            "UPDATE stage_runs SET status = 'passed' WHERE task_id = ? AND stage = 'implement' AND status = 'queued'",
            (task_id,),
        )
        conn.commit()
        db.update_task(conn, task_id, current_stage="review")
        # advance_task creates review with attempt=1
        db.insert_stage_run(
            conn, task_id=task_id, stage="review", attempt=1, status="bounced"
        )

        task = dict(db.get_task(conn, task_id))
        await engine.bounce_task(conn, task, "review", gate_result)

        queued = db.list_stage_runs(
            conn, task_id=task_id, stage="implement", status="queued"
        )
        assert len(queued) == 1
        assert queued[0]["attempt"] == 3  # NOT 2 (the bug this fixes)

        # --- Cycle 3: implement attempt=3 passes → review bounces again ---
        conn.execute(
            "UPDATE stage_runs SET status = 'passed' WHERE task_id = ? AND stage = 'implement' AND status = 'queued'",
            (task_id,),
        )
        conn.commit()
        db.update_task(conn, task_id, current_stage="review")
        db.insert_stage_run(
            conn, task_id=task_id, stage="review", attempt=1, status="bounced"
        )

        task = dict(db.get_task(conn, task_id))
        await engine.bounce_task(conn, task, "review", gate_result)

        queued = db.list_stage_runs(
            conn, task_id=task_id, stage="implement", status="queued"
        )
        assert len(queued) == 1
        assert queued[0]["attempt"] == 4

    async def test_review_error_retry_sequential_across_cycles(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """Review error retry attempt numbers increase monotonically even with passed runs between errors."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn, project_id=project_id, title="T", priority=1, max_retries=10
        )
        db.update_task(conn, task_id, status="active", current_stage="review")

        # Review attempt=1 errors
        sr_id = db.insert_stage_run(
            conn, task_id=task_id, stage="review", attempt=1, status="error"
        )

        task = dict(db.get_task(conn, task_id))
        await engine._handle_error_retry(conn, task, "review", sr_id)

        queued = db.list_stage_runs(
            conn, task_id=task_id, stage="review", status="queued"
        )
        assert len(queued) == 1
        assert queued[0]["attempt"] == 2

        # Mark attempt=2 as passed (a non-error run between error cycles)
        conn.execute(
            "UPDATE stage_runs SET status = 'passed' WHERE task_id = ? AND stage = 'review' AND status = 'queued'",
            (task_id,),
        )
        conn.commit()

        # Review attempt=3 errors
        sr_id3 = db.insert_stage_run(
            conn, task_id=task_id, stage="review", attempt=3, status="error"
        )

        task = dict(db.get_task(conn, task_id))
        await engine._handle_error_retry(conn, task, "review", sr_id3)

        # With the old formula (get_retry_count + 1), this would produce attempt=3
        # (only 2 bounced/error runs counted) — a duplicate.
        # The new formula (get_stage_run_count + 1) correctly produces attempt=4.
        queued = db.list_stage_runs(
            conn, task_id=task_id, stage="review", status="queued"
        )
        assert len(queued) == 1
        assert queued[0]["attempt"] == 4


# ---------------------------------------------------------------------------
# Engine: follow-ups processing
# ---------------------------------------------------------------------------


class TestProcessFollowUps:
    """Tests for follow-up task creation after review passes (AC 10, 11, 12, 19)."""

    async def test_follow_ups_create_backlog_tasks_with_links(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
        tmp_path,
    ) -> None:
        """AC 10, 11, 12, 19: Follow-up JSON entries produce backlog tasks with created_by links."""

        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn, project_id=project_id, title="T", priority=1
        )

        # Create follow-ups JSON
        follow_ups_dir = tmp_path / "_forge" / "follow-ups"
        follow_ups_dir.mkdir(parents=True)
        follow_ups_file = follow_ups_dir / f"{task_id}.json"
        entries = [
            {"title": "Fix logging", "description": "Add proper logging to module X"},
            {"title": "Update docs", "description": "Docs are stale"},
        ]
        follow_ups_file.write_text(json.dumps(entries))

        project = dict(db.get_project(conn, project_id))
        project["repo_path"] = str(tmp_path)

        engine._process_follow_ups(conn, task_id, project)

        # Two new backlog tasks should exist
        backlog = db.list_tasks(conn, status="backlog")
        new_tasks = [t for t in backlog if t["title"] in ("Fix logging", "Update docs")]
        assert len(new_tasks) == 2

        # Each should be linked to the source task
        for new_task in new_tasks:
            links = db.get_task_links(conn, new_task["id"])
            assert len(links) == 1
            assert links[0]["link_type"] == "created_by"
            assert links[0]["target_task_id"] == task_id

        # File should be deleted
        assert not os.path.exists(str(follow_ups_file))

    async def test_no_follow_ups_file_completes_normally(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
        tmp_path,
    ) -> None:
        """AC 13 (no follow-ups): Task completes normally without follow-ups file."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn, project_id=project_id, title="T", priority=1
        )
        db.update_task(conn, task_id, status="active", current_stage="review")

        project = dict(db.get_project(conn, project_id))
        project["repo_path"] = str(tmp_path)

        # Should not raise
        engine._process_follow_ups(conn, task_id, project)

        # No new backlog tasks created
        backlog = db.list_tasks(conn, status="backlog")
        assert len(backlog) == 0


    async def test_follow_ups_string_entries_create_backlog_tasks(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
        tmp_path,
    ) -> None:
        """Plain string entries in follow-up JSON are ingested as backlog tasks."""

        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn, project_id=project_id, title="T", priority=1
        )

        follow_ups_dir = tmp_path / "_forge" / "follow-ups"
        follow_ups_dir.mkdir(parents=True)
        follow_ups_file = follow_ups_dir / f"{task_id}.json"
        entries = [
            "Fix timeout bug: handle_timeout needs project arg",
            "Update docs",
        ]
        follow_ups_file.write_text(json.dumps(entries))

        project = dict(db.get_project(conn, project_id))
        project["repo_path"] = str(tmp_path)

        engine._process_follow_ups(conn, task_id, project)

        backlog = db.list_tasks(conn, status="backlog")
        new_tasks = [t for t in backlog if t["title"] in ("Fix timeout bug", "Update docs")]
        assert len(new_tasks) == 2

        # Check title/description parsing
        by_title = {t["title"]: t for t in new_tasks}
        assert by_title["Fix timeout bug"]["description"] == "handle_timeout needs project arg"
        assert by_title["Update docs"]["description"] == ""

        # Each should be linked to the source task
        for new_task in new_tasks:
            links = db.get_task_links(conn, new_task["id"])
            assert len(links) == 1
            assert links[0]["link_type"] == "created_by"
            assert links[0]["target_task_id"] == task_id

        # File should be deleted
        assert not os.path.exists(str(follow_ups_file))

    async def test_follow_ups_mixed_entries(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
        tmp_path,
    ) -> None:
        """Arrays with both dict and string entries are fully processed."""

        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn, project_id=project_id, title="T", priority=1
        )

        follow_ups_dir = tmp_path / "_forge" / "follow-ups"
        follow_ups_dir.mkdir(parents=True)
        follow_ups_file = follow_ups_dir / f"{task_id}.json"
        entries = [
            {"title": "Dict entry", "description": "From dict"},
            "String entry: from string",
        ]
        follow_ups_file.write_text(json.dumps(entries))

        project = dict(db.get_project(conn, project_id))
        project["repo_path"] = str(tmp_path)

        engine._process_follow_ups(conn, task_id, project)

        backlog = db.list_tasks(conn, status="backlog")
        new_tasks = [t for t in backlog if t["title"] in ("Dict entry", "String entry")]
        assert len(new_tasks) == 2

        by_title = {t["title"]: t for t in new_tasks}
        assert by_title["Dict entry"]["description"] == "From dict"
        assert by_title["String entry"]["description"] == "from string"

        for new_task in new_tasks:
            links = db.get_task_links(conn, new_task["id"])
            assert len(links) == 1
            assert links[0]["link_type"] == "created_by"
            assert links[0]["target_task_id"] == task_id

        assert not os.path.exists(str(follow_ups_file))

    async def test_follow_ups_skips_invalid_entries(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
        tmp_path,
    ) -> None:
        """Invalid entries (null, numbers) are skipped; valid entries still processed."""

        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn, project_id=project_id, title="T", priority=1
        )

        follow_ups_dir = tmp_path / "_forge" / "follow-ups"
        follow_ups_dir.mkdir(parents=True)
        follow_ups_file = follow_ups_dir / f"{task_id}.json"
        entries = [None, 42, "Valid entry: should be created"]
        follow_ups_file.write_text(json.dumps(entries))

        project = dict(db.get_project(conn, project_id))
        project["repo_path"] = str(tmp_path)

        engine._process_follow_ups(conn, task_id, project)

        backlog = db.list_tasks(conn, status="backlog")
        new_tasks = [t for t in backlog if t["title"] == "Valid entry"]
        assert len(new_tasks) == 1
        assert new_tasks[0]["description"] == "should be created"

        links = db.get_task_links(conn, new_tasks[0]["id"])
        assert len(links) == 1
        assert links[0]["link_type"] == "created_by"
        assert links[0]["target_task_id"] == task_id

        assert not os.path.exists(str(follow_ups_file))

    async def test_process_follow_ups_passes_flow_field(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
        tmp_path,
    ) -> None:
        """Follow-up entries with flow: quick create quick-flow tasks."""

        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn, project_id=project_id, title="T", priority=1
        )

        follow_ups_dir = tmp_path / "_forge" / "follow-ups"
        follow_ups_dir.mkdir(parents=True)
        follow_ups_file = follow_ups_dir / f"{task_id}.json"
        entries = [
            {"title": "Quick fix", "description": "Simple fix", "flow": "quick"},
        ]
        follow_ups_file.write_text(json.dumps(entries))

        project = dict(db.get_project(conn, project_id))
        project["repo_path"] = str(tmp_path)

        engine._process_follow_ups(conn, task_id, project)

        backlog = db.list_tasks(conn, status="backlog")
        new_tasks = [t for t in backlog if t["title"] == "Quick fix"]
        assert len(new_tasks) == 1
        assert new_tasks[0]["flow"] == "quick"

    async def test_process_follow_ups_defaults_flow_to_quick(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
        tmp_path,
    ) -> None:
        """Follow-up entries without a flow field default to quick."""

        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn, project_id=project_id, title="T", priority=1
        )

        follow_ups_dir = tmp_path / "_forge" / "follow-ups"
        follow_ups_dir.mkdir(parents=True)
        follow_ups_file = follow_ups_dir / f"{task_id}.json"
        entries = [
            {"title": "No flow", "description": "Missing flow field"},
        ]
        follow_ups_file.write_text(json.dumps(entries))

        project = dict(db.get_project(conn, project_id))
        project["repo_path"] = str(tmp_path)

        engine._process_follow_ups(conn, task_id, project)

        backlog = db.list_tasks(conn, status="backlog")
        new_tasks = [t for t in backlog if t["title"] == "No flow"]
        assert len(new_tasks) == 1
        assert new_tasks[0]["flow"] == "quick"

    async def test_process_follow_ups_invalid_flow_falls_back_to_quick(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
        tmp_path,
    ) -> None:
        """Follow-up entries with invalid flow values fall back to quick."""

        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn, project_id=project_id, title="T", priority=1
        )

        follow_ups_dir = tmp_path / "_forge" / "follow-ups"
        follow_ups_dir.mkdir(parents=True)
        follow_ups_file = follow_ups_dir / f"{task_id}.json"
        entries = [
            {"title": "Bad flow", "description": "Invalid", "flow": "invalid_value"},
        ]
        follow_ups_file.write_text(json.dumps(entries))

        project = dict(db.get_project(conn, project_id))
        project["repo_path"] = str(tmp_path)

        engine._process_follow_ups(conn, task_id, project)

        backlog = db.list_tasks(conn, status="backlog")
        new_tasks = [t for t in backlog if t["title"] == "Bad flow"]
        assert len(new_tasks) == 1
        assert new_tasks[0]["flow"] == "quick"

    async def test_process_follow_ups_string_entry_uses_quick_flow(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
        tmp_path,
    ) -> None:
        """Plain string follow-up entries default to quick flow."""

        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn, project_id=project_id, title="T", priority=1
        )

        follow_ups_dir = tmp_path / "_forge" / "follow-ups"
        follow_ups_dir.mkdir(parents=True)
        follow_ups_file = follow_ups_dir / f"{task_id}.json"
        entries = ["String entry: description here"]
        follow_ups_file.write_text(json.dumps(entries))

        project = dict(db.get_project(conn, project_id))
        project["repo_path"] = str(tmp_path)

        engine._process_follow_ups(conn, task_id, project)

        backlog = db.list_tasks(conn, status="backlog")
        new_tasks = [t for t in backlog if t["title"] == "String entry"]
        assert len(new_tasks) == 1
        assert new_tasks[0]["flow"] == "quick"

    async def test_process_follow_ups_mixed_flows(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
        tmp_path,
    ) -> None:
        """Multiple follow-up entries with different flows are handled correctly."""

        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn, project_id=project_id, title="T", priority=1
        )

        follow_ups_dir = tmp_path / "_forge" / "follow-ups"
        follow_ups_dir.mkdir(parents=True)
        follow_ups_file = follow_ups_dir / f"{task_id}.json"
        entries = [
            {"title": "Quick one", "description": "Fast", "flow": "quick"},
            {"title": "Standard one", "description": "Normal", "flow": "standard"},
            {"title": "Default one", "description": "No flow field"},
        ]
        follow_ups_file.write_text(json.dumps(entries))

        project = dict(db.get_project(conn, project_id))
        project["repo_path"] = str(tmp_path)

        engine._process_follow_ups(conn, task_id, project)

        backlog = db.list_tasks(conn, status="backlog")
        by_title = {t["title"]: t for t in backlog}
        assert by_title["Quick one"]["flow"] == "quick"
        assert by_title["Standard one"]["flow"] == "standard"
        assert by_title["Default one"]["flow"] == "quick"


class TestFollowUpMaxRetries:
    """Follow-up tasks should inherit max_retries from configured default_max_retries."""

    async def test_follow_up_tasks_use_configured_max_retries(
        self,
        conn: sqlite3.Connection,
        project_id: str,
        tmp_path,
    ) -> None:
        """Follow-up tasks created by _process_follow_ups use the configured default_max_retries."""
        custom_retries = 8
        custom_settings = Settings(engine=EngineSettings(default_max_retries=custom_retries))
        engine = PipelineEngine(custom_settings, ":memory:")

        task_id = db.insert_task(conn, project_id=project_id, title="T", priority=1)

        follow_ups_dir = tmp_path / "_forge" / "follow-ups"
        follow_ups_dir.mkdir(parents=True)
        follow_ups_file = follow_ups_dir / f"{task_id}.json"
        entries = [{"title": "Follow-up task", "description": "Do something"}]
        follow_ups_file.write_text(json.dumps(entries))

        project = dict(db.get_project(conn, project_id))
        project["repo_path"] = str(tmp_path)

        engine._process_follow_ups(conn, task_id, project)

        backlog = db.list_tasks(conn, status="backlog")
        new_tasks = [t for t in backlog if t["title"] == "Follow-up task"]
        assert len(new_tasks) == 1
        assert new_tasks[0]["max_retries"] == custom_retries

    async def test_follow_up_tasks_default_max_retries_without_config(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
        tmp_path,
    ) -> None:
        """Without custom config, follow-up tasks get the built-in default (3)."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(conn, project_id=project_id, title="T", priority=1)

        follow_ups_dir = tmp_path / "_forge" / "follow-ups"
        follow_ups_dir.mkdir(parents=True)
        follow_ups_file = follow_ups_dir / f"{task_id}.json"
        entries = [{"title": "Default retries task", "description": "Check default"}]
        follow_ups_file.write_text(json.dumps(entries))

        project = dict(db.get_project(conn, project_id))
        project["repo_path"] = str(tmp_path)

        engine._process_follow_ups(conn, task_id, project)

        backlog = db.list_tasks(conn, status="backlog")
        new_tasks = [t for t in backlog if t["title"] == "Default retries task"]
        assert len(new_tasks) == 1
        assert new_tasks[0]["max_retries"] == 3


class TestEpicDecompositionMaxRetries:
    """Epic child tasks should inherit max_retries from configured default_max_retries."""

    async def test_epic_children_use_configured_max_retries(
        self,
        conn: sqlite3.Connection,
        project_id: str,
        tmp_path,
    ) -> None:
        """Child tasks from epic decomposition use the configured default_max_retries."""
        custom_retries = 10
        custom_settings = Settings(engine=EngineSettings(default_max_retries=custom_retries))

        db.update_project(conn, project_id, repo_path=str(tmp_path))
        project = dict(db.get_project(conn, project_id))

        task_id = db.insert_task(
            conn,
            project_id=project_id,
            title="Epic",
            flow="epic",
            epic_status="pending",
        )
        db.update_task(conn, task_id, status="active", current_stage="spec")

        decomp_dir = os.path.join(str(tmp_path), "_forge/epic-decompositions")
        os.makedirs(decomp_dir, exist_ok=True)
        with open(os.path.join(decomp_dir, f"{task_id}.json"), "w") as f:
            json.dump([{"title": "Child task", "description": "A child"}], f)

        engine = PipelineEngine(custom_settings, ":memory:")
        await engine.advance_task(conn, task_id, "spec", project=project)

        children = db.get_child_tasks(conn, task_id)
        assert len(children) == 1
        assert children[0]["max_retries"] == custom_retries

    async def test_epic_children_default_max_retries_without_config(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
        tmp_path,
    ) -> None:
        """Without custom config, epic children get the built-in default (3)."""
        db.update_project(conn, project_id, repo_path=str(tmp_path))
        project = dict(db.get_project(conn, project_id))

        task_id = db.insert_task(
            conn,
            project_id=project_id,
            title="Epic",
            flow="epic",
            epic_status="pending",
        )
        db.update_task(conn, task_id, status="active", current_stage="spec")

        decomp_dir = os.path.join(str(tmp_path), "_forge/epic-decompositions")
        os.makedirs(decomp_dir, exist_ok=True)
        with open(os.path.join(decomp_dir, f"{task_id}.json"), "w") as f:
            json.dump([{"title": "Default child", "description": "Check default"}], f)

        engine = PipelineEngine(settings, ":memory:")
        await engine.advance_task(conn, task_id, "spec", project=project)

        children = db.get_child_tasks(conn, task_id)
        assert len(children) == 1
        assert children[0]["max_retries"] == 3


class TestTaskPriority:
    def test_highest_priority_picked(
        self,
        conn: sqlite3.Connection,
        project_id: str,
    ) -> None:
        """Verify get_next_queued_task returns highest-priority task."""
        t1 = db.insert_task(
            conn,
            project_id=project_id,
            title="Low",
            priority=1,
        )
        db.update_task(conn, t1, status="active", current_stage="spec")
        db.insert_stage_run(conn, task_id=t1, stage="spec", attempt=1, status="queued")

        t2 = db.insert_task(
            conn,
            project_id=project_id,
            title="High",
            priority=10,
        )
        db.update_task(conn, t2, status="active", current_stage="spec")
        db.insert_stage_run(conn, task_id=t2, stage="spec", attempt=1, status="queued")

        picked = db.get_next_queued_task(conn)
        assert picked is not None
        assert picked["id"] == t2


# ---------------------------------------------------------------------------
# Engine: GitResult error context in stage_runs and run_log
# ---------------------------------------------------------------------------


class TestGitResultErrorContext:
    async def test_create_branch_failure_includes_stderr_in_error_message(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """AC #4, #9, #11: create_branch failure puts stderr into stage_runs.error_message."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(conn, project_id=project_id, title="T", priority=1)
        db.update_task(conn, task_id, status="active", current_stage="spec")
        sr_id = db.insert_stage_run(
            conn, task_id=task_id, stage="spec", attempt=1, status="queued"
        )

        git_fail = GitResult(
            success=False, stdout="", stderr="fatal: bad ref", returncode=128
        )

        safe_conn = _UnclosableConnection(conn)
        with (
            patch("forge.engine.database.get_connection", return_value=safe_conn),
            patch(
                "forge.engine.create_branch",
                new_callable=AsyncMock,
                return_value=git_fail,
            ),
        ):
            engine.running = True
            loop_task = asyncio.create_task(engine.run_loop())
            await asyncio.sleep(0.5)
            engine.running = False
            try:
                await asyncio.wait_for(loop_task, timeout=3.0)
            except asyncio.TimeoutError:
                loop_task.cancel()

        sr = db.get_stage_run(conn, sr_id)
        assert sr["status"] == "error"
        assert "fatal: bad ref" in sr["error_message"]
        assert sr["error_message"].startswith("Failed to create branch")

    async def test_rebase_failure_includes_stderr_and_metadata(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """AC #4, #5, #8, #11: rebase failure includes stderr in error_message and metadata in run_log."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(conn, project_id=project_id, title="T", priority=1)
        db.update_task(
            conn,
            task_id,
            status="active",
            current_stage="implement",
            branch_name="forge/test-branch",
        )
        sr_id = db.insert_stage_run(
            conn, task_id=task_id, stage="implement", attempt=1, status="queued"
        )

        rebase_fail = GitResult(
            success=False, stdout="", stderr="CONFLICT in README.md", returncode=1
        )

        safe_conn = _UnclosableConnection(conn)
        with (
            patch("forge.engine.database.get_connection", return_value=safe_conn),
            patch(
                "forge.engine.rebase_branch",
                new_callable=AsyncMock,
                return_value=rebase_fail,
            ),
        ):
            engine.running = True
            loop_task = asyncio.create_task(engine.run_loop())
            await asyncio.sleep(0.5)
            engine.running = False
            try:
                await asyncio.wait_for(loop_task, timeout=3.0)
            except asyncio.TimeoutError:
                loop_task.cancel()

        sr = db.get_stage_run(conn, sr_id)
        assert sr["status"] == "error"
        assert "CONFLICT in README.md" in sr["error_message"]
        assert sr["error_message"].startswith("Rebase failed")

        # Check run_log has metadata with git details

        logs = db.get_logs(conn, task_id=task_id)
        meta_logs = [
            row for row in logs if row["metadata"] is not None
        ]
        assert len(meta_logs) > 0
        meta = json.loads(meta_logs[0]["metadata"])
        assert meta["git_stderr"] == "CONFLICT in README.md"
        assert meta["git_returncode"] == 1

    async def test_log_helper_passes_metadata(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
    ) -> None:
        """AC #6: _log passes metadata through to database.insert_log."""

        engine = PipelineEngine(settings, ":memory:")
        safe_conn = _UnclosableConnection(conn)
        with patch("forge.engine.database.get_connection", return_value=safe_conn):
            engine._log("info", "test message", metadata={"key": "val"})

        logs = db.get_logs(conn)
        assert len(logs) >= 1
        meta_log = [row for row in logs if row["message"] == "test message"][0]
        meta = json.loads(meta_log["metadata"])
        assert meta["key"] == "val"

    async def test_error_message_truncated_to_4kb(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """AC #4: stderr in error_message is truncated to at most 4 KB."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(conn, project_id=project_id, title="T", priority=1)
        db.update_task(
            conn,
            task_id,
            status="active",
            current_stage="implement",
            branch_name="forge/test-branch",
        )
        sr_id = db.insert_stage_run(
            conn, task_id=task_id, stage="implement", attempt=1, status="queued"
        )

        big_stderr = "X" * 10000  # 10 KB
        rebase_fail = GitResult(
            success=False, stdout="", stderr=big_stderr, returncode=1
        )

        safe_conn = _UnclosableConnection(conn)
        with (
            patch("forge.engine.database.get_connection", return_value=safe_conn),
            patch(
                "forge.engine.rebase_branch",
                new_callable=AsyncMock,
                return_value=rebase_fail,
            ),
        ):
            engine.running = True
            loop_task = asyncio.create_task(engine.run_loop())
            await asyncio.sleep(0.5)
            engine.running = False
            try:
                await asyncio.wait_for(loop_task, timeout=3.0)
            except asyncio.TimeoutError:
                loop_task.cancel()

        sr = db.get_stage_run(conn, sr_id)
        # The error_message includes the description prefix + truncated stderr
        # The stderr portion should be at most 4096 chars
        assert len(sr["error_message"]) <= 4096 + 200  # prefix + truncated stderr


# ---------------------------------------------------------------------------
# Review error retry with shared budget
# ---------------------------------------------------------------------------


class TestReviewErrorSharedBudget:
    """Tests for review-stage error retries using the shared implement→review budget."""

    async def test_review_error_uses_shared_budget(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """AC 1, 2: Review error checks shared budget; exhaustion → needs_human."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn, project_id=project_id, title="T", priority=1, max_retries=2
        )
        db.update_task(conn, task_id, status="active", current_stage="review")
        # 1 bounced implement + 1 bounced review = shared count 2, meets max_retries=2
        db.insert_stage_run(
            conn, task_id=task_id, stage="implement", attempt=1, status="bounced"
        )
        sr_id = db.insert_stage_run(
            conn, task_id=task_id, stage="review", attempt=1, status="bounced"
        )

        task = dict(db.get_task(conn, task_id))
        await engine._handle_error_retry(conn, task, "review", sr_id)

        task = db.get_task(conn, task_id)
        assert task["status"] == "needs_human"

    async def test_review_error_retries_review_not_implement(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """AC 3: Review errors retry review stage, not implement."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn, project_id=project_id, title="T", priority=1, max_retries=5
        )
        db.update_task(conn, task_id, status="active", current_stage="review")
        sr_id = db.insert_stage_run(
            conn, task_id=task_id, stage="review", attempt=1, status="error"
        )

        task = dict(db.get_task(conn, task_id))
        await engine._handle_error_retry(conn, task, "review", sr_id)

        queued_review = db.list_stage_runs(
            conn, task_id=task_id, stage="review", status="queued"
        )
        assert len(queued_review) == 1

        queued_implement = db.list_stage_runs(
            conn, task_id=task_id, stage="implement", status="queued"
        )
        assert len(queued_implement) == 0

    async def test_review_error_retry_attempt_sequential(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """AC 4: Attempt numbering is sequential for review error retries."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn, project_id=project_id, title="T", priority=1, max_retries=5
        )
        db.update_task(conn, task_id, status="active", current_stage="review")
        sr_id = db.insert_stage_run(
            conn, task_id=task_id, stage="review", attempt=1, status="error"
        )

        task = dict(db.get_task(conn, task_id))
        await engine._handle_error_retry(conn, task, "review", sr_id)

        queued = db.list_stage_runs(
            conn, task_id=task_id, stage="review", status="queued"
        )
        assert len(queued) == 1
        assert queued[0]["attempt"] == 2

    async def test_implement_error_retry_unchanged(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """AC 5: Non-review error retry path is unchanged (uses per-stage budget)."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn, project_id=project_id, title="T", priority=1, max_retries=5
        )
        db.update_task(conn, task_id, status="active", current_stage="implement")
        sr_id = db.insert_stage_run(
            conn, task_id=task_id, stage="implement", attempt=1, status="error"
        )

        task = dict(db.get_task(conn, task_id))
        await engine._handle_error_retry(conn, task, "implement", sr_id)

        queued = db.list_stage_runs(
            conn, task_id=task_id, stage="implement", status="queued"
        )
        assert len(queued) == 1
        assert queued[0]["attempt"] == 2

    async def test_bounce_task_shared_budget_counts_errors(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """AC 6: bounce_task shared budget now counts errors too."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn, project_id=project_id, title="T", priority=1, max_retries=2
        )
        db.update_task(conn, task_id, status="active", current_stage="review")
        # 1 error implement + 1 error review = shared count 2, meets max_retries=2
        db.insert_stage_run(
            conn, task_id=task_id, stage="implement", attempt=1, status="error"
        )
        db.insert_stage_run(
            conn, task_id=task_id, stage="review", attempt=1, status="error"
        )

        task = dict(db.get_task(conn, task_id))
        gate_result = GateResult(
            passed=False, exit_code=1, stdout="", stderr="ISSUES",
            gate_name="post-review.sh", duration_seconds=1.0,
        )

        await engine.bounce_task(conn, task, "review", gate_result)

        task = db.get_task(conn, task_id)
        assert task["status"] == "needs_human"

    @pytest.fixture
    def pause_project_id(self, conn: sqlite3.Connection) -> str:
        return db.insert_project(
            conn,
            name="PauseProject",
            repo_path="/tmp/repo",
            gate_dir="/tmp/repo/gates",
            pause_after_completion=True,
        )

    async def test_review_error_auto_pause(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        pause_project_id: str,
    ) -> None:
        """AC 2: Auto-pause works for review error exhaustion."""
        engine = PipelineEngine(settings, ":memory:")
        engine.running = True
        task_id = db.insert_task(
            conn, project_id=pause_project_id, title="T", priority=1, max_retries=1
        )
        db.update_task(conn, task_id, status="active", current_stage="review")
        sr_id = db.insert_stage_run(
            conn, task_id=task_id, stage="review", attempt=1, status="error"
        )
        # 1 error review run meets max_retries=1

        task = dict(db.get_task(conn, task_id))
        project = dict(db.get_project(conn, pause_project_id))
        await engine._handle_error_retry(conn, task, "review", sr_id, project=project)

        task = db.get_task(conn, task_id)
        assert task["status"] == "needs_human"
        assert engine.running is False


# ---------------------------------------------------------------------------
# Engine: guard _handle_error_retry after _reset_and_log failure
# ---------------------------------------------------------------------------


class TestGuardRetryAfterResetFailure:
    """Verify _handle_error_retry is skipped when _reset_and_log returns False."""

    async def test_dispatch_error_skips_retry_when_reset_fails(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        active_task_with_queued_run: tuple[str, str],
    ) -> None:
        """When reset fails after dispatch error, no retry is queued."""
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
                engine.running = False
            return dispatch_result

        reset_call_count = 0

        async def _reset_side_effect(repo_path, default_branch):
            nonlocal reset_call_count
            reset_call_count += 1
            # First call is the pre-dispatch safety check — must succeed
            if reset_call_count == 1:
                return {"success": True, "output": "ok"}
            # Second call is the post-error reset — fail
            return {"success": False, "output": "reset failed"}

        with (
            patch("forge.engine.database.get_connection", return_value=safe_conn),
            patch("forge.engine.dispatch_claude", side_effect=dispatch_side_effect),
            patch("forge.engine.build_prompt", return_value="test prompt"),
            patch("forge.engine.reset_repo_state", side_effect=_reset_side_effect),
        ):
            engine.running = True
            loop_task = asyncio.create_task(engine.run_loop())
            try:
                await asyncio.wait_for(loop_task, timeout=3.0)
            except asyncio.TimeoutError:
                loop_task.cancel()

        sr = db.get_stage_run(conn, sr_id)
        assert sr["status"] == "error"

        # No retry should be queued since reset failed
        queued = db.list_stage_runs(
            conn, task_id=task_id, stage="spec", status="queued"
        )
        assert len(queued) == 0

        task = db.get_task(conn, task_id)
        assert task["status"] == "needs_human"

    async def test_dispatch_error_retries_when_reset_succeeds(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        active_task_with_queued_run: tuple[str, str],
    ) -> None:
        """When reset succeeds after dispatch error, retry is queued."""
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
                engine.running = False
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

        sr = db.get_stage_run(conn, sr_id)
        assert sr["status"] == "error"

        # Retry should be queued since reset succeeded
        queued = db.list_stage_runs(
            conn, task_id=task_id, stage="spec", status="queued"
        )
        assert len(queued) == 1

        task = db.get_task(conn, task_id)
        assert task["status"] == "active"

    async def test_handle_timeout_skips_retry_when_reset_fails(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """When reset fails after timeout, no retry is queued."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn, project_id=project_id, title="T", priority=1, max_retries=3
        )
        db.update_task(conn, task_id, status="active", current_stage="spec")

        old_time = (datetime.now(timezone.utc) - timedelta(seconds=9999)).isoformat()
        sr_id = db.insert_stage_run(
            conn, task_id=task_id, stage="spec", attempt=1, status="running"
        )
        db.update_stage_run(conn, sr_id, started_at=old_time)

        async def _failing_reset(repo_path, default_branch):
            return {"success": False, "output": "reset failed"}

        with patch("forge.engine.reset_repo_state", side_effect=_failing_reset):
            await engine._check_timeouts(conn)

        sr = db.get_stage_run(conn, sr_id)
        assert sr["status"] == "error"
        assert "timed out" in sr["error_message"]

        # No retry queued
        queued = db.list_stage_runs(
            conn, task_id=task_id, stage="spec", status="queued"
        )
        assert len(queued) == 0

        task = db.get_task(conn, task_id)
        assert task["status"] == "needs_human"

    async def test_handle_timeout_retries_when_reset_succeeds(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """When reset succeeds after timeout, retry is queued."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn, project_id=project_id, title="T", priority=1, max_retries=3
        )
        db.update_task(conn, task_id, status="active", current_stage="spec")

        old_time = (datetime.now(timezone.utc) - timedelta(seconds=9999)).isoformat()
        sr_id = db.insert_stage_run(
            conn, task_id=task_id, stage="spec", attempt=1, status="running"
        )
        db.update_stage_run(conn, sr_id, started_at=old_time)

        await engine._check_timeouts(conn)

        sr = db.get_stage_run(conn, sr_id)
        assert sr["status"] == "error"
        assert "timed out" in sr["error_message"]

        # Retry should be queued
        queued = db.list_stage_runs(
            conn, task_id=task_id, stage="spec", status="queued"
        )
        assert len(queued) == 1

        task = db.get_task(conn, task_id)
        assert task["status"] == "active"

    async def test_handle_timeout_retries_when_no_project(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """When project doesn't exist, retry still proceeds (no reset needed)."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn, project_id=project_id, title="T", priority=1, max_retries=3
        )
        db.update_task(conn, task_id, status="active", current_stage="spec")

        sr_id = db.insert_stage_run(
            conn, task_id=task_id, stage="spec", attempt=1, status="running"
        )

        stage_run = db.get_stage_run(conn, sr_id)

        with patch("forge.engine.database.get_project", return_value=None):
            await engine.handle_timeout(conn, stage_run)

        sr = db.get_stage_run(conn, sr_id)
        assert sr["status"] == "error"

        # Retry should still be queued since no reset was attempted
        queued = db.list_stage_runs(
            conn, task_id=task_id, stage="spec", status="queued"
        )
        assert len(queued) == 1

    async def test_handle_timeout_passes_project_to_handle_error_retry(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
    ) -> None:
        """When a timeout exhausts retries and pause_after_completion is enabled, auto-pause fires."""
        pause_project_id = db.insert_project(
            conn,
            name="PauseProject",
            repo_path="/tmp/repo",
            gate_dir="/tmp/repo/gates",
            pause_after_completion=True,
        )
        engine = PipelineEngine(settings, ":memory:")
        engine.running = True
        task_id = db.insert_task(
            conn, project_id=pause_project_id, title="T", priority=1, max_retries=1
        )
        db.update_task(conn, task_id, status="active", current_stage="spec")

        # One existing error run so retry count meets max_retries=1
        db.insert_stage_run(
            conn, task_id=task_id, stage="spec", attempt=1, status="error"
        )

        # Create a running stage run that will time out
        old_time = (datetime.now(timezone.utc) - timedelta(seconds=9999)).isoformat()
        sr_id2 = db.insert_stage_run(
            conn, task_id=task_id, stage="spec", attempt=2, status="running"
        )
        db.update_stage_run(conn, sr_id2, started_at=old_time)

        await engine._check_timeouts(conn)

        sr = db.get_stage_run(conn, sr_id2)
        assert sr["status"] == "error"

        task = db.get_task(conn, task_id)
        assert task["status"] == "needs_human"
        # The key assertion: _maybe_auto_pause was reached because project was passed
        assert engine.running is False

    async def test_handle_timeout_passes_none_project_when_project_missing(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """When the project row is missing, project=None is safely passed without error."""
        engine = PipelineEngine(settings, ":memory:")
        engine.running = True
        task_id = db.insert_task(
            conn, project_id=project_id, title="T", priority=1, max_retries=1
        )
        db.update_task(conn, task_id, status="active", current_stage="spec")

        # One existing error run so retry count meets max_retries=1
        db.insert_stage_run(
            conn, task_id=task_id, stage="spec", attempt=1, status="error"
        )

        sr_id = db.insert_stage_run(
            conn, task_id=task_id, stage="spec", attempt=2, status="running"
        )

        stage_run = db.get_stage_run(conn, sr_id)

        with patch("forge.engine.database.get_project", return_value=None):
            await engine.handle_timeout(conn, stage_run)

        sr = db.get_stage_run(conn, sr_id)
        assert sr["status"] == "error"

        task = db.get_task(conn, task_id)
        assert task["status"] == "needs_human"
        # Engine should NOT be paused since project was None (no auto-pause possible)
        assert engine.running is True


# ---------------------------------------------------------------------------
# Auto-escalation from quick flow to standard flow
# ---------------------------------------------------------------------------


class TestAutoEscalation:
    async def test_bounce_task_escalates_quick_to_standard(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """Quick-flow task auto-escalates when retries exhausted via bounce."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn,
            project_id=project_id,
            title="Quick task",
            priority=1,
            max_retries=1,
            flow="quick",
        )
        db.update_task(conn, task_id, status="active", current_stage="implement")
        # Exhaust the implement→review budget
        db.insert_stage_run(
            conn, task_id=task_id, stage="implement", attempt=1, status="bounced"
        )

        task = dict(db.get_task(conn, task_id))
        gate_result = GateResult(
            passed=False, exit_code=1, stdout="", stderr="fail",
            gate_name="post-review.sh", duration_seconds=1.0,
        )

        await engine.bounce_task(conn, task, "review", gate_result)

        task = db.get_task(conn, task_id)
        assert task["flow"] == "standard"
        assert task["current_stage"] == "spec"
        assert task["escalated_from_quick"] == 1
        assert task["status"] == "active"

        # A queued spec stage_run should exist
        queued = db.list_stage_runs(conn, task_id=task_id, stage="spec", status="queued")
        assert len(queued) == 1

    async def test_bounce_task_escalated_task_goes_to_needs_human(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """Already-escalated task goes to needs_human (no second escalation)."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn,
            project_id=project_id,
            title="Already escalated",
            priority=1,
            max_retries=1,
            flow="standard",
        )
        db.update_task(
            conn, task_id,
            status="active", current_stage="review", escalated_from_quick=1,
        )
        db.insert_stage_run(
            conn, task_id=task_id, stage="implement", attempt=1, status="bounced"
        )

        task = dict(db.get_task(conn, task_id))
        gate_result = GateResult(
            passed=False, exit_code=1, stdout="", stderr="fail",
            gate_name="post-review.sh", duration_seconds=1.0,
        )

        await engine.bounce_task(conn, task, "review", gate_result)

        task = db.get_task(conn, task_id)
        assert task["status"] == "needs_human"

    async def test_error_retry_escalates_quick_to_standard(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """Quick-flow task escalates via _handle_error_retry when retries exhausted."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn,
            project_id=project_id,
            title="Quick error",
            priority=1,
            max_retries=1,
            flow="quick",
        )
        db.update_task(conn, task_id, status="active", current_stage="implement")
        sr_id = db.insert_stage_run(
            conn, task_id=task_id, stage="implement", attempt=1, status="error"
        )

        task = dict(db.get_task(conn, task_id))
        await engine._handle_error_retry(conn, task, "implement", sr_id)

        task = db.get_task(conn, task_id)
        assert task["flow"] == "standard"
        assert task["current_stage"] == "spec"
        assert task["escalated_from_quick"] == 1

        queued = db.list_stage_runs(conn, task_id=task_id, stage="spec", status="queued")
        assert len(queued) == 1

    async def test_error_retry_escalated_task_goes_to_needs_human(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """Already-escalated task goes to needs_human via error retry."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn,
            project_id=project_id,
            title="Already escalated error",
            priority=1,
            max_retries=1,
            flow="standard",
        )
        db.update_task(
            conn, task_id,
            status="active", current_stage="implement", escalated_from_quick=1,
        )
        sr_id = db.insert_stage_run(
            conn, task_id=task_id, stage="implement", attempt=1, status="error"
        )

        task = dict(db.get_task(conn, task_id))
        await engine._handle_error_retry(conn, task, "implement", sr_id)

        task = db.get_task(conn, task_id)
        assert task["status"] == "needs_human"

    async def test_bounce_task_implement_gate_escalates_quick_to_standard(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """Quick-flow task escalates when implement gate bounce exhausts retries."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn,
            project_id=project_id,
            title="Quick implement bounce",
            priority=1,
            max_retries=1,
            flow="quick",
        )
        db.update_task(conn, task_id, status="active", current_stage="implement")
        # Exhaust the implement retry budget
        db.insert_stage_run(
            conn, task_id=task_id, stage="implement", attempt=1, status="bounced"
        )

        task = dict(db.get_task(conn, task_id))
        gate_result = GateResult(
            passed=False, exit_code=1, stdout="", stderr="fail",
            gate_name="post-implement.sh", duration_seconds=1.0,
        )

        await engine.bounce_task(conn, task, "implement", gate_result)

        task = db.get_task(conn, task_id)
        assert task["flow"] == "standard"
        assert task["current_stage"] == "spec"
        assert task["escalated_from_quick"] == 1
        assert task["status"] == "active"

        queued = db.list_stage_runs(conn, task_id=task_id, stage="spec", status="queued")
        assert len(queued) == 1

    async def test_error_retry_review_escalates_quick_to_standard(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """Quick-flow task escalates via _handle_error_retry at review stage."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn,
            project_id=project_id,
            title="Quick review error",
            priority=1,
            max_retries=1,
            flow="quick",
        )
        db.update_task(conn, task_id, status="active", current_stage="review")
        # Exhaust the shared implement→review budget
        db.insert_stage_run(
            conn, task_id=task_id, stage="implement", attempt=1, status="error"
        )
        sr_id = db.insert_stage_run(
            conn, task_id=task_id, stage="review", attempt=1, status="error"
        )

        task = dict(db.get_task(conn, task_id))
        await engine._handle_error_retry(conn, task, "review", sr_id)

        task = db.get_task(conn, task_id)
        assert task["flow"] == "standard"
        assert task["current_stage"] == "spec"
        assert task["escalated_from_quick"] == 1

        queued = db.list_stage_runs(conn, task_id=task_id, stage="spec", status="queued")
        assert len(queued) == 1

    async def test_standard_flow_not_affected_by_escalation(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """Standard-flow tasks go to needs_human, never trigger escalation."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn,
            project_id=project_id,
            title="Standard task",
            priority=1,
            max_retries=1,
            flow="standard",
        )
        db.update_task(conn, task_id, status="active", current_stage="implement")
        db.insert_stage_run(
            conn, task_id=task_id, stage="implement", attempt=1, status="bounced"
        )

        task = dict(db.get_task(conn, task_id))
        gate_result = GateResult(
            passed=False, exit_code=1, stdout="", stderr="fail",
            gate_name="post-implement.sh", duration_seconds=1.0,
        )

        await engine.bounce_task(conn, task, "implement", gate_result)

        task = db.get_task(conn, task_id)
        assert task["status"] == "needs_human"
        assert task["escalated_from_quick"] == 0

    async def test_escalation_preserves_existing_stage_runs(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """Old stage_runs are preserved after escalation for audit trail."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn,
            project_id=project_id,
            title="Preserve runs",
            priority=1,
            max_retries=1,
            flow="quick",
        )
        db.update_task(conn, task_id, status="active", current_stage="implement")
        old_sr = db.insert_stage_run(
            conn, task_id=task_id, stage="implement", attempt=1, status="bounced"
        )

        task = dict(db.get_task(conn, task_id))
        gate_result = GateResult(
            passed=False, exit_code=1, stdout="", stderr="fail",
            gate_name="post-review.sh", duration_seconds=1.0,
        )

        await engine.bounce_task(conn, task, "review", gate_result)

        # Old stage_run should still exist
        old_run = db.get_stage_run(conn, old_sr)
        assert old_run is not None
        assert old_run["status"] == "bounced"

        # New spec run added
        all_runs = db.list_stage_runs(conn, task_id=task_id)
        stages = [r["stage"] for r in all_runs]
        assert "implement" in stages
        assert "spec" in stages

    async def test_escalation_logs_event(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """Escalation event is logged to run_log."""
        engine = PipelineEngine(settings, ":memory:")
        safe_conn = _UnclosableConnection(conn)
        with patch("forge.engine.database.get_connection", return_value=safe_conn):
            task_id = db.insert_task(
                conn,
                project_id=project_id,
                title="Log test",
                priority=1,
                max_retries=1,
                flow="quick",
            )
            db.update_task(conn, task_id, status="active", current_stage="implement")
            db.insert_stage_run(
                conn, task_id=task_id, stage="implement", attempt=1, status="bounced"
            )

            task = dict(db.get_task(conn, task_id))
            gate_result = GateResult(
                passed=False, exit_code=1, stdout="", stderr="fail",
                gate_name="post-review.sh", duration_seconds=1.0,
            )

            await engine.bounce_task(conn, task, "review", gate_result)

        # Check run_log for escalation message
        logs = db.get_logs(conn, task_id=task_id)
        escalation_logs = [
            log for log in logs
            if "escalat" in log["message"].lower() and log["level"] == "info"
        ]
        assert len(escalation_logs) >= 1
        assert task_id in escalation_logs[0]["message"]


# ---------------------------------------------------------------------------
# Epic decomposition tests
# ---------------------------------------------------------------------------


class TestEpicDecomposition:
    async def test_advance_task_epic_spec_creates_children(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
        tmp_path,
    ) -> None:
        """AC 3, 4: After epic spec passes, engine creates child tasks."""
        # Update project repo_path to tmp_path so we can write the decomposition file
        db.update_project(conn, project_id, repo_path=str(tmp_path))
        project = dict(db.get_project(conn, project_id))

        task_id = db.insert_task(
            conn,
            project_id=project_id,
            title="Big epic",
            flow="epic",
            epic_status="pending",
        )
        db.update_task(conn, task_id, status="active", current_stage="spec")

        # Write decomposition JSON
        decomp_dir = os.path.join(str(tmp_path), "_forge/epic-decompositions")
        os.makedirs(decomp_dir, exist_ok=True)
        decomp = [
            {"title": "Child A", "description": "Do A", "flow": "standard", "priority": 2},
            {"title": "Child B", "description": "Do B", "flow": "quick", "priority": 1},
            {"title": "Child C", "description": "Do C"},
        ]
        with open(os.path.join(decomp_dir, f"{task_id}.json"), "w") as f:
            json.dump(decomp, f)

        engine = PipelineEngine(settings, ":memory:")
        await engine.advance_task(conn, task_id, "spec", project=project)

        # Check children were created
        children = db.get_child_tasks(conn, task_id)
        assert len(children) == 3

        # Check child properties
        child_a = [c for c in children if c["title"] == "Child A"][0]
        assert child_a["parent_task_id"] == task_id
        assert child_a["flow"] == "standard"
        assert child_a["priority"] == 2
        assert child_a["status"] == "backlog"

        child_b = [c for c in children if c["title"] == "Child B"][0]
        assert child_b["flow"] == "quick"
        assert child_b["priority"] == 1

        # Default flow is standard
        child_c = [c for c in children if c["title"] == "Child C"][0]
        assert child_c["flow"] == "standard"
        assert child_c["priority"] == 0

        # Check created_by links exist for each child
        for child in children:
            links = db.get_task_links(conn, child["id"])
            created_by = [lk for lk in links if lk["link_type"] == "created_by"]
            assert len(created_by) == 1
            assert created_by[0]["target_task_id"] == task_id

    async def test_advance_task_epic_status_transitions(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
        tmp_path,
    ) -> None:
        """AC 5: Epic transitions to decomposed/paused after child creation."""
        db.update_project(conn, project_id, repo_path=str(tmp_path))
        project = dict(db.get_project(conn, project_id))

        task_id = db.insert_task(
            conn,
            project_id=project_id,
            title="Epic task",
            flow="epic",
            epic_status="pending",
        )
        db.update_task(conn, task_id, status="active", current_stage="spec")

        decomp_dir = os.path.join(str(tmp_path), "_forge/epic-decompositions")
        os.makedirs(decomp_dir, exist_ok=True)
        with open(os.path.join(decomp_dir, f"{task_id}.json"), "w") as f:
            json.dump([{"title": "Sub task"}], f)

        engine = PipelineEngine(settings, ":memory:")
        await engine.advance_task(conn, task_id, "spec", project=project)

        task = db.get_task(conn, task_id)
        assert task["epic_status"] == "decomposed"
        assert task["status"] == "paused"

    async def test_advance_task_epic_transitions_to_reviewing_when_children_done(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """AC 7: When all children are done, epic transitions to reviewing."""
        # Create epic task
        epic_id = db.insert_task(
            conn,
            project_id=project_id,
            title="Epic parent",
            flow="epic",
            epic_status="decomposed",
        )
        db.update_task(conn, epic_id, status="paused")

        # Create two child tasks (quick flow so they finish at review)
        child1_id = db.insert_task(
            conn,
            project_id=project_id,
            title="Child 1",
            flow="quick",
            parent_task_id=epic_id,
        )
        child2_id = db.insert_task(
            conn,
            project_id=project_id,
            title="Child 2",
            flow="quick",
            parent_task_id=epic_id,
        )

        engine = PipelineEngine(settings, ":memory:")

        # Mark child 1 done via advance_task at end of its pipeline
        db.update_task(conn, child1_id, status="active", current_stage="review")
        await engine.advance_task(conn, child1_id, "review")

        # Epic should NOT be in review yet
        epic = db.get_task(conn, epic_id)
        assert epic["status"] == "paused"
        assert epic["epic_status"] == "decomposed"

        # Mark child 2 done
        db.update_task(conn, child2_id, status="active", current_stage="review")
        await engine.advance_task(conn, child2_id, "review")

        # Now epic should be in reviewing status with a queued review stage_run
        epic = db.get_task(conn, epic_id)
        assert epic["status"] == "active"
        assert epic["epic_status"] == "reviewing"
        assert epic["current_stage"] == "review"

        # Verify a queued review stage_run was created
        stage_runs = db.list_stage_runs(conn, task_id=epic_id, status="queued")
        assert len(stage_runs) == 1
        assert stage_runs[0]["stage"] == "review"
        assert stage_runs[0]["attempt"] == 1

    async def test_advance_task_epic_not_complete_when_children_pending(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """AC 8: Epic stays paused/decomposed when children aren't all done."""
        epic_id = db.insert_task(
            conn,
            project_id=project_id,
            title="Epic",
            flow="epic",
            epic_status="decomposed",
        )
        db.update_task(conn, epic_id, status="paused")

        child1_id = db.insert_task(
            conn,
            project_id=project_id,
            title="Child 1",
            flow="quick",
            parent_task_id=epic_id,
        )
        db.insert_task(
            conn,
            project_id=project_id,
            title="Child 2",
            flow="quick",
            parent_task_id=epic_id,
        )

        engine = PipelineEngine(settings, ":memory:")

        # Only mark child 1 done
        db.update_task(conn, child1_id, status="active", current_stage="review")
        await engine.advance_task(conn, child1_id, "review")

        epic = db.get_task(conn, epic_id)
        assert epic["status"] == "paused"
        assert epic["epic_status"] == "decomposed"

    async def test_process_epic_decomposition_missing_file(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
        tmp_path,
    ) -> None:
        """AC 10: Missing decomposition file marks task needs_human."""
        db.update_project(conn, project_id, repo_path=str(tmp_path))
        project = dict(db.get_project(conn, project_id))

        task_id = db.insert_task(
            conn,
            project_id=project_id,
            title="Epic",
            flow="epic",
            epic_status="pending",
        )
        db.update_task(conn, task_id, status="active", current_stage="spec")

        engine = PipelineEngine(settings, ":memory:")
        await engine.advance_task(conn, task_id, "spec", project=project)

        task = db.get_task(conn, task_id)
        assert task["status"] == "needs_human"

    async def test_process_epic_decomposition_invalid_json(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
        tmp_path,
    ) -> None:
        """AC 11: Invalid JSON marks task needs_human."""
        db.update_project(conn, project_id, repo_path=str(tmp_path))
        project = dict(db.get_project(conn, project_id))

        task_id = db.insert_task(
            conn,
            project_id=project_id,
            title="Epic",
            flow="epic",
            epic_status="pending",
        )
        db.update_task(conn, task_id, status="active", current_stage="spec")

        decomp_dir = os.path.join(str(tmp_path), "_forge/epic-decompositions")
        os.makedirs(decomp_dir, exist_ok=True)
        with open(os.path.join(decomp_dir, f"{task_id}.json"), "w") as f:
            f.write("{not valid json")

        engine = PipelineEngine(settings, ":memory:")
        await engine.advance_task(conn, task_id, "spec", project=project)

        task = db.get_task(conn, task_id)
        assert task["status"] == "needs_human"

    async def test_process_epic_decomposition_empty_array(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
        tmp_path,
    ) -> None:
        """AC 12: Empty array marks task needs_human."""
        db.update_project(conn, project_id, repo_path=str(tmp_path))
        project = dict(db.get_project(conn, project_id))

        task_id = db.insert_task(
            conn,
            project_id=project_id,
            title="Epic",
            flow="epic",
            epic_status="pending",
        )
        db.update_task(conn, task_id, status="active", current_stage="spec")

        decomp_dir = os.path.join(str(tmp_path), "_forge/epic-decompositions")
        os.makedirs(decomp_dir, exist_ok=True)
        with open(os.path.join(decomp_dir, f"{task_id}.json"), "w") as f:
            json.dump([], f)

        engine = PipelineEngine(settings, ":memory:")
        await engine.advance_task(conn, task_id, "spec", project=project)

        task = db.get_task(conn, task_id)
        assert task["status"] == "needs_human"


    async def test_process_epic_decomposition_invalid_flow_falls_back_to_standard(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
        tmp_path,
    ) -> None:
        """Invalid flow value in decomposition entry falls back to standard."""
        db.update_project(conn, project_id, repo_path=str(tmp_path))
        project = dict(db.get_project(conn, project_id))

        task_id = db.insert_task(
            conn,
            project_id=project_id,
            title="Epic",
            flow="epic",
            epic_status="pending",
        )
        db.update_task(conn, task_id, status="active", current_stage="spec")

        decomp_dir = os.path.join(str(tmp_path), "_forge/epic-decompositions")
        os.makedirs(decomp_dir, exist_ok=True)
        with open(os.path.join(decomp_dir, f"{task_id}.json"), "w") as f:
            json.dump([{"title": "Child with bogus flow", "flow": "bogus"}], f)

        engine = PipelineEngine(settings, ":memory:")
        await engine.advance_task(conn, task_id, "spec", project=project)

        children = db.get_child_tasks(conn, task_id)
        assert len(children) == 1
        assert children[0]["flow"] == "standard"

    async def test_process_epic_decomposition_epic_flow_falls_back_to_standard(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
        tmp_path,
    ) -> None:
        """Child entry with flow='epic' is rejected and falls back to standard."""
        db.update_project(conn, project_id, repo_path=str(tmp_path))
        project = dict(db.get_project(conn, project_id))

        task_id = db.insert_task(
            conn,
            project_id=project_id,
            title="Epic",
            flow="epic",
            epic_status="pending",
        )
        db.update_task(conn, task_id, status="active", current_stage="spec")

        decomp_dir = os.path.join(str(tmp_path), "_forge/epic-decompositions")
        os.makedirs(decomp_dir, exist_ok=True)
        with open(os.path.join(decomp_dir, f"{task_id}.json"), "w") as f:
            json.dump([{"title": "Recursive epic attempt", "flow": "epic"}], f)

        engine = PipelineEngine(settings, ":memory:")
        await engine.advance_task(conn, task_id, "spec", project=project)

        children = db.get_child_tasks(conn, task_id)
        assert len(children) == 1
        assert children[0]["flow"] == "standard"

    async def test_process_epic_decomposition_json_object_not_array(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
        tmp_path,
    ) -> None:
        """JSON object (not array) in decomposition file marks task needs_human."""
        db.update_project(conn, project_id, repo_path=str(tmp_path))
        project = dict(db.get_project(conn, project_id))

        task_id = db.insert_task(
            conn,
            project_id=project_id,
            title="Epic",
            flow="epic",
            epic_status="pending",
        )
        db.update_task(conn, task_id, status="active", current_stage="spec")

        decomp_dir = os.path.join(str(tmp_path), "_forge/epic-decompositions")
        os.makedirs(decomp_dir, exist_ok=True)
        with open(os.path.join(decomp_dir, f"{task_id}.json"), "w") as f:
            json.dump({"title": "Not an array"}, f)

        engine = PipelineEngine(settings, ":memory:")
        await engine.advance_task(conn, task_id, "spec", project=project)

        task = db.get_task(conn, task_id)
        assert task["status"] == "needs_human"


class TestEpicDecompositionEdgeCases:
    async def test_epic_spec_without_project_marks_needs_human(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """Issue 1: Epic at spec stage with no project context marks needs_human."""
        task_id = db.insert_task(
            conn,
            project_id=project_id,
            title="Epic without project",
            flow="epic",
            epic_status="pending",
        )
        db.update_task(conn, task_id, status="active", current_stage="spec")

        engine = PipelineEngine(settings, ":memory:")
        await engine.advance_task(conn, task_id, "spec", project=None)

        task = db.get_task(conn, task_id)
        assert task["status"] == "needs_human"

    async def test_whitespace_only_title_skipped_in_decomposition(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
        tmp_path,
    ) -> None:
        """Issue 2: Whitespace-only titles are skipped during decomposition."""
        db.update_project(conn, project_id, repo_path=str(tmp_path))
        project = dict(db.get_project(conn, project_id))

        task_id = db.insert_task(
            conn,
            project_id=project_id,
            title="Epic with bad titles",
            flow="epic",
            epic_status="pending",
        )
        db.update_task(conn, task_id, status="active", current_stage="spec")

        decomp_dir = os.path.join(str(tmp_path), "_forge/epic-decompositions")
        os.makedirs(decomp_dir, exist_ok=True)
        with open(os.path.join(decomp_dir, f"{task_id}.json"), "w") as f:
            json.dump([
                {"title": "   ", "description": "Whitespace title"},
                {"title": "", "description": "Empty title"},
                {"title": "Valid child", "description": "This one is fine"},
            ], f)

        engine = PipelineEngine(settings, ":memory:")
        await engine.advance_task(conn, task_id, "spec", project=project)

        children = db.get_child_tasks(conn, task_id)
        assert len(children) == 1
        assert children[0]["title"] == "Valid child"


class TestEpicGateNameOverride:
    async def test_epic_gate_name_override(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """AC 9: Engine uses 'epic-spec' gate name for epic spec runs."""
        task_id = db.insert_task(
            conn,
            project_id=project_id,
            title="Epic task",
            flow="epic",
            epic_status="pending",
        )
        db.update_task(
            conn,
            task_id,
            status="active",
            current_stage="spec",
            branch_name="forge/epic-test",
        )
        db.insert_stage_run(
            conn, task_id=task_id, stage="spec", attempt=1, status="queued"
        )

        captured_gate_stage = []

        async def mock_run_gate(gate_dir, stage, env_vars):
            captured_gate_stage.append(stage)
            return GateResult(
                passed=True, exit_code=0, stdout="ok", stderr="",
                gate_name=f"post-{stage}.sh", duration_seconds=0.1,
            )

        async def mock_dispatch(**kwargs):
            return DispatchResult(
                output="done", exit_code=0, error=None, duration_seconds=1.0, tokens_used=10
            )

        engine = PipelineEngine(settings, ":memory:")
        engine.running = True

        with patch("forge.engine.run_gate", side_effect=mock_run_gate), \
             patch("forge.engine.dispatch_claude", side_effect=mock_dispatch), \
             patch("forge.engine.database.get_connection", return_value=_UnclosableConnection(conn)):
            # Run one iteration of the loop
            engine._loop_task = asyncio.ensure_future(engine.run_loop())
            await asyncio.sleep(0.5)
            engine.running = False
            engine._loop_task.cancel()
            try:
                await engine._loop_task
            except asyncio.CancelledError:
                pass

        assert "epic-spec" in captured_gate_stage

    async def test_epic_review_gate_name_override(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """Engine uses 'epic-review' gate name for epic review runs."""
        task_id = db.insert_task(
            conn,
            project_id=project_id,
            title="Epic task",
            flow="epic",
            epic_status="reviewing",
        )
        db.update_task(
            conn,
            task_id,
            status="active",
            current_stage="review",
            branch_name="forge/epic-review-test",
        )
        db.insert_stage_run(
            conn, task_id=task_id, stage="review", attempt=1, status="queued"
        )

        captured_gate_stage = []

        async def mock_run_gate(gate_dir, stage, env_vars):
            captured_gate_stage.append(stage)
            return GateResult(
                passed=True, exit_code=0, stdout="ok", stderr="",
                gate_name=f"post-{stage}.sh", duration_seconds=0.1,
            )

        async def mock_dispatch(**kwargs):
            return DispatchResult(
                output="done", exit_code=0, error=None, duration_seconds=1.0, tokens_used=10
            )

        engine = PipelineEngine(settings, ":memory:")
        engine.running = True

        with patch("forge.engine.run_gate", side_effect=mock_run_gate), \
             patch("forge.engine.dispatch_claude", side_effect=mock_dispatch), \
             patch("forge.engine.database.get_connection", return_value=_UnclosableConnection(conn)):
            engine._loop_task = asyncio.ensure_future(engine.run_loop())
            await asyncio.sleep(0.5)
            engine.running = False
            engine._loop_task.cancel()
            try:
                await engine._loop_task
            except asyncio.CancelledError:
                pass

        assert "epic-review" in captured_gate_stage


# ---------------------------------------------------------------------------
# Epic review tests
# ---------------------------------------------------------------------------


class TestEpicReview:
    """Tests for epic review triggering, advance, and bounce."""

    async def test_check_epic_completion_partial_children(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """Completing one child when siblings are pending does NOT trigger epic review."""
        epic_id = db.insert_task(
            conn,
            project_id=project_id,
            title="Epic",
            flow="epic",
            epic_status="decomposed",
        )
        db.update_task(conn, epic_id, status="paused")

        child1_id = db.insert_task(
            conn, project_id=project_id, title="Child 1", flow="quick", parent_task_id=epic_id,
        )
        db.insert_task(
            conn, project_id=project_id, title="Child 2", flow="quick", parent_task_id=epic_id,
        )

        # Complete only child 1
        db.update_task(conn, child1_id, status="done")

        engine = PipelineEngine(settings, ":memory:")
        engine._check_epic_completion(conn, epic_id)

        epic = db.get_task(conn, epic_id)
        assert epic["epic_status"] == "decomposed"
        assert epic["status"] == "paused"

        # No stage_runs should have been created for the epic
        runs = db.list_stage_runs(conn, task_id=epic_id)
        assert len(runs) == 0

    async def test_check_epic_completion_all_children_done(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """Completing the last child triggers epic review (parent moves to reviewing)."""
        epic_id = db.insert_task(
            conn,
            project_id=project_id,
            title="Epic",
            flow="epic",
            epic_status="decomposed",
        )
        db.update_task(conn, epic_id, status="paused")

        child1_id = db.insert_task(
            conn, project_id=project_id, title="Child 1", flow="quick", parent_task_id=epic_id,
        )
        child2_id = db.insert_task(
            conn, project_id=project_id, title="Child 2", flow="quick", parent_task_id=epic_id,
        )
        db.update_task(conn, child1_id, status="done")
        db.update_task(conn, child2_id, status="done")

        engine = PipelineEngine(settings, ":memory:")
        engine._check_epic_completion(conn, epic_id)

        epic = db.get_task(conn, epic_id)
        assert epic["epic_status"] == "reviewing"
        assert epic["status"] == "active"
        assert epic["current_stage"] == "review"

        runs = db.list_stage_runs(conn, task_id=epic_id, status="queued")
        assert len(runs) == 1
        assert runs[0]["stage"] == "review"
        assert runs[0]["attempt"] == 1

    async def test_check_epic_completion_guards_double_transition(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """If epic is already reviewing, a second call does not create another stage_run."""
        epic_id = db.insert_task(
            conn,
            project_id=project_id,
            title="Epic",
            flow="epic",
            epic_status="reviewing",
        )
        db.update_task(conn, epic_id, status="active", current_stage="review")

        child_id = db.insert_task(
            conn, project_id=project_id, title="Child", flow="quick", parent_task_id=epic_id,
        )
        db.update_task(conn, child_id, status="done")

        engine = PipelineEngine(settings, ":memory:")
        engine._check_epic_completion(conn, epic_id)

        # epic_status should remain "reviewing" — no double transition
        epic = db.get_task(conn, epic_id)
        assert epic["epic_status"] == "reviewing"

        # No new stage_runs created
        runs = db.list_stage_runs(conn, task_id=epic_id)
        assert len(runs) == 0

    async def test_check_epic_completion_increments_attempt(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """Second epic review after bounce uses attempt=2, not attempt=1."""
        epic_id = db.insert_task(
            conn,
            project_id=project_id,
            title="Epic",
            flow="epic",
            epic_status="decomposed",
        )
        db.update_task(conn, epic_id, status="paused")

        # Simulate a prior review stage_run (from first review cycle)
        db.insert_stage_run(
            conn, task_id=epic_id, stage="review", attempt=1, status="complete"
        )

        child_id = db.insert_task(
            conn, project_id=project_id, title="Follow-up child", flow="quick", parent_task_id=epic_id,
        )
        db.update_task(conn, child_id, status="done")

        engine = PipelineEngine(settings, ":memory:")
        engine._check_epic_completion(conn, epic_id)

        runs = db.list_stage_runs(conn, task_id=epic_id, status="queued")
        assert len(runs) == 1
        assert runs[0]["stage"] == "review"
        assert runs[0]["attempt"] == 2

    async def test_advance_task_epic_review_pass(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
        tmp_path,
    ) -> None:
        """PASS verdict on epic review sets epic to complete."""
        db.update_project(conn, project_id, repo_path=str(tmp_path))
        project = dict(db.get_project(conn, project_id))

        epic_id = db.insert_task(
            conn,
            project_id=project_id,
            title="Epic",
            flow="epic",
            epic_status="reviewing",
        )
        db.update_task(conn, epic_id, status="active", current_stage="review")

        engine = PipelineEngine(settings, ":memory:")
        await engine.advance_task(conn, epic_id, "review", project=project)

        epic = db.get_task(conn, epic_id)
        assert epic["epic_status"] == "complete"
        assert epic["status"] == "done"
        assert epic["completed_at"] is not None

    async def test_bounce_task_epic_review_issues(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
        tmp_path,
    ) -> None:
        """ISSUES verdict creates follow-up tasks and resets epic to decomposed."""
        db.update_project(conn, project_id, repo_path=str(tmp_path))
        project = dict(db.get_project(conn, project_id))

        epic_id = db.insert_task(
            conn,
            project_id=project_id,
            title="Epic",
            flow="epic",
            epic_status="reviewing",
        )
        db.update_task(conn, epic_id, status="active", current_stage="review")
        epic_task = dict(db.get_task(conn, epic_id))

        # Write follow-ups file
        followups_dir = tmp_path / "_forge" / "follow-ups"
        followups_dir.mkdir(parents=True)
        followups = [
            {"title": "Fix integration gap", "description": "Missing glue code"},
            {"title": "Add missing test", "description": "Coverage gap"},
        ]
        (followups_dir / f"{epic_id}.json").write_text(json.dumps(followups))

        gate_result = GateResult(
            gate_name="post-epic-review",
            exit_code=1,
            passed=False,
            stdout="",
            stderr="ISSUES found",
            duration_seconds=0.1,
        )
        engine = PipelineEngine(settings, ":memory:")
        await engine.bounce_task(conn, epic_task, "review", gate_result, project=project)

        # Epic should be reset to decomposed/paused
        epic = db.get_task(conn, epic_id)
        assert epic["epic_status"] == "decomposed"
        assert epic["status"] == "paused"

        # Follow-up tasks should have been created as children of the epic
        children = conn.execute(
            "SELECT * FROM tasks WHERE parent_task_id = ? AND title IN (?, ?)",
            (epic_id, "Fix integration gap", "Add missing test"),
        ).fetchall()
        assert len(children) == 2
        for child in children:
            assert child["flow"] == "quick"
            assert child["parent_task_id"] == epic_id

    async def test_bounce_task_epic_review_followups_default_quick(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
        tmp_path,
    ) -> None:
        """Follow-up tasks from epic review default to quick flow."""
        db.update_project(conn, project_id, repo_path=str(tmp_path))
        project = dict(db.get_project(conn, project_id))

        epic_id = db.insert_task(
            conn,
            project_id=project_id,
            title="Epic",
            flow="epic",
            epic_status="reviewing",
        )
        db.update_task(conn, epic_id, status="active", current_stage="review")
        epic_task = dict(db.get_task(conn, epic_id))

        # Follow-ups without explicit flow field
        followups_dir = tmp_path / "_forge" / "follow-ups"
        followups_dir.mkdir(parents=True)
        followups = [
            {"title": "Task A", "description": "Do A"},
        ]
        (followups_dir / f"{epic_id}.json").write_text(json.dumps(followups))

        gate_result = GateResult(
            gate_name="post-epic-review", exit_code=1, passed=False, stdout="", stderr="ISSUES",
            duration_seconds=0.1,
        )
        engine = PipelineEngine(settings, ":memory:")
        await engine.bounce_task(conn, epic_task, "review", gate_result, project=project)

        # The created follow-up should default to quick
        tasks = conn.execute(
            "SELECT flow FROM tasks WHERE title = 'Task A'"
        ).fetchall()
        assert len(tasks) == 1
        assert tasks[0]["flow"] == "quick"

    async def test_epic_review_integration_two_children(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """Integration: completing 2 children transitions epic to reviewing with queued stage_run."""
        epic_id = db.insert_task(
            conn,
            project_id=project_id,
            title="Epic integration test",
            flow="epic",
            epic_status="decomposed",
        )
        db.update_task(conn, epic_id, status="paused")

        child1_id = db.insert_task(
            conn, project_id=project_id, title="Child A", flow="quick", parent_task_id=epic_id,
        )
        child2_id = db.insert_task(
            conn, project_id=project_id, title="Child B", flow="quick", parent_task_id=epic_id,
        )

        engine = PipelineEngine(settings, ":memory:")

        # Complete child 1 via advance_task
        db.update_task(conn, child1_id, status="active", current_stage="review")
        await engine.advance_task(conn, child1_id, "review")

        # Epic still paused
        epic = db.get_task(conn, epic_id)
        assert epic["epic_status"] == "decomposed"

        # Complete child 2
        db.update_task(conn, child2_id, status="active", current_stage="review")
        await engine.advance_task(conn, child2_id, "review")

        # Epic should now be reviewing
        epic = db.get_task(conn, epic_id)
        assert epic["epic_status"] == "reviewing"
        assert epic["status"] == "active"
        assert epic["current_stage"] == "review"

        runs = db.list_stage_runs(conn, task_id=epic_id, status="queued")
        assert len(runs) == 1
        assert runs[0]["stage"] == "review"


# ---------------------------------------------------------------------------
# AC 1: _parse_stage_timeouts helper
# ---------------------------------------------------------------------------


class TestParseStageTimeouts:
    def test_returns_dict_when_present(self) -> None:
        project = {"stage_timeouts": '{"implement": 900}'}
        result = _parse_stage_timeouts(project)
        assert result == {"implement": 900}

    def test_returns_none_when_null(self) -> None:
        project = {"stage_timeouts": None}
        assert _parse_stage_timeouts(project) is None

    def test_returns_none_when_missing(self) -> None:
        project = {}
        assert _parse_stage_timeouts(project) is None

    def test_returns_none_when_empty_string(self) -> None:
        project = {"stage_timeouts": ""}
        assert _parse_stage_timeouts(project) is None


# ---------------------------------------------------------------------------
# AC 2: Auto-merge error context tests
# ---------------------------------------------------------------------------


class TestAutoMergeErrorContext:
    async def test_auto_merge_checkout_failure_includes_stderr(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """AC 2: checkout_and_pull failure stderr appears in run_log."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(conn, project_id=project_id, title="T", priority=1)
        db.update_task(
            conn, task_id, status="active", current_stage="review",
            branch_name="forge/test-branch",
        )

        project = dict(db.get_project(conn, project_id))
        task = dict(db.get_task(conn, task_id))

        cop_fail = GitResult(
            success=False, stdout="", stderr="fatal: not a git repo", returncode=128,
        )

        safe_conn = _UnclosableConnection(conn)
        with (
            patch("forge.engine.database.get_connection", return_value=safe_conn),
            patch("forge.engine.checkout_and_pull", new_callable=AsyncMock, return_value=cop_fail),
        ):
            result = await engine._auto_merge(conn, task, project)

        assert result is False
        logs = db.get_logs(conn, task_id=task_id)
        error_logs = [r for r in logs if r["level"] == "error"]
        assert any("fatal: not a git repo" in r["message"] for r in error_logs)

    async def test_auto_merge_rebase_failure_includes_stderr(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """AC 2: rebase_branch failure stderr appears in run_log."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(conn, project_id=project_id, title="T", priority=1)
        db.update_task(
            conn, task_id, status="active", current_stage="review",
            branch_name="forge/test-branch",
        )

        project = dict(db.get_project(conn, project_id))
        task = dict(db.get_task(conn, task_id))

        cop_ok = GitResult(success=True, stdout="", stderr="", returncode=0)
        rebase_fail = GitResult(
            success=False, stdout="", stderr="CONFLICT: merge conflict in foo.py", returncode=1,
        )

        safe_conn = _UnclosableConnection(conn)
        with (
            patch("forge.engine.database.get_connection", return_value=safe_conn),
            patch("forge.engine.checkout_and_pull", new_callable=AsyncMock, return_value=cop_ok),
            patch("forge.engine.rebase_branch", new_callable=AsyncMock, return_value=rebase_fail),
        ):
            result = await engine._auto_merge(conn, task, project)

        assert result is False
        logs = db.get_logs(conn, task_id=task_id)
        error_logs = [r for r in logs if r["level"] == "error"]
        assert any("CONFLICT" in r["message"] for r in error_logs)

    async def test_auto_merge_ff_merge_failure_includes_stderr(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """AC 2: ff_merge failure stderr appears in run_log."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(conn, project_id=project_id, title="T", priority=1)
        db.update_task(
            conn, task_id, status="active", current_stage="review",
            branch_name="forge/test-branch",
        )
        # Create an implement stage_run for the gate env
        db.insert_stage_run(
            conn, task_id=task_id, stage="implement", attempt=1, status="passed",
        )

        project = dict(db.get_project(conn, project_id))
        task = dict(db.get_task(conn, task_id))

        cop_ok = GitResult(success=True, stdout="", stderr="", returncode=0)
        rebase_ok = GitResult(success=True, stdout="", stderr="", returncode=0)
        gate_pass = GateResult(
            passed=True, exit_code=0, stdout="ok", stderr="",
            gate_name="post-implement.sh", duration_seconds=1.0,
        )
        merge_fail = GitResult(
            success=False, stdout="", stderr="not possible to fast-forward", returncode=1,
        )

        safe_conn = _UnclosableConnection(conn)
        with (
            patch("forge.engine.database.get_connection", return_value=safe_conn),
            patch("forge.engine.checkout_and_pull", new_callable=AsyncMock, return_value=cop_ok),
            patch("forge.engine.rebase_branch", new_callable=AsyncMock, return_value=rebase_ok),
            patch("forge.engine.run_gate", new_callable=AsyncMock, return_value=gate_pass),
            patch("forge.engine.ff_merge", new_callable=AsyncMock, return_value=merge_fail),
        ):
            result = await engine._auto_merge(conn, task, project)

        assert result is False
        logs = db.get_logs(conn, task_id=task_id)
        error_logs = [r for r in logs if r["level"] == "error"]
        assert any("not possible to fast-forward" in r["message"] for r in error_logs)


# ---------------------------------------------------------------------------
# AC 4: Multi-bounce attempt numbering
# ---------------------------------------------------------------------------


class TestMultiBounceAttemptNumbering:
    async def test_multi_bounce_attempt_numbering(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """AC 4: After two review bounces, implement attempts are numbered 1, 2, 3."""
        safe_conn = _UnclosableConnection(conn)
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn, project_id=project_id, title="T", priority=1, max_retries=10,
        )
        db.update_task(conn, task_id, status="active", current_stage="implement")

        gate_result = GateResult(
            passed=False, exit_code=1, stdout="", stderr="ISSUES",
            gate_name="post-review.sh", duration_seconds=1.0,
        )

        # Implement attempt 1 passes
        db.insert_stage_run(
            conn, task_id=task_id, stage="implement", attempt=1, status="passed",
        )
        # Review attempt 1 bounces
        db.insert_stage_run(
            conn, task_id=task_id, stage="review", attempt=1, status="bounced",
        )

        with patch("forge.engine.database.get_connection", return_value=safe_conn):
            task = dict(db.get_task(conn, task_id))
            await engine.bounce_task(conn, task, "review", gate_result)

            # Should create implement attempt 2
            queued = db.list_stage_runs(conn, task_id=task_id, stage="implement", status="queued")
            assert len(queued) == 1
            assert queued[0]["attempt"] == 2

            # Mark implement attempt 2 as passed
            db.update_stage_run(conn, queued[0]["id"], status="passed")

            # Review attempt 2 bounces
            db.insert_stage_run(
                conn, task_id=task_id, stage="review", attempt=2, status="bounced",
            )

            task = dict(db.get_task(conn, task_id))
            await engine.bounce_task(conn, task, "review", gate_result)

            # Should create implement attempt 3
            queued2 = db.list_stage_runs(conn, task_id=task_id, stage="implement", status="queued")
            assert len(queued2) == 1
            assert queued2[0]["attempt"] == 3


# ---------------------------------------------------------------------------
# AC 6: migrate() does not override pause_after_completion
# ---------------------------------------------------------------------------


class TestMigrateDoesNotOverridePause:
    def test_migrate_does_not_override_pause_after_completion(self) -> None:
        """AC 6: Calling migrate() again must not re-enable pause_after_completion."""
        c = db.get_connection(":memory:")
        db.migrate(c)

        # Insert a project named "Forge" with pause enabled
        pid = db.insert_project(c, name="Forge", repo_path="/tmp/forge")
        # Manually disable pause
        c.execute("UPDATE projects SET pause_after_completion = 0 WHERE id = ?", (pid,))
        c.commit()

        # Verify it's off
        proj = db.get_project(c, pid)
        assert proj["pause_after_completion"] == 0

        # Run migrate again
        db.migrate(c)

        # Should still be off
        proj = db.get_project(c, pid)
        assert proj["pause_after_completion"] == 0
        c.close()


# ---------------------------------------------------------------------------
# AC 8: Auto-pause triggers on timeout path
# ---------------------------------------------------------------------------


class TestAutoPauseOnTimeout:
    async def test_auto_pause_triggers_on_timeout_path(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
    ) -> None:
        """AC 8: Engine auto-pauses when a timeout causes needs_human on a pause-enabled project."""
        pause_project_id = db.insert_project(
            conn,
            name="PauseTimeoutProject",
            repo_path="/tmp/repo",
            gate_dir="/tmp/repo/gates",
            pause_after_completion=True,
        )
        safe_conn = _UnclosableConnection(conn)
        engine = PipelineEngine(settings, ":memory:")
        engine.running = True

        task_id = db.insert_task(
            conn,
            project_id=pause_project_id,
            title="Timeout Task",
            priority=1,
            max_retries=0,
        )
        db.update_task(conn, task_id, status="active", current_stage="spec")

        # Create a running stage_run that started long ago
        old_time = (datetime.now(timezone.utc) - timedelta(seconds=9999)).isoformat()
        sr_id = db.insert_stage_run(
            conn, task_id=task_id, stage="spec", attempt=1, status="running",
        )
        db.update_stage_run(conn, sr_id, started_at=old_time)

        with patch("forge.engine.database.get_connection", return_value=safe_conn):
            await engine._check_timeouts(conn)

        sr = db.get_stage_run(conn, sr_id)
        assert sr["status"] == "error"

        task = db.get_task(conn, task_id)
        assert task["status"] == "needs_human"
        assert engine.running is False


# ---------------------------------------------------------------------------
# AC 11: Auto-merge restores default branch on failure
# ---------------------------------------------------------------------------


class TestAutoMergeRestoresDefaultBranch:
    async def test_auto_merge_restores_default_branch_on_gate_failure(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """AC 11: After gate failure post-rebase, cleanup checkout restores default branch."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(conn, project_id=project_id, title="T", priority=1)
        db.update_task(
            conn, task_id, status="active", current_stage="review",
            branch_name="forge/test-branch",
        )
        db.insert_stage_run(
            conn, task_id=task_id, stage="implement", attempt=1, status="passed",
        )

        project = dict(db.get_project(conn, project_id))
        task = dict(db.get_task(conn, task_id))

        cop_ok = GitResult(success=True, stdout="", stderr="", returncode=0)
        rebase_ok = GitResult(success=True, stdout="", stderr="", returncode=0)
        gate_fail = GateResult(
            passed=False, exit_code=1, stdout="", stderr="gate failed",
            gate_name="post-implement.sh", duration_seconds=1.0,
        )

        safe_conn = _UnclosableConnection(conn)
        checkout_mock = AsyncMock(return_value=cop_ok)
        with (
            patch("forge.engine.database.get_connection", return_value=safe_conn),
            patch("forge.engine.checkout_and_pull", checkout_mock),
            patch("forge.engine.rebase_branch", new_callable=AsyncMock, return_value=rebase_ok),
            patch("forge.engine.run_gate", new_callable=AsyncMock, return_value=gate_fail),
        ):
            result = await engine._auto_merge(conn, task, project)

        assert result is False
        # checkout_and_pull should be called twice: once at step 1, once for cleanup
        assert checkout_mock.call_count == 2
        # Second call should be the cleanup with default branch
        assert checkout_mock.call_args_list[1][0] == ("/tmp/repo", "main")

    async def test_auto_merge_restores_default_branch_on_merge_failure(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """AC 11: After ff_merge failure, cleanup checkout restores default branch."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(conn, project_id=project_id, title="T", priority=1)
        db.update_task(
            conn, task_id, status="active", current_stage="review",
            branch_name="forge/test-branch",
        )
        db.insert_stage_run(
            conn, task_id=task_id, stage="implement", attempt=1, status="passed",
        )

        project = dict(db.get_project(conn, project_id))
        task = dict(db.get_task(conn, task_id))

        cop_ok = GitResult(success=True, stdout="", stderr="", returncode=0)
        rebase_ok = GitResult(success=True, stdout="", stderr="", returncode=0)
        gate_pass = GateResult(
            passed=True, exit_code=0, stdout="ok", stderr="",
            gate_name="post-implement.sh", duration_seconds=1.0,
        )
        merge_fail = GitResult(
            success=False, stdout="", stderr="ff failed", returncode=1,
        )

        safe_conn = _UnclosableConnection(conn)
        checkout_mock = AsyncMock(return_value=cop_ok)
        with (
            patch("forge.engine.database.get_connection", return_value=safe_conn),
            patch("forge.engine.checkout_and_pull", checkout_mock),
            patch("forge.engine.rebase_branch", new_callable=AsyncMock, return_value=rebase_ok),
            patch("forge.engine.run_gate", new_callable=AsyncMock, return_value=gate_pass),
            patch("forge.engine.ff_merge", new_callable=AsyncMock, return_value=merge_fail),
        ):
            result = await engine._auto_merge(conn, task, project)

        assert result is False
        # checkout_and_pull called 3 times: step 1, step 4, cleanup
        assert checkout_mock.call_count == 3


# ---------------------------------------------------------------------------
# AC 10: Engine status template handles current_stage=None
# ---------------------------------------------------------------------------


class TestEngineStatusNullStage:
    def test_engine_status_null_stage(self) -> None:
        """AC 10: Template renders without 'None' when current_stage is None."""
        from pathlib import Path

        from jinja2 import Environment, FileSystemLoader

        template_dir = Path(__file__).resolve().parent.parent / "templates"
        env = Environment(loader=FileSystemLoader(str(template_dir)))
        template = env.get_template("partials/engine_status.html")

        class _Status:
            running = True
            current_task_title = "My Task"
            current_stage = None

        html = template.render(status=_Status())
        assert "None" not in html
        assert "()" not in html


def test_no_redundant_local_import_json():
    """test_engine.py has a top-level `import json`; no local re-imports should exist."""
    import ast
    from pathlib import Path

    source = Path(__file__).read_text()
    tree = ast.parse(source)

    top_level_imports = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top_level_imports.add(alias.name)

    assert "json" in top_level_imports, "Expected top-level `import json`"

    local_reimports: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and not isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "json" and node.lineno > 1:
                    # Check it's inside a function (not top-level)
                    if node.col_offset > 0:
                        local_reimports.append(node.lineno)

    assert local_reimports == [], (
        f"Found redundant local `import json` at lines: {local_reimports}. "
        f"Use the top-level import instead."
    )
