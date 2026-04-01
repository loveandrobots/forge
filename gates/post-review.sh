#!/bin/bash
# Gate: post-review
# Validates that a review artifact was produced with a clear verdict.
#
# Environment variables (set by Forge gate runner):
#   FORGE_TASK_ID      — the task identifier
#   FORGE_REPO_PATH    — path to the project repository
#   FORGE_STAGE        — current stage name
#   FORGE_ATTEMPT      — attempt number
#   FORGE_BRANCH       — feature branch name
#   FORGE_REVIEW_PATH  — path to the review file

set -euo pipefail

REVIEW_FILE="${FORGE_REPO_PATH}/_forge/reviews/${FORGE_TASK_ID}.md"

# Check review file exists
if [ ! -f "$REVIEW_FILE" ]; then
    echo "FAIL: Review file not found: $REVIEW_FILE" >&2
    exit 1
fi

# Extract the verdict using the Python parser
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VERDICT=$(python3 "$SCRIPT_DIR/parse_verdict.py" "$REVIEW_FILE" 2>/tmp/forge_verdict_err) || {
    cat /tmp/forge_verdict_err >&2
    echo "FAIL: Could not determine verdict from review file" >&2
    exit 1
}

if [ "$VERDICT" = "PASS" ]; then
    echo "post-review gate passed"
    exit 0
elif [ "$VERDICT" = "ISSUES" ]; then
    ACTIONABLE_COUNT=$(grep -cE '^\s*[-*]\s+\S|^#*\s*[0-9]+\.\s+\S' "$REVIEW_FILE" || true)
    if [ "$ACTIONABLE_COUNT" -eq 0 ]; then
        echo "FAIL: Review with ISSUES verdict must include specific actionable items" >&2
        exit 1
    fi
    echo "FAIL: Review verdict is ISSUES with $ACTIONABLE_COUNT actionable item(s). Bouncing to implement." >&2
    exit 1
else
    echo "FAIL: Unexpected verdict value: $VERDICT" >&2
    exit 1
fi
