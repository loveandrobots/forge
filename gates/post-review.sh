#!/bin/bash
# Gate: post-review
# Validates that a review artifact was produced with a clear verdict.
#
# Requires: jq (system dependency)
#
# Environment variables (set by Forge gate runner):
#   FORGE_TASK_ID      — the task identifier
#   FORGE_REPO_PATH    — path to the project repository
#   FORGE_STAGE        — current stage name
#   FORGE_ATTEMPT      — attempt number
#   FORGE_BRANCH       — feature branch name
#   FORGE_REVIEW_PATH  — path to the review file
#   FORGE_ARTIFACT_PATH — path to the structured JSON review artifact

set -euo pipefail

# Prefer FORGE_ARTIFACT_PATH, fall back to FORGE_REVIEW_PATH
REVIEW_FILE="${FORGE_ARTIFACT_PATH:-${FORGE_REVIEW_PATH:-}}"

# Final fallback: construct path from task ID
if [ -z "$REVIEW_FILE" ]; then
    REVIEW_FILE="${FORGE_REPO_PATH}/_forge/reviews/${FORGE_TASK_ID}.json"
fi

# Check review file exists
if [ ! -f "$REVIEW_FILE" ]; then
    echo "FAIL: Review file not found: $REVIEW_FILE" >&2
    exit 1
fi

# Legacy .md fallback: warn and exit 0 to avoid blocking
if [[ "$REVIEW_FILE" == *.md ]]; then
    echo "WARNING: Legacy markdown review detected ($REVIEW_FILE). Passing gate without validation." >&2
    echo "post-review gate passed (legacy)"
    exit 0
fi

# Validate JSON
if ! jq empty "$REVIEW_FILE" 2>/dev/null; then
    echo "FAIL: Review file is not valid JSON: $REVIEW_FILE" >&2
    exit 1
fi

# Extract verdict
VERDICT=$(jq -r '.verdict // empty' "$REVIEW_FILE")

if [ -z "$VERDICT" ]; then
    echo "FAIL: Review file missing 'verdict' field" >&2
    exit 1
fi

if [ "$VERDICT" = "PASS" ]; then
    echo "post-review gate passed"
    exit 0
elif [ "$VERDICT" = "ISSUES" ]; then
    ISSUE_COUNT=$(jq '.issues | length' "$REVIEW_FILE")
    if [ "$ISSUE_COUNT" -eq 0 ]; then
        echo "FAIL: Review with ISSUES verdict has empty issues array" >&2
        exit 1
    fi
    echo "FAIL: Review verdict is ISSUES with $ISSUE_COUNT issue(s). Bouncing to implement." >&2
    exit 1
else
    echo "FAIL: Unrecognized verdict value: $VERDICT" >&2
    exit 1
fi
