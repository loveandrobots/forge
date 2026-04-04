"""API routes for task management."""

from __future__ import annotations

import json
import os
import re

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import JSONResponse

from forge import database, dispatcher
from forge.config import CONFIG_PATH, DB_PATH, FLOW_STAGES, STAGES, VALID_EPIC_STATUSES, get_settings
from forge.models import (
    BatchTaskCreate,
    CancelRequest,
    CancelWarningResponse,
    GenerateRequest,
    GenerateResponse,
    GeneratedTask,
    ResetRequest,
    TaskCreate,
    TaskResponse,
    TaskUpdate,
)

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

        # Validate parent_task_id references an existing task
        if body.parent_task_id is not None:
            parent = database.get_task(conn, body.parent_task_id)
            if parent is None:
                raise HTTPException(status_code=404, detail="Parent task not found")

        # Validate epic_status
        epic_status = body.epic_status
        if epic_status is not None and epic_status not in VALID_EPIC_STATUSES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid epic_status: {epic_status!r}. Must be one of {VALID_EPIC_STATUSES}",
            )
        if epic_status is not None and body.flow != "epic":
            raise HTTPException(
                status_code=400,
                detail="epic_status can only be set on tasks with flow 'epic'",
            )
        if body.flow == "epic" and epic_status is None:
            epic_status = "pending"

        task_id = database.insert_task(
            conn,
            project_id=body.project_id,
            title=body.title,
            description=body.description,
            priority=body.priority,
            skill_overrides=body.skill_overrides,
            max_retries=_resolve_max_retries(body.max_retries),
            flow=body.flow,
            parent_task_id=body.parent_task_id,
            epic_status=epic_status,
        )
        row = database.get_task(conn, task_id)
        return _row_to_task(row)
    finally:
        conn.close()


@router.post("/batch", response_model=list[TaskResponse], status_code=201)
def batch_create_tasks(body: BatchTaskCreate) -> list[dict]:
    conn = database.get_connection(str(DB_PATH))
    try:
        n = len(body.tasks)

        # Validate all project_ids and parent_task_ids upfront before inserting anything
        for task_input in body.tasks:
            project = database.get_project(conn, task_input.project_id)
            if project is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Project not found: {task_input.project_id}",
                )
            if task_input.parent_task_id is not None:
                parent = database.get_task(conn, task_input.parent_task_id)
                if parent is None:
                    raise HTTPException(
                        status_code=404,
                        detail="Parent task not found",
                    )
            if task_input.epic_status is not None and task_input.epic_status not in VALID_EPIC_STATUSES:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid epic_status: {task_input.epic_status!r}. Must be one of {VALID_EPIC_STATUSES}",
                )
            if task_input.epic_status is not None and task_input.flow != "epic":
                raise HTTPException(
                    status_code=400,
                    detail="epic_status can only be set on tasks with flow 'epic'",
                )

        # Deduplicate depends_on indices
        for task_input in body.tasks:
            task_input.depends_on = list(dict.fromkeys(task_input.depends_on))

        # Validate depends_on indices
        for i, task_input in enumerate(body.tasks):
            for dep_idx in task_input.depends_on:
                if dep_idx < 0 or dep_idx >= n:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Task {i} has invalid dependency index {dep_idx} "
                        f"(must be 0..{n - 1})",
                    )
                if dep_idx == i:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Task {i} depends on itself",
                    )

        # Topological sort to detect cycles and determine insertion order
        order = _topological_sort(n, [t.depends_on for t in body.tasks])

        # All validated — insert in a single transaction
        index_to_task_id: dict[int, str] = {}
        results_by_index: dict[int, dict] = {}
        conn.execute("BEGIN")
        try:
            for idx in order:
                task_input = body.tasks[idx]
                epic_status = task_input.epic_status
                if task_input.flow == "epic" and epic_status is None:
                    epic_status = "pending"
                task_id = database.insert_task_no_commit(
                    conn,
                    project_id=task_input.project_id,
                    title=task_input.title,
                    description=task_input.description,
                    priority=task_input.priority,
                    skill_overrides=task_input.skill_overrides,
                    max_retries=_resolve_max_retries(task_input.max_retries),
                    flow=task_input.flow,
                    parent_task_id=task_input.parent_task_id,
                    epic_status=epic_status,
                )
                index_to_task_id[idx] = task_id

                # Create task_links for dependencies
                for dep_idx in task_input.depends_on:
                    dep_task_id = index_to_task_id[dep_idx]
                    database.insert_task_link_no_commit(
                        conn,
                        source_task_id=dep_task_id,
                        target_task_id=task_id,
                        link_type="blocks",
                    )

                row = database.get_task(conn, task_id)
                results_by_index[idx] = _row_to_task(row)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        # Return results in original array order
        return [results_by_index[i] for i in range(n)]
    finally:
        conn.close()


def _topological_sort(n: int, deps: list[list[int]]) -> list[int]:
    """Return indices in dependency order. Raises HTTPException on cycles."""
    # Build adjacency list and in-degree counts
    in_degree = [0] * n
    dependents: list[list[int]] = [[] for _ in range(n)]
    for i, dep_list in enumerate(deps):
        in_degree[i] = len(dep_list)
        for d in dep_list:
            dependents[d].append(i)

    # Kahn's algorithm
    queue = [i for i in range(n) if in_degree[i] == 0]
    order: list[int] = []
    while queue:
        node = queue.pop(0)
        order.append(node)
        for dep in dependents[node]:
            in_degree[dep] -= 1
            if in_degree[dep] == 0:
                queue.append(dep)

    if len(order) != n:
        raise HTTPException(
            status_code=400,
            detail="Circular dependency detected among tasks",
        )
    return order


def _extract_json_array(text: str) -> str:
    """Extract a JSON array from text, stripping markdown code fences if present."""
    # Try stripping markdown code fences
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if fence_match:
        return fence_match.group(1).strip()
    # Look for raw JSON array
    arr_match = re.search(r"\[.*\]", text, re.DOTALL)
    if arr_match:
        return arr_match.group(0)
    return text


@router.post("/generate", response_model=GenerateResponse)
async def generate_tasks(body: GenerateRequest) -> dict:
    """Use Claude Code with the forge-task-writer skill to generate tasks."""
    conn = database.get_connection(str(DB_PATH))
    try:
        project = database.get_project(conn, body.project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        repo_path = project["repo_path"]
    finally:
        conn.close()

    # Check skill file exists
    skill_path = os.path.join(
        repo_path, ".claude", "skills", "forge-task-writer", "SKILL.md"
    )
    if not os.path.exists(skill_path):
        raise HTTPException(
            status_code=400,
            detail=f"Skill file not found at {skill_path}. "
            "Install the forge-task-writer skill first.",
        )

    # Dispatch to Claude Code
    result = await dispatcher.dispatch_generate(
        prompt=body.problem_description,
        repo_path=repo_path,
        skill_path=skill_path,
    )

    if result.exit_code != 0:
        raise HTTPException(
            status_code=502,
            detail=result.error or "Claude Code failed",
        )

    # Parse JSON from output
    raw_text = result.output.strip()
    if not raw_text:
        raise HTTPException(
            status_code=422,
            detail="Claude Code returned empty output",
        )

    json_text = _extract_json_array(raw_text)
    try:
        parsed = json.loads(json_text)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=422,
            detail=f"Failed to parse JSON from Claude output: {raw_text[:500]}",
        )

    if not isinstance(parsed, list):
        raise HTTPException(
            status_code=422,
            detail="Expected a JSON array of tasks",
        )

    try:
        tasks = [GeneratedTask(**item) for item in parsed]
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid task structure in Claude output: {exc}",
        )
    return {"tasks": tasks}


@router.get("/{task_id}/children", response_model=list[TaskResponse])
def get_children(task_id: str) -> list[dict]:
    """Return all child tasks of the given parent task."""
    conn = database.get_connection(str(DB_PATH))
    try:
        row = database.get_task(conn, task_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Task not found")
        rows = database.get_child_tasks(conn, task_id)
        return [_row_to_task(r) for r in rows]
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
        if "epic_status" in updates and updates["epic_status"] is not None:
            if updates["epic_status"] not in VALID_EPIC_STATUSES:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid epic_status: {updates['epic_status']!r}. Must be one of {VALID_EPIC_STATUSES}",
                )
            effective_flow = updates.get("flow", row["flow"])
            if effective_flow != "epic":
                raise HTTPException(
                    status_code=400,
                    detail="epic_status can only be set on tasks with flow 'epic'",
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
            flow = row["flow"] if row["flow"] else "standard"
            stage = FLOW_STAGES.get(flow, STAGES)[0]

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
            flow = row["flow"] if row["flow"] else "standard"
            stage = FLOW_STAGES.get(flow, STAGES)[0]

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
        from_stage = body.from_stage if body and body.from_stage else flow_stages[0]
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


_CANCELLABLE_STATUSES = database.CANCELLABLE_STATUSES
_TERMINAL_STATUSES = database.TERMINAL_STATUSES


@router.post(
    "/{task_id}/cancel",
    response_model=TaskResponse,
    responses={409: {"model": CancelWarningResponse}},
)
def cancel_task(
    task_id: str, body: CancelRequest | None = Body(default=None)
) -> TaskResponse | JSONResponse:
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

        force = body.force if body else False
        reason = body.reason if body else None

        # Epic-flow tasks: check for active children
        if row["flow"] == "epic":
            children = database.get_child_tasks(conn, task_id)
            active_children = [
                dict(c) for c in children if c["status"] not in _TERMINAL_STATUSES
            ]
            if active_children and not force:
                warning = CancelWarningResponse(
                    warning="Epic has active children. Use force=true to cancel them all.",
                    active_children=[
                        {"id": c["id"], "title": c["title"], "status": c["status"]}
                        for c in active_children
                    ],
                )
                return JSONResponse(
                    status_code=409, content=warning.model_dump()
                )
            # Force-cancel: cancel all active children first
            for child in active_children:
                database.cancel_single_task(conn, child["id"], reason="Parent epic cancelled")

        database.cancel_single_task(conn, task_id, reason)

        updated_row = database.get_task(conn, task_id)
        return _row_to_task(updated_row)
    finally:
        conn.close()
