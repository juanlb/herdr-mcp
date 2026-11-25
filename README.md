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
It lets a Claude Desktop chat (and claude.ai through Desktop, phone included)
talk to the coding agents running in Herdr on this Mac.

### Two scopes, six tools

| Tool | Reaches |
|---|---|
| `list_agents()` | Agents **outside** the blocked folders, in every running session |
| `agent_read(session, pane_id, lines=120)` | Recent output of one of those agents |
| `agent_prompt(session, pane_id, text, timeout_ms=120000)` | Sends a prompt and waits for it to settle |
| `itcrowd_list_agents()` | Agents **inside** the blocked folders (ItCrowd work) |
| `itcrowd_agent_read(session, pane_id, lines=120)` | Recent output of one of those agents |
| `itcrowd_agent_prompt(session, pane_id, text, timeout_ms=120000)` | Sends a prompt and waits for it to settle |

The list tools return `session`, `pane_id`, `name` (Herdr agent name, often
null), `terminal_title_stripped`, `workspace` (label), `workspace_id`, `cwd`,
`agent` (kind) and `agent_status`. Pass `session` + `pane_id` as the target.

### Why it is built this way

- **Only agent tools.** Upstream's `snapshot`, `pane_read`, `pane_run`,
  `agent_start` and the tab/pane tools are removed. `pane_run` is a free shell
  in any pane and `pane_read` reads any pane, so either one would bypass the
  ItCrowd split.
- **Every running Herdr session.** Herdr runs several named sessions here
  (`herdr session list`). The server queries each running one with
  `--session`, and drops inherited `HERDR_SESSION` / `HERDR_SOCKET_PATH` so
  they cannot pin it to one. Pane IDs repeat across sessions, hence
  `session` + `pane_id`.
- **Pane IDs, not agent names.** Most agents have no Herdr name, and agents
  started by other agents would never be on a hand-kept list. Listing first
  and addressing by pane finds them all.
- **Split by folder, not by an allowlist.** `~/.config/herdr-mcp/config.json`:

  ```json
  { "blocked_cwd_prefixes": ["~/Projects/itcrowd"] }
  ```

  An agent whose `cwd` or `foreground_cwd` resolves under a listed folder
  belongs to the ItCrowd scope; everything else to the normal scope. Paths are
  resolved, so the `~/Projects` symlink matches the real path Herdr reports.
  The file is read on every call. Missing or invalid file: everything refused.
- **Separate ItCrowd tools, not a parameter.** Claude Desktop sets
  permissions per tool, not per argument. Separate tools let the ItCrowd ones
  sit at "ask every time" on their own, and the tool name tells the approver
  what they approve. ItCrowd output passes through the personal Claude
  account; approving a call accepts that. A timed unlock was rejected: the
  model would trigger it, and nothing is approved per call while it lasts.
- **Prompt log.** Every prompt goes to
  `~/.local/state/herdr-mcp/prompts.jsonl` (mode 600): date, scope, session,
  pane, name, title, cwd, text and result.
- **Pinned dependencies.** `mcp==2.3.0`, and `exclude-newer` freezes its own
  dependencies.
- Upstream tests removed: they exercised the removed tools.

### Using it from a chat

1. Call `list_agents` (or `itcrowd_list_agents`) first. Never reuse an old
   pane ID: they change when Herdr restarts.
2. `agent_read` before `agent_prompt`, to see what the agent is doing.
3. A busy agent can take longer than `timeout_ms`. A `timeout` error does not
   mean the prompt was lost: read the agent later instead of resending.
4. A working agent may refuse long reads (`agent_not_idle`); ask for fewer
   lines or wait.
5. After a server update, start a new chat: claude.ai keeps the tool schemas
   it saw when the chat started.

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
