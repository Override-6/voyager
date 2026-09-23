"""The command-line side of the daemon: launch a session in the background, list, attach (text only), send, stop.

  voyager --detach [--voyager NAME] "task"        start a session daemon, print its id, return
  voyager --ps                                    list the running session daemons
  voyager --attach [ID] --cli                     watch a session live and talk to it, in plain text
  voyager --attach [ID]                           the same in the full-screen TUI (tui/app.py)
  voyager --send ID "message"                     one message to a session, then return
  voyager --stop ID                               shut a session daemon down (the session is saved)
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import stat
import sys
from typing import Any

from ..config import Config
from ..plain import PlainPrinter
from .client import RemoteAgent, RemoteSession
from .launcher import LaunchError, LiveSession, find_live, live_sessions, spawn

REPLAY_ITEMS = 12
CLI_HELP = "commands: /stop (stop the main agent) · /compact · /status · /detach (Ctrl-C too: the session keeps running) · /shutdown (end the session)"


def pick_live(cfg: Config, ref: str | None) -> LiveSession | None:
    live = find_live(cfg.sessions_dir, ref)
    if live is None:
        running = live_sessions(cfg.sessions_dir)
        what = f"no running session matches {ref!r}" if ref else "no running session"
        hint = "running: " + ", ".join(s.id for s in running) if running else "start one with: voyager --detach \"task\""
        print(f"error: {what} ({hint})", file=sys.stderr)
    return live


async def connect(cfg: Config, live: LiveSession) -> RemoteSession:
    return await RemoteSession(cfg, live.sock).connect()


# ------------------------------------------------------------------- launching
async def cmd_detach(cfg: Config, prompt: str | None, idle_exit: float = 0.0) -> int:
    try:
        live = await spawn(cfg, prompt=prompt, idle_exit=idle_exit)
    except LaunchError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(live.id)
    print(f"running in the background (pid {live.pid}, {live.mode} mode). attach: voyager --attach {live.id} [--cli] · "
          f"monitor: voyager --monitor {live.id} -f · stop: voyager --stop {live.id}", file=sys.stderr)
    return 0


# --------------------------------------------------------------------- queries
async def cmd_ps(cfg: Config) -> int:
    live = live_sessions(cfg.sessions_dir)
    if not live:
        print("no running session daemons")
        return 0
    print(f"{'ID':<20}{'PID':<8}{'MODE':<9}{'STATE':<9}{'ROUND':<6}{'CLIENTS':<8}WHERE")
    for s in live:
        st: dict[str, Any] = {}
        with contextlib.suppress(Exception):
            rs = await connect(cfg, s)
            st = (await rs.request({"op": "status"})).get("status", {})
            rs.close()
        where = s.workspace or s.cwd
        print(f"{s.id:<20}{s.pid:<8}{s.mode:<9}{st.get('state', '?'):<9}{st.get('round') if st.get('round') is not None else '-':<6}"
              f"{max(0, st.get('clients', 1) - 1):<8}{where}")  # minus this query's own connection
    return 0


async def cmd_send(cfg: Config, ref: str | None, text: str) -> int:
    live = pick_live(cfg, ref)
    if live is None:
        return 2
    rs = await connect(cfg, live)
    rs.main.submit(text)
    await rs.request({"op": "status"})  # a round trip: the message was read before we hang up
    rs.close()
    print(f"sent to {live.id}", file=sys.stderr)
    return 0


async def cmd_stop(cfg: Config, ref: str | None) -> int:
    live = pick_live(cfg, ref)
    if live is None:
        return 2
    rs = await connect(cfg, live)
    await rs.leave(keep=False)
    print(f"stopped {live.id} (saved: voyager --resume {live.id})", file=sys.stderr)
    return 0


# ------------------------------------------------------------------ attach: CLI
def replay(printer: PlainPrinter, agent: RemoteAgent, n: int = REPLAY_ITEMS) -> None:
    """Print the tail of the transcript with the same printer the live events use."""
    for item in agent.log.items[-n:]:
        if item.kind in ("thinking", "text"):
            printer(agent, "start", item, None)
            printer(agent, "delta", item, item.text)
            printer(agent, "end", item, None)
        elif item.kind == "tool":  # the printer prints the call when it sees "running", the result when it sees the end
            final = item.meta.get("status")
            for status in ("running", final):
                item.meta["status"] = status
                printer(agent, "update", item, None)
            item.meta["status"] = final
        elif item.kind != "usage":
            printer(agent, "start", item, None)
    printer._status.clear()


async def _stdin_lines() -> asyncio.StreamReader | None:
    try:
        mode = os.fstat(sys.stdin.fileno()).st_mode
        if not (sys.stdin.isatty() or stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)):
            return None  # /dev/null or a file: epoll cannot watch it (and there is nothing to wait for)
        reader = asyncio.StreamReader()
        await asyncio.get_running_loop().connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
        return reader
    except (ValueError, OSError, PermissionError):  # /dev/null, a file: nothing to read, just watch
        return None


async def cmd_attach_cli(cfg: Config, ref: str | None) -> int:
    live = pick_live(cfg, ref)
    if live is None:
        return 2
    rs = await connect(cfg, live)
    out = sys.stderr
    print(f"attached to {rs.id} · {rs.mode.name} mode · {rs.cfg.cwd}\n{CLI_HELP}", file=out)
    printer = PlainPrinter()
    replay(printer, rs.main)
    rs.hooks.append(printer)
    done = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGINT, done.set)
    tasks = [asyncio.create_task(_input_loop(rs, done)), asyncio.create_task(_wait_closed(rs, done))]
    await done.wait()
    for t in tasks:
        t.cancel()
    gone = not rs.connected
    rs.close()
    print("\nsession ended" if gone else f"\ndetached: {rs.id} keeps running (voyager --attach {rs.id})", file=out)
    return 0


async def _wait_closed(rs: RemoteSession, done: asyncio.Event) -> None:
    await rs.wait_closed()
    done.set()


async def _input_loop(rs: RemoteSession, done: asyncio.Event) -> None:
    reader = await _stdin_lines()
    if reader is None:
        return
    while line := await reader.readline():
        text = line.decode(errors="replace").strip()
        if text in ("/detach", "/exit", "/quit"):
            done.set()
        elif text == "/shutdown":
            await rs.leave(keep=False)
            done.set()
        elif text == "/stop":
            rs.main.stop()
        elif text == "/compact":
            rs.main.request_compact()
        elif text == "/status":
            st = (await rs.request({"op": "status"})).get("status", {})
            print("  ".join(f"[{a['id']}] {a['status']} {a['activity']}".strip() for a in st.get("agents", [])), file=sys.stderr)
        elif text in ("/help", "?"):
            print(CLI_HELP, file=sys.stderr)
        elif text:
            rs.main.submit(text)
