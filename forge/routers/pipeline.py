"""API routes for pipeline engine control, stage runs, and logs."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from forge import database
from forge.config import DB_PATH
from forge.gate_runner import parse_structured_output
from forge.models import (
    EngineStatus,
    PipelineStats,
    RunLogEntry,
    StageRunResponse,
)

if TYPE_CHECKING:
    from forge.engine import PipelineEngine

router = APIRouter(tags=["pipeline"])

# The engine instance is set by main.py at startup
_engine: PipelineEngine | None = None


def set_engine(engine: PipelineEngine) -> None:
    """Called by main.py to inject the engine instance."""
    global _engine
    _engine = engine


def _get_engine() -> PipelineEngine:
    assert _engine is not None, "Engine not initialized"
    return _engine


# ---------------------------------------------------------------------------
# Engine control
# ---------------------------------------------------------------------------


@router.get("/api/engine/status", response_model=EngineStatus)
def engine_status() -> dict:
    return _get_engine().get_status()


@router.post("/api/engine/start", response_model=EngineStatus)
async def engine_start() -> dict:
    engine = _get_engine()
    if not engine.running:
        await engine.start()
    return engine.get_status()


@router.post("/api/engine/pause", response_model=EngineStatus)
async def engine_pause() -> dict:
    engine = _get_engine()
    if engine.running:
        await engine.pause()
    return engine.get_status()


@router.get("/api/engine/stats", response_model=PipelineStats)
def engine_stats() -> dict:
    return _get_engine().get_stats()


# ---------------------------------------------------------------------------
# Stage runs
# ---------------------------------------------------------------------------


def _row_to_stage_run(row) -> dict:
    d = dict(row)
    if isinstance(d.get("artifacts_produced"), str):
        d["artifacts_produced"] = json.loads(d["artifacts_produced"])
    # Parse structured gate output from gate_stdout JSON when present
    gate_stdout = d.get("gate_stdout")
    if gate_stdout:
        structured = parse_structured_output(gate_stdout)
        if structured is not None:
            d["gate_structured_output"] = structured
    return d


@router.get("/api/stage-runs", response_model=list[StageRunResponse])
def list_stage_runs(
    task_id: str | None = Query(None),
    stage: str | None = Query(None),
    status: str | None = Query(None),
) -> list[dict]:
    conn = database.get_connection(str(DB_PATH))
    try:
        rows = database.list_stage_runs(
            conn,
            task_id=task_id,
            stage=stage,
            status=status,
        )
        return [_row_to_stage_run(r) for r in rows]
    finally:
        conn.close()


@router.get("/api/stage-runs/{stage_run_id}", response_model=StageRunResponse)
def get_stage_run(stage_run_id: str) -> dict:
    from fastapi import HTTPException

    conn = database.get_connection(str(DB_PATH))
    try:
        row = database.get_stage_run(conn, stage_run_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Stage run not found")
        return _row_to_stage_run(row)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------


def _row_to_log(row) -> dict:
    d = dict(row)
    if isinstance(d.get("metadata"), str):
        d["metadata"] = json.loads(d["metadata"])
    return d


@router.get("/api/logs", response_model=list[RunLogEntry])
def get_logs(
    level: str | None = Query(None),
    task_id: str | None = Query(None),
    project_id: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> list[dict]:
    conn = database.get_connection(str(DB_PATH))
    try:
        rows = database.get_logs(
            conn,
            level=level,
            task_id=task_id,
            project_id=project_id,
            limit=limit,
            offset=offset,
        )
        return [_row_to_log(r) for r in rows]
    finally:
        conn.close()


async def _log_event_stream(level: str | None) -> AsyncGenerator[str, None]:
    """Async generator that yields SSE events for log entries."""
    conn = database.get_connection(str(DB_PATH))
    try:
        # Initial burst: 20 most recent entries, oldest-first
        rows = database.get_logs(conn, level=level, limit=20)
        rows = list(reversed(rows))
        last_id = 0
        for row in rows:
            entry = _row_to_log(row)
            last_id = max(last_id, entry["id"])
            yield f"data: {json.dumps(entry)}\n\n"
        # Poll for new entries every 2 seconds
        while True:
            await asyncio.sleep(2)
            new_rows = database.get_logs_since(
                conn,
                since_id=last_id,
                level=level,
            )
            for row in new_rows:
                entry = _row_to_log(row)
                last_id = max(last_id, entry["id"])
                yield f"data: {json.dumps(entry)}\n\n"
    finally:
        conn.close()


@router.get("/api/logs/stream")
async def stream_logs(level: str | None = Query(None)) -> StreamingResponse:
    return StreamingResponse(
        _log_event_stream(level),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
