"""Tests for the smoke test module."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from tests.smoke import SmokeResult, run_smoke_tests


def test_smoke_passes_on_healthy_app():
    """All smoke checks pass on a healthy application."""
    results = run_smoke_tests()
    assert len(results) > 0, "Expected at least one smoke check result"
    failures = [r for r in results if not r.passed]
    assert failures == [], (
        f"Smoke checks failed: {[(r.name, r.detail) for r in failures]}"
    )


def test_smoke_detects_missing_route():
    """Smoke test detects when a dashboard route is missing."""
    from forge.main import app

    # Find and temporarily remove the /settings route
    settings_route = None
    for route in app.routes:
        if hasattr(route, "path") and route.path == "/settings":
            settings_route = route
            break

    assert settings_route is not None, "Could not find /settings route"

    app.routes.remove(settings_route)
    try:
        results = run_smoke_tests()
        settings_results = [r for r in results if "/settings" in r.name]
        assert len(settings_results) > 0, "No result found for /settings"
        assert not settings_results[0].passed, (
            "Expected /settings check to fail when route is removed"
        )
        # Other checks should still pass
        other_results = [r for r in results if "/settings" not in r.name]
        other_failures = [r for r in other_results if not r.passed]
        assert other_failures == [], (
            f"Non-settings checks failed: {[(r.name, r.detail) for r in other_failures]}"
        )
    finally:
        app.routes.append(settings_route)


def test_smoke_uses_temp_database():
    """Smoke test does not touch the production database."""
    prod_db = Path("forge.db")
    # Record mtime if it exists
    had_prod_db = prod_db.exists()
    mtime_before = prod_db.stat().st_mtime if had_prod_db else None

    run_smoke_tests()

    if had_prod_db:
        assert prod_db.stat().st_mtime == mtime_before, (
            "Production database was modified during smoke test"
        )
    else:
        assert not prod_db.exists(), "Smoke test created a production database file"


def test_smoke_cleans_up_temp_database():
    """Smoke test removes its temporary database after running."""
    created_paths: list[str] = []
    original_named_temp = tempfile.NamedTemporaryFile

    def tracking_temp(**kwargs):
        tmp = original_named_temp(**kwargs)
        created_paths.append(tmp.name)
        return tmp

    with patch("tests.smoke.tempfile.NamedTemporaryFile", side_effect=tracking_temp):
        run_smoke_tests()

    assert len(created_paths) > 0, "No temp file was created"
    for path in created_paths:
        assert not Path(path).exists(), f"Temp database not cleaned up: {path}"


def test_smoke_result_structure():
    """Smoke results have the expected structure."""
    results = run_smoke_tests()
    for r in results:
        assert isinstance(r, SmokeResult)
        assert isinstance(r.name, str)
        assert isinstance(r.passed, bool)
        assert r.status_code is None or isinstance(r.status_code, int)
        assert isinstance(r.detail, str)


def test_smoke_main_exit_codes():
    """Running smoke module as script exits with code 0 on healthy app."""
    result = subprocess.run(
        [sys.executable, "-m", "tests.smoke"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"Smoke script exited with {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
