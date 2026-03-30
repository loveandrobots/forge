"""Tests for reset_repo_state — uses real temporary git repos, no mocking."""

from __future__ import annotations

import asyncio
import os
import subprocess

import pytest

from forge import database as db
from forge.config import Settings
from forge.engine import PipelineEngine, reset_repo_state


def _run_git(repo: str, *args: str) -> str:
    """Run a git command in repo, return stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _git_status(repo: str) -> str:
    """Return `git status --porcelain` output (empty == clean)."""
    return _run_git(repo, "status", "--porcelain")


def _init_repo(tmp_path) -> str:
    """Create a git repo with one commit on 'main' and return its path."""
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    _run_git(repo, "init", "-b", "main")
    _run_git(repo, "config", "user.email", "test@test.com")
    _run_git(repo, "config", "user.name", "Test")
    # Initial commit
    filepath = os.path.join(repo, "README.md")
    with open(filepath, "w") as f:
        f.write("# hello\n")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "Initial commit")
    return repo


# ---- AC9 a: cleanup after uncommitted changes (modified tracked files) ----

@pytest.mark.asyncio
async def test_cleanup_modified_tracked_files(tmp_path):
    repo = _init_repo(tmp_path)
    # Modify a tracked file without committing
    with open(os.path.join(repo, "README.md"), "w") as f:
        f.write("dirty\n")
    assert _git_status(repo) != ""

    result = await reset_repo_state(repo, "main")

    assert result["success"] is True
    assert _git_status(repo) == ""


# ---- AC9 b: cleanup after an in-progress rebase ----

@pytest.mark.asyncio
async def test_cleanup_in_progress_rebase(tmp_path):
    repo = _init_repo(tmp_path)

    # Create a branch with a conflicting change
    _run_git(repo, "checkout", "-b", "feature")
    with open(os.path.join(repo, "README.md"), "w") as f:
        f.write("feature content\n")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "feature change")

    # Create a conflicting change on main
    _run_git(repo, "checkout", "main")
    with open(os.path.join(repo, "README.md"), "w") as f:
        f.write("main content\n")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "main change")

    # Start a rebase that will conflict
    _run_git(repo, "checkout", "feature")
    try:
        _run_git(repo, "rebase", "main")
    except subprocess.CalledProcessError:
        pass  # Expected conflict

    # Verify we're in a rebase state
    assert os.path.isdir(os.path.join(repo, ".git", "rebase-merge")) or \
           os.path.isdir(os.path.join(repo, ".git", "rebase-apply"))

    result = await reset_repo_state(repo, "main")

    assert result["success"] is True
    assert _git_status(repo) == ""
    # Should be on main
    branch = _run_git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    assert branch == "main"


# ---- AC9 c: cleanup after untracked files ----

@pytest.mark.asyncio
async def test_cleanup_untracked_files(tmp_path):
    repo = _init_repo(tmp_path)

    # Create untracked files
    with open(os.path.join(repo, "junk.txt"), "w") as f:
        f.write("junk\n")
    os.makedirs(os.path.join(repo, "subdir"))
    with open(os.path.join(repo, "subdir", "more_junk.txt"), "w") as f:
        f.write("more junk\n")
    assert _git_status(repo) != ""

    result = await reset_repo_state(repo, "main")

    assert result["success"] is True
    assert _git_status(repo) == ""
    assert not os.path.exists(os.path.join(repo, "junk.txt"))
    assert not os.path.exists(os.path.join(repo, "subdir"))


# ---- AC9 d: cleanup failure escalating to needs_human ----

@pytest.mark.asyncio
async def test_cleanup_failure_returns_error(tmp_path):
    """reset_repo_state returns failure if a required command fails."""
    repo = _init_repo(tmp_path)

    # Try to checkout a non-existent branch -> should fail
    result = await reset_repo_state(repo, "nonexistent-branch")

    assert result["success"] is False
    assert "nonexistent-branch" in result["output"]


@pytest.mark.asyncio
async def test_cleanup_failure_marks_task_needs_human(tmp_path):
    """When _reset_and_log fails, the task is marked needs_human."""
    repo = _init_repo(tmp_path)
    db_path = str(tmp_path / "test.db")
    conn = db.get_connection(db_path)
    db.migrate(conn)

    project_id = db.insert_project(
        conn,
        name="TestProject",
        repo_path=repo,
        gate_dir=os.path.join(repo, "gates"),
        default_branch="main",
    )
    task_id = db.insert_task(conn, project_id=project_id, title="Test task")
    db.update_task(conn, task_id, status="active", current_stage="implement")

    settings = Settings()
    engine = PipelineEngine(settings, db_path)

    # Call _reset_and_log with a nonexistent branch to trigger failure
    ok = await engine._reset_and_log(repo, "nonexistent-branch", conn, task_id)

    assert ok is False
    task = db.get_task(conn, task_id)
    assert task["status"] == "needs_human"
    conn.close()


# ---- AC9 e: next task starts with clean working directory after previous failure ----

@pytest.mark.asyncio
async def test_next_task_clean_after_previous_failure(tmp_path):
    """After a failure leaves dirty state, reset_repo_state cleans it up."""
    repo = _init_repo(tmp_path)

    # Simulate a failed stage leaving dirty state
    with open(os.path.join(repo, "README.md"), "w") as f:
        f.write("dirty from failed stage\n")
    with open(os.path.join(repo, "leftover.txt"), "w") as f:
        f.write("leftover\n")
    assert _git_status(repo) != ""

    # The pre-dispatch reset should clean everything
    result = await reset_repo_state(repo, "main")
    assert result["success"] is True
    assert _git_status(repo) == ""


# ---- AC1/AC2: structured result and command logging ----

@pytest.mark.asyncio
async def test_result_contains_command_output(tmp_path):
    """The result dict includes command logs."""
    repo = _init_repo(tmp_path)

    result = await reset_repo_state(repo, "main")

    assert result["success"] is True
    assert "output" in result
    # Should log the git commands that were run
    assert "git" in result["output"]
    assert "rebase --abort" in result["output"]
    assert "checkout" in result["output"]


# ---- AC8: uses asyncio subprocess ----

@pytest.mark.asyncio
async def test_reset_is_async(tmp_path):
    """Verify reset_repo_state is a coroutine (async)."""
    repo = _init_repo(tmp_path)
    coro = reset_repo_state(repo, "main")
    assert asyncio.iscoroutine(coro)
    result = await coro
    assert result["success"] is True


# ---- Combined dirty state: modified files + untracked files ----

@pytest.mark.asyncio
async def test_cleanup_combined_dirty_state(tmp_path):
    repo = _init_repo(tmp_path)

    # Both modified tracked and untracked
    with open(os.path.join(repo, "README.md"), "w") as f:
        f.write("modified\n")
    with open(os.path.join(repo, "new_file.py"), "w") as f:
        f.write("new\n")

    # Also be on a different branch
    _run_git(repo, "checkout", "-b", "some-feature")

    result = await reset_repo_state(repo, "main")
    assert result["success"] is True
    assert _git_status(repo) == ""
    branch = _run_git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    assert branch == "main"
