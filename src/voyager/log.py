"""Per-agent transcript. The agent writes here; front ends (TUI, plain stdout) read/subscribe.

Everything runs on one asyncio loop, so there are no locks: mutations happen between awaits.

Item kinds and their meta:
  user      text, meta.src ("user" | parent agent id), meta.queued
  thinking  text (streamed), meta.done, meta.expanded
  text      text (streamed), meta.done
  tool      meta: name, summary, status (args|running|done|error), args, args_chars,
            preview, live[], result, expanded
  notice / error / usage   text
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

MAX_LIVE_LINES = 500

Listener = Callable[[str, "Item", Any], None]


@dataclass
class Item:
    id: int
    kind: str
    text: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    version: int = 0  # bumped on every change; render caches key on it

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "text": self.text, "meta": self.meta}


class AgentLog:
    def __init__(self) -> None:
        self.items: list[Item] = []
        self.listeners: list[Listener] = []
        self._next_id = 0

    def _emit(self, event: str, item: Item, extra: Any = None) -> None:
        for fn in self.listeners:
            fn(event, item, extra)

    def add(self, kind: str, text: str = "", **meta: Any) -> Item:
        """One-shot item (a user message, a notice, ...)."""
        item = self.start(kind, text, **meta)
        self.end(item)
        return item

    def start(self, kind: str, text: str = "", **meta: Any) -> Item:
        item = Item(self._next_id, kind, text, dict(meta))
        self._next_id += 1
        self.items.append(item)
        self._emit("start", item)
        return item

    def delta(self, item: Item, s: str) -> None:
        item.text += s
        item.version += 1
        self._emit("delta", item, s)

    def update(self, item: Item, **meta: Any) -> None:
        item.meta.update(meta)
        item.version += 1
        self._emit("update", item, meta)

    def tool_live(self, item: Item, line: str) -> None:
        live: list[str] = item.meta.setdefault("live", [])
        live.append(line)
        if len(live) > MAX_LIVE_LINES:
            del live[: len(live) - MAX_LIVE_LINES]
        item.version += 1
        self._emit("live", item, line)

    def end(self, item: Item) -> None:
        item.meta["done"] = True
        item.version += 1
        self._emit("end", item)

    def toggle(self, item: Item) -> None:
        item.meta["expanded"] = not item.meta.get("expanded")
        item.version += 1
        self._emit("update", item, {"expanded": item.meta["expanded"]})

    # -- persistence ---------------------------------------------------------
    def dump(self) -> list[dict[str, Any]]:
        return [i.to_dict() for i in self.items]

    def load(self, rows: list[dict[str, Any]]) -> None:
        for r in rows:
            item = Item(self._next_id, r["kind"], r.get("text", ""), r.get("meta", {}))
            self._next_id += 1
            if item.kind == "tool" and item.meta.get("status") in ("args", "running", "background"):
                item.meta["status"] = "error"  # was in flight when the session was saved
            self.items.append(item)
