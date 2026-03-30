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
