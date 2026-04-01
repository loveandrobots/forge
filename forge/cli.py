"""CLI commands for Forge."""

from __future__ import annotations

import argparse
import sys

from forge import database
from forge.config import DB_PATH


def main(argv: list[str] | None = None) -> None:
    """Entry point for the Forge CLI."""
    parser = argparse.ArgumentParser(
        prog="forge", description="Forge pipeline orchestrator"
    )
    sub = parser.add_subparsers(dest="command")

    # migrate
    sub.add_parser("migrate", help="Initialize database schema")

    # init-project
    init_p = sub.add_parser("init-project", help="Register a new project")
    init_p.add_argument("--name", required=True, help="Project name (unique)")
    init_p.add_argument("--repo-path", required=True, help="Path to project repository")
    init_p.add_argument("--default-branch", default="main", help="Default git branch")
    init_p.add_argument(
        "--gate-dir", default="gates", help="Directory containing gate scripts"
    )
    init_p.add_argument(
        "--skills", default=None, help="Comma-separated skill references"
    )
    init_p.add_argument(
        "--pause-after-completion",
        action="store_true",
        default=False,
        help="Pause the engine after completing tasks for this project",
    )

    # update-project
    update_p = sub.add_parser("update-project", help="Update an existing project")
    update_p.add_argument("--name", required=True, help="Project name to update")
    pause_group = update_p.add_mutually_exclusive_group()
    pause_group.add_argument(
        "--pause-after-completion",
        action="store_true",
        dest="pause_after_completion",
        default=None,
        help="Enable auto-pause after task completion",
    )
    pause_group.add_argument(
        "--no-pause-after-completion",
        action="store_false",
        dest="pause_after_completion",
        help="Disable auto-pause after task completion",
    )

    # list-projects
    sub.add_parser("list-projects", help="List registered projects")

    # add-task
    add_t = sub.add_parser("add-task", help="Add a task to the backlog")
    add_t.add_argument("--project", required=True, help="Project name")
    add_t.add_argument("--title", required=True, help="Task title")
    add_t.add_argument("--description", default="", help="Task description")
    add_t.add_argument(
        "--priority", type=int, default=0, help="Priority (higher = more urgent)"
    )

    # reset-task
    reset_p = sub.add_parser("reset-task", help="Reset a task to restart from a stage")
    reset_p.add_argument("task_id", help="Task ID to reset")
    reset_p.add_argument(
        "--from-stage",
        default="spec",
        choices=["spec", "plan", "implement", "review"],
        help="Stage to restart from (default: spec)",
    )

    # serve
    serve_p = sub.add_parser("serve", help="Start the Forge server")
    serve_p.add_argument("--host", default="0.0.0.0", help="Bind host")
    serve_p.add_argument("--port", type=int, default=8000, help="Bind port")

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == "migrate":
        _cmd_migrate()
    elif args.command == "init-project":
        _cmd_init_project(args)
    elif args.command == "update-project":
        _cmd_update_project(args)
    elif args.command == "list-projects":
        _cmd_list_projects()
    elif args.command == "add-task":
        _cmd_add_task(args)
    elif args.command == "reset-task":
        _cmd_reset_task(args)
    elif args.command == "serve":
        _cmd_serve(args)


def _cmd_migrate() -> None:
    conn = database.get_connection(str(DB_PATH))
    try:
        database.migrate(conn)
        print("Database migrated successfully.")
    finally:
        conn.close()


def _cmd_init_project(args: argparse.Namespace) -> None:
    conn = database.get_connection(str(DB_PATH))
    try:
        database.migrate(conn)
        existing = database.get_project_by_name(conn, args.name)
        if existing:
            print(f"Error: project '{args.name}' already exists.", file=sys.stderr)
            sys.exit(1)
        skill_refs = (
            [s.strip() for s in args.skills.split(",")] if args.skills else None
        )
        project_id = database.insert_project(
            conn,
            name=args.name,
            repo_path=args.repo_path,
            default_branch=args.default_branch,
            gate_dir=args.gate_dir,
            skill_refs=skill_refs,
            pause_after_completion=args.pause_after_completion,
        )
        print(f"Project '{args.name}' created (id={project_id}).")
    finally:
        conn.close()


def _cmd_update_project(args: argparse.Namespace) -> None:
    conn = database.get_connection(str(DB_PATH))
    try:
        database.migrate(conn)
        project = database.get_project_by_name(conn, args.name)
        if not project:
            print(f"Error: project '{args.name}' not found.", file=sys.stderr)
            sys.exit(1)
        kwargs: dict = {}
        if args.pause_after_completion is not None:
            kwargs["pause_after_completion"] = args.pause_after_completion
        if not kwargs:
            print("No updates specified.", file=sys.stderr)
            sys.exit(1)
        database.update_project(conn, project["id"], **kwargs)
        print(f"Project '{args.name}' updated.")
    finally:
        conn.close()


def _cmd_list_projects() -> None:
    conn = database.get_connection(str(DB_PATH))
    try:
        database.migrate(conn)
        projects = database.list_projects(conn)
        if not projects:
            print("No projects registered.")
            return
        # Print as a simple table
        header = f"{'NAME':<30} {'BRANCH':<15} {'REPO PATH'}"
        print(header)
        print("-" * len(header))
        for p in projects:
            print(f"{p['name']:<30} {p['default_branch']:<15} {p['repo_path']}")
    finally:
        conn.close()


def _cmd_add_task(args: argparse.Namespace) -> None:
    conn = database.get_connection(str(DB_PATH))
    try:
        database.migrate(conn)
        project = database.get_project_by_name(conn, args.project)
        if not project:
            print(f"Error: project '{args.project}' not found.", file=sys.stderr)
            sys.exit(1)
        task_id = database.insert_task(
            conn,
            project_id=project["id"],
            title=args.title,
            description=args.description,
            priority=args.priority,
        )
        print(f"Task '{args.title}' added to '{args.project}' (id={task_id}).")
    finally:
        conn.close()


_RESETTABLE_STATUSES = {"needs_human", "failed", "paused"}


def _cmd_reset_task(args: argparse.Namespace) -> None:
    conn = database.get_connection(str(DB_PATH))
    try:
        database.migrate(conn)
        task = database.get_task(conn, args.task_id)
        if not task:
            print(f"Error: task '{args.task_id}' not found.", file=sys.stderr)
            sys.exit(1)
        if task["status"] not in _RESETTABLE_STATUSES:
            print(
                f"Error: cannot reset a task with status '{task['status']}'. "
                "Only needs_human, failed, or paused tasks can be reset.",
                file=sys.stderr,
            )
            sys.exit(1)
        database.reset_task(conn, args.task_id, args.from_stage, task["title"])
        print(
            f"Task '{task['title']}' reset to {args.from_stage} stage (id={args.task_id})."
        )
    finally:
        conn.close()


def _cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    uvicorn.run("forge.main:app", host=args.host, port=args.port, reload=False)
