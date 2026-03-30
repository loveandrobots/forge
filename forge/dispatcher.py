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


async def create_branch(
    repo_path: str,
    branch: str,
    base_branch: str,
) -> bool:
    """Create a feature branch from base_branch. Returns True on success."""
    # Ensure we're on the base branch first
    proc = await asyncio.create_subprocess_exec(
        "git",
        "checkout",
        base_branch,
        cwd=repo_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.wait()
    if proc.returncode != 0:
        return False

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
    await proc.wait()
    return proc.returncode == 0


async def rebase_branch(
    repo_path: str,
    branch: str,
    base_branch: str,
) -> bool:
    """Rebase feature branch on base_branch. Returns False if conflicts (needs_human)."""
    # Checkout the feature branch
    proc = await asyncio.create_subprocess_exec(
        "git",
        "checkout",
        branch,
        cwd=repo_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.wait()
    if proc.returncode != 0:
        return False

    # Rebase onto base
    proc = await asyncio.create_subprocess_exec(
        "git",
        "rebase",
        base_branch,
        cwd=repo_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.wait()
    if proc.returncode != 0:
        # Abort the failed rebase
        abort_proc = await asyncio.create_subprocess_exec(
            "git",
            "rebase",
            "--abort",
            cwd=repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await abort_proc.wait()
        return False

    return True


async def checkout_and_pull(repo_path: str, branch: str) -> bool:
    """Checkout a branch and pull latest (ff-only).

    Pull failure is tolerated for local-only repos with no remote.
    Returns False on checkout failure or if repo_path is invalid.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "checkout", branch,
            cwd=repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.wait()
        if proc.returncode != 0:
            return False
    except OSError:
        return False

    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "pull", "--ff-only",
            cwd=repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.wait()
    except OSError:
        pass
    # pull may fail if no remote configured — that's OK for local-only repos
    return True


async def ff_merge(repo_path: str, branch: str) -> bool:
    """Fast-forward merge branch into the currently checked-out branch."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "merge", "--ff-only", branch,
            cwd=repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.wait()
        return proc.returncode == 0
    except OSError:
        return False


async def delete_branch(repo_path: str, branch: str) -> bool:
    """Delete a local branch."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "branch", "-d", branch,
            cwd=repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.wait()
        return proc.returncode == 0
    except OSError:
        return False
