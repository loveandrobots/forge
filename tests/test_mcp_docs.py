"""Tests for MCP server configuration documentation."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from forge.mcp_server import build_arg_parser, build_run_kwargs

DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"
MCP_DOC = DOCS_DIR / "mcp-server.md"


@pytest.fixture()
def doc_content() -> str:
    """Read the MCP server documentation file."""
    return MCP_DOC.read_text()


class TestMcpDocExists:
    """Verify the MCP server documentation file exists and is non-empty."""

    def test_doc_file_exists(self) -> None:
        assert MCP_DOC.exists(), f"Expected doc file at {MCP_DOC}"

    def test_doc_file_not_empty(self, doc_content: str) -> None:
        assert len(doc_content.strip()) > 0


class TestMcpDocSections:
    """Verify required sections are present in the documentation."""

    def test_has_title(self, doc_content: str) -> None:
        assert "# Forge MCP Server" in doc_content

    def test_has_transports_section(self, doc_content: str) -> None:
        assert "## Transports" in doc_content

    def test_documents_stdio_transport(self, doc_content: str) -> None:
        assert "stdio" in doc_content

    def test_documents_sse_transport(self, doc_content: str) -> None:
        assert "sse" in doc_content

    def test_has_starting_section(self, doc_content: str) -> None:
        assert "## Starting the server" in doc_content

    def test_has_claude_code_section(self, doc_content: str) -> None:
        assert "Claude Code" in doc_content
        assert "stdio" in doc_content

    def test_has_claude_ai_section(self, doc_content: str) -> None:
        assert "Claude.ai" in doc_content
        assert "remote MCP" in doc_content.lower() or "SSE" in doc_content

    def test_has_available_tools_section(self, doc_content: str) -> None:
        assert "## Available tools" in doc_content

    def test_has_usage_examples_section(self, doc_content: str) -> None:
        assert "## Usage examples" in doc_content


class TestMcpDocClaudeCodeConfig:
    """Verify Claude Code configuration example is correct."""

    def test_has_mcp_json_example(self, doc_content: str) -> None:
        assert ".mcp.json" in doc_content

    def test_has_claude_desktop_config_example(self, doc_content: str) -> None:
        assert "claude_desktop_config.json" in doc_content

    def test_mcp_json_is_valid_json(self, doc_content: str) -> None:
        """Extract and validate the .mcp.json example is parseable JSON."""
        snippets = _extract_json_blocks(doc_content)
        assert len(snippets) >= 1, "Expected at least one JSON code block"
        for snippet in snippets:
            parsed = json.loads(snippet)
            assert "mcpServers" in parsed
            assert "forge" in parsed["mcpServers"]
            server_cfg = parsed["mcpServers"]["forge"]
            assert "command" in server_cfg
            assert "args" in server_cfg
            assert "-m" in server_cfg["args"]
            assert "forge.mcp_server" in server_cfg["args"]


class TestMcpDocRemoteConfig:
    """Verify remote MCP (SSE) configuration is documented."""

    def test_has_sse_startup_command(self, doc_content: str) -> None:
        assert "--transport sse" in doc_content

    def test_has_port_flag(self, doc_content: str) -> None:
        assert "--port" in doc_content

    def test_has_sse_url_example(self, doc_content: str) -> None:
        assert "/sse" in doc_content


class TestMcpDocToolCoverage:
    """Verify all MCP tools are documented."""

    EXPECTED_TOOLS = [
        "list_projects",
        "get_project_backlog",
        "get_completed_tasks",
        "get_project_config",
        "get_project_skills",
        "get_project_gate_scripts",
        "get_task_detail",
        "get_task_history",
        "create_task",
        "create_task_batch",
        "activate_task",
        "pause_task",
        "resume_task",
        "retry_task",
        "reset_task",
        "cancel_task",
        "delete_task",
        "update_task",
        "reprioritize_task",
    ]

    @pytest.mark.parametrize("tool_name", EXPECTED_TOOLS)
    def test_tool_is_documented(self, doc_content: str, tool_name: str) -> None:
        assert tool_name in doc_content, f"Tool '{tool_name}' not found in docs"


class TestMcpDocStartupCommands:
    """Verify documented startup commands match actual CLI implementation."""

    def test_default_transport_is_stdio(self) -> None:
        parser = build_arg_parser()
        args = parser.parse_args([])
        assert args.transport == "stdio"

    def test_default_port_is_8390(self) -> None:
        parser = build_arg_parser()
        args = parser.parse_args([])
        assert args.port == 8390

    def test_doc_default_port_matches_code(self, doc_content: str) -> None:
        """The documented default port should match the actual default."""
        parser = build_arg_parser()
        args = parser.parse_args([])
        assert str(args.port) in doc_content

    def test_sse_transport_accepted(self) -> None:
        parser = build_arg_parser()
        args = parser.parse_args(["--transport", "sse"])
        assert args.transport == "sse"

    def test_http_transport_accepted(self) -> None:
        parser = build_arg_parser()
        args = parser.parse_args(["--transport", "http"])
        assert args.transport == "http"

    def test_build_run_kwargs_stdio(self) -> None:
        parser = build_arg_parser()
        args = parser.parse_args(["--transport", "stdio"])
        kwargs = build_run_kwargs(args)
        assert "port" not in kwargs

    def test_build_run_kwargs_sse(self) -> None:
        parser = build_arg_parser()
        args = parser.parse_args(["--transport", "sse", "--port", "9000"])
        kwargs = build_run_kwargs(args)
        assert kwargs["port"] == 9000

    def test_build_run_kwargs_http(self) -> None:
        parser = build_arg_parser()
        args = parser.parse_args(["--transport", "http", "--port", "9000"])
        kwargs = build_run_kwargs(args)
        assert kwargs["port"] == 9000

    def test_doc_shows_module_entry_point(self, doc_content: str) -> None:
        assert "python -m forge.mcp_server" in doc_content


def _extract_json_blocks(markdown: str) -> list[str]:
    """Extract JSON code blocks from markdown text."""
    blocks = []
    in_block = False
    current: list[str] = []
    for line in markdown.splitlines():
        if line.strip() == "```json":
            in_block = True
            current = []
        elif line.strip() == "```" and in_block:
            in_block = False
            blocks.append("\n".join(current))
        elif in_block:
            current.append(line)
    return blocks
