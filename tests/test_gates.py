"""Tests for Forge's own gate scripts in gates/."""

from __future__ import annotations

import os
import subprocess
import textwrap

import pytest

# Absolute path to the gates directory in the repo root.
GATES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "gates")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_gate(
    script_name: str,
    env_overrides: dict[str, str],
    repo_path: str,
) -> subprocess.CompletedProcess[str]:
    """Run a gate script with the given environment overrides."""
    base_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "FORGE_TASK_ID": "test-task-42",
        "FORGE_STAGE": "spec",
        "FORGE_ATTEMPT": "1",
        "FORGE_REPO_PATH": repo_path,
        "FORGE_BRANCH": "forge/test-branch",
        "FORGE_SPEC_PATH": "",
        "FORGE_PLAN_PATH": "",
        "FORGE_REVIEW_PATH": "",
    }
    base_env.update(env_overrides)
    script_path = os.path.join(GATES_DIR, script_name)
    return subprocess.run(
        ["bash", script_path],
        env=base_env,
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _write_file(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(textwrap.dedent(content))


# ---------------------------------------------------------------------------
# post-spec.sh
# ---------------------------------------------------------------------------


class TestPostSpec:
    SCRIPT = "post-spec.sh"

    def test_passes_with_valid_spec(self, tmp_path: object) -> None:
        repo = str(tmp_path)
        _write_file(
            os.path.join(repo, "_forge/specs/test-task-42.md"),
            """\
            # Spec: Widget feature

            Some introductory context about the feature that is long enough
            to pass the minimum character threshold for the gate check.

            ## Acceptance criteria

            - The widget renders correctly
            - The widget handles edge cases

            ## Out of scope

            - Performance optimization
            - Mobile support
            """,
        )
        result = _run_gate(self.SCRIPT, {}, repo)
        assert result.returncode == 0
        assert "passed" in result.stdout

    def test_fails_when_spec_missing(self, tmp_path: object) -> None:
        repo = str(tmp_path)
        result = _run_gate(self.SCRIPT, {}, repo)
        assert result.returncode == 1
        assert "not found" in result.stderr

    def test_fails_when_spec_too_short(self, tmp_path: object) -> None:
        repo = str(tmp_path)
        _write_file(
            os.path.join(repo, "_forge/specs/test-task-42.md"),
            "# Short spec\nToo brief.",
        )
        result = _run_gate(self.SCRIPT, {}, repo)
        assert result.returncode == 1
        assert "too short" in result.stderr

    def test_fails_when_acceptance_criteria_missing(self, tmp_path: object) -> None:
        repo = str(tmp_path)
        content = "x" * 250 + "\n## Out of scope\n- Nothing\n"
        _write_file(
            os.path.join(repo, "_forge/specs/test-task-42.md"),
            content,
        )
        result = _run_gate(self.SCRIPT, {}, repo)
        assert result.returncode == 1
        assert "Acceptance criteria" in result.stderr

    def test_fails_when_out_of_scope_missing(self, tmp_path: object) -> None:
        repo = str(tmp_path)
        content = "x" * 250 + "\n## Acceptance criteria\n- Something\n"
        _write_file(
            os.path.join(repo, "_forge/specs/test-task-42.md"),
            content,
        )
        result = _run_gate(self.SCRIPT, {}, repo)
        assert result.returncode == 1
        assert "Out of scope" in result.stderr


# ---------------------------------------------------------------------------
# post-plan.sh
# ---------------------------------------------------------------------------


class TestPostPlan:
    SCRIPT = "post-plan.sh"

    def test_passes_with_valid_plan(self, tmp_path: object) -> None:
        repo = str(tmp_path)
        _write_file(
            os.path.join(repo, "_forge/plans/test-task-42.md"),
            """\
            # Plan: Widget feature

            This plan addresses the acceptance criteria from the spec.

            ## Files to create

            - src/widget.py
            - tests/test_widget.py

            ## Test plan

            - test that the widget renders correctly
            - test edge case handling
            """,
        )
        result = _run_gate(self.SCRIPT, {"FORGE_STAGE": "plan"}, repo)
        assert result.returncode == 0
        assert "passed" in result.stdout

    def test_fails_when_plan_missing(self, tmp_path: object) -> None:
        repo = str(tmp_path)
        result = _run_gate(self.SCRIPT, {"FORGE_STAGE": "plan"}, repo)
        assert result.returncode == 1
        assert "not found" in result.stderr

    def test_fails_when_plan_too_short(self, tmp_path: object) -> None:
        repo = str(tmp_path)
        _write_file(
            os.path.join(repo, "_forge/plans/test-task-42.md"),
            "# Short plan\nNot enough.",
        )
        result = _run_gate(self.SCRIPT, {"FORGE_STAGE": "plan"}, repo)
        assert result.returncode == 1
        assert "too short" in result.stderr

    def test_fails_without_acceptance_criteria_reference(
        self, tmp_path: object
    ) -> None:
        repo = str(tmp_path)
        _write_file(
            os.path.join(repo, "_forge/plans/test-task-42.md"),
            "x" * 250 + "\n## Files to create\n- foo.py\n## Tests\n- test something\n",
        )
        result = _run_gate(self.SCRIPT, {"FORGE_STAGE": "plan"}, repo)
        assert result.returncode == 1
        assert "acceptance criteria" in result.stderr.lower()

    def test_fails_without_test_descriptions(self, tmp_path: object) -> None:
        repo = str(tmp_path)
        _write_file(
            os.path.join(repo, "_forge/plans/test-task-42.md"),
            "x" * 250
            + "\nacceptance criteria reference\n## Files to create\n- foo.py\n",
        )
        result = _run_gate(self.SCRIPT, {"FORGE_STAGE": "plan"}, repo)
        assert result.returncode == 1
        assert "test" in result.stderr.lower()

    def test_fails_without_files_to_create(self, tmp_path: object) -> None:
        repo = str(tmp_path)
        _write_file(
            os.path.join(repo, "_forge/plans/test-task-42.md"),
            "x" * 250 + "\nacceptance criteria reference\ntest plan included\n",
        )
        result = _run_gate(self.SCRIPT, {"FORGE_STAGE": "plan"}, repo)
        assert result.returncode == 1
        assert "files" in result.stderr.lower()


# ---------------------------------------------------------------------------
# post-implement.sh
# ---------------------------------------------------------------------------


class TestPostImplement:
    SCRIPT = "post-implement.sh"

    def test_passes_when_tests_and_lint_pass(self, tmp_path: object) -> None:
        repo = str(tmp_path)
        # Create minimal Python package and test that passes
        _write_file(os.path.join(repo, "forge/__init__.py"), "")
        _write_file(
            os.path.join(repo, "tests/test_ok.py"),
            "def test_ok():\n    assert True\n",
        )
        # Create a pyproject.toml so ruff doesn't complain about config
        _write_file(os.path.join(repo, "pyproject.toml"), "[tool.ruff]\n")
        result = _run_gate(
            self.SCRIPT,
            {"FORGE_STAGE": "implement"},
            repo,
        )
        assert result.returncode == 0
        assert "passed" in result.stdout

    def test_fails_when_tests_fail(self, tmp_path: object) -> None:
        repo = str(tmp_path)
        _write_file(os.path.join(repo, "forge/__init__.py"), "")
        _write_file(
            os.path.join(repo, "tests/test_fail.py"),
            "def test_fail():\n    assert False\n",
        )
        _write_file(os.path.join(repo, "pyproject.toml"), "[tool.ruff]\n")
        result = _run_gate(
            self.SCRIPT,
            {"FORGE_STAGE": "implement"},
            repo,
        )
        assert result.returncode == 1
        assert "Tests failed" in result.stderr

    def test_fails_when_lint_fails(self, tmp_path: object) -> None:
        repo = str(tmp_path)
        _write_file(
            os.path.join(repo, "forge/__init__.py"),
            "import os\nimport sys\n",  # unused imports
        )
        _write_file(
            os.path.join(repo, "tests/test_ok.py"),
            "def test_ok():\n    assert True\n",
        )
        _write_file(os.path.join(repo, "pyproject.toml"), "[tool.ruff]\n")
        result = _run_gate(
            self.SCRIPT,
            {"FORGE_STAGE": "implement"},
            repo,
        )
        assert result.returncode == 1
        assert "Lint errors" in result.stderr


# ---------------------------------------------------------------------------
# post-review.sh
# ---------------------------------------------------------------------------


class TestPostReview:
    SCRIPT = "post-review.sh"

    def test_passes_with_pass_verdict(self, tmp_path: object) -> None:
        repo = str(tmp_path)
        _write_file(
            os.path.join(repo, "_forge/reviews/test-task-42.md"),
            """\
            # Review: Widget feature

            ## Verdict: PASS

            All acceptance criteria met. Code is clean and well-tested.
            """,
        )
        result = _run_gate(self.SCRIPT, {"FORGE_STAGE": "review"}, repo)
        assert result.returncode == 0
        assert "passed" in result.stdout

    def test_passes_with_issues_verdict_and_actionable_items(
        self, tmp_path: object
    ) -> None:
        repo = str(tmp_path)
        _write_file(
            os.path.join(repo, "_forge/reviews/test-task-42.md"),
            """\
            # Review: Widget feature

            ## Verdict: ISSUES

            - Fix error handling in widget.py line 42
            - Add test for empty input case
            """,
        )
        result = _run_gate(self.SCRIPT, {"FORGE_STAGE": "review"}, repo)
        assert result.returncode == 0
        assert "passed" in result.stdout

    def test_fails_when_review_missing(self, tmp_path: object) -> None:
        repo = str(tmp_path)
        result = _run_gate(self.SCRIPT, {"FORGE_STAGE": "review"}, repo)
        assert result.returncode == 1
        assert "not found" in result.stderr

    def test_fails_when_no_verdict(self, tmp_path: object) -> None:
        repo = str(tmp_path)
        _write_file(
            os.path.join(repo, "_forge/reviews/test-task-42.md"),
            "# Review\n\nSome notes about the code.\n",
        )
        result = _run_gate(self.SCRIPT, {"FORGE_STAGE": "review"}, repo)
        assert result.returncode == 1
        assert "verdict" in result.stderr.lower()

    def test_fails_with_issues_but_no_actionable_items(self, tmp_path: object) -> None:
        repo = str(tmp_path)
        _write_file(
            os.path.join(repo, "_forge/reviews/test-task-42.md"),
            """\
            # Review

            ## Verdict: ISSUES

            There are some problems but I won't say what they are.
            """,
        )
        result = _run_gate(self.SCRIPT, {"FORGE_STAGE": "review"}, repo)
        assert result.returncode == 1
        assert "actionable" in result.stderr.lower()
