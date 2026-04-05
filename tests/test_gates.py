"""Tests for Forge's own gate scripts in gates/."""

from __future__ import annotations

import json
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

    def test_passes_with_pass_json_verdict(self, tmp_path: Path) -> None:
        repo = str(tmp_path)
        review = {"verdict": "PASS", "summary": "All good.", "issues": []}
        review_path = os.path.join(repo, "_forge/reviews/test-task-42.json")
        _write_file(review_path, json.dumps(review))
        result = _run_gate(
            self.SCRIPT,
            {"FORGE_STAGE": "review", "FORGE_ARTIFACT_PATH": review_path},
            repo,
        )
        assert result.returncode == 0
        assert "passed" in result.stdout

    def test_fails_with_issues_verdict_and_nonempty_issues(
        self, tmp_path: Path
    ) -> None:
        repo = str(tmp_path)
        review = {
            "verdict": "ISSUES",
            "summary": "Problems found.",
            "issues": [
                {"description": "Fix error handling in widget.py line 42"},
                {"description": "Add test for empty input case"},
            ],
        }
        review_path = os.path.join(repo, "_forge/reviews/test-task-42.json")
        _write_file(review_path, json.dumps(review))
        result = _run_gate(
            self.SCRIPT,
            {"FORGE_STAGE": "review", "FORGE_ARTIFACT_PATH": review_path},
            repo,
        )
        assert result.returncode == 1
        assert "ISSUES" in result.stderr
        assert "Bouncing" in result.stderr

    def test_fails_with_issues_verdict_but_empty_issues(
        self, tmp_path: Path
    ) -> None:
        repo = str(tmp_path)
        review = {"verdict": "ISSUES", "summary": "Problems.", "issues": []}
        review_path = os.path.join(repo, "_forge/reviews/test-task-42.json")
        _write_file(review_path, json.dumps(review))
        result = _run_gate(
            self.SCRIPT,
            {"FORGE_STAGE": "review", "FORGE_ARTIFACT_PATH": review_path},
            repo,
        )
        assert result.returncode == 1
        assert "empty issues" in result.stderr.lower()

    def test_fails_when_review_missing(self, tmp_path: Path) -> None:
        repo = str(tmp_path)
        missing = os.path.join(repo, "_forge/reviews/test-task-42.json")
        result = _run_gate(
            self.SCRIPT,
            {"FORGE_STAGE": "review", "FORGE_ARTIFACT_PATH": missing},
            repo,
        )
        assert result.returncode == 1
        assert "not found" in result.stderr

    def test_fails_with_invalid_json(self, tmp_path: Path) -> None:
        repo = str(tmp_path)
        review_path = os.path.join(repo, "_forge/reviews/test-task-42.json")
        _write_file(review_path, "NOT VALID JSON {{{")
        result = _run_gate(
            self.SCRIPT,
            {"FORGE_STAGE": "review", "FORGE_ARTIFACT_PATH": review_path},
            repo,
        )
        assert result.returncode == 1
        assert "not valid JSON" in result.stderr

    def test_fails_with_missing_verdict_field(self, tmp_path: Path) -> None:
        repo = str(tmp_path)
        review_path = os.path.join(repo, "_forge/reviews/test-task-42.json")
        _write_file(review_path, json.dumps({"summary": "no verdict here"}))
        result = _run_gate(
            self.SCRIPT,
            {"FORGE_STAGE": "review", "FORGE_ARTIFACT_PATH": review_path},
            repo,
        )
        assert result.returncode == 1
        assert "verdict" in result.stderr.lower()

    def test_fails_with_unrecognized_verdict(self, tmp_path: Path) -> None:
        repo = str(tmp_path)
        review = {"verdict": "REJECTED", "issues": []}
        review_path = os.path.join(repo, "_forge/reviews/test-task-42.json")
        _write_file(review_path, json.dumps(review))
        result = _run_gate(
            self.SCRIPT,
            {"FORGE_STAGE": "review", "FORGE_ARTIFACT_PATH": review_path},
            repo,
        )
        assert result.returncode == 1
        assert "Unrecognized verdict" in result.stderr

    def test_legacy_md_file_passes_with_warning(self, tmp_path: Path) -> None:
        """Backward compatibility: .md review file warns and exits 0."""
        repo = str(tmp_path)
        md_path = os.path.join(repo, "_forge/reviews/test-task-42.md")
        _write_file(md_path, "# Review\n\n## Verdict: PASS\n")
        result = _run_gate(
            self.SCRIPT,
            {"FORGE_STAGE": "review", "FORGE_ARTIFACT_PATH": md_path},
            repo,
        )
        assert result.returncode == 0
        assert "legacy" in result.stderr.lower() or "legacy" in result.stdout.lower()

    def test_uses_forge_review_path_fallback(self, tmp_path: Path) -> None:
        """Falls back to FORGE_REVIEW_PATH when FORGE_ARTIFACT_PATH is unset."""
        repo = str(tmp_path)
        review = {"verdict": "PASS", "summary": "OK", "issues": []}
        review_path = os.path.join(repo, "_forge/reviews/test-task-42.json")
        _write_file(review_path, json.dumps(review))
        result = _run_gate(
            self.SCRIPT,
            {"FORGE_STAGE": "review", "FORGE_REVIEW_PATH": review_path},
            repo,
        )
        assert result.returncode == 0
        assert "passed" in result.stdout


# ---------------------------------------------------------------------------
# post-epic-spec.sh
# ---------------------------------------------------------------------------


def _valid_desc(suffix: str = "A") -> str:
    """Return a self-contained description of at least 100 characters."""
    base = "Implement the %s component with full error handling, input validation, unit tests, and documentation." % suffix
    # Pad to guarantee >= 100 chars
    while len(base) < 100:
        base += " Additional detail."
    return base


def _valid_child(title: str, **overrides: object) -> dict:
    """Return a valid child task dict, optionally overriding fields."""
    child: dict = {
        "title": title,
        "description": _valid_desc(title),
    }
    child.update(overrides)
    return child


class TestPostEpicSpec:
    SCRIPT = "post-epic-spec.sh"

    def _write_decomposition(self, repo: str, data: object) -> None:
        _write_file(
            os.path.join(repo, "_forge/epic-decompositions/test-task-42.json"),
            json.dumps(data),
        )

    # -- Existing tests (updated for stricter validation) --

    def test_passes_with_valid_decomposition(self, tmp_path: Path) -> None:
        repo = str(tmp_path)
        self._write_decomposition(repo, [
            _valid_child("Child task A"),
            _valid_child("Child task B"),
        ])
        result = _run_gate(self.SCRIPT, {}, repo)
        assert result.returncode == 0
        assert "passed" in result.stdout

    def test_fails_when_file_missing(self, tmp_path: Path) -> None:
        repo = str(tmp_path)
        result = _run_gate(self.SCRIPT, {}, repo)
        assert result.returncode == 1
        assert "not found" in result.stderr

    def test_fails_with_empty_array(self, tmp_path: Path) -> None:
        repo = str(tmp_path)
        self._write_decomposition(repo, [])
        result = _run_gate(self.SCRIPT, {}, repo)
        assert result.returncode == 1
        assert "empty" in result.stderr.lower()

    def test_fails_with_missing_title(self, tmp_path: Path) -> None:
        repo = str(tmp_path)
        self._write_decomposition(repo, [
            {"description": _valid_desc()},
            _valid_child("Other"),
        ])
        result = _run_gate(self.SCRIPT, {}, repo)
        assert result.returncode == 1
        assert "title" in result.stderr.lower()

    def test_fails_with_invalid_json(self, tmp_path: Path) -> None:
        repo = str(tmp_path)
        _write_file(
            os.path.join(repo, "_forge/epic-decompositions/test-task-42.json"),
            "{not valid json",
        )
        result = _run_gate(self.SCRIPT, {}, repo)
        assert result.returncode != 0

    def test_fails_with_empty_title(self, tmp_path: Path) -> None:
        repo = str(tmp_path)
        self._write_decomposition(repo, [
            {"title": "", "description": _valid_desc()},
            _valid_child("Other"),
        ])
        result = _run_gate(self.SCRIPT, {}, repo)
        assert result.returncode == 1
        assert "title" in result.stderr.lower()

    def test_fails_with_json_object_instead_of_array(self, tmp_path: Path) -> None:
        repo = str(tmp_path)
        self._write_decomposition(repo, {"title": "Not an array"})
        result = _run_gate(self.SCRIPT, {}, repo)
        assert result.returncode == 1
        assert "array" in result.stderr.lower()

    # -- New tests: minimum child count --

    def test_fails_when_only_one_child(self, tmp_path: Path) -> None:
        repo = str(tmp_path)
        self._write_decomposition(repo, [_valid_child("Solo task")])
        result = _run_gate(self.SCRIPT, {}, repo)
        assert result.returncode == 1
        assert "at least 2" in result.stderr

    # -- New tests: description validation --

    def test_fails_when_description_missing(self, tmp_path: Path) -> None:
        repo = str(tmp_path)
        self._write_decomposition(repo, [
            _valid_child("Task A"),
            {"title": "Task B"},  # no description key
        ])
        result = _run_gate(self.SCRIPT, {}, repo)
        assert result.returncode == 1
        assert "Task B" in result.stderr
        assert "missing description" in result.stderr

    def test_fails_when_description_too_short(self, tmp_path: Path) -> None:
        repo = str(tmp_path)
        self._write_decomposition(repo, [
            _valid_child("Task A"),
            _valid_child("Task B", description="This is way too short."),
        ])
        result = _run_gate(self.SCRIPT, {}, repo)
        assert result.returncode == 1
        assert "Task B" in result.stderr
        assert "too short" in result.stderr

    def test_fails_when_description_is_whitespace_padded_under_limit(
        self, tmp_path: Path
    ) -> None:
        repo = str(tmp_path)
        # 20 real chars + lots of whitespace
        padded = "   " + "x" * 20 + "   " * 40
        self._write_decomposition(repo, [
            _valid_child("Task A"),
            _valid_child("Task B", description=padded),
        ])
        result = _run_gate(self.SCRIPT, {}, repo)
        assert result.returncode == 1
        assert "too short" in result.stderr

    # -- New tests: flow validation --

    def test_fails_with_invalid_flow(self, tmp_path: Path) -> None:
        repo = str(tmp_path)
        self._write_decomposition(repo, [
            _valid_child("Task A"),
            _valid_child("Task B", flow="epic"),
        ])
        result = _run_gate(self.SCRIPT, {}, repo)
        assert result.returncode == 1
        assert "invalid flow" in result.stderr.lower()
        assert "epic" in result.stderr

    def test_passes_with_missing_flow_defaults(self, tmp_path: Path) -> None:
        repo = str(tmp_path)
        self._write_decomposition(repo, [
            _valid_child("Task A"),
            _valid_child("Task B"),
        ])
        result = _run_gate(self.SCRIPT, {}, repo)
        assert result.returncode == 0

    def test_passes_with_valid_flow_values(self, tmp_path: Path) -> None:
        repo = str(tmp_path)
        self._write_decomposition(repo, [
            _valid_child("Task A", flow="standard"),
            _valid_child("Task B", flow="quick"),
        ])
        result = _run_gate(self.SCRIPT, {}, repo)
        assert result.returncode == 0

    # -- New tests: depends_on validation --

    def test_fails_with_dangling_depends_on_index(self, tmp_path: Path) -> None:
        repo = str(tmp_path)
        self._write_decomposition(repo, [
            _valid_child("Task A", depends_on=[5]),
            _valid_child("Task B"),
        ])
        result = _run_gate(self.SCRIPT, {}, repo)
        assert result.returncode == 1
        assert "dangling" in result.stderr.lower()

    def test_fails_with_dangling_depends_on_title(self, tmp_path: Path) -> None:
        repo = str(tmp_path)
        self._write_decomposition(repo, [
            _valid_child("Task A", depends_on=["Nonexistent task"]),
            _valid_child("Task B"),
        ])
        result = _run_gate(self.SCRIPT, {}, repo)
        assert result.returncode == 1
        assert "dangling" in result.stderr.lower()
        assert "Nonexistent task" in result.stderr

    def test_passes_with_valid_depends_on_index(self, tmp_path: Path) -> None:
        repo = str(tmp_path)
        self._write_decomposition(repo, [
            _valid_child("Task A"),
            _valid_child("Task B"),
            _valid_child("Task C", depends_on=[0]),
        ])
        result = _run_gate(self.SCRIPT, {}, repo)
        assert result.returncode == 0

    def test_passes_with_valid_depends_on_title(self, tmp_path: Path) -> None:
        repo = str(tmp_path)
        self._write_decomposition(repo, [
            _valid_child("Task A"),
            _valid_child("Task B"),
            _valid_child("Task C", depends_on=["Task A"]),
        ])
        result = _run_gate(self.SCRIPT, {}, repo)
        assert result.returncode == 0

    # -- New tests: whitespace-only title --

    def test_fails_with_whitespace_only_title(self, tmp_path: Path) -> None:
        repo = str(tmp_path)
        self._write_decomposition(repo, [
            _valid_child("Task A"),
            {"title": "   ", "description": _valid_desc()},
        ])
        result = _run_gate(self.SCRIPT, {}, repo)
        assert result.returncode == 1
        assert "title" in result.stderr.lower()

    # -- New tests: self-referential depends_on --

    def test_fails_with_self_referential_depends_on_index(self, tmp_path: Path) -> None:
        repo = str(tmp_path)
        self._write_decomposition(repo, [
            _valid_child("Task A", depends_on=[0]),
            _valid_child("Task B"),
        ])
        result = _run_gate(self.SCRIPT, {}, repo)
        assert result.returncode == 1
        assert "references itself" in result.stderr.lower()

    def test_fails_with_self_referential_depends_on_title(self, tmp_path: Path) -> None:
        repo = str(tmp_path)
        self._write_decomposition(repo, [
            _valid_child("Task A", depends_on=["Task A"]),
            _valid_child("Task B"),
        ])
        result = _run_gate(self.SCRIPT, {}, repo)
        assert result.returncode == 1
        assert "references itself" in result.stderr.lower()

    # -- New tests: depends_on type validation --

    def test_fails_with_depends_on_not_a_list(self, tmp_path: Path) -> None:
        repo = str(tmp_path)
        self._write_decomposition(repo, [
            _valid_child("Task A", depends_on="Task B"),
            _valid_child("Task B"),
        ])
        result = _run_gate(self.SCRIPT, {}, repo)
        assert result.returncode == 1
        assert "depends_on must be an array" in result.stderr

    def test_fails_with_negative_depends_on_index(self, tmp_path: Path) -> None:
        repo = str(tmp_path)
        self._write_decomposition(repo, [
            _valid_child("Task A", depends_on=[-1]),
            _valid_child("Task B"),
        ])
        result = _run_gate(self.SCRIPT, {}, repo)
        assert result.returncode == 1
        assert "dangling" in result.stderr.lower()

    def test_fails_with_depends_on_boolean_entry(self, tmp_path: Path) -> None:
        repo = str(tmp_path)
        self._write_decomposition(repo, [
            _valid_child("Task A", depends_on=[True]),
            _valid_child("Task B"),
        ])
        result = _run_gate(self.SCRIPT, {}, repo)
        assert result.returncode == 1
        assert "integer index or string title" in result.stderr

    def test_fails_with_depends_on_invalid_entry_type(self, tmp_path: Path) -> None:
        repo = str(tmp_path)
        self._write_decomposition(repo, [
            _valid_child("Task A", depends_on=[{"task": "B"}]),
            _valid_child("Task B"),
        ])
        result = _run_gate(self.SCRIPT, {}, repo)
        assert result.returncode == 1
        assert "integer index or string title" in result.stderr

    # -- New tests: cross-reference convention --

    def test_fails_when_description_references_parent_epic(
        self, tmp_path: Path
    ) -> None:
        repo = str(tmp_path)
        bad_desc = "x" * 80 + " as described in the epic we need to do this thing properly."
        self._write_decomposition(repo, [
            _valid_child("Task A"),
            _valid_child("Task B", description=bad_desc),
        ])
        result = _run_gate(self.SCRIPT, {}, repo)
        assert result.returncode == 1
        assert "cross-reference" in result.stderr.lower()

    def test_fails_when_description_says_see_above(self, tmp_path: Path) -> None:
        repo = str(tmp_path)
        bad_desc = "x" * 80 + " see task above for more details on the implementation."
        self._write_decomposition(repo, [
            _valid_child("Task A"),
            _valid_child("Task B", description=bad_desc),
        ])
        result = _run_gate(self.SCRIPT, {}, repo)
        assert result.returncode == 1
        assert "cross-reference" in result.stderr.lower()

    def test_fails_when_description_references_parent_task(
        self, tmp_path: Path
    ) -> None:
        repo = str(tmp_path)
        bad_desc = "x" * 80 + " the parent task defines the requirements for this work."
        self._write_decomposition(repo, [
            _valid_child("Task A"),
            _valid_child("Task B", description=bad_desc),
        ])
        result = _run_gate(self.SCRIPT, {}, repo)
        assert result.returncode == 1
        assert "cross-reference" in result.stderr.lower()

    def test_passes_with_no_cross_references(self, tmp_path: Path) -> None:
        repo = str(tmp_path)
        self._write_decomposition(repo, [
            _valid_child("Task A"),
            _valid_child("Task B"),
        ])
        result = _run_gate(self.SCRIPT, {}, repo)
        assert result.returncode == 0

    # -- New tests: error reporting quality --

    def test_error_messages_identify_child_index_and_title(
        self, tmp_path: Path
    ) -> None:
        repo = str(tmp_path)
        self._write_decomposition(repo, [
            _valid_child("Task A"),
            {"title": "Bad Task", "description": "Too short", "flow": "invalid"},
            _valid_child("Task C"),
        ])
        result = _run_gate(self.SCRIPT, {}, repo)
        assert result.returncode == 1
        # Should reference child 1 and its title
        assert "Child 1" in result.stderr
        assert "Bad Task" in result.stderr

    def test_accumulates_multiple_errors(self, tmp_path: Path) -> None:
        repo = str(tmp_path)
        self._write_decomposition(repo, [
            {"title": "Task A", "description": "Short"},  # too-short description
            {"title": "Task B", "flow": "epic"},  # missing desc + invalid flow
            _valid_child("Task C", depends_on=[99]),  # dangling ref
        ])
        result = _run_gate(self.SCRIPT, {}, repo)
        assert result.returncode == 1
        # Should have errors for all three children
        assert "Child 0" in result.stderr
        assert "Child 1" in result.stderr
        assert "Child 2" in result.stderr


# ---------------------------------------------------------------------------
# post-epic-review.sh
# ---------------------------------------------------------------------------


class TestPostEpicReview:
    SCRIPT = "post-epic-review.sh"

    def test_post_epic_review_gate_pass(self, tmp_path: Path) -> None:
        """PASS verdict exits 0."""
        repo = str(tmp_path)
        _write_file(
            os.path.join(repo, "_forge/reviews/test-task-42.md"),
            """\
            # Epic Review

            Everything looks good across all child tasks.

            ## Verdict: PASS
            """,
        )
        result = _run_gate(self.SCRIPT, {"FORGE_STAGE": "review"}, repo)
        assert result.returncode == 0
        assert "passed" in result.stdout

    def test_post_epic_review_gate_issues(self, tmp_path: Path) -> None:
        """ISSUES verdict with follow-ups file exits 1."""
        repo = str(tmp_path)
        _write_file(
            os.path.join(repo, "_forge/reviews/test-task-42.md"),
            """\
            # Epic Review

            Found gaps in the implementation.

            ## Verdict: ISSUES
            """,
        )
        followups_path = os.path.join(repo, "_forge/follow-ups/test-task-42.json")
        os.makedirs(os.path.dirname(followups_path), exist_ok=True)
        with open(followups_path, "w") as f:
            json.dump([{"title": "Fix gap", "description": "Address the gap"}], f)

        result = _run_gate(self.SCRIPT, {"FORGE_STAGE": "review"}, repo)
        assert result.returncode == 1
        assert "ISSUES" in result.stderr

    def test_fails_when_review_missing(self, tmp_path: Path) -> None:
        """Missing review file exits 1."""
        repo = str(tmp_path)
        result = _run_gate(self.SCRIPT, {"FORGE_STAGE": "review"}, repo)
        assert result.returncode == 1
        assert "not found" in result.stderr

    def test_fails_when_issues_without_followups(self, tmp_path: Path) -> None:
        """ISSUES verdict without follow-ups file exits 1."""
        repo = str(tmp_path)
        _write_file(
            os.path.join(repo, "_forge/reviews/test-task-42.md"),
            """\
            # Epic Review

            Found gaps in the implementation.

            ## Verdict: ISSUES
            """,
        )
        result = _run_gate(self.SCRIPT, {"FORGE_STAGE": "review"}, repo)
        assert result.returncode == 1
        assert "follow-ups" in result.stderr.lower()
