"""Tests for forge.cli."""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest

from forge import database
from forge.cli import main


@pytest.fixture()
def db_path(tmp_path):
    """Return a temporary database path and patch DB_PATH to use it."""
    path = tmp_path / "test.db"
    with patch("forge.cli.DB_PATH", path):
        yield path


def _get_conn(db_path) -> sqlite3.Connection:
    conn = database.get_connection(str(db_path))
    return conn


class TestMigrate:
    def test_migrate_creates_tables(self, db_path, capsys):
        main(["migrate"])
        out = capsys.readouterr().out
        assert "migrated successfully" in out

        conn = _get_conn(db_path)
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = {row["name"] for row in cur.fetchall()}
        conn.close()
        assert "projects" in tables
        assert "tasks" in tables


class TestInitProject:
    def test_creates_project(self, db_path, capsys):
        main(["migrate"])
        main(["init-project", "--name", "TestProj", "--repo-path", "/tmp/repo"])
        out = capsys.readouterr().out
        assert "TestProj" in out
        assert "created" in out

        conn = _get_conn(db_path)
        proj = database.get_project_by_name(conn, "TestProj")
        conn.close()
        assert proj is not None
        assert proj["repo_path"] == "/tmp/repo"
        assert proj["default_branch"] == "main"

    def test_duplicate_name_fails(self, db_path, capsys):
        main(["migrate"])
        main(["init-project", "--name", "Dup", "--repo-path", "/tmp/a"])
        with pytest.raises(SystemExit, match="1"):
            main(["init-project", "--name", "Dup", "--repo-path", "/tmp/b"])
        err = capsys.readouterr().err
        assert "already exists" in err

    def test_custom_branch_and_gate_dir(self, db_path, capsys):
        main(["migrate"])
        main([
            "init-project", "--name", "Custom",
            "--repo-path", "/tmp/repo",
            "--default-branch", "develop",
            "--gate-dir", "ci/gates",
        ])
        conn = _get_conn(db_path)
        proj = database.get_project_by_name(conn, "Custom")
        conn.close()
        assert proj["default_branch"] == "develop"
        assert proj["gate_dir"] == "ci/gates"

    def test_skills_flag(self, db_path, capsys):
        main(["migrate"])
        main([
            "init-project", "--name", "Skilled",
            "--repo-path", "/tmp/repo",
            "--skills", "python,testing",
        ])
        conn = _get_conn(db_path)
        proj = database.get_project_by_name(conn, "Skilled")
        conn.close()
        assert proj["skill_refs"] is not None
        import json
        skills = json.loads(proj["skill_refs"])
        assert skills == ["python", "testing"]


class TestAddTask:
    def test_creates_task(self, db_path, capsys):
        main(["migrate"])
        main(["init-project", "--name", "Proj", "--repo-path", "/tmp/repo"])
        main(["add-task", "--project", "Proj", "--title", "Do stuff", "--priority", "5"])
        out = capsys.readouterr().out
        assert "Do stuff" in out
        assert "added" in out

        conn = _get_conn(db_path)
        tasks = database.list_tasks(conn, status="backlog")
        conn.close()
        assert len(tasks) == 1
        assert tasks[0]["title"] == "Do stuff"
        assert tasks[0]["priority"] == 5

    def test_missing_project_fails(self, db_path, capsys):
        main(["migrate"])
        with pytest.raises(SystemExit, match="1"):
            main(["add-task", "--project", "NoSuch", "--title", "X"])
        err = capsys.readouterr().err
        assert "not found" in err


class TestListProjects:
    def test_no_projects(self, db_path, capsys):
        main(["migrate"])
        main(["list-projects"])
        out = capsys.readouterr().out
        assert "No projects" in out

    def test_lists_projects(self, db_path, capsys):
        main(["migrate"])
        main(["init-project", "--name", "Alpha", "--repo-path", "/tmp/a"])
        main(["init-project", "--name", "Beta", "--repo-path", "/tmp/b"])
        capsys.readouterr()  # clear previous output
        main(["list-projects"])
        out = capsys.readouterr().out
        assert "Alpha" in out
        assert "Beta" in out
        assert "/tmp/a" in out
        assert "/tmp/b" in out
