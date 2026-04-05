"""Tests for artifact path setting on task records after stage completion."""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest

from forge import database as db
from forge.config import Settings
from forge.engine import (
    PipelineEngine,
    _artifact_path_for_stage,
)

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
    async def _noop_reset(repo_path: str, default_branch: str) -> dict:
        return {"success": True, "output": "mocked reset"}

    monkeypatch.setattr("forge.engine.reset_repo_state", _noop_reset)


@pytest.fixture
def conn() -> sqlite3.Connection:
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


# ---------------------------------------------------------------------------
# Unit tests: _artifact_path_for_stage
# ---------------------------------------------------------------------------


class TestArtifactPathForStage:
    def test_spec_standard(self) -> None:
        path = _artifact_path_for_stage("/tmp/repo", "task-123", "spec")
        assert path == "/tmp/repo/_forge/specs/task-123.md"

    def test_plan_standard(self) -> None:
        path = _artifact_path_for_stage("/tmp/repo", "task-123", "plan")
        assert path == "/tmp/repo/_forge/plans/task-123.md"

    def test_review_standard(self) -> None:
        path = _artifact_path_for_stage("/tmp/repo", "task-123", "review")
        assert path == "/tmp/repo/_forge/reviews/task-123.md"

    def test_implement_returns_none(self) -> None:
        path = _artifact_path_for_stage("/tmp/repo", "task-123", "implement")
        assert path is None

    def test_spec_epic_returns_decomposition_json(self) -> None:
        path = _artifact_path_for_stage("/tmp/repo", "task-123", "spec", flow="epic")
        assert path == "/tmp/repo/_forge/epic-decompositions/task-123.json"

    def test_review_epic_returns_review_md(self) -> None:
        path = _artifact_path_for_stage("/tmp/repo", "task-123", "review", flow="epic")
        assert path == "/tmp/repo/_forge/reviews/task-123.md"


# ---------------------------------------------------------------------------
# advance_task sets artifact paths
# ---------------------------------------------------------------------------


class TestAdvanceTaskSetsArtifactPaths:
    async def test_spec_pass_sets_spec_path(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(conn, project_id=project_id, title="T", priority=1)
        db.update_task(conn, task_id, status="active", current_stage="spec")
        project = dict(db.get_project(conn, project_id))

        await engine.advance_task(conn, task_id, "spec", project=project)

        task = db.get_task(conn, task_id)
        assert task["spec_path"] == f"/tmp/repo/_forge/specs/{task_id}.md"
        assert task["current_stage"] == "plan"

    async def test_plan_pass_sets_plan_path(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(conn, project_id=project_id, title="T", priority=1)
        db.update_task(conn, task_id, status="active", current_stage="plan")
        project = dict(db.get_project(conn, project_id))

        await engine.advance_task(conn, task_id, "plan", project=project)

        task = db.get_task(conn, task_id)
        assert task["plan_path"] == f"/tmp/repo/_forge/plans/{task_id}.md"
        assert task["current_stage"] == "implement"

    async def test_review_pass_sets_review_path(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(conn, project_id=project_id, title="T", priority=1)
        db.update_task(conn, task_id, status="active", current_stage="review")
        project = dict(db.get_project(conn, project_id))

        await engine.advance_task(conn, task_id, "review", project=project)

        task = db.get_task(conn, task_id)
        assert task["review_path"] == f"/tmp/repo/_forge/reviews/{task_id}.md"
        assert task["status"] == "done"

    async def test_epic_spec_pass_sets_spec_path_to_decomposition(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn,
            project_id=project_id,
            title="Epic",
            priority=1,
            flow="epic",
            epic_status="pending",
        )
        db.update_task(conn, task_id, status="active", current_stage="spec")
        project = dict(db.get_project(conn, project_id))

        # Epic decomposition reads _forge/epic-decompositions/{task_id}.json
        decomp_path = f"/tmp/repo/_forge/epic-decompositions/{task_id}.json"
        decomp_content = (
            '{"tasks": [{"title": "Sub", "description": "d", "priority": 1}]}'
        )

        with patch("forge.engine.load_artifact", return_value=decomp_content):
            await engine.advance_task(conn, task_id, "spec", project=project)

        task = db.get_task(conn, task_id)
        assert task["spec_path"] == decomp_path

    async def test_review_pass_sets_review_path_when_auto_merge_fails(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """review_path is persisted even when _auto_merge fails and returns early."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(conn, project_id=project_id, title="T", priority=1)
        db.update_task(
            conn,
            task_id,
            status="active",
            current_stage="review",
            branch_name="feature/test-branch",
        )
        project = dict(db.get_project(conn, project_id))

        with patch.object(engine, "_auto_merge", return_value=False):
            await engine.advance_task(conn, task_id, "review", project=project)

        task = db.get_task(conn, task_id)
        assert task["review_path"] == f"/tmp/repo/_forge/reviews/{task_id}.md"
        # Task should NOT be "done" since merge failed
        assert task["status"] != "done"

    async def test_no_project_does_not_set_artifact_path(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(conn, project_id=project_id, title="T", priority=1)
        db.update_task(conn, task_id, status="active", current_stage="spec")

        await engine.advance_task(conn, task_id, "spec", project=None)

        task = db.get_task(conn, task_id)
        assert task["spec_path"] is None

    async def test_all_paths_set_after_full_pipeline(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """Advancing through spec→plan→implement→review sets all three artifact paths."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(conn, project_id=project_id, title="T", priority=1)
        db.update_task(conn, task_id, status="active", current_stage="spec")
        project = dict(db.get_project(conn, project_id))

        await engine.advance_task(conn, task_id, "spec", project=project)
        await engine.advance_task(conn, task_id, "plan", project=project)
        await engine.advance_task(conn, task_id, "implement", project=project)
        await engine.advance_task(conn, task_id, "review", project=project)

        task = db.get_task(conn, task_id)
        assert task["spec_path"] == f"/tmp/repo/_forge/specs/{task_id}.md"
        assert task["plan_path"] == f"/tmp/repo/_forge/plans/{task_id}.md"
        assert task["review_path"] == f"/tmp/repo/_forge/reviews/{task_id}.md"
        assert task["status"] == "done"


# ---------------------------------------------------------------------------
# _load_artifacts fallback to conventional paths
# ---------------------------------------------------------------------------


class TestLoadArtifactsFallback:
    def test_plan_stage_falls_back_to_conventional_spec_path(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """When spec_path is null on the task, _load_artifacts uses the conventional path."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(conn, project_id=project_id, title="T", priority=1)
        db.update_task(conn, task_id, status="active", current_stage="plan")
        sr_id = db.insert_stage_run(conn, task_id=task_id, stage="plan", attempt=1)

        task = dict(db.get_task(conn, task_id))
        project = dict(db.get_project(conn, project_id))
        stage_run = dict(db.get_stage_run(conn, sr_id))

        # spec_path is None on the task — should fall back to conventional path
        assert task["spec_path"] is None

        expected_path = f"/tmp/repo/_forge/specs/{task_id}.md"
        with (
            patch("forge.engine.os.path.exists", return_value=True),
            patch(
                "forge.engine.load_artifact", return_value="spec content"
            ) as mock_load,
        ):
            artifacts = engine._load_artifacts(task, project, "plan", stage_run, conn)

        # Should have called load_artifact with the conventional fallback path
        mock_load.assert_any_call(expected_path)
        assert artifacts["spec_content"] == "spec content"

    def test_implement_stage_falls_back_to_conventional_paths(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """When spec_path and plan_path are null, implement stage uses conventional paths."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(conn, project_id=project_id, title="T", priority=1)
        db.update_task(conn, task_id, status="active", current_stage="implement")
        sr_id = db.insert_stage_run(conn, task_id=task_id, stage="implement", attempt=1)

        task = dict(db.get_task(conn, task_id))
        project = dict(db.get_project(conn, project_id))
        stage_run = dict(db.get_stage_run(conn, sr_id))

        assert task["spec_path"] is None
        assert task["plan_path"] is None

        expected_spec = f"/tmp/repo/_forge/specs/{task_id}.md"
        expected_plan = f"/tmp/repo/_forge/plans/{task_id}.md"

        def fake_load(path: str) -> str:
            if path == expected_spec:
                return "spec content"
            if path == expected_plan:
                return "plan content"
            return ""

        with (
            patch("forge.engine.os.path.exists", return_value=True),
            patch("forge.engine.load_artifact", side_effect=fake_load),
        ):
            artifacts = engine._load_artifacts(
                task, project, "implement", stage_run, conn
            )

        assert artifacts["spec_content"] == "spec content"
        assert artifacts["plan_content"] == "plan content"

    def test_uses_explicit_path_when_set(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """When spec_path is set on the task, _load_artifacts uses it directly."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(conn, project_id=project_id, title="T", priority=1)
        explicit_spec = "/custom/path/spec.md"
        db.update_task(
            conn,
            task_id,
            status="active",
            current_stage="plan",
            spec_path=explicit_spec,
        )
        sr_id = db.insert_stage_run(conn, task_id=task_id, stage="plan", attempt=1)

        task = dict(db.get_task(conn, task_id))
        project = dict(db.get_project(conn, project_id))
        stage_run = dict(db.get_stage_run(conn, sr_id))

        with (
            patch("forge.engine.os.path.exists", return_value=True),
            patch(
                "forge.engine.load_artifact", return_value="custom spec"
            ) as mock_load,
        ):
            artifacts = engine._load_artifacts(task, project, "plan", stage_run, conn)

        mock_load.assert_any_call(explicit_spec)
        assert artifacts["spec_content"] == "custom spec"

    def test_review_stage_falls_back_to_conventional_spec_path(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """Review stage also loads spec with fallback when spec_path is null."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(conn, project_id=project_id, title="T", priority=1)
        db.update_task(conn, task_id, status="active", current_stage="review")
        sr_id = db.insert_stage_run(conn, task_id=task_id, stage="review", attempt=1)

        task = dict(db.get_task(conn, task_id))
        project = dict(db.get_project(conn, project_id))
        stage_run = dict(db.get_stage_run(conn, sr_id))

        expected_spec = f"/tmp/repo/_forge/specs/{task_id}.md"

        with (
            patch("forge.engine.os.path.exists", return_value=True),
            patch(
                "forge.engine.load_artifact", return_value="spec content"
            ) as mock_load,
            patch("forge.engine.get_git_diff", return_value=""),
        ):
            artifacts = engine._load_artifacts(task, project, "review", stage_run, conn)

        mock_load.assert_any_call(expected_spec)
        assert artifacts["spec_content"] == "spec content"

    def test_raises_when_spec_file_missing(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """_load_artifacts raises RuntimeError when the spec file does not exist on disk."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(conn, project_id=project_id, title="T", priority=1)
        # Point spec_path at a path that genuinely does not exist
        missing_spec = "/tmp/nonexistent_forge_test_dir/specs/no-such-file.md"
        db.update_task(
            conn,
            task_id,
            status="active",
            current_stage="plan",
            spec_path=missing_spec,
        )
        sr_id = db.insert_stage_run(conn, task_id=task_id, stage="plan", attempt=1)

        task = dict(db.get_task(conn, task_id))
        project = dict(db.get_project(conn, project_id))
        stage_run = dict(db.get_stage_run(conn, sr_id))

        with pytest.raises(RuntimeError, match="Spec file not found"):
            engine._load_artifacts(task, project, "plan", stage_run, conn)
