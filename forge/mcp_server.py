"""MCP server exposing tools for querying and creating Forge projects and tasks."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import deque

from mcp.server.fastmcp import FastMCP

from forge import config, database
from forge.config import FLOW_STAGES, STAGES, VALID_EPIC_STATUSES

mcp = FastMCP("forge")


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a sqlite3.Row to a plain dict, parsing JSON-encoded fields."""
    d = dict(row)
    for key in (
        "skill_refs",
        "config",
        "stage_timeouts",
        "skill_overrides",
        "artifacts_produced",
    ):
        if key in d and isinstance(d[key], str):
            try:
                d[key] = json.loads(d[key])
            except (json.JSONDecodeError, TypeError):
                pass
    return d


@mcp.tool()
def list_projects() -> list[dict]:
    """List all registered projects with id, name, repo_path, default_branch, and configuration."""
    conn = database.get_connection()
    try:
        rows = database.list_projects(conn)
        return [_row_to_dict(row) for row in rows]
    finally:
        conn.close()


@mcp.tool()
def get_project_backlog(project_id: str) -> list[dict]:
    """Get all non-complete tasks for a project, ordered by priority descending.

    Excludes tasks with status done, cancelled, or failed.
    """
    conn = database.get_connection()
    try:
        rows = database.list_tasks(conn, project_id=project_id)
        excluded = ("done", "cancelled", "failed")
        results = []
        for row in rows:
            if row["status"] in excluded:
                continue
            d = _row_to_dict(row)
            task_id = d["id"]
            # Include dependency info
            links = database.get_task_links(conn, task_id)
            d["depends_on"] = [
                dict(link)
                for link in links
                if link["link_type"] == "blocks" and link["target_task_id"] == task_id
            ]
            results.append(
                {
                    "id": d["id"],
                    "title": d["title"],
                    "status": d["status"],
                    "priority": d["priority"],
                    "flow": d["flow"],
                    "current_stage": d["current_stage"],
                    "depends_on": d["depends_on"],
                    "parent_task_id": d.get("parent_task_id"),
                    "epic_status": d.get("epic_status"),
                }
            )
        return results
    finally:
        conn.close()


@mcp.tool()
def get_task_detail(task_id: str) -> dict | None:
    """Get the full task record including stage history. For epics, includes child tasks."""
    conn = database.get_connection()
    try:
        row = database.get_task(conn, task_id)
        if row is None:
            return None
        d = _row_to_dict(row)
        # Add stage runs
        stage_runs = database.list_stage_runs(conn, task_id=task_id)
        d["stage_runs"] = [_row_to_dict(sr) for sr in stage_runs]
        # Add child tasks for epics
        if d.get("flow") == "epic":
            children = database.get_child_tasks(conn, task_id)
            d["child_tasks"] = [_row_to_dict(child) for child in children]
        return d
    finally:
        conn.close()


@mcp.tool()
def get_completed_tasks(project_id: str, limit: int = 20) -> list[dict]:
    """Get recently completed tasks for a project, ordered by completion date descending."""
    conn = database.get_connection()
    try:
        rows = database.list_tasks(conn, project_id=project_id, status="done")
        tasks = [_row_to_dict(row) for row in rows]
        # Sort by completed_at descending (most recent first)
        tasks.sort(key=lambda t: t.get("completed_at") or "", reverse=True)
        tasks = tasks[:limit]
        return [
            {"id": t["id"], "title": t["title"], "completed_at": t.get("completed_at")}
            for t in tasks
        ]
    finally:
        conn.close()


_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


def _is_uuid(value: str) -> bool:
    """Return True if *value* looks like a UUID string."""
    return bool(_UUID_RE.match(value))


@mcp.tool()
def create_task(
    project_id: str,
    title: str,
    description: str = "",
    priority: int = 0,
    flow: str = "standard",
    depends_on: list[str] | None = None,
) -> dict:
    """Create a single task. Returns the created task dict, or an error dict on failure.

    depends_on is an optional list of existing task IDs that block this task.
    """
    conn = database.get_connection()
    try:
        # Validate project
        project = database.get_project(conn, project_id)
        if project is None:
            return {"error": f"Project not found: {project_id}"}

        # Validate title
        if not title:
            return {"error": "title must not be empty"}

        # Validate flow
        if flow not in config.VALID_FLOWS:
            return {
                "error": f"Invalid flow: {flow!r}. Must be one of {config.VALID_FLOWS}"
            }

        # Validate depends_on
        if depends_on:
            for dep_id in depends_on:
                dep = database.get_task(conn, dep_id)
                if dep is None:
                    return {"error": f"Dependency task not found: {dep_id}"}
                if dep["project_id"] != project_id:
                    return {
                        "error": f"Dependency task {dep_id} belongs to a different project"
                    }

        # Insert task and links atomically
        conn.execute("BEGIN")
        try:
            task_id = database.insert_task_no_commit(
                conn,
                project_id=project_id,
                title=title,
                description=description,
                priority=priority,
                flow=flow,
            )

            if depends_on:
                for dep_id in depends_on:
                    database.insert_task_link_no_commit(
                        conn,
                        source_task_id=dep_id,
                        target_task_id=task_id,
                        link_type="blocks",
                    )

            conn.commit()
        except Exception:
            conn.rollback()
            raise

        row = database.get_task(conn, task_id)
        return _row_to_dict(row)
    finally:
        conn.close()


@mcp.tool()
def create_task_batch(
    project_id: str,
    tasks: str,
) -> dict | list[dict]:
    """Create multiple tasks atomically. Returns list of created task dicts, or error dict.

    tasks is a JSON string encoding a list of objects, each with keys:
      title (str, required), description (str), priority (int), flow (str),
      depends_on (list of UUIDs or titles referencing other tasks in this batch).
    """
    # Parse JSON
    try:
        task_list: list[dict] = json.loads(tasks)
    except (json.JSONDecodeError, TypeError) as exc:
        return {"error": f"Invalid JSON for tasks: {exc}"}

    if not isinstance(task_list, list) or len(task_list) == 0:
        return {"error": "tasks must be a non-empty JSON array"}

    if not all(isinstance(t, dict) for t in task_list):
        return {"error": "Each item in tasks must be a JSON object"}

    conn = database.get_connection()
    try:
        # Validate project
        project = database.get_project(conn, project_id)
        if project is None:
            return {"error": f"Project not found: {project_id}"}

        # Validate each task's fields (must run before duplicate title check)
        for i, task_obj in enumerate(task_list):
            if not task_obj.get("title"):
                return {"error": f"Task at index {i} is missing a title"}

        # Check for duplicate titles in the batch
        titles = [t["title"] for t in task_list]
        seen_titles: set[str] = set()
        dupes: list[str] = []
        for t in titles:
            if t in seen_titles:
                dupes.append(t)
            seen_titles.add(t)
        if dupes:
            return {"error": f"Duplicate titles in batch: {dupes}"}

        title_set = set(titles)

        # Validate each task's remaining fields
        for i, task_obj in enumerate(task_list):
            task_flow = task_obj.get("flow", "standard")
            if task_flow not in config.VALID_FLOWS:
                return {
                    "error": f"Task {task_obj['title']!r} has invalid flow: {task_flow!r}. "
                    f"Must be one of {config.VALID_FLOWS}"
                }

            # Validate depends_on references
            for dep in task_obj.get("depends_on", []):
                if _is_uuid(dep):
                    dep_row = database.get_task(conn, dep)
                    if dep_row is None:
                        return {"error": f"Dependency task not found: {dep}"}
                    if dep_row["project_id"] != project_id:
                        return {
                            "error": f"Dependency task {dep} belongs to a different project"
                        }
                else:
                    # Title reference — must exist in this batch
                    if dep not in title_set:
                        return {
                            "error": f"Dependency title not found in batch: {dep!r}"
                        }

        # Build dependency graph (index-based) for cycle detection
        title_to_idx: dict[str, int] = {t["title"]: i for i, t in enumerate(task_list)}
        adj: dict[int, list[int]] = {i: [] for i in range(len(task_list))}
        in_degree: dict[int, int] = {i: 0 for i in range(len(task_list))}

        for i, task_obj in enumerate(task_list):
            for dep in task_obj.get("depends_on", []):
                if not _is_uuid(dep) and dep in title_to_idx:
                    dep_idx = title_to_idx[dep]
                    adj[dep_idx].append(i)
                    in_degree[i] += 1

        # Kahn's algorithm for topological sort / cycle detection
        queue: deque[int] = deque()
        for idx, deg in in_degree.items():
            if deg == 0:
                queue.append(idx)

        visited = 0
        while queue:
            node = queue.popleft()
            visited += 1
            for neighbor in adj[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if visited != len(task_list):
            return {"error": "Circular dependency detected among batch tasks"}

        # All validation passed — insert atomically
        conn.execute("BEGIN")
        try:
            created_ids: list[str] = []
            title_to_id: dict[str, str] = {}

            # First pass: insert all tasks
            for task_obj in task_list:
                task_id = database.insert_task_no_commit(
                    conn,
                    project_id=project_id,
                    title=task_obj["title"],
                    description=task_obj.get("description", ""),
                    priority=task_obj.get("priority", 0),
                    flow=task_obj.get("flow", "standard"),
                )
                created_ids.append(task_id)
                title_to_id[task_obj["title"]] = task_id

            # Second pass: create dependency links
            for task_obj, task_id in zip(task_list, created_ids):
                for dep in task_obj.get("depends_on", []):
                    if _is_uuid(dep):
                        source_id = dep
                    else:
                        source_id = title_to_id[dep]
                    database.insert_task_link_no_commit(
                        conn,
                        source_task_id=source_id,
                        target_task_id=task_id,
                        link_type="blocks",
                    )

            conn.commit()
        except Exception:
            conn.rollback()
            raise

        # Return created tasks
        result = []
        for task_id in created_ids:
            row = database.get_task(conn, task_id)
            result.append(_row_to_dict(row))
        return result
    finally:
        conn.close()


@mcp.tool()
def update_task(
    task_id: str,
    title: str | None = None,
    description: str | None = None,
    priority: int | None = None,
    flow: str | None = None,
    epic_status: str | None = None,
) -> dict:
    """Update task metadata. Returns the updated task dict, or an error dict on failure.

    Cannot change status via this tool — use activate, pause, resume, retry, or cancel instead.
    Flow can only be changed on backlog tasks. epic_status can only be set on epic-flow tasks.
    """
    conn = database.get_connection()
    try:
        row = database.get_task(conn, task_id)
        if row is None:
            return {"error": "Task not found"}

        updates: dict = {}
        if title is not None:
            if not title:
                return {"error": "title must not be empty"}
            updates["title"] = title
        if description is not None:
            updates["description"] = description
        if priority is not None:
            updates["priority"] = priority
        if flow is not None:
            if flow not in config.VALID_FLOWS:
                return {"error": f"Invalid flow: {flow!r}. Must be one of {config.VALID_FLOWS}"}
            updates["flow"] = flow
        if epic_status is not None:
            updates["epic_status"] = epic_status

        if "flow" in updates and row["status"] != "backlog":
            return {"error": "Cannot change flow on a task that is not in backlog status"}

        if "epic_status" in updates:
            if updates["epic_status"] not in VALID_EPIC_STATUSES:
                return {
                    "error": f"Invalid epic_status: {updates['epic_status']!r}. "
                    f"Must be one of {VALID_EPIC_STATUSES}"
                }
            effective_flow = updates.get("flow", row["flow"])
            if effective_flow != "epic":
                return {"error": "epic_status can only be set on tasks with flow 'epic'"}

        if not updates:
            return _row_to_dict(row)

        database.update_task(conn, task_id, **updates)
        updated_row = database.get_task(conn, task_id)
        return _row_to_dict(updated_row)
    finally:
        conn.close()


@mcp.tool()
def delete_task(task_id: str) -> dict:
    """Delete a backlog task. Returns {"deleted": True} or an error dict."""
    conn = database.get_connection()
    try:
        row = database.get_task(conn, task_id)
        if row is None:
            return {"error": "Task not found"}
        if row["status"] != "backlog":
            return {"error": "Only backlog tasks can be deleted"}

        database.delete_task(conn, task_id)
        return {"deleted": True}
    finally:
        conn.close()


@mcp.tool()
def activate_task(task_id: str) -> dict:
    """Move a backlog task into the pipeline. Returns the updated task dict or error dict."""
    conn = database.get_connection()
    try:
        row = database.get_task(conn, task_id)
        if row is None:
            return {"error": "Task not found"}
        if row["status"] != "backlog":
            return {"error": "Only backlog tasks can be activated"}

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
        return _row_to_dict(updated_row)
    finally:
        conn.close()


@mcp.tool()
def pause_task(task_id: str) -> dict:
    """Pause an active task. Returns the updated task dict or error dict."""
    conn = database.get_connection()
    try:
        row = database.get_task(conn, task_id)
        if row is None:
            return {"error": "Task not found"}
        if row["status"] != "active":
            return {"error": "Only active tasks can be paused"}

        database.update_task(conn, task_id, status="paused")
        updated_row = database.get_task(conn, task_id)
        return _row_to_dict(updated_row)
    finally:
        conn.close()


@mcp.tool()
def resume_task(task_id: str) -> dict:
    """Resume a needs_human task. Returns the updated task dict or error dict."""
    conn = database.get_connection()
    try:
        row = database.get_task(conn, task_id)
        if row is None:
            return {"error": "Task not found"}
        if row["status"] != "needs_human":
            return {
                "error": "Only needs_human tasks can be resumed "
                "(cancelled tasks cannot be resumed)"
            }

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
        return _row_to_dict(updated_row)
    finally:
        conn.close()


@mcp.tool()
def retry_task(task_id: str) -> dict:
    """Force retry the current stage of a task. Returns the updated task dict or error dict."""
    conn = database.get_connection()
    try:
        row = database.get_task(conn, task_id)
        if row is None:
            return {"error": "Task not found"}
        if row["status"] not in ("active", "needs_human"):
            return {
                "error": "Only active or needs_human tasks can be retried "
                "(cancelled tasks cannot be retried)"
            }

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
        return _row_to_dict(updated_row)
    finally:
        conn.close()


_RESETTABLE_STATUSES = {"needs_human", "failed", "paused"}


@mcp.tool()
def reset_task(task_id: str, from_stage: str | None = None) -> dict:
    """Reset a task to a clean state, wiping stage_run history.

    Only needs_human, failed, or paused tasks can be reset.
    Optional from_stage sets which stage to restart from (defaults to first stage in flow).
    Returns the updated task dict or error dict.
    """
    conn = database.get_connection()
    try:
        row = database.get_task(conn, task_id)
        if row is None:
            return {"error": "Task not found"}

        flow = row["flow"] if row["flow"] else "standard"
        flow_stages = FLOW_STAGES.get(flow, STAGES)
        target_stage = from_stage if from_stage else flow_stages[0]
        if target_stage not in flow_stages:
            return {
                "error": f"Invalid stage '{target_stage}' for flow '{flow}'. "
                f"Valid stages: {flow_stages}"
            }

        if row["status"] not in _RESETTABLE_STATUSES:
            return {
                "error": f"Cannot reset a task with status '{row['status']}'. "
                "Only needs_human, failed, or paused tasks can be reset."
            }

        # Block reset if task has a currently running stage_run
        running = database.list_stage_runs(conn, task_id=task_id, status="running")
        if running:
            return {"error": "Cannot reset task while a stage run is in progress."}

        database.reset_task(conn, task_id, target_stage, row["title"])
        updated_row = database.get_task(conn, task_id)
        return _row_to_dict(updated_row)
    finally:
        conn.close()


_CANCELLABLE_STATUSES = {"backlog", "active", "paused", "needs_human"}


@mcp.tool()
def cancel_task(task_id: str, reason: str | None = None) -> dict:
    """Cancel a task. Returns the updated task dict or error dict.

    Only backlog, active, paused, or needs_human tasks can be cancelled.
    Optional reason is recorded in the log.
    """
    conn = database.get_connection()
    try:
        row = database.get_task(conn, task_id)
        if row is None:
            return {"error": "Task not found"}
        if row["status"] not in _CANCELLABLE_STATUSES:
            return {
                "error": f"Cannot cancel a task with status '{row['status']}'. "
                "Only backlog, active, paused, or needs_human tasks can be cancelled."
            }

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
        message = "Task cancelled"
        if reason:
            message = f"Task cancelled: {reason}"
        database.insert_log(conn, level="info", task_id=task_id, message=message)

        updated_row = database.get_task(conn, task_id)
        return _row_to_dict(updated_row)
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Forge MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="Transport to use (default: stdio)",
    )
    args = parser.parse_args()
    mcp.run(transport=args.transport)
