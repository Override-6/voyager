"""Wire format between a session daemon (server.py) and its clients (client.py): JSON lines over a unix socket.

server -> client
  {"t": "snapshot", "session": {...}, "agents": [<agent state> + "items"], "tasks": [...]}   on attach and after /clear
  {"t": "log", "a": <agent id>, "e": start|delta|update|live|end, ...}                        one per transcript change
  {"t": "state", "agents": [...], "tasks": [...], "mode_status": "..."}                       only when something changed
  {"t": "reply", "id": n, ...}                                                                 answer to a request with "id"
  {"t": "bye"}                                                                                 the daemon is shutting down
client -> server
  {"op": "submit"|"stop"|"stop_all"|"kill_task"|"compact"|"reset"|"set_thinking"|"status"|"shutdown", ...}

The client keeps a replica of the session (client.py) that the TUI and the CLI printer use exactly like a real one.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..agent import Agent
    from ..log import Item
    from ..tasks import BackgroundTask

MAX_SOCK_PATH = 90  # AF_UNIX paths are limited to ~108 bytes
SNAPSHOT_ITEMS = 400  # transcript items per agent sent on attach (the newest ones)
REPORT_CLIP = 300
SHARED_CFG = ("base_url", "model", "context_window", "compact_at", "thinking_budget", "coder_model", "auto_background_secs")
RESULT_CLIP = 20_000


def sock_path(session_dir: Path) -> Path:
    """The daemon's socket: in the session folder, or in the temp dir when that path would be too long."""
    p = session_dir / "daemon.sock"
    if len(str(p)) <= MAX_SOCK_PATH:
        return p
    return Path(tempfile.gettempdir()) / f"voyager-{hashlib.sha1(str(session_dir).encode()).hexdigest()[:16]}.sock"


def dumps(msg: dict[str, Any]) -> bytes:
    return (json.dumps(msg, ensure_ascii=False, default=str) + "\n").encode()


async def read_msgs(reader: asyncio.StreamReader):  # noqa: ANN201
    """Yield decoded messages until the peer closes; a malformed line is skipped."""
    while True:
        try:
            line = await reader.readline()
        except (asyncio.LimitOverrunError, ValueError):
            continue
        if not line:
            return
        try:
            yield json.loads(line)
        except ValueError:
            continue


# --------------------------------------------------------------- state -> dicts
def item_dict(item: "Item") -> dict[str, Any]:
    return {"id": item.id, "kind": item.kind, "text": item.text, "meta": item.meta}


def agent_state(a: "Agent") -> dict[str, Any]:
    return {"id": a.id, "name": a.name, "kind": a.kind, "parent_id": a.parent_id, "running": a.running, "outcome": a.outcome,
            "activity": a.activity, "tool_count": a.tool_count, "queued": len(a.inbox),
            "last_report": a.last_report[:REPORT_CLIP], "age": round(time.monotonic() - a.started_at, 1)}


def task_state(bt: "BackgroundTask") -> dict[str, Any]:
    return {"id": bt.id, "owner_id": bt.owner_id, "tool": bt.tool, "summary": bt.summary, "status": bt.status,
            "item_id": bt.item.id, "elapsed": round(bt.elapsed, 1), "result": bt.result[:RESULT_CLIP], "auto": bt.auto}


def without_clocks(states: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """States minus their always-changing clocks (age, elapsed), to tell whether anything really changed."""
    return [{k: v for k, v in s.items() if k not in ("age", "elapsed")} for s in states]
