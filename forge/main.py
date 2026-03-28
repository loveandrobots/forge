"""FastAPI application — entry point for the Forge server."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from forge import database
from forge.config import DB_PATH, get_settings
from forge.engine import PipelineEngine
from forge.routers import dashboard, pipeline, projects, tasks

_engine: PipelineEngine | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _engine
    # Startup
    conn = database.get_connection(str(DB_PATH))
    try:
        database.migrate(conn)
    finally:
        conn.close()

    settings = get_settings()
    _engine = PipelineEngine(settings, str(DB_PATH))
    pipeline.set_engine(_engine)
    await _engine.start()

    yield

    # Shutdown
    if _engine is not None:
        await _engine.pause()


app = FastAPI(title="Forge", lifespan=lifespan)

# Mount static files
_static_dir = Path(__file__).resolve().parent.parent / "static"
if _static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

# Include routers
app.include_router(projects.router)
app.include_router(tasks.router)
app.include_router(pipeline.router)
app.include_router(dashboard.router)
