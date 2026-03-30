from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from forge.models import (
    EngineStatus,
    PipelineStats,
    ProjectCreate,
    ProjectResponse,
    ProjectUpdate,
    RunLogEntry,
    StageRunResponse,
    TaskCreate,
    TaskResponse,
    TaskUpdate,
)


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


class TestProjectCreate:
    def test_minimal(self) -> None:
        p = ProjectCreate(name="Forge", repo_path="/home/user/forge")
        assert p.name == "Forge"
        assert p.default_branch == "main"
        assert p.gate_dir == "gates"
        assert p.skill_refs is None

    def test_full(self) -> None:
        p = ProjectCreate(
            name="Olivia",
            repo_path="/home/user/olivia",
            default_branch="develop",
            gate_dir="ci/gates",
            skill_refs=["skill-a", "skill-b"],
            config={"key": "value"},
        )
        assert p.skill_refs == ["skill-a", "skill-b"]
        assert p.config == {"key": "value"}

    def test_missing_required(self) -> None:
        with pytest.raises(ValidationError):
            ProjectCreate(repo_path="/path")  # type: ignore[call-arg]
        with pytest.raises(ValidationError):
            ProjectCreate(name="X")  # type: ignore[call-arg]


class TestProjectUpdate:
    def test_all_optional(self) -> None:
        p = ProjectUpdate()
        assert p.name is None
        assert p.repo_path is None

    def test_partial(self) -> None:
        p = ProjectUpdate(name="New Name")
        assert p.name == "New Name"
        assert p.gate_dir is None


class TestProjectResponse:
    def test_valid(self) -> None:
        now = datetime.now()
        p = ProjectResponse(
            id="abc-123",
            name="Forge",
            repo_path="/path",
            default_branch="main",
            gate_dir="gates",
            created_at=now,
        )
        assert p.id == "abc-123"
        assert p.created_at == now


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


class TestTaskCreate:
    def test_minimal(self) -> None:
        t = TaskCreate(project_id="p1", title="Do thing")
        assert t.description == ""
        assert t.priority == 0
        assert t.max_retries == 3

    def test_missing_required(self) -> None:
        with pytest.raises(ValidationError):
            TaskCreate(title="X")  # type: ignore[call-arg]


class TestTaskUpdate:
    def test_all_optional(self) -> None:
        t = TaskUpdate()
        assert t.title is None
        assert t.status is None


class TestTaskResponse:
    def test_valid(self) -> None:
        now = datetime.now()
        t = TaskResponse(
            id="t1",
            project_id="p1",
            title="Task",
            description="desc",
            priority=5,
            status="active",
            max_retries=3,
            created_at=now,
            updated_at=now,
        )
        assert t.current_stage is None
        assert t.completed_at is None

    def test_missing_required(self) -> None:
        with pytest.raises(ValidationError):
            TaskResponse(id="t1", project_id="p1")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Stage runs
# ---------------------------------------------------------------------------


class TestStageRunResponse:
    def test_minimal(self) -> None:
        sr = StageRunResponse(
            id="sr1",
            task_id="t1",
            stage="spec",
            attempt=1,
            status="queued",
        )
        assert sr.prompt_sent is None
        assert sr.gate_exit_code is None

    def test_full(self) -> None:
        now = datetime.now()
        sr = StageRunResponse(
            id="sr1",
            task_id="t1",
            stage="implement",
            attempt=2,
            status="passed",
            started_at=now,
            finished_at=now,
            duration_seconds=42.5,
            gate_exit_code=0,
            tokens_used=1500,
            artifacts_produced=["forge/config.py"],
        )
        assert sr.duration_seconds == 42.5
        assert sr.artifacts_produced == ["forge/config.py"]


# ---------------------------------------------------------------------------
# Run log
# ---------------------------------------------------------------------------


class TestRunLogEntry:
    def test_valid(self) -> None:
        now = datetime.now()
        entry = RunLogEntry(
            id=1,
            timestamp=now,
            level="info",
            message="Engine started",
        )
        assert entry.task_id is None
        assert entry.metadata is None

    def test_with_metadata(self) -> None:
        now = datetime.now()
        entry = RunLogEntry(
            id=2,
            timestamp=now,
            level="error",
            message="Gate failed",
            task_id="t1",
            metadata={"exit_code": 1},
        )
        assert entry.metadata == {"exit_code": 1}


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class TestEngineStatus:
    def test_defaults(self) -> None:
        s = EngineStatus(running=False)
        assert s.current_task_id is None
        assert s.queue_depth == 0

    def test_active(self) -> None:
        s = EngineStatus(running=True, current_task_id="t1", queue_depth=3)
        assert s.running is True


class TestPipelineStats:
    def test_defaults(self) -> None:
        s = PipelineStats()
        assert s.total_tasks == 0
        assert s.tasks_by_status == {}
        assert s.avg_stage_duration_seconds is None

    def test_populated(self) -> None:
        s = PipelineStats(
            total_tasks=10,
            tasks_by_status={"active": 3, "done": 7},
            total_stage_runs=25,
            stage_runs_by_status={"passed": 20, "bounced": 5},
            avg_stage_duration_seconds=45.2,
        )
        assert s.total_tasks == 10
