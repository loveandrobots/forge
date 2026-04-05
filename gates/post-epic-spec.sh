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

# Support structured output object with 'tasks' key, or bare array (legacy)
if isinstance(data, dict):
    tasks = data.get('tasks')
    if tasks is None:
        print('FAIL: Structured decomposition object missing \"tasks\" key', file=sys.stderr)
        sys.exit(1)
elif isinstance(data, list):
    tasks = data
else:
    print('FAIL: Decomposition must be a JSON object or array', file=sys.stderr)
    sys.exit(1)

if not isinstance(tasks, list):
    print('FAIL: Decomposition tasks must be an array', file=sys.stderr)
    sys.exit(1)

if len(tasks) == 0:
    print('FAIL: Decomposition tasks array is empty', file=sys.stderr)
    sys.exit(1)

errors = []

# Minimum child count
if len(tasks) < 2:
    errors.append('Decomposition must contain at least 2 child tasks (got %d). A single-task decomposition should not be an epic.' % len(tasks))

# Build sets for depends_on validation
all_titles = set()
for i, entry in enumerate(tasks):
    if isinstance(entry, dict):
        t = entry.get('title', '')
        if isinstance(t, str) and t.strip():
            all_titles.add(t.strip())

# Cross-reference phrases to reject in descriptions
CROSS_REF_PHRASES = [
    'as described in the epic',
    'see the epic',
    'see task above',
    'see parent',
    'as mentioned in the epic',
    'refer to the epic',
    'described above',
    'see above',
    'the parent task',
    'the epic task',
    'as outlined in the parent',
    'as outlined in the epic',
]

for i, entry in enumerate(tasks):
    if not isinstance(entry, dict):
        errors.append('Child %d: entry is not an object' % i)
        continue

    title = entry.get('title', '')
    if not isinstance(title, str) or not title.strip():
        errors.append('Child %d: missing or empty title' % i)
        title_label = '<untitled>'
    else:
        title_label = title.strip()

    # Description checks
    desc = entry.get('description')
    if desc is None or not isinstance(desc, str):
        errors.append(\"Child %d ('%s'): missing description\" % (i, title_label))
    elif len(desc.strip()) < 100:
        errors.append(\"Child %d ('%s'): description too short (%d chars, minimum 100)\" % (i, title_label, len(desc.strip())))
    else:
        # Cross-reference convention check
        desc_lower = desc.lower()
        for phrase in CROSS_REF_PHRASES:
            if phrase in desc_lower:
                errors.append(\"Child %d ('%s'): description contains cross-reference phrase '%s' — descriptions must be self-contained\" % (i, title_label, phrase))
                break

    # Flow validation
    flow = entry.get('flow')
    if flow is not None and flow not in ('standard', 'quick'):
        errors.append(\"Child %d ('%s'): invalid flow value '%s' (must be 'standard' or 'quick')\" % (i, title_label, flow))

    # depends_on validation
    deps = entry.get('depends_on')
    if deps is not None:
        if not isinstance(deps, list):
            errors.append(\"Child %d ('%s'): depends_on must be an array\" % (i, title_label))
        else:
            for dep in deps:
                if isinstance(dep, bool):
                    errors.append(\"Child %d ('%s'): depends_on entry must be an integer index or string title\" % (i, title_label))
                elif isinstance(dep, int):
                    if dep == i:
                        errors.append(\"Child %d ('%s'): depends_on references itself\" % (i, title_label))
                    elif dep < 0 or dep >= len(tasks):
                        errors.append(\"Child %d ('%s'): dangling depends_on reference to index %d (valid range 0-%d)\" % (i, title_label, dep, len(tasks) - 1))
                elif isinstance(dep, str):
                    if dep.strip() == title_label:
                        errors.append(\"Child %d ('%s'): depends_on references itself\" % (i, title_label))
                    elif dep.strip() not in all_titles:
                        errors.append(\"Child %d ('%s'): dangling depends_on reference to title '%s'\" % (i, title_label, dep))
                else:
                    errors.append(\"Child %d ('%s'): depends_on entry must be an integer index or string title\" % (i, title_label))

if errors:
    for err in errors:
        print('FAIL: ' + err, file=sys.stderr)
    sys.exit(1)
" "$DECOMP_FILE"

echo "post-epic-spec gate passed"
exit 0
