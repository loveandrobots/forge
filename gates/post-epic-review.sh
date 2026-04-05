#!/bin/bash
# Gate: post-epic-review
# Validates that an epic review artifact was produced as structured JSON with a clear verdict.
#
# Environment variables (set by Forge gate runner):
#   FORGE_TASK_ID        — the task identifier
#   FORGE_REPO_PATH      — path to the project repository
#   FORGE_STAGE          — current stage name
#   FORGE_ATTEMPT        — attempt number
#   FORGE_ARTIFACT_PATH  — (optional) path to structured output JSON

set -euo pipefail

# Prefer FORGE_ARTIFACT_PATH, then fall back to the standard review path
if [ -n "${FORGE_ARTIFACT_PATH:-}" ] && [ -f "${FORGE_ARTIFACT_PATH}" ]; then
    REVIEW_FILE="$FORGE_ARTIFACT_PATH"
else
    REVIEW_FILE="${FORGE_REPO_PATH}/_forge/reviews/${FORGE_TASK_ID}.json"
fi

# Check review file exists
if [ ! -f "$REVIEW_FILE" ]; then
    echo "FAIL: Epic review file not found: $REVIEW_FILE" >&2
    exit 1
fi

# Validate JSON
if ! jq empty "$REVIEW_FILE" 2>/dev/null; then
    echo "FAIL: Epic review file is not valid JSON: $REVIEW_FILE" >&2
    exit 1
fi

# Extract verdict
VERDICT=$(jq -r '.verdict // empty' "$REVIEW_FILE")
if [ -z "$VERDICT" ]; then
    echo "FAIL: Could not determine verdict from epic review file" >&2
    exit 1
fi

if [ "$VERDICT" = "PASS" ]; then
    echo "post-epic-review gate passed"
    exit 0
elif [ "$VERDICT" = "ISSUES" ]; then
    # Validate that ISSUES verdict has a non-empty issues array
    ISSUES_COUNT=$(jq '.issues | length' "$REVIEW_FILE")
    if [ "$ISSUES_COUNT" -eq 0 ]; then
        echo "FAIL: Epic review with ISSUES verdict must have a non-empty issues array" >&2
        exit 1
    fi
    echo "FAIL: Epic review verdict is ISSUES. Follow-up tasks required." >&2
    exit 1
else
    echo "FAIL: Unexpected verdict value: $VERDICT" >&2
    exit 1
fi
