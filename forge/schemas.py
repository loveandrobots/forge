"""JSON schemas for structured output from Claude CLI --json-schema dispatches."""

from __future__ import annotations


# Stage schemas: keyed by stage name, with optional flow-specific overrides
# via "{flow}:{stage}" keys (e.g. "epic:spec").
STAGE_SCHEMAS: dict[str, dict] = {}


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
