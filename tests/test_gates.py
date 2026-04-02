"""Tests for Forge's own gate scripts in gates/."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import textwrap
from pathlib import Path


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

    def test_passes_with_valid_spec(self, tmp_path: Path) -> None:
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

    def test_fails_when_spec_missing(self, tmp_path: Path) -> None:
        repo = str(tmp_path)
        result = _run_gate(self.SCRIPT, {}, repo)
        assert result.returncode == 1
        assert "not found" in result.stderr

    def test_fails_when_spec_too_short(self, tmp_path: Path) -> None:
        repo = str(tmp_path)
        _write_file(
            os.path.join(repo, "_forge/specs/test-task-42.md"),
            "# Short spec\nToo brief.",
        )
        result = _run_gate(self.SCRIPT, {}, repo)
        assert result.returncode == 1
        assert "too short" in result.stderr

    def test_fails_when_acceptance_criteria_missing(self, tmp_path: Path) -> None:
        repo = str(tmp_path)
        content = "x" * 250 + "\n## Out of scope\n- Nothing\n"
        _write_file(
            os.path.join(repo, "_forge/specs/test-task-42.md"),
            content,
        )
        result = _run_gate(self.SCRIPT, {}, repo)
        assert result.returncode == 1
        assert "Acceptance criteria" in result.stderr

    def test_fails_when_out_of_scope_missing(self, tmp_path: Path) -> None:
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

    def test_passes_with_valid_plan(self, tmp_path: Path) -> None:
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

    def test_fails_when_plan_missing(self, tmp_path: Path) -> None:
        repo = str(tmp_path)
        result = _run_gate(self.SCRIPT, {"FORGE_STAGE": "plan"}, repo)
        assert result.returncode == 1
        assert "not found" in result.stderr

    def test_fails_when_plan_too_short(self, tmp_path: Path) -> None:
        repo = str(tmp_path)
        _write_file(
            os.path.join(repo, "_forge/plans/test-task-42.md"),
            "# Short plan\nNot enough.",
        )
        result = _run_gate(self.SCRIPT, {"FORGE_STAGE": "plan"}, repo)
        assert result.returncode == 1
        assert "too short" in result.stderr

    def test_fails_without_acceptance_criteria_reference(
        self, tmp_path: Path
    ) -> None:
        repo = str(tmp_path)
        _write_file(
            os.path.join(repo, "_forge/plans/test-task-42.md"),
            "x" * 250 + "\n## Files to create\n- foo.py\n## Tests\n- test something\n",
        )
        result = _run_gate(self.SCRIPT, {"FORGE_STAGE": "plan"}, repo)
        assert result.returncode == 1
        assert "acceptance criteria" in result.stderr.lower()

    def test_fails_without_test_descriptions(self, tmp_path: Path) -> None:
        repo = str(tmp_path)
        _write_file(
            os.path.join(repo, "_forge/plans/test-task-42.md"),
            "x" * 250
            + "\nacceptance criteria reference\n## Files to create\n- foo.py\n",
        )
        result = _run_gate(self.SCRIPT, {"FORGE_STAGE": "plan"}, repo)
        assert result.returncode == 1
        assert "test" in result.stderr.lower()

    def test_fails_without_files_to_create(self, tmp_path: Path) -> None:
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

    def test_passes_when_tests_and_lint_pass(self, tmp_path: Path) -> None:
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

    def test_fails_when_tests_fail(self, tmp_path: Path) -> None:
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

    def test_fails_when_lint_fails(self, tmp_path: Path) -> None:
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

    def test_passes_with_pass_verdict(self, tmp_path: Path) -> None:
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

    def test_fails_with_issues_verdict_and_actionable_items(
        self, tmp_path: Path
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
        assert result.returncode == 1
        assert "ISSUES" in result.stderr
        assert "actionable" in result.stderr.lower()

    def test_fails_when_review_missing(self, tmp_path: Path) -> None:
        repo = str(tmp_path)
        result = _run_gate(self.SCRIPT, {"FORGE_STAGE": "review"}, repo)
        assert result.returncode == 1
        assert "not found" in result.stderr

    def test_fails_when_no_verdict(self, tmp_path: Path) -> None:
        repo = str(tmp_path)
        _write_file(
            os.path.join(repo, "_forge/reviews/test-task-42.md"),
            "# Review\n\nSome notes about the code.\n",
        )
        result = _run_gate(self.SCRIPT, {"FORGE_STAGE": "review"}, repo)
        assert result.returncode == 1
        assert "verdict" in result.stderr.lower()

    def test_fails_with_issues_but_no_actionable_items(self, tmp_path: Path) -> None:
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

    def test_passes_with_pass_verdict_no_issues_mentioned(
        self, tmp_path: Path
    ) -> None:
        """A PASS-only review with no mention of ISSUES exits 0."""
        repo = str(tmp_path)
        _write_file(
            os.path.join(repo, "_forge/reviews/test-task-42.md"),
            """\
            # Review: Widget feature

            ## Verdict: PASS

            Everything looks good. All acceptance criteria met.
            Code is clean and well-tested.
            """,
        )
        result = _run_gate(self.SCRIPT, {"FORGE_STAGE": "review"}, repo)
        assert result.returncode == 0
        assert "passed" in result.stdout

    def test_passes_with_pass_verdict_containing_issues_word(
        self, tmp_path: Path
    ) -> None:
        """A PASS verdict line that also contains the word 'issues' exits 0."""
        repo = str(tmp_path)
        _write_file(
            os.path.join(repo, "_forge/reviews/test-task-42.md"),
            """\
            # Review: Widget feature

            ## Verdict: PASS (no issues found)

            Everything looks good. All acceptance criteria met.
            """,
        )
        result = _run_gate(self.SCRIPT, {"FORGE_STAGE": "review"}, repo)
        assert result.returncode == 0
        assert "passed" in result.stdout

    def test_fails_with_issues_verdict_stderr_message(
        self, tmp_path: Path
    ) -> None:
        """Stderr includes human-readable message with ISSUES and Bouncing."""
        repo = str(tmp_path)
        _write_file(
            os.path.join(repo, "_forge/reviews/test-task-42.md"),
            """\
            # Review: Widget feature

            ## Verdict: ISSUES

            - Fix error handling in widget.py line 42
            - Add test for empty input case
            - Missing docstring on public API
            """,
        )
        result = _run_gate(self.SCRIPT, {"FORGE_STAGE": "review"}, repo)
        assert result.returncode == 1
        assert "ISSUES" in result.stderr
        assert "Bouncing" in result.stderr
        assert "actionable item(s)" in result.stderr

    def test_passes_with_multiline_pass_verdict(self, tmp_path: Path) -> None:
        """Verdict heading on one line, PASS value on the next non-blank line."""
        repo = str(tmp_path)
        _write_file(
            os.path.join(repo, "_forge/reviews/test-task-42.md"),
            """\
            # Review: Widget feature

            ## Verdict

            **PASS**

            All acceptance criteria met.
            """,
        )
        result = _run_gate(self.SCRIPT, {"FORGE_STAGE": "review"}, repo)
        assert result.returncode == 0
        assert "passed" in result.stdout

    def test_fails_with_multiline_issues_verdict(self, tmp_path: Path) -> None:
        """Verdict heading on one line, ISSUES value on next, with actionable items."""
        repo = str(tmp_path)
        _write_file(
            os.path.join(repo, "_forge/reviews/test-task-42.md"),
            """\
            # Review: Widget feature

            ## Verdict

            **ISSUES**

            - Fix error handling in widget.py line 42
            - Add test for empty input case
            """,
        )
        result = _run_gate(self.SCRIPT, {"FORGE_STAGE": "review"}, repo)
        assert result.returncode == 1
        assert "ISSUES" in result.stderr

    def test_verdict_not_confused_by_body_text(self, tmp_path: Path) -> None:
        """Body text mentioning 'verdict' or 'ISSUES' doesn't override actual verdict."""
        repo = str(tmp_path)
        _write_file(
            os.path.join(repo, "_forge/reviews/test-task-42.md"),
            """\
            # Review: Widget feature

            ## Verdict: PASS

            All acceptance criteria met.

            | Criterion | Verdict |
            |-----------|---------|
            | Handles edge cases | verdict is ISSUES |
            | Performance | an ISSUES verdict was considered |
            """,
        )
        result = _run_gate(self.SCRIPT, {"FORGE_STAGE": "review"}, repo)
        assert result.returncode == 0
        assert "passed" in result.stdout

    def test_bold_verdict_format(self, tmp_path: Path) -> None:
        """Bold verdict with colon inside: **Verdict: PASS**."""
        repo = str(tmp_path)
        _write_file(
            os.path.join(repo, "_forge/reviews/test-task-42.md"),
            """\
            # Review: Widget feature

            **Verdict: PASS**

            All good.
            """,
        )
        result = _run_gate(self.SCRIPT, {"FORGE_STAGE": "review"}, repo)
        assert result.returncode == 0
        assert "passed" in result.stdout

    def test_bold_label_verdict_format(self, tmp_path: Path) -> None:
        """Bold label with value outside: **Verdict**: ISSUES."""
        repo = str(tmp_path)
        _write_file(
            os.path.join(repo, "_forge/reviews/test-task-42.md"),
            """\
            # Review: Widget feature

            **Verdict**: ISSUES

            - Fix the widget rendering
            - Add missing tests
            """,
        )
        result = _run_gate(self.SCRIPT, {"FORGE_STAGE": "review"}, repo)
        assert result.returncode == 1
        assert "ISSUES" in result.stderr

    def test_fails_with_unrecognized_verdict_keyword(self, tmp_path: Path) -> None:
        """Gate fails when review contains a verdict header with an unrecognized keyword."""
        repo = str(tmp_path)
        _write_file(
            os.path.join(repo, "_forge/reviews/test-task-42.md"),
            """\
            # Review: Widget feature

            ## Verdict: REJECTED

            The implementation does not meet requirements.
            """,
        )
        result = _run_gate(self.SCRIPT, {"FORGE_STAGE": "review"}, repo)
        assert result.returncode == 1
        assert "verdict" in result.stderr.lower()

    def test_bold_label_colon_verdict_format(self, tmp_path: Path) -> None:
        """Bold label with trailing colon: **Verdict:** PASS."""
        repo = str(tmp_path)
        _write_file(
            os.path.join(repo, "_forge/reviews/test-task-42.md"),
            """\
            # Review: Widget feature

            **Verdict:** PASS

            All good.
            """,
        )
        result = _run_gate(self.SCRIPT, {"FORGE_STAGE": "review"}, repo)
        assert result.returncode == 0
        assert "passed" in result.stdout


# ---------------------------------------------------------------------------
# parse_verdict.py (unit tests)
# ---------------------------------------------------------------------------


class TestParseVerdictScript:
    SCRIPT = os.path.join(GATES_DIR, "parse_verdict.py")

    def test_parse_verdict_script_pass(self, tmp_path: Path) -> None:
        """Script prints PASS and exits 0 for a simple verdict."""
        review = os.path.join(str(tmp_path), "review.md")
        _write_file(review, "## Verdict: PASS\n\nAll good.\n")
        result = subprocess.run(
            ["python3", self.SCRIPT, review],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "PASS"

    def test_parse_verdict_script_no_verdict(self, tmp_path: Path) -> None:
        """Script exits 1 when no verdict is found."""
        review = os.path.join(str(tmp_path), "review.md")
        _write_file(review, "# Review\n\nSome notes.\n")
        result = subprocess.run(
            ["python3", self.SCRIPT, review],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 1
        assert "verdict" in result.stderr.lower()

    def test_parse_verdict_script_unrecognized_keyword(self, tmp_path: Path) -> None:
        """Script exits 1 when verdict header has an unrecognized keyword."""
        review = os.path.join(str(tmp_path), "review.md")
        _write_file(review, "## Verdict: APPROVED\n\nAll good.\n")
        result = subprocess.run(
            ["python3", self.SCRIPT, review],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 1
        assert "verdict" in result.stderr.lower()
        assert result.stdout.strip() == ""

    def test_parse_verdict_script_missing_file(self) -> None:
        """Script exits 1 for a nonexistent file."""
        result = subprocess.run(
            ["python3", self.SCRIPT, "/tmp/nonexistent_review_file.md"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 1


# ---------------------------------------------------------------------------
# parse_verdict() direct unit tests
# ---------------------------------------------------------------------------


def _load_parse_verdict() -> object:
    """Import parse_verdict function from the gates script."""
    spec = importlib.util.spec_from_file_location(
        "parse_verdict", os.path.join(GATES_DIR, "parse_verdict.py")
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.parse_verdict  # type: ignore[attr-defined]


class TestParseVerdictUnit:
    """Direct unit tests for the parse_verdict() function."""

    parse_verdict = staticmethod(_load_parse_verdict())

    def test_returns_none_for_unrecognized_keyword_approved(self) -> None:
        assert self.parse_verdict("## Verdict: APPROVED\n\nAll good.\n") is None

    def test_returns_none_for_unrecognized_keyword_fail(self) -> None:
        assert self.parse_verdict("## Verdict: FAIL\n\nBad code.\n") is None

    def test_returns_none_for_unrecognized_keyword_rejected(self) -> None:
        assert self.parse_verdict("## Verdict: REJECTED\n\nNot acceptable.\n") is None
