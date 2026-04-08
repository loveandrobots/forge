"""Tests for forge.schemas."""

from __future__ import annotations

from forge.schemas import (
    EPIC_REVIEW_SCHEMA,
    EPIC_SPEC_SCHEMA,
    PLAN_SCHEMA,
    REVIEW_SCHEMA,
    SPEC_SCHEMA,
    STAGE_SCHEMAS,
    get_schema,
)


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
        original_epic_review = STAGE_SCHEMAS.get("epic:review")
        STAGE_SCHEMAS["review"] = {"type": "object", "properties": {"verdict": {}}}
        STAGE_SCHEMAS["epic:review"] = {"type": "object", "properties": {"epic_verdict": {}}}
        try:
            result = get_schema("review", flow="epic")
            assert "epic_verdict" in result["properties"]

            result_standard = get_schema("review", flow="standard")
            assert "verdict" in result_standard["properties"]
        finally:
            if original_epic_review is not None:
                STAGE_SCHEMAS["epic:review"] = original_epic_review
            else:
                del STAGE_SCHEMAS["epic:review"]
            if original_review is not None:
                STAGE_SCHEMAS["review"] = original_review
            else:
                del STAGE_SCHEMAS["review"]

    def test_falls_back_to_plain_stage_when_no_flow_override(self) -> None:
        STAGE_SCHEMAS["_test_stage"] = {"type": "object"}
        try:
            result = get_schema("_test_stage", flow="quick")
            assert result == {"type": "object"}
        finally:
            del STAGE_SCHEMAS["_test_stage"]

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


class TestSpecSchema:
    def test_spec_schema_registered(self) -> None:
        schema = get_schema("spec")
        assert schema is not None
        assert schema is SPEC_SCHEMA

    def test_spec_schema_has_required_fields(self) -> None:
        schema = get_schema("spec")
        required = set(schema["required"])
        assert required == {"overview", "acceptance_criteria", "out_of_scope", "dependencies", "content"}

    def test_spec_schema_acceptance_criteria_structure(self) -> None:
        schema = get_schema("spec")
        ac_items = schema["properties"]["acceptance_criteria"]["items"]
        assert ac_items["properties"]["id"]["type"] == "integer"
        assert ac_items["properties"]["text"]["type"] == "string"
        assert set(ac_items["required"]) == {"id", "text"}

    def test_get_schema_returns_none_for_implement(self) -> None:
        assert get_schema("implement") is None

    def test_get_schema_returns_none_for_quick_flow(self) -> None:
        assert get_schema("implement", flow="quick") is None

    def test_spec_schema_returns_for_standard_flow(self) -> None:
        assert get_schema("spec", flow="standard") is SPEC_SCHEMA


class TestPlanSchema:
    def test_plan_schema_registered(self) -> None:
        schema = get_schema("plan")
        assert schema is not None
        assert schema is PLAN_SCHEMA

    def test_plan_schema_has_required_fields(self) -> None:
        schema = get_schema("plan")
        required = set(schema["required"])
        assert required == {"approach", "acceptance_criteria_mapping", "files_to_modify", "test_plan", "risks", "content"}

    def test_plan_schema_mapping_structure(self) -> None:
        schema = get_schema("plan")
        mapping_items = schema["properties"]["acceptance_criteria_mapping"]["items"]
        assert mapping_items["properties"]["criterion_id"]["type"] == "integer"
        assert mapping_items["properties"]["criterion_text"]["type"] == "string"
        assert mapping_items["properties"]["implementation"]["type"] == "string"
        assert set(mapping_items["required"]) == {"criterion_id", "criterion_text", "implementation"}

    def test_plan_schema_content_field(self) -> None:
        schema = get_schema("plan")
        assert "content" in schema["properties"]
        assert schema["properties"]["content"]["type"] == "string"
        assert "content" in schema["required"]

    def test_plan_schema_test_plan_structure(self) -> None:
        schema = get_schema("plan")
        tp_items = schema["properties"]["test_plan"]["items"]
        assert tp_items["properties"]["criterion_id"]["type"] == "integer"
        assert tp_items["properties"]["description"]["type"] == "string"
        assert set(tp_items["required"]) == {"criterion_id", "description"}


class TestEpicSpecSchema:
    def test_epic_spec_schema_registered(self) -> None:
        schema = get_schema("spec", flow="epic")
        assert schema is not None
        assert schema["type"] == "object"
        assert "tasks" in schema["properties"]

    def test_epic_spec_schema_is_correct_object(self) -> None:
        assert get_schema("spec", flow="epic") is EPIC_SPEC_SCHEMA

    def test_standard_spec_unaffected(self) -> None:
        assert get_schema("spec") is SPEC_SCHEMA
        assert get_schema("spec", flow="standard") is SPEC_SCHEMA

    def test_epic_spec_schema_required_fields(self) -> None:
        schema = get_schema("spec", flow="epic")
        assert set(schema["required"]) == {"tasks", "rationale", "content"}

    def test_epic_spec_schema_tasks_array(self) -> None:
        schema = get_schema("spec", flow="epic")
        tasks_prop = schema["properties"]["tasks"]
        assert tasks_prop["type"] == "array"
        assert tasks_prop["minItems"] == 1

    def test_epic_spec_schema_task_item_properties(self) -> None:
        schema = get_schema("spec", flow="epic")
        item = schema["properties"]["tasks"]["items"]
        assert item["properties"]["title"]["type"] == "string"
        assert item["properties"]["description"]["type"] == "string"
        assert item["properties"]["flow"]["type"] == "string"
        assert set(item["properties"]["flow"]["enum"]) == {"standard", "quick"}
        assert item["properties"]["priority"]["type"] == "integer"
        assert item["required"] == ["title"]

    def test_epic_spec_schema_rationale_and_content(self) -> None:
        schema = get_schema("spec", flow="epic")
        assert schema["properties"]["rationale"]["type"] == "string"
        assert schema["properties"]["content"]["type"] == "string"

    def test_epic_spec_validation_valid(self) -> None:
        """A valid decomposition dict has the right structure."""
        schema = EPIC_SPEC_SCHEMA
        valid = {
            "tasks": [{"title": "Task A", "description": "Do something", "flow": "standard", "priority": 1}],
            "rationale": "Split into components",
            "content": "Full decomposition content",
        }
        # Verify required fields are present
        for field in schema["required"]:
            assert field in valid

    def test_epic_spec_validation_missing_tasks(self) -> None:
        """Missing tasks field violates required."""
        schema = EPIC_SPEC_SCHEMA
        invalid = {"rationale": "reason", "content": "stuff"}
        assert "tasks" not in invalid
        assert "tasks" in schema["required"]

    def test_epic_spec_validation_empty_tasks(self) -> None:
        """Empty tasks array violates minItems."""
        schema = EPIC_SPEC_SCHEMA
        assert schema["properties"]["tasks"]["minItems"] == 1


class TestEpicReviewSchema:
    def test_epic_review_schema_registered(self) -> None:
        schema = get_schema("review", flow="epic")
        assert schema is not None
        assert schema["type"] == "object"
        assert "verdict" in schema["properties"]

    def test_epic_review_schema_is_correct_object(self) -> None:
        assert get_schema("review", flow="epic") is EPIC_REVIEW_SCHEMA

    def test_standard_review_unaffected(self) -> None:
        assert get_schema("review") is REVIEW_SCHEMA
        assert get_schema("review", flow="standard") is REVIEW_SCHEMA

    def test_epic_review_schema_required_fields(self) -> None:
        schema = get_schema("review", flow="epic")
        expected = {"verdict", "epic_intent_check", "integration_check", "issues", "summary", "content"}
        assert set(schema["required"]) == expected

    def test_epic_review_schema_verdict(self) -> None:
        schema = get_schema("review", flow="epic")
        verdict = schema["properties"]["verdict"]
        assert verdict["type"] == "string"
        assert set(verdict["enum"]) == {"PASS", "ISSUES"}

    def test_epic_review_schema_issues_structure(self) -> None:
        schema = get_schema("review", flow="epic")
        issues = schema["properties"]["issues"]
        assert issues["type"] == "array"
        item = issues["items"]
        assert set(item["required"]) == {"severity", "description"}
        assert "file" in item["properties"]
        assert item["properties"]["severity"]["type"] == "string"
        assert item["properties"]["description"]["type"] == "string"

    def test_epic_review_schema_string_fields(self) -> None:
        schema = get_schema("review", flow="epic")
        for field in ("epic_intent_check", "integration_check", "summary", "content"):
            assert schema["properties"][field]["type"] == "string"

    def test_epic_review_validation_valid(self) -> None:
        """A valid review dict has the right structure."""
        valid = {
            "verdict": "PASS",
            "epic_intent_check": "All good",
            "integration_check": "Components integrate well",
            "issues": [],
            "summary": "Everything passes",
            "content": "Full review content",
        }
        for field in EPIC_REVIEW_SCHEMA["required"]:
            assert field in valid

    def test_epic_review_validation_missing_fields(self) -> None:
        """Missing required fields violate the schema."""
        incomplete = {"verdict": "PASS"}
        for field in EPIC_REVIEW_SCHEMA["required"]:
            if field != "verdict":
                assert field not in incomplete
