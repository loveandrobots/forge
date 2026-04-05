#!/bin/bash
# Gate: post-epic-review
# Validates that an epic review artifact was produced with a clear verdict.
#
# Environment variables (set by Forge gate runner):
#   FORGE_TASK_ID      — the task identifier
#   FORGE_REPO_PATH    — path to the project repository
#   FORGE_STAGE        — current stage name
#   FORGE_ATTEMPT      — attempt number

set -euo pipefail

REVIEW_FILE="${FORGE_REPO_PATH}/_forge/reviews/${FORGE_TASK_ID}.md"

# Check review file exists
if [ ! -f "$REVIEW_FILE" ]; then
    echo "FAIL: Epic review file not found: $REVIEW_FILE" >&2
    exit 1
fi

# Extract the verdict using grep (supports markdown heading formats like ## Verdict: PASS)
VERDICT=$(grep -oP '(?i)(?:^#{1,6}\s+|\*{1,2})Verdict\*{0,2}:?\s*\K(PASS|ISSUES)\b' "$REVIEW_FILE" | head -1) || true
if [ -z "$VERDICT" ]; then
    echo "FAIL: Could not determine verdict from epic review file" >&2
    exit 1
fi
VERDICT=$(echo "$VERDICT" | tr '[:lower:]' '[:upper:]')

if [ "$VERDICT" = "PASS" ]; then
    echo "post-epic-review gate passed"
    exit 0
elif [ "$VERDICT" = "ISSUES" ]; then
    # Validate that follow-ups file exists when verdict is ISSUES
    FOLLOWUPS_FILE="${FORGE_REPO_PATH}/_forge/follow-ups/${FORGE_TASK_ID}.json"
    if [ ! -f "$FOLLOWUPS_FILE" ]; then
        echo "FAIL: Epic review with ISSUES verdict must include follow-ups file: $FOLLOWUPS_FILE" >&2
        exit 1
    fi
    echo "FAIL: Epic review verdict is ISSUES. Follow-up tasks required." >&2
    exit 1
else
    echo "FAIL: Unexpected verdict value: $VERDICT" >&2
    exit 1
fi
