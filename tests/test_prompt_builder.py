"""Tests for forge.prompt_builder."""

from __future__ import annotations

import json
import subprocess

from pathlib import Path

import pytest

from forge.prompt_builder import (
    EPIC_REVIEW_TEMPLATE,
    EPIC_STAGE_TEMPLATES,
    STAGE_TEMPLATES,
    build_prompt,
    build_retry_context,
    build_review_feedback_context,
    get_git_diff,
    load_artifact,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_task() -> dict:
    return {
        "id": "abc-123",
        "title": "Add widget support",
        "description": "Implement the widget subsystem.",
        "branch_name": "forge/abc-add-widget",
        "spec_path": "",
        "plan_path": "",
        "skill_overrides": None,
    }


@pytest.fixture()
def sample_project() -> dict:
    return {
        "name": "TestProject",
        "skill_refs": ["CLAUDE.md"],
        "repo_path": "/tmp/repo",
        "default_branch": "main",
    }


@pytest.fixture()
def sample_stage_run() -> dict:
    return {"attempt": 1}


@pytest.fixture()
def empty_artifacts() -> dict:
    return {}


# ---------------------------------------------------------------------------
# load_artifact
# ---------------------------------------------------------------------------


class TestLoadArtifact:
    def test_reads_existing_file(self, tmp_path: Path) -> None:
        p = tmp_path / "spec.md"
        p.write_text("# Spec\nHello")
        assert load_artifact(str(p)) == "# Spec\nHello"

    def test_returns_empty_for_missing_file(self) -> None:
        assert load_artifact("/nonexistent/path/spec.md") == ""

    def test_returns_empty_for_empty_path(self) -> None:
        assert load_artifact("") == ""

    def test_returns_empty_for_directory(self, tmp_path: Path) -> None:
        assert load_artifact(str(tmp_path)) == ""


# ---------------------------------------------------------------------------
# build_retry_context
# ---------------------------------------------------------------------------


class TestBuildRetryContext:
    def test_no_retry_for_attempt_1(self) -> None:
        assert build_retry_context(1, "some error") == ""

    def test_retry_context_for_attempt_2(self) -> None:
        result = build_retry_context(2, "lint failed: unused import")
        assert "attempt 2" in result
        assert "lint failed: unused import" in result
        assert "Previous attempt failed" in result

    def test_retry_context_for_attempt_3(self) -> None:
        result = build_retry_context(3, "tests failed")
        assert "attempt 3" in result
        assert "tests failed" in result


# ---------------------------------------------------------------------------
# get_git_diff
# ---------------------------------------------------------------------------


class TestGetGitDiff:
    def test_returns_diff_output(self, tmp_path: Path) -> None:
        repo = str(tmp_path)
        subprocess.run(
            ["git", "init", "-b", "main"], cwd=repo, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=repo,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=repo,
            capture_output=True,
            check=True,
        )

        # Initial commit on main
        f = tmp_path / "hello.txt"
        f.write_text("hello")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "init"], cwd=repo, capture_output=True, check=True
        )

        # Create branch and make a change
        subprocess.run(
            ["git", "checkout", "-b", "feature"],
            cwd=repo,
            capture_output=True,
            check=True,
        )
        f.write_text("hello world")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "change"], cwd=repo, capture_output=True, check=True
        )

        diff = get_git_diff(repo, "feature", "main")
        assert "hello world" in diff

    def test_returns_empty_for_invalid_repo(self, tmp_path: Path) -> None:
        result = get_git_diff(str(tmp_path), "feature", "main")
        assert result == ""

    def test_returns_empty_for_nonexistent_path(self) -> None:
        result = get_git_diff("/nonexistent/repo", "feature", "main")
        assert result == ""


# ---------------------------------------------------------------------------
# build_prompt — template selection and filling
# ---------------------------------------------------------------------------


class TestBuildPrompt:
    def test_unknown_stage_raises(
        self,
        sample_task: dict,
        sample_project: dict,
        sample_stage_run: dict,
        empty_artifacts: dict,
    ) -> None:
        with pytest.raises(ValueError, match="Unknown stage"):
            build_prompt(
                "invalid",
                sample_task,
                sample_project,
                sample_stage_run,
                empty_artifacts,
            )

    def test_spec_prompt_filled(
        self,
        sample_task: dict,
        sample_project: dict,
        sample_stage_run: dict,
        empty_artifacts: dict,
    ) -> None:
        prompt = build_prompt(
            "spec", sample_task, sample_project, sample_stage_run, empty_artifacts
        )
        assert "TestProject" in prompt
        assert "Add widget support" in prompt
        assert "Implement the widget subsystem." in prompt
        assert "abc-123" in prompt
        assert "CLAUDE.md" in prompt
        # No retry context on attempt 1
        assert "Previous attempt failed" not in prompt

    def test_plan_prompt_includes_spec_content(
        self,
        sample_task: dict,
        sample_project: dict,
        sample_stage_run: dict,
    ) -> None:
        artifacts = {"spec_content": "The spec says do X."}
        prompt = build_prompt(
            "plan", sample_task, sample_project, sample_stage_run, artifacts
        )
        assert "The spec says do X." in prompt
        assert "_forge/plans/abc-123.md" in prompt

    def test_plan_prompt_contains_acceptance_criteria_phrase(
        self,
        sample_task: dict,
        sample_project: dict,
        sample_stage_run: dict,
        empty_artifacts: dict,
    ) -> None:
        """Plan prompt must contain 'acceptance criteria' so the post-plan gate passes."""
        prompt = build_prompt(
            "plan", sample_task, sample_project, sample_stage_run, empty_artifacts
        )
        assert "acceptance criteria" in prompt.lower()

    def test_plan_prompt_has_acceptance_criteria_mapping_section(
        self,
        sample_task: dict,
        sample_project: dict,
        sample_stage_run: dict,
        empty_artifacts: dict,
    ) -> None:
        """Plan prompt must instruct the agent to produce an 'Acceptance criteria mapping' section."""
        prompt = build_prompt(
            "plan", sample_task, sample_project, sample_stage_run, empty_artifacts
        )
        assert "Acceptance criteria mapping" in prompt

    def test_implement_prompt_includes_plan_and_spec(
        self,
        sample_task: dict,
        sample_project: dict,
        sample_stage_run: dict,
    ) -> None:
        artifacts = {
            "spec_content": "Spec text here.",
            "plan_content": "Plan text here.",
        }
        prompt = build_prompt(
            "implement", sample_task, sample_project, sample_stage_run, artifacts
        )
        assert "Spec text here." in prompt
        assert "Plan text here." in prompt
        assert "forge/abc-add-widget" in prompt

    def test_review_prompt_includes_diff(
        self,
        sample_task: dict,
        sample_project: dict,
        sample_stage_run: dict,
    ) -> None:
        artifacts = {
            "spec_content": "Spec.",
            "git_diff": "diff --git a/f.py b/f.py\n+new line",
        }
        prompt = build_prompt(
            "review", sample_task, sample_project, sample_stage_run, artifacts
        )
        assert "diff --git" in prompt
        assert "_forge/reviews/abc-123.md" in prompt

    def test_all_templates_have_no_unfilled_placeholders(
        self,
        sample_task: dict,
        sample_project: dict,
        sample_stage_run: dict,
    ) -> None:
        artifacts = {
            "spec_content": "spec body",
            "plan_content": "plan body",
            "git_diff": "diff output",
        }
        for stage in STAGE_TEMPLATES:
            prompt = build_prompt(
                stage, sample_task, sample_project, sample_stage_run, artifacts
            )
            # No remaining {placeholder} tokens
            assert "{" not in prompt, f"Unfilled placeholder in {stage} prompt"


# ---------------------------------------------------------------------------
# build_prompt — retry context
# ---------------------------------------------------------------------------


class TestBuildPromptRetry:
    def test_retry_context_appended_on_attempt_2(
        self,
        sample_task: dict,
        sample_project: dict,
    ) -> None:
        stage_run = {"attempt": 2}
        artifacts = {"previous_gate_stderr": "ruff check failed: E501"}
        prompt = build_prompt("spec", sample_task, sample_project, stage_run, artifacts)
        assert "attempt 2" in prompt
        assert "ruff check failed: E501" in prompt

    def test_no_retry_context_on_attempt_1(
        self,
        sample_task: dict,
        sample_project: dict,
        sample_stage_run: dict,
    ) -> None:
        artifacts = {"previous_gate_stderr": "should not appear"}
        prompt = build_prompt(
            "spec", sample_task, sample_project, sample_stage_run, artifacts
        )
        assert "Previous attempt failed" not in prompt


# ---------------------------------------------------------------------------
# build_prompt — skill references
# ---------------------------------------------------------------------------


class TestBuildPromptSkills:
    def test_task_skill_overrides_take_precedence(
        self,
        sample_project: dict,
        sample_stage_run: dict,
        empty_artifacts: dict,
    ) -> None:
        task = {
            "id": "t1",
            "title": "T",
            "description": "D",
            "branch_name": "b",
            "skill_overrides": ["CUSTOM.md", "OTHER.md"],
        }
        prompt = build_prompt(
            "spec", task, sample_project, sample_stage_run, empty_artifacts
        )
        assert "CUSTOM.md" in prompt
        assert "OTHER.md" in prompt
        # Project-level skill should NOT appear when overrides are set
        assert "CLAUDE.md" not in prompt

    def test_no_skills_shows_none(
        self,
        sample_stage_run: dict,
        empty_artifacts: dict,
    ) -> None:
        task = {
            "id": "t1",
            "title": "T",
            "description": "D",
            "branch_name": "b",
            "skill_overrides": None,
        }
        project = {"name": "P", "skill_refs": None}
        prompt = build_prompt("spec", task, project, sample_stage_run, empty_artifacts)
        assert "(none)" in prompt

    def test_json_string_skill_refs(
        self,
        sample_stage_run: dict,
        empty_artifacts: dict,
    ) -> None:
        """skill_refs may come from SQLite as a JSON string."""
        task = {
            "id": "t1",
            "title": "T",
            "description": "D",
            "branch_name": "b",
            "skill_overrides": json.dumps(["A.md", "B.md"]),
        }
        project = {"name": "P", "skill_refs": None}
        prompt = build_prompt("spec", task, project, sample_stage_run, empty_artifacts)
        assert "A.md" in prompt
        assert "B.md" in prompt


# ---------------------------------------------------------------------------
# build_review_feedback_context
# ---------------------------------------------------------------------------


class TestBuildReviewFeedbackContext:
    def test_empty_content_returns_empty(self) -> None:
        assert build_review_feedback_context("") == ""

    def test_formats_review_feedback(self) -> None:
        result = build_review_feedback_context("- Issue 1\n- Issue 2")
        assert result.startswith("## Review feedback")
        assert "- Issue 1" in result
        assert "- Issue 2" in result
        assert "Fix all issues listed below" in result


# ---------------------------------------------------------------------------
# build_prompt — review feedback in implement template
# ---------------------------------------------------------------------------


class TestBuildPromptReviewFeedback:
    def test_implement_prompt_includes_review_feedback(
        self,
        sample_task: dict,
        sample_project: dict,
        sample_stage_run: dict,
    ) -> None:
        """AC 5, 6, 17: Implement prompt includes review feedback section."""
        artifacts = {
            "spec_content": "Spec.",
            "plan_content": "Plan.",
            "review_feedback": "- Bug in parser\n- Missing test",
        }
        prompt = build_prompt(
            "implement", sample_task, sample_project, sample_stage_run, artifacts
        )
        assert "## Review feedback" in prompt
        assert "- Bug in parser" in prompt
        assert "- Missing test" in prompt

    def test_implement_prompt_no_unfilled_review_feedback_placeholder(
        self,
        sample_task: dict,
        sample_project: dict,
        sample_stage_run: dict,
        empty_artifacts: dict,
    ) -> None:
        """AC 6: No unfilled {review_feedback} placeholder when no feedback."""
        prompt = build_prompt(
            "implement", sample_task, sample_project, sample_stage_run, empty_artifacts
        )
        assert "{review_feedback}" not in prompt

    def test_review_feedback_distinct_from_retry_context(
        self,
        sample_task: dict,
        sample_project: dict,
    ) -> None:
        """AC 6: Review feedback section is distinct from gate-failure retry context."""
        stage_run = {"attempt": 2}
        artifacts = {
            "spec_content": "Spec.",
            "plan_content": "Plan.",
            "review_feedback": "- Review issue here",
            "previous_gate_stderr": "lint failed",
        }
        prompt = build_prompt(
            "implement", sample_task, sample_project, stage_run, artifacts
        )
        assert "## Review feedback" in prompt
        assert "## Previous attempt failed" in prompt
        assert "- Review issue here" in prompt
        assert "lint failed" in prompt


# ---------------------------------------------------------------------------
# build_prompt — review template issue categorization
# ---------------------------------------------------------------------------


class TestBuildPromptReviewCategorization:
    def test_review_prompt_has_issue_categorization(
        self,
        sample_task: dict,
        sample_project: dict,
        sample_stage_run: dict,
    ) -> None:
        """AC 7, 9: Review prompt instructs separating pre-existing from task-related issues."""
        artifacts = {"spec_content": "Spec.", "git_diff": "diff"}
        prompt = build_prompt(
            "review", sample_task, sample_project, sample_stage_run, artifacts
        )
        assert "Task-related issues" in prompt
        assert "Pre-existing issues" in prompt
        assert "do NOT affect your verdict" in prompt

    def test_review_prompt_specifies_follow_ups_json(
        self,
        sample_task: dict,
        sample_project: dict,
        sample_stage_run: dict,
    ) -> None:
        """AC 8: Review prompt instructs writing pre-existing issues to follow-ups JSON."""
        artifacts = {"spec_content": "Spec.", "git_diff": "diff"}
        prompt = build_prompt(
            "review", sample_task, sample_project, sample_stage_run, artifacts
        )
        assert "_forge/follow-ups/" in prompt
        assert "title" in prompt
        assert "description" in prompt

    def test_review_template_mentions_flow_field(
        self,
        sample_task: dict,
        sample_project: dict,
        sample_stage_run: dict,
    ) -> None:
        """Review prompt instructs the agent about the optional flow field in follow-ups."""
        from forge.prompt_builder import REVIEW_TEMPLATE

        assert "flow" in REVIEW_TEMPLATE
        assert '"quick"' in REVIEW_TEMPLATE
        assert '"standard"' in REVIEW_TEMPLATE


# ---------------------------------------------------------------------------
# build_prompt — quick-flow templates
# ---------------------------------------------------------------------------


class TestBuildPromptQuickFlow:
    @pytest.fixture()
    def quick_task(self) -> dict:
        return {
            "id": "qf-001",
            "title": "Fix login button color",
            "description": "Change the login button from blue to green.",
            "branch_name": "forge/qf-001-fix-login-button",
            "spec_path": "",
            "plan_path": "",
            "skill_overrides": None,
            "flow": "quick",
        }

    def test_quick_implement_prompt_has_task_description(
        self,
        quick_task: dict,
        sample_project: dict,
        sample_stage_run: dict,
        empty_artifacts: dict,
    ) -> None:
        prompt = build_prompt(
            "implement", quick_task, sample_project, sample_stage_run, empty_artifacts
        )
        assert "Fix login button color" in prompt
        assert "Change the login button from blue to green." in prompt
        assert "## Specification" not in prompt
        assert "## Implementation plan" not in prompt

    def test_quick_implement_prompt_has_no_empty_spec_plan_sections(
        self,
        quick_task: dict,
        sample_project: dict,
        sample_stage_run: dict,
        empty_artifacts: dict,
    ) -> None:
        prompt = build_prompt(
            "implement", quick_task, sample_project, sample_stage_run, empty_artifacts
        )
        assert "{spec_content}" not in prompt
        assert "{plan_content}" not in prompt
        assert "## Specification" not in prompt
        assert "## Implementation plan" not in prompt

    def test_quick_review_prompt_has_task_description(
        self,
        quick_task: dict,
        sample_project: dict,
        sample_stage_run: dict,
    ) -> None:
        artifacts = {"git_diff": "diff --git a/btn.py"}
        prompt = build_prompt(
            "review", quick_task, sample_project, sample_stage_run, artifacts
        )
        assert "Fix login button color" in prompt
        assert "Change the login button from blue to green." in prompt
        assert "## Task description" in prompt
        assert "## Specification" not in prompt

    def test_quick_review_prompt_includes_diff(
        self,
        quick_task: dict,
        sample_project: dict,
        sample_stage_run: dict,
    ) -> None:
        artifacts = {"git_diff": "diff --git a/btn.py b/btn.py\n+color = green"}
        prompt = build_prompt(
            "review", quick_task, sample_project, sample_stage_run, artifacts
        )
        assert "diff --git a/btn.py" in prompt
        assert "+color = green" in prompt

    def test_quick_review_prompt_has_issue_categorization(
        self,
        quick_task: dict,
        sample_project: dict,
        sample_stage_run: dict,
    ) -> None:
        artifacts = {"git_diff": "diff"}
        prompt = build_prompt(
            "review", quick_task, sample_project, sample_stage_run, artifacts
        )
        assert "Task-related issues" in prompt
        assert "Pre-existing issues" in prompt
        assert "_forge/follow-ups/" in prompt

    def test_standard_flow_implement_unchanged(
        self,
        sample_task: dict,
        sample_project: dict,
        sample_stage_run: dict,
    ) -> None:
        artifacts = {"spec_content": "Spec.", "plan_content": "Plan."}
        prompt = build_prompt(
            "implement", sample_task, sample_project, sample_stage_run, artifacts
        )
        assert "## Specification" in prompt
        assert "## Implementation plan" in prompt

    def test_quick_implement_no_unfilled_placeholders(
        self,
        quick_task: dict,
        sample_project: dict,
        sample_stage_run: dict,
    ) -> None:
        artifacts = {
            "spec_content": "spec body",
            "plan_content": "plan body",
            "git_diff": "diff output",
        }
        prompt = build_prompt(
            "implement", quick_task, sample_project, sample_stage_run, artifacts
        )
        assert "{" not in prompt, "Unfilled placeholder in quick implement prompt"

    def test_quick_review_no_unfilled_placeholders(
        self,
        quick_task: dict,
        sample_project: dict,
        sample_stage_run: dict,
    ) -> None:
        artifacts = {
            "spec_content": "spec body",
            "plan_content": "plan body",
            "git_diff": "diff output",
        }
        prompt = build_prompt(
            "review", quick_task, sample_project, sample_stage_run, artifacts
        )
        assert "{" not in prompt, "Unfilled placeholder in quick review prompt"

    def test_quick_implement_includes_retry_context(
        self,
        quick_task: dict,
        sample_project: dict,
    ) -> None:
        stage_run = {"attempt": 2}
        artifacts = {"previous_gate_stderr": "tests failed: 3 errors"}
        prompt = build_prompt(
            "implement", quick_task, sample_project, stage_run, artifacts
        )
        assert "attempt 2" in prompt
        assert "tests failed: 3 errors" in prompt

    def test_quick_implement_includes_review_feedback(
        self,
        quick_task: dict,
        sample_project: dict,
        sample_stage_run: dict,
    ) -> None:
        artifacts = {"review_feedback": "- Button still blue in dark mode"}
        prompt = build_prompt(
            "implement", quick_task, sample_project, sample_stage_run, artifacts
        )
        assert "## Review feedback" in prompt
        assert "- Button still blue in dark mode" in prompt

    def test_quick_implement_includes_skill_references(
        self,
        sample_project: dict,
        sample_stage_run: dict,
        empty_artifacts: dict,
    ) -> None:
        task = {
            "id": "qf-002",
            "title": "T",
            "description": "D",
            "branch_name": "b",
            "skill_overrides": ["CUSTOM.md"],
            "flow": "quick",
        }
        prompt = build_prompt(
            "implement", task, sample_project, sample_stage_run, empty_artifacts
        )
        assert "CUSTOM.md" in prompt

    def test_default_flow_falls_back_to_standard(
        self,
        sample_project: dict,
        sample_stage_run: dict,
    ) -> None:
        task = {
            "id": "t1",
            "title": "T",
            "description": "D",
            "branch_name": "b",
            "skill_overrides": None,
        }
        artifacts = {"spec_content": "Spec.", "plan_content": "Plan."}
        prompt = build_prompt(
            "implement", task, sample_project, sample_stage_run, artifacts
        )
        assert "## Specification" in prompt


# ---------------------------------------------------------------------------
# build_prompt — epic-flow templates
# ---------------------------------------------------------------------------


class TestBuildPromptEpicFlow:
    @pytest.fixture()
    def epic_task(self) -> dict:
        return {
            "id": "epic-001",
            "title": "Overhaul authentication system",
            "description": "Replace the legacy auth with OAuth2.",
            "branch_name": "forge/epic-001-overhaul-auth",
            "spec_path": "",
            "plan_path": "",
            "skill_overrides": None,
            "flow": "epic",
        }

    def test_epic_spec_uses_epic_template(
        self,
        epic_task: dict,
        sample_project: dict,
        sample_stage_run: dict,
        empty_artifacts: dict,
    ) -> None:
        """AC 1: Epic tasks use a dedicated spec-stage prompt."""
        prompt = build_prompt(
            "spec", epic_task, sample_project, sample_stage_run, empty_artifacts
        )
        assert "decompose" in prompt.lower()
        assert "epic-decompositions" in prompt
        assert "JSON" in prompt
        # Should NOT contain standard spec template text
        assert "Acceptance criteria" not in prompt
        assert "Out of scope" not in prompt

    def test_epic_spec_includes_task_id_in_path(
        self,
        epic_task: dict,
        sample_project: dict,
        sample_stage_run: dict,
        empty_artifacts: dict,
    ) -> None:
        """AC 2: Prompt directs agent to write JSON to path with task ID."""
        prompt = build_prompt(
            "spec", epic_task, sample_project, sample_stage_run, empty_artifacts
        )
        assert f"_forge/epic-decompositions/{epic_task['id']}.json" in prompt

    def test_epic_spec_retry_context(
        self,
        epic_task: dict,
        sample_project: dict,
    ) -> None:
        """AC 3: Retry context appears in the epic prompt."""
        stage_run = {"attempt": 2}
        artifacts = {"previous_gate_stderr": "decomposition file missing"}
        prompt = build_prompt(
            "spec", epic_task, sample_project, stage_run, artifacts
        )
        assert "attempt 2" in prompt
        assert "decomposition file missing" in prompt

    def test_non_epic_spec_unchanged(
        self,
        sample_task: dict,
        sample_project: dict,
        sample_stage_run: dict,
        empty_artifacts: dict,
    ) -> None:
        """AC 4: Standard flow still uses original SPEC_TEMPLATE."""
        prompt = build_prompt(
            "spec", sample_task, sample_project, sample_stage_run, empty_artifacts
        )
        assert "Acceptance criteria" in prompt
        assert "Out of scope" in prompt
        assert "decompose" not in prompt.lower()

    def test_epic_spec_no_unfilled_placeholders(
        self,
        epic_task: dict,
        sample_project: dict,
        sample_stage_run: dict,
        empty_artifacts: dict,
    ) -> None:
        prompt = build_prompt(
            "spec", epic_task, sample_project, sample_stage_run, empty_artifacts
        )
        # Check no Python format placeholders remain (but JSON braces are ok)
        import re
        unfilled = re.findall(r"\{[a-z_]+\}", prompt)
        assert not unfilled, f"Unfilled placeholders: {unfilled}"

    def test_epic_review_template_in_epic_stage_templates(self) -> None:
        """EPIC_STAGE_TEMPLATES includes review mapped to EPIC_REVIEW_TEMPLATE."""
        assert "review" in EPIC_STAGE_TEMPLATES
        assert EPIC_STAGE_TEMPLATES["review"] is EPIC_REVIEW_TEMPLATE

    def test_epic_review_prompt_content(
        self,
        epic_task: dict,
        sample_project: dict,
        sample_stage_run: dict,
    ) -> None:
        """Epic review prompt includes title, description, decomposition, and verdict instructions."""
        artifacts = {
            "spec_content": '[{"title": "Child task A"}]',
            "git_diff": "diff --git a/foo.py b/foo.py",
        }
        prompt = build_prompt(
            "review", epic_task, sample_project, sample_stage_run, artifacts
        )
        # Contains epic title and description
        assert "Overhaul authentication system" in prompt
        assert "Replace the legacy auth with OAuth2." in prompt
        # Contains decomposition content
        assert "Child task A" in prompt
        # Contains verdict instructions
        assert "PASS" in prompt
        assert "ISSUES" in prompt
        # Contains review file path instruction
        assert f"_forge/reviews/{epic_task['id']}.md" in prompt
        # Contains follow-ups file instruction
        assert f"_forge/follow-ups/{epic_task['id']}.json" in prompt
