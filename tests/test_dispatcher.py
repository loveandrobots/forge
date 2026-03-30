"""Tests for forge.dispatcher."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import textwrap
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.dispatcher import (
    DispatchResult,
    create_branch,
    dispatch_claude,
    parse_stream_json,
    rebase_branch,
)


# ---------------------------------------------------------------------------
# parse_stream_json tests
# ---------------------------------------------------------------------------


class TestParseStreamJson:
    def test_result_message(self):
        data = json.dumps(
            {
                "type": "result",
                "result": "Hello, world!",
                "usage": {"input_tokens": 100, "output_tokens": 50},
            }
        )
        text, tokens = parse_stream_json(data)
        assert text == "Hello, world!"
        assert tokens == 150

    def test_assistant_message(self):
        data = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "Some output"}],
                    "usage": {"input_tokens": 200, "output_tokens": 100},
                },
            }
        )
        text, tokens = parse_stream_json(data)
        assert text == "Some output"
        assert tokens == 300

    def test_multi_line_stream(self):
        lines = [
            json.dumps({"type": "system", "data": "init"}),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": "partial"}],
                    },
                }
            ),
            json.dumps(
                {
                    "type": "result",
                    "result": "final answer",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                }
            ),
        ]
        text, tokens = parse_stream_json("\n".join(lines))
        assert text == "final answer"
        assert tokens == 15

    def test_empty_input(self):
        text, tokens = parse_stream_json("")
        assert text == ""
        assert tokens is None

    def test_invalid_json_lines_skipped(self):
        lines = "not json\n" + json.dumps(
            {
                "type": "result",
                "result": "ok",
            }
        )
        text, tokens = parse_stream_json(lines)
        assert text == "ok"
        assert tokens is None

    def test_no_usage_info(self):
        data = json.dumps({"type": "result", "result": "done"})
        text, tokens = parse_stream_json(data)
        assert text == "done"
        assert tokens is None


# ---------------------------------------------------------------------------
# Git helpers with real repos (tmpdir)
# ---------------------------------------------------------------------------


@pytest.fixture
def git_repo(tmp_path):
    """Create a minimal git repo with one commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    # Create initial commit on main
    (repo / "README.md").write_text("init")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "branch", "-M", "main"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return str(repo)


class TestCreateBranch:
    @pytest.mark.asyncio
    async def test_create_branch_success(self, git_repo):
        result = await create_branch(git_repo, "forge/test-branch", "main")
        assert result is True
        # Verify we're on the new branch
        proc = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=git_repo,
            capture_output=True,
            text=True,
        )
        assert proc.stdout.strip() == "forge/test-branch"

    @pytest.mark.asyncio
    async def test_create_branch_bad_base(self, git_repo):
        result = await create_branch(git_repo, "forge/x", "nonexistent-base")
        assert result is False

    @pytest.mark.asyncio
    async def test_create_branch_already_exists(self, git_repo):
        await create_branch(git_repo, "forge/dup", "main")
        # Go back to main
        subprocess.run(["git", "checkout", "main"], cwd=git_repo, capture_output=True)
        # Creating the same branch again should fail
        result = await create_branch(git_repo, "forge/dup", "main")
        assert result is False


class TestRebaseBranch:
    @pytest.mark.asyncio
    async def test_rebase_success(self, git_repo):
        # Create feature branch
        await create_branch(git_repo, "forge/feature", "main")
        subprocess.run(["git", "checkout", "main"], cwd=git_repo, capture_output=True)
        # Add a commit to main
        with open(os.path.join(git_repo, "main_file.txt"), "w") as f:
            f.write("main change")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "main change"],
            cwd=git_repo,
            capture_output=True,
        )
        # Rebase feature onto main
        result = await rebase_branch(git_repo, "forge/feature", "main")
        assert result is True

    @pytest.mark.asyncio
    async def test_rebase_conflict_returns_false(self, git_repo):
        # Create feature branch and modify a file
        await create_branch(git_repo, "forge/conflict", "main")
        with open(os.path.join(git_repo, "README.md"), "w") as f:
            f.write("feature change")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "feature"],
            cwd=git_repo,
            capture_output=True,
        )
        # Modify same file on main
        subprocess.run(["git", "checkout", "main"], cwd=git_repo, capture_output=True)
        with open(os.path.join(git_repo, "README.md"), "w") as f:
            f.write("main conflicting change")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "main conflict"],
            cwd=git_repo,
            capture_output=True,
        )
        # Rebase should fail
        result = await rebase_branch(git_repo, "forge/conflict", "main")
        assert result is False

    @pytest.mark.asyncio
    async def test_rebase_nonexistent_branch(self, git_repo):
        result = await rebase_branch(git_repo, "no-such-branch", "main")
        assert result is False


# ---------------------------------------------------------------------------
# dispatch_claude tests (mocked subprocess)
# ---------------------------------------------------------------------------


class TestDispatchClaude:
    @pytest.mark.asyncio
    async def test_successful_dispatch(self, git_repo):
        """Test successful dispatch with mocked claude CLI."""
        result_json = json.dumps(
            {
                "type": "result",
                "result": "Task completed successfully",
                "usage": {"input_tokens": 500, "output_tokens": 200},
            }
        )

        async def mock_create_subprocess_exec(*args, **kwargs):
            mock_proc = AsyncMock()
            cmd = args[0] if args else ""
            if cmd == "claude":
                mock_proc.communicate = AsyncMock(
                    return_value=(result_json.encode(), b"")
                )
                mock_proc.returncode = 0
                mock_proc.kill = MagicMock()
            elif cmd == "git":
                mock_proc.wait = AsyncMock(return_value=0)
                mock_proc.returncode = 0
                mock_proc.stdout = AsyncMock()
                mock_proc.stdout.read = AsyncMock(return_value=b"")
                mock_proc.stderr = AsyncMock()
                mock_proc.stderr.read = AsyncMock(return_value=b"")
            return mock_proc

        with patch(
            "forge.dispatcher.asyncio.create_subprocess_exec",
            side_effect=mock_create_subprocess_exec,
        ):
            result = await dispatch_claude(
                prompt="Write hello world",
                repo_path=git_repo,
                branch="main",
                timeout=60,
            )

        assert result.exit_code == 0
        assert result.output == "Task completed successfully"
        assert result.tokens_used == 700
        assert result.error is None
        assert result.duration_seconds > 0

    @pytest.mark.asyncio
    async def test_timeout_handling(self, git_repo):
        """Test that timeout kills the process and returns error."""

        async def mock_create_subprocess_exec(*args, **kwargs):
            mock_proc = AsyncMock()
            cmd = args[0] if args else ""
            if cmd == "claude":
                # Simulate a hang
                async def slow_communicate():
                    await asyncio.sleep(10)
                    return (b"", b"")

                mock_proc.communicate = slow_communicate
                mock_proc.kill = MagicMock()
                mock_proc.wait = AsyncMock()
                mock_proc.returncode = -9
            elif cmd == "git":
                mock_proc.wait = AsyncMock(return_value=0)
                mock_proc.returncode = 0
                mock_proc.stdout = AsyncMock()
                mock_proc.stdout.read = AsyncMock(return_value=b"")
                mock_proc.stderr = AsyncMock()
                mock_proc.stderr.read = AsyncMock(return_value=b"")
            return mock_proc

        with patch(
            "forge.dispatcher.asyncio.create_subprocess_exec",
            side_effect=mock_create_subprocess_exec,
        ):
            result = await dispatch_claude(
                prompt="Slow task",
                repo_path=git_repo,
                branch="main",
                timeout=1,
            )

        assert result.exit_code == -1
        assert "timed out" in result.error
        assert result.duration_seconds > 0

    @pytest.mark.asyncio
    async def test_claude_cli_not_found(self, git_repo):
        """Test error handling when claude CLI is not in PATH."""
        call_count = 0

        async def mock_create_subprocess_exec(*args, **kwargs):
            nonlocal call_count
            cmd = args[0] if args else ""
            if cmd == "claude":
                raise FileNotFoundError("claude not found")
            # git commands succeed
            mock_proc = AsyncMock()
            mock_proc.wait = AsyncMock(return_value=0)
            mock_proc.returncode = 0
            mock_proc.stdout = AsyncMock()
            mock_proc.stdout.read = AsyncMock(return_value=b"")
            mock_proc.stderr = AsyncMock()
            mock_proc.stderr.read = AsyncMock(return_value=b"")
            call_count += 1
            return mock_proc

        with patch(
            "forge.dispatcher.asyncio.create_subprocess_exec",
            side_effect=mock_create_subprocess_exec,
        ):
            result = await dispatch_claude(
                prompt="test",
                repo_path=git_repo,
                branch="main",
                timeout=60,
            )

        assert result.exit_code == 1
        assert "not found" in result.error

    @pytest.mark.asyncio
    async def test_claude_nonzero_exit(self, git_repo):
        """Test handling of non-zero exit code from claude."""

        async def mock_create_subprocess_exec(*args, **kwargs):
            mock_proc = AsyncMock()
            cmd = args[0] if args else ""
            if cmd == "claude":
                mock_proc.communicate = AsyncMock(
                    return_value=(b"", b"Some error occurred")
                )
                mock_proc.returncode = 1
            elif cmd == "git":
                mock_proc.wait = AsyncMock(return_value=0)
                mock_proc.returncode = 0
                mock_proc.stdout = AsyncMock()
                mock_proc.stdout.read = AsyncMock(return_value=b"")
                mock_proc.stderr = AsyncMock()
                mock_proc.stderr.read = AsyncMock(return_value=b"")
            return mock_proc

        with patch(
            "forge.dispatcher.asyncio.create_subprocess_exec",
            side_effect=mock_create_subprocess_exec,
        ):
            result = await dispatch_claude(
                prompt="bad task",
                repo_path=git_repo,
                branch="main",
                timeout=60,
            )

        assert result.exit_code == 1
        assert "Some error occurred" in result.error

    @pytest.mark.asyncio
    async def test_branch_checkout_and_create(self, git_repo):
        """Test that dispatch creates a branch if checkout fails."""
        git_calls = []

        async def mock_create_subprocess_exec(*args, **kwargs):
            mock_proc = AsyncMock()
            cmd = args[0] if args else ""
            if cmd == "git":
                subcmd = args[1] if len(args) > 1 else ""
                git_calls.append(list(args))
                if (
                    subcmd == "checkout"
                    and len(args) > 2
                    and args[2] == "forge/new-branch"
                ):
                    # First checkout fails (branch doesn't exist)
                    mock_proc.wait = AsyncMock(return_value=1)
                    mock_proc.returncode = 1
                    mock_proc.stdout = AsyncMock()
                    mock_proc.stdout.read = AsyncMock(return_value=b"")
                    mock_proc.stderr = AsyncMock()
                    mock_proc.stderr.read = AsyncMock(return_value=b"error: pathspec")
                elif subcmd == "checkout" and "-b" in args:
                    # Branch creation succeeds
                    mock_proc.wait = AsyncMock(return_value=0)
                    mock_proc.returncode = 0
                    mock_proc.stdout = AsyncMock()
                    mock_proc.stdout.read = AsyncMock(return_value=b"")
                    mock_proc.stderr = AsyncMock()
                    mock_proc.stderr.read = AsyncMock(return_value=b"")
                else:
                    mock_proc.wait = AsyncMock(return_value=0)
                    mock_proc.returncode = 0
                    mock_proc.stdout = AsyncMock()
                    mock_proc.stdout.read = AsyncMock(return_value=b"")
                    mock_proc.stderr = AsyncMock()
                    mock_proc.stderr.read = AsyncMock(return_value=b"")
            elif cmd == "claude":
                result_json = json.dumps(
                    {
                        "type": "result",
                        "result": "done",
                    }
                )
                mock_proc.communicate = AsyncMock(
                    return_value=(result_json.encode(), b"")
                )
                mock_proc.returncode = 0
            return mock_proc

        with patch(
            "forge.dispatcher.asyncio.create_subprocess_exec",
            side_effect=mock_create_subprocess_exec,
        ):
            result = await dispatch_claude(
                prompt="test",
                repo_path=git_repo,
                branch="forge/new-branch",
                timeout=60,
            )

        assert result.exit_code == 0
        # Should have tried checkout, then checkout -b
        checkout_calls = [c for c in git_calls if c[0] == "git" and c[1] == "checkout"]
        assert len(checkout_calls) == 2
        assert "-b" in checkout_calls[1]


class TestDispatchResult:
    def test_dataclass_defaults(self):
        r = DispatchResult(output="hi", exit_code=0, duration_seconds=1.0)
        assert r.tokens_used is None
        assert r.error is None

    def test_dataclass_with_all_fields(self):
        r = DispatchResult(
            output="out",
            exit_code=0,
            duration_seconds=2.5,
            tokens_used=100,
            error=None,
        )
        assert r.output == "out"
        assert r.duration_seconds == 2.5
        assert r.tokens_used == 100
