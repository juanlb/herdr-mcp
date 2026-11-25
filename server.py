# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp==2.3.0"]
# [tool.uv]
# exclude-newer = "2026-11-25T00:00:00Z"
# ///
"""Local stdio MCP server wrapping the herdr CLI. No HTTP, ever: agent_prompt makes agents run commands
on this machine, safe only because transport is local stdio launched by Claude Desktop. Never print to
stdout (protocol channel).

This fork only talks to agents: list_agents, agent_read, agent_prompt. It covers every running Herdr
session on this machine (`herdr session list`); an agent is addressed by session + pane id, since pane
ids repeat across sessions.

Two scopes, split by folder (blocked_cwd_prefixes in CONFIG_PATH, e.g. ~/Projects/itcrowd):
- list_agents / agent_read / agent_prompt reach every agent OUTSIDE those folders.
- itcrowd_list_agents / itcrowd_agent_read / itcrowd_agent_prompt reach only agents INSIDE them.
They are separate tools so Claude Desktop can hold the ItCrowd ones at "ask every time" on their own:
Desktop sets permissions per tool, not per argument. The config is re-read on every call. A missing
or broken config refuses everything."""
import json
import os
import re
import subprocess
from datetime import datetime, timezone

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

HERDR_BIN = os.environ.get("HERDR_BIN", "/opt/homebrew/bin/herdr")
CONFIG_PATH = os.path.expanduser(os.environ.get("HERDR_MCP_CONFIG", "~/.config/herdr-mcp/config.json"))
LOG_PATH = os.path.expanduser(os.environ.get("HERDR_MCP_LOG", "~/.local/state/herdr-mcp/prompts.jsonl"))
PANE_RE = re.compile(r"^[A-Za-z0-9]+:p[A-Za-z0-9]+$")  # real ids include e.g. w1:pA
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
# --session decides the socket; drop inherited session vars so they cannot point elsewhere
CHILD_ENV = {k: v for k, v in os.environ.items() if k not in ("HERDR_SESSION", "HERDR_SOCKET_PATH")}

mcp = MCPServer("herdr")


def herdr(*args: str, timeout: float = 30) -> str:
    if any("\0" in a for a in args):
        raise ToolError("NUL byte in argument")
    try:
        r = subprocess.run([HERDR_BIN, *args], capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, env=CHILD_ENV)
    except OSError as e:
        raise ToolError(f"herdr binary unusable ({HERDR_BIN}): {e}")
    except subprocess.TimeoutExpired:
        raise ToolError(f"herdr timed out after {timeout}s; command may still have taken effect")
    if r.returncode != 0:
        # CLI puts JSON errors (agent_blocked, timeout, ...) on stdout or stderr; return as-is
        out = "\n".join(x for x in (r.stderr.strip(), r.stdout.strip()) if x)
        raise ToolError(out or f"herdr exited {r.returncode}")
    return r.stdout


def real(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def blocked_dirs() -> list[str]:
    """Blocked folders from the config, resolved. Any problem with the config refuses everything."""
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        dirs = cfg["blocked_cwd_prefixes"]
        if not isinstance(dirs, list) or not all(isinstance(d, str) and d.strip() for d in dirs):
            raise ValueError("blocked_cwd_prefixes must be a list of non-empty strings")
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise ToolError(f"refused: config {CONFIG_PATH} missing or invalid ({e}); nothing is allowed until it is fixed")
    return [real(d) for d in dirs]


def is_blocked(agent: dict, blocked: list[str]) -> bool:
    for key in ("cwd", "foreground_cwd"):
        cwd = agent.get(key)
        if not cwd:
            continue
        c = real(cwd)
        if any(c == b or c.startswith(b + os.sep) for b in blocked):
            return True
    return False


def running_sessions() -> list[str]:
    sessions = json.loads(herdr("session", "list", "--json"))["sessions"]
    return [x["name"] for x in sessions if x.get("running") and SESSION_RE.fullmatch(x.get("name", ""))]


def workspace_labels(session: str) -> dict[str, str]:
    try:
        ws = json.loads(herdr("--session", session, "workspace", "list"))["result"]["workspaces"]
        return {w["workspace_id"]: w.get("label") for w in ws}
    except (ToolError, ValueError, KeyError, TypeError):
        return {}


def visible_agents(itcrowd: bool) -> list[dict]:
    """Agents in every running session, in one scope: outside the blocked folders (itcrowd=False) or
    inside them (itcrowd=True). Each gets 'session' and 'workspace'."""
    blocked = blocked_dirs()
    out = []
    for session in running_sessions():
        try:
            agents = json.loads(herdr("--session", session, "agent", "list"))["result"]["agents"]
        except (ToolError, ValueError, KeyError, TypeError):
            continue  # a session that stops between list and query is skipped, not fatal
        agents = [a for a in agents if is_blocked(a, blocked) == itcrowd]
        labels = workspace_labels(session) if agents else {}
        for a in agents:
            a["session"] = session
            a["workspace"] = labels.get(a.get("workspace_id"))
        out += agents
    return out


def resolve(session: str, pane_id: str, itcrowd: bool) -> dict:
    """The agent in this session and pane, if it exists and is in this scope. Out of scope and missing
    look the same."""
    lister = "itcrowd_list_agents" if itcrowd else "list_agents"
    if not SESSION_RE.fullmatch(session):
        raise ToolError(f"invalid session {session!r}; use the session from {lister}")
    if not PANE_RE.fullmatch(pane_id):
        raise ToolError(f"invalid pane id {pane_id!r}; expected like wA:p7, from {lister}")
    for a in visible_agents(itcrowd):
        if a["session"] == session and a.get("pane_id") == pane_id:
            return a
    raise ToolError(f"refused: no reachable agent in session {session} pane {pane_id}; call {lister}")


def log_prompt(agent: dict, text: str, result: str, itcrowd: bool) -> None:
    os.makedirs(os.path.dirname(LOG_PATH), mode=0o700, exist_ok=True)
    entry = {"date": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
             "scope": "itcrowd" if itcrowd else "personal",
             "session": agent.get("session"), "pane_id": agent.get("pane_id"), "name": agent.get("name"),
             "title": agent.get("terminal_title_stripped"),
             "cwd": agent.get("cwd"), "text": text, "result": result}
    fd = os.open(LOG_PATH, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


KEEP = ("session", "pane_id", "name", "terminal_title_stripped", "workspace", "workspace_id", "cwd", "agent",
        "agent_status")


def _list(itcrowd: bool) -> str:
    return json.dumps([{k: a.get(k) for k in KEEP} for a in visible_agents(itcrowd)], ensure_ascii=False, indent=1)


def _read(session: str, pane_id: str, lines: int, itcrowd: bool) -> str:
    resolve(session, pane_id, itcrowd)
    n = str(max(1, min(2000, int(lines))))
    return herdr("--session", session, "agent", "read", pane_id, "--source", "recent-unwrapped", "--lines", n)


def _prompt(session: str, pane_id: str, text: str, timeout_ms: int, itcrowd: bool) -> str:
    agent = resolve(session, pane_id, itcrowd)
    if not text.strip():
        raise ToolError("empty prompt")
    t = max(1000, min(600000, int(timeout_ms)))
    try:
        out = herdr("--session", session, "agent", "prompt", pane_id, text, "--wait", "--timeout", str(t),
                    timeout=t / 1000 + 10)
    except ToolError as e:
        log_prompt(agent, text, f"error: {str(e)[:500]}", itcrowd)
        raise
    try:
        blocked = json.loads(out)["result"]["agent"]["agent_status"] == "blocked"
    except (ValueError, KeyError, TypeError):
        blocked = False
    log_prompt(agent, text, "blocked" if blocked else "ok", itcrowd)
    if blocked:  # CLI exits 0 when the turn ends at a question/approval dialog; not a success
        raise ToolError(f"{out.strip()}\nagent is waiting at an approval/question dialog and needs a human; "
                        "read it with agent_read. Not retried.")
    return out


@mcp.tool()
def list_agents() -> str:
    """Running agents in every Herdr session (ItCrowd excluded), as JSON: session and pane_id (pass both as
    the target), name (Herdr agent name, often null), title, workspace, cwd, kind, status. Call this first;
    pane ids repeat across sessions and can change when Herdr restarts, so never reuse an old one."""
    return _list(False)


@mcp.tool()
def agent_read(session: str, pane_id: str, lines: int = 120) -> str:
    """Read recent terminal output of an agent (session and pane_id from list_agents). lines clamped to
    1..2000. A working agent may refuse long reads (agent_not_idle); retry with fewer lines or later."""
    return _read(session, pane_id, lines, False)


@mcp.tool()
def agent_prompt(session: str, pane_id: str, text: str, timeout_ms: int = 120000) -> str:
    """EXECUTES commands: sends text as a prompt to a running agent (session and pane_id from list_agents),
    which will run commands on this machine.
    Waits for idle/done/blocked. A timeout does not prove the prompt was not
    delivered (the agent may still be working): read the agent later instead of resending. Every prompt is
    logged locally."""
    return _prompt(session, pane_id, text, timeout_ms, False)


@mcp.tool()
def itcrowd_list_agents() -> str:
    """ItCrowd agents only (work account, client code), in every Herdr session, as JSON with the same
    fields as list_agents. Their output passes through this personal Claude account."""
    return _list(True)


@mcp.tool()
def itcrowd_agent_read(session: str, pane_id: str, lines: int = 120) -> str:
    """Read recent terminal output of an ItCrowd agent (session and pane_id from itcrowd_list_agents).
    lines clamped to 1..2000."""
    return _read(session, pane_id, lines, True)


@mcp.tool()
def itcrowd_agent_prompt(session: str, pane_id: str, text: str, timeout_ms: int = 120000) -> str:
    """EXECUTES commands on client work: sends text as a prompt to a running ItCrowd agent (session and
    pane_id from itcrowd_list_agents), which will run commands on this machine.
    Waits for idle/done/blocked. A timeout does not prove the prompt was not
    delivered (the agent may still be working): read the agent later instead of resending. Every prompt is
    logged locally."""
    return _prompt(session, pane_id, text, timeout_ms, True)


if __name__ == "__main__":
    mcp.run(transport="stdio")
