"""MCP config file: Claude Code-compatible `mcpServers` JSON.

{
  "mcpServers": {
    "ghostchrome": {"command": "/path/to/server", "args": [], "env": {"KEY": "${HOME}"}},
    "remote":      {"url": "https://host/mcp", "headers": {"Authorization": "Bearer ${TOKEN}"}}
  }
}
Optional per server: "timeout" (s per call, 120),
"startupTimeout" (s, 60), "disabled": true, "cwd".
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_VAR = re.compile(r"\$\{(\w+)(?::-([^}]*))?\}")


@dataclass
class ServerConfig:
    name: str
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = 120.0
    startup_timeout: float = 60.0

    def fingerprint(self) -> str:
        """Identifies *what is launched*: the tool-schema cache is invalidated when it changes."""
        blob = json.dumps([self.command, self.args, self.url], sort_keys=True)  # env (headless, profile...) does not change the tools
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _expand(value: str) -> str:
    return _VAR.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)


def load_config(path: Path) -> tuple[list[ServerConfig], list[str]]:
    """Returns (servers, problems). A broken entry is reported and skipped, never fatal."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return [], []
    except (OSError, json.JSONDecodeError) as e:
        return [], [f"{path}: {e}"]
    raw = data.get("mcpServers") or data.get("servers") or {}
    servers: list[ServerConfig] = []
    problems: list[str] = []
    for name, d in raw.items():
        if not isinstance(d, dict):
            problems.append(f"{name}: entry must be an object")
        elif d.get("disabled"):
            continue
        elif not (d.get("command") or d.get("url")):
            problems.append(f"{name}: needs \"command\" (stdio) or \"url\" (http)")
        else:
            servers.append(_parse(name, d))
    return servers, problems


def _parse(name: str, d: dict[str, Any]) -> ServerConfig:
    return ServerConfig(
        name=re.sub(r"[^A-Za-z0-9_-]", "_", name),
        command=_expand(str(d.get("command", ""))),
        args=[_expand(str(a)) for a in d.get("args", [])],
        env={k: _expand(str(v)) for k, v in (d.get("env") or {}).items()},
        cwd=_expand(d["cwd"]) if d.get("cwd") else None,
        url=_expand(str(d.get("url", ""))),
        headers={k: _expand(str(v)) for k, v in (d.get("headers") or {}).items()},
        timeout=float(d.get("timeout", 120)),
        startup_timeout=float(d.get("startupTimeout", 60)),
    )
