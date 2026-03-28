# Forge — Orchestrator Specification

**Version 0.2 · March 2026**

---

## 1. Purpose

Forge is a pipeline orchestrator that manages semi-autonomous software development across one or more projects. It was created to replace an ad-hoc multi-agent system (Paperclipai) that suffered from context exhaustion, agents ignoring instructions, skipped validation, and progressive quality degradation across releases.

The first project managed by Forge is Forge itself (dogfooding). The second is Olivia, an iOS household management app for neurodiverse users.

### Core problem it solves

AI coding agents produce good work when given narrow, well-defined tasks with fresh context and mechanical quality gates. They produce bad work when given broad mandates, long-running sessions, and the ability to self-certify completion. Forge enforces the former pattern.

### Design principles

1. **Stages, not sessions.** Every unit of work passes through a pipeline of discrete stages. Each stage runs in a fresh Claude Code context window. No single agent session spans multiple stages.

2. **Gates, not instructions.** Quality enforcement happens through scripts that run deterministically after each stage — not through prompts that agents may ignore. An agent cannot advance work past a gate it hasn't passed.

3. **Artifacts, not conversations.** Each stage produces a file artifact (spec, plan, code, review). The next stage consumes that artifact as input. State lives in files and the database, not in conversation history.

4. **Separate concerns.** The orchestrator manages the pipeline. Each target project repo contains the skills, hooks, and gate scripts that define how project-specific work gets done. Forge doesn't know about Olivia's brand guidelines — it just knows where the repo is and which gates to run.

---

## 2. Architecture overview

### Components

- **FastAPI server** — API endpoints for task and project management, pipeline control, and dashboard serving
- **Pipeline engine** — async background loop that polls for queued work, dispatches Claude Code sessions, runs gates, and advances or bounces tasks
- **SQLite database** — project config, task state, stage run history, gate results
- **Web dashboard** — pipeline visualization, backlog management, task detail, run logs, manual controls
- **Gate runner** — executes gate scripts from the target project repo and interprets results
- **CLI** — project initialization and administrative commands

### Deployment

Runs on a Linux server continuously. The FastAPI server and pipeline engine run as a single process (engine as an asyncio background task). The dashboard is served by the same FastAPI instance.

### External dependencies

- **Claude Code CLI** — installed on the server, authenticated, available as `claude` in PATH
- **Git** — for branch management in target project repos
- **Target project repos** — cloned locally on the server

### Repository structure

```
forge/
├── forge/
│   ├── __init__.py
│   ├── main.py              # FastAPI app, startup, shutdown
│   ├── cli.py               # CLI commands (init-project, etc.)
│   ├── config.py             # Settings, paths, defaults
│   ├── database.py           # SQLite connection, schema, migrations
│   ├── models.py             # Pydantic models for API
│   ├── engine.py             # Pipeline engine (async loop)
│   ├── dispatcher.py         # Claude Code session spawning
│   ├── gate_runner.py        # Gate script execution
│   ├── prompt_builder.py     # Assembles stage-specific prompts
│   └── routers/
│       ├── projects.py       # Project CRUD endpoints
│       ├── tasks.py          # Task CRUD endpoints
│       ├── pipeline.py       # Pipeline control endpoints
│       └── dashboard.py      # Dashboard page routes
├── templates/                # Jinja2 templates for dashboard
│   ├── base.html
│   ├── pipeline.html         # Kanban pipeline view
│   ├── task_detail.html      # Individual task history
│   ├── backlog.html          # Task creation and management
│   ├── settings.html         # Project and engine settings
│   └── logs.html             # Live run log
├── static/                   # CSS, JS for dashboard
│   ├── styles.css
│   └── app.js                # htmx config, SSE for live updates
├── gates/                    # Forge's own gate scripts
│   ├── post-spec.sh
│   ├── post-plan.sh
│   ├── post-implement.sh
│   └── post-review.sh
├── _forge/                   # Forge's own pipeline artifacts
│   ├── specs/
│   ├── plans/
│   └── reviews/
├── tests/                    # Forge's own tests
├── forge.db                  # SQLite database (gitignored)
├── config.yaml               # Default configuration
├── requirements.txt
├── CLAUDE.md                 # Claude Code instructions for this repo
└── README.md
```

---

## 3. CLI

### Project initialization

Projects are created via a CLI command. This is the primary way to register a project with Forge in v0.1.

```bash
# Initialize Forge as its own first project
python -m forge init-project \
  --name "Forge" \
  --repo-path "/home/user/projects/forge" \
  --default-branch "main" \
  --gate-dir "gates"

# Later, add Olivia
python -m forge init-project \
  --name "Olivia" \
  --repo-path "/home/user/projects/olivia" \
  --default-branch "main" \
  --gate-dir "gates" \
  --skills "olivia-voice,olivia-ui,olivia-brand-gate,olivia-conventions"
```

The CLI writes a project record directly to the SQLite database. The dashboard displays registered projects and allows switching between them.

### Other CLI commands

```bash
# List registered projects
python -m forge list-projects

# Start the server (API + engine + dashboard)
python -m forge serve --host 0.0.0.0 --port 8000

# Run the database schema migration
python -m forge migrate

# Create a task from the command line (useful for scripting)
python -m forge add-task \
  --project "Forge" \
  --title "Add WebSocket support to run log" \
  --description "Replace htmx polling with SSE for live log updates" \
  --priority 5
```

---

## 4. Data model

### Tables

#### `projects`

A project represents a target repository that the orchestrator manages work for.

| Column | Type | Description |
|--------|------|-------------|
| id | TEXT PRIMARY KEY | UUID |
| name | TEXT NOT NULL UNIQUE | Human-readable name (e.g., "Forge", "Olivia") |
| repo_path | TEXT NOT NULL | Absolute path to local repo clone |
| default_branch | TEXT DEFAULT 'main' | Default git branch |
| gate_dir | TEXT DEFAULT 'gates' | Relative path to gate scripts within repo |
| skill_refs | TEXT | JSON array of skill names to load by default |
| created_at | TIMESTAMP | |
| config | TEXT | JSON blob for project-specific settings |

#### `tasks`

A task is a unit of work that flows through the pipeline.

| Column | Type | Description |
|--------|------|-------------|
| id | TEXT PRIMARY KEY | UUID |
| project_id | TEXT NOT NULL | FK to projects |
| title | TEXT NOT NULL | Short description |
| description | TEXT | Full task description, acceptance criteria, context |
| priority | INTEGER DEFAULT 0 | Higher = more urgent. Engine picks highest priority first |
| current_stage | TEXT | Current pipeline stage (NULL = backlog) |
| status | TEXT NOT NULL DEFAULT 'backlog' | One of: backlog, active, paused, needs_human, done, failed |
| branch_name | TEXT | Git branch created for this task |
| spec_path | TEXT | Path to spec artifact (relative to repo) |
| plan_path | TEXT | Path to plan artifact |
| review_path | TEXT | Path to review artifact |
| skill_overrides | TEXT | JSON array of additional skills for this task |
| max_retries | INTEGER DEFAULT 3 | Max gate failures per stage before escalating |
| created_at | TIMESTAMP | |
| updated_at | TIMESTAMP | |
| completed_at | TIMESTAMP | |

Valid `current_stage` values: `spec`, `plan`, `implement`, `review`

Valid `status` transitions:
- `backlog` → `active` (engine picks it up)
- `active` → `paused` (manual pause)
- `paused` → `active` (manual resume)
- `active` → `needs_human` (max retries exceeded or agent requests help)
- `active` → `done` (all stages passed)
- `active` → `failed` (unrecoverable error)
- `needs_human` → `active` (human resolves and resumes)

#### `stage_runs`

Each attempt at a stage for a task. A task may have multiple stage runs for the same stage if it bounces.

| Column | Type | Description |
|--------|------|-------------|
| id | TEXT PRIMARY KEY | UUID |
| task_id | TEXT NOT NULL | FK to tasks |
| stage | TEXT NOT NULL | spec, plan, implement, review |
| attempt | INTEGER NOT NULL | Attempt number (1, 2, 3...) |
| status | TEXT NOT NULL | queued, running, passed, failed, bounced, error |
| prompt_sent | TEXT | The full prompt dispatched to Claude Code |
| started_at | TIMESTAMP | |
| finished_at | TIMESTAMP | |
| duration_seconds | REAL | |
| claude_output | TEXT | Raw output from Claude Code session |
| artifacts_produced | TEXT | JSON array of file paths created/modified |
| gate_name | TEXT | Which gate script was run |
| gate_exit_code | INTEGER | Gate script exit code |
| gate_stdout | TEXT | Gate script stdout |
| gate_stderr | TEXT | Gate script stderr (failure reason) |
| tokens_used | INTEGER | Approximate token usage (if available from CC output) |
| error_message | TEXT | System error (not gate failure — e.g., CC crashed) |

#### `task_links`

Allows tasks to reference other tasks (e.g., review agent creates follow-up tasks).

| Column | Type | Description |
|--------|------|-------------|
| id | TEXT PRIMARY KEY | UUID |
| source_task_id | TEXT NOT NULL | The task that created the link |
| target_task_id | TEXT NOT NULL | The linked task |
| link_type | TEXT NOT NULL | One of: blocks, created_by, follows, related |
| created_at | TIMESTAMP | |

#### `run_log`

A chronological log of all engine activity, surfaced in the dashboard.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | |
| timestamp | TIMESTAMP NOT NULL | |
| level | TEXT NOT NULL | info, warn, error |
| message | TEXT NOT NULL | Human-readable log message |
| task_id | TEXT | FK to tasks (NULL for system-level events) |
| stage_run_id | TEXT | FK to stage_runs (NULL for non-stage events) |
| metadata | TEXT | JSON blob for structured data |

---

## 5. Pipeline stages

### Stage definitions

Each stage has a defined purpose, input, output, and gate.

#### Spec stage

- **Purpose:** Produce a clear, concrete specification for the task with measurable acceptance criteria
- **Input:** Task title + description from the database
- **Output:** A spec file written to `_forge/specs/{task_id}.md` in the target repo
- **Model:** Opus (highest leverage stage — a bad spec cascades into wasted work downstream)
- **Skills loaded:** Project-specific skills relevant to the task type
- **Gate:** `gates/post-spec.sh` — verifies spec file exists, has required sections (overview, acceptance criteria, out of scope), is non-trivially long (>200 chars)
- **Prompt guidance:** The agent should reference the project's existing documentation and design principles when writing acceptance criteria. Criteria should be binary (pass/fail), not subjective.

#### Plan stage

- **Purpose:** Produce an implementation plan with test definitions
- **Input:** Spec artifact from the previous stage
- **Output:** A plan file written to `_forge/plans/{task_id}.md`
- **Model:** Opus (architectural decisions require understanding the existing codebase and making trade-offs)
- **Skills loaded:** Same as spec, plus technical convention skills
- **Gate:** `gates/post-plan.sh` — verifies plan file exists, references spec acceptance criteria, includes test descriptions, identifies files to be created or modified
- **Prompt guidance:** The agent reads the spec and produces a concrete plan. The plan should be detailed enough that a different agent (with no context beyond the plan and the spec) could implement it.

#### Implement stage

- **Purpose:** Write code and tests that fulfill the spec according to the plan
- **Input:** Spec artifact + plan artifact + the existing codebase
- **Output:** Code changes on a feature branch, tests written and passing
- **Model:** Sonnet (execution against a well-defined plan — the hard decisions were made in spec/plan stages, so a faster/cheaper model works here)
- **Skills loaded:** All project skills (brand, UI, voice, technical conventions)
- **Gate:** `gates/post-implement.sh` — the heavy gate (project-specific; see section 7 for examples)
- **Prompt guidance:** The agent should write tests before or alongside implementation (TDD when practical). The agent commits to the task's feature branch. The agent should not modify files outside the scope defined in the plan.

#### Review stage

- **Purpose:** Adversarial review of the implementation against the spec
- **Input:** Spec artifact + the git diff of the feature branch vs. default branch
- **Output:** A review file written to `_forge/reviews/{task_id}.md` containing either a PASS verdict or specific issues found
- **Model:** Opus (adversarial analysis requires strong reasoning to find subtle issues)
- **Skills loaded:** Project brand and design skills
- **Gate:** `gates/post-review.sh` — verifies review file exists, contains a verdict (PASS or ISSUES), and if ISSUES, contains specific actionable items (not vague feedback)
- **Prompt guidance:** The review agent's job is to find problems, not confirm success. It should check: does the implementation match the spec's acceptance criteria? Does it violate any brand guidelines? Are there untested edge cases? Does the code change files not listed in the plan? If issues are found, the agent creates new tasks in the backlog (via writing a structured JSON file that the engine picks up).

### Model routing rationale

Spec and plan stages use Opus because they are the highest-leverage stages. A bad spec cascades into a bad plan, which cascades into a bad implementation, which bounces at the gate, wastes tokens, and fills the retry cycle. These stages require nuanced reasoning — understanding existing documentation, translating requirements into measurable criteria, making architectural decisions. Getting them right the first time saves everything downstream.

Implementation uses Sonnet because once there is a clear spec and a detailed plan, the implement stage is largely execution — write this component, add this test, follow this pattern. A good plan constrains the implementation enough that Sonnet can execute it reliably, at lower cost and higher speed.

Review uses Opus because adversarial analysis benefits from strong reasoning to find subtle issues that a weaker model might miss.

Note: in v0.1, all stages use the default model (Opus). Model routing per stage is a future enhancement, configurable per project. If Sonnet implementations bounce frequently, the project can switch to Opus for that stage.

### Stage flow

```
backlog → spec → [gate] → plan → [gate] → implement → [gate] → review → [gate] → done
                   ↑                ↑                     ↑                  ↑
                   └── bounce ──────┴──── bounce ─────────┴──── bounce ──────┘
```

When a gate fails:
1. The stage run is marked `bounced` with the gate's stderr as the reason
2. The task's retry count for that stage increments
3. If retries < max_retries: a new stage run is queued for the same stage, with the gate failure context appended to the prompt ("Previous attempt failed because: {reason}. Fix the issues and try again.")
4. If retries >= max_retries: the task is marked `needs_human`

---

## 6. Pipeline engine

### Behavior

The engine is an async loop that runs as a background task in the FastAPI process.

```
while running:
    1. Check for any stage_runs with status="running" that have exceeded timeout
       → mark as "error", increment retry count

    2. Find the highest-priority task with a queued stage_run
       → If none, sleep for poll_interval (default: 30 seconds)

    3. Build the prompt for this stage run:
       - Load the task description
       - Load the relevant artifact(s) from previous stages
       - Load the gate failure context (if this is a retry)
       - Include skill references
       - Include the stage-specific prompt template

    4. Dispatch to Claude Code:
       - cd to the project repo
       - Checkout the task's feature branch (create if first run)
       - Run: claude -p "{prompt}" --output-format stream-json
       - Stream and capture output
       - Wait for completion or timeout

    5. Run the gate script:
       - Execute the stage's gate script from the project repo
       - Capture exit code, stdout, stderr

    6. Record results:
       - Update the stage_run with output, gate results, timing
       - If gate passed: create next stage_run (queued), advance task
       - If gate failed: bounce logic (retry or needs_human)
       - If system error: mark as error, retry

    7. Log everything to run_log table
```

### Configuration

Defined in `config.yaml`:

```yaml
engine:
  poll_interval_seconds: 30
  max_concurrent_tasks: 1           # Start with 1, increase later
  stage_timeout_seconds: 600        # 10 minutes per stage
  default_max_retries: 3

claude:
  default_model: "opus"             # Default for all stages in v0.1
  headless_flags: "--output-format stream-json"
```

Project-specific configuration (repo path, gate directory, default skills) is stored in the `projects` database table, not in config.yaml. This allows adding projects at runtime via CLI or API without restarting the server.

### Concurrency

Initially single-threaded (one task at a time). The architecture supports concurrent execution (multiple asyncio tasks, each with its own git worktree), but this is a future enhancement. Starting with one task at a time avoids git conflicts and makes debugging straightforward.

### Branch management

Each task gets a feature branch: `forge/{task_id_short}-{slugified_title}` (e.g., `forge/a3f2-morning-checkin-screen`). The engine creates this branch from the default branch when the first stage starts. All implementation happens on this branch. When the task completes (all stages pass), the engine can optionally create a PR (configurable).

Before each implement stage, the engine rebases the feature branch on the default branch. If the rebase fails, the task is marked `needs_human`.

---

## 7. Gate interface

### Contract

A gate script is any executable file in the project's gate directory. The engine calls it with the following environment:

```
FORGE_TASK_ID=<task uuid>
FORGE_STAGE=<spec|plan|implement|review>
FORGE_ATTEMPT=<attempt number>
FORGE_REPO_PATH=<absolute path to repo>
FORGE_BRANCH=<feature branch name>
FORGE_SPEC_PATH=<path to spec artifact, if exists>
FORGE_PLAN_PATH=<path to plan artifact, if exists>
FORGE_REVIEW_PATH=<path to review artifact, if exists>
```

The gate script exits with:
- **0** — passed, advance to next stage
- **Non-zero** — failed, bounce back with stderr as the failure reason

The engine captures both stdout and stderr. Stdout is informational (stored for logging). Stderr is the failure reason shown to the retry prompt and the dashboard.

### Gate examples: Forge (Python project)

#### `gates/post-spec.sh`
```bash
#!/bin/bash
SPEC="$FORGE_REPO_PATH/_forge/specs/$FORGE_TASK_ID.md"
if [ ! -f "$SPEC" ]; then
    echo "Spec file not found at $SPEC" >&2
    exit 1
fi
if [ $(wc -c < "$SPEC") -lt 200 ]; then
    echo "Spec file is too short (< 200 chars)" >&2
    exit 1
fi
for section in "## Acceptance criteria" "## Out of scope"; do
    if ! grep -qi "$section" "$SPEC"; then
        echo "Spec missing required section: $section" >&2
        exit 1
    fi
done
echo "Spec gate passed"
exit 0
```

#### `gates/post-implement.sh` (Forge — Python)
```bash
#!/bin/bash
cd "$FORGE_REPO_PATH"
ERRORS=""

# Python type checking (if mypy or pyright configured)
if command -v pyright &> /dev/null; then
    if ! pyright forge/ 2>/tmp/forge-typecheck.log; then
        ERRORS="$ERRORS\nType errors:\n$(cat /tmp/forge-typecheck.log)"
    fi
fi

# Tests
if ! python -m pytest tests/ 2>/tmp/forge-test.log; then
    ERRORS="$ERRORS\nTest failures:\n$(cat /tmp/forge-test.log)"
fi

# Lint
if command -v ruff &> /dev/null; then
    if ! ruff check forge/ 2>/tmp/forge-lint.log; then
        ERRORS="$ERRORS\nLint errors:\n$(cat /tmp/forge-lint.log)"
    fi
fi

if [ -n "$ERRORS" ]; then
    echo -e "Implementation gate failed:$ERRORS" >&2
    exit 1
fi
echo "Implementation gate passed"
exit 0
```

### Gate examples: Olivia (TypeScript monorepo)

These scripts run against the Olivia repo using its existing tooling.

#### `gates/post-implement.sh` (Olivia — TypeScript)
```bash
#!/bin/bash
cd "$FORGE_REPO_PATH"
ERRORS=""

# TypeScript check (uses existing workspace-level script)
if ! npm run typecheck 2>/tmp/forge-tsc.log; then
    ERRORS="$ERRORS\nTypeScript errors:\n$(cat /tmp/forge-tsc.log)"
fi

# Lint (uses existing workspace-level script)
if ! npm run lint 2>/tmp/forge-lint.log; then
    ERRORS="$ERRORS\nLint errors:\n$(cat /tmp/forge-lint.log)"
fi

# Tests (uses existing workspace-level script — domain, API, e2e)
if ! npm test 2>/tmp/forge-test.log; then
    ERRORS="$ERRORS\nTest failures:\n$(cat /tmp/forge-test.log)"
fi

# Brand compliance checks
BRAND_VIOLATIONS=""

# Check for exclamation marks in user-facing component code
if grep -rn '!' --include='*.tsx' --include='*.ts' \
    apps/pwa/src/ | grep -v '!=\|!/\|!!.*assert\|\.test\.\|\.spec\.' | head -20; then
    BRAND_VIOLATIONS="$BRAND_VIOLATIONS\nExclamation marks found in UI components"
fi

# Check for forbidden words in user-facing code
for word in "overdue" "missed" "falling behind" "Great job" "Keep it up" "Don't forget"; do
    if grep -rni "$word" --include='*.tsx' --include='*.ts' apps/pwa/src/; then
        BRAND_VIOLATIONS="$BRAND_VIOLATIONS\nForbidden phrase found: '$word'"
    fi
done

# Check for red/urgent colors
if grep -rn '#[Ff][Ff]0000\|#[Ff][Ee]0000\|red-500\|red-600\|bg-red\|text-red' \
    --include='*.tsx' --include='*.ts' --include='*.css' apps/pwa/src/; then
    BRAND_VIOLATIONS="$BRAND_VIOLATIONS\nRed/urgent colors found"
fi

if [ -n "$BRAND_VIOLATIONS" ]; then
    ERRORS="$ERRORS\nBrand compliance violations:$BRAND_VIOLATIONS"
fi

if [ -n "$ERRORS" ]; then
    echo -e "Implementation gate failed:$ERRORS" >&2
    exit 1
fi
echo "Implementation gate passed"
exit 0
```

---

## 8. Prompt building

### Stage prompt templates

Each stage has a prompt template. The engine fills in task-specific content and artifact references.

#### Spec stage template

```
You are working on the project "{project_name}".

## Task
{task_title}

{task_description}

## Your job
Write a specification for this task. Save it to: _forge/specs/{task_id}.md

The spec must include these sections:
- **Overview**: What this task accomplishes in 2-3 sentences.
- **Acceptance criteria**: A numbered list of binary (pass/fail) criteria. Each criterion must be objectively verifiable — no subjective language like "looks good" or "feels right."
- **Out of scope**: What this task explicitly does NOT include.
- **Dependencies**: Any existing code, APIs, or features this task depends on.

Read the project's existing documentation before writing the spec to ensure alignment with established patterns and decisions.

Load the following skills for context:
{skill_references}

{retry_context}
```

#### Plan stage template

```
You are working on the project "{project_name}".

## Specification
{spec_content}

## Your job
Write an implementation plan for this spec. Save it to: _forge/plans/{task_id}.md

The plan must include:
- **Approach**: How you will implement this, in 2-3 paragraphs.
- **Files to create or modify**: An explicit list of every file path that will be touched.
- **Test plan**: Descriptions of tests that verify each acceptance criterion from the spec. Be specific about what each test asserts.
- **Risks**: Anything that might go wrong or need human input.

The plan should be detailed enough that a different agent — with no context beyond this plan and the spec — could implement it correctly.

Read the existing codebase before planning to understand current patterns and conventions.

Load the following skills for context:
{skill_references}

{retry_context}
```

#### Implement stage template

```
You are working on the project "{project_name}".
You are on branch: {branch_name}

## Specification
{spec_content}

## Implementation plan
{plan_content}

## Your job
Implement this task according to the plan. Write tests alongside your implementation.

Rules:
- Only modify files listed in the plan. If you need to modify other files, note it but proceed.
- Write tests that verify each acceptance criterion from the spec.
- Follow the project's coding conventions (load the relevant skills).
- Commit your work with clear, descriptive commit messages.
- Do NOT mark this task as complete — the gate scripts will validate your work.

Load the following skills:
{skill_references}

{retry_context}
```

#### Review stage template

```
You are working on the project "{project_name}".
You are reviewing branch: {branch_name}

## Specification
{spec_content}

## Changes made
{git_diff}

## Your job
Adversarially review this implementation against the spec. Save your review to: _forge/reviews/{task_id}.md

Your review must include:
- **Verdict**: Either "PASS" or "ISSUES"
- **Criteria check**: For each acceptance criterion in the spec, state whether the implementation satisfies it (yes/no with evidence).
- **Issues found**: If verdict is ISSUES, list each issue with: what's wrong, where it is, and what should be done about it. Be specific — cite file paths and line numbers.
- **Out of scope changes**: Flag any modifications to files not listed in the plan.

Your job is to find problems, not confirm success. Look for: unverified acceptance criteria, missing edge case tests, violations of project conventions or brand guidelines, dead code, and scope creep.

If you find issues that require new work, write a JSON file to _forge/follow-ups/{task_id}.json with an array of task descriptions the engine should add to the backlog.

Load the following skills:
{skill_references}

{retry_context}
```

### Retry context

When a stage is being retried after a gate failure, the prompt includes:

```
## Previous attempt failed
This is attempt {attempt_number}. The previous attempt failed the gate check.
Gate failure reason:
{gate_stderr}

Fix the specific issues identified above. Do not start from scratch unless the problems are fundamental.
```

---

## 9. Web dashboard

### Technology

Server-rendered HTML with Jinja2 templates, enhanced with htmx for interactivity. No build step, no frontend framework. Clean, minimal CSS.

### Pages

#### Pipeline view (`/`)

The primary view. A horizontal kanban board with columns:

**Backlog** → **Spec** → **Plan** → **Implement** → **Review** → **Done**

Each task appears as a card in its current stage column showing:
- Task title
- Project name (color-coded badge)
- Priority indicator (subtle)
- Current attempt number (if retrying)
- Time in current stage

Cards with `needs_human` status show a gentle indicator. Clicking a card navigates to the task detail page.

Controls at the top:
- Engine status (running / paused) with toggle
- "New task" button
- Project filter dropdown

The pipeline view updates via htmx polling (every 5 seconds) or SSE (if added later).

#### Task detail (`/tasks/{task_id}`)

Full history of a task:
- Task metadata (title, description, priority, status, project)
- Timeline of stage runs with status, duration, and token usage
- For each stage run: expandable sections showing the prompt sent, Claude's output, gate result, and any artifacts produced
- If bounced: the gate failure reason is prominently displayed
- If `needs_human`: a "Resolve and resume" button
- Links to related tasks (created_by, blocks, etc.)

#### Backlog management (`/backlog`)

- List of all tasks in backlog status, ordered by priority, grouped by project
- Create new task form (project, title, description, priority, skill overrides)
- Edit existing backlog tasks
- Drag-to-reorder priority (optional enhancement)

#### Run log (`/logs`)

Chronological feed of engine activity. Filterable by level (info/warn/error), by task, and by project. Auto-scrolls to latest. Useful for monitoring overnight runs.

#### Settings (`/settings`)

- Project list with configuration (repo path, gate directory, default skills)
- Engine settings (poll interval, concurrency, timeouts)
- Claude Code settings (default model)

---

## 10. API endpoints

### Project management

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/projects` | List all projects |
| POST | `/api/projects` | Create a new project |
| GET | `/api/projects/{id}` | Get project detail |
| PATCH | `/api/projects/{id}` | Update project settings |

### Task management

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/tasks` | List tasks with filtering (status, project, priority) |
| POST | `/api/tasks` | Create a new task |
| GET | `/api/tasks/{id}` | Get task detail with stage run history |
| PATCH | `/api/tasks/{id}` | Update task (title, description, priority, status) |
| DELETE | `/api/tasks/{id}` | Delete a task (only if in backlog) |
| POST | `/api/tasks/{id}/resume` | Resume a needs_human task |
| POST | `/api/tasks/{id}/pause` | Pause an active task |
| POST | `/api/tasks/{id}/retry` | Force retry current stage |

### Pipeline control

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/engine/status` | Engine status (running, paused, current task) |
| POST | `/api/engine/start` | Start the engine |
| POST | `/api/engine/pause` | Pause the engine (finish current work, stop polling) |
| GET | `/api/engine/stats` | Pipeline statistics (tasks completed, avg stage time, bounce rate) |

### Stage runs

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/stage-runs` | List stage runs with filtering |
| GET | `/api/stage-runs/{id}` | Full stage run detail (prompt, output, gate result) |

### Run log

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/logs` | Paginated run log with filtering |
| GET | `/api/logs/stream` | SSE stream of new log entries (for live dashboard) |

---

## 11. Integration with Olivia

### Existing repo structure

The Olivia repo (github.com/LoveAndCoding/olivia) is a TypeScript monorepo at v0.6.0 with the following workspace layout:

```
olivia/
├── apps/
│   ├── pwa/                  # React + Vite installable PWA (Capacitor-wrapped for iOS)
│   └── api/                  # Fastify + SQLite API server
├── packages/
│   ├── domain/               # Deterministic inbox rules, parsing, status, suggestions
│   └── contracts/            # Shared Zod schemas and API payload contracts
├── docs/
│   ├── brand/                # Brand identity (essence, tone, color, typography, logo)
│   ├── vision/               # Product vision, ethos, design foundations
│   ├── specs/                # Feature specifications
│   ├── plans/                # Implementation plans
│   ├── roadmap/              # Roadmap and milestones
│   ├── strategy/             # Architecture, agentic principles, interface direction
│   └── learnings/            # Decision history, assumptions, learnings log
├── agents/                   # Existing agent definitions (Paperclipai)
├── skills/                   # Existing skill definitions
├── deploy/                   # Deployment configuration
├── scripts/                  # Build and utility scripts
├── .claude/commands/         # Claude Code custom commands
├── .github/workflows/        # CI including iOS build on macOS runner
├── docker-compose.yml
├── package.json              # Workspace root with top-level scripts
├── tsconfig.base.json
├── vitest.config.ts
├── playwright.config.ts
├── eslint.config.js
├── capacitor.config.ts       # (in apps/pwa)
└── AGENTS.md
```

Existing top-level scripts: `npm run dev`, `npm run typecheck`, `npm run lint`, `npm test`, `npm run build`.

### Olivia codebase reset strategy

The app is at v0.6.0 but is significantly broken — the UI has degraded through agent-driven changes, and the tests pass without validating real behavior. Before Forge begins managing Olivia tasks, the codebase needs a structured teardown.

**Keep as-is:**
- Monorepo structure and tooling config (package.json, tsconfig, vitest, playwright, eslint, capacitor)
- `docs/` directory (brand, vision, specs, plans, strategy, learnings — this is the source of truth)
- `packages/contracts/` (Zod schemas define what the system should do — review and keep valid ones)
- `deploy/`, `scripts/`, `.github/workflows/`
- Docker compose setup

**Keep but audit:**
- `packages/domain/` — domain logic (parsing, status updates, stale detection) is the most valuable code. Review each module: keep what works correctly, flag what doesn't.

**Wipe and rebuild through Forge:**
- `apps/pwa/` component and page code — rebuild feature by feature through the pipeline
- `apps/api/` route handlers — rebuild with proper test coverage
- Existing tests that don't validate real behavior — replace with meaningful tests as part of each feature's implement stage

**Remove:**
- `agents/` directory (Paperclipai agent definitions — replaced by Forge)
- `AGENTS.md` (Paperclipai configuration — replaced by Forge)

**Add:**
- `_forge/` directory for pipeline artifacts (specs, plans, reviews)
- `gates/` directory for Forge gate scripts
- `.claude/skills/` with Olivia-specific skills (voice, UI, brand gate, conventions)
- Updated `CLAUDE.md` for Forge-driven development

### Initial Olivia task batch

The first batch of Olivia tasks through Forge should rebuild core functionality with proper test coverage. Each task is small, well-scoped, and has clear acceptance criteria:

1. "Write meaningful tests for the domain package — inbox item parsing, status updates, stale detection"
2. "Write meaningful tests for the contracts package — validate Zod schemas match actual API behavior"
3. "Rebuild the inbox capture flow — items can be added and appear in the inbox list"
4. "Rebuild the reminder creation flow — reminders can be created with a date/time and appear in the weekly view"
5. "Rebuild the shared list flow — lists can be created, items added, and items checked off"
6. "Rebuild push notification delivery — notifications are sent for reminders and arrive on the device"

Tasks 1-2 establish the test foundation. Tasks 3-5 rebuild core features. Task 6 addresses the push notification flow. Each task goes through the full spec→plan→implement→review pipeline with gates enforcing quality at every step.

### Olivia skills

These are Claude Code skills installed in the Olivia repo's `.claude/skills/` directory. They are loaded by the agent at the start of each stage as specified by the prompt.

**`olivia-voice/SKILL.md`**: Tone of voice guidelines. Includes "Not This / This" tables from the brand docs. Covers: reminders, task completion, overdue items, morning check-in, forgotten items. Core rules: calm not flat, proactive not pushy, clear not clever, supportive not sycophantic.

**`olivia-ui/SKILL.md`**: Calm design principles. Covers: whitespace minimums (24px between groups, 48px between sections), one primary action per screen, progressive disclosure, no guilt mechanics, gentle motion only, forgiving interactions, consistent patterns. Includes the watercolor illustration style guidance.

**`olivia-brand-gate/SKILL.md`**: The "Things to Avoid" checklist as a self-check reference. Agents can consult this before the mechanical gate catches violations. Covers: no red badges, no streak counters, no exclamation marks, no sycophantic praise, no dense dashboards, no competitive language, no dark patterns, no bright/saturated colors, no auto-playing sounds, no "behind/failing" language.

**`olivia-conventions/SKILL.md`**: Technical stack conventions for the Olivia monorepo. Workspace layout (apps/pwa, apps/api, packages/domain, packages/contracts), React + TypeScript patterns, Fastify API patterns, Zod schema conventions, Capacitor plugin usage, testing patterns (Vitest for unit/integration, Playwright for e2e).

### Hooks

Configured in `.claude/settings.json` in the Olivia repo. These run within each Claude Code session dispatched by the engine.

**Stop hook**: Runs the post-implement gate script so the agent gets immediate feedback within its session (before the engine's gate runner). This provides a faster feedback loop — the agent can self-correct before the session ends.

**PreToolUse hook**: Protects critical files from modification (brand guideline docs, gate scripts, skill definitions, capacitor config, package.json workspaces config).

**PostToolUse hook**: Auto-formats on file write (using ESLint's existing config).

---

## 12. Dogfooding plan

Forge's first managed project is itself. This validates the pipeline methodology on a low-stakes codebase before pointing it at Olivia.

### Phase 1: Manual build (Week 1)

Build Forge v0.1 manually — you and Claude Code working together without orchestration. Follow the spec→plan→implement→review flow by hand for the core components: database, engine, dispatcher, gate runner, dashboard.

Register Forge as a project in its own database:
```bash
python -m forge init-project \
  --name "Forge" \
  --repo-path "/home/user/projects/forge" \
  --default-branch "main" \
  --gate-dir "gates"
```

### Phase 2: Self-hosting (Week 2)

Feed Forge improvement tasks through its own pipeline. Start with small, well-defined tasks:
- "Add pagination to the task list API endpoint"
- "Add SSE support to the run log page"
- "Add retry count display to the pipeline view cards"

Monitor the pipeline closely. Tune prompt templates, gate strictness, and timeout values based on observed behavior.

### Phase 3: Olivia onboarding (Week 3)

Prepare the Olivia repo: install skills, create gate scripts, update CLAUDE.md, execute the codebase reset strategy.

Register Olivia as a project:
```bash
python -m forge init-project \
  --name "Olivia" \
  --repo-path "/home/user/projects/olivia" \
  --default-branch "main" \
  --gate-dir "gates" \
  --skills "olivia-voice,olivia-ui,olivia-brand-gate,olivia-conventions"
```

Begin with the initial Olivia task batch (section 11), starting with test-writing tasks before feature rebuilds.

### Phase 4: Autonomous operation (Week 4+)

With confidence from running both projects through the pipeline, increase autonomy:
- Queue larger batches of tasks and let the engine run overnight
- Reduce manual monitoring frequency
- Begin concurrent task execution (if implemented)
- Iterate on skills, gates, and prompt templates based on bounce rate data

---

## 13. Acceptance criteria for Forge v0.1

The orchestrator is complete when:

1. [ ] A project can be registered via the CLI with name, repo path, and gate directory
2. [ ] A task can be created via the dashboard with a project, title, description, and priority
3. [ ] The engine picks up the highest-priority queued task and advances it through all four stages
4. [ ] Each stage runs in a fresh Claude Code session via `claude -p`
5. [ ] Each stage produces the expected artifact file in the target project's `_forge/` directory
6. [ ] Gate scripts run after each stage and their results are recorded in the database
7. [ ] A gate failure bounces the task back to the same stage with the failure reason in the retry prompt
8. [ ] After max_retries gate failures, the task is marked needs_human
9. [ ] The pipeline view shows tasks in their current stage, grouped or filterable by project
10. [ ] The task detail page shows the full history of stage runs with gate results
11. [ ] The engine can be started and paused from the dashboard
12. [ ] The run log captures all engine activity and is viewable in the dashboard
13. [ ] The engine runs continuously without memory leaks or crashes over a 24-hour period
14. [ ] Forge is registered as a project and has successfully processed at least one task through its own pipeline

### Out of scope for v0.1

- Concurrent task execution (single task at a time)
- PR creation (manual for now)
- CI integration for async gates (e.g., macOS builds for Olivia iOS)
- Task dependency enforcement (task_links table exists but is not used by the engine)
- Per-stage model selection (all stages use default model)
- Token usage tracking (log it if available but don't act on it)
- Authentication on the dashboard (runs on private server)
- Dashboard project management UI (use CLI; settings page is view-only in v0.1)

---

## 14. Risks and mitigations

**Risk: Claude Code CLI changes break the dispatcher.**
Mitigation: Pin Claude Code version. Isolate all CC interaction in `dispatcher.py` so changes are contained.

**Risk: Gate scripts are too strict or too lenient, causing excessive bouncing or passing bad work.**
Mitigation: Start with minimal gates and tighten over time. Log all gate results so you can analyze false positives/negatives. Gates are plain scripts in the target repo — easy to iterate on independently of the orchestrator.

**Risk: The engine gets stuck in a retry loop on a fundamentally impossible task.**
Mitigation: max_retries cap (default 3). After max retries, the task stops and waits for human intervention. The dashboard makes these visible.

**Risk: Long-running Claude Code sessions consume excessive tokens or time.**
Mitigation: Stage timeout (default 10 minutes). The engine kills sessions that exceed the timeout and marks them as errors. Start with conservative timeouts and adjust based on observed stage durations.

**Risk: Git conflicts when the default branch moves ahead of a task's feature branch.**
Mitigation: Before each implement stage, the engine rebases the feature branch on the default branch. If the rebase fails, the task is marked needs_human.

**Risk: Forge's own gate scripts are too simple to catch real quality issues early on.**
Mitigation: Start with basic structural checks (does the artifact exist, is it non-trivial) and add more sophisticated checks as patterns of failure emerge. The gate scripts are the part of the system you'll iterate on most.

---

## 15. Future enhancements (post v0.1)

- **Concurrent execution**: Multiple tasks in parallel using git worktrees
- **CI integration**: Async gates that trigger GitHub Actions and poll for results (especially for iOS builds)
- **PR automation**: Automatic PR creation when review stage passes
- **Model routing**: Opus for spec/plan/review, Sonnet for implement (configurable per project and stage)
- **Token budgeting**: Track per-task token usage and set budgets
- **Notification system**: Slack/email/push notifications for needs_human tasks and pipeline completions
- **Task templates**: Pre-defined task structures for common work types (new feature, bug fix, refactor, test backfill)
- **Metrics dashboard**: Bounce rates per stage, average completion time, token cost trends, per-project comparisons
- **Agent learning**: When a gate failure is resolved, capture the pattern and add it to the relevant skill
- **Dashboard project management**: Full CRUD for projects via the settings page (replace CLI for project creation)
- **Scheduled task generation**: Recurring tasks (e.g., weekly dependency update, periodic test coverage check)
