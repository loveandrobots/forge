"""Tests for the smoke test module."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from tests.smoke import (
    SmokeResult,
    _DB_PATH_LOCATIONS,
    _EXCLUDED_ROUTES,
    _discover_routes,
    _normalize_path,
    run_smoke_tests,
)


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


def test_db_path_locations_includes_database_module():
    """The smoke test patches DB_PATH in forge.database to prevent prod DB access."""
    assert "forge.database.DB_PATH" in _DB_PATH_LOCATIONS, (
        "forge.database.DB_PATH must be patched — the database module imports "
        "DB_PATH at module level and get_connection() uses it as the default path"
    )


def test_db_path_locations_covers_all_importers():
    """Every module that imports DB_PATH from forge.config is in the patch list."""
    import ast
    from pathlib import Path

    forge_dir = Path(__file__).resolve().parent.parent / "forge"
    importers: set[str] = set()
    for py_file in forge_dir.rglob("*.py"):
        try:
            tree = ast.parse(py_file.read_text())
        except SyntaxError:
            continue
        module_path = (
            str(py_file.relative_to(forge_dir.parent))
            .replace("/", ".")
            .removesuffix(".py")
            .replace(".__init__", "")
        )
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "forge.config"
                and any(alias.name == "DB_PATH" for alias in node.names)
            ):
                importers.add(f"{module_path}.DB_PATH")

    missing = importers - set(_DB_PATH_LOCATIONS)
    assert missing == set(), (
        f"Modules that import DB_PATH but are not patched in smoke tests: {missing}"
    )


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


def test_route_discovery_finds_all_routes():
    """Route discovery returns all registered API routes."""
    from forge.main import app

    discovered = _discover_routes(app)

    # Should find key routes
    expected_routes = {
        ("GET", "/"),
        ("GET", "/api/tasks"),
        ("POST", "/api/tasks"),
        ("GET", "/api/engine/status"),
        ("DELETE", "/api/tasks/{task_id}"),
        ("POST", "/api/tasks/{task_id}/activate"),
        ("GET", "/api/stage-runs/{stage_run_id}"),
        ("PATCH", "/api/projects/{project_id}"),
    }
    for route in expected_routes:
        assert route in discovered, f"Expected route {route} not found in discovery"

    # Should NOT include static file mounts (those are Mount, not Route)
    static_routes = {(m, p) for m, p in discovered if p.startswith("/static")}
    assert static_routes == set(), f"Static routes should not be discovered: {static_routes}"

    # Should have a reasonable number of routes
    assert len(discovered) >= 20, (
        f"Expected at least 20 routes, found {len(discovered)}"
    )


def test_coverage_check_detects_uncovered_route():
    """Coverage check reports failure when a route has no smoke test."""
    from fastapi.routing import APIRoute
    from starlette.responses import JSONResponse

    from forge.main import app

    # Add a dummy route
    async def dummy_endpoint():
        return JSONResponse({"ok": True})

    dummy_route = APIRoute("/api/dummy-smoke-test", endpoint=dummy_endpoint, methods=["GET"])
    app.routes.append(dummy_route)
    try:
        results = run_smoke_tests()
        coverage_results = [r for r in results if "COVERAGE" in r.name and "dummy-smoke-test" in r.name]
        assert len(coverage_results) == 1, (
            f"Expected 1 coverage failure for dummy route, got {len(coverage_results)}"
        )
        assert not coverage_results[0].passed, "Expected coverage check to fail for uncovered route"
        assert "not exercised" in coverage_results[0].detail
    finally:
        app.routes.remove(dummy_route)


def test_normalize_path_matches_parameterized_routes():
    """Path normalization matches concrete URLs to route patterns."""
    patterns = {
        "/tasks/{task_id}",
        "/api/tasks/{task_id}",
        "/api/tasks/{task_id}/activate",
        "/api/stage-runs/{stage_run_id}",
        "/api/tasks",
        "/api/tasks/batch",
        "/",
    }

    # Parameterized matches
    assert _normalize_path("/tasks/abc-123", patterns) == "/tasks/{task_id}"
    assert _normalize_path("/api/tasks/abc-123", patterns) == "/api/tasks/{task_id}"
    assert _normalize_path("/api/tasks/abc-123/activate", patterns) == "/api/tasks/{task_id}/activate"
    assert _normalize_path("/api/stage-runs/xyz-456", patterns) == "/api/stage-runs/{stage_run_id}"

    # Exact matches (no params)
    assert _normalize_path("/api/tasks", patterns) == "/api/tasks"
    assert _normalize_path("/api/tasks/batch", patterns) == "/api/tasks/batch"
    assert _normalize_path("/", patterns) == "/"

    # Unknown path passes through unchanged
    assert _normalize_path("/unknown/path", patterns) == "/unknown/path"


def test_excluded_routes_not_flagged():
    """Adding a route to _EXCLUDED_ROUTES suppresses its coverage failure."""
    from fastapi.routing import APIRoute
    from starlette.responses import JSONResponse

    from forge.main import app

    # Add a dummy route that will NOT be exercised
    async def excluded_endpoint():
        return JSONResponse({"ok": True})

    dummy_route = APIRoute("/api/excluded-smoke-test", endpoint=excluded_endpoint, methods=["GET"])
    app.routes.append(dummy_route)
    exclusion_entry = ("GET", "/api/excluded-smoke-test")
    _EXCLUDED_ROUTES.add(exclusion_entry)
    try:
        results = run_smoke_tests()
        # The excluded dummy route should NOT appear as a coverage failure
        coverage_failures = [
            r for r in results
            if "excluded-smoke-test" in r.name and not r.passed
        ]
        assert coverage_failures == [], (
            f"Excluded route should not be flagged as uncovered: {coverage_failures}"
        )
    finally:
        app.routes.remove(dummy_route)
        _EXCLUDED_ROUTES.discard(exclusion_entry)
