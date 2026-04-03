"""SQLite schema, migrations, and query functions."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone


def get_connection(db_path: str = "") -> sqlite3.Connection:
    """Return a sqlite3 Connection with Row factory and WAL mode."""
    if not db_path:
        from forge.config import DB_PATH

        db_path = str(DB_PATH)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Create all tables if they don't exist (idempotent)."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            repo_path TEXT NOT NULL,
            default_branch TEXT NOT NULL DEFAULT 'main',
            gate_dir TEXT NOT NULL DEFAULT 'gates',
            skill_refs TEXT,
            created_at TEXT NOT NULL,
            config TEXT,
            pause_after_completion INTEGER NOT NULL DEFAULT 0,
            stage_timeouts TEXT
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id),
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            priority INTEGER NOT NULL DEFAULT 0,
            current_stage TEXT,
            status TEXT NOT NULL DEFAULT 'backlog',
            branch_name TEXT,
            spec_path TEXT,
            plan_path TEXT,
            review_path TEXT,
            skill_overrides TEXT,
            max_retries INTEGER NOT NULL DEFAULT 3,
            flow TEXT NOT NULL DEFAULT 'standard',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS stage_runs (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(id),
            stage TEXT NOT NULL,
            attempt INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            prompt_sent TEXT,
            started_at TEXT,
            finished_at TEXT,
            duration_seconds REAL,
            claude_output TEXT,
            artifacts_produced TEXT,
            gate_name TEXT,
            gate_exit_code INTEGER,
            gate_stdout TEXT,
            gate_stderr TEXT,
            tokens_used INTEGER,
            error_message TEXT
        );

        CREATE TABLE IF NOT EXISTS task_links (
            id TEXT PRIMARY KEY,
            source_task_id TEXT NOT NULL REFERENCES tasks(id),
            target_task_id TEXT NOT NULL REFERENCES tasks(id),
            link_type TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS run_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            level TEXT NOT NULL,
            message TEXT NOT NULL,
            task_id TEXT,
            stage_run_id TEXT,
            metadata TEXT
        );
    """)

    # Migrations for existing databases
    try:
        conn.execute(
            "ALTER TABLE projects ADD COLUMN pause_after_completion INTEGER NOT NULL DEFAULT 0"
        )
        # Enable pause_after_completion for Forge's own project on first migration
        conn.execute("UPDATE projects SET pause_after_completion = 1 WHERE name = 'Forge'")
    except sqlite3.OperationalError:
        pass  # Column already exists

    try:
        conn.execute("ALTER TABLE projects ADD COLUMN stage_timeouts TEXT")
    except sqlite3.OperationalError:
        pass  # Column already exists

    try:
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN flow TEXT NOT NULL DEFAULT 'standard'"
        )
    except sqlite3.OperationalError:
        pass  # Column already exists

    try:
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN escalated_from_quick INTEGER NOT NULL DEFAULT 0"
        )
    except sqlite3.OperationalError:
        pass  # Column already exists

    try:
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN parent_task_id TEXT REFERENCES tasks(id)"
        )
    except sqlite3.OperationalError:
        pass  # Column already exists

    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN epic_status TEXT")
    except sqlite3.OperationalError:
        pass  # Column already exists

    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


def _json_encode(value: list | dict | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value)


def _json_decode(value: str | None) -> list | dict | None:
    if value is None:
        return None
    return json.loads(value)


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


def insert_project(
    conn: sqlite3.Connection,
    *,
    name: str,
    repo_path: str,
    default_branch: str = "main",
    gate_dir: str = "gates",
    skill_refs: list[str] | None = None,
    config: dict | None = None,
    pause_after_completion: bool = False,
    stage_timeouts: dict[str, int] | None = None,
) -> str:
    """Insert a new project. Returns the project id."""
    project_id = _new_id()
    conn.execute(
        """INSERT INTO projects (id, name, repo_path, default_branch, gate_dir, skill_refs, created_at, config, pause_after_completion, stage_timeouts)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            project_id,
            name,
            repo_path,
            default_branch,
            gate_dir,
            _json_encode(skill_refs),
            _now(),
            _json_encode(config),
            int(pause_after_completion),
            _json_encode(stage_timeouts),
        ),
    )
    conn.commit()
    return project_id


def get_project(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row | None:
    """Look up a project by primary key."""
    cur = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
    return cur.fetchone()


def get_project_by_name(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    """Look up a project by unique name."""
    cur = conn.execute("SELECT * FROM projects WHERE name = ?", (name,))
    return cur.fetchone()


def list_projects(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """List all projects ordered by name."""
    cur = conn.execute("SELECT * FROM projects ORDER BY name")
    return cur.fetchall()


_SENTINEL = object()


def update_project(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    name: str | None = None,
    repo_path: str | None = None,
    default_branch: str | None = None,
    gate_dir: str | None = None,
    skill_refs: list[str] | None = None,
    config: dict | None = None,
    pause_after_completion: bool | None = _SENTINEL,
    stage_timeouts: dict[str, int] | None = _SENTINEL,
) -> bool:
    """Update only the provided fields. Returns True if a row was modified."""
    fields: list[str] = []
    values: list = []
    for col, val, encoder in [
        ("name", name, None),
        ("repo_path", repo_path, None),
        ("default_branch", default_branch, None),
        ("gate_dir", gate_dir, None),
        ("skill_refs", skill_refs, _json_encode),
        ("config", config, _json_encode),
    ]:
        if val is not None:
            fields.append(f"{col} = ?")
            values.append(encoder(val) if encoder else val)
    if pause_after_completion is not _SENTINEL:
        fields.append("pause_after_completion = ?")
        values.append(int(pause_after_completion))
    if stage_timeouts is not _SENTINEL:
        fields.append("stage_timeouts = ?")
        values.append(_json_encode(stage_timeouts))
    if not fields:
        return False
    values.append(project_id)
    cur = conn.execute(
        f"UPDATE projects SET {', '.join(fields)} WHERE id = ?",
        values,
    )
    conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


def insert_task_no_commit(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    title: str,
    description: str = "",
    priority: int = 0,
    skill_overrides: list[str] | None = None,
    max_retries: int = 3,
    flow: str = "standard",
    parent_task_id: str | None = None,
    epic_status: str | None = None,
) -> str:
    """Insert a new task with status='backlog' without committing. Returns the task id."""
    from forge.config import VALID_FLOWS

    if flow not in VALID_FLOWS:
        raise ValueError(
            f"Invalid flow: {flow!r}. Must be one of {VALID_FLOWS}"
        )
    task_id = _new_id()
    now = _now()
    conn.execute(
        """INSERT INTO tasks
           (id, project_id, title, description, priority, status, skill_overrides, max_retries, flow, parent_task_id, epic_status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, 'backlog', ?, ?, ?, ?, ?, ?, ?)""",
        (
            task_id,
            project_id,
            title,
            description,
            priority,
            _json_encode(skill_overrides),
            max_retries,
            flow,
            parent_task_id,
            epic_status,
            now,
            now,
        ),
    )
    return task_id


def insert_task(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    title: str,
    description: str = "",
    priority: int = 0,
    skill_overrides: list[str] | None = None,
    max_retries: int = 3,
    flow: str = "standard",
    parent_task_id: str | None = None,
    epic_status: str | None = None,
) -> str:
    """Insert a new task with status='backlog'. Returns the task id."""
    task_id = insert_task_no_commit(
        conn,
        project_id=project_id,
        title=title,
        description=description,
        priority=priority,
        skill_overrides=skill_overrides,
        max_retries=max_retries,
        flow=flow,
        parent_task_id=parent_task_id,
        epic_status=epic_status,
    )
    conn.commit()
    return task_id


def get_task(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row | None:
    """Look up a task by primary key."""
    cur = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
    return cur.fetchone()


def list_tasks(
    conn: sqlite3.Connection,
    *,
    project_id: str | None = None,
    status: str | None = None,
    priority_gte: int | None = None,
) -> list[sqlite3.Row]:
    """List tasks with optional filters, ordered by priority DESC, created_at ASC."""
    clauses: list[str] = []
    params: list = []
    if project_id is not None:
        clauses.append("project_id = ?")
        params.append(project_id)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if priority_gte is not None:
        clauses.append("priority >= ?")
        params.append(priority_gte)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    cur = conn.execute(
        f"SELECT * FROM tasks {where} ORDER BY priority DESC, created_at ASC",
        params,
    )
    return cur.fetchall()


def update_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    title: str | None = None,
    description: str | None = None,
    priority: int | None = None,
    status: str | None = None,
    current_stage: str | None = None,
    branch_name: str | None = None,
    spec_path: str | None = None,
    plan_path: str | None = None,
    review_path: str | None = None,
    skill_overrides: list[str] | None = None,
    completed_at: str | None = None,
    flow: str | None = None,
    escalated_from_quick: int | None = None,
    epic_status: str | None = None,
) -> bool:
    """Update only the provided fields. Always sets updated_at. Returns True if modified."""
    from forge.config import FLOW_STAGES, STAGES, VALID_FLOWS

    # Validate flow if provided
    if flow is not None and flow not in VALID_FLOWS:
        raise ValueError(
            f"Invalid flow: {flow!r}. Must be one of {VALID_FLOWS}"
        )

    # Validate current_stage against the effective flow
    if current_stage is not None and current_stage != "":
        if flow is not None:
            effective_flow = flow
        else:
            row = conn.execute(
                "SELECT flow FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            effective_flow = row["flow"] if row and row["flow"] else "standard"
        valid_stages = FLOW_STAGES.get(effective_flow, STAGES)
        if current_stage not in valid_stages:
            raise ValueError(
                f"Stage {current_stage!r} is not a valid stage for flow {effective_flow!r}. "
                f"Valid stages: {valid_stages}"
            )

    fields: list[str] = ["updated_at = ?"]
    values: list = [_now()]
    for col, val, encoder in [
        ("title", title, None),
        ("description", description, None),
        ("priority", priority, None),
        ("status", status, None),
        ("current_stage", current_stage, None),
        ("branch_name", branch_name, None),
        ("spec_path", spec_path, None),
        ("plan_path", plan_path, None),
        ("review_path", review_path, None),
        ("skill_overrides", skill_overrides, _json_encode),
        ("completed_at", completed_at, None),
        ("flow", flow, None),
        ("escalated_from_quick", escalated_from_quick, None),
        ("epic_status", epic_status, None),
    ]:
        if val is not None:
            fields.append(f"{col} = ?")
            values.append(encoder(val) if encoder else val)
    values.append(task_id)
    cur = conn.execute(
        f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?",
        values,
    )
    conn.commit()
    return cur.rowcount > 0


def delete_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """Delete a task only if status='backlog'. Returns True if deleted."""
    cur = conn.execute(
        "DELETE FROM tasks WHERE id = ? AND status = 'backlog'",
        (task_id,),
    )
    conn.commit()
    return cur.rowcount > 0


def get_next_queued_task(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Find the highest-priority active task that has a queued stage_run."""
    cur = conn.execute(
        """SELECT t.* FROM tasks t
           JOIN stage_runs sr ON sr.task_id = t.id
           WHERE t.status = 'active' AND sr.status = 'queued'
           ORDER BY t.priority DESC, t.created_at ASC
           LIMIT 1""",
    )
    return cur.fetchone()


# ---------------------------------------------------------------------------
# Parent-child (epic) queries
# ---------------------------------------------------------------------------


def get_child_tasks(
    conn: sqlite3.Connection, parent_task_id: str
) -> list[sqlite3.Row]:
    """Return all child tasks for a given parent, ordered by priority DESC, created_at ASC."""
    cur = conn.execute(
        "SELECT * FROM tasks WHERE parent_task_id = ? ORDER BY priority DESC, created_at ASC",
        (parent_task_id,),
    )
    return cur.fetchall()


def get_parent_task(
    conn: sqlite3.Connection, task_id: str
) -> sqlite3.Row | None:
    """Return the parent task for a given child task, or None."""
    cur = conn.execute(
        """SELECT parent.* FROM tasks child
           JOIN tasks parent ON child.parent_task_id = parent.id
           WHERE child.id = ?""",
        (task_id,),
    )
    return cur.fetchone()


def get_parent_tasks_batch(
    conn: sqlite3.Connection, task_ids: list[str]
) -> dict[str, sqlite3.Row]:
    """Return a mapping of child task_id → parent Row for all given child task IDs."""
    if not task_ids:
        return {}
    placeholders = ",".join("?" for _ in task_ids)
    cur = conn.execute(
        f"""SELECT child.id AS child_id, parent.*
            FROM tasks child
            JOIN tasks parent ON child.parent_task_id = parent.id
            WHERE child.id IN ({placeholders})""",
        task_ids,
    )
    return {row["child_id"]: row for row in cur.fetchall()}


def get_child_counts_batch(
    conn: sqlite3.Connection, parent_task_ids: list[str]
) -> dict[str, dict[str, int]]:
    """Return {parent_id: {"total": N, "done": M}} for all given parent IDs."""
    if not parent_task_ids:
        return {}
    placeholders = ",".join("?" for _ in parent_task_ids)
    cur = conn.execute(
        f"""SELECT parent_task_id,
                   COUNT(*) AS total,
                   SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done
            FROM tasks
            WHERE parent_task_id IN ({placeholders})
            GROUP BY parent_task_id""",
        parent_task_ids,
    )
    return {
        row["parent_task_id"]: {"total": row["total"], "done": row["done"]}
        for row in cur.fetchall()
    }


def all_children_complete(conn: sqlite3.Connection, parent_task_id: str) -> bool:
    """Return True when all children of a parent exist and have status='done'."""
    cur = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE parent_task_id = ?",
        (parent_task_id,),
    )
    total = cur.fetchone()[0]
    if total == 0:
        return False
    cur = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE parent_task_id = ? AND status != 'done'",
        (parent_task_id,),
    )
    incomplete = cur.fetchone()[0]
    return incomplete == 0


# ---------------------------------------------------------------------------
# Stage runs
# ---------------------------------------------------------------------------


def insert_stage_run(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    stage: str,
    attempt: int,
    status: str = "queued",
    prompt_sent: str | None = None,
) -> str:
    """Insert a new stage run. Returns the stage_run id."""
    sr_id = _new_id()
    conn.execute(
        """INSERT INTO stage_runs (id, task_id, stage, attempt, status, prompt_sent)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (sr_id, task_id, stage, attempt, status, prompt_sent),
    )
    conn.commit()
    return sr_id


def get_stage_run(conn: sqlite3.Connection, stage_run_id: str) -> sqlite3.Row | None:
    """Look up a stage run by primary key."""
    cur = conn.execute("SELECT * FROM stage_runs WHERE id = ?", (stage_run_id,))
    return cur.fetchone()


def list_stage_runs(
    conn: sqlite3.Connection,
    *,
    task_id: str | None = None,
    stage: str | None = None,
    status: str | None = None,
) -> list[sqlite3.Row]:
    """List stage runs with optional filters, ordered by started_at ASC."""
    clauses: list[str] = []
    params: list = []
    if task_id is not None:
        clauses.append("task_id = ?")
        params.append(task_id)
    if stage is not None:
        clauses.append("stage = ?")
        params.append(stage)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    cur = conn.execute(
        f"SELECT * FROM stage_runs {where} ORDER BY started_at ASC",
        params,
    )
    return cur.fetchall()


def update_stage_run(
    conn: sqlite3.Connection,
    stage_run_id: str,
    *,
    status: str | None = None,
    prompt_sent: str | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
    duration_seconds: float | None = None,
    claude_output: str | None = None,
    artifacts_produced: list[str] | None = None,
    gate_name: str | None = None,
    gate_exit_code: int | None = None,
    gate_stdout: str | None = None,
    gate_stderr: str | None = None,
    tokens_used: int | None = None,
    error_message: str | None = None,
) -> bool:
    """Update only the provided fields. Returns True if modified."""
    fields: list[str] = []
    values: list = []
    for col, val, encoder in [
        ("status", status, None),
        ("prompt_sent", prompt_sent, None),
        ("started_at", started_at, None),
        ("finished_at", finished_at, None),
        ("duration_seconds", duration_seconds, None),
        ("claude_output", claude_output, None),
        ("artifacts_produced", artifacts_produced, _json_encode),
        ("gate_name", gate_name, None),
        ("gate_exit_code", gate_exit_code, None),
        ("gate_stdout", gate_stdout, None),
        ("gate_stderr", gate_stderr, None),
        ("tokens_used", tokens_used, None),
        ("error_message", error_message, None),
    ]:
        if val is not None:
            fields.append(f"{col} = ?")
            values.append(encoder(val) if encoder else val)
    if not fields:
        return False
    values.append(stage_run_id)
    cur = conn.execute(
        f"UPDATE stage_runs SET {', '.join(fields)} WHERE id = ?",
        values,
    )
    conn.commit()
    return cur.rowcount > 0


def count_tasks_by_exact_status(conn: sqlite3.Connection, status: str) -> int:
    """Count tasks matching an exact status value."""
    cur = conn.execute("SELECT COUNT(*) FROM tasks WHERE status = ?", (status,))
    return cur.fetchone()[0]


def get_avg_duration_by_stage(conn: sqlite3.Connection) -> dict[str, float]:
    """Return average duration_seconds grouped by stage for finished runs.

    Includes all runs where duration_seconds IS NOT NULL, regardless of status.
    """
    cur = conn.execute(
        """SELECT stage, AVG(duration_seconds)
           FROM stage_runs
           WHERE duration_seconds IS NOT NULL
           GROUP BY stage""",
    )
    return {row[0]: row[1] for row in cur.fetchall()}


def get_bounce_rate_by_stage(conn: sqlite3.Connection) -> dict[str, float]:
    """Return bounce rate (bounced / total) grouped by stage."""
    cur = conn.execute(
        """SELECT stage,
                  SUM(CASE WHEN status = 'bounced' THEN 1 ELSE 0 END) * 1.0 / COUNT(*)
           FROM stage_runs
           GROUP BY stage""",
    )
    return {row[0]: row[1] for row in cur.fetchall()}


def get_retry_count(conn: sqlite3.Connection, task_id: str, stage: str) -> int:
    """Count stage_runs for this task+stage with status in ('bounced', 'error')."""
    cur = conn.execute(
        """SELECT COUNT(*) FROM stage_runs
           WHERE task_id = ? AND stage = ? AND status IN ('bounced', 'error')""",
        (task_id, stage),
    )
    return cur.fetchone()[0]


def reset_task(
    conn: sqlite3.Connection,
    task_id: str,
    from_stage: str,
    task_title: str,
) -> str:
    """Reset a task to a clean state starting from the given stage.

    Deletes all stage_runs, updates task status/stage, creates a fresh
    queued stage_run, and logs the action. All within one transaction.
    Returns the new stage_run id.
    """
    conn.execute("BEGIN")
    try:
        conn.execute("DELETE FROM stage_runs WHERE task_id = ?", (task_id,))
        now = _now()
        conn.execute(
            "UPDATE tasks SET status = 'active', current_stage = ?, updated_at = ? WHERE id = ?",
            (from_stage, now, task_id),
        )
        sr_id = _new_id()
        conn.execute(
            """INSERT INTO stage_runs (id, task_id, stage, attempt, status)
               VALUES (?, ?, ?, 1, 'queued')""",
            (sr_id, task_id, from_stage),
        )
        conn.execute(
            """INSERT INTO run_log (timestamp, level, message, task_id)
               VALUES (?, 'info', ?, ?)""",
            (now, f"Task '{task_title}' reset to {from_stage} stage. Previous stage_run history cleared.", task_id),
        )
        conn.commit()
        return sr_id
    except Exception:
        conn.rollback()
        raise


def get_stage_run_count(conn: sqlite3.Connection, task_id: str, stage: str) -> int:
    """Count all stage_runs for a task+stage regardless of status."""
    cur = conn.execute(
        "SELECT COUNT(*) FROM stage_runs WHERE task_id = ? AND stage = ?",
        (task_id, stage),
    )
    return cur.fetchone()[0]


def get_implement_review_retry_count(conn: sqlite3.Connection, task_id: str) -> int:
    cur = conn.execute(
        """SELECT COUNT(*) FROM stage_runs
           WHERE task_id = ? AND stage IN ('implement', 'review')
           AND status IN ('bounced', 'error')""",
        (task_id,),
    )
    return cur.fetchone()[0]

# ---------------------------------------------------------------------------
# Task links
# ---------------------------------------------------------------------------

VALID_LINK_TYPES = {"blocks", "created_by", "follows", "related"}


def insert_task_link_no_commit(
    conn: sqlite3.Connection,
    *,
    source_task_id: str,
    target_task_id: str,
    link_type: str,
) -> str:
    """Insert a task link without committing. Returns the link id."""
    if link_type not in VALID_LINK_TYPES:
        raise ValueError(
            f"Invalid link_type: {link_type!r}. Must be one of {VALID_LINK_TYPES}"
        )
    link_id = _new_id()
    conn.execute(
        """INSERT INTO task_links (id, source_task_id, target_task_id, link_type, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (link_id, source_task_id, target_task_id, link_type, _now()),
    )
    return link_id


def insert_task_link(
    conn: sqlite3.Connection,
    *,
    source_task_id: str,
    target_task_id: str,
    link_type: str,
) -> str:
    """Insert a task link. link_type must be valid. Returns the link id."""
    link_id = insert_task_link_no_commit(
        conn,
        source_task_id=source_task_id,
        target_task_id=target_task_id,
        link_type=link_type,
    )
    conn.commit()
    return link_id


def get_task_links(conn: sqlite3.Connection, task_id: str) -> list[sqlite3.Row]:
    """Return all links where task_id is either source or target, ordered by created_at."""
    cur = conn.execute(
        """SELECT * FROM task_links
           WHERE source_task_id = ? OR target_task_id = ?
           ORDER BY created_at""",
        (task_id, task_id),
    )
    return cur.fetchall()


# ---------------------------------------------------------------------------
# Run log
# ---------------------------------------------------------------------------

VALID_LOG_LEVELS = {"info", "warn", "error"}


def insert_log(
    conn: sqlite3.Connection,
    *,
    level: str,
    message: str,
    task_id: str | None = None,
    stage_run_id: str | None = None,
    metadata: dict | None = None,
) -> int:
    """Insert a log entry. Returns the autoincrement id."""
    if level not in VALID_LOG_LEVELS:
        raise ValueError(
            f"Invalid log level: {level!r}. Must be one of {VALID_LOG_LEVELS}"
        )
    cur = conn.execute(
        """INSERT INTO run_log (timestamp, level, message, task_id, stage_run_id, metadata)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (_now(), level, message, task_id, stage_run_id, _json_encode(metadata)),
    )
    conn.commit()
    return cur.lastrowid


def get_logs(
    conn: sqlite3.Connection,
    *,
    level: str | None = None,
    task_id: str | None = None,
    project_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """Get filtered, paginated log entries ordered by timestamp DESC."""
    clauses: list[str] = []
    params: list = []
    joins = ""
    if project_id is not None:
        joins = "JOIN tasks t ON run_log.task_id = t.id"
        clauses.append("t.project_id = ?")
        params.append(project_id)
    if level is not None:
        clauses.append("run_log.level = ?")
        params.append(level)
    if task_id is not None:
        clauses.append("run_log.task_id = ?")
        params.append(task_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([limit, offset])
    cur = conn.execute(
        f"SELECT run_log.* FROM run_log {joins} {where} ORDER BY run_log.timestamp DESC LIMIT ? OFFSET ?",
        params,
    )
    return cur.fetchall()


def get_logs_since(
    conn: sqlite3.Connection,
    *,
    since_id: int = 0,
    level: str | None = None,
    limit: int = 100,
) -> list[sqlite3.Row]:
    """Get log entries with id > since_id, ordered ASC by id."""
    clauses = ["id > ?"]
    params: list = [since_id]
    if level is not None:
        clauses.append("level = ?")
        params.append(level)
    params.append(limit)
    where = " AND ".join(clauses)
    return conn.execute(
        f"SELECT * FROM run_log WHERE {where} ORDER BY id ASC LIMIT ?",
        params,
    ).fetchall()


# ---------------------------------------------------------------------------
# Task cancellation helpers (shared by MCP + REST)
# ---------------------------------------------------------------------------

TERMINAL_STATUSES = frozenset({"done", "cancelled", "error"})


def cancel_single_task(
    conn: sqlite3.Connection, task_id: str, reason: str | None = None
) -> None:
    """Cancel a single task: mark running stage runs as errored, set status, log."""
    running_runs = list_stage_runs(conn, task_id=task_id, status="running")
    for sr in running_runs:
        update_stage_run(conn, sr["id"], status="error", error_message="Task cancelled")
    update_task(conn, task_id, status="cancelled")
    message = "Task cancelled"
    if reason:
        message = f"Task cancelled: {reason}"
    insert_log(conn, level="info", task_id=task_id, message=message)
