"""Tests for auto-merge feature branches after successful review."""

from __future__ import annotations

import os
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.dispatcher import (
    checkout_and_pull,
    delete_branch,
    ff_merge,
    rebase_branch,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def git_repo(tmp_path):
    """Create a minimal git repo with one commit on main and a feature branch."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_COMMITTER_NAME": "Test",
           "GIT_AUTHOR_EMAIL": "t@t.com", "GIT_COMMITTER_EMAIL": "t@t.com"}

    def run(*args):
        return subprocess.run(args, cwd=repo, check=True, capture_output=True, env=env)

    run("git", "init")
    run("git", "config", "user.email", "t@t.com")
    run("git", "config", "user.name", "Test")
    (repo / "README.md").write_text("init")
    run("git", "add", ".")
    run("git", "commit", "-m", "init")
    run("git", "branch", "-M", "main")
    return str(repo)


def _run(repo, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_COMMITTER_NAME": "Test",
           "GIT_AUTHOR_EMAIL": "t@t.com", "GIT_COMMITTER_EMAIL": "t@t.com"}
    return subprocess.run(args, cwd=repo, check=True, capture_output=True, text=True, env=env)


def _make_feature_branch(repo, branch="feature", file="feature.txt", content="feature"):
    """Create a feature branch with one commit, then switch back to main."""
    _run(repo, "git", "checkout", "-b", branch)
    with open(os.path.join(repo, file), "w") as f:
        f.write(content)
    _run(repo, "git", "add", ".")
    _run(repo, "git", "commit", "-m", f"add {file}")
    _run(repo, "git", "checkout", "main")


# ---------------------------------------------------------------------------
# Test 1: Clean merge with no conflicts (AC 2, 6, 7, 8)
# ---------------------------------------------------------------------------


class TestCleanMerge:
    @pytest.mark.asyncio
    async def test_full_merge_sequence(self, git_repo):
        _make_feature_branch(git_repo, "feature", "feature.txt", "hello")

        # checkout default, rebase feature onto main, checkout default, ff merge, delete
        assert (await checkout_and_pull(git_repo, "main")).success is True
        assert (await rebase_branch(git_repo, "feature", "main")).success is True
        assert (await checkout_and_pull(git_repo, "main")).success is True
        assert (await ff_merge(git_repo, "feature")).success is True
        assert (await delete_branch(git_repo, "feature")).success is True

        # Feature file should be on main
        assert os.path.exists(os.path.join(git_repo, "feature.txt"))

        # Feature branch should be deleted
        result = subprocess.run(
            ["git", "branch", "--list", "feature"],
            cwd=git_repo, capture_output=True, text=True,
        )
        assert result.stdout.strip() == ""

        # Should be on main
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=git_repo, capture_output=True, text=True,
        )
        assert result.stdout.strip() == "main"


# ---------------------------------------------------------------------------
# Test 2: Rebase with conflicts triggers needs_human (AC 3)
# ---------------------------------------------------------------------------


class TestRebaseConflict:
    @pytest.mark.asyncio
    async def test_rebase_conflict_returns_false(self, git_repo):
        # Create feature branch modifying README
        _run(git_repo, "git", "checkout", "-b", "feature")
        with open(os.path.join(git_repo, "README.md"), "w") as f:
            f.write("feature version")
        _run(git_repo, "git", "add", ".")
        _run(git_repo, "git", "commit", "-m", "feature change")
        _run(git_repo, "git", "checkout", "main")

        # Modify same file on main
        with open(os.path.join(git_repo, "README.md"), "w") as f:
            f.write("main conflicting version")
        _run(git_repo, "git", "add", ".")
        _run(git_repo, "git", "commit", "-m", "main conflict")

        # Rebase should fail
        result = await rebase_branch(git_repo, "feature", "main")
        assert result.success is False

        # Repo should be clean (rebase aborted)
        assert not os.path.exists(os.path.join(git_repo, ".git", "rebase-merge"))


# ---------------------------------------------------------------------------
# Test 3: Post-rebase gate failure triggers needs_human (AC 4, 5)
# ---------------------------------------------------------------------------


class TestPostRebaseGateFailure:
    @pytest.mark.asyncio
    async def test_gate_failure_sets_needs_human(self, git_repo):
        """Simulate the _auto_merge flow with a failing gate."""
        from forge.engine import PipelineEngine
        from forge.config import Settings

        _make_feature_branch(git_repo, "forge/feat", "feat.txt", "content")

        # Create a gate that fails
        gate_dir = os.path.join(git_repo, "gates")
        os.makedirs(gate_dir, exist_ok=True)
        gate_script = os.path.join(gate_dir, "post-implement.sh")
        with open(gate_script, "w") as f:
            f.write("#!/bin/bash\necho 'lint error' >&2\nexit 1\n")
        os.chmod(gate_script, 0o755)

        # Mock database calls
        mock_conn = MagicMock()

        task = {
            "id": "task-123",
            "branch_name": "forge/feat",
            "project_id": "proj-1",
            "spec_path": "",
            "plan_path": "",
            "review_path": "",
        }
        project = {
            "id": "proj-1",
            "name": "Test",
            "repo_path": git_repo,
            "default_branch": "main",
            "gate_dir": "gates",
        }

        # Mock stage_run for build_gate_env
        mock_stage_run = {
            "stage": "implement",
            "attempt": 1,
            "status": "passed",
        }

        settings = Settings()
        engine = PipelineEngine(settings, ":memory:")

        with patch("forge.engine.database") as mock_db, \
             patch.object(engine, "_log"):
            mock_db.list_stage_runs.return_value = [MagicMock(**{
                "__getitem__": lambda self, key: mock_stage_run[key],
            })]
            mock_db.get_project.return_value = MagicMock(**{
                "__getitem__": lambda self, key: project[key],
            })
            mock_db.get_task.return_value = MagicMock(**{
                "__getitem__": lambda self, key: task[key],
            })

            result = await engine._auto_merge(mock_conn, task, project)

        assert result is False
        mock_db.update_task.assert_called_once_with(
            mock_conn, "task-123", status="needs_human"
        )


# ---------------------------------------------------------------------------
# Test 4: Branch is deleted after successful merge (AC 7)
# ---------------------------------------------------------------------------


class TestBranchDeletion:
    @pytest.mark.asyncio
    async def test_delete_branch_removes_it(self, git_repo):
        _make_feature_branch(git_repo, "to-delete", "file.txt", "data")

        # Merge first (can't delete checked-out branch)
        assert (await ff_merge(git_repo, "to-delete")).success is True
        assert (await delete_branch(git_repo, "to-delete")).success is True

        result = subprocess.run(
            ["git", "branch", "--list", "to-delete"],
            cwd=git_repo, capture_output=True, text=True,
        )
        assert result.stdout.strip() == ""


# ---------------------------------------------------------------------------
# Test 5: Merge is skipped for non-done tasks (AC 9)
# ---------------------------------------------------------------------------


class TestMergeSkippedForNonDone:
    @pytest.mark.asyncio
    async def test_advance_to_next_stage_no_merge(self):
        """When there's a next stage, _auto_merge should not be called."""
        from forge.engine import PipelineEngine
        from forge.config import Settings

        settings = Settings()
        engine = PipelineEngine(settings, ":memory:")

        mock_conn = MagicMock()
        project = {"name": "Test"}

        with patch("forge.engine.database") as mock_db, \
             patch("forge.engine._next_stage", return_value="review"), \
             patch.object(engine, "_auto_merge", new_callable=AsyncMock) as mock_merge, \
             patch.object(engine, "_log"):
            await engine.advance_task(mock_conn, "task-1", "implement", project=project)

        mock_merge.assert_not_called()
        mock_db.update_task.assert_called_once()
        # Should have advanced to next stage, not set to done
        call_kwargs = mock_db.update_task.call_args
        assert call_kwargs[1].get("status") is None  # not set to done
        assert call_kwargs[1].get("current_stage") == "review"


# ---------------------------------------------------------------------------
# Test 6: advance_task calls merge before marking done (AC 1, 10)
# ---------------------------------------------------------------------------


class TestAdvanceTaskCallOrder:
    @pytest.mark.asyncio
    async def test_merge_before_done(self):
        """_auto_merge is called before update_task(status=done)."""
        from forge.engine import PipelineEngine
        from forge.config import Settings

        settings = Settings()
        engine = PipelineEngine(settings, ":memory:")

        call_order = []

        async def fake_merge(conn, task, project):
            call_order.append("merge")
            return True

        async def fake_pause(conn, task_id, project):
            call_order.append("pause")

        mock_conn = MagicMock()
        project = {"name": "Test"}

        mock_task_row = MagicMock()
        mock_task_row.__getitem__ = lambda self, key: {
            "id": "task-1", "branch_name": "forge/feat"
        }.get(key)
        mock_task_row.__bool__ = lambda self: True

        with patch("forge.engine.database") as mock_db, \
             patch("forge.engine._next_stage", return_value=None), \
             patch.object(engine, "_auto_merge", side_effect=fake_merge), \
             patch.object(engine, "_maybe_auto_pause", side_effect=fake_pause), \
             patch.object(engine, "_log"):
            mock_db.get_task.return_value = mock_task_row
            await engine.advance_task(mock_conn, "task-1", "review", project=project)

        assert call_order == ["merge", "pause"]
        # update_task should be called with status=done
        mock_db.update_task.assert_called_once()
        assert mock_db.update_task.call_args[1]["status"] == "done"


# ---------------------------------------------------------------------------
# Test 7: Git operation failure (non-conflict) sets needs_human (AC 12)
# ---------------------------------------------------------------------------


class TestGitOperationFailure:
    @pytest.mark.asyncio
    async def test_checkout_nonexistent_dir_fails(self, tmp_path):
        bad_path = str(tmp_path / "nonexistent")
        result = await checkout_and_pull(bad_path, "main")
        assert result.success is False

    @pytest.mark.asyncio
    async def test_ff_merge_nonexistent_branch(self, git_repo):
        result = await ff_merge(git_repo, "no-such-branch")
        assert result.success is False


# ---------------------------------------------------------------------------
# Test 8: Task without branch_name skips merge
# ---------------------------------------------------------------------------


class TestNoBranchNameSkipsMerge:
    @pytest.mark.asyncio
    async def test_advance_no_branch_marks_done(self):
        from forge.engine import PipelineEngine
        from forge.config import Settings

        settings = Settings()
        engine = PipelineEngine(settings, ":memory:")

        mock_conn = MagicMock()
        project = {"name": "Test"}

        mock_task_row = MagicMock()
        mock_task_row.__getitem__ = lambda self, key: {
            "id": "task-1", "branch_name": None
        }.get(key)
        mock_task_row.__bool__ = lambda self: True

        with patch("forge.engine.database") as mock_db, \
             patch("forge.engine._next_stage", return_value=None), \
             patch.object(engine, "_auto_merge", new_callable=AsyncMock) as mock_merge, \
             patch.object(engine, "_maybe_auto_pause", new_callable=AsyncMock), \
             patch.object(engine, "_log"):
            mock_db.get_task.return_value = mock_task_row
            await engine.advance_task(mock_conn, "task-1", "review", project=project)

        # _auto_merge should NOT be called (branch_name is None)
        mock_merge.assert_not_called()
        # Task should still be marked done
        mock_db.update_task.assert_called_once()
        assert mock_db.update_task.call_args[1]["status"] == "done"


# ---------------------------------------------------------------------------
# Test 9: checkout_and_pull tolerates missing remote (robustness)
# ---------------------------------------------------------------------------


class TestCheckoutAndPullNoRemote:
    @pytest.mark.asyncio
    async def test_local_only_repo_succeeds(self, git_repo):
        """checkout_and_pull returns True even without a remote (pull fails but is tolerated)."""
        result = await checkout_and_pull(git_repo, "main")
        assert result.success is True
