"""Shared test fixtures — ensures all tests use a temporary database."""

from __future__ import annotations

import pytest

from forge import database


# Every module that does `from forge.config import DB_PATH` gets its own
# local reference.  We must patch each one so no code path touches the
# production database.
_DB_PATH_LOCATIONS = [
    "forge.config.DB_PATH",
    "forge.cli.DB_PATH",
    "forge.main.DB_PATH",
    "forge.routers.projects.DB_PATH",
    "forge.routers.tasks.DB_PATH",
    "forge.routers.pipeline.DB_PATH",
    "forge.routers.dashboard.DB_PATH",
]


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
