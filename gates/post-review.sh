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

# Extract just the verdict line
VERDICT_LINE=$(grep -i 'verdict' "$REVIEW_FILE" | head -1)

if echo "$VERDICT_LINE" | grep -qi 'ISSUES'; then
    ACTIONABLE_COUNT=$(grep -cE '^\s*[-*]\s+\S|^\s*[0-9]+\.\s+\S' "$REVIEW_FILE" || true)
    if [ "$ACTIONABLE_COUNT" -eq 0 ]; then
        echo "FAIL: Review with ISSUES verdict must include specific actionable items" >&2
        exit 1
    fi
    echo "FAIL: Review verdict is ISSUES with $ACTIONABLE_COUNT actionable item(s). Bouncing to implement." >&2
    exit 1
elif echo "$VERDICT_LINE" | grep -qi 'PASS'; then
    echo "post-review gate passed"
    exit 0
else
    echo "FAIL: Could not determine verdict from line: $VERDICT_LINE" >&2
    exit 1
fi
