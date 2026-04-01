#!/usr/bin/env python3
"""Parse the verdict from a Forge review markdown file.

Reads the review file path from argv[1], extracts the verdict (PASS or ISSUES),
prints it to stdout, and exits 0. If no verdict is found or the file is missing,
prints an error to stderr and exits 1.

Supports formats:
  - ## Verdict: PASS
  - **Verdict: PASS**
  - **Verdict**: PASS
  - **Verdict:** PASS
  - ## Verdict\\n\\nPASS  (multi-line)
"""

import re
import sys

# Matches a line starting with optional markdown heading or bold markers,
# followed by "Verdict", optionally followed by a colon and the value.
# Groups: (1) optional value on same line
VERDICT_HEADER_RE = re.compile(
    r"^\s*(?:#{1,6}\s+|\*{1,2})"  # heading or bold marker
    r"Verdict"                      # the word
    r"(?:\*{0,2})"                  # optional closing bold
    r"(?::?\s*(.*?))?\s*$",         # optional colon + value
    re.IGNORECASE,
)

VERDICT_VALUE_RE = re.compile(r"\b(PASS|ISSUES)\b", re.IGNORECASE)


def parse_verdict(text: str) -> str | None:
    """Extract the verdict from review markdown text.

    Returns 'PASS' or 'ISSUES', or None if not found.
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = VERDICT_HEADER_RE.match(line)
        if m is None:
            continue
        # Check for value on same line (group 1 or rest of line)
        tail = m.group(1) or ""
        # Also check the full line for a verdict value
        vm = VERDICT_VALUE_RE.search(tail) or VERDICT_VALUE_RE.search(line)
        if vm:
            return vm.group(1).upper()
        # Multi-line: look at next non-blank line
        for j in range(i + 1, len(lines)):
            next_line = lines[j].strip()
            if not next_line:
                continue
            vm = VERDICT_VALUE_RE.search(next_line)
            if vm:
                return vm.group(1).upper()
            break  # non-blank line without verdict value
        return None  # header found but no value
    return None


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: parse_verdict.py <review-file>", file=sys.stderr)
        sys.exit(1)

    filepath = sys.argv[1]
    try:
        with open(filepath) as f:
            text = f.read()
    except FileNotFoundError:
        print(f"Error: file not found: {filepath}", file=sys.stderr)
        sys.exit(1)

    verdict = parse_verdict(text)
    if verdict is None:
        print("Error: could not find verdict in review file", file=sys.stderr)
        sys.exit(1)

    print(verdict)


if __name__ == "__main__":
    main()
