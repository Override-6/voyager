"""Agent base class: an asyncio worker with an inbox, states, stop and persistence.

One event loop runs everything (no threads). Each agent owns:
  * a *worker task* that drains the inbox, one turn at a time, and exits when the inbox is empty
  * a *turn task* (child of the worker) that `stop()` cancels; a stopped turn ends the turn only
Messages arriving while a turn runs (user typing, sub-agent notifications) queue in `inbox`;
a local-model agent picks them up at the next step boundary, otherwise they start the next turn.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from .log import AgentLog

if TYPE_CHECKING:
    from .session import Session


class AgentError(Exception):
    """A failure the user should see; ends the turn, not the session."""


class Agent:
    kind = "local"  # "local" (the local model) | "coder" (claude -p)

    def __init__(
        self, session: "Session", agent_id: str, name: str, *, parent_id: str | None = None, prompt: str = ""
    ) -> None:
        self.session = session
        self.id = agent_id
        self.name = name
        self.parent_id = parent_id
        self.prompt = prompt
        self.log = AgentLog()
        self.log.listeners.append(lambda ev, item, extra: session.log_event(self, ev, item, extra))
        self.messages: list[dict[str, Any]] = []  # model-facing history
        self.inbox: list[str] = []
        self.running = False
        self.outcome = ""  # "" | done | stopped | error   (result of the last turn)
        self.activity = ""  # short "what is it doing now" for the agent list
        self.last_report = ""
        self.unseen = False  # finished while the user was looking elsewhere
        self.tool_count = 0
        self.started_at = time.monotonic()
        self._worker_task: asyncio.Task[None] | None = None
        self._turn_task: asyncio.Task[None] | None = None
        self._closing = False
        self.compact_requested = False  # /compact: summarize the discussion at the next turn boundary

    # ------------------------------------------------------------------ state
    @property
    def is_main(self) -> bool:
        return self.parent_id is None

    @property
    def status(self) -> str:
        if self.running:
            return "running"
        if self.outcome == "done" and self.is_main:
            return "idle"
        return self.outcome or "idle"

    # --------------------------------------------------------------- messaging
    def submit(self, text: str, *, src: str = "user", shown: str | None = None) -> None:
        """Queue a message for this agent; starts (resumes) it if idle. Call from the event loop."""
        if src == "agent":
            self.log.add("notice", shown or text)  # sub-agent notification: model sees `text`
        else:
            self.log.add("user", shown or text, src=src)
        self.inbox.append(text)
        self._ensure_worker()

    def request_compact(self) -> None:
        """Ask a local-model agent to compact its context now (runs as a turn of its own if idle)."""
        self.compact_requested = True
        self._ensure_worker()

    def _ensure_worker(self) -> None:
        if not self.running:
            self.running = True
            self.outcome = ""
            self._worker_task = asyncio.get_running_loop().create_task(self._worker())

    def take_inbox(self) -> list[str]:
        items, self.inbox = self.inbox, []
        return items

    def stop(self) -> None:
        """User pressed stop: cancel the current turn and drop queued messages."""
        self.inbox.clear()
        if self._turn_task and not self._turn_task.done():
            self._turn_task.cancel()

    async def close(self) -> None:
        """Session shutdown: cancel everything and wait for the worker to unwind."""
        self._closing = True
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            await asyncio.gather(self._worker_task, return_exceptions=True)

    # ------------------------------------------------------------------ worker
    async def _worker(self) -> None:
        try:
            while self.inbox or self.compact_requested:
                text = "\n\n".join(self.take_inbox())
                self.outcome = ""
                self._turn_task = asyncio.create_task(self.run_turn(text))
                try:
                    await self._turn_task
                    self.outcome = "done"
                except asyncio.CancelledError:
                    if self._closing:
                        raise
                    self.outcome = "stopped"
                    self.log.add("notice", "⏹ stopped")
                except AgentError as e:
                    self.outcome = "error"
                    self.log.add("error", str(e))
                except Exception as e:  # a bug must not kill the session
                    self.outcome = "error"
                    self.log.add("error", f"{type(e).__name__}: {e}")
                self.compact_requested = False  # never loop on a stale request (e.g. cancelled mid-compaction)
                self.activity = ""
                self.session.save()
                self.session.events.turn_end(self)
        finally:
            self.running = False
            if not self._closing:
                self.session.agent_finished(self)

    async def run_turn(self, text: str) -> None:
        raise NotImplementedError

    def report_extra(self) -> str:
        """Extra info appended to the parent's notification (e.g. where files are)."""
        return ""

    # ------------------------------------------------------------- persistence
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "parent_id": self.parent_id,
            "prompt": self.prompt,
            "messages": self.messages,
            "items": self.log.dump(),
            "outcome": "stopped" if self.running else self.outcome,
            "last_report": self.last_report,
            "tool_count": self.tool_count,
            "extra": self.extra_state(),
        }

    def restore(self, d: dict[str, Any]) -> None:
        self.messages = d.get("messages", [])
        self.log.load(d.get("items", []))
        self.outcome = d.get("outcome", "")
        self.last_report = d.get("last_report", "")
        self.tool_count = d.get("tool_count", 0)
        self.load_extra_state(d.get("extra", {}))

    def extra_state(self) -> dict[str, Any]:
        return {}

    def load_extra_state(self, extra: dict[str, Any]) -> None:
        pass
