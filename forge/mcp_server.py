"""MCP server exposing read-only tools for querying Forge projects and tasks."""

from __future__ import annotations

import json
import sqlite3

from mcp.server.fastmcp import FastMCP

from forge import database

mcp = FastMCP("forge")


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a sqlite3.Row to a plain dict, parsing JSON-encoded fields."""
    d = dict(row)
    for key in ("skill_refs", "config", "stage_timeouts", "skill_overrides", "artifacts_produced"):
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
                dict(link) for link in links if link["link_type"] == "blocks" and link["target_task_id"] == task_id
            ]
            results.append({
                "id": d["id"],
                "title": d["title"],
                "status": d["status"],
                "priority": d["priority"],
                "flow": d["flow"],
                "current_stage": d["current_stage"],
                "depends_on": d["depends_on"],
                "parent_task_id": d.get("parent_task_id"),
                "epic_status": d.get("epic_status"),
            })
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
