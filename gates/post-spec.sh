#!/bin/bash
# Gate: post-spec
# Validates that a spec artifact was produced with required content.
#
# Environment variables (set by Forge gate runner):
#   FORGE_TASK_ID      — the task identifier
#   FORGE_REPO_PATH    — path to the project repository
#   FORGE_STAGE        — current stage name
#   FORGE_ATTEMPT      — attempt number
#   FORGE_BRANCH       — feature branch name
#   FORGE_SPEC_PATH    — path to the spec file

set -euo pipefail

SPEC_FILE="${FORGE_REPO_PATH}/_forge/specs/${FORGE_TASK_ID}.md"

# Check spec file exists
if [ ! -f "$SPEC_FILE" ]; then
    echo "FAIL: Spec file not found: $SPEC_FILE" >&2
    exit 1
fi

# Check file is >200 chars
CHAR_COUNT=$(wc -c < "$SPEC_FILE")
if [ "$CHAR_COUNT" -le 200 ]; then
    echo "FAIL: Spec file is too short (${CHAR_COUNT} chars, need >200)" >&2
    exit 1
fi

# Check for required sections
if ! grep -qi '## Acceptance criteria' "$SPEC_FILE"; then
    echo "FAIL: Spec file missing required section: '## Acceptance criteria'" >&2
    exit 1
fi

if ! grep -qi '## Out of scope' "$SPEC_FILE"; then
    echo "FAIL: Spec file missing required section: '## Out of scope'" >&2
    exit 1
fi

echo "post-spec gate passed"
exit 0
