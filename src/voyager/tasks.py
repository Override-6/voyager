"""Background tasks: tool calls that outlive the agent step that started them.

A tool call runs as its own asyncio.Task. If the model asked for `run_in_background`, or the call is
still running after `auto_background_secs` (30s), it is *detached*: the agent immediately gets a
"moved to background as task tN" result and continues; when the task ends the owner gets a
`<task-notification>` (waking it if idle). The user can browse / watch / kill tasks in the TUI.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .log import Item
from .tools.base import Tool, ToolContext, clip

if TYPE_CHECKING:
    from .agent import Agent
    from .session import Session

NOTIFY_TAIL_CHARS = 3000


@dataclass
class BackgroundTask:
    id: str
    owner_id: str
    tool: str
    summary: str
    item: Item  # the tool-call item in the owner's transcript; its meta["live"] holds the output so far
    started: float = field(default_factory=time.monotonic)
    ended: float | None = None
    status: str = "running"  # running | done | error | killed
    result: str = ""
    auto: bool = False  # backgrounded automatically after the threshold (vs. requested by the model)
    task: "asyncio.Task[str] | None" = None

    @property
    def elapsed(self) -> float:
        return (self.ended or time.monotonic()) - self.started

    def output(self) -> str:
        live = self.item.meta.get("live") or []
        return "\n".join(str(x) for x in live) if live and self.status == "running" else (self.result or "\n".join(str(x) for x in live))


class TaskManager:
    def __init__(self, session: "Session") -> None:
        self.session = session
        self.tasks: dict[str, BackgroundTask] = {}
        self._n = 0
        self._closing = False

    # ------------------------------------------------------------------- run
    async def run(
        self, agent: "Agent", tool: Tool, args: dict[str, Any], ctx: ToolContext, item: Item, background: bool
    ) -> tuple[str, bool]:
        """Run a tool call. Returns (result text, backgrounded?)."""
        secs = self.session.cfg.auto_background_secs
        t = asyncio.get_running_loop().create_task(tool.run(args, ctx))
        if not background:
            try:
                done, _ = await asyncio.wait({t}, timeout=secs if secs > 0 else None)
            except asyncio.CancelledError:  # the user stopped the agent while it waited: kill the call
                t.cancel()
                await asyncio.gather(t, return_exceptions=True)
                raise
            if done:
                return t.result(), False  # re-raises ToolError etc. for the caller
        bt = self._register(agent, tool, ctx_summary(tool, args), item, t, auto=not background)
        how = "Started in the background" if background else f"Still running after {secs}s, so it was moved to the background"
        return (
            f"{how} as task {bt.id}. You will be notified when it finishes. Continue with other work or end "
            f"your turn; use tasks(id='{bt.id}') to look at its output so far, kill_task to stop it.",
            True,
        )

    def _register(self, agent: "Agent", tool: Tool, summary: str, item: Item, t: "asyncio.Task[str]", auto: bool) -> BackgroundTask:
        self._n += 1
        bt = BackgroundTask(f"t{self._n}", agent.id, tool.name, summary, item, auto=auto, task=t)
        self.tasks[bt.id] = bt
        agent.log.update(item, status="background", task_id=bt.id)
        t.add_done_callback(lambda fut: self._finished(bt, fut))
        return bt

    # ------------------------------------------------------------- lifecycle
    def _finished(self, bt: BackgroundTask, fut: "asyncio.Future[str]") -> None:
        bt.ended = time.monotonic()
        if fut.cancelled():
            bt.status, bt.result = "killed", bt.output() + "\n[killed]"
        elif (exc := fut.exception()) is not None:
            bt.status, bt.result = "error", str(exc) or type(exc).__name__
        else:
            bt.status, bt.result = "done", clip(fut.result())
        owner = self.session.agents.get(bt.owner_id)
        if owner is None or self._closing:
            return
        owner.log.update(bt.item, status="error" if bt.status != "done" else "done", result=bt.result)
        verb = {"done": "finished", "error": "failed", "killed": "was killed"}[bt.status]
        if bt.status == "killed":  # the user did this deliberately; don't wake the agent
            owner.log.add("notice", f"◀ task {bt.id} was killed")
        else:
            text = (
                f'<task-notification task="{bt.id}" agent="{owner.id}" tool="{bt.tool}" status="{bt.status}">\n'
                f"Background task {bt.id} ({bt.tool}: {bt.summary}) {verb} after {bt.elapsed:.0f}s.\n"
                f"Output{_tail_note(bt.result)}:\n{_tail(bt.result, NOTIFY_TAIL_CHARS)}\n</task-notification>"
            )
            owner.submit(text, src="agent", shown=f"◀ task {bt.id} {verb}: {bt.tool} {bt.summary[:60]}")
        self.session.save()

    def get(self, ref: str) -> BackgroundTask | None:
        return self.tasks.get(ref.strip())

    def kill(self, task_id: str) -> bool:
        bt = self.tasks.get(task_id)
        if bt and bt.status == "running" and bt.task:
            bt.task.cancel()
            return True
        return False

    def running(self) -> list[BackgroundTask]:
        return [b for b in self.tasks.values() if b.status == "running"]

    def kill_all(self) -> None:
        for b in self.running():
            self.kill(b.id)

    async def shutdown(self) -> None:
        self._closing = True
        pending = [b.task for b in self.running() if b.task]
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    def reset(self) -> None:
        self.kill_all()
        self.tasks.clear()


def ctx_summary(tool: Tool, args: dict[str, Any]) -> str:
    return tool.summary(args)


def _tail(text: str, limit: int) -> str:
    """Last `limit` chars, starting at a line boundary (never a half line)."""
    if len(text) <= limit:
        return text
    cut = text[-limit:]
    nl = cut.find("\n")
    return cut[nl + 1:] if nl != -1 else cut


def _tail_note(text: str) -> str:
    return "" if len(text) <= NOTIFY_TAIL_CHARS else f" (last lines only, {len(text)} chars in total; tasks(id=...) shows more)"
