"""Non-interactive front end (`voyager -p "..."`): streams everything to stdout."""

from __future__ import annotations

import asyncio
import sys
from typing import Any, TextIO

from .agent import Agent
from .log import Item
from .session import Session

DIM, ITALIC, BOLD, CYAN, RED, RESET = "\x1b[2m", "\x1b[3m", "\x1b[1m", "\x1b[36m", "\x1b[31m", "\x1b[0m"


class PlainPrinter:
    """Session hook: main agent streams token by token; sub-agents print tool calls and notices."""

    def __init__(self, out: TextIO = sys.stdout) -> None:
        self.out = out
        self._status: dict[tuple[str, int], str] = {}
        self._headed: set[tuple[str, int]] = set()  # tools whose header line was already printed

    def w(self, s: str) -> None:
        self.out.write(s)
        self.out.flush()

    def __call__(self, agent: Agent, ev: str, item: Item, extra: Any) -> None:
        pre = "" if agent.is_main else f"{DIM}[{agent.id}]{RESET} "
        k = item.kind
        if k in ("thinking", "text") and agent.is_main:
            if ev == "start":
                self.w(f"{DIM}{ITALIC}✻ Thinking…\n  " if k == "thinking" else f"{BOLD}⏺{RESET} ")
            elif ev == "delta":
                self.w(str(extra).replace("\n", "\n  ") if k == "thinking" else str(extra))
            elif ev == "end":
                self.w(f"{RESET}\n")
        elif k == "tool":
            self._tool(agent, ev, item, extra, pre)
        elif k in ("notice", "error", "usage", "user") and ev == "start":
            style = RED if k == "error" else DIM
            label = f"{CYAN}❯ {item.text}{RESET}" if k == "user" and agent.is_main else f"{style}{item.text}{RESET}"
            if k != "usage" or agent.is_main:
                self.w(f"{pre}{label}\n")

    def _tool(self, agent: Agent, ev: str, item: Item, extra: Any, pre: str) -> None:
        m = item.meta
        if ev == "live" and agent.is_main:
            self.w(f"{DIM}    │ {extra}{RESET}\n")
        status = m.get("status")
        key = (agent.id, item.id)
        if ev != "update" or status == self._status.get(key):
            return
        self._status[key] = status
        head = f"{pre}{CYAN}{BOLD}⏺ {m.get('name')}{RESET}{CYAN}({m.get('summary', '')}){RESET}"
        if status in ("running", "error") and key not in self._headed:
            self._headed.add(key)
            self.w(f"{head}\n")
        if status in ("done", "error"):
            lines = str(m.get("result", "")).rstrip().splitlines() or ["(empty)"]
            style = RED if status == "error" else DIM
            body = lines[-1:] if m.get("name") == "bash" else lines[:6]
            for i, line in enumerate(body):
                self.w(f"{style}{'  ⎿ ' if i == 0 else '    '}{line[:200]}{RESET}\n")


async def run_plain(session: Session, prompt: str, *, attachable: bool = True) -> int:
    """Run `prompt` to completion in this process. While it runs, `voyager --attach <id>` can watch and steer it."""
    from .daemon.server import SessionServer  # only -p needs it

    server = SessionServer(session) if attachable else None
    if server:
        await server.start()
    session.hooks.append(PlainPrinter())
    session.main.submit(prompt)
    try:
        await session.wait_idle()
    except (KeyboardInterrupt, asyncio.CancelledError):
        session.stop_all()
        await session.wait_idle()
    finally:
        if server:
            await server.close()
    await session.shutdown()
    return 1 if session.main.outcome == "error" else 0
