"""Tests for the MCP server tools."""

from __future__ import annotations

import pytest

from mcp.server.fastmcp import FastMCP

from forge import database
from forge.mcp_server import (
    get_completed_tasks,
    get_project_backlog,
    get_task_detail,
    list_projects,
    mcp,
)


@pytest.fixture()
def project_id(tmp_path):
    """Insert a test project and return its ID."""
    conn = database.get_connection()
    try:
        pid = database.insert_project(
            conn,
            name="TestProject",
            repo_path="/tmp/test-repo",
            default_branch="main",
            gate_dir="gates",
            config={"key": "value"},
        )
        return pid
    finally:
        conn.close()


@pytest.fixture()
def populated_db(project_id):
    """Insert a mix of tasks for testing. Returns a dict of IDs."""
    conn = database.get_connection()
    try:
        ids = {"project_id": project_id}

        # Backlog tasks with different priorities
        ids["backlog_p5"] = database.insert_task(
            conn, project_id=project_id, title="High priority", priority=5,
        )
        ids["backlog_p3"] = database.insert_task(
            conn, project_id=project_id, title="Medium priority", priority=3,
        )

        # Active task with priority 1
        ids["active_p1"] = database.insert_task(
            conn, project_id=project_id, title="Active task", priority=1,
        )
        database.update_task(conn, ids["active_p1"], status="active", current_stage="implement")

        # Done task
        ids["done"] = database.insert_task(
            conn, project_id=project_id, title="Done task", priority=2,
        )
        database.update_task(conn, ids["done"], status="done", completed_at="2026-01-15T10:00:00Z")

        # Cancelled task
        ids["cancelled"] = database.insert_task(
            conn, project_id=project_id, title="Cancelled task", priority=2,
        )
        database.update_task(conn, ids["cancelled"], status="cancelled")

        # Epic task with children
        ids["epic"] = database.insert_task(
            conn, project_id=project_id, title="Epic task", priority=4,
            flow="epic", epic_status="decomposed",
        )
        ids["child1"] = database.insert_task(
            conn, project_id=project_id, title="Child 1", priority=2,
            parent_task_id=ids["epic"],
        )
        ids["child2"] = database.insert_task(
            conn, project_id=project_id, title="Child 2", priority=1,
            parent_task_id=ids["epic"],
        )

        # Stage run for the active task
        ids["stage_run"] = database.insert_stage_run(
            conn, task_id=ids["active_p1"], stage="implement", attempt=1, status="running",
        )

        return ids
    finally:
        conn.close()


class TestListProjects:
    def test_returns_all_projects(self, project_id):
        conn = database.get_connection()
        try:
            database.insert_project(
                conn, name="SecondProject", repo_path="/tmp/second", default_branch="develop",
            )
        finally:
            conn.close()

        result = list_projects()
        assert len(result) == 2
        for proj in result:
            assert "id" in proj
            assert "name" in proj
            assert "repo_path" in proj
            assert "default_branch" in proj
            assert "config" in proj

    def test_returns_empty_when_no_projects(self):
        result = list_projects()
        assert result == []

    def test_parses_json_config(self, project_id):
        result = list_projects()
        assert len(result) == 1
        assert result[0]["config"] == {"key": "value"}


class TestGetProjectBacklog:
    def test_excludes_done_and_cancelled(self, populated_db):
        result = get_project_backlog(populated_db["project_id"])
        ids_returned = {t["id"] for t in result}
        assert populated_db["done"] not in ids_returned
        assert populated_db["cancelled"] not in ids_returned
        assert populated_db["backlog_p5"] in ids_returned
        assert populated_db["active_p1"] in ids_returned

    def test_ordered_by_priority_descending(self, populated_db):
        result = get_project_backlog(populated_db["project_id"])
        priorities = [t["priority"] for t in result]
        assert priorities == sorted(priorities, reverse=True)

    def test_includes_required_fields(self, populated_db):
        result = get_project_backlog(populated_db["project_id"])
        assert len(result) > 0
        for task in result:
            assert "id" in task
            assert "title" in task
            assert "status" in task
            assert "priority" in task
            assert "flow" in task
            assert "current_stage" in task
            assert "parent_task_id" in task
            assert "epic_status" in task

    def test_nonexistent_project_returns_empty(self):
        result = get_project_backlog("nonexistent-project-id")
        assert result == []


class TestGetTaskDetail:
    def test_returns_full_task_record(self, populated_db):
        result = get_task_detail(populated_db["backlog_p5"])
        assert result is not None
        for field in ("id", "title", "description", "priority", "status",
                      "current_stage", "flow", "branch_name", "spec_path",
                      "plan_path", "review_path", "created_at", "updated_at"):
            assert field in result

    def test_includes_stage_runs(self, populated_db):
        result = get_task_detail(populated_db["active_p1"])
        assert result is not None
        assert "stage_runs" in result
        assert len(result["stage_runs"]) == 1
        assert result["stage_runs"][0]["stage"] == "implement"

    def test_epic_includes_child_tasks(self, populated_db):
        result = get_task_detail(populated_db["epic"])
        assert result is not None
        assert "child_tasks" in result
        assert len(result["child_tasks"]) == 2

    def test_non_epic_has_no_child_tasks(self, populated_db):
        result = get_task_detail(populated_db["backlog_p5"])
        assert result is not None
        assert "child_tasks" not in result

    def test_nonexistent_task_returns_none(self):
        result = get_task_detail("nonexistent-task-id")
        assert result is None


class TestGetCompletedTasks:
    def test_returns_only_completed(self, populated_db):
        result = get_completed_tasks(populated_db["project_id"])
        assert len(result) == 1
        assert result[0]["id"] == populated_db["done"]

    def test_respects_limit(self, project_id):
        conn = database.get_connection()
        try:
            for i in range(5):
                tid = database.insert_task(
                    conn, project_id=project_id, title=f"Done task {i}", priority=0,
                )
                database.update_task(
                    conn, tid, status="done",
                    completed_at=f"2026-01-{10 + i:02d}T00:00:00Z",
                )
        finally:
            conn.close()

        result = get_completed_tasks(project_id, limit=3)
        assert len(result) == 3

    def test_default_limit_is_20(self, project_id):
        conn = database.get_connection()
        try:
            for i in range(25):
                tid = database.insert_task(
                    conn, project_id=project_id, title=f"Done task {i}", priority=0,
                )
                database.update_task(
                    conn, tid, status="done",
                    completed_at=f"2026-02-{(i % 28) + 1:02d}T00:00:00Z",
                )
        finally:
            conn.close()

        result = get_completed_tasks(project_id)
        assert len(result) == 20

    def test_ordered_by_completed_at_descending(self, project_id):
        conn = database.get_connection()
        try:
            for i, date in enumerate(["2026-01-01T00:00:00Z", "2026-03-01T00:00:00Z", "2026-02-01T00:00:00Z"]):
                tid = database.insert_task(
                    conn, project_id=project_id, title=f"Done task {i}", priority=0,
                )
                database.update_task(conn, tid, status="done", completed_at=date)
        finally:
            conn.close()

        result = get_completed_tasks(project_id)
        assert result[0]["completed_at"] == "2026-03-01T00:00:00Z"
        assert result[1]["completed_at"] == "2026-02-01T00:00:00Z"
        assert result[2]["completed_at"] == "2026-01-01T00:00:00Z"


class TestServerInit:
    def test_server_instance_created(self):
        assert isinstance(mcp, FastMCP)

    def test_tools_registered(self):
        # The server should have our four tools registered
        tool_names = {tool.name for tool in mcp._tool_manager.list_tools()}
        assert "list_projects" in tool_names
        assert "get_project_backlog" in tool_names
        assert "get_task_detail" in tool_names
        assert "get_completed_tasks" in tool_names
