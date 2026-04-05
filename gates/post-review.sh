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

# Criteria coverage check: if structured spec exists, verify all criterion IDs
# appear in the review's criteria_check array.
SPEC_FILE="${FORGE_REPO_PATH}/_forge/specs/${FORGE_TASK_ID}.json"
if [ -f "$SPEC_FILE" ] && jq empty "$SPEC_FILE" 2>/dev/null; then
    # Extract spec criterion IDs
    SPEC_IDS=$(jq -r '.acceptance_criteria[]?.id // empty' "$SPEC_FILE" 2>/dev/null | sort -n)
    if [ -n "$SPEC_IDS" ]; then
        # Extract review criteria_check criterion IDs (from criterion field, try to match AC N pattern)
        REVIEW_CRITERIA=$(jq -r '.criteria_check[]?.criterion // empty' "$REVIEW_FILE" 2>/dev/null)
        MISSING=""
        for SID in $SPEC_IDS; do
            # Check if the review criteria_check mentions this criterion ID
            FOUND=false
            # First try: look for criterion_id field directly
            if jq -e ".criteria_check[]? | select(.criterion_id == $SID)" "$REVIEW_FILE" >/dev/null 2>&1; then
                FOUND=true
            fi
            # Second try: look for the ID in the criterion text (e.g. "AC 1:" or "1.")
            if [ "$FOUND" = false ] && echo "$REVIEW_CRITERIA" | grep -qE "(^|[^0-9])${SID}([^0-9]|$)"; then
                FOUND=true
            fi
            if [ "$FOUND" = false ]; then
                MISSING="${MISSING}  - Spec criterion ID $SID not covered in review\n"
            fi
        done
        if [ -n "$MISSING" ]; then
            echo "FAIL: Review does not cover all spec acceptance criteria:" >&2
            printf "%b" "$MISSING" >&2
            exit 1
        fi
    fi
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
