"""Tests for forge.dispatcher."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.dispatcher import (
    DispatchResult,
    GitResult,
    checkout_and_pull,
    create_branch,
    delete_branch,
    dispatch_claude,
    ff_merge,
    parse_json_output,
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

    def test_json_array_result_message(self):
        """--verbose mode emits a JSON array; result message is found in it."""
        data = json.dumps([
            {"type": "system", "data": "init"},
            {
                "type": "result",
                "result": "array output",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        ])
        text, tokens = parse_stream_json(data)
        assert text == "array output"
        assert tokens == 15

    def test_json_array_result_overrides_assistant_text(self):
        """Array format: result message text takes precedence over assistant."""
        data = json.dumps([
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "hello from array"}],
                    "usage": {"input_tokens": 20, "output_tokens": 8},
                },
            },
            {
                "type": "result",
                "result": "final",
                "usage": {"input_tokens": 20, "output_tokens": 8},
            },
        ])
        text, tokens = parse_stream_json(data)
        assert text == "final"
        assert tokens == 28

    def test_json_array_no_result_falls_back_to_assistant(self):
        """If no result message, assistant message text is returned."""
        data = json.dumps([
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "assistant text"}],
                    "usage": {"input_tokens": 5, "output_tokens": 3},
                },
            },
        ])
        text, tokens = parse_stream_json(data)
        assert text == "assistant text"
        assert tokens == 8


# ---------------------------------------------------------------------------
# parse_json_output tests
# ---------------------------------------------------------------------------


class TestParseJsonOutput:
    def test_valid_json_with_all_fields(self):
        """Parses a complete JSON output with result, structured_output, and usage."""
        raw = json.dumps({
            "result": "Task completed",
            "structured_output": {"spec_path": "_forge/specs/abc.md"},
            "usage": {"input_tokens": 100, "output_tokens": 50},
        })
        parsed = parse_json_output(raw)
        assert parsed["result"] == "Task completed"
        assert parsed["structured_output"] == {"spec_path": "_forge/specs/abc.md"}
        assert parsed["tokens"] == 150

    def test_missing_structured_output(self):
        """Returns None for structured_output when not present."""
        raw = json.dumps({
            "result": "Done",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        })
        parsed = parse_json_output(raw)
        assert parsed["result"] == "Done"
        assert parsed["structured_output"] is None
        assert parsed["tokens"] == 15

    def test_missing_usage(self):
        """Returns None for tokens when usage not present."""
        raw = json.dumps({
            "result": "Done",
            "structured_output": {"verdict": "PASS"},
        })
        parsed = parse_json_output(raw)
        assert parsed["tokens"] is None
        assert parsed["structured_output"] == {"verdict": "PASS"}

    def test_invalid_json_returns_raw(self):
        """Returns raw text as result when JSON is invalid."""
        raw = "not valid json"
        parsed = parse_json_output(raw)
        assert parsed["result"] == raw
        assert parsed["structured_output"] is None
        assert parsed["tokens"] is None

    def test_json_array_no_result_item_returns_raw(self):
        """Returns raw text when JSON array has no type='result' item."""
        raw = json.dumps([{"type": "assistant", "result": "something"}])
        parsed = parse_json_output(raw)
        assert parsed["result"] == raw
        assert parsed["structured_output"] is None
        assert parsed["tokens"] is None

    def test_json_array_verbose_mode_extracts_structured_output(self):
        """Verbose mode: extracts structured_output from type='result' array item."""
        spec = {
            "overview": "Adds widgets.",
            "acceptance_criteria": [{"id": 1, "text": "Widget renders"}],
            "out_of_scope": [],
            "dependencies": [],
            "content": "Full spec.",
        }
        raw = json.dumps([
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "thinking"}]}},
            {
                "type": "result",
                "result": "done",
                "structured_output": spec,
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
        ])
        parsed = parse_json_output(raw)
        assert parsed["result"] == "done"
        assert parsed["structured_output"] == spec
        assert parsed["tokens"] == 150

    def test_json_array_result_item_no_structured_output(self):
        """Array with type='result' item but no structured_output returns None."""
        raw = json.dumps([
            {"type": "result", "result": "plain text", "usage": {"input_tokens": 10, "output_tokens": 5}},
        ])
        parsed = parse_json_output(raw)
        assert parsed["result"] == "plain text"
        assert parsed["structured_output"] is None
        assert parsed["tokens"] == 15

    def test_empty_string(self):
        """Handles empty string input."""
        parsed = parse_json_output("")
        assert parsed["result"] == ""
        assert parsed["structured_output"] is None
        assert parsed["tokens"] is None

    def test_structured_output_with_nested_data(self):
        """Handles complex nested structured output."""
        raw = json.dumps({
            "result": "Review done",
            "structured_output": {
                "verdict": "ISSUES",
                "issues": ["Missing test", "Bad naming"],
                "follow_ups": [{"title": "Fix typo", "flow": "quick"}],
            },
            "usage": {"input_tokens": 200, "output_tokens": 100},
        })
        parsed = parse_json_output(raw)
        assert parsed["structured_output"]["verdict"] == "ISSUES"
        assert len(parsed["structured_output"]["issues"]) == 2
        assert parsed["tokens"] == 300


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
    async def test_create_branch_success(self, git_repo):
        result = await create_branch(git_repo, "forge/test-branch", "main")
        assert isinstance(result, GitResult)
        assert result.success is True
        assert result.returncode == 0
        # Verify we're on the new branch
        proc = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=git_repo,
            capture_output=True,
            text=True,
        )
        assert proc.stdout.strip() == "forge/test-branch"

    async def test_create_branch_bad_base(self, git_repo):
        result = await create_branch(git_repo, "forge/x", "nonexistent-base")
        assert result.success is False
        assert result.returncode != 0
        assert result.stderr  # should contain git error text

    async def test_create_branch_already_exists(self, git_repo):
        await create_branch(git_repo, "forge/dup", "main")
        # Go back to main
        subprocess.run(["git", "checkout", "main"], cwd=git_repo, capture_output=True)
        # Creating the same branch again should fail
        result = await create_branch(git_repo, "forge/dup", "main")
        assert result.success is False


class TestRebaseBranch:
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
        assert isinstance(result, GitResult)
        assert result.success is True

    async def test_rebase_conflict_returns_failure_with_stderr(self, git_repo):
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
        # Rebase should fail with conflict info in stderr
        result = await rebase_branch(git_repo, "forge/conflict", "main")
        assert result.success is False
        assert result.stderr  # should contain conflict-related text
        assert result.returncode != 0

    async def test_rebase_nonexistent_branch(self, git_repo):
        result = await rebase_branch(git_repo, "no-such-branch", "main")
        assert result.success is False
        assert result.stderr  # should have error text

    async def test_rebase_with_dirty_index_staged_deletion(self, git_repo):
        """Rebase succeeds even when the feature branch has staged deletions."""
        # Create feature branch with a commit
        await create_branch(git_repo, "forge/dirty-idx", "main")
        with open(os.path.join(git_repo, "feature_file.txt"), "w") as f:
            f.write("feature work")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "feature commit"],
            cwd=git_repo,
            capture_output=True,
        )

        # Stage a deletion without committing (simulates timed-out session)
        subprocess.run(
            ["git", "rm", "README.md"], cwd=git_repo, capture_output=True
        )
        # Verify index is dirty
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=git_repo,
            capture_output=True,
            text=True,
        )
        assert "D" in status.stdout

        # Switch to main and add a commit so rebase has work to do
        subprocess.run(
            ["git", "checkout", "main"], cwd=git_repo, capture_output=True
        )
        with open(os.path.join(git_repo, "main_new.txt"), "w") as f:
            f.write("main progress")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "main progress"],
            cwd=git_repo,
            capture_output=True,
        )

        # Rebase should succeed despite the dirty index on the feature branch
        result = await rebase_branch(git_repo, "forge/dirty-idx", "main")
        assert result.success is True


class TestCheckoutAndPull:
    async def test_checkout_and_pull_success(self, git_repo):
        result = await checkout_and_pull(git_repo, "main")
        assert isinstance(result, GitResult)
        assert result.success is True

    async def test_checkout_and_pull_bad_branch(self, git_repo):
        result = await checkout_and_pull(git_repo, "nonexistent-branch")
        assert result.success is False
        assert result.stderr


class TestFfMerge:
    async def test_ff_merge_success(self, git_repo):
        # Create a branch with a commit, then ff-merge back
        await create_branch(git_repo, "forge/ff-test", "main")
        with open(os.path.join(git_repo, "ff_file.txt"), "w") as f:
            f.write("ff content")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "ff commit"],
            cwd=git_repo,
            capture_output=True,
        )
        subprocess.run(["git", "checkout", "main"], cwd=git_repo, capture_output=True)
        result = await ff_merge(git_repo, "forge/ff-test")
        assert isinstance(result, GitResult)
        assert result.success is True

    async def test_ff_merge_not_ff(self, git_repo):
        # Create diverged branches
        await create_branch(git_repo, "forge/diverge", "main")
        with open(os.path.join(git_repo, "diverge.txt"), "w") as f:
            f.write("branch content")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "branch commit"],
            cwd=git_repo,
            capture_output=True,
        )
        subprocess.run(["git", "checkout", "main"], cwd=git_repo, capture_output=True)
        with open(os.path.join(git_repo, "main_only.txt"), "w") as f:
            f.write("main content")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "main commit"],
            cwd=git_repo,
            capture_output=True,
        )
        result = await ff_merge(git_repo, "forge/diverge")
        assert result.success is False
        assert result.stderr


class TestDeleteBranch:
    async def test_delete_branch_success(self, git_repo):
        await create_branch(git_repo, "forge/to-delete", "main")
        subprocess.run(["git", "checkout", "main"], cwd=git_repo, capture_output=True)
        result = await delete_branch(git_repo, "forge/to-delete")
        assert isinstance(result, GitResult)
        assert result.success is True

    async def test_delete_branch_nonexistent(self, git_repo):
        result = await delete_branch(git_repo, "no-such-branch")
        assert result.success is False


# ---------------------------------------------------------------------------
# dispatch_claude tests (mocked subprocess)
# ---------------------------------------------------------------------------


class TestDispatchClaude:
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
                mock_proc.stdout = AsyncMock()
                mock_proc.stdout.read = AsyncMock(return_value=b"")
                mock_proc.stderr = AsyncMock()
                mock_proc.stderr.read = AsyncMock(return_value=b"")
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

    async def test_timeout_preserves_partial_output(self, git_repo):
        """Partial stdout buffered before timeout is captured in result.output."""
        partial_lines = b'{"type":"system","session_id":"abc"}\n{"type":"assistant","message":{"content":[{"type":"text","text":"Working..."}]}}\n'

        async def mock_create_subprocess_exec(*args, **kwargs):
            mock_proc = AsyncMock()
            cmd = args[0] if args else ""
            if cmd == "claude":
                async def slow_communicate():
                    await asyncio.sleep(10)
                    return (b"", b"")

                mock_proc.communicate = slow_communicate
                mock_proc.kill = MagicMock()
                mock_proc.wait = AsyncMock()
                mock_proc.returncode = -9
                mock_proc.stdout = AsyncMock()
                mock_proc.stdout.read = AsyncMock(return_value=partial_lines)
                mock_proc.stderr = AsyncMock()
                mock_proc.stderr.read = AsyncMock(return_value=b"")
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
        assert result.output == partial_lines.decode()

    async def test_timeout_stderr_appended_to_error(self, git_repo):
        """stderr captured after timeout is appended to the error message."""

        async def mock_create_subprocess_exec(*args, **kwargs):
            mock_proc = AsyncMock()
            cmd = args[0] if args else ""
            if cmd == "claude":
                async def slow_communicate():
                    await asyncio.sleep(10)
                    return (b"", b"")

                mock_proc.communicate = slow_communicate
                mock_proc.kill = MagicMock()
                mock_proc.wait = AsyncMock()
                mock_proc.returncode = -9
                mock_proc.stdout = AsyncMock()
                mock_proc.stdout.read = AsyncMock(return_value=b"")
                mock_proc.stderr = AsyncMock()
                mock_proc.stderr.read = AsyncMock(return_value=b"rate limit exceeded")
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
        assert "rate limit exceeded" in result.error

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


class TestDispatchClaudeJsonSchema:
    async def test_dispatch_with_json_schema(self, git_repo):
        """dispatch_claude passes --output-format json --json-schema when schema provided."""
        schema = '{"type": "object", "properties": {"verdict": {"type": "string"}}}'
        json_response = json.dumps({
            "result": "Review complete",
            "structured_output": {"verdict": "PASS"},
            "usage": {"input_tokens": 50, "output_tokens": 25},
        })
        captured_args: list[list] = []

        async def mock_create_subprocess_exec(*args, **kwargs):
            mock_proc = AsyncMock()
            cmd = args[0] if args else ""
            if cmd == "claude":
                captured_args.append(list(args))
                mock_proc.communicate = AsyncMock(
                    return_value=(json_response.encode(), b"")
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
                prompt="Review this",
                repo_path=git_repo,
                branch="main",
                timeout=60,
                json_schema=schema,
            )

        assert result.exit_code == 0
        assert result.output == "Review complete"
        assert result.structured_output == {"verdict": "PASS"}
        assert result.tokens_used == 75
        # Verify CLI flags included json schema args
        claude_args = captured_args[0]
        assert "--output-format" in claude_args
        fmt_idx = claude_args.index("--output-format")
        assert claude_args[fmt_idx + 1] == "json"
        assert "--json-schema" in claude_args

    async def test_dispatch_without_json_schema_uses_stream_json(self, git_repo):
        """dispatch_claude defaults to stream-json when no json_schema provided."""
        result_json = json.dumps({
            "type": "result",
            "result": "Done",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        })
        captured_args: list[list] = []

        async def mock_create_subprocess_exec(*args, **kwargs):
            mock_proc = AsyncMock()
            cmd = args[0] if args else ""
            if cmd == "claude":
                captured_args.append(list(args))
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
                prompt="Do something",
                repo_path=git_repo,
                branch="main",
                timeout=60,
            )

        assert result.exit_code == 0
        assert result.structured_output is None
        # Verify stream-json is used
        claude_args = captured_args[0]
        assert "--output-format" in claude_args
        fmt_idx = claude_args.index("--output-format")
        assert claude_args[fmt_idx + 1] == "stream-json"


class TestDispatchResult:
    def test_dataclass_defaults(self):
        r = DispatchResult(output="hi", exit_code=0, duration_seconds=1.0)
        assert r.tokens_used is None
        assert r.error is None
        assert r.structured_output is None

    def test_dataclass_with_all_fields(self):
        r = DispatchResult(
            output="out",
            exit_code=0,
            duration_seconds=2.5,
            tokens_used=100,
            error=None,
            structured_output={"verdict": "PASS"},
        )
        assert r.output == "out"
        assert r.duration_seconds == 2.5
        assert r.tokens_used == 100
        assert r.structured_output == {"verdict": "PASS"}


# ---------------------------------------------------------------------------
# AC 3: Rebase abort failure preserves original error
# ---------------------------------------------------------------------------


class TestRebaseAbortFailure:
    async def test_rebase_abort_failure_preserves_original_error(self, git_repo):
        """AC 3: When rebase conflicts and abort also fails, stderr has both messages."""
        # Create a conflict scenario
        await create_branch(git_repo, "forge/abort-test", "main")
        with open(os.path.join(git_repo, "README.md"), "w") as f:
            f.write("feature change for abort test")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "feature abort-test"],
            cwd=git_repo,
            capture_output=True,
        )
        subprocess.run(["git", "checkout", "main"], cwd=git_repo, capture_output=True)
        with open(os.path.join(git_repo, "README.md"), "w") as f:
            f.write("main conflicting change for abort test")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "main conflict abort-test"],
            cwd=git_repo,
            capture_output=True,
        )

        # Mock the abort subprocess to also fail
        original_create_subprocess_exec = asyncio.create_subprocess_exec

        call_count = 0

        async def mock_create_subprocess_exec(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # The fourth call is the rebase --abort (1: checkout, 2: reset, 3: rebase, 4: abort)
            if call_count == 4 and args[1:3] == ("rebase", "--abort"):
                mock_proc = MagicMock()
                mock_proc.communicate = AsyncMock(
                    return_value=(b"", b"abort failed: lock held")
                )
                mock_proc.returncode = 1
                return mock_proc
            return await original_create_subprocess_exec(*args, **kwargs)

        with patch("forge.dispatcher.asyncio.create_subprocess_exec", side_effect=mock_create_subprocess_exec):
            result = await rebase_branch(git_repo, "forge/abort-test", "main")

        assert result.success is False
        assert "(abort also failed:" in result.stderr
        assert "abort failed: lock held" in result.stderr


# ---------------------------------------------------------------------------
# dispatch_claude: git checkout timeout and pid_callback
# ---------------------------------------------------------------------------


class TestDispatchClaudeCheckoutTimeout:
    async def test_checkout_timeout_returns_error(self, git_repo):
        """If git checkout hangs, dispatch_claude returns a timeout DispatchResult."""

        async def _hanging_wait():
            await asyncio.sleep(999)

        original_exec = asyncio.create_subprocess_exec

        async def mock_exec(*args, **kwargs):
            if args[0] == "git" and "checkout" in args:
                mock_proc = MagicMock()
                mock_proc.wait = _hanging_wait
                mock_proc.kill = MagicMock()
                # wait() after kill must be a no-op coroutine
                mock_proc.wait = AsyncMock(side_effect=[asyncio.TimeoutError(), None])
                return mock_proc
            return await original_exec(*args, **kwargs)

        with (
            patch("forge.dispatcher.asyncio.create_subprocess_exec", side_effect=mock_exec),
            patch("forge.dispatcher._GIT_CHECKOUT_TIMEOUT", 0.01),
        ):
            result = await dispatch_claude(
                prompt="test",
                repo_path=git_repo,
                branch="main",
                timeout=900,
            )

        assert result.error is not None
        assert "timed out" in result.error.lower()
        assert result.exit_code == 1

    async def test_checkout_branch_create_timeout_returns_error(self, git_repo):
        """If 'git checkout -b' hangs, dispatch_claude returns a timeout DispatchResult."""
        call_count = 0
        original_exec = asyncio.create_subprocess_exec

        async def mock_exec(*args, **kwargs):
            nonlocal call_count
            if args[0] == "git" and "checkout" in args:
                call_count += 1
                if call_count == 1:
                    # First checkout fails (branch doesn't exist yet)
                    mock_proc = MagicMock()
                    mock_proc.wait = AsyncMock(return_value=None)
                    mock_proc.returncode = 1
                    return mock_proc
                else:
                    # Second checkout (-b) hangs
                    mock_proc = MagicMock()
                    mock_proc.wait = AsyncMock(side_effect=[asyncio.TimeoutError(), None])
                    mock_proc.kill = MagicMock()
                    return mock_proc
            return await original_exec(*args, **kwargs)

        with (
            patch("forge.dispatcher.asyncio.create_subprocess_exec", side_effect=mock_exec),
            patch("forge.dispatcher._GIT_CHECKOUT_TIMEOUT", 0.01),
        ):
            result = await dispatch_claude(
                prompt="test",
                repo_path=git_repo,
                branch="new-nonexistent-branch",
                timeout=900,
            )

        assert result.error is not None
        assert "timed out" in result.error.lower()
        assert result.exit_code == 1

    async def test_pid_callback_called_with_process_pid(self, git_repo):
        """pid_callback is invoked with the claude subprocess PID."""
        received_pids: list[int] = []
        call_count = 0

        async def mock_exec(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if args[0] == "git":
                # Git checkout succeeds
                mock_proc = MagicMock()
                mock_proc.wait = AsyncMock(return_value=None)
                mock_proc.returncode = 0
                return mock_proc
            else:
                # Claude process — expose a known PID
                mock_proc = MagicMock()
                mock_proc.pid = 99999
                mock_proc.communicate = AsyncMock(return_value=(b"", b""))
                mock_proc.returncode = 1
                return mock_proc

        with patch("forge.dispatcher.asyncio.create_subprocess_exec", side_effect=mock_exec):
            await dispatch_claude(
                prompt="test",
                repo_path=git_repo,
                branch="main",
                timeout=10,
                pid_callback=received_pids.append,
            )

        # pid_callback should have been called once (for the claude process, not git)
        assert 99999 in received_pids
