"""The permanent record of a session: nothing the agents did is lost when their context is compacted.

Two append-only stores in the session folder, both written as things happen (a crash loses nothing):

  transcript.jsonl          every finished item of every agent, in full (thinking, text, tool calls with their whole
                            arguments and results, notices, compaction summaries), tagged with the agent and the round
  rounds/<agent>-rNNN.json  what one compaction threw away: the exact model-facing history of that round, the summary that
                            replaced it, the system prompt and tool list the agent had, and (voyager) the phase and commit

A *round* is the stretch between two compactions of one agent (round 0 is before its first). Only the logging lives
here: nothing reads these files back yet. events.jsonl (events.py) is a different thing: a clipped live feed for
monitoring. Best effort: a write error never reaches an agent.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .agent import Agent
from .log import Item

if TYPE_CHECKING:
    from .session import Session

SKIP_META = {"live", "expanded", "done", "preview", "preview_kind", "args_chars"}  # display state, not record


def round_of(agent: Any) -> int:
    return getattr(agent, "compactions", 0)


class TranscriptWriter:
    """Session hook: appends each item to transcript.jsonl once it is complete."""

    def __init__(self, session: "Session") -> None:
        self.session = session
        self._written: set[tuple[str, int]] = set()

    @property
    def path(self) -> Path:
        return self.session.dir / "transcript.jsonl"

    def __call__(self, agent: Agent, event: str, item: Item, extra: Any) -> None:
        k = item.kind
        if k == "tool":
            if event == "update" and isinstance(extra, dict) and extra.get("status") in ("done", "error"):
                self._write(agent, item)
        elif k == "compact":  # complete once the token count after is known (it comes after the item's end)
            if event == "update" and isinstance(extra, dict) and "after" in extra:
                self._write(agent, item)
        elif event == "end":
            self._write(agent, item)

    def _write(self, agent: Agent, item: Item) -> None:
        key = (agent.id, item.id)
        if key in self._written:
            return
        self._written.add(key)
        meta = {k: v for k, v in item.meta.items() if k not in SKIP_META}
        row = {"ts": round(time.time(), 2), "agent": agent.id, "round": round_of(agent), "id": item.id, "kind": item.kind,
               "text": item.text, "meta": meta}
        self._append(self.path, row)

    def _append(self, path: Path, row: dict[str, Any]) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass

    def archive_round(self, agent: Agent, messages: list[dict[str, Any]], summary: str, *, before: int, after: int,
                      manual: bool, system_prompt: str, tools: list[str], info: dict[str, Any]) -> None:
        """Called by a compaction, with the history it is about to replace."""
        rnd = round_of(agent)
        data = {"agent": agent.id, "name": agent.name, "round": rnd, "ended": round(time.time(), 2), "manual": manual,
                "tokens_before": before, "tokens_after": after, "info": info, "summary": summary,
                "system_prompt": system_prompt, "tools": tools, "messages": messages}
        try:
            d = self.session.dir / "rounds"
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{agent.id}-r{rnd:03d}.json").write_text(json.dumps(data, ensure_ascii=False, default=str, indent=1))
        except OSError:
            pass
