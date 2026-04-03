#!/bin/bash
# Gate: post-epic-spec
# Validates that the epic decomposition JSON was produced with valid content.
#
# Environment variables (set by Forge gate runner):
#   FORGE_TASK_ID      — the task identifier
#   FORGE_REPO_PATH    — path to the project repository

set -euo pipefail

DECOMP_FILE="${FORGE_REPO_PATH}/_forge/epic-decompositions/${FORGE_TASK_ID}.json"

# Check decomposition file exists
if [ ! -f "$DECOMP_FILE" ]; then
    echo "FAIL: Epic decomposition file not found: $DECOMP_FILE" >&2
    exit 1
fi

# Validate JSON and check structure
python3 -c "
import json, sys

with open(sys.argv[1]) as f:
    data = json.load(f)

if not isinstance(data, list):
    print('FAIL: Decomposition must be a JSON array', file=sys.stderr)
    sys.exit(1)

if len(data) == 0:
    print('FAIL: Decomposition array is empty', file=sys.stderr)
    sys.exit(1)

for i, entry in enumerate(data):
    if not isinstance(entry, dict):
        print(f'FAIL: Entry {i} is not an object', file=sys.stderr)
        sys.exit(1)
    title = entry.get('title', '')
    if not isinstance(title, str) or not title.strip():
        print(f'FAIL: Entry {i} missing or empty title', file=sys.stderr)
        sys.exit(1)
" "$DECOMP_FILE"

echo "post-epic-spec gate passed"
exit 0
