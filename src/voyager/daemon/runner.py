"""The daemon process: `voyager --serve` hosts one session and serves it to whoever attaches (see server.py).

Started by launcher.spawn, detached from the terminal. It keeps running while nobody is attached, and exits when a
client asks it to shut down (`--stop`, or quitting an idle session in the TUI), on SIGTERM / SIGINT, or after
`--idle-exit` seconds without clients and without work. Its session is saved on the way out, like any other.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

from ..session import Session
from .launcher import config_from_json
from .server import SessionServer


async def serve(a) -> int:  # noqa: ANN001  (argparse.Namespace)
    cfg = config_from_json(Path(a.config_file).read_text(encoding="utf-8"))
    try:
        session = Session.load(cfg, a.resume) if a.resume else Session(cfg, session_id=a.session_id)
    except (FileNotFoundError, ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    server = SessionServer(session, idle_exit=a.idle_exit or 0.0)
    await server.start()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        loop.add_signal_handler(sig, server.stopped.set)
    print(f"session {session.id} serving on {server.path}", flush=True)
    if a.prompt:
        session.main.submit(a.prompt)
    await server.stopped.wait()
    session.stop_all()
    await server.close()  # clients get "bye" while the agents unwind
    await session.shutdown()
    return 0
