"""Dashboard page routes — server-rendered Jinja2 + htmx views."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from forge import database
from forge.config import DB_PATH, STAGES

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

router = APIRouter(tags=["dashboard"])


def _row_to_dict(row) -> dict:
    """Convert a sqlite3.Row to a plain dict with JSON fields decoded."""
    d = dict(row)
    for key in ("skill_refs", "config", "skill_overrides", "artifacts_produced", "metadata"):
        if isinstance(d.get(key), str):
            d[key] = json.loads(d[key])
    return d


@router.get("/", response_class=HTMLResponse)
def pipeline_view(request: Request, project_id: str | None = None) -> HTMLResponse:
    """Pipeline kanban board."""
    conn = database.get_connection(str(DB_PATH))
    try:
        projects = [_row_to_dict(r) for r in database.list_projects(conn)]
        tasks = database.list_tasks(conn, project_id=project_id)
        task_list = [_row_to_dict(t) for t in tasks]

        # Group tasks into columns
        columns: dict[str, list[dict]] = {
            "backlog": [],
            "spec": [],
            "plan": [],
            "implement": [],
            "review": [],
            "done": [],
        }
        for t in task_list:
            status = t["status"]
            stage = t.get("current_stage")
            if status == "backlog":
                columns["backlog"].append(t)
            elif status == "done":
                columns["done"].append(t)
            elif stage and stage in columns:
                columns[stage].append(t)
            else:
                # active/paused/needs_human/failed without a stage go to backlog column
                columns["backlog"].append(t)

        # Get stage runs for active tasks to show attempt info
        stage_run_info: dict[str, dict] = {}
        for t in task_list:
            if t["status"] in ("active", "needs_human", "paused"):
                runs = database.list_stage_runs(conn, task_id=t["id"])
                if runs:
                    latest = _row_to_dict(runs[-1])
                    stage_run_info[t["id"]] = latest

        return templates.TemplateResponse(request, "pipeline.html", {
            "columns": columns,
            "column_names": ["backlog"] + STAGES + ["done"],
            "projects": projects,
            "selected_project_id": project_id,
            "stage_run_info": stage_run_info,
        })
    finally:
        conn.close()


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
def task_detail_page(request: Request, task_id: str) -> HTMLResponse:
    """Task detail page with stage run history."""
    conn = database.get_connection(str(DB_PATH))
    try:
        task_row = database.get_task(conn, task_id)
        if task_row is None:
            return HTMLResponse(content="Task not found", status_code=404)
        task = _row_to_dict(task_row)

        project_row = database.get_project(conn, task["project_id"])
        project = _row_to_dict(project_row) if project_row else {}

        stage_runs = [_row_to_dict(r) for r in database.list_stage_runs(conn, task_id=task_id)]

        return templates.TemplateResponse(request, "task_detail.html", {
            "task": task,
            "project": project,
            "stage_runs": stage_runs,
        })
    finally:
        conn.close()


@router.get("/backlog", response_class=HTMLResponse)
def backlog_page(request: Request) -> HTMLResponse:
    """Backlog management page."""
    conn = database.get_connection(str(DB_PATH))
    try:
        projects = [_row_to_dict(r) for r in database.list_projects(conn)]
        tasks = [_row_to_dict(t) for t in database.list_tasks(conn, status="backlog")]

        # Group tasks by project
        by_project: dict[str, list[dict]] = {}
        project_map: dict[str, str] = {p["id"]: p["name"] for p in projects}
        for t in tasks:
            pname = project_map.get(t["project_id"], "Unknown")
            by_project.setdefault(pname, []).append(t)

        return templates.TemplateResponse(request, "backlog.html", {
            "projects": projects,
            "tasks_by_project": by_project,
        })
    finally:
        conn.close()


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    """Settings page (view-only in v0.1)."""
    from forge.config import get_settings

    conn = database.get_connection(str(DB_PATH))
    try:
        projects = [_row_to_dict(r) for r in database.list_projects(conn)]
        settings = get_settings()
        return templates.TemplateResponse(request, "settings.html", {
            "projects": projects,
            "settings": settings,
        })
    finally:
        conn.close()


@router.get("/logs", response_class=HTMLResponse)
def logs_page(
    request: Request,
    level: str | None = None,
    task_id: str | None = None,
    project_id: str | None = None,
) -> HTMLResponse:
    """Run log page."""
    conn = database.get_connection(str(DB_PATH))
    try:
        projects = [_row_to_dict(r) for r in database.list_projects(conn)]
        logs = [
            _row_to_dict(r) for r in database.get_logs(
                conn, level=level, task_id=task_id, project_id=project_id, limit=200,
            )
        ]
        return templates.TemplateResponse(request, "logs.html", {
            "logs": logs,
            "projects": projects,
            "selected_level": level,
            "selected_task_id": task_id,
            "selected_project_id": project_id,
        })
    finally:
        conn.close()
