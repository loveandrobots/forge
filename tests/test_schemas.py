"""Tests for forge.schemas."""

from __future__ import annotations

from forge.schemas import REVIEW_SCHEMA, STAGE_SCHEMAS, get_schema


class TestGetSchema:
    def test_returns_none_for_unregistered_stage(self) -> None:
        result = get_schema("nonexistent")
        assert result is None

    def test_returns_schema_for_registered_stage(self) -> None:
        STAGE_SCHEMAS["test_stage"] = {"type": "object"}
        try:
            result = get_schema("test_stage")
            assert result == {"type": "object"}
        finally:
            del STAGE_SCHEMAS["test_stage"]

    def test_flow_specific_override(self) -> None:
        original_review = STAGE_SCHEMAS.get("review")
        STAGE_SCHEMAS["review"] = {"type": "object", "properties": {"verdict": {}}}
        STAGE_SCHEMAS["epic:review"] = {"type": "object", "properties": {"epic_verdict": {}}}
        try:
            result = get_schema("review", flow="epic")
            assert "epic_verdict" in result["properties"]

            result_standard = get_schema("review", flow="standard")
            assert "verdict" in result_standard["properties"]
        finally:
            del STAGE_SCHEMAS["epic:review"]
            if original_review is not None:
                STAGE_SCHEMAS["review"] = original_review
            else:
                del STAGE_SCHEMAS["review"]

    def test_falls_back_to_plain_stage_when_no_flow_override(self) -> None:
        STAGE_SCHEMAS["spec"] = {"type": "object"}
        try:
            result = get_schema("spec", flow="quick")
            assert result == {"type": "object"}
        finally:
            del STAGE_SCHEMAS["spec"]

    def test_empty_schemas_returns_none_for_unknown(self) -> None:
        assert get_schema("anything") is None


class TestReviewSchema:
    def test_review_schema_registered(self) -> None:
        """get_schema('review') returns a valid schema."""
        schema = get_schema("review")
        assert schema is not None
        assert schema["type"] == "object"

    def test_review_schema_same_for_quick_flow(self) -> None:
        """get_schema('review', 'quick') falls back to the same review schema."""
        schema = get_schema("review", "quick")
        assert schema is not None
        assert schema is REVIEW_SCHEMA

    def test_review_schema_has_required_fields(self) -> None:
        """Schema has all required fields with correct types."""
        schema = get_schema("review")
        props = schema["properties"]

        # verdict: enum string
        assert props["verdict"]["type"] == "string"
        assert set(props["verdict"]["enum"]) == {"PASS", "ISSUES"}

        # issues: array of objects with file, severity, description
        assert props["issues"]["type"] == "array"
        issue_props = props["issues"]["items"]["properties"]
        assert issue_props["file"]["type"] == "string"
        assert issue_props["severity"]["type"] == "string"
        assert set(issue_props["severity"]["enum"]) == {"critical", "major", "minor", "nit"}
        assert issue_props["description"]["type"] == "string"

        # criteria_check: array of objects with criterion, satisfied, evidence
        assert props["criteria_check"]["type"] == "array"
        cc_props = props["criteria_check"]["items"]["properties"]
        assert cc_props["criterion"]["type"] == "string"
        assert cc_props["satisfied"]["type"] == "boolean"
        assert cc_props["evidence"]["type"] == "string"

        # out_of_scope_changes: array of strings
        assert props["out_of_scope_changes"]["type"] == "array"
        assert props["out_of_scope_changes"]["items"]["type"] == "string"

        # summary: string
        assert props["summary"]["type"] == "string"

        # content: string
        assert props["content"]["type"] == "string"

    def test_review_schema_required_list(self) -> None:
        """All top-level fields are required."""
        schema = get_schema("review")
        required = set(schema["required"])
        expected = {"verdict", "issues", "criteria_check", "out_of_scope_changes", "summary", "content"}
        assert required == expected
