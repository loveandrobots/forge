from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


class ProjectCreate(BaseModel):
    name: str
    repo_path: str
    default_branch: str = "main"
    gate_dir: str = "gates"
    skill_refs: list[str] | None = None
    config: dict | None = None


class ProjectUpdate(BaseModel):
    name: str | None = None
    repo_path: str | None = None
    default_branch: str | None = None
    gate_dir: str | None = None
    skill_refs: list[str] | None = None
    config: dict | None = None


class ProjectResponse(BaseModel):
    id: str
    name: str
    repo_path: str
    default_branch: str
    gate_dir: str
    skill_refs: list[str] | None = None
    config: dict | None = None
    created_at: datetime


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


class TaskCreate(BaseModel):
    project_id: str
    title: str
    description: str = ""
    priority: int = 0
    skill_overrides: list[str] | None = None
    max_retries: int = 3
    flow: Literal["standard", "quick"] = "standard"


class BatchTaskCreate(BaseModel):
    tasks: list[TaskCreate]


class TaskUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    priority: int | None = None
    status: str | None = None
    current_stage: str | None = None
    branch_name: str | None = None
    spec_path: str | None = None
    plan_path: str | None = None
    review_path: str | None = None
    skill_overrides: list[str] | None = None
    flow: Literal["standard", "quick"] | None = None


class TaskResponse(BaseModel):
    id: str
    project_id: str
    title: str
    description: str
    priority: int
    current_stage: str | None = None
    status: str
    branch_name: str | None = None
    spec_path: str | None = None
    plan_path: str | None = None
    review_path: str | None = None
    skill_overrides: list[str] | None = None
    max_retries: int
    flow: Literal["standard", "quick"] = "standard"
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None


# ---------------------------------------------------------------------------
# Cancel request
# ---------------------------------------------------------------------------


class CancelRequest(BaseModel):
    reason: str | None = None


class ResetRequest(BaseModel):
    from_stage: str = "spec"


# ---------------------------------------------------------------------------
# Stage runs
# ---------------------------------------------------------------------------


class StageRunResponse(BaseModel):
    id: str
    task_id: str
    stage: str
    attempt: int
    status: str
    prompt_sent: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: float | None = None
    claude_output: str | None = None
    artifacts_produced: list[str] | None = None
    gate_name: str | None = None
    gate_exit_code: int | None = None
    gate_stdout: str | None = None
    gate_stderr: str | None = None
    tokens_used: int | None = None
    error_message: str | None = None


# ---------------------------------------------------------------------------
# Run log
# ---------------------------------------------------------------------------


class RunLogEntry(BaseModel):
    id: int
    timestamp: datetime
    level: str
    message: str
    task_id: str | None = None
    stage_run_id: str | None = None
    metadata: dict | None = None


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class EngineStatus(BaseModel):
    running: bool
    current_task_id: str | None = None
    current_task_title: str | None = None
    current_stage: str | None = None
    queue_depth: int = 0


class PipelineStats(BaseModel):
    total_tasks: int = 0
    tasks_by_status: dict[str, int] = Field(default_factory=dict)
    total_stage_runs: int = 0
    stage_runs_by_status: dict[str, int] = Field(default_factory=dict)
    avg_stage_duration_seconds: float | None = None
    total_completed: int = 0
    total_active: int = 0
    avg_duration_by_stage: dict[str, float] = Field(default_factory=dict)
    bounce_rate_by_stage: dict[str, float] = Field(default_factory=dict)
