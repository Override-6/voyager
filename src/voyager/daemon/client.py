"""RemoteSession: a replica of a session that runs in a daemon, with the surface the TUI and the CLI printer use.

It is fed by the daemon's messages (protocol.py) and behaves like a `Session` for reading (`agents`, `main`, `tasks`,
`hooks`, transcripts as real `AgentLog`s that fire the same events) and forwards what the user does (`submit`, `stop`,
`request_compact`, `reset` ...) to the real session. Attaching, detaching or losing the connection never touches the
agents: they keep running in the daemon.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from pathlib import Path
from typing import Any

from ..config import Config
from ..log import Item
from . import protocol as P
from .replica import RemoteAgent, RemoteMode, RemoteTask, RemoteTasks


class RemoteSession:
    detachable = True  # quitting can leave the session running in its daemon

    def __init__(self, cfg: Config, sock: Path) -> None:
        self.cfg, self.sock = cfg, sock
        self.id, self.path, self.workspace_name = "", Path(), None
        self.agents: dict[str, RemoteAgent] = {}
        self.tasks = RemoteTasks(self)
        self.mode = RemoteMode()
        self.hooks: list[Any] = []
        self.connected = False
        self._closing = False
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._replies: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._n = 0

    # ------------------------------------------------------------ connecting
    async def connect(self, timeout: float = 10.0) -> "RemoteSession":
        reader, self._writer = await asyncio.open_unix_connection(str(self.sock), limit=64 * 1024 * 1024)
        self.connected = True
        self._reader_task = asyncio.create_task(self._read(reader))
        await asyncio.wait_for(self._ready.wait(), timeout)
        return self

    async def _read(self, reader: asyncio.StreamReader) -> None:
        try:
            async for msg in P.read_msgs(reader):
                self._apply(msg)
        except (ConnectionError, OSError):
            pass
        finally:
            self.connected = False
            self._ready.set()
            for fut in self._replies.values():
                if not fut.done():
                    fut.cancel()
            main = self.agents.get("main")
            if main is not None and not self._closing:  # not when we detached ourselves
                main.log.add("notice", "connection to the session daemon closed (it exited: the session was saved, /resume it)")

    async def wait_closed(self) -> None:
        """Returns when the connection is gone (the daemon exited, or we closed it)."""
        if self._reader_task:
            await asyncio.wait({self._reader_task})

    def close(self) -> None:
        """Detach: drop the connection; the daemon and its agents carry on."""
        self._closing = True
        if self._writer:
            with contextlib.suppress(Exception):
                self._writer.close()
        if self._reader_task:
            self._reader_task.cancel()

    # -------------------------------------------------------------- applying
    def _apply(self, msg: dict[str, Any]) -> None:
        t = msg.get("t")
        if t == "snapshot":
            self._snapshot(msg)
        elif t == "log":
            self._agent(msg["a"]).log.apply(msg)
        elif t == "state":
            self._states(msg)
            self._notify()
        elif t == "reply":
            fut = self._replies.pop(msg.get("id"), None)
            if fut and not fut.done():
                fut.set_result(msg)
        elif t == "bye":
            self.connected = False

    def _agent(self, agent_id: str) -> RemoteAgent:
        if agent_id not in self.agents:  # a sub-agent spawned after the snapshot: its first log event may come before its state
            self.agents[agent_id] = RemoteAgent(self, agent_id)
        return self.agents[agent_id]

    def _snapshot(self, msg: dict[str, Any]) -> None:
        s = msg["session"]
        self.id, self.path, self.workspace_name = s["id"], Path(s["path"]), s["workspace"]
        self.mode.name = s["mode"]
        self.cfg = dataclasses.replace(self.cfg, cwd=Path(s["cwd"]), **{k: v for k, v in s.get("cfg", {}).items() if k in P.SHARED_CFG})
        for st in msg["agents"]:
            self._agent(st["id"]).log.replace(st.get("items", []))
        self._states(msg)
        self._ready.set()
        self._notify()

    def _states(self, msg: dict[str, Any]) -> None:
        for st in msg["agents"]:
            self._agent(st["id"]).update(st)
        seen = {st["id"] for st in msg["tasks"]}
        for st in msg["tasks"]:
            if (bt := self.tasks.tasks.get(st["id"])) is not None:
                bt.update(st)
            else:
                self.tasks.tasks[st["id"]] = RemoteTask(self, st)
        for tid in [t for t in self.tasks.tasks if t not in seen]:
            del self.tasks.tasks[tid]
        self.mode.text = msg.get("mode_status", self.mode.text)

    def _notify(self) -> None:
        for hook in self.hooks:  # state changes have no log item: wake the UI with a dummy event
            hook(self.main, "state", Item(-1, "state"), None)

    # ------------------------------------------------------- Session surface
    @property
    def main(self) -> RemoteAgent:
        return self._agent("main")

    def log_event(self, agent: RemoteAgent, event: str, item: Item, extra: Any) -> None:
        for hook in self.hooks:
            hook(agent, event, item, extra)

    def ordered(self) -> list[RemoteAgent]:
        return list(self.agents.values())

    def resolve(self, ref: str) -> RemoteAgent | None:
        ref = ref.strip()
        if ref in self.agents:
            return self.agents[ref]
        for a in self.agents.values():
            if a.name == ref:
                return a
        matches = [a for a in self.agents.values() if a.id.startswith(ref) or a.name.startswith(ref)]
        return matches[0] if len(matches) == 1 else None

    def busy(self) -> bool:
        return any(a.running for a in self.agents.values()) or bool(self.tasks.running())

    def is_empty(self) -> bool:
        return not any(a.log.items for a in self.agents.values())

    # ------------------------------------------------------------ requests
    def send(self, msg: dict[str, Any]) -> None:
        if self._writer is None or not self.connected:
            return
        with contextlib.suppress(ConnectionError, OSError, RuntimeError):
            self._writer.write(P.dumps(msg))

    async def request(self, msg: dict[str, Any], timeout: float = 5.0) -> dict[str, Any]:
        self._n += 1
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._replies[self._n] = fut
        self.send({**msg, "id": self._n})
        return await asyncio.wait_for(fut, timeout)

    def stop_all(self) -> None:
        self.send({"op": "stop_all"})

    def reset(self) -> None:
        self.send({"op": "reset"})

    def set_thinking(self, n: int) -> None:
        self.cfg.thinking_budget = n
        self.send({"op": "set_thinking", "n": n})

    async def leave(self, keep: bool | None = None) -> str:
        """The user quits this view. Detach when asked to (`keep`) or when work is still running; otherwise end the
        session (it is saved, like quitting a normal session). Returns what happened: "detached" or "stopped"."""
        keep = self.busy() if keep is None else keep
        if not keep:
            self._closing = True  # the daemon's goodbye is expected, no "connection closed" notice
            self.send({"op": "shutdown"})
            if self._reader_task:  # the daemon says bye and closes; don't wait forever for a hung one
                await asyncio.wait({self._reader_task}, timeout=15)
        self.close()
        return "detached" if keep else "stopped"
