"""API routes for task management."""

from __future__ import annotations

import json

from fastapi import APIRouter, Body, HTTPException, Query

from forge import database
from forge.config import CONFIG_PATH, DB_PATH, FLOW_STAGES, STAGES, get_settings
from forge.models import BatchTaskCreate, CancelRequest, ResetRequest, TaskCreate, TaskResponse, TaskUpdate

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _resolve_max_retries(value: int | None) -> int:
    """Return the explicit value or fall back to the configured default."""
    if value is not None:
        return value
    settings = get_settings(CONFIG_PATH)
    return settings.engine.default_max_retries


def _row_to_task(row) -> dict:
    """Convert a sqlite3.Row to a TaskResponse-compatible dict."""
    d = dict(row)
    if isinstance(d.get("skill_overrides"), str):
        d["skill_overrides"] = json.loads(d["skill_overrides"])
    return d


@router.get("", response_model=list[TaskResponse])
def list_tasks(
    project_id: str | None = Query(None),
    status: str | None = Query(None),
    priority_gte: int | None = Query(None),
) -> list[dict]:
    conn = database.get_connection(str(DB_PATH))
    try:
        rows = database.list_tasks(
            conn,
            project_id=project_id,
            status=status,
            priority_gte=priority_gte,
        )
        return [_row_to_task(r) for r in rows]
    finally:
        conn.close()


@router.post("", response_model=TaskResponse, status_code=201)
def create_task(body: TaskCreate) -> dict:
    conn = database.get_connection(str(DB_PATH))
    try:
        # Verify project exists
        project = database.get_project(conn, body.project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")

        task_id = database.insert_task(
            conn,
            project_id=body.project_id,
            title=body.title,
            description=body.description,
            priority=body.priority,
            skill_overrides=body.skill_overrides,
            max_retries=_resolve_max_retries(body.max_retries),
            flow=body.flow,
        )
        row = database.get_task(conn, task_id)
        return _row_to_task(row)
    finally:
        conn.close()


@router.post("/batch", response_model=list[TaskResponse], status_code=201)
def batch_create_tasks(body: BatchTaskCreate) -> list[dict]:
    conn = database.get_connection(str(DB_PATH))
    try:
        # Validate all project_ids upfront before inserting anything
        for task_input in body.tasks:
            project = database.get_project(conn, task_input.project_id)
            if project is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Project not found: {task_input.project_id}",
                )

        # All validated — insert in a single transaction
        results = []
        conn.execute("BEGIN")
        try:
            for task_input in body.tasks:
                task_id = database.insert_task_no_commit(
                    conn,
                    project_id=task_input.project_id,
                    title=task_input.title,
                    description=task_input.description,
                    priority=task_input.priority,
                    skill_overrides=task_input.skill_overrides,
                    max_retries=_resolve_max_retries(task_input.max_retries),
                    flow=task_input.flow,
                )
                row = database.get_task(conn, task_id)
                results.append(_row_to_task(row))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return results
    finally:
        conn.close()


@router.get("/{task_id}", response_model=TaskResponse)
def get_task(task_id: str) -> dict:
    conn = database.get_connection(str(DB_PATH))
    try:
        row = database.get_task(conn, task_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Task not found")
        return _row_to_task(row)
    finally:
        conn.close()


@router.patch("/{task_id}", response_model=TaskResponse)
def update_task(task_id: str, body: TaskUpdate) -> dict:
    conn = database.get_connection(str(DB_PATH))
    try:
        row = database.get_task(conn, task_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Task not found")

        updates = body.model_dump(exclude_unset=True)
        if "status" in updates:
            raise HTTPException(
                status_code=400,
                detail="Use /activate, /pause, /resume, /retry, or /cancel to change task status",
            )
        if "flow" in updates and row["status"] != "backlog":
            raise HTTPException(
                status_code=400,
                detail="Cannot change flow on a task that is not in backlog status",
            )
        if not updates:
            return _row_to_task(row)

        database.update_task(conn, task_id, **updates)
        updated_row = database.get_task(conn, task_id)
        return _row_to_task(updated_row)
    finally:
        conn.close()


@router.delete("/{task_id}", status_code=204)
def delete_task(task_id: str) -> None:
    conn = database.get_connection(str(DB_PATH))
    try:
        row = database.get_task(conn, task_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if row["status"] != "backlog":
            raise HTTPException(
                status_code=400, detail="Only backlog tasks can be deleted"
            )

        database.delete_task(conn, task_id)
    finally:
        conn.close()


@router.post("/{task_id}/activate", response_model=TaskResponse)
def activate_task(task_id: str) -> dict:
    """Move a backlog task into the pipeline by creating its first stage_run."""
    conn = database.get_connection(str(DB_PATH))
    try:
        row = database.get_task(conn, task_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if row["status"] != "backlog":
            raise HTTPException(
                status_code=400, detail="Only backlog tasks can be activated"
            )

        flow = row["flow"] if row["flow"] else "standard"
        first_stage = FLOW_STAGES.get(flow, STAGES)[0]
        database.insert_stage_run(
            conn,
            task_id=task_id,
            stage=first_stage,
            attempt=1,
            status="queued",
        )
        database.update_task(conn, task_id, status="active", current_stage=first_stage)
        updated_row = database.get_task(conn, task_id)
        return _row_to_task(updated_row)
    finally:
        conn.close()


@router.post("/{task_id}/resume", response_model=TaskResponse)
def resume_task(task_id: str) -> dict:
    """Resume a needs_human task by creating a new stage_run for its current stage."""
    conn = database.get_connection(str(DB_PATH))
    try:
        row = database.get_task(conn, task_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if row["status"] != "needs_human":
            raise HTTPException(
                status_code=400,
                detail="Only needs_human tasks can be resumed (cancelled tasks cannot be resumed)",
            )

        stage = row["current_stage"]
        if not stage:
            stage = STAGES[0]

        retry_count = database.get_retry_count(conn, task_id, stage)
        database.insert_stage_run(
            conn,
            task_id=task_id,
            stage=stage,
            attempt=retry_count + 1,
            status="queued",
        )
        database.update_task(conn, task_id, status="active")
        updated_row = database.get_task(conn, task_id)
        return _row_to_task(updated_row)
    finally:
        conn.close()


@router.post("/{task_id}/pause", response_model=TaskResponse)
def pause_task(task_id: str) -> dict:
    """Pause an active task."""
    conn = database.get_connection(str(DB_PATH))
    try:
        row = database.get_task(conn, task_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if row["status"] != "active":
            raise HTTPException(
                status_code=400, detail="Only active tasks can be paused"
            )

        database.update_task(conn, task_id, status="paused")
        updated_row = database.get_task(conn, task_id)
        return _row_to_task(updated_row)
    finally:
        conn.close()


@router.post("/{task_id}/retry", response_model=TaskResponse)
def retry_task(task_id: str) -> dict:
    """Force retry the current stage of a task."""
    conn = database.get_connection(str(DB_PATH))
    try:
        row = database.get_task(conn, task_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if row["status"] not in ("active", "needs_human"):
            raise HTTPException(
                status_code=400,
                detail="Only active or needs_human tasks can be retried (cancelled tasks cannot be retried)",
            )

        stage = row["current_stage"]
        if not stage:
            stage = STAGES[0]

        retry_count = database.get_retry_count(conn, task_id, stage)
        database.insert_stage_run(
            conn,
            task_id=task_id,
            stage=stage,
            attempt=retry_count + 1,
            status="queued",
        )
        database.update_task(conn, task_id, status="active")
        updated_row = database.get_task(conn, task_id)
        return _row_to_task(updated_row)
    finally:
        conn.close()


_RESETTABLE_STATUSES = {"needs_human", "failed", "paused"}


@router.post("/{task_id}/reset", response_model=TaskResponse)
def reset_task(task_id: str, body: ResetRequest | None = Body(default=None)) -> dict:
    """Reset a task to a clean state, wiping stage_run history."""
    conn = database.get_connection(str(DB_PATH))
    try:
        row = database.get_task(conn, task_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Task not found")

        flow = row["flow"] if row["flow"] else "standard"
        flow_stages = FLOW_STAGES.get(flow, STAGES)
        from_stage = body.from_stage if body else flow_stages[0]
        if from_stage not in flow_stages:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid stage '{from_stage}' for flow '{flow}'. "
                f"Valid stages: {flow_stages}",
            )

        if row["status"] not in _RESETTABLE_STATUSES:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot reset a task with status '{row['status']}'. "
                "Only needs_human, failed, or paused tasks can be reset.",
            )

        # Block reset if task has a currently running stage_run
        running = database.list_stage_runs(conn, task_id=task_id, status="running")
        if running:
            raise HTTPException(
                status_code=409,
                detail="Cannot reset task while a stage run is in progress.",
            )

        database.reset_task(conn, task_id, from_stage, row["title"])
        updated_row = database.get_task(conn, task_id)
        return _row_to_task(updated_row)
    finally:
        conn.close()


_CANCELLABLE_STATUSES = {"backlog", "active", "paused", "needs_human"}


@router.post("/{task_id}/cancel", response_model=TaskResponse)
def cancel_task(
    task_id: str, body: CancelRequest | None = Body(default=None)
) -> dict:
    """Cancel a task that is in a cancellable state."""
    conn = database.get_connection(str(DB_PATH))
    try:
        row = database.get_task(conn, task_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if row["status"] not in _CANCELLABLE_STATUSES:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot cancel a task with status '{row['status']}'. "
                "Only backlog, active, paused, or needs_human tasks can be cancelled.",
            )

        # Mark any running stage runs as errored
        running_runs = database.list_stage_runs(
            conn, task_id=task_id, status="running"
        )
        for sr in running_runs:
            database.update_stage_run(
                conn, sr["id"], status="error", error_message="Task cancelled"
            )

        # Update task status
        database.update_task(conn, task_id, status="cancelled")

        # Log the cancellation
        reason = body.reason if body else None
        message = "Task cancelled"
        if reason:
            message = f"Task cancelled: {reason}"
        database.insert_log(conn, level="info", task_id=task_id, message=message)

        updated_row = database.get_task(conn, task_id)
        return _row_to_task(updated_row)
    finally:
        conn.close()
