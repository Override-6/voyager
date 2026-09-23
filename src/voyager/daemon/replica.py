"""The pieces of a session replica (see client.py): transcript, agents, background tasks, mode.

Each offers what the TUI and the CLI printer read of its real counterpart, and forwards what they ask it to do
(submit, stop, compact, kill) to the daemon through the RemoteSession.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from ..log import AgentLog, Item, MAX_LIVE_LINES

if TYPE_CHECKING:
    from .client import RemoteSession


class MirrorLog(AgentLog):
    """A transcript that applies the daemon's changes (same items, same ids) instead of producing its own."""

    def __init__(self) -> None:
        super().__init__()
        self._by_id: dict[int, Item] = {}

    def replace(self, rows: list[dict[str, Any]]) -> None:
        self.items = [Item(r["id"], r["kind"], r.get("text", ""), r.get("meta", {})) for r in rows]
        self._by_id = {i.id: i for i in self.items}
        self._next_id = max(self._by_id, default=-1) + 1

    def item(self, item_id: int) -> Item | None:
        return self._by_id.get(item_id)

    def apply(self, msg: dict[str, Any]) -> None:
        e = msg.get("e")
        if e == "start":
            r = msg["item"]
            item = Item(r["id"], r["kind"], r.get("text", ""), r.get("meta", {}))
            self.items.append(item)
            self._by_id[item.id] = item
            self._emit("start", item)
            return
        item = self._by_id.get(msg.get("id", -1))
        if item is None:  # older than the snapshot's window, or cleared meanwhile
            return
        item.version += 1
        x = msg.get("x")
        if e == "delta":
            item.text += str(x)
        elif e == "update" and isinstance(x, dict):
            item.meta.update(x)
        elif e == "live":
            live: list[str] = item.meta.setdefault("live", [])
            live.append(str(x))
            del live[: max(0, len(live) - MAX_LIVE_LINES)]
        elif e == "end":
            item.meta["done"] = True
        self._emit(e or "update", item, x)


class RemoteAgent:
    """What the UI reads of an agent, plus the few things it asks an agent to do (sent to the daemon)."""

    def __init__(self, session: "RemoteSession", agent_id: str) -> None:
        self.session, self.id = session, agent_id
        self.name, self.kind, self.parent_id = agent_id, "local", None if agent_id == "main" else "main"
        self.running, self.outcome, self.activity, self.last_report = False, "", "", ""
        self.tool_count, self.queued, self.unseen = 0, 0, False
        self.started_at = time.monotonic()
        self.log = MirrorLog()
        self.log.listeners.append(lambda ev, item, extra: session.log_event(self, ev, item, extra))

    @property
    def is_main(self) -> bool:
        return self.parent_id is None

    @property
    def status(self) -> str:
        if self.running:
            return "running"
        return "idle" if self.outcome == "done" and self.is_main else (self.outcome or "idle")

    def update(self, st: dict[str, Any]) -> None:
        was_running = self.running
        self.name, self.kind, self.parent_id = st["name"], st["kind"], st["parent_id"]
        self.running, self.outcome, self.activity = st["running"], st["outcome"], st["activity"]
        self.tool_count, self.queued, self.last_report = st["tool_count"], st["queued"], st["last_report"]
        if "age" in st:
            self.started_at = time.monotonic() - st["age"]
        if was_running and not self.running and not self.is_main:
            self.unseen = True

    # what the UI asks of an agent: forwarded to the real one
    def submit(self, text: str, **_: Any) -> None:
        self.session.send({"op": "submit", "agent": self.id, "text": text})

    def stop(self) -> None:
        self.session.send({"op": "stop", "agent": self.id})

    def request_compact(self) -> None:
        self.session.send({"op": "compact", "agent": self.id})


class RemoteTask:
    def __init__(self, session: "RemoteSession", st: dict[str, Any]) -> None:
        self.session = session
        self.id = st["id"]
        self.update(st)

    def update(self, st: dict[str, Any]) -> None:
        self.owner_id, self.tool, self.summary, self.status = st["owner_id"], st["tool"], st["summary"], st["status"]
        self.result, self.auto, self.item_id = st["result"], st["auto"], st["item_id"]
        self._elapsed, self._at = st["elapsed"], time.monotonic()

    @property
    def elapsed(self) -> float:
        return self._elapsed + (time.monotonic() - self._at if self.status == "running" else 0)

    @property
    def item(self) -> Item:
        owner = self.session.agents.get(self.owner_id)
        return (owner.log.item(self.item_id) if owner else None) or Item(-1, "tool", "", {})

    def output(self) -> str:
        live = self.item.meta.get("live") or []
        return "\n".join(str(x) for x in live) if live and self.status == "running" else (self.result or "\n".join(str(x) for x in live))


class RemoteTasks:
    def __init__(self, session: "RemoteSession") -> None:
        self.session = session
        self.tasks: dict[str, RemoteTask] = {}

    def get(self, ref: str) -> RemoteTask | None:
        return self.tasks.get(ref.strip())

    def kill(self, task_id: str) -> bool:
        bt = self.tasks.get(task_id)
        if bt and bt.status == "running":
            self.session.send({"op": "kill_task", "id": task_id})
            return True
        return False

    def running(self) -> list[RemoteTask]:
        return [t for t in self.tasks.values() if t.status == "running"]


class RemoteMode:
    def __init__(self) -> None:
        self.name, self.text = "chat", ""

    def status(self, agent: Any = None) -> str:
        return self.text
