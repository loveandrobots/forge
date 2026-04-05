"""Tests for forge.schemas."""

from __future__ import annotations

from forge.schemas import STAGE_SCHEMAS, get_schema


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
        STAGE_SCHEMAS["review"] = {"type": "object", "properties": {"verdict": {}}}
        STAGE_SCHEMAS["epic:review"] = {"type": "object", "properties": {"epic_verdict": {}}}
        try:
            result = get_schema("review", flow="epic")
            assert "epic_verdict" in result["properties"]

            result_standard = get_schema("review", flow="standard")
            assert "verdict" in result_standard["properties"]
        finally:
            del STAGE_SCHEMAS["review"]
            del STAGE_SCHEMAS["epic:review"]

    def test_falls_back_to_plain_stage_when_no_flow_override(self) -> None:
        STAGE_SCHEMAS["spec"] = {"type": "object"}
        try:
            result = get_schema("spec", flow="quick")
            assert result == {"type": "object"}
        finally:
            del STAGE_SCHEMAS["spec"]

    def test_empty_schemas_dict(self) -> None:
        assert get_schema("anything") is None

    def test_default_stage_schemas_is_empty(self) -> None:
        assert STAGE_SCHEMAS == {}
