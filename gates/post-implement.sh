#!/bin/bash
# Gate: post-implement
# Runs tests and linting against the project.
#
# Environment variables (set by Forge gate runner):
#   FORGE_TASK_ID      — the task identifier
#   FORGE_REPO_PATH    — path to the project repository
#   FORGE_STAGE        — current stage name
#   FORGE_ATTEMPT      — attempt number
#   FORGE_BRANCH       — feature branch name

set -uo pipefail

cd "$FORGE_REPO_PATH"

# Use python3 if python is not available
PYTHON="${FORGE_PYTHON:-$(command -v python3 || command -v python)}"

ERRORS=""

# Run tests
if ! TEST_OUTPUT=$("$PYTHON" -m pytest tests/ 2>&1); then
    ERRORS="${ERRORS}Tests failed:\n${TEST_OUTPUT}\n\n"
fi

# Run linter
if ! LINT_OUTPUT=$(ruff check forge/ 2>&1); then
    ERRORS="${ERRORS}Lint errors:\n${LINT_OUTPUT}\n\n"
fi

if [ -n "$ERRORS" ]; then
    echo -e "FAIL: post-implement gate failed\n${ERRORS}" >&2
    exit 1
fi

echo "post-implement gate passed"
exit 0
