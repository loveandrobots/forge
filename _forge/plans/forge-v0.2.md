# Forge v0.2 — Implementation Plan

**Spec:** `_forge/specs/forge-v0.2.md`

---

## Approach

Build Forge bottom-up: foundational modules first (config, models, database), then the core pipeline machinery (dispatcher, gate runner, prompt builder, engine), then the API layer and dashboard, and finally the gate scripts and self-hosting setup. Each task is a single focused session that produces working, tested code. Tasks are ordered so each one builds on the previous — no task requires code that hasn't been written yet.

The v0.1 scope is a single-process FastAPI server with an async pipeline engine that advances tasks through spec → plan → implement → review stages, runs gate scripts between stages, and serves a web dashboard. All Claude Code interaction is isolated in the dispatcher. No ORM, no frontend build step, no authentication.

---

## Task 1: Project scaffolding, config, and models

**Goal:** Set up the Python package, dependencies, configuration loading, and all Pydantic models.

**Files to create:**
- `requirements.txt`
- `config.yaml`
- `forge/__init__.py`
- `forge/__main__.py`
- `forge/config.py`
- `forge/models.py`

**Details:**

`requirements.txt` — pin major versions:
- fastapi, uvicorn[standard], jinja2, python-multipart
- pydantic, pydantic-settings
- pyyaml
- pytest, httpx (test client), ruff

`config.yaml` — default engine configuration:
```yaml
engine:
  poll_interval_seconds: 30
  max_concurrent_tasks: 1
  stage_timeout_seconds: 600
  default_max_retries: 3
claude:
  default_model: "opus"
  headless_flags: "--output-format stream-json"
```

`forge/config.py`:
- `Settings` dataclass or Pydantic model loaded from `config.yaml`
- `get_settings()` function that reads config.yaml from the project root
- Path constants: `BASE_DIR`, `DB_PATH`, `CONFIG_PATH`
- Valid stage names, status values, and link types as enums or constants

`forge/models.py` — Pydantic models for all API schemas:
- `ProjectCreate`, `ProjectUpdate`, `ProjectResponse`
- `TaskCreate`, `TaskUpdate`, `TaskResponse`
- `StageRunResponse`
- `RunLogEntry`
- `EngineStatus`
- `PipelineStats`

`forge/__main__.py` — entry point that imports and runs `cli.main()`

**Tests:** `tests/test_config.py`, `tests/test_models.py`
- Config loads from yaml, provides defaults for missing keys
- All Pydantic models validate correctly, reject bad input

---

## Task 2: Database schema and query layer

**Goal:** SQLite schema creation, migrations, and all query functions used by the rest of the app.

**Files to create:**
- `forge/database.py`

**Details:**

`forge/database.py`:
- `get_connection()` → returns `sqlite3.Connection` with `row_factory = sqlite3.Row`, WAL mode
- `migrate()` — creates all tables if they don't exist (idempotent):
  - `projects` (id, name, repo_path, default_branch, gate_dir, skill_refs, created_at, config)
  - `tasks` (id, project_id, title, description, priority, current_stage, status, branch_name, spec_path, plan_path, review_path, skill_overrides, max_retries, created_at, updated_at, completed_at)
  - `stage_runs` (id, task_id, stage, attempt, status, prompt_sent, started_at, finished_at, duration_seconds, claude_output, artifacts_produced, gate_name, gate_exit_code, gate_stdout, gate_stderr, tokens_used, error_message)
  - `task_links` (id, source_task_id, target_task_id, link_type, created_at)
  - `run_log` (id autoincrement, timestamp, level, message, task_id, stage_run_id, metadata)

Query functions (all parameterized, no string formatting):

**Projects:**
- `insert_project(conn: Connection, *, name: str, repo_path: str, default_branch: str = "main", gate_dir: str = "gates", skill_refs: list[str] | None = None, config: dict | None = None) -> str` — generates UUID, sets created_at, returns project id
- `get_project(conn: Connection, project_id: str) -> Row | None` — lookup by primary key
- `get_project_by_name(conn: Connection, name: str) -> Row | None` — lookup by unique name
- `list_projects(conn: Connection) -> list[Row]` — all projects ordered by name
- `update_project(conn: Connection, project_id: str, *, name: str | None = None, repo_path: str | None = None, default_branch: str | None = None, gate_dir: str | None = None, skill_refs: list[str] | None = None, config: dict | None = None) -> bool` — updates only provided fields, returns True if a row was modified

**Tasks:**
- `insert_task(conn: Connection, *, project_id: str, title: str, description: str = "", priority: int = 0, skill_overrides: list[str] | None = None, max_retries: int = 3) -> str` — generates UUID, sets status="backlog", current_stage=NULL, created_at/updated_at, returns task id
- `get_task(conn: Connection, task_id: str) -> Row | None` — lookup by primary key
- `list_tasks(conn: Connection, *, project_id: str | None = None, status: str | None = None, priority_gte: int | None = None) -> list[Row]` — filtered list ordered by priority DESC, created_at ASC
- `update_task(conn: Connection, task_id: str, *, title: str | None = None, description: str | None = None, priority: int | None = None, status: str | None = None, current_stage: str | None = None, branch_name: str | None = None, spec_path: str | None = None, plan_path: str | None = None, review_path: str | None = None, skill_overrides: list[str] | None = None, completed_at: str | None = None) -> bool` — updates only provided fields, always sets updated_at, returns True if a row was modified
- `delete_task(conn: Connection, task_id: str) -> bool` — deletes only if status="backlog", returns True if deleted
- `get_next_queued_task(conn: Connection) -> Row | None` — joins tasks and stage_runs to find the highest-priority active task that has a stage_run with status="queued"; returns the task row

**Stage runs:**
- `insert_stage_run(conn: Connection, *, task_id: str, stage: str, attempt: int, status: str = "queued", prompt_sent: str | None = None) -> str` — generates UUID, sets started_at=NULL, returns stage_run id
- `get_stage_run(conn: Connection, stage_run_id: str) -> Row | None` — lookup by primary key
- `list_stage_runs(conn: Connection, *, task_id: str | None = None, stage: str | None = None, status: str | None = None) -> list[Row]` — filtered list ordered by started_at ASC
- `update_stage_run(conn: Connection, stage_run_id: str, *, status: str | None = None, prompt_sent: str | None = None, started_at: str | None = None, finished_at: str | None = None, duration_seconds: float | None = None, claude_output: str | None = None, artifacts_produced: list[str] | None = None, gate_name: str | None = None, gate_exit_code: int | None = None, gate_stdout: str | None = None, gate_stderr: str | None = None, tokens_used: int | None = None, error_message: str | None = None) -> bool` — updates only provided fields, returns True if a row was modified
- `get_retry_count(conn: Connection, task_id: str, stage: str) -> int` — counts stage_runs for this task+stage with status in ("bounced", "error")

**Task links:**
- `insert_task_link(conn: Connection, *, source_task_id: str, target_task_id: str, link_type: str) -> str` — generates UUID, sets created_at, returns link id. link_type must be one of: "blocks", "created_by", "follows", "related"
- `get_task_links(conn: Connection, task_id: str) -> list[Row]` — returns all links where task_id is either source or target, ordered by created_at

**Run log:**
- `insert_log(conn: Connection, *, level: str, message: str, task_id: str | None = None, stage_run_id: str | None = None, metadata: dict | None = None) -> int` — sets timestamp to now, level must be one of "info", "warn", "error", returns the autoincrement id
- `get_logs(conn: Connection, *, level: str | None = None, task_id: str | None = None, project_id: str | None = None, limit: int = 100, offset: int = 0) -> list[Row]` — filtered and paginated, ordered by timestamp DESC. project_id filter requires joining through task_id → tasks.project_id

**Tests:** `tests/test_database.py`
- Schema creation is idempotent (call migrate twice, no error)
- CRUD operations for each table
- `get_next_queued_task()` returns highest-priority task
- Filtering and pagination work correctly
- Foreign key relationships hold

---

## Task 3: CLI commands

**Goal:** CLI for project initialization, listing, task creation, migration, and server startup.

**Files to create:**
- `forge/cli.py`

**Details:**

Use `argparse` (no external CLI library needed). Commands:

- `python -m forge migrate` — calls `database.migrate()`
- `python -m forge init-project --name NAME --repo-path PATH --default-branch BRANCH --gate-dir DIR [--skills SKILLS]` — validates inputs, inserts project record
- `python -m forge list-projects` — prints registered projects as a table
- `python -m forge add-task --project NAME --title TITLE --description DESC --priority N` — looks up project by name, inserts task with status=backlog
- `python -m forge serve --host HOST --port PORT` — starts uvicorn with the FastAPI app

Each command validates inputs and prints clear error messages for missing projects, duplicate names, etc.

**Tests:** `tests/test_cli.py`
- `migrate` creates tables successfully
- `init-project` creates a project record in the database
- `init-project` with duplicate name fails gracefully
- `add-task` creates a task linked to the correct project
- `list-projects` outputs registered projects

---

## Task 4: Dispatcher (Claude Code subprocess interface)

**Goal:** Isolated module for spawning Claude Code CLI sessions and capturing output.

**Files to create:**
- `forge/dispatcher.py`

**Details:**

`forge/dispatcher.py`:
- `async def dispatch_claude(prompt: str, repo_path: str, branch: str, timeout: int) -> DispatchResult`
  - `DispatchResult` dataclass: `output: str`, `exit_code: int`, `duration_seconds: float`, `tokens_used: int | None`, `error: str | None`
  - `cd` to repo_path
  - Checkout the branch (create from default if needed): `git checkout {branch} || git checkout -b {branch}`
  - Run: `claude -p "{prompt}" --output-format stream-json`
  - Use `asyncio.create_subprocess_exec` with stdout/stderr pipes
  - Stream and accumulate output
  - Apply timeout via `asyncio.wait_for`; if timeout, kill process and return error
  - Parse stream-json output to extract final text and token usage if available
  - Return `DispatchResult`

- `async def create_branch(repo_path: str, branch: str, base_branch: str) -> bool`
  - Create feature branch from base_branch
  - Return success/failure

- `async def rebase_branch(repo_path: str, branch: str, base_branch: str) -> bool`
  - Rebase feature branch on base_branch
  - Return success/failure (False means needs_human)

**Tests:** `tests/test_dispatcher.py`
- Test branch creation/checkout with a real git repo (tmpdir fixture)
- Test timeout handling (mock subprocess that sleeps)
- Test output parsing from stream-json format
- Test error handling when claude CLI is not available

---

## Task 5: Gate runner

**Goal:** Module that executes gate scripts from the target project repo and interprets results.

**Files to create:**
- `forge/gate_runner.py`

**Details:**

`forge/gate_runner.py`:
- `async def run_gate(gate_dir: str, stage: str, env_vars: dict) -> GateResult`
  - `GateResult` dataclass: `passed: bool`, `exit_code: int`, `stdout: str`, `stderr: str`, `gate_name: str`, `duration_seconds: float`
  - Determine gate script path: `{gate_dir}/post-{stage}.sh`
  - If gate script doesn't exist, pass by default (log a warning)
  - Set environment variables per the gate contract:
    - `FORGE_TASK_ID`, `FORGE_STAGE`, `FORGE_ATTEMPT`, `FORGE_REPO_PATH`, `FORGE_BRANCH`
    - `FORGE_SPEC_PATH`, `FORGE_PLAN_PATH`, `FORGE_REVIEW_PATH`
  - Execute via `asyncio.create_subprocess_exec` with `bash` or direct execution
  - Capture stdout, stderr, exit code
  - Return `GateResult`

- `def build_gate_env(task, stage_run, project) -> dict` — assembles the env vars dict from task/project/stage_run data

**Tests:** `tests/test_gate_runner.py`
- Test with a passing gate script (exit 0)
- Test with a failing gate script (exit 1, stderr message)
- Test with a missing gate script (should pass with warning)
- Test that environment variables are set correctly

---

## Task 6: Prompt builder

**Goal:** Module that assembles stage-specific prompts from templates and task/artifact data.

**Files to create:**
- `forge/prompt_builder.py`

**Details:**

`forge/prompt_builder.py`:
- Prompt templates as string constants for each stage (spec, plan, implement, review) — copy from spec section 8
- `def build_prompt(stage: str, task: dict, project: dict, stage_run: dict, artifacts: dict) -> str`
  - `artifacts` dict has keys like `spec_content`, `plan_content`, `git_diff`
  - Select the template for the stage
  - Fill in: project_name, task_title, task_description, branch_name, skill_references
  - Load artifact content from file paths (spec_path, plan_path)
  - Append retry context if attempt > 1 (gate_stderr from previous run)
  - Return the assembled prompt string

- `def load_artifact(path: str) -> str` — reads an artifact file, returns content or empty string

- `def build_retry_context(attempt: int, previous_gate_stderr: str) -> str` — formats the retry section

- `def get_git_diff(repo_path: str, branch: str, base_branch: str) -> str` — runs `git diff` for the review stage

**Tests:** `tests/test_prompt_builder.py`
- Each stage template is filled correctly with no missing placeholders
- Retry context is appended only when attempt > 1
- Missing artifacts produce empty strings, not errors
- Git diff function returns diff output

---

## Task 7: Pipeline engine

**Goal:** The core async loop that drives tasks through the pipeline stages.

**Files to create:**
- `forge/engine.py`

**Details:**

`forge/engine.py`:
- `class PipelineEngine`:
  - `__init__(settings, db_path)` — store config, initialize state
  - `running: bool` — toggle for start/pause
  - `current_task_id: str | None` — what's currently executing
  - `async def start()` — set running=True, begin the loop
  - `async def pause()` — set running=False (finishes current work)
  - `async def run_loop()` — the main while loop:
    1. Check for timed-out running stage_runs → mark error, handle retry
    2. Call `database.get_next_queued_task()` → if None, sleep poll_interval
    3. Build prompt via `prompt_builder.build_prompt()`
    4. Dispatch via `dispatcher.dispatch_claude()`
    5. Run gate via `gate_runner.run_gate()`
    6. Record results, advance or bounce:
       - Gate passed → create next stage_run (or mark done if review passed)
       - Gate failed → increment retry, re-queue or mark needs_human
       - Error → retry or mark failed
    7. Log to run_log

  - `async def advance_task(task_id, current_stage)` — creates the next stage_run:
    - spec → plan, plan → implement, implement → review, review → done
  - `async def bounce_task(task_id, stage, gate_result)` — handles retry logic
  - `async def handle_timeout(stage_run)` — marks timed-out runs

  - `def get_status() -> EngineStatus` — returns running state, current task, queue depth
  - `def get_stats() -> PipelineStats` — query aggregate metrics

Stage sequence constant: `STAGES = ["spec", "plan", "implement", "review"]`

Branch management:
- Before first stage_run of a task: create feature branch `forge/{short_id}-{slug}`
- Before implement stage: rebase on default branch; if fails → needs_human

**Tests:** `tests/test_engine.py`
- Engine picks highest-priority task
- Stage advancement follows correct order (spec→plan→implement→review→done)
- Gate failure triggers bounce with retry context
- Max retries exceeded triggers needs_human
- Timeout detection marks stage_run as error
- Engine respects pause state
- Branch naming follows convention

---

## Task 8: FastAPI app and API routers

**Goal:** The FastAPI application with all API endpoints for projects, tasks, pipeline control, and stage runs.

**Files to create:**
- `forge/main.py`
- `forge/routers/__init__.py`
- `forge/routers/projects.py`
- `forge/routers/tasks.py`
- `forge/routers/pipeline.py`

**Details:**

`forge/main.py`:
- Create FastAPI app
- Mount static files, configure Jinja2 templates
- On startup: run `database.migrate()`, create `PipelineEngine` instance, start engine as background task
- On shutdown: pause engine gracefully
- Include all routers

`forge/routers/projects.py`:
- `GET /api/projects` — list all projects
- `POST /api/projects` — create project (validate name uniqueness, repo path exists)
- `GET /api/projects/{id}` — project detail
- `PATCH /api/projects/{id}` — update project settings

`forge/routers/tasks.py`:
- `GET /api/tasks` — list with filters (status, project_id, priority)
- `POST /api/tasks` — create task (status=backlog)
- `GET /api/tasks/{id}` — task detail with stage_run history
- `PATCH /api/tasks/{id}` — update task fields
- `DELETE /api/tasks/{id}` — delete (only backlog tasks)
- `POST /api/tasks/{id}/resume` — resume needs_human task (creates new stage_run)
- `POST /api/tasks/{id}/pause` — pause active task
- `POST /api/tasks/{id}/retry` — force retry current stage

`forge/routers/pipeline.py`:
- `GET /api/engine/status` — engine status
- `POST /api/engine/start` — start engine
- `POST /api/engine/pause` — pause engine
- `GET /api/engine/stats` — pipeline statistics
- `GET /api/stage-runs` — list stage runs with filtering
- `GET /api/stage-runs/{id}` — stage run detail
- `GET /api/logs` — paginated run log
- `GET /api/logs/stream` — SSE stream for live log updates

**Tests:** `tests/test_routers_projects.py`, `tests/test_routers_tasks.py`, `tests/test_routers_pipeline.py`
- All CRUD operations return correct status codes and response shapes
- Filters work correctly
- Invalid operations return appropriate errors (delete non-backlog task → 400)
- Task status transitions are enforced
- Engine start/pause toggles state

---

## Task 9: Dashboard templates and static assets

**Goal:** Server-rendered dashboard with Jinja2 + htmx for pipeline visualization and task management.

**Files to create:**
- `forge/routers/dashboard.py`
- `templates/base.html`
- `templates/pipeline.html`
- `templates/task_detail.html`
- `templates/backlog.html`
- `templates/settings.html`
- `templates/logs.html`
- `static/styles.css`
- `static/app.js`

**Details:**

`forge/routers/dashboard.py`:
- `GET /` — pipeline view (kanban board)
- `GET /tasks/{task_id}` — task detail page
- `GET /backlog` — backlog management page
- `GET /settings` — settings page (view-only in v0.1)
- `GET /logs` — run log page

`templates/base.html`:
- HTML5 boilerplate, htmx script tag, stylesheet link
- Navigation: Pipeline, Backlog, Logs, Settings
- Content block for page-specific content

`templates/pipeline.html`:
- Horizontal kanban with columns: Backlog, Spec, Plan, Implement, Review, Done
- Each task as a card: title, project badge, priority, attempt number, time in stage
- `needs_human` indicator on relevant cards
- Engine status toggle (start/pause) at top
- "New task" button
- Project filter dropdown
- htmx polling (hx-get, hx-trigger="every 5s") for live updates

`templates/task_detail.html`:
- Task metadata header (title, project, status, priority)
- Timeline of stage runs: stage name, status badge, duration, attempt number
- Expandable sections for each run: prompt, output, gate result
- Gate failure reason prominently displayed for bounced runs
- "Resolve and resume" button for needs_human tasks

`templates/backlog.html`:
- Task list grouped by project, ordered by priority
- Create task form: project dropdown, title, description, priority, skill overrides
- Edit button on each task (inline editing via htmx)

`templates/settings.html`:
- Project list with config (view-only in v0.1)
- Engine settings display

`templates/logs.html`:
- Chronological log feed
- Filters: level (info/warn/error), task, project
- Auto-scroll to latest

`static/styles.css`:
- Clean, minimal CSS for the dashboard
- Kanban column layout (flexbox)
- Card styling, status badges, priority indicators
- Responsive basics

`static/app.js`:
- htmx configuration
- SSE connection setup for live log updates (if endpoint exists)
- Auto-scroll behavior for logs page

**Tests:** `tests/test_dashboard.py`
- Dashboard pages return 200 status codes
- Pipeline view contains expected column structure
- Task detail page renders stage run history

---

## Task 10: Gate scripts for Forge

**Goal:** Forge's own gate scripts that validate pipeline artifacts.

**Files to create:**
- `gates/post-spec.sh`
- `gates/post-plan.sh`
- `gates/post-implement.sh`
- `gates/post-review.sh`

**Details:**

`gates/post-spec.sh`:
- Check spec file exists at `$FORGE_REPO_PATH/_forge/specs/$FORGE_TASK_ID.md`
- Check file is >200 chars
- Check for required sections: "## Acceptance criteria", "## Out of scope"
- Exit 0 on pass, exit 1 with stderr reason on fail

`gates/post-plan.sh`:
- Check plan file exists at `$FORGE_REPO_PATH/_forge/plans/$FORGE_TASK_ID.md`
- Check file is >200 chars
- Check for required sections: references to spec acceptance criteria, test descriptions, files to create/modify
- Exit 0/1

`gates/post-implement.sh`:
- cd to `$FORGE_REPO_PATH`
- Run `python -m pytest tests/` — capture failures
- Run `ruff check forge/` — capture lint errors
- Aggregate errors, exit 1 if any

`gates/post-review.sh`:
- Check review file exists at `$FORGE_REPO_PATH/_forge/reviews/$FORGE_TASK_ID.md`
- Check for verdict ("PASS" or "ISSUES")
- If "ISSUES", check for specific actionable items (not empty)
- Exit 0/1

All scripts: `chmod +x`, use `#!/bin/bash`, follow the env var contract from the spec.

**Tests:** `tests/test_gates.py`
- Each gate script passes with valid artifacts
- Each gate script fails with missing/malformed artifacts
- Correct stderr messages on failure

---

## Task 11: Integration testing and self-registration

**Goal:** End-to-end test of the full pipeline flow. Register Forge as its own first project.

**Files to create:**
- `tests/test_integration.py`

**Details:**

`tests/test_integration.py`:
- Full pipeline flow test (with mocked dispatcher to avoid real Claude calls):
  1. Migrate database
  2. Create a project
  3. Create a task
  4. Engine picks up task, dispatches spec stage (mock dispatcher returns canned output)
  5. Gate runs on the canned spec artifact
  6. Task advances to plan stage
  7. Continue through all stages
  8. Task reaches done status
- Test bounce flow: mock gate failure, verify retry with context
- Test needs_human flow: mock max_retries exceeded
- Test engine pause/resume
- Test API endpoints return correct data during pipeline execution

Self-registration:
- Verify `python -m forge migrate` works
- Verify `python -m forge init-project --name "Forge" --repo-path "$(pwd)" --default-branch main --gate-dir gates` registers successfully
- Verify project appears in `list-projects` and dashboard

**Tests:** The task itself is the tests. Verify the full system works end-to-end.

---

## Risks

- **Claude Code CLI availability:** The dispatcher depends on `claude` being in PATH. Tests should mock the subprocess call. Integration testing with real Claude calls is manual.
- **Gate script portability:** Gate scripts use bash and common Unix tools. Should work on any Linux/macOS system but won't run on Windows.
- **SQLite concurrency:** WAL mode handles concurrent reads well, but the async engine accessing SQLite from multiple coroutines needs care — use a single connection or connection-per-call with proper closing.
- **htmx polling load:** 5-second polling on the pipeline view is fine for single-user use but could be optimized with SSE later.

---

## Summary

| Task | Creates | Tests |
|------|---------|-------|
| 1. Scaffolding, config, models | requirements.txt, config.yaml, forge/__init__.py, forge/__main__.py, forge/config.py, forge/models.py | test_config.py, test_models.py |
| 2. Database | forge/database.py | test_database.py |
| 3. CLI | forge/cli.py | test_cli.py |
| 4. Dispatcher | forge/dispatcher.py | test_dispatcher.py |
| 5. Gate runner | forge/gate_runner.py | test_gate_runner.py |
| 6. Prompt builder | forge/prompt_builder.py | test_prompt_builder.py |
| 7. Engine | forge/engine.py | test_engine.py |
| 8. API routers | forge/main.py, forge/routers/{__init__,projects,tasks,pipeline}.py | test_routers_{projects,tasks,pipeline}.py |
| 9. Dashboard | forge/routers/dashboard.py, templates/*.html, static/* | test_dashboard.py |
| 10. Gate scripts | gates/post-{spec,plan,implement,review}.sh | test_gates.py |
| 11. Integration | tests/test_integration.py | test_integration.py |
