"""Dispatcher — Claude Code subprocess interface.

All Claude Code CLI interaction is isolated in this module.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import time
from dataclasses import dataclass


@dataclass
class GitResult:
    """Result of a git subprocess operation."""

    success: bool
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


@dataclass
class DispatchResult:
    """Result of a Claude Code dispatch."""

    output: str
    exit_code: int
    duration_seconds: float
    tokens_used: int | None = None
    error: str | None = None


def parse_stream_json(raw: str) -> tuple[str, int | None]:
    """Parse stream-json output from Claude Code CLI.

    Each line is a JSON object. We look for "result" type messages
    to extract the final text, and "usage" or token info along the way.

    Returns (final_text, tokens_used).
    """
    final_text = ""
    tokens_used: int | None = None

    for line in raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg_type = obj.get("type", "")

        # Collect assistant text from result message
        if msg_type == "result":
            final_text = obj.get("result", final_text)
            # Token usage in result message
            usage = obj.get("usage", {})
            if usage:
                input_tokens = usage.get("input_tokens", 0)
                output_tokens = usage.get("output_tokens", 0)
                tokens_used = input_tokens + output_tokens

        # Some stream-json formats put content in assistant messages
        elif msg_type == "assistant":
            message = obj.get("message", {})
            content_blocks = message.get("content", [])
            text_parts = []
            for block in content_blocks:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
            if text_parts:
                final_text = "\n".join(text_parts)
            # Token usage
            usage = message.get("usage", obj.get("usage", {}))
            if usage:
                input_tokens = usage.get("input_tokens", 0)
                output_tokens = usage.get("output_tokens", 0)
                tokens_used = input_tokens + output_tokens

    return final_text, tokens_used


async def dispatch_claude(
    prompt: str,
    repo_path: str,
    branch: str,
    timeout: int,
    headless_flags: str = "--output-format stream-json",
) -> DispatchResult:
    """Spawn a Claude Code CLI session and capture output.

    Checks out the given branch in repo_path, runs ``claude -p`` with
    stream-json output, and returns the parsed result.
    """
    start = time.monotonic()

    # Checkout branch (create from current if needed)
    checkout_proc = await asyncio.create_subprocess_exec(
        "git",
        "checkout",
        branch,
        cwd=repo_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await checkout_proc.wait()
    if checkout_proc.returncode != 0:
        create_proc = await asyncio.create_subprocess_exec(
            "git",
            "checkout",
            "-b",
            branch,
            cwd=repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await create_proc.wait()
        if create_proc.returncode != 0:
            stderr_bytes = (
                await create_proc.stderr.read() if create_proc.stderr else b""
            )
            return DispatchResult(
                output="",
                exit_code=create_proc.returncode or 1,
                duration_seconds=time.monotonic() - start,
                error=f"Failed to checkout branch {branch}: {stderr_bytes.decode().strip()}",
            )

    # Run claude CLI
    try:
        extra_flags = shlex.split(headless_flags)
        proc = await asyncio.create_subprocess_exec(
            "claude",
            "-p",
            prompt,
            *extra_flags,
            cwd=repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return DispatchResult(
                output="",
                exit_code=-1,
                duration_seconds=time.monotonic() - start,
                error=f"Claude Code session timed out after {timeout}s",
            )

        raw_output = stdout_bytes.decode(errors="replace")
        exit_code = proc.returncode or 0

        if exit_code != 0:
            stderr_text = stderr_bytes.decode(errors="replace").strip()
            return DispatchResult(
                output=raw_output,
                exit_code=exit_code,
                duration_seconds=time.monotonic() - start,
                error=stderr_text or f"Claude exited with code {exit_code}",
            )

        final_text, tokens_used = parse_stream_json(raw_output)

        return DispatchResult(
            output=final_text,
            exit_code=exit_code,
            duration_seconds=time.monotonic() - start,
            tokens_used=tokens_used,
        )

    except FileNotFoundError:
        return DispatchResult(
            output="",
            exit_code=1,
            duration_seconds=time.monotonic() - start,
            error="claude CLI not found in PATH",
        )


async def dispatch_generate(
    prompt: str,
    repo_path: str,
    skill_path: str,
    timeout: int = 120,
) -> DispatchResult:
    """Run Claude Code headless with a skill loaded, without branch checkout.

    Used for AI-assisted task generation. Runs ``claude -p <prompt>
    -s <skill_path> --output-format stream-json --allowedTools ""``
    in the given repo directory.
    """
    start = time.monotonic()

    try:
        proc = await asyncio.create_subprocess_exec(
            "claude",
            "-p",
            prompt,
            "-s",
            skill_path,
            "--output-format",
            "stream-json",
            "--allowedTools",
            "",
            cwd=repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return DispatchResult(
                output="",
                exit_code=-1,
                duration_seconds=time.monotonic() - start,
                error=f"Claude Code session timed out after {timeout}s",
            )

        raw_output = stdout_bytes.decode(errors="replace")
        exit_code = proc.returncode or 0

        if exit_code != 0:
            stderr_text = stderr_bytes.decode(errors="replace").strip()
            return DispatchResult(
                output=raw_output,
                exit_code=exit_code,
                duration_seconds=time.monotonic() - start,
                error=stderr_text or f"Claude exited with code {exit_code}",
            )

        final_text, tokens_used = parse_stream_json(raw_output)

        return DispatchResult(
            output=final_text,
            exit_code=exit_code,
            duration_seconds=time.monotonic() - start,
            tokens_used=tokens_used,
        )

    except FileNotFoundError:
        return DispatchResult(
            output="",
            exit_code=1,
            duration_seconds=time.monotonic() - start,
            error="claude CLI not found in PATH",
        )


async def create_branch(
    repo_path: str,
    branch: str,
    base_branch: str,
) -> GitResult:
    """Create a feature branch from base_branch. Returns GitResult."""
    # Ensure we're on the base branch first
    proc = await asyncio.create_subprocess_exec(
        "git",
        "checkout",
        base_branch,
        cwd=repo_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    if proc.returncode != 0:
        return GitResult(
            success=False,
            stdout=stdout_bytes.decode(errors="replace"),
            stderr=stderr_bytes.decode(errors="replace"),
            returncode=proc.returncode or 1,
        )

    # Create the new branch
    proc = await asyncio.create_subprocess_exec(
        "git",
        "checkout",
        "-b",
        branch,
        cwd=repo_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    return GitResult(
        success=proc.returncode == 0,
        stdout=stdout_bytes.decode(errors="replace"),
        stderr=stderr_bytes.decode(errors="replace"),
        returncode=proc.returncode or 0,
    )


async def rebase_branch(
    repo_path: str,
    branch: str,
    base_branch: str,
) -> GitResult:
    """Rebase feature branch on base_branch. Returns GitResult with conflict details."""
    # Checkout the feature branch
    proc = await asyncio.create_subprocess_exec(
        "git",
        "checkout",
        branch,
        cwd=repo_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    if proc.returncode != 0:
        return GitResult(
            success=False,
            stdout=stdout_bytes.decode(errors="replace"),
            stderr=stderr_bytes.decode(errors="replace"),
            returncode=proc.returncode or 1,
        )

    # Rebase onto base
    proc = await asyncio.create_subprocess_exec(
        "git",
        "rebase",
        base_branch,
        cwd=repo_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    rebase_stdout, rebase_stderr = await proc.communicate()
    if proc.returncode != 0:
        # Capture conflict stderr before abort
        conflict_stdout = rebase_stdout.decode(errors="replace")
        conflict_stderr = rebase_stderr.decode(errors="replace")

        # Abort the failed rebase
        abort_proc = await asyncio.create_subprocess_exec(
            "git",
            "rebase",
            "--abort",
            cwd=repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        abort_stdout, abort_stderr = await abort_proc.communicate()
        if abort_proc.returncode != 0:
            abort_err = abort_stderr.decode(errors="replace")
            conflict_stderr += f"\n(abort also failed: {abort_err})"

        return GitResult(
            success=False,
            stdout=conflict_stdout,
            stderr=conflict_stderr,
            returncode=proc.returncode or 1,
        )

    return GitResult(
        success=True,
        stdout=rebase_stdout.decode(errors="replace"),
        stderr=rebase_stderr.decode(errors="replace"),
        returncode=0,
    )


async def checkout_and_pull(repo_path: str, branch: str) -> GitResult:
    """Checkout a branch and pull latest (ff-only).

    Pull failure is tolerated for local-only repos with no remote.
    Returns GitResult with success=False only on checkout failure.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "checkout", branch,
            cwd=repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await proc.communicate()
        if proc.returncode != 0:
            return GitResult(
                success=False,
                stdout=stdout_bytes.decode(errors="replace"),
                stderr=stderr_bytes.decode(errors="replace"),
                returncode=proc.returncode or 1,
            )
    except OSError as e:
        return GitResult(
            success=False,
            stderr=str(e),
            returncode=1,
        )

    all_stdout = stdout_bytes.decode(errors="replace")
    all_stderr = stderr_bytes.decode(errors="replace")

    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "pull", "--ff-only",
            cwd=repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        pull_stdout, pull_stderr = await proc.communicate()
        all_stdout += pull_stdout.decode(errors="replace")
        all_stderr += pull_stderr.decode(errors="replace")
    except OSError:
        pass
    # pull may fail if no remote configured — that's OK for local-only repos
    return GitResult(
        success=True,
        stdout=all_stdout,
        stderr=all_stderr,
        returncode=0,
    )


async def ff_merge(repo_path: str, branch: str) -> GitResult:
    """Fast-forward merge branch into the currently checked-out branch."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "merge", "--ff-only", branch,
            cwd=repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await proc.communicate()
        return GitResult(
            success=proc.returncode == 0,
            stdout=stdout_bytes.decode(errors="replace"),
            stderr=stderr_bytes.decode(errors="replace"),
            returncode=proc.returncode or 0,
        )
    except OSError as e:
        return GitResult(
            success=False,
            stderr=str(e),
            returncode=1,
        )


async def delete_branch(repo_path: str, branch: str) -> GitResult:
    """Delete a local branch."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "branch", "-d", branch,
            cwd=repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await proc.communicate()
        return GitResult(
            success=proc.returncode == 0,
            stdout=stdout_bytes.decode(errors="replace"),
            stderr=stderr_bytes.decode(errors="replace"),
            returncode=proc.returncode or 0,
        )
    except OSError as e:
        return GitResult(
            success=False,
            stderr=str(e),
            returncode=1,
        )
