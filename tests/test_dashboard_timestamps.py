"""Integration tests for relative timestamps on pipeline view cards."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from forge import database
from forge.main import app

_NOW = datetime(2026, 3, 30, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def client():
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture()
def project(tmp_path):
    conn = database.get_connection(str(tmp_path / "test.db"))
    try:
        pid = database.insert_project(conn, name="TsProj", repo_path=str(tmp_path))
        return pid
    finally:
        conn.close()


def _iso(delta: timedelta) -> str:
    return (_NOW - delta).isoformat()


def _patch_now():
    return patch("forge.utils.datetime", wraps=datetime, **{
        "now.return_value": _NOW,
    })


class TestPipelineTimestamps:
    def test_card_shows_created_ago(
        self, tmp_path, client: TestClient, project: str
    ) -> None:
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            tid = database.insert_task(
                conn, project_id=project, title="Created Test", priority=1,
            )
            conn.execute(
                "UPDATE tasks SET created_at = ? WHERE id = ?",
                (_iso(timedelta(minutes=5)), tid),
            )
            conn.commit()
        finally:
            conn.close()
        with _patch_now():
            resp = client.get("/")
        assert "Created 5m ago" in resp.text

    def test_done_card_shows_completed_ago(
        self, tmp_path, client: TestClient, project: str
    ) -> None:
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            tid = database.insert_task(
                conn, project_id=project, title="Done Test", priority=1,
            )
            database.update_task(
                conn, tid, status="done", current_stage="review",
                completed_at=_iso(timedelta(hours=2)),
            )
        finally:
            conn.close()
        with _patch_now():
            resp = client.get("/")
        assert "Completed 2h ago" in resp.text

    def test_active_card_shows_stage_duration(
        self, tmp_path, client: TestClient, project: str
    ) -> None:
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            tid = database.insert_task(
                conn, project_id=project, title="Active Test", priority=1,
            )
            database.update_task(conn, tid, status="active", current_stage="implement")
            sr = database.insert_stage_run(
                conn, task_id=tid, stage="implement", attempt=1, status="running",
            )
            database.update_stage_run(
                conn, sr, started_at=_iso(timedelta(minutes=30)),
            )
        finally:
            conn.close()
        with _patch_now():
            resp = client.get("/")
        assert "In stage for 30m" in resp.text

    def test_no_stage_run_no_duration(
        self, tmp_path, client: TestClient, project: str
    ) -> None:
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            database.insert_task(
                conn, project_id=project, title="Backlog Task", priority=1,
            )
        finally:
            conn.close()
        with _patch_now():
            resp = client.get("/")
        assert "In stage for" not in resp.text

    def test_no_completed_at_no_completed_text(
        self, tmp_path, client: TestClient, project: str
    ) -> None:
        conn = database.get_connection(str(tmp_path / "test.db"))
        try:
            tid = database.insert_task(
                conn, project_id=project, title="Active NoCmp", priority=1,
            )
            database.update_task(conn, tid, status="active", current_stage="spec")
        finally:
            conn.close()
        with _patch_now():
            resp = client.get("/")
        assert "Completed" not in resp.text

    def test_htmx_attributes_preserved(self, client: TestClient) -> None:
        resp = client.get("/")
        assert "hx-get" in resp.text
        assert 'hx-trigger="every 5s"' in resp.text
