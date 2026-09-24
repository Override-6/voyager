"""Workspace files the agent must re-read when they changed while it was not looking.

After a compaction the agent only has a summary (and, for PLAN.md and OBJECTIVE.md, a possibly cut-down copy in its
state block); after a resume it has an old conversation. In both cases a key file may have moved on: the agent's own
edits during the ended round, or the user's edits while the session was stopped. `fingerprint` takes the state of
the key files, `changed` compares two of them, and `changed_note` is the instruction (system/voyager/CHANGED_FILES.md).
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from ..config import Config, load_system_prompt
from .approach import APPROACH_PATH

if TYPE_CHECKING:
    from .workspace import Workspace

WATCHED = ("OBJECTIVE.md", "PLAN.md", APPROACH_PATH)


def fingerprint(ws: "Workspace") -> dict[str, str]:
    """A hash of each watched file ("" when it does not exist)."""
    out = {}
    for rel in WATCHED:
        try:
            out[rel] = hashlib.sha1((ws.root / rel).read_bytes()).hexdigest()
        except OSError:
            out[rel] = ""
    return out


def changed(before: dict[str, str] | None, now: dict[str, str]) -> list[str]:
    """Watched files whose content differs from `before` (a file `before` knows nothing about is not reported)."""
    return [rel for rel in WATCHED if before is not None and rel in before and before[rel] != now.get(rel, "")]


def changed_note(cfg: Config, files: list[str]) -> str:
    """The instruction to read `files` before continuing; "" when nothing changed."""
    if not files:
        return ""
    listed = ", ".join(f"`{f}`" for f in files)
    return load_system_prompt(cfg, "voyager/CHANGED_FILES", extra={"files": listed}).strip()
