# Forge MCP Server

The Forge MCP server exposes project and task management tools via the [Model Context Protocol](https://modelcontextprotocol.io/). It lets AI assistants like Claude interact with Forge directly -- querying projects, creating tasks, managing the pipeline, and more.

## Transports

The server supports three transports:

| Transport | Use case | Default port |
|-----------|----------|--------------|
| `stdio`   | Claude Code (local, stdin/stdout) | N/A |
| `sse`     | Claude.ai remote MCP (Server-Sent Events) | 8390 |
| `http`    | Generic HTTP clients | 8390 |

## Starting the server

```bash
# stdio (default) -- for Claude Code
python -m forge.mcp_server

# SSE -- for Claude.ai remote MCP
python -m forge.mcp_server --transport sse --port 8390

# HTTP
python -m forge.mcp_server --transport http --port 8390
```

The MCP server runs as a separate process from the main Forge API server (`python -m forge serve`). Both share the same SQLite database.

## Configuring Claude Code (stdio)

Add the Forge MCP server to your project's `.mcp.json` file at the repository root:

```json
{
  "mcpServers": {
    "forge": {
      "command": "python",
      "args": ["-m", "forge.mcp_server"],
      "env": {
        "FORGE_DB_PATH": "/absolute/path/to/forge.db"
      }
    }
  }
}
```

Or add it to `~/.claude/claude_desktop_config.json` for global access:

```json
{
  "mcpServers": {
    "forge": {
      "command": "python",
      "args": ["-m", "forge.mcp_server"],
      "env": {
        "FORGE_DB_PATH": "/absolute/path/to/forge.db"
      }
    }
  }
}
```

The `FORGE_DB_PATH` environment variable is optional. If omitted, the server uses the default database path (`forge.db` in the Forge installation directory).

## Configuring Claude.ai (remote MCP via SSE)

1. Start the MCP server with SSE transport on a host reachable by Claude.ai:

   ```bash
   python -m forge.mcp_server --transport sse --port 8390
   ```

2. In Claude.ai, go to **Settings > Integrations > Add MCP Server** and enter:

   - **URL:** `https://your-host:8390/sse`

   The server must be accessible from the internet. Use a reverse proxy (e.g., nginx, Caddy) to add TLS and authentication in production.

## Available tools

The server exposes the following tools:

### Project queries
- `list_projects` -- List all registered projects
- `get_project_backlog` -- Get pending tasks for a project
- `get_completed_tasks` -- Get completed tasks for a project
- `get_project_config` -- Get project configuration
- `get_project_skills` -- List skills available in a project
- `get_project_gate_scripts` -- List gate scripts for a project

### Task queries
- `get_task_detail` -- Get full details of a specific task
- `get_task_history` -- Get stage run history for a task

### Task creation
- `create_task` -- Create a single task
- `create_task_batch` -- Create multiple tasks at once

### Task lifecycle
- `activate_task` -- Move a task from backlog to active
- `pause_task` -- Pause an active task
- `resume_task` -- Resume a paused task
- `retry_task` -- Retry a failed task
- `reset_task` -- Reset a task to backlog
- `cancel_task` -- Cancel a task
- `delete_task` -- Delete a task permanently

### Task modification
- `update_task` -- Update task title, description, priority, flow, or epic status
- `reprioritize_task` -- Change task priority

## Usage examples

### List projects and their backlogs

```
> List all projects in Forge
(calls list_projects)

> Show me the backlog for project "forge"
(calls get_project_backlog with project_id="<project-uuid>")
```

### Create and manage tasks

```
> Create a task to add input validation to the API
(calls create_task with project_id, title, description)

> Activate task abc123
(calls activate_task with task_id="abc123")

> What's the status of task abc123?
(calls get_task_detail with task_id="abc123")
```

### Batch task creation

```
> Create these three tasks for project "myapp":
> 1. Add rate limiting
> 2. Fix login redirect bug
> 3. Update dependencies
(calls create_task_batch with project_id and list of tasks)
```
