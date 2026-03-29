"""Pipeline engine — the core async loop that drives tasks through pipeline stages."""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from datetime import datetime, timezone

from forge import database
from forge.config import STAGES, Settings
from forge.dispatcher import DispatchResult, create_branch, dispatch_claude, rebase_branch
from forge.gate_runner import GateResult, build_gate_env, run_gate
from forge.prompt_builder import build_prompt, get_git_diff, load_artifact

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_branch_name(task_id: str, title: str) -> str:
    """Generate a branch name: forge/{short_id}-{slug}."""
    short_id = task_id[:8]
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40]
    return f"forge/{short_id}-{slug}"


def _next_stage(current_stage: str) -> str | None:
    """Return the next stage after current_stage, or None if done."""
    try:
        idx = STAGES.index(current_stage)
    except ValueError:
        return None
    if idx + 1 < len(STAGES):
        return STAGES[idx + 1]
    return None


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a sqlite3.Row to a plain dict."""
    return dict(row)


class PipelineEngine:
    """Core async loop that drives tasks through the pipeline stages."""

    def __init__(self, settings: Settings, db_path: str) -> None:
        self.settings = settings
        self.db_path = db_path
        self.running: bool = False
        self.current_task_id: str | None = None
        self._loop_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Set running=True and begin the loop."""
        self.running = True
        self._loop_task = asyncio.create_task(self.run_loop())
        self._log("info", "Engine started")

    async def pause(self) -> None:
        """Set running=False, then wait for the loop task to finish."""
        self.running = False
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None
        self._log("info", "Engine paused")

    async def run_loop(self) -> None:
        """Main engine loop."""
        poll_interval = self.settings.engine.poll_interval_seconds
        timeout = self.settings.engine.stage_timeout_seconds

        while self.running:
            conn = database.get_connection(self.db_path)
            try:
                # Step 1: Check for timed-out running stage_runs
                await self._check_timeouts(conn, timeout)

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
                    conn, task_id=task_id, status="queued",
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

                # Ensure branch exists
                branch_name = task.get("branch_name")
                if not branch_name:
                    branch_name = _make_branch_name(task_id, task["title"])
                    ok = await create_branch(
                        project["repo_path"], branch_name, project["default_branch"],
                    )
                    if not ok:
                        self._log(
                            "error",
                            f"Failed to create branch {branch_name}",
                            task_id=task_id,
                            stage_run_id=stage_run_id,
                        )
                        database.update_stage_run(
                            conn, stage_run_id,
                            status="error",
                            error_message=f"Failed to create branch {branch_name}",
                            finished_at=_now(),
                        )
                        await self._handle_error_retry(conn, task, stage, stage_run_id, project=project)
                        conn.close()
                        self.current_task_id = None
                        continue
                    database.update_task(conn, task_id, branch_name=branch_name)
                    task["branch_name"] = branch_name

                # Rebase before implement stage
                if stage == "implement":
                    rebase_ok = await rebase_branch(
                        project["repo_path"], branch_name, project["default_branch"],
                    )
                    if not rebase_ok:
                        self._log(
                            "warn",
                            f"Rebase failed for {branch_name} — needs human",
                            task_id=task_id,
                        )
                        database.update_stage_run(
                            conn, stage_run_id,
                            status="error",
                            error_message="Rebase failed — conflicts need human resolution",
                            finished_at=_now(),
                        )
                        database.update_task(conn, task_id, status="needs_human")
                        self._log(
                            "warn",
                            f"Task {task_id} marked needs_human due to rebase conflict",
                            task_id=task_id,
                            stage_run_id=stage_run_id,
                        )
                        await self._maybe_auto_pause(conn, task_id, project)
                        conn.close()
                        self.current_task_id = None
                        continue

                # Step 3: Build prompt
                artifacts = self._load_artifacts(
                    task, project, stage, stage_run, conn,
                )
                prompt = build_prompt(
                    stage, task, project, stage_run, artifacts,
                )

                # Mark stage_run as running
                started_at = _now()
                database.update_stage_run(
                    conn, stage_run_id,
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
                result: DispatchResult = await dispatch_claude(
                    prompt=prompt,
                    repo_path=project["repo_path"],
                    branch=branch_name,
                    timeout=timeout,
                )

                finished_at = _now()

                # Handle dispatch error
                if result.error:
                    database.update_stage_run(
                        conn, stage_run_id,
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
                    await self._handle_error_retry(conn, task, stage, stage_run_id, project=project)
                    conn.close()
                    self.current_task_id = None
                    continue

                # Step 5: Run gate
                gate_env = build_gate_env(
                    task_row, stage_runs[0], project_row,
                )
                gate_dir = project["gate_dir"]
                # Resolve relative gate_dir against repo_path
                import os
                if not os.path.isabs(gate_dir):
                    gate_dir = os.path.join(project["repo_path"], gate_dir)

                gate_result: GateResult = await run_gate(
                    gate_dir=gate_dir,
                    stage=stage,
                    env_vars=gate_env,
                )

                # Step 6: Record results and advance or bounce
                database.update_stage_run(
                    conn, stage_run_id,
                    finished_at=finished_at,
                    duration_seconds=result.duration_seconds,
                    claude_output=result.output,
                    tokens_used=result.tokens_used,
                    gate_name=gate_result.gate_name,
                    gate_exit_code=gate_result.exit_code,
                    gate_stdout=gate_result.stdout,
                    gate_stderr=gate_result.stderr,
                )

                if gate_result.passed:
                    # Gate passed
                    database.update_stage_run(
                        conn, stage_run_id, status="passed",
                    )
                    self._log(
                        "info",
                        f"Stage {stage} passed for task {task_id}",
                        task_id=task_id,
                        stage_run_id=stage_run_id,
                    )
                    await self.advance_task(conn, task_id, stage, project=project)
                else:
                    # Gate failed — bounce
                    database.update_stage_run(
                        conn, stage_run_id, status="bounced",
                    )
                    self._log(
                        "warn",
                        f"Stage {stage} bounced for task {task_id}: {gate_result.stderr}",
                        task_id=task_id,
                        stage_run_id=stage_run_id,
                    )
                    await self.bounce_task(conn, task, stage, gate_result, project=project)

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
        """Pause the engine if the project has pause_after_completion enabled."""
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

    async def advance_task(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        current_stage: str,
        project: dict | None = None,
    ) -> None:
        """Create the next stage_run or mark the task done."""
        next_stage = _next_stage(current_stage)
        if next_stage is None:
            # All stages complete
            database.update_task(
                conn, task_id,
                status="done",
                current_stage=current_stage,
                completed_at=_now(),
            )
            self._log("info", f"Task {task_id} completed", task_id=task_id)
            if project is not None:
                await self._maybe_auto_pause(conn, task_id, project)
        else:
            database.update_task(conn, task_id, current_stage=next_stage)
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
        max_retries = task.get("max_retries", self.settings.engine.default_max_retries)
        retry_count = database.get_retry_count(conn, task_id, stage)

        if retry_count >= max_retries:
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
                attempt=new_attempt + 1,
                status="queued",
            )
            self._log(
                "info",
                f"Task {task_id} stage {stage} queued for retry (attempt {new_attempt + 1})",
                task_id=task_id,
            )

    async def handle_timeout(
        self,
        conn: sqlite3.Connection,
        stage_run: sqlite3.Row,
    ) -> None:
        """Mark a timed-out stage_run as error and handle retry."""
        sr_id = stage_run["id"]
        task_id = stage_run["task_id"]
        stage = stage_run["stage"]

        database.update_stage_run(
            conn, sr_id,
            status="error",
            finished_at=_now(),
            error_message="Stage run timed out",
        )
        self._log(
            "error",
            f"Stage run {sr_id} timed out",
            task_id=task_id,
            stage_run_id=sr_id,
        )

        task_row = database.get_task(conn, task_id)
        if task_row:
            await self._handle_error_retry(conn, _row_to_dict(task_row), stage, sr_id)

    async def _check_timeouts(
        self,
        conn: sqlite3.Connection,
        timeout_seconds: int,
    ) -> None:
        """Find and handle running stage_runs that have exceeded the timeout."""
        running_runs = database.list_stage_runs(conn, status="running")
        now = datetime.now(timezone.utc)
        for sr in running_runs:
            started_at = sr["started_at"]
            if not started_at:
                continue
            start_dt = datetime.fromisoformat(started_at)
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
            elapsed = (now - start_dt).total_seconds()
            if elapsed > timeout_seconds:
                await self.handle_timeout(conn, sr)

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
            first_stage = STAGES[0]
            database.update_task(
                conn, task_id,
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
        retry_count = database.get_retry_count(conn, task_id, stage)

        if retry_count >= max_retries:
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
                attempt=new_attempt + 1,
                status="queued",
            )
            self._log(
                "info",
                f"Task {task_id} stage {stage} queued for retry after error (attempt {new_attempt + 1})",
                task_id=task_id,
                stage_run_id=stage_run_id,
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

        if stage in ("plan", "implement", "review") and task.get("spec_path"):
            artifacts["spec_content"] = load_artifact(task["spec_path"])

        if stage in ("implement",) and task.get("plan_path"):
            artifacts["plan_content"] = load_artifact(task["plan_path"])

        if stage == "review":
            branch = task.get("branch_name", "")
            base = project.get("default_branch", "main")
            repo = project.get("repo_path", "")
            if branch and repo:
                artifacts["git_diff"] = get_git_diff(repo, branch, base)

        # Load previous gate stderr for retries
        if stage_run.get("attempt", 1) > 1:
            prev_runs = database.list_stage_runs(
                conn, task_id=task["id"], stage=stage,
            )
            for prev in reversed(prev_runs):
                prev_dict = _row_to_dict(prev)
                if prev_dict["id"] != stage_run["id"] and prev_dict.get("gate_stderr"):
                    artifacts["previous_gate_stderr"] = prev_dict["gate_stderr"]
                    break

        return artifacts

    def get_status(self) -> dict:
        """Return engine status: running state, current task, queue depth."""
        conn = database.get_connection(self.db_path)
        try:
            queued_runs = database.list_stage_runs(conn, status="queued")
            backlog_tasks = database.list_tasks(conn, status="backlog")
            return {
                "running": self.running,
                "current_task_id": self.current_task_id,
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

            avg_duration = (
                sum(durations) / len(durations) if durations else None
            )

            return {
                "total_tasks": total_tasks,
                "tasks_by_status": tasks_by_status,
                "total_stage_runs": total_stage_runs,
                "stage_runs_by_status": runs_by_status,
                "avg_stage_duration_seconds": avg_duration,
            }
        finally:
            conn.close()

    def _log(
        self,
        level: str,
        message: str,
        task_id: str | None = None,
        stage_run_id: str | None = None,
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
                )
            finally:
                conn.close()
        except Exception:
            logger.exception("Failed to write to run_log")
