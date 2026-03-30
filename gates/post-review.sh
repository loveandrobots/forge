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

# Check for verdict
HAS_PASS=$(grep -ci 'PASS' "$REVIEW_FILE" || true)
HAS_ISSUES=$(grep -ci 'ISSUES' "$REVIEW_FILE" || true)

if [ "$HAS_PASS" -eq 0 ] && [ "$HAS_ISSUES" -eq 0 ]; then
    echo "FAIL: Review file must contain a verdict ('PASS' or 'ISSUES')" >&2
    exit 1
fi

# If verdict is ISSUES, check for actionable items
if [ "$HAS_ISSUES" -gt 0 ] && [ "$HAS_PASS" -eq 0 ]; then
    # Look for list items (lines starting with - or * or numbered) after ISSUES
    ACTIONABLE_COUNT=$(grep -cE '^\s*[-*]\s+\S|^#*\s*[0-9]+\.\s+\S' "$REVIEW_FILE" || true)
    if [ "$ACTIONABLE_COUNT" -eq 0 ]; then
        echo "FAIL: Review with ISSUES verdict must include specific actionable items" >&2
        exit 1
    fi
    # ISSUES verdict with actionable items — fail the gate so the engine
    # bounces the task back to implement for fixes.
    echo "FAIL: Review verdict is ISSUES with $ACTIONABLE_COUNT actionable item(s). Bouncing to implement." >&2
    exit 1
fi

echo "post-review gate passed"
exit 0
