# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp==2.3.0"]
# [tool.uv]
# exclude-newer = "2026-11-25T00:00:00Z"
# ///
"""Local stdio MCP server wrapping the herdr CLI. No HTTP, ever: agent_prompt makes agents run commands
on this machine, safe only because transport is local stdio launched by Claude Desktop. Never print to
stdout (protocol channel).

This fork only talks to agents: list_agents, agent_read, agent_prompt. Agents whose cwd sits under a
blocked folder (see CONFIG_PATH) are hidden and refused. The config is re-read on every call, so the
block can be lifted and restored without restarting the server. A missing or broken config refuses
everything."""
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

mcp = MCPServer("herdr")


def herdr(*args: str, timeout: float = 30) -> str:
    if any("\0" in a for a in args):
        raise ToolError("NUL byte in argument")
    try:
        r = subprocess.run([HERDR_BIN, *args], capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
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


def visible_agents() -> list[dict]:
    blocked = blocked_dirs()
    agents = json.loads(herdr("agent", "list"))["result"]["agents"]
    return [a for a in agents if not is_blocked(a, blocked)]


def resolve(pane_id: str) -> dict:
    """The agent in this pane, if it exists and is not blocked. Blocked and missing look the same."""
    if not PANE_RE.fullmatch(pane_id):
        raise ToolError(f"invalid pane id {pane_id!r}; expected like wA:p7, from list_agents")
    for a in visible_agents():
        if a.get("pane_id") == pane_id:
            return a
    raise ToolError(f"refused: no reachable agent in pane {pane_id}; call list_agents")


def log_prompt(agent: dict, text: str, result: str) -> None:
    os.makedirs(os.path.dirname(LOG_PATH), mode=0o700, exist_ok=True)
    entry = {"date": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
             "pane_id": agent.get("pane_id"), "title": agent.get("terminal_title_stripped"),
             "cwd": agent.get("cwd"), "text": text, "result": result}
    fd = os.open(LOG_PATH, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


@mcp.tool()
def list_agents() -> str:
    """Running agents you can reach, as JSON: pane_id (use it as target), title, cwd, kind, status.
    Call this first; pane ids can change when Herdr restarts, so never reuse an old one."""
    keep = ("pane_id", "terminal_title_stripped", "cwd", "agent", "agent_status", "workspace_id", "tab_id")
    return json.dumps([{k: a.get(k) for k in keep} for a in visible_agents()], ensure_ascii=False, indent=1)


@mcp.tool()
def agent_read(pane_id: str, lines: int = 120) -> str:
    """Read recent terminal output of an agent (pane id from list_agents). lines clamped to 1..2000."""
    resolve(pane_id)
    n = str(max(1, min(2000, int(lines))))
    return herdr("agent", "read", pane_id, "--source", "recent-unwrapped", "--lines", n)


@mcp.tool()
def agent_prompt(pane_id: str, text: str, timeout_ms: int = 120000) -> str:
    """EXECUTES commands: sends text as a prompt to a running agent (pane id from list_agents), which will
    run commands on this machine. Waits for idle/done/blocked. On agent_blocked / agent_prompt_stalled /
    timeout an error with the CLI's JSON is returned; a timeout does not prove the prompt was not
    delivered, so do not blindly retry. Every prompt is logged locally."""
    agent = resolve(pane_id)
    if not text.strip():
        raise ToolError("empty prompt")
    t = max(1000, min(600000, int(timeout_ms)))
    try:
        out = herdr("agent", "prompt", pane_id, text, "--wait", "--timeout", str(t), timeout=t / 1000 + 10)
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
                        "read it with agent_read. Not retried.")
    return out


if __name__ == "__main__":
    mcp.run(transport="stdio")
