"""Runtime configuration and system-prompt loading (prompts live in app/system/*.md)."""

from __future__ import annotations

import datetime as dt
import os
import platform
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:8080"  # the local llama-server
PROMPT_DIRS = {"MAIN": "chat", "MISSION": "voyager", "METHOD": "voyager", "PERSONA": "voyager"}  # others (LOCAL, CODER): system/
DEFAULT_SYSTEM_DIR = Path(__file__).resolve().parents[2] / "system"
DEFAULT_MCP_CONFIG = Path(__file__).resolve().parents[2] / "mcp.json"


def env(name: str, default: str | None = None) -> str | None:
    """VOYAGER_<NAME> (BONSAI_<NAME> is still read as a legacy fallback)."""
    return os.environ.get(f"VOYAGER_{name}") or os.environ.get(f"BONSAI_{name}") or default


def default_home() -> Path:
    """~/.voyager; a legacy ~/.bonsai-agent keeps being used as long as ~/.voyager does not exist."""
    new, old = Path.home() / ".voyager", Path.home() / ".bonsai-agent"
    return new if new.exists() or not old.exists() else old


HOME_DIR = Path(env("HOME") or default_home())  # sessions + input history
WORKSPACES_DIR = Path(env("WORKSPACES") or HOME_DIR / "workspaces")

# Env vars that would send the Coder's `claude -p` somewhere other than the CLI's own login
# (an API key, or the local llama-server proxy set by claude-local.sh).
CLAUDE_ENV_SCRUB = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
)


@dataclass
class Config:
    # --- the local model (llama-server, Anthropic-compatible /v1/messages) ---
    base_url: str = field(default_factory=lambda: env("URL", DEFAULT_URL) or DEFAULT_URL)
    model: str = field(default_factory=lambda: env("MODEL", "bonsai") or "bonsai")  # informational: the served model's name
    max_tokens: int = 16384
    thinking_budget: int = 4096  # 0 disables thinking
    # the server's n_ctx; compaction is sized from it. The CLI replaces this fallback with the value
    # read from the server's /props at startup (see server_context_window), unless --context-window is given.
    context_window: int = 65536
    compact_at: float = 0.70  # auto-compact when the context reaches this fraction of the window
    compact_max_tokens: int = 2048  # room for the summary
    # in a workspace: ask the agent to save its state to the workspace at this fraction (None = compact_at - 0.10)
    checkpoint_at: float | None = None
    # in a workspace: automatic "continue" messages when the main agent stops with its plan unfinished
    # (per user message; one nudge in a row without workspace progress stops it; 0 = never)
    max_nudges: int = 20
    # --- Coder agent: `claude -p` subprocess, never the API ---
    claude_bin: str = field(default_factory=lambda: env("CLAUDE_BIN", "claude") or "claude")
    coder_model: str = "sonnet"
    coder_cwd: Path | None = None  # default: a per-agent scratch dir; set to give the Coder a real folder
    # --- behaviour ---
    max_running_agents: int = 4
    auto_background_secs: float = 30  # tool calls running longer than this are moved to the background (0 = never)
    cwd: Path = field(default_factory=Path.cwd)
    launch_cwd: Path | None = None  # where the CLI was started (/workspace none returns there); None = cwd
    workspaces_dir: Path = field(default_factory=lambda: WORKSPACES_DIR)
    system_dir: Path = field(
        default_factory=lambda: Path(env("SYSTEM_DIR") or DEFAULT_SYSTEM_DIR)
    )
    home: Path = field(default_factory=lambda: HOME_DIR)  # sessions, history, MCP schema cache
    mcp_config: Path | None = field(default_factory=lambda: Path(env("MCP_CONFIG") or DEFAULT_MCP_CONFIG))
    sessions_dir: Path = field(default_factory=lambda: HOME_DIR / "sessions")
    history_path: Path = field(default_factory=lambda: HOME_DIR / "history")  # input history (↑/↓)


def server_context_window(base_url: str, timeout: float = 2.0) -> int | None:
    """The per-slot n_ctx llama-server actually runs with (GET /props), or None if it can't be read.

    Asked once at startup so compaction is sized from the real server window, not a guess that
    goes stale whenever the launch script's -c changes.
    """
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/props", timeout=timeout) as r:
            n_ctx = json.load(r).get("default_generation_settings", {}).get("n_ctx")
    except (OSError, ValueError):
        return None
    return n_ctx if isinstance(n_ctx, int) and n_ctx > 0 else None


def find_agents_md(cwd: Path) -> str:
    for d in [cwd, *cwd.parents]:
        if (d / "AGENTS.md").is_file():
            return str(d / "AGENTS.md")
    return "AGENTS.md (not found from the working directory upward)"


def load_system_prompt(
    cfg: Config, name: str, *, agent_id: str = "", agent_name: str = "", extra: dict[str, str] | None = None
) -> str:
    """Read a system prompt and fill the {{placeholders}} (`extra` adds dynamic ones, filled last).

    Layout: system/chat/ (chat mode), system/voyager/ (voyager mode), and system/ itself for what both share.
    """
    path = cfg.system_dir / PROMPT_DIRS.get(name, "") / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(f"system prompt not found: {path} (set VOYAGER_SYSTEM_DIR?)")
    text = path.read_text(encoding="utf-8")
    values = {
        "cwd": str(cfg.cwd),
        "platform": f"{platform.system()} ({platform.release()})",
        "date": dt.date.today().isoformat(),
        "agent_id": agent_id,
        "agent_name": agent_name,
        "agents_md": find_agents_md(cfg.cwd),
        "workspace_state": "",
        **(extra or {}),
    }
    for key, val in values.items():
        text = text.replace("{{" + key + "}}", val)
    return text
