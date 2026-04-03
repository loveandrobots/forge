"""Tests for forge.cli."""

from __future__ import annotations

import sqlite3

import pytest

from forge import database
from forge.cli import main


@pytest.fixture()
def db_path(tmp_path):
    """Return the temporary database path (patching handled by conftest)."""
    return tmp_path / "test.db"


def _get_conn(db_path) -> sqlite3.Connection:
    conn = database.get_connection(str(db_path))
    return conn


class TestMigrate:
    def test_migrate_creates_tables(self, db_path, capsys):
        main(["migrate"])
        out = capsys.readouterr().out
        assert "migrated successfully" in out

        conn = _get_conn(db_path)
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
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
        main(
            [
                "init-project",
                "--name",
                "Custom",
                "--repo-path",
                "/tmp/repo",
                "--default-branch",
                "develop",
                "--gate-dir",
                "ci/gates",
            ]
        )
        conn = _get_conn(db_path)
        proj = database.get_project_by_name(conn, "Custom")
        conn.close()
        assert proj["default_branch"] == "develop"
        assert proj["gate_dir"] == "ci/gates"

    def test_skills_flag(self, db_path, capsys):
        main(["migrate"])
        main(
            [
                "init-project",
                "--name",
                "Skilled",
                "--repo-path",
                "/tmp/repo",
                "--skills",
                "python,testing",
            ]
        )
        conn = _get_conn(db_path)
        proj = database.get_project_by_name(conn, "Skilled")
        conn.close()
        assert proj["skill_refs"] is not None
        import json

        skills = json.loads(proj["skill_refs"])
        assert skills == ["python", "testing"]


class TestUpdateProject:
    def test_enable_pause_after_completion(self, db_path, capsys):
        main(["migrate"])
        main(["init-project", "--name", "Proj", "--repo-path", "/tmp/repo"])
        main(["update-project", "--name", "Proj", "--pause-after-completion"])
        out = capsys.readouterr().out
        assert "updated" in out

        conn = _get_conn(db_path)
        proj = database.get_project_by_name(conn, "Proj")
        conn.close()
        assert proj["pause_after_completion"] == 1

    def test_disable_pause_after_completion(self, db_path, capsys):
        main(["migrate"])
        main(
            [
                "init-project",
                "--name",
                "Proj",
                "--repo-path",
                "/tmp/repo",
                "--pause-after-completion",
            ]
        )
        main(["update-project", "--name", "Proj", "--no-pause-after-completion"])
        capsys.readouterr()

        conn = _get_conn(db_path)
        proj = database.get_project_by_name(conn, "Proj")
        conn.close()
        assert proj["pause_after_completion"] == 0

    def test_update_project_not_found(self, db_path, capsys):
        main(["migrate"])
        with pytest.raises(SystemExit, match="1"):
            main(
                ["update-project", "--name", "NonExistent", "--pause-after-completion"]
            )
        err = capsys.readouterr().err
        assert "not found" in err


class TestInitProjectPauseAfterCompletion:
    def test_pause_after_completion_flag(self, db_path, capsys):
        main(["migrate"])
        main(
            [
                "init-project",
                "--name",
                "PauseProj",
                "--repo-path",
                "/tmp/repo",
                "--pause-after-completion",
            ]
        )
        conn = _get_conn(db_path)
        proj = database.get_project_by_name(conn, "PauseProj")
        conn.close()
        assert proj["pause_after_completion"] == 1

    def test_default_no_pause(self, db_path, capsys):
        main(["migrate"])
        main(["init-project", "--name", "NoPause", "--repo-path", "/tmp/repo"])
        conn = _get_conn(db_path)
        proj = database.get_project_by_name(conn, "NoPause")
        conn.close()
        assert proj["pause_after_completion"] == 0


class TestAddTask:
    def test_creates_task(self, db_path, capsys):
        main(["migrate"])
        main(["init-project", "--name", "Proj", "--repo-path", "/tmp/repo"])
        main(
            ["add-task", "--project", "Proj", "--title", "Do stuff", "--priority", "5"]
        )
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


class TestAddTaskMaxRetries:
    """CLI add-task should use configured default_max_retries, not the hardcoded default."""

    def test_uses_configured_max_retries(self, db_path, tmp_path, monkeypatch, capsys):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("engine:\n  default_max_retries: 7\n")
        monkeypatch.setattr("forge.cli.CONFIG_PATH", config_file)

        main(["init-project", "--name", "Proj", "--repo-path", "/tmp/repo"])
        main(["add-task", "--project", "Proj", "--title", "Retry task"])
        capsys.readouterr()

        conn = _get_conn(db_path)
        tasks = database.list_tasks(conn, status="backlog")
        conn.close()
        assert len(tasks) == 1
        assert tasks[0]["max_retries"] == 7

    def test_default_max_retries_without_config(self, db_path, tmp_path, monkeypatch, capsys):
        """Without a config file, max_retries should use the built-in default (3)."""
        config_file = tmp_path / "nonexistent_config.yaml"
        monkeypatch.setattr("forge.cli.CONFIG_PATH", config_file)

        main(["init-project", "--name", "Proj", "--repo-path", "/tmp/repo"])
        main(["add-task", "--project", "Proj", "--title", "Default retry"])
        capsys.readouterr()

        conn = _get_conn(db_path)
        tasks = database.list_tasks(conn, status="backlog")
        conn.close()
        assert len(tasks) == 1
        assert tasks[0]["max_retries"] == 3


class TestAddTaskFlow:
    def test_add_task_with_flow_quick(self, db_path, capsys):
        main(["migrate"])
        main(["init-project", "--name", "Proj", "--repo-path", "/tmp/repo"])
        main(
            [
                "add-task",
                "--project",
                "Proj",
                "--title",
                "Quick task",
                "--flow",
                "quick",
            ]
        )
        out = capsys.readouterr().out
        assert "Quick task" in out

        conn = _get_conn(db_path)
        tasks = database.list_tasks(conn, status="backlog")
        conn.close()
        assert len(tasks) == 1
        assert tasks[0]["flow"] == "quick"

    def test_add_task_default_flow(self, db_path, capsys):
        main(["migrate"])
        main(["init-project", "--name", "Proj", "--repo-path", "/tmp/repo"])
        main(["add-task", "--project", "Proj", "--title", "Default flow"])
        capsys.readouterr()

        conn = _get_conn(db_path)
        tasks = database.list_tasks(conn, status="backlog")
        conn.close()
        assert len(tasks) == 1
        assert tasks[0]["flow"] == "standard"


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
