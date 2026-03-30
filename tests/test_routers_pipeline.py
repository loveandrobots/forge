"""Tests for forge.routers.pipeline."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from forge import database
from forge.main import app
from forge.routers import pipeline


def _make_mock_engine(running: bool = False) -> MagicMock:
    engine = MagicMock()
    engine.running = running
    engine.current_task_id = None
    engine.start = AsyncMock()
    engine.pause = AsyncMock()
    engine.get_status.return_value = {
        "running": running,
        "current_task_id": None,
        "queue_depth": 0,
    }
    engine.get_stats.return_value = {
        "total_tasks": 0,
        "tasks_by_status": {},
        "total_stage_runs": 0,
        "stage_runs_by_status": {},
        "avg_stage_duration_seconds": None,
        "total_completed": 0,
        "total_active": 0,
        "avg_duration_by_stage": {},
        "bounce_rate_by_stage": {},
    }
    return engine


@pytest.fixture()
def client():
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


class TestEngineStatus:
    def test_get_status(self, client: TestClient) -> None:
        engine = _make_mock_engine(running=False)
        pipeline.set_engine(engine)
        resp = client.get("/api/engine/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["running"] is False
        assert data["queue_depth"] == 0


class TestEngineStart:
    def test_start(self, client: TestClient) -> None:
        engine = _make_mock_engine(running=False)
        pipeline.set_engine(engine)
        engine.get_status.return_value = {
            "running": True,
            "current_task_id": None,
            "queue_depth": 0,
        }
        resp = client.post("/api/engine/start")
        assert resp.status_code == 200
        engine.start.assert_called_once()

    def test_start_already_running(self, client: TestClient) -> None:
        engine = _make_mock_engine(running=True)
        pipeline.set_engine(engine)
        resp = client.post("/api/engine/start")
        assert resp.status_code == 200
        engine.start.assert_not_called()


class TestEnginePause:
    def test_pause(self, client: TestClient) -> None:
        engine = _make_mock_engine(running=True)
        pipeline.set_engine(engine)
        resp = client.post("/api/engine/pause")
        assert resp.status_code == 200
        engine.pause.assert_called_once()

    def test_pause_already_paused(self, client: TestClient) -> None:
        engine = _make_mock_engine(running=False)
        pipeline.set_engine(engine)
        resp = client.post("/api/engine/pause")
        assert resp.status_code == 200
        engine.pause.assert_not_called()


class TestEngineStats:
    def test_get_stats(self, client: TestClient) -> None:
        engine = _make_mock_engine()
        pipeline.set_engine(engine)
        resp = client.get("/api/engine/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_tasks"] == 0

    def test_stats_includes_new_fields(self, client: TestClient) -> None:
        engine = _make_mock_engine()
        pipeline.set_engine(engine)
        resp = client.get("/api/engine/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_completed"] == 0
        assert data["total_active"] == 0
        assert data["avg_duration_by_stage"] == {}
        assert data["bounce_rate_by_stage"] == {}
        # Backward compatibility
        assert "total_tasks" in data
        assert "tasks_by_status" in data
        assert "total_stage_runs" in data
        assert "stage_runs_by_status" in data
        assert "avg_stage_duration_seconds" in data


class TestStageRuns:
    def test_list_empty(self, client: TestClient) -> None:
        resp = client.get("/api/stage-runs")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_with_filters(self, client: TestClient, tmp_path) -> None:
        db_path = tmp_path / "test.db"
        conn = database.get_connection(str(db_path))
        try:
            pid = database.insert_project(conn, name="P", repo_path=str(tmp_path))
            tid = database.insert_task(conn, project_id=pid, title="T")
            database.insert_stage_run(conn, task_id=tid, stage="spec", attempt=1)
        finally:
            conn.close()

        resp = client.get(f"/api/stage-runs?task_id={tid}")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_get_not_found(self, client: TestClient) -> None:
        resp = client.get("/api/stage-runs/nonexistent")
        assert resp.status_code == 404

    def test_get_found(self, client: TestClient, tmp_path) -> None:
        db_path = tmp_path / "test.db"
        conn = database.get_connection(str(db_path))
        try:
            pid = database.insert_project(conn, name="P", repo_path=str(tmp_path))
            tid = database.insert_task(conn, project_id=pid, title="T")
            sr_id = database.insert_stage_run(
                conn, task_id=tid, stage="spec", attempt=1
            )
        finally:
            conn.close()

        resp = client.get(f"/api/stage-runs/{sr_id}")
        assert resp.status_code == 200
        assert resp.json()["stage"] == "spec"


class TestLogs:
    def test_list_empty(self, client: TestClient) -> None:
        resp = client.get("/api/logs")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_with_entries(self, client: TestClient, tmp_path) -> None:
        db_path = tmp_path / "test.db"
        conn = database.get_connection(str(db_path))
        try:
            database.insert_log(conn, level="info", message="hello")
            database.insert_log(conn, level="error", message="boom")
        finally:
            conn.close()

        resp = client.get("/api/logs")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_filter_by_level(self, client: TestClient, tmp_path) -> None:
        db_path = tmp_path / "test.db"
        conn = database.get_connection(str(db_path))
        try:
            database.insert_log(conn, level="info", message="hello")
            database.insert_log(conn, level="error", message="boom")
        finally:
            conn.close()

        resp = client.get("/api/logs?level=error")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["level"] == "error"

    def test_pagination(self, client: TestClient, tmp_path) -> None:
        db_path = tmp_path / "test.db"
        conn = database.get_connection(str(db_path))
        try:
            for i in range(5):
                database.insert_log(conn, level="info", message=f"msg-{i}")
        finally:
            conn.close()

        resp = client.get("/api/logs?limit=2&offset=0")
        assert resp.status_code == 200
        assert len(resp.json()) == 2
