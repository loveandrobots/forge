"""Standalone smoke test module for Forge endpoint and page verification.

Can be run three ways:
    python -m tests.smoke          # standalone script
    from tests.smoke import run_smoke_tests  # importable
    pytest tests/test_smoke.py     # via pytest wrapper
"""

from __future__ import annotations

import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from forge import database
from forge.config import TASK_STATUSES

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

# Dashboard pages that should return 200 on GET
_DASHBOARD_PAGES: list[str] = [
    "/",
    "/backlog",
    "/logs",
    "/settings",
    "/partials/engine-status",
]

# API GET endpoints that should return 200
_API_GET_ENDPOINTS: list[str] = [
    "/api/engine/status",
    "/api/engine/stats",
    "/api/tasks",
    "/api/projects",
    "/api/logs",
    "/api/stage-runs",
]

# POST-only endpoints that should reject GET with 405
_POST_ONLY_ENDPOINTS: list[str] = [
    "/api/engine/start",
    "/api/engine/pause",
]

# Routes excluded from the auto-discovery coverage check.
# These are FastAPI-generated or cannot be meaningfully smoke-tested.
_EXCLUDED_ROUTES: set[tuple[str, str]] = {
    ("GET", "/openapi.json"),
    ("HEAD", "/openapi.json"),
    ("GET", "/docs"),
    ("HEAD", "/docs"),
    ("GET", "/docs/oauth2-redirect"),
    ("HEAD", "/docs/oauth2-redirect"),
    ("GET", "/redoc"),
    ("HEAD", "/redoc"),
}


def _discover_routes(app) -> set[tuple[str, str]]:
    """Introspect the FastAPI app and return all registered (method, path) tuples.

    Filters to Route instances with methods — excludes static file mounts.
    """
    from starlette.routing import Route

    routes: set[tuple[str, str]] = set()
    for route in app.routes:
        if isinstance(route, Route) and hasattr(route, "methods"):
            for method in route.methods:
                routes.add((method, route.path))
    return routes


def _normalize_path(url: str, route_patterns: set[str]) -> str:
    """Match a concrete URL to its registered route pattern.

    For example, '/tasks/abc-123' matched against {'/tasks/{task_id}'}
    returns '/tasks/{task_id}'.

    Falls back to returning the url unchanged if no pattern matches.
    Prefers exact matches over parameterized matches.
    """
    # Exact match first
    if url in route_patterns:
        return url
    # Then try parameterized patterns (prefer most-specific: fewest params)
    candidates = []
    for pattern in route_patterns:
        if "{" not in pattern:
            continue
        parts = re.split(r"\{[^}]+\}", pattern)
        regex = r"[^/]+".join(re.escape(p) for p in parts)
        if re.fullmatch(regex, url):
            candidates.append(pattern)
    if candidates:
        # Pick the pattern with the fewest parameters (most literal segments)
        return min(candidates, key=lambda p: p.count("{"))
    return url


@dataclass
class SmokeResult:
    """Result of a single smoke check."""

    name: str
    passed: bool
    status_code: int | None = None
    detail: str = ""


def _seed_data(db_path: str) -> dict:
    """Seed minimal test data. Returns dict with project_id and task_ids."""
    conn = database.get_connection(db_path)
    try:
        project_id = database.insert_project(
            conn,
            name="SmokeTestProject",
            repo_path="/tmp/smoke-test-repo",
            default_branch="main",
        )

        task_ids: dict[str, str] = {}
        for status in TASK_STATUSES:
            task_id = database.insert_task(
                conn,
                project_id=project_id,
                title=f"Smoke test task ({status})",
                description=f"Task seeded for smoke test with status {status}",
            )
            if status != "backlog":
                database.update_task(conn, task_id, status=status)
            task_ids[status] = task_id

        # Insert a stage run for the active task
        active_task_id = task_ids.get("active")
        if active_task_id:
            database.insert_stage_run(
                conn,
                task_id=active_task_id,
                stage="implement",
                attempt=1,
                status="queued",
            )

        # Create an extra backlog task for deletion test (so we don't remove the
        # one used for activate)
        delete_task_id = database.insert_task(
            conn,
            project_id=project_id,
            title="Smoke test task (delete target)",
            description="Task seeded for smoke test DELETE endpoint",
        )
        task_ids["_delete_target"] = delete_task_id

        return {"project_id": project_id, "task_ids": task_ids}
    finally:
        conn.close()


def _check(
    client,
    method: str,
    url: str,
    expected: int,
    results: list[SmokeResult],
    exercised: set[tuple[str, str]],
    route_patterns: set[str],
    **kwargs,
) -> None:
    """Execute a single smoke check and record the result."""
    name = f"{method.upper()} {url}"
    if expected != 200:
        name += f" (expect {expected})"
    try:
        resp = getattr(client, method.lower())(url, **kwargs)
        results.append(
            SmokeResult(
                name=name,
                passed=resp.status_code == expected,
                status_code=resp.status_code,
                detail=""
                if resp.status_code == expected
                else f"expected {expected}, got {resp.status_code}",
            )
        )
    except Exception as exc:
        results.append(
            SmokeResult(
                name=name,
                passed=False,
                detail=str(exc),
            )
        )
    # Track coverage using the normalized route pattern
    normalized = _normalize_path(url, route_patterns)
    exercised.add((method.upper(), normalized))


def run_smoke_tests() -> list[SmokeResult]:
    """Run all smoke checks and return results.

    Creates a temporary database, seeds data, patches DB_PATH across all
    modules, and exercises every dashboard page and core API endpoint.
    """
    results: list[SmokeResult] = []
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = Path(tmp.name)
    tmp.close()

    try:
        # Migrate schema
        conn = database.get_connection(str(db_path))
        database.migrate(conn)
        conn.close()

        # Build patch context: all DB_PATH locations + engine no-ops
        patches = [patch(loc, db_path) for loc in _DB_PATH_LOCATIONS]

        async def _noop_start(self):
            pass

        async def _noop_pause(self):
            pass

        patches.append(patch("forge.engine.PipelineEngine.start", _noop_start))
        patches.append(patch("forge.engine.PipelineEngine.pause", _noop_pause))

        for p in patches:
            p.start()

        try:
            # Seed data after patching so any imports inside seed use temp db
            seed_info = _seed_data(str(db_path))
            task_ids = seed_info["task_ids"]
            project_id = seed_info["project_id"]

            from starlette.testclient import TestClient

            from forge.main import app

            # Discover all registered routes
            discovered = _discover_routes(app)
            route_patterns = {path for _, path in discovered}

            # Track which (method, pattern) tuples we exercise
            exercised: set[tuple[str, str]] = set()

            with TestClient(app) as client:
                # --- Dashboard pages ---
                for page_url in _DASHBOARD_PAGES:
                    _check(client, "GET", page_url, 200, results, exercised, route_patterns)

                # Task detail page (parameterized)
                first_task_id = next(iter(task_ids.values()))
                _check(client, "GET", f"/tasks/{first_task_id}", 200, results, exercised, route_patterns)

                # --- API GET endpoints ---
                for url in _API_GET_ENDPOINTS:
                    _check(client, "GET", url, 200, results, exercised, route_patterns)

                # Parameterized GET endpoints
                _check(client, "GET", f"/api/tasks/{first_task_id}", 200, results, exercised, route_patterns)
                _check(client, "GET", f"/api/projects/{project_id}", 200, results, exercised, route_patterns)

                # Stage run GET - find a stage run ID from the active task
                stage_runs_resp = client.get(f"/api/stage-runs?task_id={task_ids['active']}")
                if stage_runs_resp.status_code == 200 and stage_runs_resp.json():
                    sr_id = stage_runs_resp.json()[0]["id"]
                    _check(client, "GET", f"/api/stage-runs/{sr_id}", 200, results, exercised, route_patterns)
                else:
                    results.append(SmokeResult(
                        name="GET /api/stage-runs/{stage_run_id}",
                        passed=False,
                        detail=f"setup failed: could not fetch stage runs (got {stage_runs_resp.status_code})",
                    ))

                # --- POST endpoints ---

                # POST /api/tasks
                _check(
                    client, "POST", "/api/tasks", 201, results, exercised, route_patterns,
                    json={"title": "Smoke POST task", "project_id": project_id, "description": "Created by smoke test"},
                )

                # POST /api/tasks/batch
                _check(
                    client, "POST", "/api/tasks/batch", 201, results, exercised, route_patterns,
                    json={"tasks": [{"title": "Batch task", "project_id": project_id, "description": "Batch smoke"}]},
                )

                # POST /api/projects (repo_path must be a real directory)
                _check(
                    client, "POST", "/api/projects", 201, results, exercised, route_patterns,
                    json={"name": "SmokeProject2", "repo_path": "/tmp", "default_branch": "main"},
                )

                # Engine control POST endpoints
                for post_url in _POST_ONLY_ENDPOINTS:
                    _check(client, "POST", post_url, 200, results, exercised, route_patterns)

                # Verify POST-only endpoints reject GET with 405
                for post_url in _POST_ONLY_ENDPOINTS:
                    _check(client, "GET", post_url, 405, results, exercised, route_patterns)

                # --- Task action endpoints ---
                # activate: requires backlog
                _check(client, "POST", f"/api/tasks/{task_ids['backlog']}/activate", 200, results, exercised, route_patterns)

                # pause: requires active
                _check(client, "POST", f"/api/tasks/{task_ids['active']}/pause", 200, results, exercised, route_patterns)

                # resume: requires needs_human
                _check(client, "POST", f"/api/tasks/{task_ids['needs_human']}/resume", 200, results, exercised, route_patterns)

                # retry: requires active or needs_human
                # The needs_human task was resumed above (now active), so create a fresh one
                retry_resp = client.post(
                    "/api/tasks",
                    json={"title": "Retry target", "project_id": project_id, "description": "For retry smoke"},
                )
                if retry_resp.status_code == 201:
                    retry_task_id = retry_resp.json()["id"]
                    # Move to active by activating
                    client.post(f"/api/tasks/{retry_task_id}/activate")
                    _check(client, "POST", f"/api/tasks/{retry_task_id}/retry", 200, results, exercised, route_patterns)
                else:
                    results.append(SmokeResult(
                        name="POST /api/tasks/{task_id}/retry",
                        passed=False,
                        detail=f"setup failed: could not create task (got {retry_resp.status_code})",
                    ))

                # reset: requires needs_human, failed, paused — use failed task
                _check(client, "POST", f"/api/tasks/{task_ids['failed']}/reset", 200, results, exercised, route_patterns)

                # cancel: create a fresh backlog task since prior seeded tasks have changed status
                cancel_resp = client.post(
                    "/api/tasks",
                    json={"title": "Cancel target", "project_id": project_id, "description": "For cancel smoke"},
                )
                if cancel_resp.status_code == 201:
                    cancel_task_id = cancel_resp.json()["id"]
                    _check(client, "POST", f"/api/tasks/{cancel_task_id}/cancel", 200, results, exercised, route_patterns)
                else:
                    results.append(SmokeResult(
                        name="POST /api/tasks/{task_id}/cancel",
                        passed=False,
                        detail=f"setup failed: could not create task (got {cancel_resp.status_code})",
                    ))

                # --- PATCH endpoints ---
                _check(
                    client, "PATCH", f"/api/tasks/{task_ids['cancelled']}", 200, results, exercised, route_patterns,
                    json={"title": "Updated smoke task"},
                )
                _check(
                    client, "PATCH", f"/api/projects/{project_id}", 200, results, exercised, route_patterns,
                    json={"name": "UpdatedSmokeProject"},
                )

                # --- DELETE endpoint (run last, uses dedicated backlog task) ---
                _check(client, "DELETE", f"/api/tasks/{task_ids['_delete_target']}", 204, results, exercised, route_patterns)

                # --- Route coverage check ---
                uncovered = discovered - exercised - _EXCLUDED_ROUTES
                for method, path in sorted(uncovered):
                    results.append(
                        SmokeResult(
                            name=f"COVERAGE {method} {path}",
                            passed=False,
                            detail=f"route {method} {path} is registered but not exercised by smoke tests",
                        )
                    )

        finally:
            for p in patches:
                p.stop()
    finally:
        # Clean up temp database
        if db_path.exists():
            db_path.unlink()

    return results


def main() -> None:
    """Entry point for standalone execution."""
    results = run_smoke_tests()
    passed = sum(1 for r in results if r.passed)
    total = len(results)

    for r in results:
        status = "PASS" if r.passed else "FAIL"
        detail = f"  ({r.detail})" if r.detail else ""
        print(f"  {status}: {r.name}{detail}")

    print(f"\n{passed}/{total} checks passed")

    failures = [r for r in results if not r.passed]
    if failures:
        print("\nFailures:", file=sys.stderr)
        for r in failures:
            print(f"  FAIL: {r.name} — {r.detail}", file=sys.stderr)
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
