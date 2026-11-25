# herdr-mcp

A local stdio [MCP](https://modelcontextprotocol.io) server that wraps the
[Herdr](https://herdr.dev) CLI, so Claude Desktop can inspect and drive the
Herdr session running on the same machine.

Tested with herdr 0.9.5, `mcp` 2.3.0, and Claude Desktop on macOS.

## Security

`agent_prompt` makes agents execute commands on this machine.
That is the point of the tool, and it is acceptable only because the transport
is local stdio started by Claude Desktop. There is no HTTP transport; do not put
this server behind HTTP or a tunnel. In Claude Desktop, keep these tools on
"ask every time".

## Fork changes (juanlb)

Personal-use fork. Upstream has no license; this fork is not redistributed.

- Only three tools: `list_agents`, `agent_read`, `agent_prompt`. `snapshot`,
  `pane_read`, `pane_run`, `agent_start` and the tab/pane tools are removed:
  `pane_run` and `pane_read` reached any pane, including blocked ones.
- **Every running Herdr session.** The server reads `herdr session list --json`
  and queries each running session with `--session`. Pane IDs repeat across
  sessions, so an agent is addressed by `session` + `pane_id`, both from
  `list_agents`. No agent names are needed, so agents started by other agents
  show up on their own. Inherited `HERDR_SESSION` / `HERDR_SOCKET_PATH` are
  dropped from the CLI's environment so they cannot pin it to one session.
- **Block by folder.** `~/.config/herdr-mcp/config.json` lists folders:

  ```json
  { "blocked_cwd_prefixes": ["~/Projects/itcrowd"] }
  ```

  An agent whose `cwd` or `foreground_cwd` sits under one of them is hidden
  from `list_agents` and refused by `agent_read` and `agent_prompt`. Paths are
  resolved, so symlinks match. The file is read on every call: edit it and the
  next call uses it, no restart. Missing or invalid file: everything is refused.
  Lift the block with `"blocked_cwd_prefixes": []`.
  Override the path with `HERDR_MCP_CONFIG`.
- **Prompt log.** Every prompt sent goes to
  `~/.local/state/herdr-mcp/prompts.jsonl` (mode 600): date, session, pane, name, title, cwd,
  text and result. Override with `HERDR_MCP_LOG`.
- `mcp` pinned to `2.3.0`; `exclude-newer` freezes its own dependencies too.
- Upstream tests removed: they exercised the removed tools.

## Tools

| Tool | What it does |
|---|---|
| `list_agents()` | Reachable agents in all sessions: session, pane ID, Herdr name, title, workspace, cwd, kind, status |
| `agent_read(session, pane_id, lines=120)` | Recent output of an agent |
| `agent_prompt(session, pane_id, text, timeout_ms=120000)` | Sends a prompt to an agent and waits for it to settle |

## Install

Requires [uv](https://docs.astral.sh/uv/) and a running Herdr server.

Clone the repo, then add the server to
`~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "herdr": {
      "command": "/opt/homebrew/bin/uv",
      "args": ["run", "/path/to/herdr-mcp/server.py"]
    }
  }
}
```

Use absolute paths: Claude Desktop starts servers with a minimal `PATH`.
Quit
and reopen Claude Desktop. `uv` installs the one dependency on first run.

The server calls `/opt/homebrew/bin/herdr` by default. Set `HERDR_BIN` to use
another location:

```json
"env": { "HERDR_BIN": "/usr/local/bin/herdr" }
```
