"""Integration tests — full pipeline flow and self-registration."""

from __future__ import annotations

import asyncio
import sqlite3
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from forge import database as db
from forge.config import STAGES, Settings
from forge.dispatcher import DispatchResult
from forge.engine import PipelineEngine
from forge.gate_runner import GateResult
from forge.main import app

# Save real methods before conftest's autouse fixture replaces them.
_real_start = PipelineEngine.start
_real_pause = PipelineEngine.pause


@pytest.fixture(autouse=True)
def _real_engine_methods(monkeypatch):
    """Undo the conftest no-op patches so integration tests use real start/pause."""
    monkeypatch.setattr(PipelineEngine, "start", _real_start)
    monkeypatch.setattr(PipelineEngine, "pause", _real_pause)


class _UnclosableConnection:
    """Wraps a sqlite3.Connection so that close() is a no-op."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def close(self) -> None:
        pass

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


def _passing_gate(stage: str) -> GateResult:
    return GateResult(
        passed=True, exit_code=0, stdout="ok", stderr="",
        gate_name=f"post-{stage}.sh", duration_seconds=0.5,
    )


def _failing_gate(stage: str, reason: str = "gate check failed") -> GateResult:
    return GateResult(
        passed=False, exit_code=1, stdout="", stderr=reason,
        gate_name=f"post-{stage}.sh", duration_seconds=0.5,
    )


def _dispatch_ok(output: str = "canned output") -> DispatchResult:
    return DispatchResult(
        output=output, exit_code=0, duration_seconds=3.0, tokens_used=100,
    )


@pytest.fixture()
def conn() -> sqlite3.Connection:
    """In-memory database with schema applied."""
    c = db.get_connection(":memory:")
    db.migrate(c)
    return c


@pytest.fixture()
def settings() -> Settings:
    return Settings()


@pytest.fixture()
def project_id(conn: sqlite3.Connection) -> str:
    return db.insert_project(
        conn, name="IntegrationProject", repo_path="/tmp/repo",
        default_branch="main", gate_dir="/tmp/repo/gates",
    )


@pytest.fixture()
def client():
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ---------------------------------------------------------------------------
# Helper to run the engine for a controlled number of iterations
# ---------------------------------------------------------------------------

async def _run_engine_iterations(
    engine: PipelineEngine, max_seconds: float = 2.0,
) -> None:
    """Start the engine loop, let it run briefly, then stop."""
    engine.running = True
    loop_task = asyncio.create_task(engine.run_loop())
    await asyncio.sleep(max_seconds)
    engine.running = False
    try:
        await asyncio.wait_for(loop_task, timeout=5.0)
    except asyncio.TimeoutError:
        loop_task.cancel()


# ---------------------------------------------------------------------------
# Full pipeline flow: spec → plan → implement → review → done
# ---------------------------------------------------------------------------


class TestFullPipelineFlow:
    """End-to-end pipeline flow with mocked dispatcher and gates."""

    @pytest.mark.asyncio
    async def test_task_completes_all_stages(
        self, conn: sqlite3.Connection, settings: Settings, project_id: str,
    ) -> None:
        """A task goes through spec→plan→implement→review→done."""
        task_id = db.insert_task(
            conn, project_id=project_id, title="Full flow task", priority=5,
        )
        db.update_task(conn, task_id, status="active", current_stage="spec")
        db.insert_stage_run(
            conn, task_id=task_id, stage="spec", attempt=1, status="queued",
        )
        db.update_task(conn, task_id, branch_name="forge/test-full-flow")

        safe_conn = _UnclosableConnection(conn)
        engine = PipelineEngine(settings, ":memory:")

        # Track which stages are dispatched
        dispatched_stages: list[str] = []
        stage_index = 0

        async def mock_dispatch(prompt, repo_path, branch, timeout, **kwargs):
            nonlocal stage_index
            dispatched_stages.append(STAGES[stage_index])
            stage_index += 1
            return _dispatch_ok(f"output for stage {stage_index}")

        async def mock_gate(gate_dir, stage, env_vars):
            return _passing_gate(stage)

        with (
            patch("forge.engine.database.get_connection", return_value=safe_conn),
            patch("forge.engine.dispatch_claude", side_effect=mock_dispatch),
            patch("forge.engine.run_gate", side_effect=mock_gate),
            patch("forge.engine.build_prompt", return_value="test prompt"),
            patch("forge.engine.create_branch", new_callable=AsyncMock, return_value=True),
            patch("forge.engine.rebase_branch", new_callable=AsyncMock, return_value=True),
        ):
            await _run_engine_iterations(engine, max_seconds=8.0)

        # Task should be done
        task = db.get_task(conn, task_id)
        assert task["status"] == "done"
        assert task["completed_at"] is not None

        # All four stages should have been dispatched
        assert dispatched_stages == ["spec", "plan", "implement", "review"]

        # Each stage should have a passed stage_run
        for stage in STAGES:
            runs = db.list_stage_runs(conn, task_id=task_id, stage=stage, status="passed")
            assert len(runs) == 1, f"Expected 1 passed run for {stage}, got {len(runs)}"

    @pytest.mark.asyncio
    async def test_stage_advancement_order(
        self, conn: sqlite3.Connection, settings: Settings, project_id: str,
    ) -> None:
        """Verify stages advance in the correct order."""
        task_id = db.insert_task(
            conn, project_id=project_id, title="Order check", priority=1,
        )
        db.update_task(conn, task_id, status="active", current_stage="spec")
        db.insert_stage_run(
            conn, task_id=task_id, stage="spec", attempt=1, status="queued",
        )
        db.update_task(conn, task_id, branch_name="forge/test-order")

        safe_conn = _UnclosableConnection(conn)
        engine = PipelineEngine(settings, ":memory:")

        observed_stages: list[str] = []

        async def tracking_dispatch(prompt, repo_path, branch, timeout, **kwargs):
            # Determine current stage from the running stage_runs
            runs = db.list_stage_runs(conn, task_id=task_id, status="running")
            for r in runs:
                observed_stages.append(r["stage"])
            return _dispatch_ok()

        async def mock_gate(gate_dir, stage, env_vars):
            return _passing_gate(stage)

        with (
            patch("forge.engine.database.get_connection", return_value=safe_conn),
            patch("forge.engine.dispatch_claude", side_effect=tracking_dispatch),
            patch("forge.engine.run_gate", side_effect=mock_gate),
            patch("forge.engine.build_prompt", return_value="prompt"),
            patch("forge.engine.create_branch", new_callable=AsyncMock, return_value=True),
            patch("forge.engine.rebase_branch", new_callable=AsyncMock, return_value=True),
        ):
            await _run_engine_iterations(engine, max_seconds=8.0)

        assert observed_stages == ["spec", "plan", "implement", "review"]


# ---------------------------------------------------------------------------
# Bounce flow: gate failure → retry with context
# ---------------------------------------------------------------------------


class TestBounceFlow:
    @pytest.mark.asyncio
    async def test_gate_failure_triggers_retry(
        self, conn: sqlite3.Connection, settings: Settings, project_id: str,
    ) -> None:
        """When a gate fails, the task gets a new queued stage_run for retry."""
        task_id = db.insert_task(
            conn, project_id=project_id, title="Bounce test", priority=5, max_retries=3,
        )
        db.update_task(conn, task_id, status="active", current_stage="spec")
        db.insert_stage_run(
            conn, task_id=task_id, stage="spec", attempt=1, status="queued",
        )
        db.update_task(conn, task_id, branch_name="forge/test-bounce")

        safe_conn = _UnclosableConnection(conn)
        engine = PipelineEngine(settings, ":memory:")

        call_count = 0

        async def mock_gate(gate_dir, stage, env_vars):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _failing_gate(stage, "spec too short")
            # Pass on second attempt
            return _passing_gate(stage)

        with (
            patch("forge.engine.database.get_connection", return_value=safe_conn),
            patch("forge.engine.dispatch_claude", new_callable=AsyncMock, return_value=_dispatch_ok()),
            patch("forge.engine.run_gate", side_effect=mock_gate),
            patch("forge.engine.build_prompt", return_value="prompt"),
            patch("forge.engine.create_branch", new_callable=AsyncMock, return_value=True),
            patch("forge.engine.rebase_branch", new_callable=AsyncMock, return_value=True),
        ):
            await _run_engine_iterations(engine, max_seconds=3.0)

        # First spec run should be bounced
        bounced = db.list_stage_runs(conn, task_id=task_id, stage="spec", status="bounced")
        assert len(bounced) == 1
        assert bounced[0]["gate_stderr"] == "spec too short"

        # Second spec run should have passed
        passed = db.list_stage_runs(conn, task_id=task_id, stage="spec", status="passed")
        assert len(passed) == 1

        # Task should have advanced past spec
        task = db.get_task(conn, task_id)
        assert task["current_stage"] != "spec"


# ---------------------------------------------------------------------------
# Needs human flow: max retries exceeded
# ---------------------------------------------------------------------------


class TestNeedsHumanFlow:
    @pytest.mark.asyncio
    async def test_max_retries_marks_needs_human(
        self, conn: sqlite3.Connection, settings: Settings, project_id: str,
    ) -> None:
        """Exceeding max retries sets the task to needs_human."""
        task_id = db.insert_task(
            conn, project_id=project_id, title="Max retry test",
            priority=5, max_retries=1,
        )
        db.update_task(conn, task_id, status="active", current_stage="spec")
        db.insert_stage_run(
            conn, task_id=task_id, stage="spec", attempt=1, status="queued",
        )
        db.update_task(conn, task_id, branch_name="forge/test-needs-human")

        safe_conn = _UnclosableConnection(conn)
        engine = PipelineEngine(settings, ":memory:")

        async def always_fail_gate(gate_dir, stage, env_vars):
            return _failing_gate(stage, "always fails")

        dispatch_count = 0

        async def counting_dispatch(prompt, repo_path, branch, timeout, **kwargs):
            nonlocal dispatch_count
            dispatch_count += 1
            if dispatch_count >= 3:
                engine.running = False
            return _dispatch_ok()

        with (
            patch("forge.engine.database.get_connection", return_value=safe_conn),
            patch("forge.engine.dispatch_claude", side_effect=counting_dispatch),
            patch("forge.engine.run_gate", side_effect=always_fail_gate),
            patch("forge.engine.build_prompt", return_value="prompt"),
            patch("forge.engine.create_branch", new_callable=AsyncMock, return_value=True),
        ):
            await _run_engine_iterations(engine, max_seconds=4.0)

        task = db.get_task(conn, task_id)
        assert task["status"] == "needs_human"


# ---------------------------------------------------------------------------
# Engine pause / resume
# ---------------------------------------------------------------------------


class TestEnginePauseResume:
    @pytest.mark.asyncio
    async def test_pause_stops_processing(self, settings: Settings) -> None:
        """Engine does not process tasks when paused."""
        engine = PipelineEngine(settings, ":memory:")
        assert engine.running is False

        # run_loop exits immediately when running=False
        await asyncio.wait_for(engine.run_loop(), timeout=1.0)

    @pytest.mark.asyncio
    async def test_start_pause_cycle(self, settings: Settings) -> None:
        """Engine can be started and paused."""
        engine = PipelineEngine(settings, ":memory:")

        with patch("forge.engine.database.get_connection") as mock_conn:
            c = db.get_connection(":memory:")
            db.migrate(c)
            mock_conn.return_value = _UnclosableConnection(c)

            await engine.start()
            assert engine.running is True
            assert engine._loop_task is not None

            await engine.pause()
            assert engine.running is False


# ---------------------------------------------------------------------------
# API endpoints during pipeline execution
# ---------------------------------------------------------------------------


class TestAPIDuringPipeline:
    def test_engine_status_endpoint(self, client: TestClient) -> None:
        resp = client.get("/api/engine/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "running" in data
        assert "queue_depth" in data

    def test_engine_stats_endpoint(self, client: TestClient) -> None:
        resp = client.get("/api/engine/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_tasks" in data
        assert "total_stage_runs" in data

    def test_create_project_and_task_via_api(self, client: TestClient, tmp_path) -> None:
        """Create a project and task via API, verify they appear in lists."""
        resp = client.post("/api/projects", json={
            "name": "APITestProject",
            "repo_path": str(tmp_path),
        })
        assert resp.status_code == 201
        project_id = resp.json()["id"]

        resp = client.post("/api/tasks", json={
            "project_id": project_id,
            "title": "API integration task",
            "priority": 5,
        })
        assert resp.status_code == 201
        task_id = resp.json()["id"]
        assert resp.json()["status"] == "backlog"

        # Task appears in list
        resp = client.get(f"/api/tasks?project_id={project_id}")
        assert resp.status_code == 200
        assert len(resp.json()) == 1
        assert resp.json()[0]["id"] == task_id

        # Task detail
        resp = client.get(f"/api/tasks/{task_id}")
        assert resp.status_code == 200
        assert resp.json()["title"] == "API integration task"

    def test_stage_runs_endpoint(self, client: TestClient) -> None:
        resp = client.get("/api/stage-runs")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_logs_endpoint(self, client: TestClient) -> None:
        resp = client.get("/api/logs")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)


# ---------------------------------------------------------------------------
# Self-registration: Forge registers itself as a project
# ---------------------------------------------------------------------------


class TestSelfRegistration:
    def test_migrate_works(self, tmp_path, monkeypatch) -> None:
        """python -m forge migrate creates the schema."""
        from forge.cli import main as cli_main

        db_path = tmp_path / "self_reg.db"
        monkeypatch.setattr("forge.cli.DB_PATH", db_path)

        cli_main(["migrate"])

        conn = db.get_connection(str(db_path))
        try:
            # Tables should exist
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
            )
            tables = {r[0] for r in cur.fetchall()}
            assert "projects" in tables
            assert "tasks" in tables
            assert "stage_runs" in tables
        finally:
            conn.close()

    def test_init_project_registers_forge(self, tmp_path, monkeypatch) -> None:
        """python -m forge init-project --name Forge ... registers successfully."""
        from forge.cli import main as cli_main

        db_path = tmp_path / "self_reg.db"
        monkeypatch.setattr("forge.cli.DB_PATH", db_path)

        repo_path = str(tmp_path)
        cli_main([
            "init-project",
            "--name", "Forge",
            "--repo-path", repo_path,
            "--default-branch", "main",
            "--gate-dir", "gates",
        ])

        # Verify project exists in DB
        conn = db.get_connection(str(db_path))
        try:
            project = db.get_project_by_name(conn, "Forge")
            assert project is not None
            assert project["name"] == "Forge"
            assert project["repo_path"] == repo_path
            assert project["default_branch"] == "main"
            assert project["gate_dir"] == "gates"
        finally:
            conn.close()

    def test_forge_appears_in_list_projects(self, tmp_path, monkeypatch, capsys) -> None:
        """After init-project, Forge appears in list-projects output."""
        from forge.cli import main as cli_main

        db_path = tmp_path / "self_reg.db"
        monkeypatch.setattr("forge.cli.DB_PATH", db_path)

        cli_main(["migrate"])
        cli_main([
            "init-project",
            "--name", "Forge",
            "--repo-path", str(tmp_path),
            "--default-branch", "main",
            "--gate-dir", "gates",
        ])

        cli_main(["list-projects"])
        captured = capsys.readouterr()
        assert "Forge" in captured.out
        assert "main" in captured.out

    def test_forge_appears_in_dashboard(self, client: TestClient, tmp_path) -> None:
        """After registering via API, the project appears in the dashboard."""
        # Register via API
        resp = client.post("/api/projects", json={
            "name": "Forge",
            "repo_path": str(tmp_path),
            "default_branch": "main",
            "gate_dir": "gates",
        })
        assert resp.status_code == 201

        # Check API listing
        resp = client.get("/api/projects")
        assert resp.status_code == 200
        names = [p["name"] for p in resp.json()]
        assert "Forge" in names

        # Check dashboard renders
        resp = client.get("/")
        assert resp.status_code == 200
