"""Session: owns all agents, spawning, parent notification, persistence."""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any, Callable

import anthropic

from .agent import Agent
from .local import LocalAgent
from .coder import CoderAgent
from .config import Config
from .events import EventWriter
from .log import Item
from .mode import mode_for
from .sessions_store import list_saved, make_meta, write_meta
from .mcpclient.manager import McpManager
from .tasks import TaskManager
from .tools import Tool, ToolError, tools_for
from .tools.base import clip
from .transcript import TranscriptWriter

AGENT_CLASSES: dict[str, type[Agent]] = {"local": LocalAgent, "coder": CoderAgent, "bonsai": LocalAgent}  # "bonsai": legacy name of "local" in saved sessions
Hook = Callable[[Agent, str, Item, Any], None]
RESUME_TEXT = (
    "This session was stopped and has just been resumed; the conversation above is exactly as it was. Continue from where "
    "you left off without repeating what is already done."
)


class Session:
    def __init__(self, cfg: Config, *, session_id: str | None = None, client: Any = None) -> None:
        self.cfg = cfg
        self.id = session_id or dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.dir: Path = cfg.sessions_dir / self.id
        self.agents: dict[str, Agent] = {}
        # chat or voyager: a session is in voyager mode when its cwd is a workspace (so a resumed one finds it by itself)
        self.mode = mode_for(cfg)
        self.workspace = self.mode.workspace  # None in chat mode
        self.events = EventWriter(self)  # live events.jsonl, for monitoring from outside (see events.py)
        self.transcript = TranscriptWriter(self)  # the permanent, complete record (see transcript.py)
        self.hooks: list[Hook] = [self.events, self.transcript]  # front ends subscribe to every agent's log events here
        self._client = client
        self._counter = 0
        self._resumed = False  # set by load(): see resume_work()
        self.tasks = TaskManager(self)
        self.mcp = McpManager(self)
        self.main: Agent = LocalAgent(self, "main", "main")
        self.agents["main"] = self.main

    # ----------------------------------------------------------------- basics
    @property
    def client(self) -> anthropic.AsyncAnthropic:
        # llama-server speaks the Anthropic /v1/messages API natively and needs no real key.
        if self._client is None:
            self._client = anthropic.AsyncAnthropic(base_url=self.cfg.base_url, api_key="local", max_retries=1)
        return self._client

    def tools_for(self, agent: Agent) -> dict[str, Tool]:
        return {**tools_for(agent.is_main), **self.mode.tools(), **self.mcp.tools()}  # MCP tools appear once a server has schemas

    def log_event(self, agent: Agent, event: str, item: Item, extra: Any) -> None:
        for hook in self.hooks:
            hook(agent, event, item, extra)

    def ordered(self) -> list[Agent]:
        return list(self.agents.values())

    def resolve(self, ref: str) -> Agent | None:
        ref = ref.strip()
        if ref in self.agents:
            return self.agents[ref]
        for a in self.agents.values():
            if a.name == ref:
                return a
        matches = [a for a in self.agents.values() if a.id.startswith(ref) or a.name.startswith(ref)]
        return matches[0] if len(matches) == 1 else None

    # ---------------------------------------------------------------- spawning
    def spawn(self, parent: Agent, name: str, prompt: str, kind: str = "local") -> Agent:
        cls = AGENT_CLASSES.get(kind)
        if cls is None:
            raise ToolError(f"unknown agent type {kind!r}; use one of: {', '.join(AGENT_CLASSES)}")
        if not prompt.strip():
            raise ToolError("prompt is empty")
        running = sum(1 for a in self.agents.values() if not a.is_main and a.running)
        if running >= self.cfg.max_running_agents:
            raise ToolError(f"{running} agents already running (max {self.cfg.max_running_agents}); wait for one to finish")
        self._counter += 1
        agent = cls(self, f"a{self._counter}", self._unique_name(name), parent_id=parent.id, prompt=prompt)
        self.agents[agent.id] = agent
        agent.submit(prompt, src=parent.id)
        self.save()
        return agent

    def _unique_name(self, name: str) -> str:
        base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] or "agent"
        taken = {a.name for a in self.agents.values()}
        cand, n = base, 2
        while cand in taken:
            cand, n = f"{base}-{n}", n + 1
        return cand

    # ---------------------------------------------------------- notifications
    def agent_finished(self, agent: Agent) -> None:
        """A sub-agent's worker ended: tell its parent (waking it if it is idle)."""
        parent = self.agents.get(agent.parent_id or "")
        if parent is None:
            return
        agent.unseen = True
        who = f"{agent.name} ({agent.id})"
        if agent.outcome == "stopped":  # the user did this; no need to wake the parent
            parent.log.add("notice", f"◀ {who} was stopped")
            return
        if agent.outcome == "error":
            errors = [i.text for i in agent.log.items if i.kind == "error"]
            report, status, verb = errors[-1] if errors else "unknown error", "error", "failed"
        else:
            report, status, verb = agent.last_report or "(no report)", "done", "finished"
        text = (
            f'<agent-notification agent="{agent.id}" name="{agent.name}" type="{agent.kind}" status="{status}">\n'
            f"Agent {who} {verb}.\n\nFinal report:\n{clip(report, 6000)}\n{agent.report_extra()}\n"
            "</agent-notification>"
        )
        parent.submit(text, src="agent", shown=f"◀ {who} {verb}")

    # -------------------------------------------------------------- lifecycle
    async def wait_idle(self) -> None:
        """Wait until no agent is running and no background task is pending (their notifications may wake agents)."""
        while pending := [a._worker_task for a in self.agents.values() if a.running and a._worker_task] + [
            b.task for b in self.tasks.running() if b.task
        ]:
            await asyncio.wait(pending)

    def stop_all(self) -> None:
        for a in self.agents.values():
            a.stop()
        self.tasks.kill_all()

    def reset(self) -> None:
        """/clear: forget everything except the (emptied) main agent."""
        self.stop_all()
        self.tasks.reset()
        self.agents = {"main": self.main}
        self.main.messages.clear()
        self.main.log.items.clear()
        self.main.inbox.clear()
        self.main.last_report = self.main.activity = ""

    detachable = False  # a session that lives in this process ends with it; daemon sessions (daemon/) can be left running

    def busy(self) -> bool:
        return any(a.running for a in self.agents.values()) or bool(self.tasks.running())

    def set_thinking(self, n: int) -> None:
        self.cfg.thinking_budget = max(0, n)

    async def leave(self, keep: bool | None = None) -> str:
        """The user quits this view of the session: stop everything and save it (`keep` cannot apply here)."""
        self.stop_all()
        await self.shutdown()
        return "stopped"

    async def shutdown(self) -> None:
        await asyncio.gather(*(a.close() for a in self.agents.values()))
        await self.tasks.shutdown()
        await self.mcp.shutdown()
        self.save()
        self.events.close()
        if self._client is not None:
            await self._client.close()

    # ------------------------------------------------------------ persistence
    @property
    def path(self) -> Path:
        return self.dir / "session.json"

    def is_empty(self) -> bool:
        return len(self.agents) == 1 and not self.main.log.items

    def save(self) -> None:
        if self.is_empty():
            return  # nothing worth resuming: don't litter the sessions folder
        data = {
            "id": self.id,
            "cwd": str(self.cfg.cwd),
            "counter": self._counter,
            "agents": [a.to_dict() for a in self.agents.values()],
        }
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, default=str))
            tmp.replace(self.path)
            write_meta(self.dir, make_meta(self.id, data["cwd"], data["agents"]))
        except OSError:
            pass  # a failed save must never take an agent down

    @classmethod
    def load(cls, cfg: Config, ref: str | None = None, client: Any = None) -> "Session":
        """Restore a saved session. ref = session id, or None for the latest one in this directory."""
        root = cfg.sessions_dir
        if ref:
            path = root / ref / "session.json"
            if not path.is_file():  # accept a unique id prefix
                found = [d for d in root.glob(f"{ref}*/") if (d / "session.json").is_file()]
                if len(found) > 1:
                    raise FileNotFoundError(f"ambiguous session id {ref!r}: " + ", ".join(sorted(d.name for d in found)[:5]))
                if found:
                    path = found[0] / "session.json"
        else:
            infos = list_saved(root, cwd=str(cfg.cwd))
            if not infos:
                raise FileNotFoundError(f"no saved session for {cfg.cwd} under {root}")
            path = root / infos[0].id / "session.json"
        if not path.is_file():
            raise FileNotFoundError(f"no such session: {ref or path}")
        data = json.loads(path.read_text())
        # The mode is derived from the cwd (a workspace = voyager mode), so a session resumes where it was started,
        # wherever it is resumed from; a copy of the config, so the caller's own stays untouched.
        saved = Path(data.get("cwd") or cfg.cwd)
        if saved != cfg.cwd and saved.is_dir():
            cfg = dataclasses.replace(cfg, cwd=saved)
        s = cls(cfg, session_id=data["id"], client=client)
        s._counter = data.get("counter", 0)
        for d in data["agents"]:
            if d["id"] == "main":
                agent = s.main
            else:
                agent = AGENT_CLASSES[d["kind"]](s, d["id"], d["name"], parent_id=d["parent_id"], prompt=d.get("prompt", ""))
                s.agents[agent.id] = agent
            agent.restore(d)
        s._resumed = True  # its front end calls resume_work() once the event loop runs
        return s

    def resume_work(self, note: str = "") -> bool:
        """A resumed session picks its work up by itself instead of waiting for a message.

        Sends the main agent back to work when its last turn did not end cleanly (stopped, failed, or cut off with a
        message or tool results it never answered) or when its mode says there is still work (an unfinished plan).
        A conversation whose last answer was delivered stays quiet. Once per resume; needs a running event loop."""
        if not self._resumed:
            return False
        self._resumed = False
        m = self.main
        if not m.messages or not (m.messages[-1]["role"] == "user" or m.outcome in ("stopped", "error") or m.mode.wants_continue()):
            return False
        cut = [f"{a.id} ({a.name})" for a in self.agents.values() if a is not m and a.outcome in ("stopped", "error")]
        parts = [RESUME_TEXT, m.mode.resume_note(), note]
        if cut:
            parts.append(f"Sub-agents that were interrupted by the stop: {', '.join(cut)}. Resume the ones whose work you still need with send_message.")
        m.submit("<resumed>\n" + " ".join(p for p in parts if p) + "\n</resumed>", src="agent",
                 shown="↻ session resumed: continuing where it stopped")
        return True
