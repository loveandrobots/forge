from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


# Path constants
BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("FORGE_DB_PATH", str(BASE_DIR / "forge.db")))
CONFIG_PATH = BASE_DIR / "config.yaml"

# Pipeline stages in order
STAGES: list[str] = ["spec", "plan", "implement", "review"]

# Flow definitions: mapping of flow name to ordered stage list
FLOW_STAGES: dict[str, list[str]] = {
    "standard": ["spec", "plan", "implement", "review"],
    "quick": ["implement", "review"],
    "epic": ["spec", "review"],
}

# Valid flow values
VALID_FLOWS: tuple[str, ...] = tuple(FLOW_STAGES.keys())

# Valid epic_status values
VALID_EPIC_STATUSES: tuple[str, ...] = ("pending", "decomposed", "reviewing", "complete")

# Valid task statuses
TASK_STATUSES: list[str] = [
    "backlog",
    "active",
    "paused",
    "needs_human",
    "done",
    "failed",
    "cancelled",
]

# Valid stage run statuses
STAGE_RUN_STATUSES: list[str] = [
    "queued",
    "running",
    "passed",
    "failed",
    "bounced",
    "error",
]

# Valid task link types
LINK_TYPES: list[str] = ["blocks", "created_by", "follows", "related"]

# Valid log levels
LOG_LEVELS: list[str] = ["info", "warn", "error"]


class EngineSettings(BaseModel):
    poll_interval_seconds: int = 30
    max_concurrent_tasks: int = 1
    stage_timeout_seconds: int = 600
    stage_timeouts: dict[str, int] = Field(default_factory=lambda: {"implement": 900})
    default_max_retries: int = 3


def resolve_stage_timeout(
    stage: str,
    project_stage_timeouts: dict[str, int] | None,
    engine: EngineSettings,
) -> int:
    """Resolve timeout for a stage: project override > per-stage config > global default."""
    if project_stage_timeouts and stage in project_stage_timeouts:
        return project_stage_timeouts[stage]
    if engine.stage_timeouts and stage in engine.stage_timeouts:
        return engine.stage_timeouts[stage]
    return engine.stage_timeout_seconds


class ClaudeSettings(BaseModel):
    default_model: str = "opus"
    headless_flags: str = "--output-format stream-json"


class Settings(BaseModel):
    engine: EngineSettings = Field(default_factory=EngineSettings)
    claude: ClaudeSettings = Field(default_factory=ClaudeSettings)


def get_settings(config_path: Path | None = None) -> Settings:
    """Load settings from config.yaml. Returns defaults if file is missing."""
    path = config_path or CONFIG_PATH
    if path.exists():
        with open(path) as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}
        return Settings(**data)
    return Settings()
