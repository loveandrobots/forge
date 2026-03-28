#!/bin/bash
# Gate: post-plan
# Validates that a plan artifact was produced with required content.
#
# Environment variables (set by Forge gate runner):
#   FORGE_TASK_ID      — the task identifier
#   FORGE_REPO_PATH    — path to the project repository
#   FORGE_STAGE        — current stage name
#   FORGE_ATTEMPT      — attempt number
#   FORGE_BRANCH       — feature branch name
#   FORGE_PLAN_PATH    — path to the plan file

set -euo pipefail

PLAN_FILE="${FORGE_REPO_PATH}/_forge/plans/${FORGE_TASK_ID}.md"

# Check plan file exists
if [ ! -f "$PLAN_FILE" ]; then
    echo "FAIL: Plan file not found: $PLAN_FILE" >&2
    exit 1
fi

# Check file is >200 chars
CHAR_COUNT=$(wc -c < "$PLAN_FILE")
if [ "$CHAR_COUNT" -le 200 ]; then
    echo "FAIL: Plan file is too short (${CHAR_COUNT} chars, need >200)" >&2
    exit 1
fi

# Check for required content: references to acceptance criteria
if ! grep -qi 'acceptance criteria\|acceptance criterion' "$PLAN_FILE"; then
    echo "FAIL: Plan file must reference spec acceptance criteria" >&2
    exit 1
fi

# Check for test descriptions
if ! grep -qi 'test' "$PLAN_FILE"; then
    echo "FAIL: Plan file must include test descriptions" >&2
    exit 1
fi

# Check for files to create/modify
if ! grep -qi 'files\?\s*to\s*\(create\|modify\)\|files changed\|files:\|create.*:\|modify.*:' "$PLAN_FILE"; then
    echo "FAIL: Plan file must list files to create or modify" >&2
    exit 1
fi

echo "post-plan gate passed"
exit 0
