# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp==2.3.0"]
# [tool.uv]
# exclude-newer = "2026-11-25T00:00:00Z"
# ///
"""Local stdio MCP server wrapping the herdr CLI. No HTTP, ever: agent_prompt makes agents run commands
on this machine, safe only because transport is local stdio launched by Claude Desktop. Never print to
stdout (protocol channel).

This fork only talks to agents. It covers every running Herdr session on this machine
(`herdr session list`); an agent is addressed by session + pane id, since pane ids repeat across
sessions.

Protected zones live in CONFIG_PATH, re-read on every call:
    {"protected": {"itcrowd": {"sessions": ["itcrowd", "shadow"], "cwd_prefixes": ["~/Projects/itcrowd"]}}}
An agent belongs to a zone if its session is listed there OR its cwd / foreground_cwd (resolved, symlinks
followed) sits under one of the zone's folders. If several zones match, the first one in the file wins.
Everything not in a zone is open.
- list_agents / agent_read / agent_prompt reach only open agents.
- protected_list_agents / protected_agent_read / protected_agent_prompt take a mandatory zone and reach
  only agents of that zone.
They are separate tools so Claude Desktop can hold the protected ones at "ask every time": Desktop sets
permissions per tool, not per argument. A missing or broken config refuses everything."""
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


ZONE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def load_zones() -> dict[str, dict]:
    """Zones from the config as {name: {"sessions": set, "dirs": [resolved paths]}}, in file order.
    Any problem with the config refuses everything."""
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        raw = cfg["protected"]
        if not isinstance(raw, dict):
            raise ValueError("'protected' must be an object of zones")
        zones = {}
        for name, z in raw.items():
            if not ZONE_RE.fullmatch(name):
                raise ValueError(f"invalid zone name {name!r}")
            if not isinstance(z, dict) or set(z) - {"sessions", "cwd_prefixes"}:
                raise ValueError(f"zone {name!r} takes only 'sessions' and 'cwd_prefixes'")
            sessions, dirs = z.get("sessions", []), z.get("cwd_prefixes", [])
            for k, v in (("sessions", sessions), ("cwd_prefixes", dirs)):
                if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
                    raise ValueError(f"zone {name!r}: {k} must be a list of non-empty strings")
            if not sessions and not dirs:
                raise ValueError(f"zone {name!r} is empty; it would protect nothing")
            zones[name] = {"sessions": set(sessions), "dirs": [real(d) for d in dirs]}
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise ToolError(f"refused: config {CONFIG_PATH} missing or invalid ({e}); nothing is allowed until it is fixed")
    return zones


def zone_of(agent: dict, session: str, zones: dict[str, dict]) -> str | None:
    cwds = [real(agent[k]) for k in ("cwd", "foreground_cwd") if agent.get(k)]
    for name, z in zones.items():
        if session in z["sessions"]:
            return name
        if any(c == d or c.startswith(d + os.sep) for c in cwds for d in z["dirs"]):
            return name
    return None


def running_sessions() -> list[str]:
    sessions = json.loads(herdr("session", "list", "--json"))["sessions"]
    return [x["name"] for x in sessions if x.get("running") and SESSION_RE.fullmatch(x.get("name", ""))]


def workspace_labels(session: str) -> dict[str, str]:
    try:
        ws = json.loads(herdr("--session", session, "workspace", "list"))["result"]["workspaces"]
        return {w["workspace_id"]: w.get("label") for w in ws}
    except (ToolError, ValueError, KeyError, TypeError):
        return {}


def all_agents(zones: dict[str, dict]) -> list[dict]:
    """Agents in every running session. Each gets 'session', 'workspace' and 'zone' (None when open)."""
    out = []
    for session in running_sessions():
        try:
            agents = json.loads(herdr("--session", session, "agent", "list"))["result"]["agents"]
        except (ToolError, ValueError, KeyError, TypeError):
            continue  # a session that stops between list and query is skipped, not fatal
        labels = workspace_labels(session) if agents else {}
        for a in agents:
            a["session"] = session
            a["workspace"] = labels.get(a.get("workspace_id"))
            a["zone"] = zone_of(a, session, zones)
        out += agents
    return out


def check_zone(zone: str | None, zones: dict[str, dict]) -> None:
    if zone is not None and zone not in zones:
        raise ToolError(f"refused: zone {zone!r} does not exist; configured zones: {sorted(zones)}")


def resolve(session: str, pane_id: str, zone: str | None) -> dict:
    """The agent at session + pane, if it belongs to `zone` (None = open agents only)."""
    lister = "protected_list_agents" if zone else "list_agents"
    if not SESSION_RE.fullmatch(session):
        raise ToolError(f"invalid session {session!r}; use the session from {lister}")
    if not PANE_RE.fullmatch(pane_id):
        raise ToolError(f"invalid pane id {pane_id!r}; expected like wA:p7, from {lister}")
    zones = load_zones()
    check_zone(zone, zones)
    for a in all_agents(zones):
        if a["session"] != session or a.get("pane_id") != pane_id:
            continue
        if a["zone"] == zone:
            return a
        if zone is None:
            raise ToolError(f"refused: the agent in session {session} pane {pane_id} is protected (zone "
                            f"{a['zone']!r}); only the protected_* tools with zone={a['zone']!r} reach it")
        where = f"zone {a['zone']!r}" if a["zone"] else "no zone (it is open; use the normal tools)"
        raise ToolError(f"refused: the agent in session {session} pane {pane_id} is outside zone {zone!r}; "
                        f"it belongs to {where}")
    raise ToolError(f"refused: no agent in session {session} pane {pane_id}; call {lister}")


def log_prompt(agent: dict, text: str, result: str) -> None:
    os.makedirs(os.path.dirname(LOG_PATH), mode=0o700, exist_ok=True)
    entry = {"date": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
             "zone": agent.get("zone"),
             "session": agent.get("session"), "pane_id": agent.get("pane_id"), "name": agent.get("name"),
             "title": agent.get("terminal_title_stripped"),
             "cwd": agent.get("cwd"), "text": text, "result": result}
    fd = os.open(LOG_PATH, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


KEEP = ("session", "pane_id", "zone", "name", "terminal_title_stripped", "workspace", "workspace_id", "cwd",
        "agent", "agent_status")


def _list(zone: str | None) -> str:
    zones = load_zones()
    check_zone(zone, zones)
    agents = [a for a in all_agents(zones) if a["zone"] == zone]
    return json.dumps([{k: a.get(k) for k in KEEP} for a in agents], ensure_ascii=False, indent=1)


def _read(zone: str | None, session: str, pane_id: str, lines: int) -> str:
    resolve(session, pane_id, zone)
    n = str(max(1, min(2000, int(lines))))
    return herdr("--session", session, "agent", "read", pane_id, "--source", "recent-unwrapped", "--lines", n)


def _prompt(zone: str | None, session: str, pane_id: str, text: str, timeout_ms: int) -> str:
    agent = resolve(session, pane_id, zone)
    if not text.strip():
        raise ToolError("empty prompt")
    t = max(1000, min(600000, int(timeout_ms)))
    try:
        out = herdr("--session", session, "agent", "prompt", pane_id, text, "--wait", "--timeout", str(t),
                    timeout=t / 1000 + 10)
    except ToolError as e:
        log_prompt(agent, text, f"error: {str(e)[:500]}")
        raise
    try:
        blocked = json.loads(out)["result"]["agent"]["agent_status"] == "blocked"
    except (ValueError, KeyError, TypeError):
        blocked = False
    log_prompt(agent, text, "blocked" if blocked else "ok")
    if blocked:  # CLI exits 0 when the turn ends at a question/approval dialog; not a success
        raise ToolError(f"{out.strip()}\nagent is waiting at an approval/question dialog and needs a human; "
                        "read it first. Not retried.")
    return out


@mcp.tool()
def list_agents() -> str:
    """Open (not protected) agents in every Herdr session, as JSON: session and pane_id (pass both as the
    target), zone (always null here), name (Herdr agent name, often null), title, workspace, cwd, kind,
    status. Call this first; pane ids repeat across sessions and can change when Herdr restarts."""
    return _list(None)


@mcp.tool()
def agent_read(session: str, pane_id: str, lines: int = 120) -> str:
    """Read recent terminal output of an open agent (session and pane_id from list_agents). lines clamped
    to 1..2000. A working agent may refuse long reads (agent_not_idle); retry with fewer lines or later."""
    return _read(None, session, pane_id, lines)


@mcp.tool()
def agent_prompt(session: str, pane_id: str, text: str, timeout_ms: int = 120000) -> str:
    """EXECUTES commands: sends text as a prompt to an open agent (session and pane_id from list_agents),
    which will run commands on this machine. Waits for idle/done/blocked. A timeout does not prove the
    prompt was not delivered (the agent may still be working): read the agent later instead of resending.
    Every prompt is logged locally."""
    return _prompt(None, session, pane_id, text, timeout_ms)


@mcp.tool()
def protected_list_agents(zone: str) -> str:
    """Agents of one protected zone (e.g. "itcrowd"), in every Herdr session, as JSON with the same fields
    as list_agents. An unknown zone is refused and the error lists the configured ones. Their output
    passes through this Claude account."""
    return _list(zone)


@mcp.tool()
def protected_agent_read(zone: str, session: str, pane_id: str, lines: int = 120) -> str:
    """Read recent terminal output of an agent in a protected zone (zone, session and pane_id from
    protected_list_agents). lines clamped to 1..2000."""
    return _read(zone, session, pane_id, lines)


@mcp.tool()
def protected_agent_prompt(zone: str, session: str, pane_id: str, text: str, timeout_ms: int = 120000) -> str:
    """EXECUTES commands in a protected zone: sends text as a prompt to an agent of that zone (zone, session
    and pane_id from protected_list_agents), which will run commands on this machine. Waits for
    idle/done/blocked. A timeout does not prove the prompt was not delivered: read the agent later instead
    of resending. Every prompt is logged locally."""
    return _prompt(zone, session, pane_id, text, timeout_ms)


if __name__ == "__main__":
    mcp.run(transport="stdio")
