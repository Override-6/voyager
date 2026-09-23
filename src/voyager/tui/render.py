"""Log items -> styled, wrapped lines. Pure functions (no prompt_toolkit app state) so they can be tested.

Tool calls and thinking blocks are collapsed by default; clicking one toggles meta["expanded"]
and this module then shows the full parameters and output.
"""

from __future__ import annotations

import json
from typing import Any

from ..log import Item
from .wrap import Line, wrap_line, wrap_text

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
DIM, DIM_I = "ansibrightblack", "italic ansibrightblack"
COLLAPSED_TOOL_LINES = 8
THINKING_TAIL = 8
THINKING_HEAD = 2


def spinner(frame: int) -> str:
    return SPINNER[frame % len(SPINNER)]


def is_animated(item: Item) -> bool:
    if item.kind == "tool":
        return item.meta.get("status") in ("args", "running", "background")
    return item.kind in ("thinking", "compact") and not item.meta.get("done")


def item_lines(item: Item, width: int, frame: int) -> list[Line]:
    fn = {"user": _user, "thinking": _thinking, "text": _text, "tool": _tool, "compact": _compact}.get(item.kind, _note)
    return fn(item, width, frame)


# ---------------------------------------------------------------- simple items
def _user(item: Item, width: int, frame: int) -> list[Line]:
    src = item.meta.get("src", "user")
    prefix = ("bold ansicyan", "❯ ") if src == "user" else ("bold ansimagenta", f"◀ from {src}: ")
    return wrap_text(item.text, "bold", width, first=prefix, hang="  ") + [[]]


def _text(item: Item, width: int, frame: int) -> list[Line]:
    return wrap_text(item.text, "", width, first=("bold", "⏺ "), hang="  ") + ([[]] if item.meta.get("done") else [])


def _note(item: Item, width: int, frame: int) -> list[Line]:
    style = {"error": "bold ansired", "usage": DIM}.get(item.kind, DIM)
    first = ("bold ansired", "✗ ") if item.kind == "error" else ("", "  " if item.kind == "usage" else "")
    return wrap_text(item.text, style, width, first=first, hang="  ")


def _thinking(item: Item, width: int, frame: int) -> list[Line]:
    done, expanded = bool(item.meta.get("done")), bool(item.meta.get("expanded"))
    body = wrap_text(item.text.strip(), DIM_I, width, first=("", "  "), hang="  ") if item.text.strip() else []
    if not done:
        head, body = f"{spinner(frame)} Thinking…", body[-THINKING_TAIL:]
    elif expanded:
        head = "▾ Thought"
    else:
        head = "▸ Thought"
        if len(body) > THINKING_HEAD + 1:
            hidden = len(body) - THINKING_HEAD
            body = body[:THINKING_HEAD] + [[(DIM, f"  … +{hidden} lines (click to expand)")]]
    return [[(DIM_I, head)], *body]


def _compact(item: Item, width: int, frame: int) -> list[Line]:
    m, done, expanded = item.meta, bool(item.meta.get("done")), bool(item.meta.get("expanded"))
    body = wrap_text(item.text.strip(), DIM_I, width, first=("", "  "), hang="  ") if item.text.strip() else []
    if not done:
        head, body = f"{spinner(frame)} Compacting context (~{m.get('before', 0) // 1000}k tokens)…", body[-THINKING_TAIL:]
    else:
        head = f"{'▾' if expanded else '▸'} Context compacted · ~{m.get('before', 0) // 1000}k → ~{m.get('after', 0) // 1000}k tokens"
        if not expanded and len(body) > THINKING_HEAD + 1:
            body = body[:THINKING_HEAD] + [[(DIM, f"  … +{len(body) - THINKING_HEAD} lines (click to expand)")]]
    return [[("ansimagenta", head)], *body]


# ------------------------------------------------------------------ tool calls
def _status_style(item: Item) -> tuple[str, str]:
    st = item.meta.get("status")
    return {"done": ("ansigreen", "⏺"), "error": ("ansired", "⏺")}.get(
        st, ("ansimagenta", "") if st == "background" else ("ansicyan", "")
    )


def _tool(item: Item, width: int, frame: int) -> list[Line]:
    m = item.meta
    color, glyph = _status_style(item)
    glyph = glyph or spinner(frame)
    chevron = "▾" if m.get("expanded") else "▸"
    head: Line = [(f"bold {color}", f"{glyph} {m.get('name', '?')}"), (color, f"({m.get('summary', '')})"), (DIM, f"  {chevron}")]
    lines = wrap_line(head, width, "  ")
    body = _tool_expanded(item, width) if m.get("expanded") else _tool_collapsed(item, width, frame)
    return lines + body


def _tool_collapsed(item: Item, width: int, frame: int) -> list[Line]:
    m = item.meta
    st = m.get("status")
    out: list[Line] = []
    if st == "args":
        return [[(DIM, f"  ⎿ writing arguments… {m.get('args_chars', 0)} chars")]]
    if st == "background":
        live = [str(x) for x in m.get("live", [])][-4:]
        tail = [ln for x in live for ln in wrap_text(x, DIM, width, first=(DIM, "    │ "), hang="    │ ")]
        return tail + [[("ansimagenta", f"  ⧗ running in the background as task {m.get('task_id', '?')}")]]
    if m.get("preview"):
        out += _preview_lines(m["preview"], m.get("preview_kind"), width, limit=20)
    live = [str(x) for x in m.get("live", [])]
    result = str(m.get("result", ""))
    color = "ansired" if st == "error" else DIM
    if live:  # streamed command output: last few lines (the result's last line is the exit code)
        shown = live[-(COLLAPSED_TOOL_LINES - 2):]
        if len(live) > len(shown):
            out.append([(DIM, f"    … {len(live) - len(shown)} earlier lines")])
        for ln in shown:
            out.extend(wrap_text(ln, DIM, width, first=(DIM, "    │ "), hang="    │ "))
        if st in ("done", "error") and result:
            out.append([(color, "  ⎿ " + result.rstrip().splitlines()[-1][:width])])
        return out
    if st in ("done", "error") and result:
        rl = result.rstrip("\n").split("\n")
        for i, ln in enumerate(rl[:COLLAPSED_TOOL_LINES]):
            out.extend(wrap_text(ln, color, width, first=(color, "  ⎿ " if i == 0 else "    "), hang="    "))
        if len(rl) > COLLAPSED_TOOL_LINES:
            out.append([(DIM, f"    … +{len(rl) - COLLAPSED_TOOL_LINES} lines (click to expand)")])
    return out


def _tool_expanded(item: Item, width: int) -> list[Line]:
    m = item.meta
    out: list[Line] = [[("bold", "  Parameters")]]
    args: dict[str, Any] = m.get("args") or {}
    if not args:
        out.append([(DIM, "    (none)")])
    for k, v in args.items():
        val = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, indent=2)
        out.extend(wrap_text(val, "", width, first=("ansicyan", f"    {k}: "), hang="      "))
    if m.get("preview"):
        out.append([("bold", "  Preview")])
        out += _preview_lines(m["preview"], m.get("preview_kind"), width, limit=None)
    text = str(m.get("result") or "\n".join(str(x) for x in m.get("live", [])))
    if text:
        out.append([("bold ansired" if m.get("status") == "error" else "bold", "  Output")])
        for ln in text.rstrip("\n").split("\n"):
            out.extend(wrap_text(ln, "ansired" if m.get("status") == "error" else DIM, width, first=("", "    "), hang="    "))
    return out


def _preview_lines(text: str, kind: str | None, width: int, limit: int | None) -> list[Line]:
    rows = text.split("\n")
    out: list[Line] = []
    for ln in rows[:limit]:
        style = ""
        if kind == "diff":
            style = "ansigreen" if ln.startswith("+") else "ansired" if ln.startswith("-") else "ansicyan" if ln.startswith("@@") else DIM
        out.extend(wrap_text(ln, style, width, first=("", "    "), hang="    "))
    if limit is not None and len(rows) > limit:
        out.append([(DIM, f"    … +{len(rows) - limit} more lines (click to expand)")])
    return out
