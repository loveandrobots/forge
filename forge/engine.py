"""Pipeline engine — the core async loop that drives tasks through pipeline stages."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import sqlite3
import time
from datetime import datetime, timezone

from forge import database
from forge.config import (
    FLOW_STAGES,
    EngineSettings,
    STAGES,
    VALID_FLOWS,
    Settings,
    resolve_progress_timeout,
    resolve_stage_timeout,
    resolve_token_budget,
)
from forge.dispatcher import (
    DispatchResult,
    GitResult,
    checkout_and_pull,
    create_branch,
    delete_branch,
    dispatch_claude,
    ff_merge,
    rebase_branch,
)
from forge.gate_runner import (
    GateResult,
    build_gate_env,
    format_structured_bounce_context,
    parse_structured_output,
    run_gate,
)
from forge.prompt_builder import build_prompt, get_git_diff, load_artifact
from forge.schemas import get_schema

logger = logging.getLogger(__name__)


def load_structured_artifact(path: str) -> dict | None:
    """Read and parse a JSON artifact file. Returns None on failure."""
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        return None
    except (OSError, IOError, json.JSONDecodeError, ValueError):
        logger.warning("Could not read/parse structured artifact: %s", path)
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_branch_name(task_id: str, title: str) -> str:
    """Generate a branch name: forge/{short_id}-{slug}."""
    short_id = task_id[:8]
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40]
    return f"forge/{short_id}-{slug}"


def _next_stage(current_stage: str, flow: str = "standard") -> str | None:
    """Return the next stage after current_stage, or None if done."""
    stages = FLOW_STAGES.get(flow, STAGES)
    try:
        idx = stages.index(current_stage)
    except ValueError:
        return None
    if idx + 1 < len(stages):
        return stages[idx + 1]
    return None


def _artifact_path_for_stage(
    repo_path: str,
    task_id: str,
    stage: str,
    flow: str = "standard",
) -> str | None:
    """Return the conventional artifact path for a completed stage, or None."""
    if stage == "spec" and flow == "epic":
        return os.path.join(repo_path, f"_forge/epic-decompositions/{task_id}.json")
    mapping = {
        "spec": f"_forge/specs/{task_id}.json",
        "plan": f"_forge/plans/{task_id}.json",
        "review": f"_forge/reviews/{task_id}.md",
    }
    rel = mapping.get(stage)
    if rel is None:
        return None
    return os.path.join(repo_path, rel)


_STAGE_TO_PATH_FIELD: dict[str, str] = {
    "spec": "spec_path",
    "plan": "plan_path",
    "review": "review_path",
}


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a sqlite3.Row to a plain dict."""
    return dict(row)


async def reset_repo_state(repo_path: str, default_branch: str) -> dict:
    """Reset a git repo to a clean state on the default branch.

    Runs (in order): git rebase --abort, git merge --abort,
    git reset --hard HEAD, git clean -fd, git checkout {default_branch}.
    The abort commands ignore failures (no rebase/merge may be active).
    The reset clears both the index and working tree (including staged deletions).
    The remaining commands must all succeed.

    Returns {"success": True/False, "output": str with combined command logs}.
    """
    log_lines: list[str] = []

    async def _run(*cmd: str, allow_failure: bool = False) -> bool:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        out = stdout.decode(errors="replace").strip()
        err = stderr.decode(errors="replace").strip()
        label = " ".join(cmd)
        rc = proc.returncode
        log_lines.append(f"$ {label} -> rc={rc}")
        if out:
            log_lines.append(f"  stdout: {out}")
        if err:
            log_lines.append(f"  stderr: {err}")
        if rc != 0 and not allow_failure:
            return False
        return True

    # Abort any in-progress rebase or merge (ignore failures)
    await _run("git", "rebase", "--abort", allow_failure=True)
    await _run("git", "merge", "--abort", allow_failure=True)

    # These must succeed
    for cmd in [
        ("git", "reset", "--hard", "HEAD"),
        ("git", "clean", "-fd"),
        ("git", "checkout", default_branch),
    ]:
        if not await _run(*cmd):
            return {"success": False, "output": "\n".join(log_lines)}

    return {"success": True, "output": "\n".join(log_lines)}


_STDERR_MAX_BYTES = 4096


def _truncate_stderr(stderr: str) -> str:
    """Truncate stderr to at most 4 KB."""
    return stderr[:_STDERR_MAX_BYTES]


def _parse_stage_timeouts(project: dict) -> dict | None:
    """Parse the stage_timeouts JSON string from a project row, or return None."""
    raw = project.get("stage_timeouts")
    if not raw:
        return None
    return json.loads(raw)


def _resolve_progress_timeout(
    project: dict | None,
    engine: "EngineSettings",
) -> int:
    """Resolve progress timeout: direct column > config dict fallback > engine default."""
    project_value = None
    if project:
        # Prefer the direct column (set via update_project / MCP API)
        col = project.get("progress_timeout_seconds")
        if col is not None:
            try:
                project_value = int(col)
            except (TypeError, ValueError):
                pass
        # Fall back to legacy config dict for backwards compatibility
        if project_value is None:
            raw = project.get("config")
            if raw:
                try:
                    cfg = json.loads(raw) if isinstance(raw, str) else raw
                    val = cfg.get("progress_timeout_seconds")
                    if val is not None:
                        project_value = int(val)
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass
    return resolve_progress_timeout(project_value, engine)


def _git_metadata(result: GitResult) -> dict:
    """Build metadata dict from a GitResult for log entries."""
    return {
        "git_stdout": result.stdout,
        "git_stderr": result.stderr,
        "git_returncode": result.returncode,
    }


class PipelineEngine:
    """Core async loop that drives tasks through the pipeline stages."""

    def __init__(self, settings: Settings, db_path: str) -> None:
        self.settings = settings
        self.db_path = db_path
        self.running: bool = False
        self.current_task_id: str | None = None
        self._loop_task: asyncio.Task | None = None
        self._timeout_task: asyncio.Task | None = None
        self._current_dispatch_task: asyncio.Task | None = None
        self._current_dispatch_pid: int | None = None
        self._progress_timestamps: dict[str, list[float]] = {}
        self._token_counts: dict[str, list[int]] = {}

    async def start(self) -> None:
        """Set running=True and begin the loop."""
        self.running = True
        self._loop_task = asyncio.create_task(self.run_loop())
        self._timeout_task = asyncio.create_task(self._timeout_loop())
        self._log("info", "Engine started")

    async def pause(self) -> None:
        """Set running=False, then wait for both background tasks to finish."""
        self.running = False
        for attr in ("_timeout_task", "_loop_task"):
            task = getattr(self, attr)
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                setattr(self, attr, None)
        self._log("info", "Engine paused")

    async def run_loop(self) -> None:
        """Main engine loop."""
        poll_interval = self.settings.engine.poll_interval_seconds

        while self.running:
            conn = database.get_connection(self.db_path)
            try:
                # Step 1: Check for timed-out running stage_runs
                await self._check_timeouts(conn)

                # Step 1b: Activate backlog tasks up to concurrency limit
                self._activate_backlog_tasks(conn)

                # Step 2: Find next queued task
                task_row = database.get_next_queued_task(conn)
                if task_row is None:
                    conn.close()
                    await asyncio.sleep(poll_interval)
                    continue

                task = _row_to_dict(task_row)
                task_id = task["id"]
                self.current_task_id = task_id

                # Get the queued stage_run for this task
                stage_runs = database.list_stage_runs(
                    conn,
                    task_id=task_id,
                    status="queued",
                )
                if not stage_runs:
                    conn.close()
                    await asyncio.sleep(poll_interval)
                    continue

                stage_run = _row_to_dict(stage_runs[0])
                stage_run_id = stage_run["id"]
                stage = stage_run["stage"]
                attempt = stage_run["attempt"]

                # Get project
                project_row = database.get_project(conn, task["project_id"])
                if project_row is None:
                    self._log(
                        "error",
                        f"Project {task['project_id']} not found for task {task_id}",
                        task_id=task_id,
                    )
                    conn.close()
                    await asyncio.sleep(poll_interval)
                    continue
                project = _row_to_dict(project_row)

                # Safety check: ensure clean working directory before dispatch
                reset_ok = await self._reset_and_log(
                    project["repo_path"],
                    project["default_branch"],
                    conn,
                    task_id,
                )
                if not reset_ok:
                    conn.close()
                    self.current_task_id = None
                    continue

                # Ensure branch exists
                branch_name = task.get("branch_name")
                if not branch_name:
                    branch_name = _make_branch_name(task_id, task["title"])
                    branch_result = await create_branch(
                        project["repo_path"],
                        branch_name,
                        project["default_branch"],
                    )
                    if not branch_result.success:
                        error_detail = _truncate_stderr(branch_result.stderr)
                        error_msg = (
                            f"Failed to create branch {branch_name}:\n{error_detail}"
                        )
                        self._log(
                            "error",
                            error_msg,
                            task_id=task_id,
                            stage_run_id=stage_run_id,
                            metadata=_git_metadata(branch_result),
                        )
                        database.update_stage_run(
                            conn,
                            stage_run_id,
                            status="error",
                            error_message=error_msg,
                            finished_at=_now(),
                        )
                        await self._handle_error_retry(
                            conn, task, stage, stage_run_id, project=project
                        )
                        conn.close()
                        self.current_task_id = None
                        continue
                    database.update_task(conn, task_id, branch_name=branch_name)
                    task["branch_name"] = branch_name

                # Rebase before implement stage
                if stage == "implement":
                    rebase_result = await rebase_branch(
                        project["repo_path"],
                        branch_name,
                        project["default_branch"],
                    )
                    if not rebase_result.success:
                        error_detail = _truncate_stderr(rebase_result.stderr)
                        error_msg = f"Rebase failed for {branch_name} — conflicts need human resolution:\n{error_detail}"
                        git_meta = _git_metadata(rebase_result)
                        self._log(
                            "warn",
                            f"Rebase failed for {branch_name} — needs human",
                            task_id=task_id,
                            metadata=git_meta,
                        )
                        database.update_stage_run(
                            conn,
                            stage_run_id,
                            status="error",
                            error_message=error_msg,
                            finished_at=_now(),
                        )
                        database.update_task(conn, task_id, status="needs_human")
                        self._log(
                            "warn",
                            f"Task {task_id} marked needs_human due to rebase conflict",
                            task_id=task_id,
                            stage_run_id=stage_run_id,
                            metadata=git_meta,
                        )
                        await self._maybe_auto_pause(conn, task_id, project)
                        conn.close()
                        self.current_task_id = None
                        continue

                # Step 3: Build prompt
                try:
                    artifacts = self._load_artifacts(
                        task,
                        project,
                        stage,
                        stage_run,
                        conn,
                    )
                    prompt = build_prompt(
                        stage,
                        task,
                        project,
                        stage_run,
                        artifacts,
                    )
                except RuntimeError as exc:
                    error_msg = str(exc)
                    database.update_stage_run(
                        conn,
                        stage_run_id,
                        status="error",
                        error_message=error_msg,
                        finished_at=_now(),
                    )
                    database.update_task(conn, task_id, status="needs_human")
                    self._log(
                        "error",
                        f"Artifact loading failed for task {task_id}: {error_msg}",
                        task_id=task_id,
                        stage_run_id=stage_run_id,
                    )
                    await self._maybe_auto_pause(conn, task_id, project)
                    conn.close()
                    self.current_task_id = None
                    continue

                # Mark stage_run as running
                started_at = _now()
                database.update_stage_run(
                    conn,
                    stage_run_id,
                    status="running",
                    started_at=started_at,
                    prompt_sent=prompt,
                )
                self._log(
                    "info",
                    f"Dispatching {stage} stage for task {task_id} (attempt {attempt})",
                    task_id=task_id,
                    stage_run_id=stage_run_id,
                )

                # Step 4: Dispatch to Claude Code
                proj_timeouts = _parse_stage_timeouts(project)
                stage_timeout = resolve_stage_timeout(
                    stage, proj_timeouts, self.settings.engine
                )
                # Resolve JSON schema for structured output dispatch
                json_schema_str: str | None = None
                flow = task.get("flow", "standard")
                schema = get_schema(stage, flow)
                if schema:
                    json_schema_str = json.dumps(schema)
                progress_ts: list[float] = [time.monotonic()]
                self._progress_timestamps[task_id] = progress_ts
                token_count: list[int] = [0]
                self._token_counts[task_id] = token_count
                self._current_dispatch_task = asyncio.create_task(
                    dispatch_claude(
                        prompt=prompt,
                        repo_path=project["repo_path"],
                        branch=branch_name,
                        timeout=stage_timeout,
                        headless_flags=self.settings.claude.headless_flags,
                        json_schema=json_schema_str,
                        pid_callback=self._set_dispatch_pid,
                        last_output_time=progress_ts,
                        token_count=token_count,
                    )
                )
                try:
                    result: DispatchResult = await self._current_dispatch_task
                except asyncio.CancelledError:
                    # handle_timeout cancelled this dispatch; DB was already updated
                    conn.close()
                    self.current_task_id = None
                    self._current_dispatch_task = None
                    self._current_dispatch_pid = None
                    self._token_counts.pop(task_id, None)
                    continue
                finally:
                    self._current_dispatch_task = None
                    self._current_dispatch_pid = None
                    self._progress_timestamps.pop(task_id, None)
                    self._token_counts.pop(task_id, None)

                finished_at = _now()

                # Handle dispatch error
                if result.error:
                    database.update_stage_run(
                        conn,
                        stage_run_id,
                        status="error",
                        finished_at=finished_at,
                        duration_seconds=result.duration_seconds,
                        claude_output=result.output,
                        tokens_used=result.tokens_used,
                        error_message=result.error,
                    )
                    self._log(
                        "error",
                        f"Dispatch error for {stage}: {result.error}",
                        task_id=task_id,
                        stage_run_id=stage_run_id,
                    )
                    reset_ok = await self._reset_and_log(
                        project["repo_path"],
                        project["default_branch"],
                        conn,
                        task_id,
                    )
                    if reset_ok:
                        await self._handle_error_retry(
                            conn, task, stage, stage_run_id, project=project
                        )
                    conn.close()
                    self.current_task_id = None
                    continue

                # Step 4b: Handle structured output from --json-schema dispatch
                structured = result.structured_output
                structured_json: str | None = None
                if structured is not None:
                    structured_json = json.dumps(structured)
                    # Update task record with artifact paths
                    update_kwargs: dict = {}
                    if structured.get("spec_path"):
                        update_kwargs["spec_path"] = structured["spec_path"]
                    if structured.get("plan_path"):
                        update_kwargs["plan_path"] = structured["plan_path"]
                    if structured.get("review_path"):
                        update_kwargs["review_path"] = structured["review_path"]
                    if update_kwargs:
                        database.update_task(conn, task_id, **update_kwargs)
                        task.update(update_kwargs)
                    # Store on stage_run
                    database.update_stage_run(
                        conn,
                        stage_run_id,
                        structured_output=structured_json,
                    )

                # Step 4c: Write structured output as JSON artifact for spec/plan/review
                if stage == "spec" and structured is not None:
                    repo_path = project.get("repo_path", "")
                    if flow == "epic":
                        spec_dir = os.path.join(
                            repo_path, "_forge", "epic-decompositions"
                        )
                    else:
                        spec_dir = os.path.join(repo_path, "_forge", "specs")
                    os.makedirs(spec_dir, exist_ok=True)
                    spec_json_path = os.path.join(spec_dir, f"{task_id}.json")
                    with open(spec_json_path, "w", encoding="utf-8") as sf:
                        json.dump(structured, sf, indent=2)
                    database.update_task(conn, task_id, spec_path=spec_json_path)
                    task["spec_path"] = spec_json_path

                if stage == "plan" and structured is not None:
                    repo_path = project.get("repo_path", "")
                    plan_dir = os.path.join(repo_path, "_forge", "plans")
                    os.makedirs(plan_dir, exist_ok=True)
                    plan_json_path = os.path.join(plan_dir, f"{task_id}.json")
                    with open(plan_json_path, "w", encoding="utf-8") as pf:
                        json.dump(structured, pf, indent=2)
                    database.update_task(conn, task_id, plan_path=plan_json_path)
                    task["plan_path"] = plan_json_path

                if stage == "review" and structured is not None:
                    repo_path = project.get("repo_path", "")
                    review_dir = os.path.join(repo_path, "_forge", "reviews")
                    os.makedirs(review_dir, exist_ok=True)
                    review_json_path = os.path.join(review_dir, f"{task_id}.json")
                    with open(review_json_path, "w", encoding="utf-8") as rf:
                        json.dump(structured, rf, indent=2)
                    database.update_task(conn, task_id, review_path=review_json_path)
                    task["review_path"] = review_json_path

                # Step 4d: Extract verdict from structured review output
                review_verdict: str | None = None
                if stage == "review" and structured is not None:
                    review_verdict = structured.get("verdict")

                # Step 5: Run gate
                # Write structured output to a temp file for gate scripts
                artifact_file_path: str | None = None
                if structured_json is not None:
                    repo_path = project.get("repo_path", "")
                    artifact_dir = os.path.join(repo_path, "_forge", "artifacts")
                    os.makedirs(artifact_dir, exist_ok=True)
                    artifact_file_path = os.path.join(
                        artifact_dir, f"{task_id}_{stage}.json"
                    )
                    with open(artifact_file_path, "w", encoding="utf-8") as af:
                        af.write(structured_json)

                task_row = database.get_task(conn, task_id)
                gate_env = build_gate_env(
                    task_row,
                    stage_runs[0],
                    project_row,
                    artifact_path=artifact_file_path,
                )
                gate_dir = project["gate_dir"]
                # Resolve relative gate_dir against repo_path
                if not os.path.isabs(gate_dir):
                    gate_dir = os.path.join(project["repo_path"], gate_dir)

                # Override gate stage name for epic spec runs
                gate_stage = stage
                flow = task.get("flow", "standard")
                if flow == "epic" and stage == "spec":
                    gate_stage = "epic-spec"
                elif flow == "epic" and stage == "review":
                    gate_stage = "epic-review"

                gate_result: GateResult = await run_gate(
                    gate_dir=gate_dir,
                    stage=gate_stage,
                    env_vars=gate_env,
                )

                # Step 6: Record results and advance or bounce
                database.update_stage_run(
                    conn,
                    stage_run_id,
                    finished_at=finished_at,
                    duration_seconds=result.duration_seconds,
                    claude_output=result.output,
                    tokens_used=result.tokens_used,
                    gate_name=gate_result.gate_name,
                    gate_exit_code=gate_result.exit_code,
                    gate_stdout=gate_result.stdout,
                    gate_stderr=gate_result.stderr,
                )

                # Use structured verdict for review stages, gate for others
                stage_passed = (
                    review_verdict == "PASS"
                    if review_verdict is not None
                    else gate_result.passed
                )

                if stage_passed:
                    # Stage passed
                    database.update_stage_run(
                        conn,
                        stage_run_id,
                        status="passed",
                    )
                    self._log(
                        "info",
                        f"Stage {stage} passed for task {task_id}",
                        task_id=task_id,
                        stage_run_id=stage_run_id,
                    )
                    await self.advance_task(
                        conn,
                        task_id,
                        stage,
                        project=project,
                        structured_output=structured,
                    )
                else:
                    # Stage failed — bounce
                    database.update_stage_run(
                        conn,
                        stage_run_id,
                        status="bounced",
                    )
                    if review_verdict is not None:
                        bounce_detail = f"verdict={review_verdict}"
                    elif gate_result.structured_output is not None:
                        bounce_detail = format_structured_bounce_context(
                            gate_result.structured_output
                        )
                    else:
                        bounce_detail = gate_result.stderr
                    self._log(
                        "warn",
                        f"Stage {stage} bounced for task {task_id}: {bounce_detail}",
                        task_id=task_id,
                        stage_run_id=stage_run_id,
                    )
                    await self.bounce_task(
                        conn, task, stage, gate_result, project=project
                    )

            except Exception:
                logger.exception("Unhandled error in engine loop")
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
                self.current_task_id = None

            # Small sleep to avoid tight-looping when there's work
            await asyncio.sleep(1)

    async def _maybe_auto_pause(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        project: dict,
    ) -> None:
        """Pause the engine if the project has pause_after_completion enabled.

        Sets self.running = False so the while-loop in run_loop() exits
        naturally at the top of the next iteration.  When the loop exits,
        _loop_task completes (not cancelled), so start() can safely
        create a fresh task without cancelling a dangling one.
        """
        if not project.get("pause_after_completion"):
            return
        task = database.get_task(conn, task_id)
        task_title = task["title"] if task else task_id
        project_name = project.get("name", "unknown")
        self._log(
            "info",
            f"Engine auto-paused after completing task '{task_title}' for project "
            f"'{project_name}'. Restart the service and unpause to continue.",
            task_id=task_id,
        )
        self.running = False

    async def _auto_merge(
        self,
        conn: sqlite3.Connection,
        task: dict,
        project: dict,
    ) -> bool:
        """Attempt to rebase and merge the feature branch into the default branch.

        Returns True if merge succeeded, False if the task was set to needs_human.
        """
        import os

        task_id = task["id"]
        branch_name = task["branch_name"]
        default_branch = project["default_branch"]
        repo_path = project["repo_path"]
        gate_dir = project["gate_dir"]

        if not branch_name:
            return True  # No branch to merge

        # Step 1: Checkout default branch and pull latest
        cop_result = await checkout_and_pull(repo_path, default_branch)
        if not cop_result.success:
            error_detail = _truncate_stderr(cop_result.stderr)
            database.update_task(conn, task_id, status="needs_human")
            self._log(
                "error",
                f"Failed to checkout/pull {default_branch}:\n{error_detail}",
                task_id=task_id,
                metadata=_git_metadata(cop_result),
            )
            return False

        # Step 2: Rebase feature branch onto default
        rebase_result = await rebase_branch(repo_path, branch_name, default_branch)
        if not rebase_result.success:
            error_detail = _truncate_stderr(rebase_result.stderr)
            database.update_task(conn, task_id, status="needs_human")
            self._log(
                "error",
                f"Merge conflict rebasing {branch_name} onto {default_branch}. Resolve manually.:\n{error_detail}",
                task_id=task_id,
                metadata=_git_metadata(rebase_result),
            )
            await self._restore_default_branch(repo_path, default_branch, task_id)
            return False

        # Step 3: Re-run post-implement gate
        if not os.path.isabs(gate_dir):
            gate_dir = os.path.join(repo_path, gate_dir)

        # Find the most recent passed implement stage_run for gate env
        stage_runs = database.list_stage_runs(conn, task_id=task_id)
        implement_run = None
        for sr in stage_runs:
            if sr["stage"] == "implement" and sr["status"] == "passed":
                implement_run = sr
        if implement_run is None:
            implement_run = stage_runs[-1] if stage_runs else None

        project_row = database.get_project(conn, task["project_id"])
        task_row = database.get_task(conn, task_id)
        # Check for structured artifact from the implement stage
        impl_artifact_path: str | None = None
        if implement_run and implement_run["structured_output"]:
            artifact_candidate = os.path.join(
                repo_path, "_forge", "artifacts", f"{task_id}_implement.json"
            )
            if os.path.exists(artifact_candidate):
                impl_artifact_path = artifact_candidate
        gate_env = build_gate_env(
            task_row, implement_run, project_row, artifact_path=impl_artifact_path
        )
        gate_result = await run_gate(gate_dir, "implement", gate_env)

        if not gate_result.passed:
            database.update_task(conn, task_id, status="needs_human")
            self._log(
                "error",
                f"Post-merge gate failed after rebasing onto {default_branch}. Gate output: {gate_result.stderr}",
                task_id=task_id,
            )
            await self._restore_default_branch(repo_path, default_branch, task_id)
            return False

        # Step 4: Checkout default branch and fast-forward merge
        cop_result2 = await checkout_and_pull(repo_path, default_branch)
        if not cop_result2.success:
            error_detail = _truncate_stderr(cop_result2.stderr)
            database.update_task(conn, task_id, status="needs_human")
            self._log(
                "error",
                f"Failed to checkout {default_branch} for merge:\n{error_detail}",
                task_id=task_id,
                metadata=_git_metadata(cop_result2),
            )
            return False

        merge_result = await ff_merge(repo_path, branch_name)
        if not merge_result.success:
            error_detail = _truncate_stderr(merge_result.stderr)
            database.update_task(conn, task_id, status="needs_human")
            self._log(
                "error",
                f"Fast-forward merge of {branch_name} into {default_branch} failed:\n{error_detail}",
                task_id=task_id,
                metadata=_git_metadata(merge_result),
            )
            await self._restore_default_branch(repo_path, default_branch, task_id)
            return False

        # Step 5: Delete feature branch (best-effort)
        await delete_branch(repo_path, branch_name)

        # Step 6: Log success
        self._log(
            "info",
            f"Merged {branch_name} into {default_branch}",
            task_id=task_id,
        )
        return True

    async def _restore_default_branch(
        self,
        repo_path: str,
        default_branch: str,
        task_id: str,
    ) -> None:
        """Best-effort checkout of the default branch after an auto-merge failure."""
        try:
            await checkout_and_pull(repo_path, default_branch)
        except Exception:
            self._log(
                "warn",
                f"Failed to restore {default_branch} after auto-merge failure",
                task_id=task_id,
            )

    async def advance_task(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        current_stage: str,
        project: dict | None = None,
        structured_output: dict | None = None,
    ) -> None:
        """Create the next stage_run or mark the task done."""
        task_row = database.get_task(conn, task_id)
        flow = task_row["flow"] if task_row else "standard"

        # Build artifact path kwargs for the completed stage (merged into the
        # next update_task call to avoid extra DB writes).
        artifact_kwargs: dict[str, str] = {}
        if project is not None:
            path_field = _STAGE_TO_PATH_FIELD.get(current_stage)
            if path_field and not (task_row and task_row[path_field]):
                art_path = _artifact_path_for_stage(
                    project.get("repo_path", ""),
                    task_id,
                    current_stage,
                    flow=flow,
                )
                if art_path:
                    artifact_kwargs[path_field] = art_path

        # Epic flow: after spec passes, decompose into child tasks
        if flow == "epic" and current_stage == "spec":
            if project is None:
                database.update_task(conn, task_id, status="needs_human")
                self._log(
                    "error",
                    "Cannot decompose epic without project context",
                    task_id=task_id,
                )
                return
            if artifact_kwargs:
                database.update_task(conn, task_id, **artifact_kwargs)
            self._process_epic_decomposition(conn, task_id, project)
            return

        next_stage = _next_stage(current_stage, flow=flow)
        if next_stage is None:
            # Process follow-ups from review before completing
            if current_stage == "review" and project is not None:
                self._process_follow_ups(
                    conn,
                    task_id,
                    project,
                    structured_output=structured_output,
                    parent_priority=task_row["priority"] if task_row else 0,
                )

            # Epic review pass: complete the epic
            if flow == "epic" and current_stage == "review":
                database.update_task(
                    conn,
                    task_id,
                    status="done",
                    current_stage=current_stage,
                    completed_at=_now(),
                    epic_status="complete",
                    **artifact_kwargs,
                )
                self._log(
                    "info", f"Epic {task_id} review passed — completed", task_id=task_id
                )
                if project is not None:
                    await self._maybe_auto_pause(conn, task_id, project)
                return

            # Attempt auto-merge before marking done
            if project is not None:
                task_row = database.get_task(conn, task_id)
                if task_row and task_row["branch_name"]:
                    merge_ok = await self._auto_merge(
                        conn, _row_to_dict(task_row), project
                    )
                    if not merge_ok:
                        if artifact_kwargs:
                            database.update_task(conn, task_id, **artifact_kwargs)
                        return  # Task set to needs_human by _auto_merge
            # All stages complete
            database.update_task(
                conn,
                task_id,
                status="done",
                current_stage=current_stage,
                completed_at=_now(),
                **artifact_kwargs,
            )
            self._log("info", f"Task {task_id} completed", task_id=task_id)

            # Check if this task's parent epic is now complete
            if task_row and task_row["parent_task_id"]:
                self._check_epic_completion(conn, task_row["parent_task_id"])

            if project is not None:
                await self._maybe_auto_pause(conn, task_id, project)
        else:
            database.update_task(
                conn, task_id, current_stage=next_stage, **artifact_kwargs
            )
            database.insert_stage_run(
                conn,
                task_id=task_id,
                stage=next_stage,
                attempt=1,
                status="queued",
            )
            self._log(
                "info",
                f"Task {task_id} advanced to {next_stage}",
                task_id=task_id,
            )

    def _escalate_to_standard(
        self,
        conn: sqlite3.Connection,
        task: dict,
    ) -> None:
        """Escalate a quick-flow task to standard flow, resetting to spec stage."""
        task_id = task["id"]
        database.update_task(
            conn,
            task_id,
            flow="standard",
            current_stage="spec",
            escalated_from_quick=1,
        )
        database.insert_stage_run(
            conn,
            task_id=task_id,
            stage="spec",
            attempt=1,
            status="queued",
        )
        self._log(
            "info",
            f"Task {task_id} auto-escalated from quick flow to standard flow — resetting to spec stage",
            task_id=task_id,
        )

    def _should_escalate(self, task: dict) -> bool:
        """Check if a quick-flow task is eligible for escalation to standard."""
        return task.get("flow") == "quick" and not task.get("escalated_from_quick")

    async def bounce_task(
        self,
        conn: sqlite3.Connection,
        task: dict,
        stage: str,
        gate_result: GateResult,
        project: dict | None = None,
    ) -> None:
        """Handle gate failure: retry or mark needs_human."""
        task_id = task["id"]
        flow = task.get("flow", "standard")
        max_retries = task.get("max_retries", self.settings.engine.default_max_retries)

        # Epic review bounce: create follow-up tasks and reset epic to decomposed
        if flow == "epic" and stage == "review":
            if project is not None:
                self._process_follow_ups(
                    conn,
                    task_id,
                    project,
                    parent_task_id=task_id,
                    parent_priority=task.get("priority", 0),
                )
            database.update_task(
                conn,
                task_id,
                epic_status="decomposed",
                status="paused",
                current_stage="review",
            )
            self._log(
                "info",
                f"Epic {task_id} review found issues — reset to decomposed, awaiting follow-up children",
                task_id=task_id,
            )
            return

        if stage == "review":
            # Review bounces go back to implement with shared retry budget
            retry_count = database.get_implement_review_retry_count(conn, task_id)
            if retry_count >= max_retries:
                bounce_count = database.get_implement_review_bounce_count(conn, task_id)
                if bounce_count >= max_retries and self._should_escalate(task):
                    self._escalate_to_standard(conn, task)
                else:
                    database.update_task(conn, task_id, status="needs_human")
                    self._log(
                        "warn",
                        f"Task {task_id} implement→review loop exceeded max retries ({max_retries}) — needs human",
                        task_id=task_id,
                    )
                    if project is not None:
                        await self._maybe_auto_pause(conn, task_id, project)
            else:
                # Bounce back to implement stage
                new_attempt = (
                    database.get_stage_run_count(conn, task_id, "implement") + 1
                )
                database.update_task(conn, task_id, current_stage="implement")
                database.insert_stage_run(
                    conn,
                    task_id=task_id,
                    stage="implement",
                    attempt=new_attempt,
                    status="queued",
                )
                self._log(
                    "info",
                    f"Task {task_id} review bounced to implement (attempt {new_attempt})",
                    task_id=task_id,
                )
        else:
            # Existing behavior for spec, plan, implement
            retry_count = database.get_retry_count(conn, task_id, stage)
            if retry_count >= max_retries:
                bounce_count = database.get_bounce_count(conn, task_id, stage)
                if bounce_count >= max_retries and self._should_escalate(task):
                    self._escalate_to_standard(conn, task)
                else:
                    database.update_task(conn, task_id, status="needs_human")
                    self._log(
                        "warn",
                        f"Task {task_id} stage {stage} exceeded max retries ({max_retries}) — needs human",
                        task_id=task_id,
                    )
                    if project is not None:
                        await self._maybe_auto_pause(conn, task_id, project)
            else:
                new_attempt = retry_count + 1
                database.insert_stage_run(
                    conn,
                    task_id=task_id,
                    stage=stage,
                    attempt=new_attempt,
                    status="queued",
                )
                self._log(
                    "info",
                    f"Task {task_id} stage {stage} queued for retry (attempt {new_attempt})",
                    task_id=task_id,
                )

    def _set_dispatch_pid(self, pid: int) -> None:
        """Store the PID of the currently running dispatch subprocess."""
        self._current_dispatch_pid = pid

    async def handle_timeout(
        self,
        conn: sqlite3.Connection,
        stage_run: sqlite3.Row,
    ) -> None:
        """Mark a timed-out stage_run as error and handle retry."""
        sr_id = stage_run["id"]
        task_id = stage_run["task_id"]
        stage = stage_run["stage"]

        # Kill the dispatch subprocess and cancel the dispatch task so the engine
        # loop unblocks immediately rather than waiting for the next poll cycle.
        if self._current_dispatch_pid is not None:
            try:
                os.kill(self._current_dispatch_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self._current_dispatch_pid = None
        if (
            self._current_dispatch_task is not None
            and not self._current_dispatch_task.done()
        ):
            self._current_dispatch_task.cancel()

        # Capture any partial token count before updating the stage_run
        tc = self._token_counts.get(task_id, [0])
        tokens_used = tc[0] if tc[0] > 0 else None

        database.update_stage_run(
            conn,
            sr_id,
            status="error",
            finished_at=_now(),
            error_message="Stage run timed out",
            termination_reason="wall_clock_timeout",
            tokens_used=tokens_used,
        )
        self._log(
            "warn",
            f"Stage run {sr_id} hit wall-clock timeout — progress timeout and token budget may need adjustment",
            task_id=task_id,
            stage_run_id=sr_id,
        )

        task_row = database.get_task(conn, task_id)
        if task_row:
            project_row = database.get_project(conn, task_row["project_id"])
            project = None
            reset_ok = True
            if project_row:
                project = _row_to_dict(project_row)
                reset_ok = await self._reset_and_log(
                    project["repo_path"],
                    project["default_branch"],
                    conn,
                    task_id,
                )
            if reset_ok:
                await self._handle_error_retry(
                    conn, _row_to_dict(task_row), stage, sr_id, project=project
                )

    async def _timeout_loop(self) -> None:
        """Background task: check for timed-out stage runs every 30 seconds.

        Runs independently of the main dispatch loop so that timeouts are
        detected even while ``dispatch_claude`` is blocking on I/O.
        """
        while self.running:
            await asyncio.sleep(30)
            if not self.running:
                break
            conn = database.get_connection(self.db_path)
            try:
                await self._check_timeouts(conn)
            finally:
                conn.close()

    async def _check_timeouts(
        self,
        conn: sqlite3.Connection,
    ) -> None:
        """Find and handle running stage_runs that have exceeded their timeout."""
        running_runs = database.list_stage_runs(conn, status="running")
        now = datetime.now(timezone.utc)
        # Cache project lookups to avoid repeated queries
        project_cache: dict[str, dict | None] = {}
        for sr in running_runs:
            started_at = sr["started_at"]
            if not started_at:
                continue
            start_dt = datetime.fromisoformat(started_at)
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
            elapsed = (now - start_dt).total_seconds()

            # Resolve per-stage timeout
            task_row = database.get_task(conn, sr["task_id"])
            proj_timeouts = None
            if task_row:
                pid = task_row["project_id"]
                if pid not in project_cache:
                    proj_row = database.get_project(conn, pid)
                    project_cache[pid] = _row_to_dict(proj_row) if proj_row else None
                project = project_cache[pid]
                if project:
                    proj_timeouts = _parse_stage_timeouts(project)

            timeout_seconds = resolve_stage_timeout(
                sr["stage"], proj_timeouts, self.settings.engine
            )
            if elapsed > timeout_seconds:
                await self.handle_timeout(conn, sr)
                continue

            # Check progress-based inactivity timeout
            task_id = sr["task_id"]
            if task_id in self._progress_timestamps:
                idle = time.monotonic() - self._progress_timestamps[task_id][0]
                progress_timeout = _resolve_progress_timeout(
                    project if task_row else None,
                    self.settings.engine,
                )
                if idle > progress_timeout:
                    await self.handle_progress_stall(conn, sr, idle)
                    continue

            # Check token budget
            if task_id in self._token_counts:
                used = self._token_counts[task_id][0]
                budget = resolve_token_budget(
                    project.get("max_token_budget") if task_row and project else None,
                    self.settings.engine,
                )
                if used > budget:
                    await self.handle_token_budget_exceeded(conn, sr, used, budget)

    async def handle_token_budget_exceeded(
        self,
        conn: sqlite3.Connection,
        stage_run: sqlite3.Row,
        used: int,
        budget: int,
    ) -> None:
        """Kill a dispatch that exceeded the token budget and trigger retry."""
        sr_id = stage_run["id"]
        task_id = stage_run["task_id"]
        stage = stage_run["stage"]

        if self._current_dispatch_pid is not None:
            try:
                os.kill(self._current_dispatch_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self._current_dispatch_pid = None
        if (
            self._current_dispatch_task is not None
            and not self._current_dispatch_task.done()
        ):
            self._current_dispatch_task.cancel()

        database.update_stage_run(
            conn,
            sr_id,
            status="error",
            finished_at=_now(),
            termination_reason="token_budget_exceeded",
            error_message=f"Token budget exceeded: {used} / {budget}",
            tokens_used=used,
        )
        self._log(
            "warn",
            f"Stage run {sr_id} killed — token budget exceeded: {used} / {budget}",
            task_id=task_id,
            stage_run_id=sr_id,
        )

        task_row = database.get_task(conn, task_id)
        if task_row:
            project_row = database.get_project(conn, task_row["project_id"])
            project = None
            reset_ok = True
            if project_row:
                project = _row_to_dict(project_row)
                reset_ok = await self._reset_and_log(
                    project["repo_path"],
                    project["default_branch"],
                    conn,
                    task_id,
                )
            if reset_ok:
                await self._handle_error_retry(
                    conn, _row_to_dict(task_row), stage, sr_id, project=project
                )

    async def handle_progress_stall(
        self,
        conn: sqlite3.Connection,
        stage_run: sqlite3.Row,
        idle_seconds: float,
    ) -> None:
        """Kill a stalled dispatch and trigger retry."""
        sr_id = stage_run["id"]
        task_id = stage_run["task_id"]
        stage = stage_run["stage"]

        if self._current_dispatch_pid is not None:
            try:
                os.kill(self._current_dispatch_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self._current_dispatch_pid = None
        if (
            self._current_dispatch_task is not None
            and not self._current_dispatch_task.done()
        ):
            self._current_dispatch_task.cancel()

        # Capture any partial token count
        tc = self._token_counts.get(task_id, [0])
        tokens_used = tc[0] if tc[0] > 0 else None

        database.update_stage_run(
            conn,
            sr_id,
            status="error",
            finished_at=_now(),
            termination_reason="progress_stall",
            error_message=f"No output for {int(idle_seconds)}s",
            tokens_used=tokens_used,
        )
        self._log(
            "warn",
            f"Stage run {sr_id} stalled — no output for {int(idle_seconds)}s",
            task_id=task_id,
            stage_run_id=sr_id,
        )

        task_row = database.get_task(conn, task_id)
        if task_row:
            project_row = database.get_project(conn, task_row["project_id"])
            project = None
            reset_ok = True
            if project_row:
                project = _row_to_dict(project_row)
                reset_ok = await self._reset_and_log(
                    project["repo_path"],
                    project["default_branch"],
                    conn,
                    task_id,
                )
            if reset_ok:
                await self._handle_error_retry(
                    conn, _row_to_dict(task_row), stage, sr_id, project=project
                )

    def _activate_backlog_tasks(self, conn: sqlite3.Connection) -> None:
        """Pick up backlog tasks: set status='active', current_stage to first stage, create initial stage_run."""
        active_count = len(database.list_tasks(conn, status="active"))
        max_concurrent = self.settings.engine.max_concurrent_tasks
        if active_count >= max_concurrent:
            return

        slots = max_concurrent - active_count
        backlog_tasks = database.list_tasks(conn, status="backlog")
        for task_row in backlog_tasks[:slots]:
            task = _row_to_dict(task_row)
            task_id = task["id"]
            flow = task.get("flow", "standard")
            first_stage = FLOW_STAGES.get(flow, STAGES)[0]
            database.update_task(
                conn,
                task_id,
                status="active",
                current_stage=first_stage,
            )
            database.insert_stage_run(
                conn,
                task_id=task_id,
                stage=first_stage,
                attempt=1,
                status="queued",
            )
            self._log(
                "info",
                f"Activated backlog task {task_id}, queued {first_stage}",
                task_id=task_id,
            )

    async def _handle_error_retry(
        self,
        conn: sqlite3.Connection,
        task: dict,
        stage: str,
        stage_run_id: str,
        project: dict | None = None,
    ) -> None:
        """After an error, retry or mark failed."""
        task_id = task["id"]
        max_retries = task.get("max_retries", self.settings.engine.default_max_retries)

        if stage == "review":
            # Review errors use the shared implement→review budget
            shared_count = database.get_implement_review_retry_count(conn, task_id)
            if shared_count >= max_retries:
                bounce_count = database.get_implement_review_bounce_count(conn, task_id)
                if bounce_count >= max_retries and self._should_escalate(task):
                    self._escalate_to_standard(conn, task)
                else:
                    database.update_task(conn, task_id, status="needs_human")
                    self._log(
                        "warn",
                        f"Task {task_id} implement→review loop exceeded max retries ({max_retries}) — needs human",
                        task_id=task_id,
                        stage_run_id=stage_run_id,
                    )
                    if project is not None:
                        await self._maybe_auto_pause(conn, task_id, project)
            else:
                # Retry review (not implement) — error is infrastructure, not quality
                new_attempt = database.get_stage_run_count(conn, task_id, "review") + 1
                database.insert_stage_run(
                    conn,
                    task_id=task_id,
                    stage="review",
                    attempt=new_attempt,
                    status="queued",
                )
                self._log(
                    "info",
                    f"Task {task_id} stage review queued for retry after error (attempt {new_attempt})",
                    task_id=task_id,
                    stage_run_id=stage_run_id,
                )
        else:
            retry_count = database.get_retry_count(conn, task_id, stage)
            if retry_count >= max_retries:
                bounce_count = database.get_bounce_count(conn, task_id, stage)
                if bounce_count >= max_retries and self._should_escalate(task):
                    self._escalate_to_standard(conn, task)
                else:
                    database.update_task(conn, task_id, status="needs_human")
                    self._log(
                        "warn",
                        f"Task {task_id} stage {stage} exceeded max retries after error — needs human",
                        task_id=task_id,
                        stage_run_id=stage_run_id,
                    )
                    if project is not None:
                        await self._maybe_auto_pause(conn, task_id, project)
            else:
                new_attempt = retry_count + 1
                database.insert_stage_run(
                    conn,
                    task_id=task_id,
                    stage=stage,
                    attempt=new_attempt,
                    status="queued",
                )
                self._log(
                    "info",
                    f"Task {task_id} stage {stage} queued for retry after error (attempt {new_attempt})",
                    task_id=task_id,
                    stage_run_id=stage_run_id,
                )

    def _process_epic_decomposition(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        project: dict,
    ) -> None:
        """Read decomposition JSON and create child tasks for an epic."""
        repo_path = project.get("repo_path", "")
        path = os.path.join(repo_path, f"_forge/epic-decompositions/{task_id}.json")

        if not os.path.exists(path):
            database.update_task(conn, task_id, status="needs_human")
            self._log(
                "error",
                f"Epic decomposition file not found: {path}",
                task_id=task_id,
            )
            return

        try:
            with open(path, encoding="utf-8") as f:
                entries = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            database.update_task(conn, task_id, status="needs_human")
            self._log(
                "error",
                f"Failed to read epic decomposition JSON: {exc}",
                task_id=task_id,
            )
            return

        # Support structured output object with a "tasks" key,
        # falling back to bare list for legacy decompositions.
        if isinstance(entries, dict):
            entries = entries.get("tasks", [])

        if not isinstance(entries, list) or not entries:
            database.update_task(conn, task_id, status="needs_human")
            self._log(
                "error",
                "Epic decomposition JSON is empty or not an array",
                task_id=task_id,
            )
            return

        created = 0
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            title = (entry.get("title") or "").strip()
            if not title:
                continue
            description = entry.get("description", "")
            flow = entry.get("flow", "standard")
            if flow not in VALID_FLOWS or flow == "epic":
                flow = "standard"
            priority = entry.get("priority", 0)

            child_id = database.insert_task(
                conn,
                project_id=project["id"],
                title=title,
                description=description,
                priority=priority,
                flow=flow,
                parent_task_id=task_id,
                max_retries=self.settings.engine.default_max_retries,
            )
            database.insert_task_link(
                conn,
                source_task_id=child_id,
                target_task_id=task_id,
                link_type="created_by",
            )
            created += 1

        if created == 0:
            database.update_task(conn, task_id, status="needs_human")
            self._log(
                "error",
                "Epic decomposition produced zero valid child tasks",
                task_id=task_id,
            )
            return

        # Transition epic to decomposed/paused
        database.update_task(
            conn,
            task_id,
            epic_status="decomposed",
            status="paused",
        )
        self._log(
            "info",
            f"Epic {task_id} decomposed into {created} child task(s)",
            task_id=task_id,
        )

    def _check_epic_completion(
        self,
        conn: sqlite3.Connection,
        parent_task_id: str,
    ) -> None:
        """Check if all children of a parent epic are done; if so, transition to review."""
        parent = database.get_task(conn, parent_task_id)
        if not parent or parent["flow"] != "epic":
            return
        # Guard against double-transition (concurrent child completion)
        if parent["epic_status"] != "decomposed":
            return
        if database.all_children_complete(conn, parent_task_id):
            database.update_task(
                conn,
                parent_task_id,
                epic_status="reviewing",
                status="active",
                current_stage="review",
            )
            attempt = database.get_stage_run_count(conn, parent_task_id, "review") + 1
            database.insert_stage_run(
                conn,
                task_id=parent_task_id,
                stage="review",
                attempt=attempt,
                status="queued",
            )
            self._log(
                "info",
                f"Epic {parent_task_id} all children done — queued for review",
                task_id=parent_task_id,
            )

    def _process_follow_ups(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        project: dict,
        parent_task_id: str | None = None,
        structured_output: dict | None = None,
        parent_priority: int = 0,
    ) -> None:
        """Create backlog tasks from follow-ups in structured output or filesystem JSON."""
        repo_path = project.get("repo_path", "")
        from_file = False

        # Try structured output first
        entries: list | None = None
        if structured_output and isinstance(structured_output.get("follow_ups"), list):
            entries = structured_output["follow_ups"]

        # Fall back to filesystem JSON
        if entries is None:
            if not repo_path:
                return
            path = os.path.join(repo_path, f"_forge/follow-ups/{task_id}.json")
            if not os.path.exists(path):
                return
            try:
                with open(path, encoding="utf-8") as f:
                    entries = json.load(f)
                from_file = True
            except (json.JSONDecodeError, OSError):
                logger.warning("Failed to process follow-ups for task %s", task_id)
                return

        if not isinstance(entries, list) or not entries:
            if from_file:
                os.remove(path)
            return

        created = 0
        for entry in entries:
            if isinstance(entry, dict):
                title = entry.get("title", "Follow-up")
                description = entry.get("description", "")
                flow = entry.get("flow", "quick")
                if flow not in VALID_FLOWS:
                    flow = "quick"
            elif isinstance(entry, str):
                if ": " in entry:
                    title, description = entry.split(": ", 1)
                else:
                    title = entry
                    description = ""
                flow = "quick"
            else:
                logger.warning(
                    "Skipping invalid follow-up entry for task %s: %r",
                    task_id,
                    entry,
                )
                continue
            follow_up_priority = max(parent_priority - 1, 1)
            new_task_id = database.insert_task(
                conn,
                project_id=project["id"],
                title=title,
                description=description,
                flow=flow,
                priority=follow_up_priority,
                parent_task_id=parent_task_id,
                max_retries=self.settings.engine.default_max_retries,
            )
            database.insert_task_link(
                conn,
                source_task_id=new_task_id,
                target_task_id=task_id,
                link_type="created_by",
            )
            created += 1

        if from_file:
            os.remove(path)
        self._log(
            "info",
            f"Created {created} follow-up task(s) from review of task {task_id}",
            task_id=task_id,
        )

    def _load_artifacts(
        self,
        task: dict,
        project: dict,
        stage: str,
        stage_run: dict,
        conn: sqlite3.Connection,
    ) -> dict:
        """Load artifacts needed for the current stage's prompt."""
        artifacts: dict = {}

        flow = task.get("flow", "standard")

        # Epic review: load decomposition as spec_content and diff from default branch
        if flow == "epic" and stage == "review":
            repo_path = project.get("repo_path", "")
            decomp_path = os.path.join(
                repo_path, f"_forge/epic-decompositions/{task['id']}.json"
            )
            if not os.path.exists(decomp_path):
                raise RuntimeError(
                    f"Epic decomposition file not found: {decomp_path}"
                    " — task may need to be reset to spec stage"
                )
            artifacts["spec_content"] = load_artifact(decomp_path)
            # Epic has no feature branch — diff is empty (children merged to default)
            artifacts["git_diff"] = ""
        else:
            repo_path = project.get("repo_path", "")
            flow_stages = FLOW_STAGES.get(flow, STAGES)
            spec_structured = None
            if stage in ("plan", "implement", "review") and "spec" in flow_stages:
                spec_path = task.get("spec_path") or _artifact_path_for_stage(
                    repo_path,
                    task["id"],
                    "spec",
                    flow=flow,
                )
                # Fallback: if .json path doesn't exist, try .md
                if (
                    spec_path
                    and not os.path.exists(spec_path)
                    and spec_path.endswith(".json")
                ):
                    md_fallback = spec_path.rsplit(".json", 1)[0] + ".md"
                    if os.path.exists(md_fallback):
                        spec_path = md_fallback
                if not spec_path or not os.path.exists(spec_path):
                    raise RuntimeError(
                        f"Spec file not found: {spec_path}"
                        " — task may need to be reset to spec stage"
                    )
                spec_structured = None
                if spec_path.endswith(".json"):
                    spec_structured = load_structured_artifact(spec_path)
                    artifacts["spec_content"] = (
                        json.dumps(spec_structured, indent=2) if spec_structured else ""
                    )
                else:
                    artifacts["spec_content"] = load_artifact(spec_path)

                # For plan stage: inject spec criteria list
                if stage == "plan" and spec_structured:
                    from forge.prompt_builder import format_spec_criteria_list

                    artifacts["spec_criteria_list"] = format_spec_criteria_list(
                        spec_structured
                    )

            plan_structured = None
            if stage in ("implement",) and "plan" in flow_stages:
                plan_path = task.get("plan_path") or _artifact_path_for_stage(
                    repo_path,
                    task["id"],
                    "plan",
                    flow=flow,
                )
                # Fallback: if .json path doesn't exist, try .md
                if (
                    plan_path
                    and not os.path.exists(plan_path)
                    and plan_path.endswith(".json")
                ):
                    md_fallback = plan_path.rsplit(".json", 1)[0] + ".md"
                    if os.path.exists(md_fallback):
                        plan_path = md_fallback
                if not plan_path or not os.path.exists(plan_path):
                    raise RuntimeError(
                        f"Plan file not found: {plan_path}"
                        " — task may need to be reset to plan stage"
                    )
                if plan_path.endswith(".json"):
                    plan_structured = load_structured_artifact(plan_path)
                    artifacts["plan_content"] = (
                        json.dumps(plan_structured, indent=2) if plan_structured else ""
                    )
                else:
                    artifacts["plan_content"] = load_artifact(plan_path)

            # For implement stage: format structured artifacts as organized context
            if stage == "implement" and spec_structured and plan_structured:
                from forge.prompt_builder import format_structured_implement_context

                ctx = format_structured_implement_context(
                    spec_structured, plan_structured
                )
                artifacts.update(ctx)

            if stage == "review":
                branch = task.get("branch_name", "")
                base = project.get("default_branch", "main")
                repo = project.get("repo_path", "")
                if branch and repo:
                    artifacts["git_diff"] = get_git_diff(repo, branch, base)

        # Load review feedback for implement retries after a review bounce
        if stage == "implement":
            bounced_reviews = database.list_stage_runs(
                conn,
                task_id=task["id"],
                stage="review",
                status="bounced",
            )
            if bounced_reviews:
                review_path = task.get("review_path") or _artifact_path_for_stage(
                    project.get("repo_path", ""),
                    task["id"],
                    "review",
                    flow=task.get("flow", "standard"),
                )
                if review_path.endswith(".json"):
                    review_data = load_structured_artifact(review_path)
                    if review_data:
                        from forge.prompt_builder import (
                            build_structured_review_feedback,
                        )

                        artifacts["review_feedback"] = build_structured_review_feedback(
                            review_data
                        )
                else:
                    review_content = load_artifact(review_path)
                    if review_content:
                        artifacts["review_feedback"] = review_content

        # Load previous gate output for retries
        if stage_run.get("attempt", 1) > 1:
            prev_runs = database.list_stage_runs(
                conn,
                task_id=task["id"],
                stage=stage,
            )
            for prev in reversed(prev_runs):
                prev_dict = _row_to_dict(prev)
                if prev_dict["id"] != stage_run["id"]:
                    # Prefer structured gate output when available
                    gate_stdout = prev_dict.get("gate_stdout", "")
                    if gate_stdout:
                        structured = parse_structured_output(gate_stdout)
                        if structured is not None:
                            artifacts["previous_gate_structured"] = (
                                format_structured_bounce_context(structured)
                            )
                    if prev_dict.get("gate_stderr"):
                        artifacts["previous_gate_stderr"] = prev_dict["gate_stderr"]
                    if (
                        prev_dict.get("gate_stderr")
                        or "previous_gate_structured" in artifacts
                    ):
                        break

        return artifacts

    def get_status(self) -> dict:
        """Return engine status: running state, current task, queue depth."""
        conn = database.get_connection(self.db_path)
        try:
            queued_runs = database.list_stage_runs(conn, status="queued")
            backlog_tasks = database.list_tasks(conn, status="backlog")
            current_task_title = None
            current_stage = None
            if self.current_task_id:
                task_row = database.get_task(conn, self.current_task_id)
                if task_row:
                    current_task_title = task_row["title"]
                    current_stage = task_row["current_stage"]
            return {
                "running": self.running,
                "current_task_id": self.current_task_id,
                "current_task_title": current_task_title,
                "current_stage": current_stage,
                "queue_depth": len(queued_runs) + len(backlog_tasks),
            }
        finally:
            conn.close()

    def get_stats(self) -> dict:
        """Return aggregate pipeline statistics."""
        conn = database.get_connection(self.db_path)
        try:
            all_tasks = database.list_tasks(conn)
            total_tasks = len(all_tasks)
            tasks_by_status: dict[str, int] = {}
            for t in all_tasks:
                s = t["status"]
                tasks_by_status[s] = tasks_by_status.get(s, 0) + 1

            all_runs = database.list_stage_runs(conn)
            total_stage_runs = len(all_runs)
            runs_by_status: dict[str, int] = {}
            durations: list[float] = []
            for sr in all_runs:
                s = sr["status"]
                runs_by_status[s] = runs_by_status.get(s, 0) + 1
                if sr["duration_seconds"] is not None:
                    durations.append(sr["duration_seconds"])

            avg_duration = sum(durations) / len(durations) if durations else None

            total_completed = database.count_tasks_by_exact_status(conn, "done")
            total_active = database.count_tasks_by_exact_status(conn, "active")
            avg_duration_by_stage = database.get_avg_duration_by_stage(conn)
            bounce_rate_by_stage = database.get_bounce_rate_by_stage(conn)

            return {
                "total_tasks": total_tasks,
                "tasks_by_status": tasks_by_status,
                "total_stage_runs": total_stage_runs,
                "stage_runs_by_status": runs_by_status,
                "avg_stage_duration_seconds": avg_duration,
                "total_completed": total_completed,
                "total_active": total_active,
                "avg_duration_by_stage": avg_duration_by_stage,
                "bounce_rate_by_stage": bounce_rate_by_stage,
            }
        finally:
            conn.close()

    def _log(
        self,
        level: str,
        message: str,
        task_id: str | None = None,
        stage_run_id: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        """Log to both Python logger and the run_log database table."""
        getattr(logger, level if level != "warn" else "warning")(message)
        try:
            conn = database.get_connection(self.db_path)
            try:
                database.insert_log(
                    conn,
                    level=level,
                    message=message,
                    task_id=task_id,
                    stage_run_id=stage_run_id,
                    metadata=metadata,
                )
            finally:
                conn.close()
        except Exception:
            logger.exception("Failed to write to run_log")

    async def _reset_and_log(
        self,
        repo_path: str,
        default_branch: str,
        conn: sqlite3.Connection,
        task_id: str | None = None,
    ) -> bool:
        """Run reset_repo_state and log the outcome.

        If the reset fails, marks the task as needs_human (when task_id is given).
        Returns True if the reset succeeded.
        """
        result = await reset_repo_state(repo_path, default_branch)
        self._log(
            "info" if result["success"] else "error",
            f"reset_repo_state: success={result['success']}\n{result['output']}",
            task_id=task_id,
        )
        if not result["success"] and task_id:
            database.update_task(
                conn,
                task_id,
                status="needs_human",
            )
            self._log(
                "error",
                f"Task {task_id} marked needs_human — repo cleanup failed",
                task_id=task_id,
            )
            return False
        return True
