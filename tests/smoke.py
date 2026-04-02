"""Standalone smoke test module for Forge endpoint and page verification.

Can be run three ways:
    python -m tests.smoke          # standalone script
    from tests.smoke import run_smoke_tests  # importable
    pytest tests/test_smoke.py     # via pytest wrapper
"""

from __future__ import annotations

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
]

# API endpoints: (method, url, kwargs)
_API_CHECKS: list[tuple[str, str, dict]] = [
    ("GET", "/api/engine/status", {}),
    ("GET", "/api/tasks", {}),
    ("GET", "/api/projects", {}),
    ("GET", "/api/logs", {}),
]

# POST-only endpoints that should reject GET with 405
_POST_ONLY_ENDPOINTS: list[str] = [
    "/api/engine/start",
    "/api/engine/pause",
]


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

        return {"project_id": project_id, "task_ids": task_ids}
    finally:
        conn.close()


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

            from starlette.testclient import TestClient

            from forge.main import app

            with TestClient(app) as client:
                # Check dashboard pages
                for page_url in _DASHBOARD_PAGES:
                    name = f"GET {page_url}"
                    try:
                        resp = client.get(page_url)
                        results.append(
                            SmokeResult(
                                name=name,
                                passed=resp.status_code == 200,
                                status_code=resp.status_code,
                                detail=""
                                if resp.status_code == 200
                                else f"expected 200, got {resp.status_code}",
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

                # Check task detail page
                first_task_id = next(iter(seed_info["task_ids"].values()))
                task_url = f"/tasks/{first_task_id}"
                name = f"GET {task_url}"
                try:
                    resp = client.get(task_url)
                    results.append(
                        SmokeResult(
                            name=name,
                            passed=resp.status_code == 200,
                            status_code=resp.status_code,
                            detail=""
                            if resp.status_code == 200
                            else f"expected 200, got {resp.status_code}",
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

                # Check API GET endpoints
                for method, url, kwargs in _API_CHECKS:
                    name = f"{method} {url}"
                    try:
                        resp = client.get(url, **kwargs)
                        results.append(
                            SmokeResult(
                                name=name,
                                passed=resp.status_code == 200,
                                status_code=resp.status_code,
                                detail=""
                                if resp.status_code == 200
                                else f"expected 200, got {resp.status_code}",
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

                # POST /api/tasks
                name = "POST /api/tasks"
                try:
                    resp = client.post(
                        "/api/tasks",
                        json={
                            "title": "Smoke POST task",
                            "project_id": seed_info["project_id"],
                            "description": "Created by smoke test",
                        },
                    )
                    results.append(
                        SmokeResult(
                            name=name,
                            passed=resp.status_code == 201,
                            status_code=resp.status_code,
                            detail=""
                            if resp.status_code == 201
                            else f"expected 201, got {resp.status_code}",
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

                # POST /api/engine/start and /api/engine/pause
                for post_url in _POST_ONLY_ENDPOINTS:
                    name = f"POST {post_url}"
                    try:
                        resp = client.post(post_url)
                        results.append(
                            SmokeResult(
                                name=name,
                                passed=resp.status_code == 200,
                                status_code=resp.status_code,
                                detail=""
                                if resp.status_code == 200
                                else f"expected 200, got {resp.status_code}",
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

                # Verify POST-only endpoints reject GET with 405
                for post_url in _POST_ONLY_ENDPOINTS:
                    name = f"GET {post_url} (expect 405)"
                    try:
                        resp = client.get(post_url)
                        results.append(
                            SmokeResult(
                                name=name,
                                passed=resp.status_code == 405,
                                status_code=resp.status_code,
                                detail=""
                                if resp.status_code == 405
                                else f"expected 405, got {resp.status_code}",
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
