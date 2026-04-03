"""Tests for the SSE log streaming endpoint and get_logs_since database function."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from forge import database
from forge.main import app
from forge.routers import pipeline
from forge.routers.pipeline import _log_event_stream


def _insert_logs(tmp_path, count: int, *, level: str = "info", prefix: str = "msg"):
    """Insert ``count`` log entries and return their ids."""
    db_path = tmp_path / "test.db"
    conn = database.get_connection(str(db_path))
    ids = []
    try:
        for i in range(count):
            log_id = database.insert_log(
                conn, level=level, message=f"{prefix}-{i}"
            )
            ids.append(log_id)
    finally:
        conn.close()
    return ids


async def _collect_events_from_generator(
    gen,
    n: int,
) -> list[dict]:
    """Consume ``n`` SSE events from an async generator, then break."""
    events: list[dict] = []
    async for chunk in gen:
        if chunk.startswith("data: "):
            events.append(json.loads(chunk[len("data: "):].strip()))
            if len(events) >= n:
                break
    return events


@pytest.fixture()
def client():
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ---------------------------------------------------------------------------
# AC 1: Content-Type is text/event-stream
# ---------------------------------------------------------------------------


class TestSSEContentType:
    async def test_returns_event_stream_content_type(self, tmp_path) -> None:
        """Verify the endpoint returns text/event-stream by calling stream_logs directly."""
        from forge.routers.pipeline import stream_logs
        from starlette.responses import StreamingResponse

        _insert_logs(tmp_path, 1)
        resp = await stream_logs(level=None)
        assert isinstance(resp, StreamingResponse)
        assert resp.media_type == "text/event-stream"


# ---------------------------------------------------------------------------
# AC 2: Event data JSON format
# ---------------------------------------------------------------------------


class TestSSEEventFormat:
    async def test_event_data_has_required_keys(self, tmp_path) -> None:
        _insert_logs(tmp_path, 1)
        gen = _log_event_stream(None)
        events = await asyncio.wait_for(
            _collect_events_from_generator(gen, 1), timeout=5,
        )
        assert len(events) == 1
        data = events[0]
        for key in ("id", "timestamp", "level", "message", "task_id", "metadata"):
            assert key in data, f"Missing key: {key}"


# ---------------------------------------------------------------------------
# AC 3: Initial burst of 20 most recent entries oldest-first
# ---------------------------------------------------------------------------


class TestSSEInitialBurst:
    async def test_sends_20_most_recent_oldest_first(self, tmp_path) -> None:
        ids = _insert_logs(tmp_path, 25)
        gen = _log_event_stream(None)
        events = await asyncio.wait_for(
            _collect_events_from_generator(gen, 20), timeout=5,
        )
        assert len(events) == 20
        event_ids = [e["id"] for e in events]
        # Ascending order (oldest first)
        assert event_ids == sorted(event_ids)
        # The 20 most recent entries are ids[5:]
        assert event_ids[0] == ids[5]
        assert event_ids[-1] == ids[24]

    async def test_last_id_tracks_max_to_avoid_duplicates(self, tmp_path) -> None:
        """Verify that last_id uses max(id) so polling never re-sends entries."""
        _insert_logs(tmp_path, 3)

        async def _insert_later():
            await asyncio.sleep(1)
            _insert_logs(tmp_path, 2, prefix="new")

        task = asyncio.create_task(_insert_later())
        gen = _log_event_stream(None)
        events = await asyncio.wait_for(
            _collect_events_from_generator(gen, 5), timeout=15,
        )
        await task
        # All event ids must be unique — no duplicates from bad last_id tracking
        event_ids = [e["id"] for e in events]
        assert len(event_ids) == len(set(event_ids)), (
            f"Duplicate event ids detected: {event_ids}"
        )


# ---------------------------------------------------------------------------
# AC 4: Polling sends new entries after initial burst
# ---------------------------------------------------------------------------


class TestSSEPolling:
    async def test_receives_new_entries_after_initial(self, tmp_path) -> None:
        _insert_logs(tmp_path, 3)

        async def _insert_later():
            await asyncio.sleep(1)
            _insert_logs(tmp_path, 2, prefix="new")

        task = asyncio.create_task(_insert_later())
        gen = _log_event_stream(None)
        events = await asyncio.wait_for(
            _collect_events_from_generator(gen, 5), timeout=15,
        )
        await task
        assert len(events) == 5
        assert events[3]["message"] == "new-0"
        assert events[4]["message"] == "new-1"


# ---------------------------------------------------------------------------
# AC 5: Level filter
# ---------------------------------------------------------------------------


class TestSSELevelFilter:
    async def test_filters_by_level(self, tmp_path) -> None:
        _insert_logs(tmp_path, 2, level="info")
        _insert_logs(tmp_path, 3, level="error", prefix="err")
        gen = _log_event_stream("error")
        events = await asyncio.wait_for(
            _collect_events_from_generator(gen, 3), timeout=5,
        )
        assert len(events) == 3
        assert all(e["level"] == "error" for e in events)


# ---------------------------------------------------------------------------
# AC 6: Clean disconnect
# ---------------------------------------------------------------------------


class TestSSEDisconnect:
    async def test_client_disconnect_no_error(self, tmp_path) -> None:
        _insert_logs(tmp_path, 1)
        gen = _log_event_stream(None)
        events = await asyncio.wait_for(
            _collect_events_from_generator(gen, 1), timeout=5,
        )
        assert len(events) == 1
        # Closing the generator should not raise
        await gen.aclose()


# ---------------------------------------------------------------------------
# AC 7: Endpoint in pipeline.py, StreamingResponse, no new deps
# ---------------------------------------------------------------------------


class TestSSEImplementation:
    def test_endpoint_exists_in_pipeline_router(self) -> None:
        routes = [r.path for r in pipeline.router.routes]
        assert "/api/logs/stream" in routes

    async def test_response_is_streaming(self, tmp_path) -> None:
        from forge.routers.pipeline import stream_logs
        from starlette.responses import StreamingResponse

        _insert_logs(tmp_path, 1)
        resp = await stream_logs(level=None)
        assert isinstance(resp, StreamingResponse)

    def test_no_new_dependencies(self) -> None:
        import pathlib

        req_path = pathlib.Path(__file__).parent.parent / "requirements.txt"
        if req_path.exists():
            content = req_path.read_text()
            assert "sse-starlette" not in content.lower()
            assert "aiohttp-sse" not in content.lower()


# ---------------------------------------------------------------------------
# AC 8: Template uses EventSource
# ---------------------------------------------------------------------------


class TestTemplateEventSource:
    def test_template_contains_eventsource(self) -> None:
        import pathlib

        template = pathlib.Path(__file__).parent.parent / "templates" / "logs.html"
        content = template.read_text()
        assert "new EventSource" in content
        assert "/api/logs/stream" in content

    def test_template_level_uses_tojson_filter(self) -> None:
        """Level variable uses |tojson for safe JS interpolation (Issue 2)."""
        import pathlib

        template = pathlib.Path(__file__).parent.parent / "templates" / "logs.html"
        content = template.read_text()
        assert "| tojson" in content or "|tojson" in content

    def test_template_no_htmx_polling_for_logs(self) -> None:
        import pathlib

        template = pathlib.Path(__file__).parent.parent / "templates" / "logs.html"
        content = template.read_text()
        assert 'hx-trigger="every' not in content


# ---------------------------------------------------------------------------
# AC 9: New entries prepended to tbody
# ---------------------------------------------------------------------------


class TestTemplatePrepend:
    def test_template_uses_insert_before(self) -> None:
        import pathlib

        template = pathlib.Path(__file__).parent.parent / "templates" / "logs.html"
        content = template.read_text()
        assert "insertBefore" in content
        assert "tbody.firstChild" in content

    def test_template_skips_duplicate_entries(self) -> None:
        """SSE handler skips entries already rendered server-side (Issue 1)."""
        import pathlib

        template = pathlib.Path(__file__).parent.parent / "templates" / "logs.html"
        content = template.read_text()
        assert "data-log-id" in content
        assert "maxRenderedId" in content
        assert "log.id <= maxRenderedId" in content

    def test_template_no_innerhtml_for_log_fields(self) -> None:
        """Log fields use DOM APIs, not innerHTML, to prevent XSS (Issues 2 & 3)."""
        import pathlib

        template = pathlib.Path(__file__).parent.parent / "templates" / "logs.html"
        content = template.read_text()
        # The onmessage handler should use textContent/createElement, not innerHTML
        script_section = content.split("es.onmessage")[1]
        assert "innerHTML" not in script_section
        assert "createElement" in script_section
        assert "textContent" in script_section


# ---------------------------------------------------------------------------
# AC 10: Auto-reconnect via built-in EventSource behavior
# ---------------------------------------------------------------------------


class TestAutoReconnect:
    def test_no_custom_reconnect_logic(self) -> None:
        import pathlib

        template = pathlib.Path(__file__).parent.parent / "templates" / "logs.html"
        content = template.read_text()
        assert "setTimeout" not in content


# ---------------------------------------------------------------------------
# AC 11: Existing GET /api/logs still works
# ---------------------------------------------------------------------------


class TestExistingLogsEndpoint:
    def test_get_logs_unchanged(self, client: TestClient, tmp_path) -> None:
        _insert_logs(tmp_path, 3)
        resp = client.get("/api/logs")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 3
        for entry in data:
            for key in ("id", "timestamp", "level", "message", "task_id", "metadata"):
                assert key in entry


# ---------------------------------------------------------------------------
# AC 12: Filter form reloads page and re-establishes EventSource
# ---------------------------------------------------------------------------


class TestFilterFormReload:
    def test_filter_form_uses_get_method(self) -> None:
        import pathlib

        template = pathlib.Path(__file__).parent.parent / "templates" / "logs.html"
        content = template.read_text()
        assert 'method="get"' in content
        assert 'action="/logs"' in content

    def test_eventsource_uses_selected_level(self) -> None:
        import pathlib

        template = pathlib.Path(__file__).parent.parent / "templates" / "logs.html"
        content = template.read_text()
        assert "selected_level" in content


# ---------------------------------------------------------------------------
# get_logs_since database function
# ---------------------------------------------------------------------------


class TestGetLogsSince:
    def test_returns_entries_after_since_id(self, tmp_path) -> None:
        db_path = tmp_path / "test.db"
        conn = database.get_connection(str(db_path))
        try:
            ids = []
            for i in range(5):
                ids.append(database.insert_log(conn, level="info", message=f"msg-{i}"))
            rows = database.get_logs_since(conn, since_id=ids[2])
            assert len(rows) == 2
            assert rows[0]["id"] == ids[3]
            assert rows[1]["id"] == ids[4]
        finally:
            conn.close()

    def test_ordered_ascending(self, tmp_path) -> None:
        db_path = tmp_path / "test.db"
        conn = database.get_connection(str(db_path))
        try:
            for i in range(5):
                database.insert_log(conn, level="info", message=f"msg-{i}")
            rows = database.get_logs_since(conn, since_id=0)
            row_ids = [r["id"] for r in rows]
            assert row_ids == sorted(row_ids)
        finally:
            conn.close()

    def test_level_filter(self, tmp_path) -> None:
        db_path = tmp_path / "test.db"
        conn = database.get_connection(str(db_path))
        try:
            database.insert_log(conn, level="info", message="info-msg")
            database.insert_log(conn, level="error", message="error-msg")
            database.insert_log(conn, level="info", message="info-msg-2")
            rows = database.get_logs_since(conn, since_id=0, level="error")
            assert len(rows) == 1
            assert rows[0]["level"] == "error"
        finally:
            conn.close()

    def test_since_id_zero_returns_all(self, tmp_path) -> None:
        db_path = tmp_path / "test.db"
        conn = database.get_connection(str(db_path))
        try:
            for i in range(3):
                database.insert_log(conn, level="info", message=f"msg-{i}")
            rows = database.get_logs_since(conn, since_id=0)
            assert len(rows) == 3
        finally:
            conn.close()

    def test_limit(self, tmp_path) -> None:
        db_path = tmp_path / "test.db"
        conn = database.get_connection(str(db_path))
        try:
            for i in range(10):
                database.insert_log(conn, level="info", message=f"msg-{i}")
            rows = database.get_logs_since(conn, since_id=0, limit=3)
            assert len(rows) == 3
        finally:
            conn.close()
