"""The /resume conversation picker: browse saved sessions, type to filter, Enter to resume."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config import Config
from ..sessions_store import SessionInfo, ago, list_saved
from .render import DIM

if TYPE_CHECKING:
    from .controller import Controller

ENTRY_ROWS = 3  # title, details, blank


class ResumePicker:
    def __init__(self, cfg: Config, current_id: str) -> None:
        self.cfg, self.current_id = cfg, current_id
        self.all_dirs = False
        self.index = 0
        self.items: list[SessionInfo] = []
        self.reload()

    def reload(self) -> None:
        self.items = list_saved(self.cfg.sessions_dir, cwd=None if self.all_dirs else str(self.cfg.cwd), exclude=self.current_id)
        self.index = 0

    def visible(self, query: str) -> list[SessionInfo]:
        return [i for i in self.items if i.matches(query)]

    def move(self, delta: int, query: str) -> None:
        n = len(self.visible(query))
        self.index = max(0, min(self.index + delta, n - 1)) if n else 0

    def selected(self, query: str) -> SessionInfo | None:
        vis = self.visible(query)
        return vis[min(self.index, len(vis) - 1)] if vis else None

    def toggle_dirs(self) -> None:
        self.all_dirs = not self.all_dirs
        self.reload()


def picker_rows(ctl: "Controller", width: int, height: int) -> list[tuple[list[tuple[Any, ...]], None]]:
    """Rows for the big pane. Fragments carry their own mouse handler (click = resume that conversation)."""
    pk, query = ctl.picker, ctl.buffer.text
    assert pk is not None
    vis = pk.visible(query)
    pk.index = min(pk.index, max(0, len(vis) - 1))
    scope = "all directories" if pk.all_dirs else f"this directory ({Path(pk.cfg.cwd).name})"
    rows: list[list[tuple[Any, ...]]] = [
        [("bold", " Resume a conversation"), (DIM, f"  ·  {len(vis)} in {scope}" + (f' matching "{query}"' if query.strip() else ""))],
        [(DIM, "")],
    ]
    if not vis:
        rows.append([(DIM, "  No saved conversations here." + ("" if pk.all_dirs else " Tab: show every directory."))])
    capacity = max(1, (height - len(rows)) // ENTRY_ROWS)
    start = max(0, min(pk.index - capacity + 1, len(vis) - capacity)) if len(vis) > capacity else 0
    for i in range(start, min(len(vis), start + capacity)):
        info, sel = vis[i], i == pk.index
        h = ctl.picker_handler(info)
        base = "reverse" if sel else ""
        where = f" · {Path(info.cwd).name}" if pk.all_dirs else ""
        n_agents = f"{info.agents - 1} sub-agent{'s' if info.agents != 2 else ''}" if info.agents > 1 else "no sub-agents"
        rows.append([(f"{base} bold", f" {'❯' if sel else ' '} {info.title or '(no title)'} ", h)])
        rows.append([(f"{base} {DIM}" if not sel else base, f"     {ago(info.updated)} · {info.turns} turn{'s' if info.turns != 1 else ''} · {n_agents}{where} · {info.id}", h)])
        rows.append([("", "", h)])
    rows = rows[:height]
    return [(r, None) for r in rows] + [([], None)] * (height - len(rows))
