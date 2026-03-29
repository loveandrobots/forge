# Forge

Forge is a pipeline orchestrator that manages semi-autonomous software development. It dispatches tasks through a staged pipeline (spec → plan → implement → review), runs mechanical quality gates between stages, and provides a web dashboard for visibility and control.

## Spec and pipeline artifacts

Pipeline artifacts live in `_forge/`. Check `_forge/specs/`, `_forge/plans/`, and `_forge/reviews/` for the latest context on what's been decided and why. Read the most recent spec before making architectural decisions.

## Tech stack

- Python 3.11+
- FastAPI (API server + dashboard serving)
- SQLite via `sqlite3` stdlib (no ORM)
- Jinja2 templates + htmx for dashboard
- pytest for tests
- ruff for linting

## Project structure

```
forge/
├── forge/              # Application code
│   ├── main.py         # FastAPI app, startup/shutdown
│   ├── cli.py          # CLI commands (init-project, serve, etc.)
│   ├── config.py       # Settings and defaults
│   ├── database.py     # SQLite connection, schema, queries
│   ├── models.py       # Pydantic models
│   ├── engine.py       # Pipeline engine (async background loop)
│   ├── dispatcher.py   # Claude Code session spawning via subprocess
│   ├── gate_runner.py  # Gate script execution
│   ├── prompt_builder.py
│   └── routers/        # FastAPI route modules
├── templates/          # Jinja2 templates for dashboard
├── static/             # CSS, JS
├── gates/              # Forge's own gate scripts
├── _forge/             # Pipeline artifacts (specs, plans, reviews)
├── tests/              # pytest tests
└── config.yaml         # Engine configuration
```

## Skills

Load relevant skills from `.claude/skills/` before starting work:
- `forge-implement` — required for all implementation tasks
- `forge-testing` — required when writing or modifying tests

## Conventions

- Use type hints on all function signatures.
- SQL queries go in `database.py`, not in routers or engine code. Use parameterized queries, never string formatting.
- Pydantic models in `models.py` for all API request/response schemas.
- Async functions for anything that touches the engine loop or subprocess calls.
- Tests go in `tests/` mirroring the `forge/` structure. Test file names: `test_{module}.py`.
- No classes where a function will do. Keep it simple.
- Commits should be atomic and descriptive. One logical change per commit.

## Key design decisions

- The pipeline engine runs as an asyncio background task inside the FastAPI process, not a separate worker.
- All Claude Code interaction is isolated in `dispatcher.py`. Every other module is unaware of Claude Code.
- Gate scripts are external executables in the target project's repo, not Python code in Forge. Forge runs them via subprocess and interprets exit codes.
- Project configuration lives in SQLite, not config.yaml. Projects can be added at runtime without restarting.
- Engine configuration (poll interval, timeouts, concurrency) lives in config.yaml and is read at startup.

## Running locally

```bash
pip install -r requirements.txt
python -m forge migrate           # Initialize database schema
python -m forge init-project --name "Forge" --repo-path "$(pwd)" --default-branch main --gate-dir gates
python -m forge serve --port 8000 # Start server + engine + dashboard
```

## What not to do

- Do not add an ORM. Raw SQL with parameterized queries is sufficient for this data model.
- Do not add a frontend build step. The dashboard uses server-rendered templates with htmx. No webpack, no vite, no npm.
- Do not put gate logic in Python. Gates are shell scripts in the target repo so each project controls its own quality checks.
- Do not add authentication in v0.1. The server runs on a private network.
