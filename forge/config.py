from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


# Path constants
BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "forge.db"
CONFIG_PATH = BASE_DIR / "config.yaml"

# Pipeline stages in order
STAGES: list[str] = ["spec", "plan", "implement", "review"]

# Valid task statuses
TASK_STATUSES: list[str] = [
    "backlog",
    "active",
    "paused",
    "needs_human",
    "done",
    "failed",
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
    default_max_retries: int = 3


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
