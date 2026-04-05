#!/bin/bash
# Gate: post-spec
# Validates that a spec artifact was produced as valid structured JSON.
#
# Requires: jq (system dependency)
#
# Environment variables (set by Forge gate runner):
#   FORGE_TASK_ID      — the task identifier
#   FORGE_REPO_PATH    — path to the project repository
#   FORGE_STAGE        — current stage name
#   FORGE_ATTEMPT      — attempt number
#   FORGE_BRANCH       — feature branch name
#   FORGE_SPEC_PATH    — path to the spec file

set -euo pipefail

SPEC_FILE="${FORGE_REPO_PATH}/_forge/specs/${FORGE_TASK_ID}.json"

# Check spec file exists
if [ ! -f "$SPEC_FILE" ]; then
    echo "FAIL: Spec file not found: $SPEC_FILE" >&2
    exit 1
fi

# Validate JSON
if ! jq empty "$SPEC_FILE" 2>/dev/null; then
    echo "FAIL: Spec file is not valid JSON: $SPEC_FILE" >&2
    exit 1
fi

# Check overview is non-empty
if ! jq -e '.overview | length > 0' "$SPEC_FILE" >/dev/null 2>&1; then
    echo "FAIL: Spec 'overview' field is empty or missing" >&2
    exit 1
fi

# Check acceptance_criteria has at least one item
if ! jq -e '.acceptance_criteria | length > 0' "$SPEC_FILE" >/dev/null 2>&1; then
    echo "FAIL: Spec 'acceptance_criteria' array is empty or missing" >&2
    exit 1
fi

echo "post-spec gate passed"
exit 0
