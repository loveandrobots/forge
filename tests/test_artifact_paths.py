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
        assert path == "/tmp/repo/_forge/specs/task-123.json"

    def test_plan_standard(self) -> None:
        path = _artifact_path_for_stage("/tmp/repo", "task-123", "plan")
        assert path == "/tmp/repo/_forge/plans/task-123.json"

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
        assert task["spec_path"] == f"/tmp/repo/_forge/specs/{task_id}.json"
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
        assert task["plan_path"] == f"/tmp/repo/_forge/plans/{task_id}.json"
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
        assert task["spec_path"] == f"/tmp/repo/_forge/specs/{task_id}.json"
        assert task["plan_path"] == f"/tmp/repo/_forge/plans/{task_id}.json"
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

        # spec_path is None on the task — should fall back to conventional .json path
        assert task["spec_path"] is None

        expected_json_path = f"/tmp/repo/_forge/specs/{task_id}.json"
        structured_data = {
            "overview": "Test",
            "acceptance_criteria": [{"id": 1, "text": "AC1"}],
        }
        with (
            patch("forge.engine.os.path.exists", return_value=True),
            patch(
                "forge.engine.load_structured_artifact",
                return_value=structured_data,
            ) as mock_structured,
        ):
            artifacts = engine._load_artifacts(task, project, "plan", stage_run, conn)

        mock_structured.assert_called_once_with(expected_json_path)
        import json
        assert artifacts["spec_content"] == json.dumps(structured_data, indent=2)

    def test_implement_stage_falls_back_to_conventional_paths(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """When spec_path and plan_path are null, implement stage uses conventional .json paths."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(conn, project_id=project_id, title="T", priority=1)
        db.update_task(conn, task_id, status="active", current_stage="implement")
        sr_id = db.insert_stage_run(conn, task_id=task_id, stage="implement", attempt=1)

        task = dict(db.get_task(conn, task_id))
        project = dict(db.get_project(conn, project_id))
        stage_run = dict(db.get_stage_run(conn, sr_id))

        assert task["spec_path"] is None
        assert task["plan_path"] is None

        expected_spec = f"/tmp/repo/_forge/specs/{task_id}.json"
        expected_plan = f"/tmp/repo/_forge/plans/{task_id}.json"

        spec_data = {"overview": "Test", "acceptance_criteria": [{"id": 1, "text": "AC1"}]}
        plan_data = {"approach": "Do it", "files_to_modify": [], "test_plan": []}

        def fake_structured(path: str) -> dict | None:
            if path == expected_spec:
                return spec_data
            if path == expected_plan:
                return plan_data
            return None

        with (
            patch("forge.engine.os.path.exists", return_value=True),
            patch("forge.engine.load_structured_artifact", side_effect=fake_structured),
        ):
            artifacts = engine._load_artifacts(
                task, project, "implement", stage_run, conn
            )

        import json
        assert artifacts["spec_content"] == json.dumps(spec_data, indent=2)
        assert artifacts["plan_content"] == json.dumps(plan_data, indent=2)

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

        expected_spec = f"/tmp/repo/_forge/specs/{task_id}.json"
        structured_data = {"overview": "Test", "acceptance_criteria": []}

        with (
            patch("forge.engine.os.path.exists", return_value=True),
            patch(
                "forge.engine.load_structured_artifact",
                return_value=structured_data,
            ) as mock_structured,
            patch("forge.engine.get_git_diff", return_value=""),
        ):
            artifacts = engine._load_artifacts(task, project, "review", stage_run, conn)

        mock_structured.assert_called_once_with(expected_spec)
        import json
        assert artifacts["spec_content"] == json.dumps(structured_data, indent=2)

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


# ---------------------------------------------------------------------------
# _load_artifacts JSON artifact handling
# ---------------------------------------------------------------------------


class TestLoadArtifactsJsonHandling:
    """Verify that _load_artifacts uses load_structured_artifact for .json paths."""

    def test_json_spec_path_parsed_as_structured(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """When spec_path ends in .json, _load_artifacts parses it as JSON."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(conn, project_id=project_id, title="T", priority=1)
        db.update_task(
            conn,
            task_id,
            status="active",
            current_stage="plan",
            spec_path="/tmp/repo/_forge/specs/test.json",
        )
        sr_id = db.insert_stage_run(conn, task_id=task_id, stage="plan", attempt=1)

        task = dict(db.get_task(conn, task_id))
        project = dict(db.get_project(conn, project_id))
        stage_run = dict(db.get_stage_run(conn, sr_id))

        structured_data = {"title": "My spec", "description": "Details"}
        with (
            patch("forge.engine.os.path.exists", return_value=True),
            patch(
                "forge.engine.load_structured_artifact",
                return_value=structured_data,
            ) as mock_structured,
            patch("forge.engine.load_artifact") as mock_load,
        ):
            artifacts = engine._load_artifacts(task, project, "plan", stage_run, conn)

        mock_structured.assert_called_once_with("/tmp/repo/_forge/specs/test.json")
        mock_load.assert_not_called()
        import json
        assert artifacts["spec_content"] == json.dumps(structured_data, indent=2)

    def test_json_plan_path_parsed_as_structured(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """When plan_path ends in .json, _load_artifacts parses it as JSON."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(conn, project_id=project_id, title="T", priority=1)
        db.update_task(
            conn,
            task_id,
            status="active",
            current_stage="implement",
            spec_path="/tmp/repo/_forge/specs/test.md",
            plan_path="/tmp/repo/_forge/plans/test.json",
        )
        sr_id = db.insert_stage_run(
            conn, task_id=task_id, stage="implement", attempt=1
        )

        task = dict(db.get_task(conn, task_id))
        project = dict(db.get_project(conn, project_id))
        stage_run = dict(db.get_stage_run(conn, sr_id))

        plan_data = {"steps": ["step1", "step2"]}
        with (
            patch("forge.engine.os.path.exists", return_value=True),
            patch(
                "forge.engine.load_structured_artifact",
                return_value=plan_data,
            ) as mock_structured,
            patch("forge.engine.load_artifact", return_value="spec content"),
        ):
            artifacts = engine._load_artifacts(
                task, project, "implement", stage_run, conn
            )

        mock_structured.assert_called_once_with("/tmp/repo/_forge/plans/test.json")
        import json
        assert artifacts["plan_content"] == json.dumps(plan_data, indent=2)
        assert artifacts["spec_content"] == "spec content"

    def test_md_paths_still_use_load_artifact(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """Non-.json paths still use load_artifact as before."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(conn, project_id=project_id, title="T", priority=1)
        db.update_task(
            conn,
            task_id,
            status="active",
            current_stage="plan",
            spec_path="/tmp/repo/_forge/specs/test.md",
        )
        sr_id = db.insert_stage_run(conn, task_id=task_id, stage="plan", attempt=1)

        task = dict(db.get_task(conn, task_id))
        project = dict(db.get_project(conn, project_id))
        stage_run = dict(db.get_stage_run(conn, sr_id))

        with (
            patch("forge.engine.os.path.exists", return_value=True),
            patch(
                "forge.engine.load_artifact", return_value="spec content"
            ) as mock_load,
            patch("forge.engine.load_structured_artifact") as mock_structured,
        ):
            artifacts = engine._load_artifacts(task, project, "plan", stage_run, conn)

        mock_load.assert_called_once_with("/tmp/repo/_forge/specs/test.md")
        mock_structured.assert_not_called()
        assert artifacts["spec_content"] == "spec content"


# ---------------------------------------------------------------------------
# _load_artifacts flow gating
# ---------------------------------------------------------------------------


class TestLoadArtifactsFlowGating:
    """Verify that _load_artifacts gates spec/plan loading on the task's flow."""

    def test_quick_flow_implement_skips_spec_and_plan(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """Quick flow implement does not attempt to load spec or plan files."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn, project_id=project_id, title="T", priority=1, flow="quick"
        )
        db.update_task(conn, task_id, status="active", current_stage="implement")
        sr_id = db.insert_stage_run(conn, task_id=task_id, stage="implement", attempt=1)

        task = dict(db.get_task(conn, task_id))
        project = dict(db.get_project(conn, project_id))
        stage_run = dict(db.get_stage_run(conn, sr_id))

        # No mocks for os.path.exists or load_artifact — they should never be
        # called for spec/plan on a quick flow task.
        artifacts = engine._load_artifacts(task, project, "implement", stage_run, conn)

        assert "spec_content" not in artifacts
        assert "plan_content" not in artifacts

    def test_quick_flow_review_skips_spec(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """Quick flow review does not attempt to load spec file."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn, project_id=project_id, title="T", priority=1, flow="quick"
        )
        db.update_task(
            conn,
            task_id,
            status="active",
            current_stage="review",
            branch_name="feature/quick-task",
        )
        sr_id = db.insert_stage_run(conn, task_id=task_id, stage="review", attempt=1)

        task = dict(db.get_task(conn, task_id))
        project = dict(db.get_project(conn, project_id))
        stage_run = dict(db.get_stage_run(conn, sr_id))

        with patch("forge.engine.get_git_diff", return_value="diff output"):
            artifacts = engine._load_artifacts(
                task, project, "review", stage_run, conn
            )

        assert "spec_content" not in artifacts
        assert artifacts["git_diff"] == "diff output"

    def test_standard_flow_implement_raises_when_spec_missing(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """Standard flow implement raises RuntimeError when spec file is missing."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn, project_id=project_id, title="T", priority=1, flow="standard"
        )
        db.update_task(conn, task_id, status="active", current_stage="implement")
        sr_id = db.insert_stage_run(
            conn, task_id=task_id, stage="implement", attempt=1
        )

        task = dict(db.get_task(conn, task_id))
        project = dict(db.get_project(conn, project_id))
        stage_run = dict(db.get_stage_run(conn, sr_id))

        with pytest.raises(RuntimeError, match="Spec file not found"):
            engine._load_artifacts(task, project, "implement", stage_run, conn)

    def test_standard_flow_implement_raises_when_plan_missing(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
        tmp_path,
    ) -> None:
        """Standard flow implement raises RuntimeError when spec exists but plan is missing."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn, project_id=project_id, title="T", priority=1, flow="standard"
        )
        db.update_task(conn, task_id, status="active", current_stage="implement")
        sr_id = db.insert_stage_run(
            conn, task_id=task_id, stage="implement", attempt=1
        )

        # Write a spec file so spec loading succeeds
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("spec content")
        db.update_task(conn, task_id, spec_path=str(spec_file))

        task = dict(db.get_task(conn, task_id))
        project = dict(db.get_project(conn, project_id))
        stage_run = dict(db.get_stage_run(conn, sr_id))

        with pytest.raises(RuntimeError, match="Plan file not found"):
            engine._load_artifacts(task, project, "implement", stage_run, conn)

    def test_standard_flow_review_raises_when_spec_missing(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """Standard flow review raises RuntimeError when spec file is missing."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(
            conn, project_id=project_id, title="T", priority=1, flow="standard"
        )
        db.update_task(conn, task_id, status="active", current_stage="review")
        sr_id = db.insert_stage_run(conn, task_id=task_id, stage="review", attempt=1)

        task = dict(db.get_task(conn, task_id))
        project = dict(db.get_project(conn, project_id))
        stage_run = dict(db.get_stage_run(conn, sr_id))

        with pytest.raises(RuntimeError, match="Spec file not found"):
            engine._load_artifacts(task, project, "review", stage_run, conn)


# ---------------------------------------------------------------------------
# _load_artifacts legacy .md fallback
# ---------------------------------------------------------------------------


class TestLoadArtifactsLegacyFallback:
    """Verify _load_artifacts falls back from .json to .md for legacy artifacts."""

    def test_load_artifacts_prefers_json_spec(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
        tmp_path,
    ) -> None:
        """When both .json and .md exist, prefers .json."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(conn, project_id=project_id, title="T", priority=1)
        json_path = str(tmp_path / f"_forge/specs/{task_id}.json")
        md_path = str(tmp_path / f"_forge/specs/{task_id}.md")
        import json
        import os
        os.makedirs(os.path.dirname(json_path), exist_ok=True)
        with open(json_path, "w") as f:
            json.dump({"overview": "JSON spec", "acceptance_criteria": []}, f)
        with open(md_path, "w") as f:
            f.write("# Markdown spec")
        db.update_task(
            conn, task_id, status="active", current_stage="plan",
            spec_path=json_path,
        )
        sr_id = db.insert_stage_run(conn, task_id=task_id, stage="plan", attempt=1)
        task = dict(db.get_task(conn, task_id))
        project = dict(db.get_project(conn, project_id))
        project["repo_path"] = str(tmp_path)
        stage_run = dict(db.get_stage_run(conn, sr_id))

        artifacts = engine._load_artifacts(task, project, "plan", stage_run, conn)
        assert "JSON spec" in artifacts["spec_content"]

    def test_load_artifacts_falls_back_to_md_spec(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
        tmp_path,
    ) -> None:
        """When only .md exists (legacy), loads it successfully."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(conn, project_id=project_id, title="T", priority=1)
        md_path = str(tmp_path / f"_forge/specs/{task_id}.md")
        import os
        os.makedirs(os.path.dirname(md_path), exist_ok=True)
        with open(md_path, "w") as f:
            f.write("# Legacy spec\n## Acceptance criteria\n- AC1")
        # Don't set spec_path — it will use conventional path which is .json,
        # then fall back to .md
        db.update_task(conn, task_id, status="active", current_stage="plan")
        sr_id = db.insert_stage_run(conn, task_id=task_id, stage="plan", attempt=1)
        task = dict(db.get_task(conn, task_id))
        project = dict(db.get_project(conn, project_id))
        project["repo_path"] = str(tmp_path)
        stage_run = dict(db.get_stage_run(conn, sr_id))

        artifacts = engine._load_artifacts(task, project, "plan", stage_run, conn)
        assert "Legacy spec" in artifacts["spec_content"]

    def test_load_artifacts_falls_back_to_md_plan(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
        tmp_path,
    ) -> None:
        """When only .md plan exists (legacy), loads it successfully."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(conn, project_id=project_id, title="T", priority=1)
        # Create spec as .md
        spec_md = str(tmp_path / f"_forge/specs/{task_id}.md")
        import os
        os.makedirs(os.path.dirname(spec_md), exist_ok=True)
        with open(spec_md, "w") as f:
            f.write("# Spec content")
        plan_md = str(tmp_path / f"_forge/plans/{task_id}.md")
        os.makedirs(os.path.dirname(plan_md), exist_ok=True)
        with open(plan_md, "w") as f:
            f.write("# Legacy plan\n## Approach")
        db.update_task(
            conn, task_id, status="active", current_stage="implement",
            spec_path=spec_md,
        )
        sr_id = db.insert_stage_run(conn, task_id=task_id, stage="implement", attempt=1)
        task = dict(db.get_task(conn, task_id))
        project = dict(db.get_project(conn, project_id))
        project["repo_path"] = str(tmp_path)
        stage_run = dict(db.get_stage_run(conn, sr_id))

        artifacts = engine._load_artifacts(task, project, "implement", stage_run, conn)
        assert "Legacy plan" in artifacts["plan_content"]

    def test_review_feedback_falls_back_to_conventional_review_path(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """When review_path is null, review feedback loading uses _artifact_path_for_stage."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(conn, project_id=project_id, title="T", priority=1)
        db.update_task(
            conn,
            task_id,
            status="active",
            current_stage="implement",
            spec_path="/tmp/repo/_forge/specs/test.md",
            plan_path="/tmp/repo/_forge/plans/test.md",
        )
        sr_id = db.insert_stage_run(conn, task_id=task_id, stage="implement", attempt=1)
        # Insert a bounced review run so the feedback loading block is triggered
        db.insert_stage_run(
            conn, task_id=task_id, stage="review", attempt=1, status="bounced"
        )

        task = dict(db.get_task(conn, task_id))
        project = dict(db.get_project(conn, project_id))
        stage_run = dict(db.get_stage_run(conn, sr_id))

        assert task["review_path"] is None

        expected_review_path = f"/tmp/repo/_forge/reviews/{task_id}.md"

        with (
            patch("forge.engine.os.path.exists", return_value=True),
            patch("forge.engine.load_artifact") as mock_load,
        ):
            mock_load.return_value = "review feedback content"
            artifacts = engine._load_artifacts(
                task, project, "implement", stage_run, conn
            )

        # Verify load_artifact was called with the conventional path from
        # _artifact_path_for_stage, not a hardcoded os.path.join
        assert any(
            call.args == (expected_review_path,) for call in mock_load.call_args_list
        ), f"Expected load_artifact to be called with {expected_review_path}"
        assert artifacts["review_feedback"] == "review feedback content"

    def test_review_feedback_uses_explicit_review_path(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
    ) -> None:
        """When review_path is set on the task, review feedback uses it directly."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(conn, project_id=project_id, title="T", priority=1)
        custom_review = "/custom/path/review.md"
        db.update_task(
            conn,
            task_id,
            status="active",
            current_stage="implement",
            spec_path="/tmp/repo/_forge/specs/test.md",
            plan_path="/tmp/repo/_forge/plans/test.md",
            review_path=custom_review,
        )
        sr_id = db.insert_stage_run(conn, task_id=task_id, stage="implement", attempt=1)
        db.insert_stage_run(
            conn, task_id=task_id, stage="review", attempt=1, status="bounced"
        )

        task = dict(db.get_task(conn, task_id))
        project = dict(db.get_project(conn, project_id))
        stage_run = dict(db.get_stage_run(conn, sr_id))

        with (
            patch("forge.engine.os.path.exists", return_value=True),
            patch("forge.engine.load_artifact") as mock_load,
        ):
            mock_load.return_value = "custom review feedback"
            artifacts = engine._load_artifacts(
                task, project, "implement", stage_run, conn
            )

        assert any(
            call.args == (custom_review,) for call in mock_load.call_args_list
        ), f"Expected load_artifact to be called with {custom_review}"
        assert artifacts["review_feedback"] == "custom review feedback"

    def test_load_artifacts_formats_spec_criteria_for_plan_stage(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        project_id: str,
        tmp_path,
    ) -> None:
        """When .json spec is loaded for plan stage, spec_criteria_list is populated."""
        engine = PipelineEngine(settings, ":memory:")
        task_id = db.insert_task(conn, project_id=project_id, title="T", priority=1)
        import json as json_mod
        import os
        json_path = str(tmp_path / f"_forge/specs/{task_id}.json")
        os.makedirs(os.path.dirname(json_path), exist_ok=True)
        spec_data = {
            "overview": "Test",
            "acceptance_criteria": [
                {"id": 1, "text": "First criterion"},
                {"id": 2, "text": "Second criterion"},
            ],
        }
        with open(json_path, "w") as f:
            json_mod.dump(spec_data, f)
        db.update_task(
            conn, task_id, status="active", current_stage="plan",
            spec_path=json_path,
        )
        sr_id = db.insert_stage_run(conn, task_id=task_id, stage="plan", attempt=1)
        task = dict(db.get_task(conn, task_id))
        project = dict(db.get_project(conn, project_id))
        project["repo_path"] = str(tmp_path)
        stage_run = dict(db.get_stage_run(conn, sr_id))

        artifacts = engine._load_artifacts(task, project, "plan", stage_run, conn)
        assert "spec_criteria_list" in artifacts
        assert "1. First criterion" in artifacts["spec_criteria_list"]
        assert "2. Second criterion" in artifacts["spec_criteria_list"]
