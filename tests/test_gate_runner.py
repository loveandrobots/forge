"""Tests for forge.gate_runner."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
import textwrap

from forge.gate_runner import (
    GateResult,
    _parse_structured_output,
    build_gate_env,
    format_structured_bounce_context,
    run_gate,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_gate_script(gate_dir: str, stage: str, body: str) -> str:
    """Write a gate script into *gate_dir* and make it executable."""
    path = os.path.join(gate_dir, f"post-{stage}.sh")
    with open(path, "w") as f:
        f.write(textwrap.dedent(body))
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path


def _make_env(
    tmp_path: str,
    stage: str = "spec",
    attempt: int = 1,
) -> dict[str, str]:
    """Return minimal FORGE_* env vars for testing."""
    return {
        "FORGE_TASK_ID": "test-task-id",
        "FORGE_STAGE": stage,
        "FORGE_ATTEMPT": str(attempt),
        "FORGE_REPO_PATH": tmp_path,
        "FORGE_BRANCH": "forge/test-branch",
        "FORGE_SPEC_PATH": "",
        "FORGE_PLAN_PATH": "",
        "FORGE_REVIEW_PATH": "",
    }


# ---------------------------------------------------------------------------
# build_gate_env
# ---------------------------------------------------------------------------


class _FakeRow(dict):
    """Dict subclass that supports both key access and bracket access."""

    def __getitem__(self, key: str):
        return super().__getitem__(key)


class TestBuildGateEnv:
    def test_env_vars_set_correctly(self) -> None:
        task = _FakeRow(
            id="task-123",
            branch_name="forge/abc-feature",
            spec_path="_forge/specs/task-123.md",
            plan_path="_forge/plans/task-123.md",
            review_path=None,
            flow="standard",
        )
        stage_run = _FakeRow(stage="implement", attempt=2)
        project = _FakeRow(repo_path="/srv/repos/myproject")

        env = build_gate_env(task, stage_run, project)

        assert env["FORGE_TASK_ID"] == "task-123"
        assert env["FORGE_STAGE"] == "implement"
        assert env["FORGE_ATTEMPT"] == "2"
        assert env["FORGE_REPO_PATH"] == "/srv/repos/myproject"
        assert env["FORGE_BRANCH"] == "forge/abc-feature"
        assert env["FORGE_SPEC_PATH"] == "_forge/specs/task-123.md"
        assert env["FORGE_PLAN_PATH"] == "_forge/plans/task-123.md"
        assert env["FORGE_REVIEW_PATH"] == ""

    def test_none_fields_become_empty_strings(self) -> None:
        task = _FakeRow(
            id="t1",
            branch_name=None,
            spec_path=None,
            plan_path=None,
            review_path=None,
            flow="standard",
        )
        stage_run = _FakeRow(stage="spec", attempt=1)
        project = _FakeRow(repo_path="/tmp/repo")

        env = build_gate_env(task, stage_run, project)

        assert env["FORGE_BRANCH"] == ""
        assert env["FORGE_SPEC_PATH"] == ""
        assert env["FORGE_PLAN_PATH"] == ""
        assert env["FORGE_REVIEW_PATH"] == ""

    def test_artifact_path_set_when_provided(self) -> None:
        task = _FakeRow(
            id="task-456",
            branch_name="forge/abc",
            spec_path="",
            plan_path="",
            review_path="",
            flow="standard",
        )
        stage_run = _FakeRow(stage="review", attempt=1)
        project = _FakeRow(repo_path="/srv/repos/myproject")

        env = build_gate_env(
            task, stage_run, project,
            artifact_path="/tmp/artifacts/review.json",
        )

        assert env["FORGE_ARTIFACT_PATH"] == "/tmp/artifacts/review.json"

    def test_artifact_path_not_set_when_none(self) -> None:
        task = _FakeRow(
            id="task-789",
            branch_name="forge/abc",
            spec_path="",
            plan_path="",
            review_path="",
            flow="standard",
        )
        stage_run = _FakeRow(stage="spec", attempt=1)
        project = _FakeRow(repo_path="/srv/repos/myproject")

        env = build_gate_env(task, stage_run, project)

        assert "FORGE_ARTIFACT_PATH" not in env

    def test_forge_flow_included(self) -> None:
        task = _FakeRow(
            id="task-epic-1",
            branch_name="forge/epic-branch",
            spec_path="",
            plan_path="",
            review_path="",
            flow="epic",
        )
        stage_run = _FakeRow(stage="spec", attempt=1)
        project = _FakeRow(repo_path="/srv/repos/myproject")

        env = build_gate_env(task, stage_run, project)

        assert env["FORGE_FLOW"] == "epic"

    def test_forge_flow_defaults_to_standard(self) -> None:
        task = _FakeRow(
            id="task-no-flow",
            branch_name="forge/branch",
            spec_path="",
            plan_path="",
            review_path="",
        )
        stage_run = _FakeRow(stage="spec", attempt=1)
        project = _FakeRow(repo_path="/srv/repos/myproject")

        env = build_gate_env(task, stage_run, project)

        assert env["FORGE_FLOW"] == "standard"


# ---------------------------------------------------------------------------
# run_gate
# ---------------------------------------------------------------------------


class TestRunGate:
    async def test_passing_gate(self, tmp_path: Path) -> None:
        gate_dir = str(tmp_path)
        _write_gate_script(
            gate_dir,
            "spec",
            """\
            #!/bin/bash
            echo "all good"
            exit 0
        """,
        )
        env = _make_env(gate_dir, stage="spec")

        result = await run_gate(gate_dir, "spec", env)

        assert isinstance(result, GateResult)
        assert result.passed is True
        assert result.exit_code == 0
        assert "all good" in result.stdout
        assert result.stderr == ""
        assert result.gate_name == "post-spec.sh"
        assert result.duration_seconds >= 0

    async def test_failing_gate(self, tmp_path: Path) -> None:
        gate_dir = str(tmp_path)
        _write_gate_script(
            gate_dir,
            "plan",
            """\
            #!/bin/bash
            echo "info output"
            echo "missing required section" >&2
            exit 1
        """,
        )
        env = _make_env(gate_dir, stage="plan")

        result = await run_gate(gate_dir, "plan", env)

        assert result.passed is False
        assert result.exit_code == 1
        assert "info output" in result.stdout
        assert "missing required section" in result.stderr
        assert result.gate_name == "post-plan.sh"

    async def test_missing_gate_passes_by_default(self, tmp_path: Path) -> None:
        gate_dir = str(tmp_path)
        env = _make_env(gate_dir, stage="review")

        result = await run_gate(gate_dir, "review", env)

        assert result.passed is True
        assert result.exit_code == 0
        assert result.gate_name == "post-review.sh"
        assert result.duration_seconds == 0.0

    async def test_env_vars_available_in_script(self, tmp_path: Path) -> None:
        gate_dir = str(tmp_path)
        _write_gate_script(
            gate_dir,
            "implement",
            """\
            #!/bin/bash
            echo "TASK=$FORGE_TASK_ID"
            echo "STAGE=$FORGE_STAGE"
            echo "ATTEMPT=$FORGE_ATTEMPT"
            echo "REPO=$FORGE_REPO_PATH"
            echo "BRANCH=$FORGE_BRANCH"
            exit 0
        """,
        )
        env = _make_env(gate_dir, stage="implement", attempt=3)
        env["FORGE_BRANCH"] = "forge/my-branch"

        result = await run_gate(gate_dir, "implement", env)

        assert result.passed is True
        assert "TASK=test-task-id" in result.stdout
        assert "STAGE=implement" in result.stdout
        assert "ATTEMPT=3" in result.stdout
        assert "BRANCH=forge/my-branch" in result.stdout

    async def test_nonzero_exit_code_other_than_one(self, tmp_path: Path) -> None:
        gate_dir = str(tmp_path)
        _write_gate_script(
            gate_dir,
            "spec",
            """\
            #!/bin/bash
            echo "crashed" >&2
            exit 2
        """,
        )
        env = _make_env(gate_dir, stage="spec")

        result = await run_gate(gate_dir, "spec", env)

        assert result.passed is False
        assert result.exit_code == 2
        assert "crashed" in result.stderr

    async def test_structured_json_stdout_parsed(self, tmp_path: Path) -> None:
        gate_dir = str(tmp_path)
        output = json.dumps({
            "passed": False,
            "reason": "Lint failed",
            "checks": [
                {"name": "tests", "passed": True},
                {"name": "lint", "passed": False, "detail": "3 errors found"},
            ],
        })
        _write_gate_script(
            gate_dir,
            "implement",
            f"""\
            #!/bin/bash
            echo '{output}'
            exit 1
        """,
        )
        env = _make_env(gate_dir, stage="implement")

        result = await run_gate(gate_dir, "implement", env)

        assert result.passed is False
        assert result.structured_output is not None
        assert result.structured_output["passed"] is False
        assert result.structured_output["reason"] == "Lint failed"
        assert len(result.structured_output["checks"]) == 2
        assert result.structured_output["checks"][0]["name"] == "tests"
        assert result.structured_output["checks"][0]["passed"] is True
        assert result.structured_output["checks"][1]["name"] == "lint"
        assert result.structured_output["checks"][1]["passed"] is False

    async def test_non_json_stdout_no_structured_output(self, tmp_path: Path) -> None:
        gate_dir = str(tmp_path)
        _write_gate_script(
            gate_dir,
            "spec",
            """\
            #!/bin/bash
            echo "all good, plain text"
            exit 0
        """,
        )
        env = _make_env(gate_dir, stage="spec")

        result = await run_gate(gate_dir, "spec", env)

        assert result.passed is True
        assert result.structured_output is None
        assert "all good, plain text" in result.stdout

    async def test_structured_output_passing_gate(self, tmp_path: Path) -> None:
        gate_dir = str(tmp_path)
        output = json.dumps({
            "passed": True,
            "reason": "All checks passed",
            "checks": [
                {"name": "tests", "passed": True},
                {"name": "lint", "passed": True},
            ],
        })
        _write_gate_script(
            gate_dir,
            "implement",
            f"""\
            #!/bin/bash
            echo '{output}'
            exit 0
        """,
        )
        env = _make_env(gate_dir, stage="implement")

        result = await run_gate(gate_dir, "implement", env)

        assert result.passed is True
        assert result.structured_output is not None
        assert result.structured_output["passed"] is True

    async def test_exit_code_authoritative_over_structured_passed(
        self, tmp_path: Path
    ) -> None:
        """Exit code is authoritative — structured passed field is informational."""
        gate_dir = str(tmp_path)
        # JSON says passed=True but exit code says failure
        output = json.dumps({"passed": True, "reason": "Looks good"})
        _write_gate_script(
            gate_dir,
            "spec",
            f"""\
            #!/bin/bash
            echo '{output}'
            exit 1
        """,
        )
        env = _make_env(gate_dir, stage="spec")

        result = await run_gate(gate_dir, "spec", env)

        # Exit code wins
        assert result.passed is False
        # But structured output is still parsed
        assert result.structured_output is not None
        assert result.structured_output["passed"] is True


# ---------------------------------------------------------------------------
# _parse_structured_output
# ---------------------------------------------------------------------------


class TestParseStructuredOutput:
    def test_valid_json_with_passed(self) -> None:
        data = json.dumps({"passed": True, "reason": "ok"})
        result = _parse_structured_output(data)
        assert result is not None
        assert result["passed"] is True

    def test_valid_json_without_passed_returns_none(self) -> None:
        data = json.dumps({"reason": "ok"})
        assert _parse_structured_output(data) is None

    def test_invalid_json_returns_none(self) -> None:
        assert _parse_structured_output("not json at all") is None

    def test_empty_string_returns_none(self) -> None:
        assert _parse_structured_output("") is None

    def test_json_array_returns_none(self) -> None:
        assert _parse_structured_output("[1, 2, 3]") is None

    def test_passed_not_bool_returns_none(self) -> None:
        assert _parse_structured_output('{"passed": "yes"}') is None

    def test_full_structured_output(self) -> None:
        data = json.dumps({
            "passed": False,
            "reason": "Tests failed",
            "checks": [
                {"name": "tests", "passed": False, "detail": "2 failures"},
                {"name": "lint", "passed": True},
            ],
        })
        result = _parse_structured_output(data)
        assert result is not None
        assert result["passed"] is False
        assert len(result["checks"]) == 2


# ---------------------------------------------------------------------------
# format_structured_bounce_context
# ---------------------------------------------------------------------------


class TestFormatStructuredBounceContext:
    def test_with_checks(self) -> None:
        output = {
            "passed": False,
            "checks": [
                {"name": "tests", "passed": True},
                {"name": "lint", "passed": False, "detail": "3 errors"},
                {"name": "typecheck", "passed": True},
            ],
        }
        result = format_structured_bounce_context(output)
        assert "Gate failed:" in result
        assert "tests passed" in result
        assert "lint failed: 3 errors" in result
        assert "typecheck passed" in result

    def test_with_reason_no_checks(self) -> None:
        output = {"passed": False, "reason": "Missing required files"}
        result = format_structured_bounce_context(output)
        assert result == "Gate failed: Missing required files"

    def test_no_reason_no_checks(self) -> None:
        output = {"passed": False}
        result = format_structured_bounce_context(output)
        assert "no detail" in result

    def test_empty_checks_falls_back_to_reason(self) -> None:
        output = {"passed": False, "reason": "Something broke", "checks": []}
        result = format_structured_bounce_context(output)
        assert result == "Gate failed: Something broke"
