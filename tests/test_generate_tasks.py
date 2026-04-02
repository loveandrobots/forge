"""Tests for the AI-generate endpoint and enhanced batch endpoint with depends_on."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from forge import database
from forge.dispatcher import DispatchResult
from forge.main import app


@pytest.fixture()
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture()
def project_id(client: TestClient, tmp_path) -> str:
    resp = client.post(
        "/api/projects",
        json={
            "name": "GenTestProject",
            "repo_path": str(tmp_path),
        },
    )
    return resp.json()["id"]


@pytest.fixture()
def project_with_skill(client: TestClient, tmp_path) -> str:
    """Create a project whose repo has the forge-task-writer skill installed."""
    skill_dir = tmp_path / ".claude" / "skills" / "forge-task-writer"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Forge Task Writer\nTest skill.")
    resp = client.post(
        "/api/projects",
        json={
            "name": "SkillProject",
            "repo_path": str(tmp_path),
        },
    )
    return resp.json()["id"]


# ---------------------------------------------------------------------------
# Generate endpoint tests
# ---------------------------------------------------------------------------


class TestGenerateEndpoint:
    def test_generate_calls_claude_and_returns_tasks(
        self, client: TestClient, project_with_skill: str
    ) -> None:
        """Test 1: generate endpoint calls Claude Code and returns parsed tasks."""
        tasks_json = json.dumps([
            {
                "title": "Add user model",
                "priority": 0,
                "description": "Create the user model",
                "depends_on": [],
            },
            {
                "title": "Add auth endpoint",
                "priority": 1,
                "description": "Create login endpoint",
                "depends_on": [0],
            },
        ])
        mock_result = DispatchResult(
            output=tasks_json, exit_code=0, duration_seconds=5.0, tokens_used=100
        )

        with patch(
            "forge.routers.tasks.dispatcher.dispatch_generate",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            resp = client.post(
                "/api/tasks/generate",
                json={
                    "project_id": project_with_skill,
                    "problem_description": "Need user auth",
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["tasks"]) == 2
        assert data["tasks"][0]["title"] == "Add user model"
        assert data["tasks"][1]["depends_on"] == [0]

    def test_generate_missing_skill(
        self, client: TestClient, project_id: str
    ) -> None:
        """Test 2: returns 400 when skill file is missing."""
        resp = client.post(
            "/api/tasks/generate",
            json={
                "project_id": project_id,
                "problem_description": "Need something",
            },
        )
        assert resp.status_code == 400
        assert "Skill file not found" in resp.json()["detail"]

    def test_generate_invalid_json(
        self, client: TestClient, project_with_skill: str
    ) -> None:
        """Test 3: returns 422 on invalid JSON from Claude."""
        mock_result = DispatchResult(
            output="Sorry, I couldn't parse that.",
            exit_code=0,
            duration_seconds=3.0,
        )

        with patch(
            "forge.routers.tasks.dispatcher.dispatch_generate",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            resp = client.post(
                "/api/tasks/generate",
                json={
                    "project_id": project_with_skill,
                    "problem_description": "Do something",
                },
            )

        assert resp.status_code == 422
        assert "Failed to parse JSON" in resp.json()["detail"]

    def test_generate_claude_failure(
        self, client: TestClient, project_with_skill: str
    ) -> None:
        """Test 4: returns 502 when Claude Code fails."""
        mock_result = DispatchResult(
            output="",
            exit_code=1,
            duration_seconds=1.0,
            error="Process crashed",
        )

        with patch(
            "forge.routers.tasks.dispatcher.dispatch_generate",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            resp = client.post(
                "/api/tasks/generate",
                json={
                    "project_id": project_with_skill,
                    "problem_description": "Do something",
                },
            )

        assert resp.status_code == 502
        assert resp.json()["detail"] == "Process crashed"

    def test_generate_nonexistent_project(self, client: TestClient) -> None:
        """Returns 404 when project does not exist."""
        resp = client.post(
            "/api/tasks/generate",
            json={
                "project_id": "nonexistent-id",
                "problem_description": "Something",
            },
        )
        assert resp.status_code == 404
        assert "Project not found" in resp.json()["detail"]

    def test_generate_empty_output(
        self, client: TestClient, project_with_skill: str
    ) -> None:
        """Returns 422 when Claude Code returns empty output."""
        mock_result = DispatchResult(
            output="", exit_code=0, duration_seconds=2.0
        )

        with patch(
            "forge.routers.tasks.dispatcher.dispatch_generate",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            resp = client.post(
                "/api/tasks/generate",
                json={
                    "project_id": project_with_skill,
                    "problem_description": "Do something",
                },
            )

        assert resp.status_code == 422
        assert "empty output" in resp.json()["detail"]

    def test_generate_strips_code_fences(
        self, client: TestClient, project_with_skill: str
    ) -> None:
        """Test 5: strips markdown code fences from JSON output."""
        fenced_output = '```json\n[{"title":"Task A","priority":0,"description":"Do A","depends_on":[]}]\n```'
        mock_result = DispatchResult(
            output=fenced_output, exit_code=0, duration_seconds=4.0
        )

        with patch(
            "forge.routers.tasks.dispatcher.dispatch_generate",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            resp = client.post(
                "/api/tasks/generate",
                json={
                    "project_id": project_with_skill,
                    "problem_description": "Simple task",
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["tasks"]) == 1
        assert data["tasks"][0]["title"] == "Task A"


# ---------------------------------------------------------------------------
# Batch endpoint with depends_on tests
# ---------------------------------------------------------------------------


class TestBatchWithDependencies:
    def test_batch_with_depends_on_creates_links(
        self, client: TestClient, project_id: str, tmp_path
    ) -> None:
        """Test 6: batch creation with depends_on creates tasks in dependency order."""
        resp = client.post(
            "/api/tasks/batch",
            json={
                "tasks": [
                    {
                        "project_id": project_id,
                        "title": "Task 0",
                        "depends_on": [],
                    },
                    {
                        "project_id": project_id,
                        "title": "Task 1",
                        "depends_on": [0],
                    },
                    {
                        "project_id": project_id,
                        "title": "Task 2",
                        "depends_on": [0, 1],
                    },
                ]
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert len(data) == 3

        # Verify task_links were created
        from forge.config import DB_PATH

        conn = database.get_connection(str(DB_PATH))
        try:
            # Task 0 should have no incoming links (only outgoing)
            links_0 = database.get_task_links(conn, data[0]["id"])
            incoming_0 = [r for r in links_0 if r["target_task_id"] == data[0]["id"]]
            assert len(incoming_0) == 0

            # Task 1 should have one incoming "blocks" link from task 0
            links_1 = database.get_task_links(conn, data[1]["id"])
            incoming_1 = [r for r in links_1 if r["target_task_id"] == data[1]["id"]]
            assert len(incoming_1) == 1
            assert incoming_1[0]["source_task_id"] == data[0]["id"]
            assert incoming_1[0]["link_type"] == "blocks"

            # Task 2 should have two incoming links from tasks 0 and 1
            links_2 = database.get_task_links(conn, data[2]["id"])
            incoming_2 = [r for r in links_2 if r["target_task_id"] == data[2]["id"]]
            assert len(incoming_2) == 2
            source_ids = {row["source_task_id"] for row in incoming_2}
            assert data[0]["id"] in source_ids
            assert data[1]["id"] in source_ids
        finally:
            conn.close()

    def test_batch_no_dependencies(
        self, client: TestClient, project_id: str
    ) -> None:
        """Test 7: batch creation with no dependencies creates all tasks."""
        resp = client.post(
            "/api/tasks/batch",
            json={
                "tasks": [
                    {
                        "project_id": project_id,
                        "title": "Independent A",
                        "depends_on": [],
                    },
                    {
                        "project_id": project_id,
                        "title": "Independent B",
                        "depends_on": [],
                    },
                ]
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert len(data) == 2

        # Verify no task_links exist
        from forge.config import DB_PATH

        conn = database.get_connection(str(DB_PATH))
        try:
            for task in data:
                links = database.get_task_links(conn, task["id"])
                assert len(links) == 0
        finally:
            conn.close()

    def test_batch_invalid_depends_on_index(
        self, client: TestClient, project_id: str
    ) -> None:
        """Test 8: batch creation with invalid depends_on index returns 400."""
        resp = client.post(
            "/api/tasks/batch",
            json={
                "tasks": [
                    {
                        "project_id": project_id,
                        "title": "Task A",
                        "depends_on": [5],
                    },
                ]
            },
        )
        assert resp.status_code == 400
        assert "invalid dependency index" in resp.json()["detail"]

    def test_batch_self_referencing_depends_on(
        self, client: TestClient, project_id: str
    ) -> None:
        """Test 9: batch creation with self-reference returns 400."""
        resp = client.post(
            "/api/tasks/batch",
            json={
                "tasks": [
                    {
                        "project_id": project_id,
                        "title": "Self ref",
                        "depends_on": [0],
                    },
                ]
            },
        )
        assert resp.status_code == 400
        assert "depends on itself" in resp.json()["detail"]

    def test_batch_circular_depends_on(
        self, client: TestClient, project_id: str
    ) -> None:
        """Test 10: batch creation with circular deps returns 400."""
        resp = client.post(
            "/api/tasks/batch",
            json={
                "tasks": [
                    {
                        "project_id": project_id,
                        "title": "Task A",
                        "depends_on": [1],
                    },
                    {
                        "project_id": project_id,
                        "title": "Task B",
                        "depends_on": [0],
                    },
                ]
            },
        )
        assert resp.status_code == 400
        assert "Circular dependency" in resp.json()["detail"]

    def test_batch_backward_compatible(
        self, client: TestClient, project_id: str
    ) -> None:
        """Test 11: existing batch format without depends_on still works."""
        resp = client.post(
            "/api/tasks/batch",
            json={
                "tasks": [
                    {
                        "project_id": project_id,
                        "title": "Old format task",
                    },
                ]
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert len(data) == 1
        assert data[0]["title"] == "Old format task"
