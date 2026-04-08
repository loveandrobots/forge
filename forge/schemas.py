"""JSON schemas for structured output from Claude CLI --json-schema dispatches."""

from __future__ import annotations


# Stage schemas: keyed by stage name, with optional flow-specific overrides
# via "{flow}:{stage}" keys (e.g. "epic:spec").
STAGE_SCHEMAS: dict[str, dict] = {}

SPEC_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "overview": {"type": "string"},
        "acceptance_criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "text": {"type": "string"},
                },
                "required": ["id", "text"],
            },
        },
        "out_of_scope": {
            "type": "array",
            "items": {"type": "string"},
        },
        "dependencies": {
            "type": "array",
            "items": {"type": "string"},
        },
        "content": {"type": "string"},
    },
    "required": [
        "overview",
        "acceptance_criteria",
        "out_of_scope",
        "dependencies",
        "content",
    ],
}

PLAN_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "approach": {"type": "string"},
        "acceptance_criteria_mapping": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "criterion_id": {"type": "integer"},
                    "criterion_text": {"type": "string"},
                    "implementation": {"type": "string"},
                },
                "required": ["criterion_id", "criterion_text", "implementation"],
            },
        },
        "files_to_modify": {
            "type": "array",
            "items": {"type": "string"},
        },
        "test_plan": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "criterion_id": {"type": "integer"},
                    "description": {"type": "string"},
                },
                "required": ["criterion_id", "description"],
            },
        },
        "risks": {
            "type": "array",
            "items": {"type": "string"},
        },
        "content": {"type": "string"},
    },
    "required": [
        "approach",
        "acceptance_criteria_mapping",
        "files_to_modify",
        "test_plan",
        "risks",
        "content",
    ],
}

STAGE_SCHEMAS["spec"] = SPEC_SCHEMA
STAGE_SCHEMAS["plan"] = PLAN_SCHEMA

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

EPIC_SPEC_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "flow": {
                        "type": "string",
                        "enum": ["standard", "quick"],
                    },
                    "priority": {"type": "integer"},
                },
                "required": ["title"],
            },
        },
        "rationale": {"type": "string"},
        "content": {"type": "string"},
    },
    "required": ["tasks", "rationale", "content"],
}

STAGE_SCHEMAS["epic:spec"] = EPIC_SPEC_SCHEMA

EPIC_REVIEW_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["PASS", "ISSUES"],
        },
        "epic_intent_check": {"type": "string"},
        "integration_check": {"type": "string"},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string"},
                    "description": {"type": "string"},
                    "file": {"type": "string"},
                },
                "required": ["severity", "description"],
            },
        },
        "summary": {"type": "string"},
        "content": {"type": "string"},
    },
    "required": [
        "verdict",
        "epic_intent_check",
        "integration_check",
        "issues",
        "summary",
        "content",
    ],
}

STAGE_SCHEMAS["epic:review"] = EPIC_REVIEW_SCHEMA


def get_schema(stage: str, flow: str = "standard") -> dict | None:
    """Look up a JSON schema by stage name.

    Checks for a flow-specific override first (``{flow}:{stage}``),
    then falls back to the plain stage key.  Returns None if no schema
    is registered for the given stage or if the flow explicitly opts out.
    """
    flow_key = f"{flow}:{stage}"
    if flow_key in STAGE_SCHEMAS:
        return STAGE_SCHEMAS[flow_key]
    return STAGE_SCHEMAS.get(stage)
