#!/bin/bash
# Gate: post-plan
# Validates that a plan artifact was produced as valid structured JSON,
# and cross-references criterion IDs against the spec.
#
# Requires: jq (system dependency)
#
# Environment variables (set by Forge gate runner):
#   FORGE_TASK_ID      — the task identifier
#   FORGE_REPO_PATH    — path to the project repository
#   FORGE_STAGE        — current stage name
#   FORGE_ATTEMPT      — attempt number
#   FORGE_BRANCH       — feature branch name
#   FORGE_PLAN_PATH    — path to the plan file

set -euo pipefail

PLAN_FILE="${FORGE_REPO_PATH}/_forge/plans/${FORGE_TASK_ID}.json"
SPEC_FILE="${FORGE_REPO_PATH}/_forge/specs/${FORGE_TASK_ID}.json"

# Check plan file exists
if [ ! -f "$PLAN_FILE" ]; then
    echo "FAIL: Plan file not found: $PLAN_FILE" >&2
    exit 1
fi

# Validate JSON
if ! jq empty "$PLAN_FILE" 2>/dev/null; then
    echo "FAIL: Plan file is not valid JSON: $PLAN_FILE" >&2
    exit 1
fi

# Check acceptance_criteria_mapping is non-empty
MAPPING_COUNT=$(jq '.acceptance_criteria_mapping | length' "$PLAN_FILE")
if [ "$MAPPING_COUNT" -eq 0 ]; then
    echo "FAIL: Plan 'acceptance_criteria_mapping' is empty" >&2
    exit 1
fi

# Cross-reference: verify every criterion_id in the plan exists in the spec
if [ ! -f "$SPEC_FILE" ]; then
    echo "WARNING: Spec file not found at $SPEC_FILE — skipping cross-reference check" >&2
    echo "post-plan gate passed (no spec cross-ref)"
    exit 0
fi

# Extract spec criterion IDs as a sorted list
SPEC_IDS=$(jq -r '.acceptance_criteria[].id' "$SPEC_FILE" | sort -n)

# Check each plan criterion_id against spec IDs
ERRORS=""
for PLAN_CID in $(jq -r '.acceptance_criteria_mapping[].criterion_id' "$PLAN_FILE"); do
    if ! echo "$SPEC_IDS" | grep -qx "$PLAN_CID"; then
        ERRORS="${ERRORS}  - criterion_id $PLAN_CID not found in spec acceptance_criteria\n"
    fi
done

if [ -n "$ERRORS" ]; then
    echo "FAIL: Plan references criterion IDs not present in spec:" >&2
    printf "%b" "$ERRORS" >&2
    exit 1
fi

echo "post-plan gate passed"
exit 0
