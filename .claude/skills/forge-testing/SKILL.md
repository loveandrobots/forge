---
name: forge-testing
description: |
  Testing conventions for Forge. Load when writing or fixing tests.
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

# Testing conventions

## Stage flow changes

For any change to the engine's stage flow or bounce logic, tests must include an integration test that creates a task, advances it through the relevant stages, triggers the condition being changed, and verifies the resulting database state. Unit tests of helper functions are not sufficient.

## Database isolation

All tests use an isolated temporary database. This is handled by the `_use_tmp_db` autouse fixture in `tests/conftest.py`. The fixture:

- Creates a temp SQLite file via `tmp_path`
- Patches `DB_PATH` in every module that imports it (forge.config, forge.cli, forge.main, and all routers)
- Runs the schema migration against the temp database
- No-ops the PipelineEngine start/pause methods to prevent background activity during tests

You do not need to set up database isolation in individual test files — the conftest fixture handles it automatically.

**If you add a new module that imports `DB_PATH` from `forge.config`, you must add its patch location to `_DB_PATH_LOCATIONS` in `tests/conftest.py`.** Forgetting this causes that module to hit the production database during tests.

## Test quality

- Each test should assert one specific behavior.
- Test names describe the behavior: `test_engine_bounces_task_after_max_retries`, not `test_engine_3`.
- Run tests with `python -m pytest tests/ -W error`. Warnings are failures.

## Pre-existing failures

If you encounter failing tests that existed before your change, fix them. Do not skip, ignore, or comment them out. Note in the commit message that you fixed a pre-existing test failure.

## Integration tests for endpoints and UI

Unit tests of handler functions are not sufficient for API or UI changes.
Every new or modified endpoint must have a test that uses FastAPI's
TestClient to send an actual HTTP request and check the response:

    from fastapi.testclient import TestClient
    from forge.main import app

    client = TestClient(app)

    def test_reset_task_endpoint():
        # Create a task in needs_human status first
        # ...
        response = client.post(f"/api/tasks/{task_id}/reset",
                               json={"from_stage": "spec"})
        assert response.status_code == 200
        # Verify the task was actually reset in the database

Do not test endpoints by calling the handler function directly.
Do not mock the FastAPI request/response cycle.
The TestClient sends a real HTTP request through the full router
stack — this catches wrong HTTP methods, missing routes, incorrect
path parameters, and middleware issues.

For UI pages that use htmx, the test must verify the rendered HTML
contains the correct htmx attributes:

    def test_task_detail_has_reset_button():
        response = client.get(f"/tasks/{task_id}")
        assert response.status_code == 200
        assert f'hx-post="/api/tasks/{task_id}/reset"' in response.text

This catches: wrong URL in the template, wrong HTTP method in the
htmx attribute, missing form elements, and conditional rendering bugs.

