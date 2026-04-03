"""Tests for the MCP server tools."""

from __future__ import annotations

import json

import pytest

from mcp.server.fastmcp import FastMCP

from forge import database
from forge.mcp_server import (
    activate_task,
    cancel_task,
    create_task,
    create_task_batch,
    delete_task,
    get_completed_tasks,
    get_project_backlog,
    get_task_detail,
    list_projects,
    mcp,
    pause_task,
    reset_task,
    resume_task,
    retry_task,
    update_task,
)


@pytest.fixture()
def project_id():
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
            conn,
            project_id=project_id,
            title="High priority",
            priority=5,
        )
        ids["backlog_p3"] = database.insert_task(
            conn,
            project_id=project_id,
            title="Medium priority",
            priority=3,
        )

        # Active task with priority 1
        ids["active_p1"] = database.insert_task(
            conn,
            project_id=project_id,
            title="Active task",
            priority=1,
        )
        database.update_task(
            conn, ids["active_p1"], status="active", current_stage="implement"
        )

        # Done task
        ids["done"] = database.insert_task(
            conn,
            project_id=project_id,
            title="Done task",
            priority=2,
        )
        database.update_task(
            conn, ids["done"], status="done", completed_at="2026-01-15T10:00:00Z"
        )

        # Cancelled task
        ids["cancelled"] = database.insert_task(
            conn,
            project_id=project_id,
            title="Cancelled task",
            priority=2,
        )
        database.update_task(conn, ids["cancelled"], status="cancelled")

        # Failed task
        ids["failed"] = database.insert_task(
            conn,
            project_id=project_id,
            title="Failed task",
            priority=2,
        )
        database.update_task(conn, ids["failed"], status="failed")

        # Epic task with children
        ids["epic"] = database.insert_task(
            conn,
            project_id=project_id,
            title="Epic task",
            priority=4,
            flow="epic",
            epic_status="decomposed",
        )
        ids["child1"] = database.insert_task(
            conn,
            project_id=project_id,
            title="Child 1",
            priority=2,
            parent_task_id=ids["epic"],
        )
        ids["child2"] = database.insert_task(
            conn,
            project_id=project_id,
            title="Child 2",
            priority=1,
            parent_task_id=ids["epic"],
        )

        # Stage run for the active task
        ids["stage_run"] = database.insert_stage_run(
            conn,
            task_id=ids["active_p1"],
            stage="implement",
            attempt=1,
            status="running",
        )

        return ids
    finally:
        conn.close()


class TestListProjects:
    def test_returns_all_projects(self, project_id):
        conn = database.get_connection()
        try:
            database.insert_project(
                conn,
                name="SecondProject",
                repo_path="/tmp/second",
                default_branch="develop",
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
    def test_excludes_done_cancelled_and_failed(self, populated_db):
        result = get_project_backlog(populated_db["project_id"])
        ids_returned = {t["id"] for t in result}
        assert populated_db["done"] not in ids_returned
        assert populated_db["cancelled"] not in ids_returned
        assert populated_db["failed"] not in ids_returned
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
            assert "depends_on" in task

    def test_depends_on_includes_blocking_links(self, populated_db):
        conn = database.get_connection()
        try:
            # backlog_p3 blocks backlog_p5 → backlog_p5's depends_on should list this link
            database.insert_task_link(
                conn,
                source_task_id=populated_db["backlog_p3"],
                target_task_id=populated_db["backlog_p5"],
                link_type="blocks",
            )
        finally:
            conn.close()

        result = get_project_backlog(populated_db["project_id"])
        by_id = {t["id"]: t for t in result}
        blocked = by_id[populated_db["backlog_p5"]]
        assert len(blocked["depends_on"]) == 1
        assert blocked["depends_on"][0]["source_task_id"] == populated_db["backlog_p3"]
        # The blocker itself should have no depends_on entries
        blocker = by_id[populated_db["backlog_p3"]]
        assert blocker["depends_on"] == []

    def test_nonexistent_project_returns_empty(self):
        result = get_project_backlog("nonexistent-project-id")
        assert result == []


class TestGetTaskDetail:
    def test_returns_full_task_record(self, populated_db):
        result = get_task_detail(populated_db["backlog_p5"])
        assert result is not None
        for field in (
            "id",
            "title",
            "description",
            "priority",
            "status",
            "current_stage",
            "flow",
            "branch_name",
            "spec_path",
            "plan_path",
            "review_path",
            "created_at",
            "updated_at",
        ):
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
                    conn,
                    project_id=project_id,
                    title=f"Done task {i}",
                    priority=0,
                )
                database.update_task(
                    conn,
                    tid,
                    status="done",
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
                    conn,
                    project_id=project_id,
                    title=f"Done task {i}",
                    priority=0,
                )
                database.update_task(
                    conn,
                    tid,
                    status="done",
                    completed_at=f"2026-02-{(i % 28) + 1:02d}T00:00:00Z",
                )
        finally:
            conn.close()

        result = get_completed_tasks(project_id)
        assert len(result) == 20

    def test_ordered_by_completed_at_descending(self, project_id):
        conn = database.get_connection()
        try:
            for i, date in enumerate(
                ["2026-01-01T00:00:00Z", "2026-03-01T00:00:00Z", "2026-02-01T00:00:00Z"]
            ):
                tid = database.insert_task(
                    conn,
                    project_id=project_id,
                    title=f"Done task {i}",
                    priority=0,
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

    @pytest.mark.asyncio
    async def test_tools_registered(self):
        tool_names = {tool.name for tool in await mcp.list_tools()}
        assert "list_projects" in tool_names
        assert "get_project_backlog" in tool_names
        assert "get_task_detail" in tool_names
        assert "get_completed_tasks" in tool_names
        assert "create_task" in tool_names
        assert "create_task_batch" in tool_names
        assert "update_task" in tool_names
        assert "delete_task" in tool_names
        assert "activate_task" in tool_names
        assert "pause_task" in tool_names
        assert "resume_task" in tool_names
        assert "retry_task" in tool_names
        assert "reset_task" in tool_names
        assert "cancel_task" in tool_names


class TestCreateTask:
    def test_create_task_all_fields(self, project_id):
        result = create_task(
            project_id=project_id,
            title="My Task",
            description="A description",
            priority=5,
            flow="quick",
        )
        assert "id" in result
        assert result["title"] == "My Task"
        assert result["description"] == "A description"
        assert result["priority"] == 5
        assert result["flow"] == "quick"
        # Verify in DB
        conn = database.get_connection()
        try:
            row = database.get_task(conn, result["id"])
            assert row is not None
            assert row["title"] == "My Task"
        finally:
            conn.close()

    def test_create_task_defaults(self, project_id):
        result = create_task(project_id=project_id, title="Minimal Task")
        assert result["description"] == ""
        assert result["priority"] == 0
        assert result["flow"] == "standard"

    def test_create_task_invalid_project(self):
        result = create_task(project_id="nonexistent-id", title="Task")
        assert "error" in result
        assert "project" in result["error"].lower()

    def test_create_task_empty_title(self, project_id):
        result = create_task(project_id=project_id, title="")
        assert "error" in result
        assert "title" in result["error"].lower()

    def test_create_task_invalid_flow(self, project_id):
        result = create_task(project_id=project_id, title="Task", flow="invalid")
        assert "error" in result
        assert "flow" in result["error"].lower()

    def test_create_task_with_depends_on(self, project_id):
        t1 = create_task(project_id=project_id, title="Dep 1")
        t2 = create_task(project_id=project_id, title="Dep 2")
        t3 = create_task(
            project_id=project_id,
            title="Blocked Task",
            depends_on=[t1["id"], t2["id"]],
        )
        assert "id" in t3
        conn = database.get_connection()
        try:
            links = database.get_task_links(conn, t3["id"])
            blocking = [
                lnk
                for lnk in links
                if lnk["link_type"] == "blocks" and lnk["target_task_id"] == t3["id"]
            ]
            source_ids = {lnk["source_task_id"] for lnk in blocking}
            assert source_ids == {t1["id"], t2["id"]}
        finally:
            conn.close()

    def test_create_task_depends_on_nonexistent(self, project_id):
        result = create_task(
            project_id=project_id,
            title="Task",
            depends_on=["00000000-0000-0000-0000-000000000000"],
        )
        assert "error" in result
        # Task should NOT have been created
        conn = database.get_connection()
        try:
            rows = database.list_tasks(conn, project_id=project_id)
            assert all(r["title"] != "Task" for r in rows)
        finally:
            conn.close()

    def test_create_task_depends_on_wrong_project(self, project_id):
        # Create a second project and a task in it
        conn = database.get_connection()
        try:
            pid2 = database.insert_project(
                conn,
                name="OtherProject",
                repo_path="/tmp/other",
                default_branch="main",
            )
            other_task_id = database.insert_task(
                conn,
                project_id=pid2,
                title="Other Task",
            )
        finally:
            conn.close()

        result = create_task(
            project_id=project_id,
            title="Task",
            depends_on=[other_task_id],
        )
        assert "error" in result
        assert "different project" in result["error"].lower()


class TestCreateTaskBatch:
    def test_batch_basic(self, project_id):
        tasks_json = json.dumps(
            [
                {"title": "Task A"},
                {"title": "Task B", "priority": 3},
                {"title": "Task C", "flow": "quick", "description": "desc"},
            ]
        )
        result = create_task_batch(project_id=project_id, tasks=tasks_json)
        assert isinstance(result, list)
        assert len(result) == 3
        assert result[0]["title"] == "Task A"
        assert result[1]["priority"] == 3
        assert result[2]["flow"] == "quick"

    def test_batch_title_dependency(self, project_id):
        tasks_json = json.dumps(
            [
                {"title": "Foundation"},
                {"title": "Walls", "depends_on": ["Foundation"]},
                {"title": "Roof", "depends_on": ["Walls"]},
            ]
        )
        result = create_task_batch(project_id=project_id, tasks=tasks_json)
        assert isinstance(result, list)
        assert len(result) == 3

        # Verify links
        conn = database.get_connection()
        try:
            roof_id = result[2]["id"]
            walls_id = result[1]["id"]
            foundation_id = result[0]["id"]

            # Walls blocked by Foundation
            links = database.get_task_links(conn, walls_id)
            blocking = [
                lnk
                for lnk in links
                if lnk["link_type"] == "blocks" and lnk["target_task_id"] == walls_id
            ]
            assert len(blocking) == 1
            assert blocking[0]["source_task_id"] == foundation_id

            # Roof blocked by Walls
            links = database.get_task_links(conn, roof_id)
            blocking = [
                lnk
                for lnk in links
                if lnk["link_type"] == "blocks" and lnk["target_task_id"] == roof_id
            ]
            assert len(blocking) == 1
            assert blocking[0]["source_task_id"] == walls_id
        finally:
            conn.close()

    def test_batch_atomic_rollback(self, project_id):
        tasks_json = json.dumps(
            [
                {"title": "Good Task 1"},
                {"title": "Good Task 2"},
                {"title": "Bad Task", "flow": "invalid"},
            ]
        )
        result = create_task_batch(project_id=project_id, tasks=tasks_json)
        assert isinstance(result, dict)
        assert "error" in result

        # None of the tasks should exist
        conn = database.get_connection()
        try:
            rows = database.list_tasks(conn, project_id=project_id)
            titles = {r["title"] for r in rows}
            assert "Good Task 1" not in titles
            assert "Good Task 2" not in titles
            assert "Bad Task" not in titles
        finally:
            conn.close()

    def test_batch_circular_dependency(self, project_id):
        tasks_json = json.dumps(
            [
                {"title": "A", "depends_on": ["B"]},
                {"title": "B", "depends_on": ["A"]},
            ]
        )
        result = create_task_batch(project_id=project_id, tasks=tasks_json)
        assert isinstance(result, dict)
        assert "error" in result
        assert "circular" in result["error"].lower()

    def test_batch_multiple_missing_titles(self, project_id):
        """Two tasks with no title should get 'missing title', not 'duplicate'."""
        tasks_json = json.dumps(
            [
                {"description": "a"},
                {"description": "b"},
            ]
        )
        result = create_task_batch(project_id=project_id, tasks=tasks_json)
        assert isinstance(result, dict)
        assert "error" in result
        assert "missing a title" in result["error"].lower()

    def test_batch_duplicate_titles(self, project_id):
        tasks_json = json.dumps(
            [
                {"title": "Same Title"},
                {"title": "Same Title"},
            ]
        )
        result = create_task_batch(project_id=project_id, tasks=tasks_json)
        assert isinstance(result, dict)
        assert "error" in result
        assert "duplicate" in result["error"].lower()

    def test_batch_mixed_dependencies(self, project_id):
        # Create an existing task
        existing = create_task(project_id=project_id, title="Existing Task")
        assert "id" in existing

        tasks_json = json.dumps(
            [
                {"title": "Batch Task 1"},
                {"title": "Batch Task 2", "depends_on": [existing["id"]]},
                {"title": "Batch Task 3", "depends_on": ["Batch Task 1"]},
            ]
        )
        result = create_task_batch(project_id=project_id, tasks=tasks_json)
        assert isinstance(result, list)
        assert len(result) == 3

        conn = database.get_connection()
        try:
            # Batch Task 2 blocked by existing task (UUID ref)
            links = database.get_task_links(conn, result[1]["id"])
            blocking = [
                lnk
                for lnk in links
                if lnk["link_type"] == "blocks"
                and lnk["target_task_id"] == result[1]["id"]
            ]
            assert len(blocking) == 1
            assert blocking[0]["source_task_id"] == existing["id"]

            # Batch Task 3 blocked by Batch Task 1 (title ref)
            links = database.get_task_links(conn, result[2]["id"])
            blocking = [
                lnk
                for lnk in links
                if lnk["link_type"] == "blocks"
                and lnk["target_task_id"] == result[2]["id"]
            ]
            assert len(blocking) == 1
            assert blocking[0]["source_task_id"] == result[0]["id"]
        finally:
            conn.close()

    def test_batch_invalid_project(self):
        tasks_json = json.dumps([{"title": "Task"}])
        result = create_task_batch(project_id="nonexistent", tasks=tasks_json)
        assert isinstance(result, dict)
        assert "error" in result
        assert "project" in result["error"].lower()

    def test_batch_depends_on_nonexistent_uuid(self, project_id):
        tasks_json = json.dumps(
            [
                {
                    "title": "Task",
                    "depends_on": ["00000000-0000-0000-0000-000000000000"],
                },
            ]
        )
        result = create_task_batch(project_id=project_id, tasks=tasks_json)
        assert isinstance(result, dict)
        assert "error" in result

        # No tasks created
        conn = database.get_connection()
        try:
            rows = database.list_tasks(conn, project_id=project_id)
            assert all(r["title"] != "Task" for r in rows)
        finally:
            conn.close()

    def test_batch_depends_on_nonexistent_title(self, project_id):
        tasks_json = json.dumps(
            [
                {"title": "Task", "depends_on": ["No Such Title"]},
            ]
        )
        result = create_task_batch(project_id=project_id, tasks=tasks_json)
        assert isinstance(result, dict)
        assert "error" in result

    def test_batch_self_dependency_by_title(self, project_id):
        tasks_json = json.dumps(
            [
                {"title": "Self", "depends_on": ["Self"]},
            ]
        )
        result = create_task_batch(project_id=project_id, tasks=tasks_json)
        assert isinstance(result, dict)
        assert "error" in result
        assert "circular" in result["error"].lower()

    def test_batch_empty_array(self, project_id):
        result = create_task_batch(project_id=project_id, tasks="[]")
        assert isinstance(result, dict)
        assert "error" in result

    def test_batch_invalid_json(self, project_id):
        result = create_task_batch(project_id=project_id, tasks="not json")
        assert isinstance(result, dict)
        assert "error" in result

    def test_batch_non_list_json(self, project_id):
        result = create_task_batch(project_id=project_id, tasks='{"title": "x"}')
        assert isinstance(result, dict)
        assert "error" in result

    def test_batch_missing_title(self, project_id):
        tasks_json = json.dumps([{"description": "no title"}])
        result = create_task_batch(project_id=project_id, tasks=tasks_json)
        assert isinstance(result, dict)
        assert "error" in result
        assert "title" in result["error"].lower()

    def test_batch_non_dict_items(self, project_id):
        result = create_task_batch(project_id=project_id, tasks="[1, 2]")
        assert isinstance(result, dict)
        assert "error" in result
        assert "JSON object" in result["error"]

    def test_batch_depends_on_wrong_project(self, project_id):
        # Create a second project and a task in it
        conn = database.get_connection()
        try:
            pid2 = database.insert_project(
                conn,
                name="OtherProject2",
                repo_path="/tmp/other2",
                default_branch="main",
            )
            other_task_id = database.insert_task(
                conn,
                project_id=pid2,
                title="Other Task",
            )
        finally:
            conn.close()

        tasks_json = json.dumps(
            [{"title": "Task A", "depends_on": [other_task_id]}]
        )
        result = create_task_batch(project_id=project_id, tasks=tasks_json)
        assert isinstance(result, dict)
        assert "error" in result
        assert "different project" in result["error"].lower()


class TestUpdateTask:
    def test_update_title(self, project_id):
        task = create_task(project_id=project_id, title="Original")
        result = update_task(task_id=task["id"], title="Updated")
        assert result["title"] == "Updated"

    def test_update_description_and_priority(self, project_id):
        task = create_task(project_id=project_id, title="Task")
        result = update_task(task_id=task["id"], description="New desc", priority=10)
        assert result["description"] == "New desc"
        assert result["priority"] == 10

    def test_update_flow_on_backlog(self, project_id):
        task = create_task(project_id=project_id, title="Task", flow="standard")
        result = update_task(task_id=task["id"], flow="quick")
        assert result["flow"] == "quick"

    def test_update_flow_on_non_backlog_rejected(self, project_id):
        task = create_task(project_id=project_id, title="Task")
        activate_task(task_id=task["id"])
        result = update_task(task_id=task["id"], flow="quick")
        assert "error" in result
        assert "backlog" in result["error"].lower()

    def test_update_epic_status_on_epic(self, project_id):
        task = create_task(project_id=project_id, title="Epic", flow="epic")
        result = update_task(task_id=task["id"], epic_status="decomposed")
        assert result["epic_status"] == "decomposed"

    def test_update_epic_status_on_non_epic_rejected(self, project_id):
        task = create_task(project_id=project_id, title="Task", flow="standard")
        result = update_task(task_id=task["id"], epic_status="decomposed")
        assert "error" in result
        assert "epic" in result["error"].lower()

    def test_update_invalid_epic_status_rejected(self, project_id):
        task = create_task(project_id=project_id, title="Epic", flow="epic")
        result = update_task(task_id=task["id"], epic_status="invalid_value")
        assert "error" in result
        assert "epic_status" in result["error"].lower()

    def test_update_empty_title_rejected(self, project_id):
        task = create_task(project_id=project_id, title="Task")
        result = update_task(task_id=task["id"], title="")
        assert "error" in result
        assert "title" in result["error"].lower()

    def test_update_invalid_flow_rejected(self, project_id):
        task = create_task(project_id=project_id, title="Task")
        result = update_task(task_id=task["id"], flow="nonexistent")
        assert "error" in result
        assert "flow" in result["error"].lower()

    def test_update_nonexistent_task(self):
        result = update_task(task_id="nonexistent-id")
        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_update_no_changes(self, project_id):
        task = create_task(project_id=project_id, title="Task")
        result = update_task(task_id=task["id"])
        assert result["title"] == "Task"
        assert "error" not in result


class TestDeleteTask:
    def test_delete_backlog_task(self, project_id):
        task = create_task(project_id=project_id, title="To Delete")
        result = delete_task(task_id=task["id"])
        assert result == {"deleted": True}
        # Verify gone
        detail = get_task_detail(task["id"])
        assert detail is None

    def test_delete_active_task_rejected(self, project_id):
        task = create_task(project_id=project_id, title="Task")
        activate_task(task_id=task["id"])
        result = delete_task(task_id=task["id"])
        assert "error" in result
        assert "backlog" in result["error"].lower()

    def test_delete_nonexistent_task(self):
        result = delete_task(task_id="nonexistent-id")
        assert "error" in result
        assert "not found" in result["error"].lower()


class TestActivateTask:
    def test_activate_backlog_task(self, project_id):
        task = create_task(project_id=project_id, title="Task")
        result = activate_task(task_id=task["id"])
        assert result["status"] == "active"
        assert result["current_stage"] == "spec"

    def test_activate_quick_flow(self, project_id):
        task = create_task(project_id=project_id, title="Quick Task", flow="quick")
        result = activate_task(task_id=task["id"])
        assert result["status"] == "active"
        assert result["current_stage"] == "implement"

    def test_activate_non_backlog_rejected(self, project_id):
        task = create_task(project_id=project_id, title="Task")
        activate_task(task_id=task["id"])
        result = activate_task(task_id=task["id"])
        assert "error" in result
        assert "backlog" in result["error"].lower()

    def test_activate_nonexistent_task(self):
        result = activate_task(task_id="nonexistent-id")
        assert "error" in result
        assert "not found" in result["error"].lower()


class TestPauseTask:
    def test_pause_active_task(self, project_id):
        task = create_task(project_id=project_id, title="Task")
        activate_task(task_id=task["id"])
        result = pause_task(task_id=task["id"])
        assert result["status"] == "paused"

    def test_pause_non_active_rejected(self, project_id):
        task = create_task(project_id=project_id, title="Task")
        result = pause_task(task_id=task["id"])
        assert "error" in result
        assert "active" in result["error"].lower()

    def test_pause_nonexistent_task(self):
        result = pause_task(task_id="nonexistent-id")
        assert "error" in result
        assert "not found" in result["error"].lower()


class TestResumeTask:
    def test_resume_needs_human_task(self, project_id):
        task = create_task(project_id=project_id, title="Task")
        activate_task(task_id=task["id"])
        # Set to needs_human
        conn = database.get_connection()
        try:
            database.update_task(conn, task["id"], status="needs_human")
        finally:
            conn.close()
        result = resume_task(task_id=task["id"])
        assert result["status"] == "active"
        detail = get_task_detail(task["id"])
        assert len(detail["stage_runs"]) == 2

    def test_resume_non_needs_human_rejected(self, project_id):
        task = create_task(project_id=project_id, title="Task")
        result = resume_task(task_id=task["id"])
        assert "error" in result
        assert "needs_human" in result["error"].lower()

    def test_resume_nonexistent_task(self):
        result = resume_task(task_id="nonexistent-id")
        assert "error" in result
        assert "not found" in result["error"].lower()


class TestRetryTask:
    def test_retry_active_task(self, project_id):
        task = create_task(project_id=project_id, title="Task")
        activate_task(task_id=task["id"])
        result = retry_task(task_id=task["id"])
        assert result["status"] == "active"
        # Verify new stage_run created
        detail = get_task_detail(task["id"])
        assert len(detail["stage_runs"]) == 2

    def test_retry_needs_human_task(self, project_id):
        task = create_task(project_id=project_id, title="Task")
        activate_task(task_id=task["id"])
        conn = database.get_connection()
        try:
            database.update_task(conn, task["id"], status="needs_human")
        finally:
            conn.close()
        result = retry_task(task_id=task["id"])
        assert result["status"] == "active"

    def test_retry_backlog_rejected(self, project_id):
        task = create_task(project_id=project_id, title="Task")
        result = retry_task(task_id=task["id"])
        assert "error" in result

    def test_retry_nonexistent_task(self):
        result = retry_task(task_id="nonexistent-id")
        assert "error" in result
        assert "not found" in result["error"].lower()


class TestResetTask:
    def test_reset_failed_task(self, project_id):
        task = create_task(project_id=project_id, title="Task")
        activate_task(task_id=task["id"])
        conn = database.get_connection()
        try:
            database.update_task(conn, task["id"], status="failed")
        finally:
            conn.close()
        result = reset_task(task_id=task["id"])
        assert result["status"] == "active"
        assert result["current_stage"] == "spec"

    def test_reset_with_from_stage(self, project_id):
        task = create_task(project_id=project_id, title="Task")
        activate_task(task_id=task["id"])
        conn = database.get_connection()
        try:
            database.update_task(conn, task["id"], status="failed")
        finally:
            conn.close()
        result = reset_task(task_id=task["id"], from_stage="plan")
        assert result["current_stage"] == "plan"

    def test_reset_invalid_stage_rejected(self, project_id):
        task = create_task(project_id=project_id, title="Task")
        activate_task(task_id=task["id"])
        conn = database.get_connection()
        try:
            database.update_task(conn, task["id"], status="failed")
        finally:
            conn.close()
        result = reset_task(task_id=task["id"], from_stage="nonexistent_stage")
        assert "error" in result
        assert "invalid stage" in result["error"].lower()

    def test_reset_active_task_rejected(self, project_id):
        task = create_task(project_id=project_id, title="Task")
        activate_task(task_id=task["id"])
        result = reset_task(task_id=task["id"])
        assert "error" in result
        assert "status" in result["error"].lower()

    def test_reset_with_running_stage_run_rejected(self, project_id):
        task = create_task(project_id=project_id, title="Task")
        activate_task(task_id=task["id"])
        conn = database.get_connection()
        try:
            # Set status to paused (resettable) but leave a running stage_run
            database.update_task(conn, task["id"], status="paused")
            # Update the existing queued stage_run to running
            stage_runs = database.list_stage_runs(conn, task_id=task["id"])
            database.update_stage_run(conn, stage_runs[0]["id"], status="running")
        finally:
            conn.close()
        result = reset_task(task_id=task["id"])
        assert "error" in result
        assert "in progress" in result["error"].lower()

    def test_reset_nonexistent_task(self):
        result = reset_task(task_id="nonexistent-id")
        assert "error" in result
        assert "not found" in result["error"].lower()


class TestCancelTask:
    def test_cancel_backlog_task(self, project_id):
        task = create_task(project_id=project_id, title="Task")
        result = cancel_task(task_id=task["id"])
        assert result["status"] == "cancelled"

    def test_cancel_active_task(self, project_id):
        task = create_task(project_id=project_id, title="Task")
        activate_task(task_id=task["id"])
        # Set the stage_run to running
        conn = database.get_connection()
        try:
            stage_runs = database.list_stage_runs(conn, task_id=task["id"])
            database.update_stage_run(conn, stage_runs[0]["id"], status="running")
        finally:
            conn.close()
        result = cancel_task(task_id=task["id"])
        assert result["status"] == "cancelled"
        # Verify running stage_runs marked as error
        conn = database.get_connection()
        try:
            stage_runs = database.list_stage_runs(conn, task_id=task["id"])
            for sr in stage_runs:
                if sr["id"] == stage_runs[0]["id"]:
                    assert sr["status"] == "error"
        finally:
            conn.close()

    def test_cancel_with_reason(self, project_id):
        task = create_task(project_id=project_id, title="Task")
        result = cancel_task(task_id=task["id"], reason="No longer needed")
        assert result["status"] == "cancelled"
        # Verify log entry
        conn = database.get_connection()
        try:
            logs = conn.execute(
                "SELECT message FROM run_log WHERE task_id = ?", (task["id"],)
            ).fetchall()
            messages = [log[0] for log in logs]
            assert any("No longer needed" in m for m in messages)
        finally:
            conn.close()

    def test_cancel_done_task_rejected(self, project_id):
        task = create_task(project_id=project_id, title="Task")
        conn = database.get_connection()
        try:
            database.update_task(conn, task["id"], status="done")
        finally:
            conn.close()
        result = cancel_task(task_id=task["id"])
        assert "error" in result
        assert "status" in result["error"].lower()

    def test_cancel_nonexistent_task(self):
        result = cancel_task(task_id="nonexistent-id")
        assert "error" in result
        assert "not found" in result["error"].lower()
