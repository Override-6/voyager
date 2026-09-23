"""Starting a session daemon (a detached `voyager --serve` process) and finding the ones that are alive.

One daemon per session: no central process to babysit, a crash only takes that session with it, and each keeps its
own MCP servers. A daemon is found through `<session dir>/daemon.json` (written by server.py, removed when it exits).
The daemon receives its exact Config as a JSON file, so nothing is lost in translation through command-line flags.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Config
from ..monitor import alive

START_TIMEOUT = 60.0  # seconds for the daemon to start listening (MCP config, workspace git ...)
PATH_FIELDS = ("cwd", "launch_cwd", "workspaces_dir", "system_dir", "home", "mcp_config", "sessions_dir", "history_path", "coder_cwd")


class LaunchError(RuntimeError):
    pass


# ----------------------------------------------------------------- config file
def config_to_json(cfg: Config) -> str:
    return json.dumps({k: (str(v) if isinstance(v, Path) else v) for k, v in dataclasses.asdict(cfg).items()})


def config_from_json(text: str) -> Config:
    data: dict[str, Any] = json.loads(text)
    known = {f.name for f in dataclasses.fields(Config)}
    return Config(**{k: (Path(v) if k in PATH_FIELDS and v is not None else v) for k, v in data.items() if k in known})


# -------------------------------------------------------------------- registry
@dataclass
class LiveSession:
    id: str
    pid: int
    sock: Path
    cwd: str
    mode: str
    workspace: str | None
    started: float
    dir: Path


def live_sessions(sessions_dir: Path) -> list[LiveSession]:
    """Daemons that are running, newest first. A daemon.json whose process is gone is stale and ignored."""
    out = []
    for f in sessions_dir.glob("*/daemon.json"):
        try:
            d = json.loads(f.read_text())
            ls = LiveSession(d["id"], int(d["pid"]), Path(d["sock"]), d["cwd"], d["mode"], d.get("workspace"), d["started"], f.parent)
        except (OSError, ValueError, KeyError):
            continue
        if alive(ls.pid) and ls.sock.exists():
            out.append(ls)
    return sorted(out, key=lambda s: s.started, reverse=True)


def find_live(sessions_dir: Path, ref: str | None) -> LiveSession | None:
    """A live daemon by session id (a unique prefix is enough); no ref: the newest one."""
    live = live_sessions(sessions_dir)
    if not ref:
        return live[0] if live else None
    hits = [s for s in live if s.id == ref] or [s for s in live if s.id.startswith(ref)]
    return hits[0] if len(hits) == 1 else None


# -------------------------------------------------------------------- spawning
def new_session_id(sessions_dir: Path) -> str:
    base = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    sid, n = base, 1
    while (sessions_dir / sid).exists():
        n += 1
        sid = f"{base}-{n}"
    return sid


async def spawn(cfg: Config, *, session_id: str | None = None, prompt: str | None = None, resume: str | None = None,
                idle_exit: float = 0.0) -> LiveSession:
    """Start a daemon for a new session (or `resume` a saved one) and wait until it accepts clients."""
    sid = session_id or resume or new_session_id(cfg.sessions_dir)
    d = cfg.sessions_dir / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text(config_to_json(cfg))
    cmd = [sys.executable, "-m", "voyager", "--serve", "--session-id", sid, "--config-file", str(d / "config.json")]
    if resume:
        cmd += ["--resume", resume]
    if idle_exit:
        cmd += ["--idle-exit", str(idle_exit)]
    if prompt:
        cmd.append(prompt)
    with (d / "daemon.log").open("ab") as log:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, cwd=cfg.cwd)
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        if (live := next((s for s in live_sessions(cfg.sessions_dir) if s.id == sid), None)) is not None:
            return live
        if proc.poll() is not None:
            tail = (d / "daemon.log").read_text(errors="replace").strip().splitlines()[-5:]
            raise LaunchError(f"the session daemon exited with code {proc.returncode}: " + " | ".join(tail))
        await asyncio.sleep(0.1)
    proc.kill()
    raise LaunchError(f"the session daemon did not start listening within {START_TIMEOUT:.0f}s (see {d / 'daemon.log'})")
