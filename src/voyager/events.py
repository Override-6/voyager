"""A live, append-only event log of a session, for monitoring it from outside (`voyager --monitor`).

`<session dir>/events.jsonl` gets one JSON line per event, written as it happens (a session is only saved to
session.json at the end of a turn, which is too late to watch a long mission). `run.json` beside it says who runs
(pid, cwd, mode, workspace) so a monitor can tell a running session from a finished or dead one. Both files are
created lazily, on the first event, so an empty session leaves nothing behind. Best effort: a write error never
reaches an agent.

Events (all carry `ts`, `agent`, and `round` in voyager mode):
  start  user  waiting  gen_start  gen  text  thinking  tool_start  tool_end  tool_bg  usage  compact_start  compaction
  round  checkpoint  notice  error  turn_end  end
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .agent import Agent
from .log import Item

if TYPE_CHECKING:
    from .session import Session

TEXT_CLIP, RESULT_CLIP = 800, 300
GEN_EVERY = 5.0  # seconds between progress events while the model streams one block


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + "…"


class EventWriter:
    """Session hook (same signature as the front ends') that turns log items into events."""

    def __init__(self, session: "Session") -> None:
        self.session = session
        self._started = False
        self._tool_t0: dict[tuple[str, int], float] = {}
        self._gen_t: dict[tuple[str, int], float] = {}

    @property
    def path(self) -> Path:
        return self.session.dir / "events.jsonl"

    # ---------------------------------------------------------------- writing
    def emit(self, agent: Agent | None, ev: str, **fields: Any) -> None:
        try:
            if not self._started:
                self._start()
            row: dict[str, Any] = {"ts": round(time.time(), 2), "agent": agent.id if agent else "main", "ev": ev}
            rnd = getattr(getattr(agent, "mode", None), "round", None)
            if rnd is not None:
                row["round"] = rnd
            row.update({k: v for k, v in fields.items() if v is not None})
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass

    def _start(self) -> None:
        s = self.session
        self._started = True
        s.dir.mkdir(parents=True, exist_ok=True)
        ws = s.workspace
        run = {"id": s.id, "pid": os.getpid(), "started": round(time.time(), 2), "cwd": str(s.cfg.cwd),
               "mode": s.mode.name, "workspace": ws.name if ws else None, "workspaces_dir": str(s.cfg.workspaces_dir)}
        (s.dir / "run.json").write_text(json.dumps(run))
        self.emit(s.main, "start", mode=run["mode"], workspace=run["workspace"], cwd=run["cwd"])

    # ---------------------------------------------------------- session hooks
    def __call__(self, agent: Agent, event: str, item: Item, extra: Any) -> None:
        k, m = item.kind, item.meta
        if k in ("thinking", "text", "tool") and event in ("start", "delta"):
            self._generating(agent, item, event)
        if k == "user" and event == "start":
            self.emit(agent, "user", src=m.get("src"), text=_clip(item.text, TEXT_CLIP))
        elif k == "text" and event == "end":
            self.emit(agent, "text", text=_clip(item.text, TEXT_CLIP))
        elif k == "thinking" and event == "end":
            self.emit(agent, "thinking", chars=len(item.text))
        elif k == "tool" and event == "update" and isinstance(extra, dict) and extra.get("status"):
            self._tool(agent, item, extra["status"])
        elif k == "usage" and event == "start":
            self.emit(agent, "usage", text=item.text, ctx=m.get("ctx"), window=m.get("window"))
        elif k == "compact":
            if event == "start":
                self.emit(agent, "compact_start", before=m.get("before"), manual=m.get("manual"))
            elif event == "update" and isinstance(extra, dict) and "after" in extra:
                self.emit(agent, "compaction", before=m.get("before"), after=extra["after"], manual=m.get("manual"))
        elif k in ("notice", "error") and event == "start":
            self.emit(agent, m.get("event") or k, text=_clip(item.text, TEXT_CLIP))

    def _generating(self, agent: Agent, item: Item, event: str) -> None:
        """The model is streaming: say so when a block starts, then every GEN_EVERY seconds, so a long generation
        (minutes of planning text on a local model) does not look like a hang."""
        key, now = (agent.id, item.id), time.monotonic()
        if event == "start":
            self._gen_t[key] = now
            self.emit(agent, "gen_start", kind=item.kind, name=item.meta.get("name"))
        elif now - self._gen_t.get(key, now) >= GEN_EVERY:
            self._gen_t[key] = now
            self.emit(agent, "gen", kind=item.kind, chars=len(item.text))

    def _tool(self, agent: Agent, item: Item, status: str) -> None:
        m, key = item.meta, (agent.id, item.id)
        base = {"id": item.id, "name": m.get("name"), "summary": _clip(str(m.get("summary", "")), 160)}
        if status == "running":
            self._tool_t0[key] = time.monotonic()
            self.emit(agent, "tool_start", **base)
        elif status == "background":
            self.emit(agent, "tool_bg", **base)
        elif status in ("done", "error"):
            t0 = self._tool_t0.pop(key, None)
            self.emit(agent, "tool_end", **base, ok=status == "done", secs=round(time.monotonic() - t0, 1) if t0 else None,
                      result=_clip(str(m.get("result", "")), RESULT_CLIP))

    # -------------------------------------------------------------- lifecycle
    def turn_end(self, agent: Agent) -> None:
        self.emit(agent, "turn_end", outcome=agent.outcome, tools=agent.tool_count)

    def close(self) -> None:
        """Session shutdown: the monitor sees a clean end (no event if the session never did anything)."""
        if self._started:
            self.emit(self.session.main, "end", outcome=self.session.main.outcome)
