"""What the agent actually looked up, recorded by the harness (never self-reported).

`scratch/.evidence.json` in the workspace counts web searches, page fetches and workspace searches and remembers the
URLs it saw. The approach gate (approach.py) uses it so that "Prior art" cannot be written from memory: the research
must have happened, and the URLs cited must be ones the agent really came across. Workspace-scoped, so it survives
resumes and covers sub-agents too. Best effort: a failure to record never fails a tool call.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .workspace import Workspace

PATH = "scratch/.evidence.json"
TOOLS = ("web_search", "web_fetch", "search_workspace")
MAX_URLS = 500
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")


def norm(url: str) -> str:
    """Comparable form of a URL: no scheme, fragment, query, trailing punctuation or slash."""
    return re.sub(r"^https?://(www\.)?", "", url.split("#")[0].split("?")[0]).rstrip("/.,;:").lower()


@dataclass
class Evidence:
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(TOOLS, 0))
    urls: list[str] = field(default_factory=list)

    def knows(self, url: str) -> bool:
        n = norm(url)
        return any(n == u or n.startswith(u + "/") or u.startswith(n + "/") for u in map(norm, self.urls))


def load(ws: "Workspace") -> Evidence:
    try:
        data = json.loads((ws.root / PATH).read_text())
        return Evidence({t: int(data.get("counts", {}).get(t, 0)) for t in TOOLS}, [str(u) for u in data.get("urls", [])])
    except (OSError, ValueError, AttributeError):
        return Evidence()


def record(ws: "Workspace", tool: str, args: dict[str, Any], output: str) -> None:
    """Called by the harness after a tool call succeeded."""
    if tool not in TOOLS:
        return
    ev = load(ws)
    ev.counts[tool] += 1
    if tool != "search_workspace":
        seen = ([str(args["url"])] if args.get("url") else []) + URL_RE.findall(output)
        ev.urls = list(dict.fromkeys([*ev.urls, *seen]))[-MAX_URLS:]
    try:
        p = ws.root / PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"counts": ev.counts, "urls": ev.urls}))
    except OSError:
        pass
