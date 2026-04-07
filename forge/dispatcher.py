"""Dispatcher — Claude Code subprocess interface.

All Claude Code CLI interaction is isolated in this module.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


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
    structured_output: dict | None = None


def parse_json_output(raw: str) -> dict:
    """Parse the --output-format json response from Claude CLI.

    Extracts ``result`` (text), ``structured_output`` (parsed JSON from schema),
    and ``tokens`` (usage info).  Returns a dict with these keys.
    """
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning("Failed to parse JSON output from Claude CLI")
        return {"result": raw, "structured_output": None, "tokens": None}

    if not isinstance(obj, dict):
        logger.warning(
            "Unexpected JSON root type from Claude CLI: %s", type(obj).__name__
        )
        return {"result": raw, "structured_output": None, "tokens": None}

    result_text = obj.get("result", "")
    structured_output = obj.get("structured_output", None)

    tokens: int | None = None
    usage = obj.get("usage", {})
    if usage:
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        tokens = input_tokens + output_tokens

    return {
        "result": result_text,
        "structured_output": structured_output,
        "tokens": tokens,
    }


def parse_stream_json(raw: str) -> tuple[str, int | None]:
    """Parse stream-json output from Claude Code CLI.

    With ``--verbose``, the CLI emits a JSON array of event objects.
    Without ``--verbose``, it emits newline-delimited JSON objects.
    Both formats are handled here.

    We look for ``"type": "result"`` and ``"type": "assistant"`` messages
    to extract the final text and token usage.

    Returns (final_text, tokens_used).
    """
    final_text = ""
    tokens_used: int | None = None

    # Try to parse the whole output as a JSON array first (--verbose mode).
    items: list | None = None
    stripped = raw.strip()
    if stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, list):
                items = parsed
        except json.JSONDecodeError:
            pass

    # Fall back to newline-delimited objects.
    if items is None:
        items = []
        for line in stripped.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    items.append(obj)
            except json.JSONDecodeError:
                continue

    for obj in items:
        if not isinstance(obj, dict):
            continue

        msg_type = obj.get("type", "")

        # Collect assistant text from result message
        if msg_type == "result":
            final_text = obj.get("result", final_text)
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
            usage = message.get("usage", obj.get("usage", {}))
            if usage:
                input_tokens = usage.get("input_tokens", 0)
                output_tokens = usage.get("output_tokens", 0)
                tokens_used = input_tokens + output_tokens

    return final_text, tokens_used


_GIT_CHECKOUT_TIMEOUT = 60.0


async def dispatch_claude(
    prompt: str,
    repo_path: str,
    branch: str,
    timeout: int,
    headless_flags: str = "",
    json_schema: str | None = None,
    pid_callback: Callable[[int], None] | None = None,
) -> DispatchResult:
    """Spawn a Claude Code CLI session and capture output.

    Checks out the given branch in repo_path, runs ``claude -p`` with
    the appropriate output format, and returns the parsed result.

    When *json_schema* is provided, uses ``--output-format json --json-schema``
    to get structured output.  Otherwise uses ``--output-format stream-json``.
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
    try:
        await asyncio.wait_for(checkout_proc.wait(), timeout=_GIT_CHECKOUT_TIMEOUT)
    except asyncio.TimeoutError:
        checkout_proc.kill()
        await checkout_proc.wait()
        return DispatchResult(
            output="",
            exit_code=1,
            duration_seconds=time.monotonic() - start,
            error=f"Git checkout timed out after {_GIT_CHECKOUT_TIMEOUT:.0f}s",
        )
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
        try:
            await asyncio.wait_for(create_proc.wait(), timeout=_GIT_CHECKOUT_TIMEOUT)
        except asyncio.TimeoutError:
            create_proc.kill()
            await create_proc.wait()
            return DispatchResult(
                output="",
                exit_code=1,
                duration_seconds=time.monotonic() - start,
                error=f"Git checkout -b timed out after {_GIT_CHECKOUT_TIMEOUT:.0f}s",
            )
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
        extra_flags = shlex.split(headless_flags) if headless_flags else []
        if json_schema is not None:
            extra_flags.extend(
                [
                    "--output-format",
                    "json",
                    "--json-schema",
                    json_schema,
                ]
            )
        elif not any(f.startswith("--output-format") for f in extra_flags):
            extra_flags.extend(["--output-format", "stream-json"])
        proc = await asyncio.create_subprocess_exec(
            "claude",
            "-p",
            prompt,
            *extra_flags,
            cwd=repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if pid_callback is not None and proc.pid is not None:
            pid_callback(proc.pid)

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            partial_out = ""
            partial_err = ""
            if proc.stdout:
                try:
                    partial_out = (
                        await asyncio.wait_for(proc.stdout.read(), timeout=5.0)
                    ).decode(errors="replace")
                except asyncio.TimeoutError:
                    pass
            if proc.stderr:
                try:
                    partial_err = (
                        await asyncio.wait_for(proc.stderr.read(), timeout=5.0)
                    ).decode(errors="replace")
                except asyncio.TimeoutError:
                    pass
            error_msg = f"Claude Code session timed out after {timeout}s"
            if partial_err:
                error_msg += f"\nstderr: {partial_err.strip()}"
            return DispatchResult(
                output=partial_out,
                exit_code=-1,
                duration_seconds=time.monotonic() - start,
                error=error_msg,
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

        if json_schema is not None:
            parsed = parse_json_output(raw_output)
            return DispatchResult(
                output=parsed["result"],
                exit_code=exit_code,
                duration_seconds=time.monotonic() - start,
                tokens_used=parsed["tokens"],
                structured_output=parsed["structured_output"],
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
            partial_out = ""
            partial_err = ""
            if proc.stdout:
                try:
                    partial_out = (
                        await asyncio.wait_for(proc.stdout.read(), timeout=5.0)
                    ).decode(errors="replace")
                except asyncio.TimeoutError:
                    pass
            if proc.stderr:
                try:
                    partial_err = (
                        await asyncio.wait_for(proc.stderr.read(), timeout=5.0)
                    ).decode(errors="replace")
                except asyncio.TimeoutError:
                    pass
            error_msg = f"Claude Code session timed out after {timeout}s"
            if partial_err:
                error_msg += f"\nstderr: {partial_err.strip()}"
            return DispatchResult(
                output=partial_out,
                exit_code=-1,
                duration_seconds=time.monotonic() - start,
                error=error_msg,
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

    # Clean index and working tree before rebase (defends against dirty state
    # left by a timed-out session, e.g. staged deletions).
    reset_proc = await asyncio.create_subprocess_exec(
        "git",
        "reset",
        "--hard",
        "HEAD",
        cwd=repo_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    reset_stdout, reset_stderr = await reset_proc.communicate()
    if reset_proc.returncode != 0:
        return GitResult(
            success=False,
            stdout=reset_stdout.decode(errors="replace"),
            stderr=reset_stderr.decode(errors="replace"),
            returncode=reset_proc.returncode or 1,
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
            "git",
            "pull",
            "--ff-only",
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
            "git",
            "merge",
            "--ff-only",
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
            "git",
            "branch",
            "-d",
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
    except OSError as e:
        return GitResult(
            success=False,
            stderr=str(e),
            returncode=1,
        )
