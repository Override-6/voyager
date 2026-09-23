"""Where the TUI's sessions come from. Two backends with the same two methods:

* LocalBackend (here): sessions live inside the TUI process and end with it (`--local`, and the tests).
* DaemonBackend (daemon/backend.py): sessions live in background daemons; the TUI only attaches to them.
"""

from __future__ import annotations

from ..config import Config
from ..session import Session


class LocalBackend:
    async def new_session(self, cfg: Config) -> Session:
        return Session(cfg)

    async def load_session(self, cfg: Config, ref: str) -> Session:
        return Session.load(cfg, ref)
