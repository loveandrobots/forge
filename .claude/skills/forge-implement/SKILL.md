---
name: forge-implement
description: |
  Standards for implementing tasks in the Forge project.
  Load when implementing any feature, fix, or enhancement.
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

# Implementation standards

## Testing

- Run `python -m pytest tests/ -W error` after implementation. All tests must pass with zero warnings.
- If existing tests fail, fix them. Never dismiss a failure as "not related to my change" — either it is related (fix it) or the test was already broken (fix it anyway and note it in the commit message).
- Every new function or endpoint needs at least one test.
- See the `forge-testing` skill for database isolation and test quality conventions.

## Code quality

- Run `ruff check forge/` before committing. Fix all lint violations.
- Run `ruff format forge/` before committing. `check` catches logic/style issues; `format` normalizes whitespace, line length, and formatting.
- No commits with lint errors, format diffs, or warnings.

## Scope discipline

- Only modify files listed in the implementation plan.
- If you discover you need to change something outside scope, note it in the commit message but proceed.
- Do not refactor, reorganize, or "improve" code outside the task scope.
- Do not add dependencies not in requirements.txt without noting it.
