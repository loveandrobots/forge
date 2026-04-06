"""Tests for structured artifact rendering on the task detail dashboard."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import forge.config
from forge import database
from forge.main import app


@pytest.fixture()
def client():
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture()
def project_id(client: TestClient, tmp_path) -> str:
    resp = client.post(
        "/api/projects",
        json={"name": "ArtifactProject", "repo_path": str(tmp_path)},
    )
    return resp.json()["id"]


@pytest.fixture()
def task_id(client: TestClient, project_id: str) -> str:
    resp = client.post(
        "/api/tasks",
        json={"project_id": project_id, "title": "Artifact task", "priority": 5},
    )
    return resp.json()["id"]


def _set_artifact_path(task_id: str, field: str, path: str) -> None:
    """Update an artifact path directly in the database."""
    conn = database.get_connection(str(forge.config.DB_PATH))
    try:
        database.update_task(conn, task_id, **{field: path})
        conn.commit()
    finally:
        conn.close()


class TestReviewArtifactRendering:
    """Review JSON artifacts render with verdict, criteria, issues, and collapsible content."""

    def test_review_verdict_pass_badge(
        self, client: TestClient, task_id: str, tmp_path
    ) -> None:
        review = {
            "verdict": "PASS",
            "issues": [],
            "criteria_check": [],
            "out_of_scope_changes": [],
            "summary": "All good.",
            "content": "Full review text.",
        }
        path = tmp_path / "review.json"
        path.write_text(json.dumps(review))
        _set_artifact_path(task_id, "review_path", str(path))

        resp = client.get(f"/tasks/{task_id}")
        assert resp.status_code == 200
        html = resp.text
        assert "verdict-pass" in html
        assert ">PASS</span>" in html

    def test_review_verdict_issues_badge(
        self, client: TestClient, task_id: str, tmp_path
    ) -> None:
        review = {
            "verdict": "ISSUES",
            "issues": [
                {
                    "file": "foo.py",
                    "severity": "critical",
                    "description": "Missing validation",
                }
            ],
            "criteria_check": [],
            "out_of_scope_changes": [],
            "summary": "Problems found.",
            "content": "",
        }
        path = tmp_path / "review.json"
        path.write_text(json.dumps(review))
        _set_artifact_path(task_id, "review_path", str(path))

        resp = client.get(f"/tasks/{task_id}")
        html = resp.text
        assert "verdict-issues" in html
        assert ">ISSUES</span>" in html

    def test_review_criteria_checklist(
        self, client: TestClient, task_id: str, tmp_path
    ) -> None:
        review = {
            "verdict": "PASS",
            "issues": [],
            "criteria_check": [
                {
                    "criterion": "Tests pass",
                    "satisfied": True,
                    "evidence": "All 50 tests green",
                },
                {
                    "criterion": "No regressions",
                    "satisfied": False,
                    "evidence": "One flaky test",
                },
            ],
            "out_of_scope_changes": [],
            "summary": "",
            "content": "",
        }
        path = tmp_path / "review.json"
        path.write_text(json.dumps(review))
        _set_artifact_path(task_id, "review_path", str(path))

        resp = client.get(f"/tasks/{task_id}")
        html = resp.text
        assert "check-pass" in html
        assert "check-fail" in html
        assert "Tests pass" in html
        assert "No regressions" in html
        assert "All 50 tests green" in html

    def test_review_issues_with_severity_tags(
        self, client: TestClient, task_id: str, tmp_path
    ) -> None:
        review = {
            "verdict": "ISSUES",
            "issues": [
                {"file": "a.py", "severity": "critical", "description": "SQL injection"},
                {
                    "file": "b.py",
                    "severity": "major",
                    "description": "Missing error handling",
                },
                {"file": "c.py", "severity": "minor", "description": "Unused import"},
                {
                    "file": "d.py",
                    "severity": "nit",
                    "description": "Trailing whitespace",
                },
            ],
            "criteria_check": [],
            "out_of_scope_changes": [],
            "summary": "",
            "content": "",
        }
        path = tmp_path / "review.json"
        path.write_text(json.dumps(review))
        _set_artifact_path(task_id, "review_path", str(path))

        resp = client.get(f"/tasks/{task_id}")
        html = resp.text
        assert "severity-critical" in html
        assert "severity-major" in html
        assert "severity-minor" in html
        assert "severity-nit" in html
        assert "SQL injection" in html
        assert "a.py" in html

    def test_review_summary_displayed(
        self, client: TestClient, task_id: str, tmp_path
    ) -> None:
        review = {
            "verdict": "PASS",
            "issues": [],
            "criteria_check": [],
            "out_of_scope_changes": [],
            "summary": "Implementation meets all requirements.",
            "content": "",
        }
        path = tmp_path / "review.json"
        path.write_text(json.dumps(review))
        _set_artifact_path(task_id, "review_path", str(path))

        resp = client.get(f"/tasks/{task_id}")
        assert "Implementation meets all requirements." in resp.text

    def test_review_collapsible_content(
        self, client: TestClient, task_id: str, tmp_path
    ) -> None:
        review = {
            "verdict": "PASS",
            "issues": [],
            "criteria_check": [],
            "out_of_scope_changes": [],
            "summary": "",
            "content": "Detailed review content here.",
        }
        path = tmp_path / "review.json"
        path.write_text(json.dumps(review))
        _set_artifact_path(task_id, "review_path", str(path))

        resp = client.get(f"/tasks/{task_id}")
        html = resp.text
        assert "artifact-collapsible" in html
        assert "Detailed review content here." in html


class TestSpecArtifactRendering:
    """Spec JSON artifacts render with overview, acceptance criteria, and lists."""

    def test_spec_structured_rendering(
        self, client: TestClient, task_id: str, tmp_path
    ) -> None:
        spec = {
            "overview": "Build a widget system.",
            "acceptance_criteria": [
                {"id": 1, "text": "Widget renders"},
                {"id": 2, "text": "Widget is responsive"},
            ],
            "out_of_scope": ["Mobile app"],
            "dependencies": ["React 18"],
            "content": "Full spec markdown.",
        }
        path = tmp_path / "spec.json"
        path.write_text(json.dumps(spec))
        _set_artifact_path(task_id, "spec_path", str(path))

        resp = client.get(f"/tasks/{task_id}")
        html = resp.text
        assert "artifact-spec" in html
        assert "Build a widget system." in html
        assert "Widget renders" in html
        assert "Widget is responsive" in html
        assert "Mobile app" in html
        assert "React 18" in html
        assert "artifact-collapsible" in html
        assert "Full spec markdown." in html


class TestPlanArtifactRendering:
    """Plan JSON artifacts render with approach, mapping table, file list, etc."""

    def test_plan_structured_rendering(
        self, client: TestClient, task_id: str, tmp_path
    ) -> None:
        plan = {
            "approach": "Incremental refactor of the renderer.",
            "acceptance_criteria_mapping": [
                {
                    "criterion_id": 1,
                    "criterion_text": "Widget renders",
                    "implementation": "Add WidgetView component",
                },
            ],
            "files_to_modify": ["src/widget.py", "tests/test_widget.py"],
            "test_plan": [
                {"criterion_id": 1, "description": "Verify widget output"},
            ],
            "risks": ["Breaking change in public API"],
            "content": "Full plan text.",
        }
        path = tmp_path / "plan.json"
        path.write_text(json.dumps(plan))
        _set_artifact_path(task_id, "plan_path", str(path))

        resp = client.get(f"/tasks/{task_id}")
        html = resp.text
        assert "artifact-plan" in html
        assert "Incremental refactor of the renderer." in html
        assert "Widget renders" in html
        assert "Add WidgetView component" in html
        assert "src/widget.py" in html
        assert "Verify widget output" in html
        assert "Breaking change in public API" in html
        assert "artifact-collapsible" in html


class TestLegacyMarkdownArtifacts:
    """Legacy .md artifacts continue to render as plain text."""

    def test_markdown_review_renders_as_text(
        self, client: TestClient, task_id: str, tmp_path
    ) -> None:
        path = tmp_path / "review.md"
        path.write_text("# Review\n\nVerdict: PASS\n\nAll good.")
        _set_artifact_path(task_id, "review_path", str(path))

        resp = client.get(f"/tasks/{task_id}")
        html = resp.text
        assert "verdict-pass" not in html
        assert "# Review" in html
        assert "All good." in html

    def test_markdown_spec_renders_as_text(
        self, client: TestClient, task_id: str, tmp_path
    ) -> None:
        path = tmp_path / "spec.md"
        path.write_text("# Spec\n\nBuild the thing.")
        _set_artifact_path(task_id, "spec_path", str(path))

        resp = client.get(f"/tasks/{task_id}")
        html = resp.text
        assert "artifact-spec" not in html
        assert "Build the thing." in html

    def test_markdown_plan_renders_as_text(
        self, client: TestClient, task_id: str, tmp_path
    ) -> None:
        path = tmp_path / "plan.md"
        path.write_text("# Plan\n\nStep 1: Do things.")
        _set_artifact_path(task_id, "plan_path", str(path))

        resp = client.get(f"/tasks/{task_id}")
        html = resp.text
        assert "artifact-plan" not in html
        assert "Step 1: Do things." in html


class TestNoArtifacts:
    """Task detail page still works with no artifacts."""

    def test_no_artifacts_section_when_empty(
        self, client: TestClient, task_id: str
    ) -> None:
        resp = client.get(f"/tasks/{task_id}")
        assert resp.status_code == 200
        html = resp.text
        assert ">Artifacts</h2>" not in html
        assert "artifact-panel" not in html

    def test_missing_artifact_file_handled(
        self, client: TestClient, task_id: str
    ) -> None:
        _set_artifact_path(task_id, "review_path", "/nonexistent/review.json")

        resp = client.get(f"/tasks/{task_id}")
        assert resp.status_code == 200
        assert "artifact-panel" not in resp.text


class TestMixedArtifacts:
    """Tasks with a mix of JSON and MD artifacts render each correctly."""

    def test_json_review_with_md_spec(
        self, client: TestClient, task_id: str, tmp_path
    ) -> None:
        spec_path = tmp_path / "spec.md"
        spec_path.write_text("# Spec\n\nMarkdown spec content.")
        _set_artifact_path(task_id, "spec_path", str(spec_path))

        review = {
            "verdict": "PASS",
            "issues": [],
            "criteria_check": [],
            "out_of_scope_changes": [],
            "summary": "Looks good.",
            "content": "",
        }
        review_path = tmp_path / "review.json"
        review_path.write_text(json.dumps(review))
        _set_artifact_path(task_id, "review_path", str(review_path))

        resp = client.get(f"/tasks/{task_id}")
        html = resp.text
        assert "verdict-pass" in html
        assert "Markdown spec content." in html
        assert "artifact-spec" not in html


class TestReviewSeverityColorsDistinct:
    """Severity tags must be color-coded and visually distinct (AC #5)."""

    def test_all_four_severity_classes_present(
        self, client: TestClient, task_id: str, tmp_path
    ) -> None:
        """Each severity level uses a unique CSS class for color coding."""
        review = {
            "verdict": "ISSUES",
            "issues": [
                {"file": "a.py", "severity": "critical", "description": "d1"},
                {"file": "b.py", "severity": "major", "description": "d2"},
                {"file": "c.py", "severity": "minor", "description": "d3"},
                {"file": "d.py", "severity": "nit", "description": "d4"},
            ],
            "criteria_check": [],
            "out_of_scope_changes": [],
            "summary": "",
            "content": "",
        }
        path = tmp_path / "review.json"
        path.write_text(json.dumps(review))
        _set_artifact_path(task_id, "review_path", str(path))

        resp = client.get(f"/tasks/{task_id}")
        html = resp.text
        # Each severity level maps to its own CSS class
        assert 'class="severity-tag severity-critical"' in html
        assert 'class="severity-tag severity-major"' in html
        assert 'class="severity-tag severity-minor"' in html
        assert 'class="severity-tag severity-nit"' in html


class TestCollapsibleSectionsNative:
    """Collapsible sections use native <details>/<summary> — no JS framework (AC #6)."""

    def test_collapsible_uses_details_element(
        self, client: TestClient, task_id: str, tmp_path
    ) -> None:
        review = {
            "verdict": "PASS",
            "issues": [],
            "criteria_check": [],
            "out_of_scope_changes": [],
            "summary": "",
            "content": "Full review content for collapsing.",
        }
        path = tmp_path / "review.json"
        path.write_text(json.dumps(review))
        _set_artifact_path(task_id, "review_path", str(path))

        resp = client.get(f"/tasks/{task_id}")
        html = resp.text
        assert "<details" in html
        assert "<summary>" in html
        assert "Full review content for collapsing." in html

    def test_spec_collapsible_uses_details_element(
        self, client: TestClient, task_id: str, tmp_path
    ) -> None:
        spec = {
            "overview": "Overview text.",
            "acceptance_criteria": [{"id": 1, "text": "AC1"}],
            "out_of_scope": [],
            "dependencies": [],
            "content": "Full spec for collapsing.",
        }
        path = tmp_path / "spec.json"
        path.write_text(json.dumps(spec))
        _set_artifact_path(task_id, "spec_path", str(path))

        resp = client.get(f"/tasks/{task_id}")
        html = resp.text
        assert "Full spec for collapsing." in html
        assert "artifact-collapsible" in html


class TestSpecAcceptanceCriteriaObjectFormat:
    """Spec acceptance_criteria renders correctly with id+text object format."""

    def test_object_format_renders_text_not_dict(
        self, client: TestClient, task_id: str, tmp_path
    ) -> None:
        spec = {
            "overview": "Test spec.",
            "acceptance_criteria": [
                {"id": 1, "text": "First criterion"},
                {"id": 2, "text": "Second criterion"},
            ],
            "out_of_scope": [],
            "dependencies": [],
            "content": "",
        }
        path = tmp_path / "spec.json"
        path.write_text(json.dumps(spec))
        _set_artifact_path(task_id, "spec_path", str(path))

        resp = client.get(f"/tasks/{task_id}")
        html = resp.text
        assert "First criterion" in html
        assert "Second criterion" in html
        # Must NOT render the raw dict representation
        assert "&#39;id&#39;" not in html
        assert "{'id'" not in html


class TestPlanSchemaFieldNames:
    """Plan template correctly uses schema field names (criterion_text, criterion_id)."""

    def test_criterion_text_rendered_in_mapping_table(
        self, client: TestClient, task_id: str, tmp_path
    ) -> None:
        plan = {
            "approach": "Direct approach.",
            "acceptance_criteria_mapping": [
                {
                    "criterion_id": 1,
                    "criterion_text": "Must handle edge cases",
                    "implementation": "Add validation layer",
                },
            ],
            "files_to_modify": [],
            "test_plan": [
                {"criterion_id": 1, "description": "Test edge case handling"},
            ],
            "risks": [],
            "content": "",
        }
        path = tmp_path / "plan.json"
        path.write_text(json.dumps(plan))
        _set_artifact_path(task_id, "plan_path", str(path))

        resp = client.get(f"/tasks/{task_id}")
        html = resp.text
        assert "Must handle edge cases" in html
        assert "Add validation layer" in html
        assert "Test edge case handling" in html


class TestLoadArtifactHelper:
    """Unit tests for _load_artifact helper function."""

    def test_load_json_artifact(self, tmp_path) -> None:
        from forge.routers.dashboard import _load_artifact

        data = {"verdict": "PASS", "summary": "ok"}
        path = tmp_path / "review.json"
        path.write_text(json.dumps(data))
        result = _load_artifact(str(path))
        assert isinstance(result, dict)
        assert result["verdict"] == "PASS"

    def test_load_md_artifact(self, tmp_path) -> None:
        from forge.routers.dashboard import _load_artifact

        path = tmp_path / "review.md"
        path.write_text("# Review\nAll good.")
        result = _load_artifact(str(path))
        assert isinstance(result, str)
        assert "# Review" in result

    def test_load_none_path(self) -> None:
        from forge.routers.dashboard import _load_artifact

        assert _load_artifact(None) is None

    def test_load_missing_file(self) -> None:
        from forge.routers.dashboard import _load_artifact

        assert _load_artifact("/nonexistent/file.json") is None

    def test_load_invalid_json(self, tmp_path) -> None:
        from forge.routers.dashboard import _load_artifact

        path = tmp_path / "bad.json"
        path.write_text("not valid json {{{")
        result = _load_artifact(str(path))
        assert result is None
