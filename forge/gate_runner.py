"""Gate runner — executes gate scripts from target project repos.

Runs post-stage gate scripts and interprets their exit codes to determine
whether a pipeline stage passed or needs to bounce.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from sqlite3 import Row

logger = logging.getLogger(__name__)


@dataclass
class GateResult:
    """Result of running a gate script."""

    passed: bool
    exit_code: int
    stdout: str
    stderr: str
    gate_name: str
    duration_seconds: float


def build_gate_env(
    task: Row,
    stage_run: Row,
    project: Row,
    artifact_path: str | None = None,
) -> dict[str, str]:
    """Assemble environment variables for the gate script.

    Sets the FORGE_* env vars per the gate contract (spec section 7).
    When *artifact_path* is provided, sets ``FORGE_ARTIFACT_PATH`` pointing
    to the structured output JSON file.
    """
    env: dict[str, str] = {
        "FORGE_TASK_ID": str(task["id"]),
        "FORGE_STAGE": str(stage_run["stage"]),
        "FORGE_ATTEMPT": str(stage_run["attempt"]),
        "FORGE_REPO_PATH": str(project["repo_path"]),
        "FORGE_BRANCH": str(task["branch_name"] or ""),
        "FORGE_SPEC_PATH": str(task["spec_path"] or ""),
        "FORGE_PLAN_PATH": str(task["plan_path"] or ""),
        "FORGE_REVIEW_PATH": str(task["review_path"] or ""),
    }
    try:
        env["FORGE_FLOW"] = str(task["flow"] or "standard")
    except (KeyError, IndexError):
        env["FORGE_FLOW"] = "standard"
    if artifact_path:
        env["FORGE_ARTIFACT_PATH"] = artifact_path
    return env


async def run_gate(
    gate_dir: str,
    stage: str,
    env_vars: dict[str, str],
) -> GateResult:
    """Execute a gate script and return the result.

    Looks for ``{gate_dir}/post-{stage}.sh``.  If the script does not exist
    the gate passes by default with a logged warning.
    """
    gate_name = f"post-{stage}.sh"
    gate_path = os.path.join(gate_dir, gate_name)

    if not os.path.isfile(gate_path):
        logger.warning("Gate script not found: %s — passing by default", gate_path)
        return GateResult(
            passed=True,
            exit_code=0,
            stdout="",
            stderr="",
            gate_name=gate_name,
            duration_seconds=0.0,
        )

    # Merge FORGE_* vars into a copy of the current environment so the
    # script can still access PATH and other system essentials.
    full_env = {**os.environ, **env_vars}

    start = time.monotonic()

    proc = await asyncio.create_subprocess_exec(
        "bash",
        gate_path,
        cwd=env_vars.get("FORGE_REPO_PATH"),
        env=full_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout_bytes, stderr_bytes = await proc.communicate()
    duration = time.monotonic() - start
    exit_code = proc.returncode or 0

    stdout_text = stdout_bytes.decode(errors="replace").strip()
    stderr_text = stderr_bytes.decode(errors="replace").strip()

    passed = exit_code == 0

    if passed:
        logger.info("Gate %s passed (%.1fs)", gate_name, duration)
    else:
        logger.warning(
            "Gate %s failed (exit %d, %.1fs): %s",
            gate_name,
            exit_code,
            duration,
            stderr_text,
        )

    return GateResult(
        passed=passed,
        exit_code=exit_code,
        stdout=stdout_text,
        stderr=stderr_text,
        gate_name=gate_name,
        duration_seconds=duration,
    )
