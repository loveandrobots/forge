"""Tests for forge.gate_runner."""

from __future__ import annotations

import asyncio
import os
import stat
import textwrap

import pytest

from forge.gate_runner import GateResult, build_gate_env, run_gate


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
        )
        stage_run = _FakeRow(stage="spec", attempt=1)
        project = _FakeRow(repo_path="/tmp/repo")

        env = build_gate_env(task, stage_run, project)

        assert env["FORGE_BRANCH"] == ""
        assert env["FORGE_SPEC_PATH"] == ""
        assert env["FORGE_PLAN_PATH"] == ""
        assert env["FORGE_REVIEW_PATH"] == ""


# ---------------------------------------------------------------------------
# run_gate
# ---------------------------------------------------------------------------

class TestRunGate:
    def test_passing_gate(self, tmp_path: object) -> None:
        gate_dir = str(tmp_path)
        _write_gate_script(gate_dir, "spec", """\
            #!/bin/bash
            echo "all good"
            exit 0
        """)
        env = _make_env(gate_dir, stage="spec")

        result = asyncio.run(run_gate(gate_dir, "spec", env))

        assert isinstance(result, GateResult)
        assert result.passed is True
        assert result.exit_code == 0
        assert "all good" in result.stdout
        assert result.stderr == ""
        assert result.gate_name == "post-spec.sh"
        assert result.duration_seconds >= 0

    def test_failing_gate(self, tmp_path: object) -> None:
        gate_dir = str(tmp_path)
        _write_gate_script(gate_dir, "plan", """\
            #!/bin/bash
            echo "info output"
            echo "missing required section" >&2
            exit 1
        """)
        env = _make_env(gate_dir, stage="plan")

        result = asyncio.run(run_gate(gate_dir, "plan", env))

        assert result.passed is False
        assert result.exit_code == 1
        assert "info output" in result.stdout
        assert "missing required section" in result.stderr
        assert result.gate_name == "post-plan.sh"

    def test_missing_gate_passes_by_default(self, tmp_path: object) -> None:
        gate_dir = str(tmp_path)
        env = _make_env(gate_dir, stage="review")

        result = asyncio.run(run_gate(gate_dir, "review", env))

        assert result.passed is True
        assert result.exit_code == 0
        assert result.gate_name == "post-review.sh"
        assert result.duration_seconds == 0.0

    def test_env_vars_available_in_script(self, tmp_path: object) -> None:
        gate_dir = str(tmp_path)
        _write_gate_script(gate_dir, "implement", """\
            #!/bin/bash
            echo "TASK=$FORGE_TASK_ID"
            echo "STAGE=$FORGE_STAGE"
            echo "ATTEMPT=$FORGE_ATTEMPT"
            echo "REPO=$FORGE_REPO_PATH"
            echo "BRANCH=$FORGE_BRANCH"
            exit 0
        """)
        env = _make_env(gate_dir, stage="implement", attempt=3)
        env["FORGE_BRANCH"] = "forge/my-branch"

        result = asyncio.run(run_gate(gate_dir, "implement", env))

        assert result.passed is True
        assert "TASK=test-task-id" in result.stdout
        assert "STAGE=implement" in result.stdout
        assert "ATTEMPT=3" in result.stdout
        assert "BRANCH=forge/my-branch" in result.stdout

    def test_nonzero_exit_code_other_than_one(self, tmp_path: object) -> None:
        gate_dir = str(tmp_path)
        _write_gate_script(gate_dir, "spec", """\
            #!/bin/bash
            echo "crashed" >&2
            exit 2
        """)
        env = _make_env(gate_dir, stage="spec")

        result = asyncio.run(run_gate(gate_dir, "spec", env))

        assert result.passed is False
        assert result.exit_code == 2
        assert "crashed" in result.stderr
