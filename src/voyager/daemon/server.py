"""SessionServer: lets clients attach to a session that lives in this (background) process.

It mirrors what the session does to whoever is attached: every transcript change is pushed as it happens, agent and
task states are pushed when they change, and the clients' requests (send a message, stop, compact ...) are applied to
the real session. Any number of clients can be attached at once, and none is needed for the agents to keep working.
The wire format is described in protocol.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from typing import TYPE_CHECKING, Any

from ..agent import Agent
from ..log import Item
from . import protocol as P

if TYPE_CHECKING:
    from ..session import Session

TICK_SECS = 0.25
MAX_BUFFERED = 32 * 1024 * 1024  # a client this far behind is dropped rather than allowed to eat memory


class SessionServer:
    def __init__(self, session: "Session", *, idle_exit: float = 0.0) -> None:
        self.session = session
        self.idle_exit = idle_exit  # seconds without clients and without work before the daemon exits (0: never)
        self.stopped = asyncio.Event()
        self.path = P.sock_path(session.dir)
        self._clients: list[asyncio.StreamWriter] = []
        self._server: asyncio.AbstractServer | None = None
        self._tick_task: asyncio.Task[None] | None = None
        self._last_state: dict[str, Any] = {}
        self._idle_since = time.monotonic()

    # -------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        s = self.session
        s.dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()  # a stale socket of a dead daemon
        self._server = await asyncio.start_unix_server(self._client, path=str(self.path), limit=64 * 1024 * 1024)
        s.hooks.append(self._on_log)
        self._tick_task = asyncio.create_task(self._tick())
        ws = s.workspace
        info = {"id": s.id, "pid": os.getpid(), "sock": str(self.path), "cwd": str(s.cfg.cwd), "mode": s.mode.name,
                "workspace": ws.name if ws else None, "started": round(time.time(), 2)}
        (s.dir / "daemon.json").write_text(json.dumps(info))

    async def close(self) -> None:
        for w in list(self._clients):
            self._send(w, {"t": "bye"})
            with contextlib.suppress(Exception):
                w.close()
        if self._tick_task:
            self._tick_task.cancel()
            await asyncio.gather(self._tick_task, return_exceptions=True)
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for p in (self.path, self.session.dir / "daemon.json"):
            with contextlib.suppress(FileNotFoundError):
                p.unlink()

    # ------------------------------------------------------------------ state
    def _states(self) -> dict[str, Any]:
        s = self.session
        return {"agents": [P.agent_state(a) for a in s.ordered()], "tasks": [P.task_state(t) for t in s.tasks.tasks.values()],
                "mode_status": s.mode.status(s.main)}

    def snapshot(self) -> dict[str, Any]:
        s = self.session
        st = self._states()
        for a_state, agent in zip(st["agents"], s.ordered()):
            a_state["items"] = [P.item_dict(i) for i in agent.log.items[-P.SNAPSHOT_ITEMS:]]
        ws, c = s.workspace, s.cfg
        shown = {k: getattr(c, k) for k in P.SHARED_CFG}  # what the UI displays: the daemon's values, not the client's
        return {"t": "snapshot", "session": {"id": s.id, "cwd": str(c.cwd), "mode": s.mode.name, "workspace": ws.name if ws else None,
                                             "path": str(s.path), "cfg": shown}, **st}

    def busy(self) -> bool:
        s = self.session
        return any(a.running for a in s.agents.values()) or bool(s.tasks.running())

    # ------------------------------------------------------------------ output
    def _send(self, w: asyncio.StreamWriter, msg: dict[str, Any]) -> None:
        try:
            if w.transport.get_write_buffer_size() > MAX_BUFFERED:
                raise ConnectionError("client too slow")
            w.write(P.dumps(msg))
        except (ConnectionError, OSError, RuntimeError):
            self._drop(w)

    def _broadcast(self, msg: dict[str, Any]) -> None:
        for w in list(self._clients):
            self._send(w, msg)

    def _drop(self, w: asyncio.StreamWriter) -> None:
        if w in self._clients:
            self._clients.remove(w)
        with contextlib.suppress(Exception):
            w.close()

    def _on_log(self, agent: Agent, event: str, item: Item, extra: Any) -> None:
        if not self._clients:
            return
        msg: dict[str, Any] = {"t": "log", "a": agent.id, "e": event}
        if event == "start":
            msg["item"] = P.item_dict(item)
        else:
            msg["id"] = item.id
            if event in ("delta", "live", "update"):
                msg["x"] = extra
        self._broadcast(msg)

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(TICK_SECS)
            st = self._states()
            key = {"agents": P.without_clocks(st["agents"]), "tasks": P.without_clocks(st["tasks"]), "m": st["mode_status"]}
            if key != self._last_state and self._clients:
                self._broadcast({"t": "state", **st})
            self._last_state = key
            if self.busy() or self._clients:
                self._idle_since = time.monotonic()
            elif self.idle_exit and time.monotonic() - self._idle_since >= self.idle_exit:
                self.stopped.set()

    # ------------------------------------------------------------------- input
    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._send(writer, self.snapshot())  # snapshot and registration in one step: no event can slip in between
        self._clients.append(writer)
        try:
            async for msg in P.read_msgs(reader):
                self._handle(writer, msg)
        except (ConnectionError, OSError):
            pass
        finally:
            self._drop(writer)

    def _agent(self, msg: dict[str, Any]) -> Agent | None:
        return self.session.agents.get(msg.get("agent") or "main")

    def _handle(self, w: asyncio.StreamWriter, msg: dict[str, Any]) -> None:
        s, op = self.session, msg.get("op")
        a = self._agent(msg)
        if op == "submit" and a is not None and str(msg.get("text", "")).strip():
            a.submit(str(msg["text"]))
        elif op == "stop" and a is not None:
            a.stop()
        elif op == "stop_all":
            s.stop_all()
        elif op == "kill_task":
            s.tasks.kill(str(msg.get("id", "")))
        elif op == "compact" and a is not None and a.kind == "local":
            a.request_compact()
        elif op == "reset":
            s.reset()
            self._broadcast(self.snapshot())
        elif op == "set_thinking":
            s.cfg.thinking_budget = max(0, int(msg.get("n", 0)))
        elif op == "status":
            self._send(w, {"t": "reply", "id": msg.get("id"), "status": self.status()})
        elif op == "shutdown":
            self.stopped.set()

    def status(self) -> dict[str, Any]:
        s = self.session
        return {"id": s.id, "mode": s.mode.name, "workspace": s.workspace.name if s.workspace else None, "cwd": str(s.cfg.cwd),
                "state": "running" if self.busy() else "idle", "clients": len(self._clients), "tasks": len(s.tasks.running()),
                "round": getattr(s.main.mode, "round", None), "agents": [
                    {"id": a.id, "name": a.name, "status": a.status, "activity": a.activity, "tools": a.tool_count}
                    for a in s.ordered()]}
