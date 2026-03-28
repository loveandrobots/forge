Read the spec at _forge/specs/forge-v0.2.md and the plan at _forge/plans/forge-v0.2.md.

You are implementing task 9: Dashboard templates and static assets

Scope:
- Files to create/modify:
    - forge/routers/dashboard.py
    - templates/base.html
    - templates/pipeline.html
    - templates/task_detail.html
    - templates/backlog.html
    - templates/settings.html
    - templates/logs.html
    - static/styles.css
    - static/app.js
- This task depends on: task 7

Rules:
- Write tests alongside the implementation.
- Only touch files in scope. If you discover you need to modify something else, note it and proceed.
- Commit when done with a descriptive message.
- Do not refactor, reorganize, or "improve" anything outside the scope of this task.

When finished, run the tests (python -m pytest tests/), linter (ruff check forge/) and all test criteria, and confirm they pass before committing. Treat any warning message in tests as failures and resolve them before continuing. Additionally include basic validation of the static assets to ensure they are valid
