"""API routes for project management."""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException

from forge import database
from forge.config import DB_PATH
from forge.models import ProjectCreate, ProjectResponse, ProjectUpdate

router = APIRouter(prefix="/api/projects", tags=["projects"])


def _row_to_project(row) -> dict:
    """Convert a sqlite3.Row to a ProjectResponse-compatible dict."""
    import json

    d = dict(row)
    if isinstance(d.get("skill_refs"), str):
        d["skill_refs"] = json.loads(d["skill_refs"])
    if isinstance(d.get("config"), str):
        d["config"] = json.loads(d["config"])
    return d


@router.get("", response_model=list[ProjectResponse])
def list_projects() -> list[dict]:
    conn = database.get_connection(str(DB_PATH))
    try:
        rows = database.list_projects(conn)
        return [_row_to_project(r) for r in rows]
    finally:
        conn.close()


@router.post("", response_model=ProjectResponse, status_code=201)
def create_project(body: ProjectCreate) -> dict:
    # Validate repo_path exists
    if not os.path.isdir(body.repo_path):
        raise HTTPException(
            status_code=400, detail=f"repo_path does not exist: {body.repo_path}"
        )

    conn = database.get_connection(str(DB_PATH))
    try:
        # Check name uniqueness
        existing = database.get_project_by_name(conn, body.name)
        if existing:
            raise HTTPException(
                status_code=409, detail=f"Project name '{body.name}' already exists"
            )

        project_id = database.insert_project(
            conn,
            name=body.name,
            repo_path=body.repo_path,
            default_branch=body.default_branch,
            gate_dir=body.gate_dir,
            skill_refs=body.skill_refs,
            config=body.config,
        )
        row = database.get_project(conn, project_id)
        return _row_to_project(row)
    finally:
        conn.close()


@router.get("/{project_id}", response_model=ProjectResponse)
def get_project(project_id: str) -> dict:
    conn = database.get_connection(str(DB_PATH))
    try:
        row = database.get_project(conn, project_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Project not found")
        return _row_to_project(row)
    finally:
        conn.close()


@router.patch("/{project_id}", response_model=ProjectResponse)
def update_project(project_id: str, body: ProjectUpdate) -> dict:
    conn = database.get_connection(str(DB_PATH))
    try:
        row = database.get_project(conn, project_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Project not found")

        updates = body.model_dump(exclude_unset=True)
        if not updates:
            return _row_to_project(row)

        # Validate repo_path if being updated
        if "repo_path" in updates and not os.path.isdir(updates["repo_path"]):
            raise HTTPException(
                status_code=400,
                detail=f"repo_path does not exist: {updates['repo_path']}",
            )

        # Check name uniqueness if being updated
        if "name" in updates:
            existing = database.get_project_by_name(conn, updates["name"])
            if existing and existing["id"] != project_id:
                raise HTTPException(
                    status_code=409,
                    detail=f"Project name '{updates['name']}' already exists",
                )

        database.update_project(conn, project_id, **updates)
        updated_row = database.get_project(conn, project_id)
        return _row_to_project(updated_row)
    finally:
        conn.close()
