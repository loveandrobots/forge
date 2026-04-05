"""JSON schemas for structured output from Claude CLI --json-schema dispatches."""

from __future__ import annotations


# Stage schemas: keyed by stage name, with optional flow-specific overrides
# via "{flow}:{stage}" keys (e.g. "epic:spec").
STAGE_SCHEMAS: dict[str, dict] = {}

REVIEW_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["PASS", "ISSUES"],
        },
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "severity": {
                        "type": "string",
                        "enum": ["critical", "major", "minor", "nit"],
                    },
                    "description": {"type": "string"},
                },
                "required": ["file", "severity", "description"],
            },
        },
        "criteria_check": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "criterion": {"type": "string"},
                    "satisfied": {"type": "boolean"},
                    "evidence": {"type": "string"},
                },
                "required": ["criterion", "satisfied", "evidence"],
            },
        },
        "out_of_scope_changes": {
            "type": "array",
            "items": {"type": "string"},
        },
        "summary": {"type": "string"},
        "content": {"type": "string"},
    },
    "required": [
        "verdict",
        "issues",
        "criteria_check",
        "out_of_scope_changes",
        "summary",
        "content",
    ],
}

STAGE_SCHEMAS["review"] = REVIEW_SCHEMA


def get_schema(stage: str, flow: str = "standard") -> dict | None:
    """Look up a JSON schema by stage name.

    Checks for a flow-specific override first (``{flow}:{stage}``),
    then falls back to the plain stage key.  Returns None if no schema
    is registered for the given stage.
    """
    flow_key = f"{flow}:{stage}"
    if flow_key in STAGE_SCHEMAS:
        return STAGE_SCHEMAS[flow_key]
    return STAGE_SCHEMAS.get(stage)
