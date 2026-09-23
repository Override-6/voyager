"""The approach gate: a mission must record *how* it will reach the objective before it acts on the environment.

`knowledge/approach.md` is checked by the harness (never left to the model's good will). It has four sections:
Channels (how the agent observes and acts, with the estimated cost of each), Approaches (at least three that differ
in kind), Prior art (what already exists, with URLs) and Decision (the pick and why the others were rejected).
The checks are structural on purpose: they cannot tell a good decision from a bad one, only that one was made after
real research (evidence.py counts the searches and fetches the harness saw, and the URLs cited must be ones it saw).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .evidence import URL_RE, load

if TYPE_CHECKING:
    from .workspace import Workspace

APPROACH_PATH = "knowledge/approach.md"
SECTIONS = ("Channels", "Approaches", "Prior art", "Decision")
MIN_APPROACHES = 3
MIN_SECTION_CHARS = 40
MIN_SEARCHES, MIN_FETCHES = 3, 2  # web_search queries and pages actually read before "Prior art" counts
ITEM_RE = re.compile(r"^\s*(?:[-*]|\d+[.)]|#{3,4})\s+\S")


def _sections(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    cur = None
    for line in text.splitlines():
        if m := re.match(r"^##\s+(.+?)\s*$", line):
            cur = m.group(1).strip().lower()
            out[cur] = []
        elif cur is not None:
            out[cur].append(line)
    return out


def approach_status(ws: "Workspace") -> str:
    """"" when the approach is recorded and complete, else what is missing (one line, shown to the agent)."""
    text = ws.read(APPROACH_PATH)
    if not text.strip():
        return f"{APPROACH_PATH} does not exist"
    secs = _sections(text)
    for name in SECTIONS:
        body = "\n".join(secs.get(name.lower(), [])).strip()
        if len(body) < MIN_SECTION_CHARS:
            return f"{APPROACH_PATH} has no filled '## {name}' section"
    if sum(bool(ITEM_RE.match(ln)) for ln in secs["approaches"]) < MIN_APPROACHES:
        return f"'## Approaches' in {APPROACH_PATH} lists fewer than {MIN_APPROACHES} approaches (one item each)"
    cited = URL_RE.findall("\n".join(secs["prior art"]))
    if not cited:
        return f"'## Prior art' in {APPROACH_PATH} cites no URL: search the web and cite what you found"
    ev = load(ws)
    c = ev.counts
    if c["web_search"] < MIN_SEARCHES or c["web_fetch"] < MIN_FETCHES:
        return (f"you ran {c['web_search']} web_search and {c['web_fetch']} web_fetch calls: research prior art with at "
                f"least {MIN_SEARCHES} different queries and read at least {MIN_FETCHES} pages (READMEs, licences)")
    if not c["search_workspace"]:
        return "you never ran search_workspace: check what tools/ and knowledge/ already offer"
    if not any(ev.knows(u) for u in cited):
        return "none of the URLs cited under '## Prior art' came from your web searches or fetches: cite real sources"
    return ""


GATE_TEXT = (
    "Approach decision: NOT RECORDED ({why}). Before you act on the environment, do it as your method describes "
    "(Phase 0, step 3): search the workspace and the web for what you can use, adapt or fork, then write {path} "
    "with sections Channels, Approaches (at least {n}, different in kind), Prior art (with URLs) and Decision "
    "(the pick, and why the others were rejected)."
)


def gate_text(ws: "Workspace") -> str:
    """The reminder for the workspace state block; "" once the approach is recorded."""
    why = approach_status(ws)
    return GATE_TEXT.format(why=why, path=APPROACH_PATH, n=MIN_APPROACHES) if why else ""
