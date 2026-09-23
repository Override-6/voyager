"""UI state + actions. Key bindings, mouse handlers and slash commands all call into this."""

from __future__ import annotations

import asyncio
import dataclasses
import time
from pathlib import Path
from typing import Any, Callable

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.history import FileHistory
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

from ..agent import Agent
from ..log import Item
from ..sessions_store import SessionInfo
from .backend import LocalBackend
from .commands import run_command
from .picker import ResumePicker

class DedupFileHistory(FileHistory):
    """FileHistory that skips consecutive duplicates. Persisted in ~/.voyager/history."""

    def append_string(self, string: str) -> None:
        if self._loaded_strings and self._loaded_strings[0] == string:
            return  # loaded strings are newest-first
        super().append_string(string)


class Controller:
    def __init__(self, session: Any, backend: Any = None) -> None:
        """`session`: a Session, or a daemon's RemoteSession (same surface). `backend` makes/loads the next ones."""
        self.backend = backend or LocalBackend()
        self.keep: bool | None = None  # what quitting does to a daemon session (see exit)
        self.picker: ResumePicker | None = None
        self._swap_task: asyncio.Task[None] | None = None
        self.session = session
        self.focus_id = "main"
        self.task_view: str | None = None
        self.list_mode = False
        self.list_index = 0
        self.scroll: dict[str, int] = {}
        self.last_total: dict[str, int] = {}
        self.cache: dict[tuple[str, int], Any] = {}
        self.flash = ""
        self.flash_until = 0.0
        self.app: Application[int] | None = None
        self._last_ctrl_c = 0.0
        self._handlers: dict[Any, Callable[[MouseEvent], Any]] = {}
        hp = session.cfg.history_path
        hp.parent.mkdir(parents=True, exist_ok=True)
        self.buffer = Buffer(multiline=True, history=DedupFileHistory(str(hp)))
        self.attach(session)

    def attach(self, session: Any) -> None:
        self.session = session
        session.hooks.append(lambda *_: self.app.invalidate() if self.app else None)  # redraw on any agent's log change

    # ------------------------------------------------------------------ state
    @property
    def focused(self) -> Agent:
        return self.session.agents.get(self.focus_id) or self.session.main

    @property
    def view_key(self) -> str:
        return self.task_view or self.focus_id

    def entries(self) -> list[tuple[str, Any]]:
        return [("agent", a) for a in self.session.ordered()] + [("task", t) for t in self.session.tasks.tasks.values()]

    def current_entry_index(self) -> int:
        for i, (kind, obj) in enumerate(self.entries()):
            if (kind == "task" and obj.id == self.task_view) or (kind == "agent" and not self.task_view and obj.id == self.focus_id):
                return i
        return 0

    def say(self, msg: str, secs: float = 4.0) -> None:
        self.flash, self.flash_until = msg, time.monotonic() + secs

    # ------------------------------------------------------------- navigation
    def focus(self, agent_id: str) -> None:
        self.focus_id, self.task_view, self.list_mode = agent_id, None, False
        self.focused.unseen = False

    def open_task(self, task_id: str) -> None:
        self.task_view, self.list_mode = task_id, False

    def back(self) -> None:
        if self.task_view:
            self.task_view = None
        elif self.focus_id != "main":
            self.focus("main")

    def cycle(self, delta: int) -> None:
        ids = [a.id for a in self.session.ordered()]
        i = ids.index(self.focus_id) if self.focus_id in ids else 0
        self.focus(ids[(i + delta) % len(ids)])

    def enter_list(self) -> None:
        self.list_mode, self.list_index = True, self.current_entry_index()

    def list_move(self, delta: int) -> None:
        if delta < 0 and self.list_index == 0:
            self.list_mode = False
            return
        self.list_index = max(0, min(self.list_index + delta, len(self.entries()) - 1))

    def list_select(self) -> None:
        entries = self.entries()
        kind, obj = entries[min(self.list_index, len(entries) - 1)]
        self.focus(obj.id) if kind == "agent" else self.open_task(obj.id)

    def list_kill(self) -> None:
        entries = self.entries()
        kind, obj = entries[min(self.list_index, len(entries) - 1)]
        self._kill_entry(kind, obj)

    def _kill_entry(self, kind: str, obj: Any) -> None:
        if kind == "task":
            self.say(f"killing task {obj.id}…" if self.session.tasks.kill(obj.id) else f"task {obj.id} is not running")
        elif obj.running:
            obj.stop()
            self.say(f"stopping {obj.id}…")
        else:
            self.say(f"{obj.id} is not running")

    def scroll_by(self, n: int) -> None:
        self.scroll[self.view_key] = max(0, self.scroll.get(self.view_key, 0) + n)

    # ------------------------------------------------------------ mouse hooks
    def handler(self, agent: Agent | None, item: Item | None) -> Callable[[MouseEvent], Any]:
        """Transcript lines: wheel scrolls; a click on a tool call / thinking block expands or collapses it."""
        key = (agent.id if agent else "", item.id if item else -1)
        if key not in self._handlers:
            def on_mouse(ev: MouseEvent) -> Any:
                if ev.event_type == MouseEventType.SCROLL_UP:
                    self.scroll_by(3)
                elif ev.event_type == MouseEventType.SCROLL_DOWN:
                    self.scroll_by(-3)
                elif ev.event_type == MouseEventType.MOUSE_UP and ev.button == MouseButton.LEFT and agent and item:
                    if item.kind in ("tool", "thinking", "compact"):
                        agent.log.toggle(item)
                else:
                    return NotImplemented
                return None
            self._handlers[key] = on_mouse
        return self._handlers[key]

    def row_handler(self, kind: str, obj_id: str) -> Callable[[MouseEvent], Any]:
        key = ("row", kind, obj_id)
        if key not in self._handlers:
            def on_mouse(ev: MouseEvent) -> Any:
                if ev.event_type == MouseEventType.MOUSE_UP and ev.button == MouseButton.LEFT:
                    self.focus(obj_id) if kind == "agent" else self.open_task(obj_id)
                    return None
                return NotImplemented
            self._handlers[key] = on_mouse
        return self._handlers[key]

    # ------------------------------------------------------------ input / send
    def submit(self) -> None:
        a = self.focused
        text = self.buffer.text
        if self.task_view:
            self.say("watching a task: Esc to go back, x / Ctrl-C to kill it")
            return
        if not text.strip():
            return
        self.buffer.reset(append_to_history=True)
        self.scroll[a.id] = 0
        if text.strip().startswith("/"):
            self.command(text.strip())
        else:
            a.submit(text.strip())

    def interrupt(self) -> None:
        if self.picker:
            self.close_picker()
            return
        if self.buffer.text:
            self.buffer.reset()
            return
        if self.task_view:
            bt = self.session.tasks.get(self.task_view)
            if bt:
                self._kill_entry("task", bt)
            return
        a = self.focused
        if a.running:
            a.stop()
            self.say(f"stopping {a.id}…")
            return
        now = time.monotonic()
        if now - self._last_ctrl_c < 1.5:
            self.exit()
        self._last_ctrl_c = now
        self.say("Press Ctrl-C again (or Ctrl-D) to exit")

    def exit(self, keep: bool | None = None) -> None:
        """Quit the TUI. `keep` (daemon sessions): True leaves the session running in the background, False ends it,
        None leaves it running only if it still has work in progress."""
        self.keep = keep
        if self.app:
            self.app.exit(result=0)

    # ------------------------------------------------- resume: browse conversations
    def open_picker(self) -> None:
        self.picker, self.list_mode, self.task_view = ResumePicker(self.session.cfg, self.session.id), False, None
        self.buffer.reset()  # the input box becomes the filter box

    def close_picker(self) -> None:
        self.picker = None
        self.buffer.reset()

    def picker_move(self, delta: int) -> None:
        if self.picker:
            self.picker.move(delta, self.buffer.text)

    def picker_select(self, info: SessionInfo | None = None) -> None:
        info = info or (self.picker.selected(self.buffer.text) if self.picker else None)
        if info:
            self.resume_session(info.id, info.cwd)

    def picker_handler(self, info: SessionInfo) -> Callable[[MouseEvent], Any]:
        key = ("pick", info.id)
        if key not in self._handlers:
            def on_mouse(ev: MouseEvent) -> Any:
                if ev.event_type == MouseEventType.MOUSE_UP and ev.button == MouseButton.LEFT:
                    self.picker_select(info)
                    return None
                return NotImplemented
            self._handlers[key] = on_mouse
        return self._handlers[key]

    def resume_session(self, ref: str, cwd: str | None = None) -> None:
        if self._swap_task and not self._swap_task.done():
            return
        self._swap_task = asyncio.get_running_loop().create_task(self._swap(ref, cwd))

    async def _swap(self, ref: str, cwd: str | None) -> None:
        """Replace the running conversation with a saved one. Load first: a bad load must not cost the current one."""
        cfg = self.session.cfg
        # a conversation resumes in the directory it was started in; a copy, so the old session still saves as itself
        new_cfg = dataclasses.replace(cfg, cwd=Path(cwd)) if cwd and Path(cwd).is_dir() else cfg
        try:
            new = await self.backend.load_session(new_cfg, ref)
        except (OSError, ValueError, KeyError, RuntimeError, asyncio.TimeoutError) as e:  # RuntimeError: LaunchError
            self.say(f"cannot resume {ref}: {e}", 6)
            return
        await self._replace(new, f"resumed {new.id} · {len(new.agents)} agent(s) · {Path(new_cfg.cwd).name}")

    def switch_to(self, cwd: Path, message: str, first_message: str | None = None) -> None:
        """Start a new conversation in `cwd` (a workspace root, or back to the launch directory)."""
        if self._swap_task and not self._swap_task.done():
            return
        self._swap_task = asyncio.get_running_loop().create_task(self._switch(cwd, message, first_message))

    async def _switch(self, cwd: Path, message: str, first_message: str | None) -> None:
        try:
            new = await self.backend.new_session(dataclasses.replace(self.session.cfg, cwd=cwd))
        except (OSError, ValueError, RuntimeError, asyncio.TimeoutError) as e:  # RuntimeError: LaunchError
            self.say(f"cannot start a session in {cwd}: {e}", 6)
            return
        await self._replace(new, message, first_message)

    async def _replace(self, new: Any, message: str, first_message: str | None = None) -> None:
        old = self.session
        left = await old.leave()  # local: stopped and saved; daemon: left running if it is busy, else saved
        if left == "detached":
            message += f" · previous session {old.id} keeps running: voyager --attach {old.id}"
        self.attach(new)
        self.picker, self.task_view, self.list_mode = None, None, False
        self.cache.clear(), self.scroll.clear(), self.last_total.clear(), self._handlers.clear()
        self.buffer.reset()
        self.focus("main")
        self.say(message, 6)
        if first_message:  # e.g. /voyager <name> <objective>: the mission starts right away
            new.main.submit(first_message)

    # --------------------------------------------------------------- commands
    def command(self, text: str) -> None:
        run_command(self, text)
