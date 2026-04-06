#!/bin/bash
# Gate: post-implement
# Runs tests and linting against the project.
# Emits structured JSON output on stdout for richer failure context.
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

# Track individual check results as a JSON array built via Python
CHECKS='[]'
ALL_PASSED=true

add_check() {
    local name="$1"
    local passed="$2"
    local detail="$3"
    local py_passed
    if [ "$passed" = "true" ]; then py_passed="True"; else py_passed="False"; fi
    CHECKS=$("$PYTHON" -c "
import sys, json
checks = json.loads(sys.argv[1])
checks.append({'name': sys.argv[2], 'passed': $py_passed, 'detail': sys.argv[3]})
json.dump(checks, sys.stdout)
" "$CHECKS" "$name" "$detail")
    if [ "$passed" = "false" ]; then
        ALL_PASSED=false
    fi
}

# Run tests
if TEST_OUTPUT=$("$PYTHON" -m pytest tests/ 2>&1); then
    add_check "tests" "true" ""
else
    add_check "tests" "false" "$(echo "$TEST_OUTPUT" | tail -20)"
    echo "Tests failed:" >&2
    echo "$TEST_OUTPUT" >&2
fi

# Run linter
if LINT_OUTPUT=$(ruff check forge/ 2>&1); then
    add_check "lint" "true" ""
else
    add_check "lint" "false" "$(echo "$LINT_OUTPUT" | tail -20)"
    echo "Lint errors:" >&2
    echo "$LINT_OUTPUT" >&2
fi

# Run smoke tests (only if the module exists in this repo)
if [ -f "tests/smoke.py" ]; then
    if SMOKE_OUTPUT=$("$PYTHON" -m tests.smoke 2>&1); then
        add_check "smoke" "true" ""
    else
        add_check "smoke" "false" "$(echo "$SMOKE_OUTPUT" | tail -20)"
        echo "Smoke tests failed:" >&2
        echo "$SMOKE_OUTPUT" >&2
    fi
fi

# Emit structured JSON on stdout
if [ "$ALL_PASSED" = "true" ]; then
    PY_ALL_PASSED="True"
    REASON="All checks passed."
else
    PY_ALL_PASSED="False"
    REASON="One or more checks failed."
fi

"$PYTHON" -c "
import json, sys
result = {
    'passed': $PY_ALL_PASSED,
    'reason': sys.argv[1],
    'checks': json.loads(sys.argv[2])
}
json.dump(result, sys.stdout)
" "$REASON" "$CHECKS"

if [ "$ALL_PASSED" = "true" ]; then
    exit 0
else
    exit 1
fi
