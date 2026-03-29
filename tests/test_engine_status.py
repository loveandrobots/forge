"""Tests for engine status indicator feature (AC 1–16)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from forge import database
from forge.config import Settings
from forge.engine import PipelineEngine
from forge.main import app
from forge.models import EngineStatus
from forge.routers import pipeline


def _make_mock_engine(
    running: bool = False,
    current_task_id: str | None = None,
    current_task_title: str | None = None,
    current_stage: str | None = None,
    queue_depth: int = 0,
) -> MagicMock:
    engine = MagicMock()
    engine.running = running
    engine.current_task_id = current_task_id
    engine.start = AsyncMock()
    engine.pause = AsyncMock()
    engine.get_status.return_value = {
        "running": running,
        "current_task_id": current_task_id,
        "current_task_title": current_task_title,
        "current_stage": current_stage,
        "queue_depth": queue_depth,
    }
    return engine


@pytest.fixture()
def client():
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ---------------------------------------------------------------------------
# AC 1, 3: EngineStatus model includes new fields
# ---------------------------------------------------------------------------


class TestEngineStatusModel:
    def test_new_fields_with_values(self) -> None:
        status = EngineStatus(
            running=True,
            current_task_id="t1",
            current_task_title="My Task",
            current_stage="implement",
            queue_depth=2,
        )
        assert status.current_task_title == "My Task"
        assert status.current_stage == "implement"

    def test_new_fields_default_none(self) -> None:
        status = EngineStatus(running=False)
        assert status.current_task_title is None
        assert status.current_stage is None

    def test_model_dump_includes_all_fields(self) -> None:
        status = EngineStatus(running=True, queue_depth=1)
        dumped = status.model_dump()
        assert "current_task_title" in dumped
        assert "current_stage" in dumped
        assert "running" in dumped
        assert "current_task_id" in dumped
        assert "queue_depth" in dumped


# ---------------------------------------------------------------------------
# AC 2: get_status() populates title and stage from DB
# ---------------------------------------------------------------------------


class TestGetStatusPopulation:
    def test_populates_title_and_stage_when_task_active(self, tmp_path) -> None:
        db_path = str(tmp_path / "test.db")
        conn = database.get_connection(db_path)
        database.migrate(conn)
        pid = database.insert_project(conn, name="P", repo_path="/tmp")
        tid = database.insert_task(conn, project_id=pid, title="Fix login bug", priority=1)
        database.update_task(conn, tid, status="active", current_stage="plan")
        database.insert_stage_run(conn, task_id=tid, stage="plan", attempt=1, status="queued")
        conn.close()

        engine = PipelineEngine(Settings(), db_path)
        engine.running = True
        engine.current_task_id = tid

        status = engine.get_status()
        assert status["current_task_title"] == "Fix login bug"
        assert status["current_stage"] == "plan"

    def test_returns_none_when_no_current_task(self, tmp_path) -> None:
        db_path = str(tmp_path / "test.db")
        conn = database.get_connection(db_path)
        database.migrate(conn)
        conn.close()

        engine = PipelineEngine(Settings(), db_path)
        engine.running = True
        engine.current_task_id = None

        status = engine.get_status()
        assert status["current_task_title"] is None
        assert status["current_stage"] is None

    def test_handles_deleted_task_gracefully(self, tmp_path) -> None:
        db_path = str(tmp_path / "test.db")
        conn = database.get_connection(db_path)
        database.migrate(conn)
        conn.close()

        engine = PipelineEngine(Settings(), db_path)
        engine.running = True
        engine.current_task_id = "nonexistent-id"

        status = engine.get_status()
        assert status["current_task_title"] is None
        assert status["current_stage"] is None


# ---------------------------------------------------------------------------
# AC 3: GET /api/engine/status returns new fields
# ---------------------------------------------------------------------------


class TestApiEngineStatus:
    def test_returns_new_fields(self, client: TestClient) -> None:
        engine = _make_mock_engine(
            running=True,
            current_task_id="t1",
            current_task_title="Deploy fix",
            current_stage="implement",
            queue_depth=3,
        )
        pipeline.set_engine(engine)
        resp = client.get("/api/engine/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["current_task_title"] == "Deploy fix"
        assert data["current_stage"] == "implement"
        assert data["running"] is True
        assert data["current_task_id"] == "t1"
        assert data["queue_depth"] == 3


# ---------------------------------------------------------------------------
# AC 4, 5: GET /partials/engine-status returns HTML fragment
# ---------------------------------------------------------------------------


class TestEngineStatusPartial:
    def test_returns_html_fragment(self, client: TestClient) -> None:
        engine = _make_mock_engine(running=False)
        pipeline.set_engine(engine)
        resp = client.get("/partials/engine-status")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "<!DOCTYPE html>" not in resp.text
        assert "Paused" in resp.text

    def test_returns_200(self, client: TestClient) -> None:
        engine = _make_mock_engine(running=True)
        pipeline.set_engine(engine)
        resp = client.get("/partials/engine-status")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# AC 6, 15: Status indicator in base.html on every page
# ---------------------------------------------------------------------------


class TestStatusInBaseHtml:
    def test_pipeline_page_has_status_indicator(self, client: TestClient) -> None:
        engine = _make_mock_engine(running=False)
        pipeline.set_engine(engine)
        resp = client.get("/")
        assert 'id="engine-status"' in resp.text
        assert 'hx-get="/partials/engine-status"' in resp.text
        assert "every 5s" in resp.text

    def test_backlog_page_has_status_indicator(self, client: TestClient) -> None:
        engine = _make_mock_engine(running=False)
        pipeline.set_engine(engine)
        resp = client.get("/backlog")
        assert 'id="engine-status"' in resp.text

    def test_logs_page_has_status_indicator(self, client: TestClient) -> None:
        engine = _make_mock_engine(running=False)
        pipeline.set_engine(engine)
        resp = client.get("/logs")
        assert 'id="engine-status"' in resp.text

    def test_settings_page_has_status_indicator(self, client: TestClient) -> None:
        engine = _make_mock_engine(running=False)
        pipeline.set_engine(engine)
        resp = client.get("/settings")
        assert 'id="engine-status"' in resp.text


# ---------------------------------------------------------------------------
# AC 7: Running with active task shows correct text
# ---------------------------------------------------------------------------


class TestRunningWithTask:
    def test_shows_task_title_and_stage(self, client: TestClient) -> None:
        engine = _make_mock_engine(
            running=True,
            current_task_id="t1",
            current_task_title="Fix login bug",
            current_stage="implement",
        )
        pipeline.set_engine(engine)
        resp = client.get("/partials/engine-status")
        assert "Running" in resp.text
        assert "Fix login bug" in resp.text
        assert "implement" in resp.text


# ---------------------------------------------------------------------------
# AC 8: Running idle shows correct text
# ---------------------------------------------------------------------------


class TestRunningIdle:
    def test_shows_idle(self, client: TestClient) -> None:
        engine = _make_mock_engine(running=True)
        pipeline.set_engine(engine)
        resp = client.get("/partials/engine-status")
        assert "Running" in resp.text
        assert "idle" in resp.text


# ---------------------------------------------------------------------------
# AC 9: Paused shows correct text
# ---------------------------------------------------------------------------


class TestPaused:
    def test_shows_paused(self, client: TestClient) -> None:
        engine = _make_mock_engine(running=False)
        pipeline.set_engine(engine)
        resp = client.get("/partials/engine-status")
        assert "Paused" in resp.text


# ---------------------------------------------------------------------------
# AC 10, 11: Pause/Start buttons shown appropriately
# ---------------------------------------------------------------------------


class TestToggleButtons:
    def test_pause_button_when_running(self, client: TestClient) -> None:
        engine = _make_mock_engine(running=True)
        pipeline.set_engine(engine)
        resp = client.get("/partials/engine-status")
        assert "Pause" in resp.text
        assert 'hx-post="/api/engine/pause"' in resp.text

    def test_start_button_when_paused(self, client: TestClient) -> None:
        engine = _make_mock_engine(running=False)
        pipeline.set_engine(engine)
        resp = client.get("/partials/engine-status")
        assert "Start" in resp.text
        assert 'hx-post="/api/engine/start"' in resp.text


# ---------------------------------------------------------------------------
# AC 12: Buttons trigger status refresh
# ---------------------------------------------------------------------------


class TestButtonRefresh:
    def test_pause_button_triggers_refresh(self, client: TestClient) -> None:
        engine = _make_mock_engine(running=True)
        pipeline.set_engine(engine)
        resp = client.get("/partials/engine-status")
        assert "htmx.trigger" in resp.text
        assert "#engine-status" in resp.text
        assert "refresh" in resp.text

    def test_start_button_triggers_refresh(self, client: TestClient) -> None:
        engine = _make_mock_engine(running=False)
        pipeline.set_engine(engine)
        resp = client.get("/partials/engine-status")
        assert "htmx.trigger" in resp.text
        assert "#engine-status" in resp.text
        assert "refresh" in resp.text


# ---------------------------------------------------------------------------
# AC 13, 14: Styled as small status line with muted dot
# ---------------------------------------------------------------------------


class TestStyling:
    def test_css_has_engine_status_rules(self) -> None:
        import pathlib

        css_path = pathlib.Path(__file__).resolve().parent.parent / "static" / "styles.css"
        css = css_path.read_text()
        assert ".engine-status" in css
        assert ".engine-status-dot" in css
        assert "var(--text-secondary)" in css
        # No red or green in engine status styles
        engine_block = css[css.index(".engine-status-dot"):css.index("/* Content */")]
        assert "#ff0000" not in engine_block.lower()
        assert "#00ff00" not in engine_block.lower()
        assert "red" not in engine_block.lower()
        assert "green" not in engine_block.lower()

    def test_partial_contains_dot_element(self, client: TestClient) -> None:
        engine = _make_mock_engine(running=True)
        pipeline.set_engine(engine)
        resp = client.get("/partials/engine-status")
        assert "engine-status-dot" in resp.text


# ---------------------------------------------------------------------------
# AC 16: Existing start/pause API behavior unchanged
# ---------------------------------------------------------------------------


class TestApiUnchanged:
    def test_start_returns_engine_status_json(self, client: TestClient) -> None:
        engine = _make_mock_engine(running=False)
        engine.get_status.return_value = {
            "running": True,
            "current_task_id": None,
            "current_task_title": None,
            "current_stage": None,
            "queue_depth": 0,
        }
        pipeline.set_engine(engine)
        resp = client.post("/api/engine/start")
        assert resp.status_code == 200
        data = resp.json()
        assert "running" in data
        assert "current_task_title" in data
        assert "current_stage" in data
        assert "queue_depth" in data

    def test_pause_returns_engine_status_json(self, client: TestClient) -> None:
        engine = _make_mock_engine(running=True)
        engine.get_status.return_value = {
            "running": False,
            "current_task_id": None,
            "current_task_title": None,
            "current_stage": None,
            "queue_depth": 0,
        }
        pipeline.set_engine(engine)
        resp = client.post("/api/engine/pause")
        assert resp.status_code == 200
        data = resp.json()
        assert "running" in data
        assert "current_task_title" in data
        assert "current_stage" in data
