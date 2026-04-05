from __future__ import annotations

import tempfile
from pathlib import Path

import yaml

from forge.config import (
    BASE_DIR,
    CONFIG_PATH,
    DB_PATH,
    FLOW_STAGES,
    LINK_TYPES,
    LOG_LEVELS,
    STAGE_RUN_STATUSES,
    STAGES,
    TASK_STATUSES,
    Settings,
    get_settings,
)


def test_path_constants() -> None:
    assert BASE_DIR.is_dir()
    assert DB_PATH.name == "forge.db"
    assert CONFIG_PATH.name == "config.yaml"


def test_default_settings() -> None:
    s = Settings()
    assert s.engine.poll_interval_seconds == 30
    assert s.engine.max_concurrent_tasks == 1
    assert s.engine.stage_timeout_seconds == 600
    assert s.engine.default_max_retries == 3
    assert s.claude.default_model == "opus"
    assert s.claude.headless_flags == ""


def test_get_settings_from_yaml() -> None:
    data = {
        "engine": {"poll_interval_seconds": 10, "max_concurrent_tasks": 2},
        "claude": {"default_model": "sonnet"},
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        tmp = Path(f.name)

    s = get_settings(config_path=tmp)
    assert s.engine.poll_interval_seconds == 10
    assert s.engine.max_concurrent_tasks == 2
    # Defaults for missing keys
    assert s.engine.stage_timeout_seconds == 600
    assert s.claude.default_model == "sonnet"
    assert s.claude.headless_flags == ""
    tmp.unlink()


def test_get_settings_missing_file() -> None:
    s = get_settings(config_path=Path("/nonexistent/config.yaml"))
    assert s.engine.poll_interval_seconds == 30
    assert s.claude.default_model == "opus"


def test_get_settings_empty_file() -> None:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write("")
        tmp = Path(f.name)

    s = get_settings(config_path=tmp)
    assert s.engine.poll_interval_seconds == 30
    tmp.unlink()


def test_stages_constant() -> None:
    assert STAGES == ["spec", "plan", "implement", "review"]


def test_task_statuses_constant() -> None:
    assert "backlog" in TASK_STATUSES
    assert "active" in TASK_STATUSES
    assert "done" in TASK_STATUSES
    assert "needs_human" in TASK_STATUSES
    assert "cancelled" in TASK_STATUSES


def test_stage_run_statuses_constant() -> None:
    assert "queued" in STAGE_RUN_STATUSES
    assert "running" in STAGE_RUN_STATUSES
    assert "passed" in STAGE_RUN_STATUSES
    assert "bounced" in STAGE_RUN_STATUSES


def test_link_types_constant() -> None:
    assert LINK_TYPES == ["blocks", "created_by", "follows", "related"]


def test_log_levels_constant() -> None:
    assert LOG_LEVELS == ["info", "warn", "error"]


def test_epic_flow_stages() -> None:
    """Epic flow should have spec and review stages."""
    assert FLOW_STAGES["epic"] == ["spec", "review"]
