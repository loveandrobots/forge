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
    _extract_usage_tokens,
    checkout_and_pull,
    create_branch,
    delete_branch,
    dispatch_claude,
    ff_merge,
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
        text, tokens, structured = parse_stream_json(data)
        assert text == "Hello, world!"
        assert tokens == 150
        assert structured is None

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
        text, tokens, structured = parse_stream_json(data)
        assert text == "Some output"
        assert tokens == 300
        assert structured is None

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
        text, tokens, structured = parse_stream_json("\n".join(lines))
        assert text == "final answer"
        assert tokens == 15
        assert structured is None

    def test_empty_input(self):
        text, tokens, structured = parse_stream_json("")
        assert text == ""
        assert tokens is None
        assert structured is None

    def test_invalid_json_lines_skipped(self):
        lines = "not json\n" + json.dumps(
            {
                "type": "result",
                "result": "ok",
            }
        )
        text, tokens, structured = parse_stream_json(lines)
        assert text == "ok"
        assert tokens is None
        assert structured is None

    def test_no_usage_info(self):
        data = json.dumps({"type": "result", "result": "done"})
        text, tokens, structured = parse_stream_json(data)
        assert text == "done"
        assert tokens is None
        assert structured is None

    def test_json_array_result_message(self):
        """--verbose mode emits a JSON array; result message is found in it."""
        data = json.dumps(
            [
                {"type": "system", "data": "init"},
                {
                    "type": "result",
                    "result": "array output",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            ]
        )
        text, tokens, structured = parse_stream_json(data)
        assert text == "array output"
        assert tokens == 15
        assert structured is None

    def test_json_array_result_overrides_assistant_text(self):
        """Array format: result message text takes precedence over assistant."""
        data = json.dumps(
            [
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
            ]
        )
        text, tokens, structured = parse_stream_json(data)
        assert text == "final"
        assert tokens == 28
        assert structured is None

    def test_json_array_no_result_falls_back_to_assistant(self):
        """If no result message, assistant message text is returned."""
        data = json.dumps(
            [
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": "assistant text"}],
                        "usage": {"input_tokens": 5, "output_tokens": 3},
                    },
                },
            ]
        )
        text, tokens, structured = parse_stream_json(data)
        assert text == "assistant text"
        assert tokens == 8
        assert structured is None

    def test_result_with_structured_output(self):
        """structured_output in result event is returned as third tuple element."""
        data = json.dumps(
            {
                "type": "result",
                "result": "done",
                "structured_output": {"verdict": "PASS"},
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }
        )
        text, tokens, structured = parse_stream_json(data)
        assert text == "done"
        assert tokens == 15
        assert structured == {"verdict": "PASS"}


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
        subprocess.run(["git", "rm", "README.md"], cwd=git_repo, capture_output=True)
        # Verify index is dirty
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=git_repo,
            capture_output=True,
            text=True,
        )
        assert "D" in status.stdout

        # Switch to main and add a commit so rebase has work to do
        subprocess.run(["git", "checkout", "main"], cwd=git_repo, capture_output=True)
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


def _make_claude_proc_mock(
    stdout_lines: list[bytes],
    stderr: bytes = b"",
    returncode: int = 0,
    *,
    hang_wait: bool = False,
    hang_readline: bool = False,
    pid: int | None = None,
) -> MagicMock:
    """Build a mock claude process using read()/wait (incremental drain)."""
    mock_proc = MagicMock()
    mock_proc.returncode = returncode
    mock_proc.kill = MagicMock()
    if pid is not None:
        mock_proc.pid = pid

    # stdout with read() — chunks are returned one per call, then b""
    mock_proc.stdout = MagicMock()
    if hang_readline:
        # Yield chunks, then block forever (simulates slow process)
        call_count = 0

        async def read_chunk(_size: int = 65536) -> bytes:
            nonlocal call_count
            if call_count < len(stdout_lines):
                chunk = stdout_lines[call_count]
                call_count += 1
                return chunk
            await asyncio.sleep(999)
            return b""

        mock_proc.stdout.read = read_chunk
    else:
        call_count = 0

        async def read_chunk(_size: int = 65536) -> bytes:
            nonlocal call_count
            if call_count < len(stdout_lines):
                chunk = stdout_lines[call_count]
                call_count += 1
                return chunk
            return b""

        mock_proc.stdout.read = read_chunk

    # stderr
    mock_proc.stderr = MagicMock()
    mock_proc.stderr.read = AsyncMock(return_value=stderr)

    # wait
    if hang_wait:
        _killed = False

        def _kill_side_effect():
            nonlocal _killed
            _killed = True

        mock_proc.kill = MagicMock(side_effect=_kill_side_effect)

        async def hanging_wait():
            if not _killed:
                await asyncio.sleep(999)
            return returncode

        mock_proc.wait = hanging_wait
    else:
        mock_proc.wait = AsyncMock(return_value=returncode)

    return mock_proc


def _make_git_proc_mock(returncode: int = 0) -> MagicMock:
    """Build a mock git process."""
    mock_proc = MagicMock()
    mock_proc.wait = AsyncMock(return_value=returncode)
    mock_proc.returncode = returncode
    mock_proc.stdout = MagicMock()
    mock_proc.stdout.read = AsyncMock(return_value=b"")
    mock_proc.stderr = MagicMock()
    mock_proc.stderr.read = AsyncMock(return_value=b"")
    return mock_proc


class TestDispatchClaude:
    async def test_successful_dispatch(self, git_repo):
        """Test successful dispatch with mocked claude CLI."""
        result_line = json.dumps(
            {
                "type": "result",
                "result": "Task completed successfully",
                "usage": {"input_tokens": 500, "output_tokens": 200},
            }
        )

        async def mock_create_subprocess_exec(*args, **kwargs):
            cmd = args[0] if args else ""
            if cmd == "claude":
                return _make_claude_proc_mock(
                    stdout_lines=[result_line.encode() + b"\n"],
                )
            return _make_git_proc_mock()

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
            cmd = args[0] if args else ""
            if cmd == "claude":
                return _make_claude_proc_mock(
                    stdout_lines=[],
                    returncode=-9,
                    hang_wait=True,
                    hang_readline=True,
                )
            return _make_git_proc_mock()

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
        partial_line_1 = b'{"type":"system","session_id":"abc"}\n'
        partial_line_2 = b'{"type":"assistant","message":{"content":[{"type":"text","text":"Working..."}]}}\n'

        async def mock_create_subprocess_exec(*args, **kwargs):
            cmd = args[0] if args else ""
            if cmd == "claude":
                return _make_claude_proc_mock(
                    stdout_lines=[partial_line_1, partial_line_2],
                    returncode=-9,
                    hang_wait=True,
                    hang_readline=True,
                )
            return _make_git_proc_mock()

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
        assert '{"type":"system"' in result.output
        assert "Working..." in result.output

    async def test_timeout_stderr_appended_to_error(self, git_repo):
        """stderr captured after timeout is appended to the error message."""

        async def mock_create_subprocess_exec(*args, **kwargs):
            cmd = args[0] if args else ""
            if cmd == "claude":
                return _make_claude_proc_mock(
                    stdout_lines=[],
                    stderr=b"rate limit exceeded",
                    returncode=-9,
                    hang_wait=True,
                    hang_readline=True,
                )
            return _make_git_proc_mock()

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

        async def mock_create_subprocess_exec(*args, **kwargs):
            cmd = args[0] if args else ""
            if cmd == "claude":
                raise FileNotFoundError("claude not found")
            return _make_git_proc_mock()

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
            cmd = args[0] if args else ""
            if cmd == "claude":
                return _make_claude_proc_mock(
                    stdout_lines=[],
                    stderr=b"Some error occurred",
                    returncode=1,
                )
            return _make_git_proc_mock()

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
            cmd = args[0] if args else ""
            if cmd == "git":
                subcmd = args[1] if len(args) > 1 else ""
                git_calls.append(list(args))
                if (
                    subcmd == "checkout"
                    and len(args) > 2
                    and args[2] == "forge/new-branch"
                ):
                    mock_proc = _make_git_proc_mock(returncode=1)
                    mock_proc.stderr.read = AsyncMock(return_value=b"error: pathspec")
                    return mock_proc
                elif subcmd == "checkout" and "-b" in args:
                    return _make_git_proc_mock()
                else:
                    return _make_git_proc_mock()
            elif cmd == "claude":
                result_line = json.dumps({"type": "result", "result": "done"})
                return _make_claude_proc_mock(
                    stdout_lines=[result_line.encode() + b"\n"],
                )
            return _make_git_proc_mock()

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
        """dispatch_claude passes --output-format stream-json --json-schema when schema provided."""
        schema = '{"type": "object", "properties": {"verdict": {"type": "string"}}}'
        # stream-json: newline-delimited event objects
        system_line = json.dumps({"type": "system", "session_id": "sess123"})
        result_line = json.dumps(
            {
                "type": "result",
                "result": "Review complete",
                "structured_output": {"verdict": "PASS"},
                "usage": {"input_tokens": 50, "output_tokens": 25},
            }
        )
        captured_args: list[list] = []

        async def mock_create_subprocess_exec(*args, **kwargs):
            cmd = args[0] if args else ""
            if cmd == "claude":
                captured_args.append(list(args))
                return _make_claude_proc_mock(
                    stdout_lines=[
                        system_line.encode() + b"\n",
                        result_line.encode() + b"\n",
                    ],
                )
            return _make_git_proc_mock()

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
        # Verify CLI flags: stream-json + json-schema
        claude_args = captured_args[0]
        assert "--output-format" in claude_args
        fmt_idx = claude_args.index("--output-format")
        assert claude_args[fmt_idx + 1] == "stream-json"
        assert "--json-schema" in claude_args

    async def test_dispatch_without_json_schema_uses_stream_json(self, git_repo):
        """dispatch_claude defaults to stream-json when no json_schema provided."""
        result_line = json.dumps(
            {
                "type": "result",
                "result": "Done",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }
        )
        captured_args: list[list] = []

        async def mock_create_subprocess_exec(*args, **kwargs):
            cmd = args[0] if args else ""
            if cmd == "claude":
                captured_args.append(list(args))
                return _make_claude_proc_mock(
                    stdout_lines=[result_line.encode() + b"\n"],
                )
            return _make_git_proc_mock()

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

        with patch(
            "forge.dispatcher.asyncio.create_subprocess_exec",
            side_effect=mock_create_subprocess_exec,
        ):
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
            patch(
                "forge.dispatcher.asyncio.create_subprocess_exec", side_effect=mock_exec
            ),
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
                    mock_proc.wait = AsyncMock(
                        side_effect=[asyncio.TimeoutError(), None]
                    )
                    mock_proc.kill = MagicMock()
                    return mock_proc
            return await original_exec(*args, **kwargs)

        with (
            patch(
                "forge.dispatcher.asyncio.create_subprocess_exec", side_effect=mock_exec
            ),
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

        async def mock_exec(*args, **kwargs):
            if args[0] == "git":
                return _make_git_proc_mock()
            else:
                # Claude process — expose a known PID, non-zero exit
                return _make_claude_proc_mock(
                    stdout_lines=[],
                    stderr=b"error",
                    returncode=1,
                    pid=99999,
                )

        with patch(
            "forge.dispatcher.asyncio.create_subprocess_exec", side_effect=mock_exec
        ):
            await dispatch_claude(
                prompt="test",
                repo_path=git_repo,
                branch="main",
                timeout=10,
                pid_callback=received_pids.append,
            )

        # pid_callback should have been called once (for the claude process, not git)
        assert 99999 in received_pids


class TestProgressTimerUpdates:
    """AC12: last_output_time is updated on each stdout read."""

    async def test_last_output_time_updated_on_read(self, git_repo):
        """last_output_time[0] is updated to near-current monotonic time after output."""
        import time

        result_line = json.dumps(
            {
                "type": "result",
                "result": "done",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }
        )

        async def mock_exec(*args, **kwargs):
            if args[0] == "git":
                return _make_git_proc_mock()
            return _make_claude_proc_mock(
                stdout_lines=[
                    b"line1\n",
                    b"line2\n",
                    result_line.encode() + b"\n",
                ],
            )

        last_output_time: list[float] = [0.0]
        before = time.monotonic()

        with patch(
            "forge.dispatcher.asyncio.create_subprocess_exec",
            side_effect=mock_exec,
        ):
            result = await dispatch_claude(
                prompt="test",
                repo_path=git_repo,
                branch="main",
                timeout=60,
                last_output_time=last_output_time,
            )

        after = time.monotonic()
        assert result.exit_code == 0
        # last_output_time should have been updated to a time between before and after
        assert last_output_time[0] >= before
        assert last_output_time[0] <= after

    async def test_last_output_time_none_by_default(self, git_repo):
        """dispatch_claude works normally when last_output_time is not provided."""
        result_line = json.dumps(
            {
                "type": "result",
                "result": "ok",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        )

        async def mock_exec(*args, **kwargs):
            if args[0] == "git":
                return _make_git_proc_mock()
            return _make_claude_proc_mock(
                stdout_lines=[result_line.encode() + b"\n"],
            )

        with patch(
            "forge.dispatcher.asyncio.create_subprocess_exec",
            side_effect=mock_exec,
        ):
            result = await dispatch_claude(
                prompt="test",
                repo_path=git_repo,
                branch="main",
                timeout=60,
            )

        assert result.exit_code == 0
        assert result.error is None


# ---------------------------------------------------------------------------
# _extract_usage_tokens tests
# ---------------------------------------------------------------------------


class TestExtractUsageTokens:
    def test_result_message_with_usage(self):
        line = json.dumps(
            {
                "type": "result",
                "result": "done",
                "usage": {"input_tokens": 100, "output_tokens": 50},
            }
        )
        assert _extract_usage_tokens(line) == 150

    def test_assistant_message_with_nested_usage(self):
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "hi"}],
                    "usage": {"input_tokens": 200, "output_tokens": 80},
                },
            }
        )
        assert _extract_usage_tokens(line) == 280

    def test_no_usage_field(self):
        line = json.dumps({"type": "system", "data": "init"})
        assert _extract_usage_tokens(line) == 0

    def test_invalid_json(self):
        assert _extract_usage_tokens("not json at all") == 0

    def test_empty_string(self):
        assert _extract_usage_tokens("") == 0


# ---------------------------------------------------------------------------
# Incremental token accumulation in drain_stdout (AC10)
# ---------------------------------------------------------------------------


class TestIncrementalTokenAccumulation:
    async def test_drain_stdout_accumulates_token_counts(self, git_repo):
        """AC10: drain_stdout incrementally parses usage from multiple JSON lines."""
        assistant_line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "Working..."}],
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                },
            }
        )
        result_line = json.dumps(
            {
                "type": "result",
                "result": "Done",
                "usage": {"input_tokens": 200, "output_tokens": 80},
            }
        )
        system_line = json.dumps({"type": "system", "data": "init"})

        stdout_data = [
            system_line.encode() + b"\n",
            assistant_line.encode() + b"\n",
            result_line.encode() + b"\n",
        ]

        async def mock_create_subprocess_exec(*args, **kwargs):
            cmd = args[0] if args else ""
            if cmd == "claude":
                return _make_claude_proc_mock(stdout_lines=stdout_data)
            return _make_git_proc_mock()

        token_count: list[int] = [0]

        with patch(
            "forge.dispatcher.asyncio.create_subprocess_exec",
            side_effect=mock_create_subprocess_exec,
        ):
            result = await dispatch_claude(
                prompt="test",
                repo_path=git_repo,
                branch="main",
                timeout=60,
                token_count=token_count,
            )

        # 100+50 + 200+80 = 430
        assert token_count[0] == 430
        assert result.tokens_used == 430
        assert result.exit_code == 0

    async def test_token_count_none_does_not_error(self, git_repo):
        """When token_count is None, no parsing errors occur."""
        result_line = json.dumps(
            {
                "type": "result",
                "result": "ok",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }
        )

        async def mock_create_subprocess_exec(*args, **kwargs):
            cmd = args[0] if args else ""
            if cmd == "claude":
                return _make_claude_proc_mock(
                    stdout_lines=[result_line.encode() + b"\n"],
                )
            return _make_git_proc_mock()

        with patch(
            "forge.dispatcher.asyncio.create_subprocess_exec",
            side_effect=mock_create_subprocess_exec,
        ):
            result = await dispatch_claude(
                prompt="test",
                repo_path=git_repo,
                branch="main",
                timeout=60,
                token_count=None,
            )

        # Falls back to parse_stream_json
        assert result.tokens_used == 15
        assert result.exit_code == 0

    async def test_incremental_counter_on_nonzero_exit(self, git_repo):
        """Incremental counter is used even when exit code is non-zero."""
        assistant_line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "partial"}],
                    "usage": {"input_tokens": 300, "output_tokens": 100},
                },
            }
        )

        async def mock_create_subprocess_exec(*args, **kwargs):
            cmd = args[0] if args else ""
            if cmd == "claude":
                return _make_claude_proc_mock(
                    stdout_lines=[assistant_line.encode() + b"\n"],
                    returncode=1,
                    stderr=b"some error",
                )
            return _make_git_proc_mock()

        token_count: list[int] = [0]

        with patch(
            "forge.dispatcher.asyncio.create_subprocess_exec",
            side_effect=mock_create_subprocess_exec,
        ):
            result = await dispatch_claude(
                prompt="test",
                repo_path=git_repo,
                branch="main",
                timeout=60,
                token_count=token_count,
            )

        assert token_count[0] == 400
        assert result.tokens_used == 400
        assert result.exit_code == 1

    async def test_chunk_split_across_json_line_boundary(self, git_repo):
        """Token parsing handles chunks that split a JSON line across reads."""
        line1 = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "hi"}],
                    "usage": {"input_tokens": 50, "output_tokens": 25},
                },
            }
        )
        line2 = json.dumps(
            {
                "type": "result",
                "result": "done",
                "usage": {"input_tokens": 60, "output_tokens": 30},
            }
        )
        # Split line2 across two chunks
        full = line1.encode() + b"\n" + line2.encode() + b"\n"
        mid = len(full) // 2
        chunk1 = full[:mid]
        chunk2 = full[mid:]

        async def mock_create_subprocess_exec(*args, **kwargs):
            cmd = args[0] if args else ""
            if cmd == "claude":
                return _make_claude_proc_mock(stdout_lines=[chunk1, chunk2])
            return _make_git_proc_mock()

        token_count: list[int] = [0]

        with patch(
            "forge.dispatcher.asyncio.create_subprocess_exec",
            side_effect=mock_create_subprocess_exec,
        ):
            result = await dispatch_claude(
                prompt="test",
                repo_path=git_repo,
                branch="main",
                timeout=60,
                token_count=token_count,
            )

        # 50+25 + 60+30 = 165
        assert token_count[0] == 165
        assert result.tokens_used == 165


# ---------------------------------------------------------------------------
# Dead code removal verification
# ---------------------------------------------------------------------------


class TestNoDeadReadlineMock:
    """Verify _make_readline_mock (readline-based helper) was removed."""

    def test_make_readline_mock_not_defined(self):
        """_make_readline_mock should not exist — _make_claude_proc_mock uses read()."""
        import tests.test_dispatcher as mod

        assert not hasattr(mod, "_make_readline_mock"), (
            "_make_readline_mock is dead code; it should be removed"
        )

    def test_claude_proc_mock_uses_read(self):
        """_make_claude_proc_mock wires stdout.read, not stdout.readline."""
        proc = _make_claude_proc_mock(stdout_lines=[b'{"type":"result"}\n'])
        assert hasattr(proc.stdout, "read") and callable(proc.stdout.read)
        # readline should not be explicitly wired (only MagicMock default)
        assert not isinstance(proc.stdout.readline, (type(proc.stdout.read),))
