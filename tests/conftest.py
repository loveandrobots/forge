"""Shared test fixtures — ensures all tests use a temporary database."""

from __future__ import annotations

import pytest

from forge import database
from tests.smoke import _DB_PATH_LOCATIONS


async def _noop_start(self):
    """No-op replacement for PipelineEngine.start during tests."""


async def _noop_pause(self):
    """No-op replacement for PipelineEngine.pause during tests."""


@pytest.fixture(autouse=True)
def _use_tmp_db(tmp_path, monkeypatch):
    """Redirect DB_PATH to a temporary database for every test."""
    db_path = tmp_path / "test.db"
    for location in _DB_PATH_LOCATIONS:
        monkeypatch.setattr(location, db_path)
    # Prevent the engine from starting (and writing logs) during tests.
    monkeypatch.setattr("forge.engine.PipelineEngine.start", _noop_start)
    monkeypatch.setattr("forge.engine.PipelineEngine.pause", _noop_pause)
    conn = database.get_connection(str(db_path))
    database.migrate(conn)
    conn.close()
