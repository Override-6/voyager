"""Model-facing task tools: tasks (list / show output), kill_task."""

from __future__ import annotations

from typing import Any

from .base import Tool, ToolContext, ToolError, clip


class Tasks(Tool):
    def __init__(self) -> None:
        super().__init__(
            name="tasks",
            description=(
                "List background tasks with their status, or show one task's output so far (pass id). "
                "You are notified automatically when a task finishes, so don't poll in a loop."
            ),
            properties={
                "id": {"type": "string", "description": "Task id (e.g. 't1'). Omit to list all tasks."},
                "tail": {"type": "integer", "description": "Show only the last N lines of output. Default 60"},
            },
        )

    def summary(self, args: dict[str, Any]) -> str:
        return str(args.get("id", ""))

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        tm = ctx.session.tasks
        if not args.get("id"):
            rows = [
                f"{b.id}  {b.tool}: {b.summary[:60]}  status={b.status}  {b.elapsed:.0f}s  owner={b.owner_id}"
                for b in tm.tasks.values()
            ]
            return "\n".join(rows) or "No background tasks."
        bt = tm.get(str(args["id"]))
        if bt is None:
            raise ToolError(f"no such task {args['id']!r}. Call tasks() to list them.")
        tail = max(int(args.get("tail") or 60), 1)
        lines = bt.output().rstrip("\n").split("\n")
        shown = "\n".join(lines[-tail:])
        head = f"task {bt.id} ({bt.tool}: {bt.summary[:80]}) status={bt.status} elapsed={bt.elapsed:.0f}s"
        return clip(f"{head}\n{shown}" + (f"\n… ({len(lines) - tail} earlier lines not shown)" if len(lines) > tail else ""))


class KillTask(Tool):
    def __init__(self) -> None:
        super().__init__(
            name="kill_task",
            description="Stop a running background task.",
            properties={"id": {"type": "string", "description": "Task id, e.g. 't1'"}},
            required=["id"],
        )

    def summary(self, args: dict[str, Any]) -> str:
        return str(args.get("id", ""))

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        bt = ctx.session.tasks.get(str(args["id"]))
        if bt is None:
            raise ToolError(f"no such task {args['id']!r}")
        return f"Killing task {bt.id}." if ctx.session.tasks.kill(bt.id) else f"Task {bt.id} is not running (status={bt.status})."
