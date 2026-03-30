"""Tests for forge.engine module."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from forge import database as db
from forge.config import Settings
from forge.dispatcher import DispatchResult, GitResult
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
    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
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
    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
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


# ---------------------------------------------------------------------------
# Engine: timeout detection
# ---------------------------------------------------------------------------


class TestTimeoutDetection:
    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
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
        # No bounced/error implement runs yet, so retry_count=0, new_attempt = 0+2 = 2
        assert queued[0]["attempt"] == 2

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
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


# ---------------------------------------------------------------------------
# Engine: follow-ups processing
# ---------------------------------------------------------------------------


class TestProcessFollowUps:
    """Tests for follow-up task creation after review passes (AC 10, 11, 12, 19)."""

    @pytest.mark.asyncio
    async def test_follow_ups_create_backlog_tasks_with_links(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
        tmp_path,
    ) -> None:
        """AC 10, 11, 12, 19: Follow-up JSON entries produce backlog tasks with created_by links."""
        import json
        import os

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

    @pytest.mark.asyncio
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


    @pytest.mark.asyncio
    async def test_follow_ups_string_entries_create_backlog_tasks(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
        tmp_path,
    ) -> None:
        """Plain string entries in follow-up JSON are ingested as backlog tasks."""
        import json
        import os

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

    @pytest.mark.asyncio
    async def test_follow_ups_mixed_entries(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
        tmp_path,
    ) -> None:
        """Arrays with both dict and string entries are fully processed."""
        import json
        import os

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

    @pytest.mark.asyncio
    async def test_follow_ups_skips_invalid_entries(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
        tmp_path,
    ) -> None:
        """Invalid entries (null, numbers) are skipped; valid entries still processed."""
        import json
        import os

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
    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
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
        import json

        logs = db.get_logs(conn, task_id=task_id)
        meta_logs = [
            row for row in logs if row["metadata"] is not None
        ]
        assert len(meta_logs) > 0
        meta = json.loads(meta_logs[0]["metadata"])
        assert meta["git_stderr"] == "CONFLICT in README.md"
        assert meta["git_returncode"] == 1

    @pytest.mark.asyncio
    async def test_log_helper_passes_metadata(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
    ) -> None:
        """AC #6: _log passes metadata through to database.insert_log."""
        import json

        engine = PipelineEngine(settings, ":memory:")
        safe_conn = _UnclosableConnection(conn)
        with patch("forge.engine.database.get_connection", return_value=safe_conn):
            engine._log("info", "test message", metadata={"key": "val"})

        logs = db.get_logs(conn)
        assert len(logs) >= 1
        meta_log = [row for row in logs if row["message"] == "test message"][0]
        meta = json.loads(meta_log["metadata"])
        assert meta["key"] == "val"

    @pytest.mark.asyncio
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
