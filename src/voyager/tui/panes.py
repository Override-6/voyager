"""Builds what each pane of the screen shows: transcript, agent/task list, header, help line."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from prompt_toolkit.utils import get_cwidth

from ..agent import Agent
from ..log import Item
from ..tasks import BackgroundTask
from .picker import picker_rows
from .render import DIM, is_animated, item_lines, spinner
from .wrap import Line, clean, wrap_text

if TYPE_CHECKING:
    from .controller import Controller

MAX_LIST_ROWS = 6
GLYPHS = {"done": ("✔", "ansigreen"), "error": ("✗", "ansired"), "stopped": ("⏹", "ansiyellow"),
          "killed": ("⏹", "ansiyellow"), "idle": ("○", DIM)}
Rows = list[tuple[Line, Item | None]]


def _fit(text: str, width: int) -> str:
    out, w = "", 0
    for ch in text:
        cw = get_cwidth(ch)
        if w + cw > width:
            return out[:-1] + "…" if out else ""
        out, w = out + ch, w + cw
    return out


# ------------------------------------------------------------------ transcript
def view(ctl: "Controller", width: int, height: int) -> Rows:
    """What the big pane shows: the focused agent's transcript, or a background task's output."""
    if ctl.picker is not None:
        return picker_rows(ctl, width, height)
    if ctl.task_view and (bt := ctl.session.tasks.get(ctl.task_view)):
        return _slice(ctl, bt.id, _task_rows(bt, width), height)
    agent = ctl.focused
    frame = int(time.monotonic() * 10)
    rows: Rows = []
    for item in agent.log.items:
        sig = (item.version, width, frame if is_animated(item) else 0)
        hit = ctl.cache.get((agent.id, item.id))
        if hit is None or hit[0] != sig:
            hit = (sig, item_lines(item, width, frame))
            ctl.cache[(agent.id, item.id)] = hit
        rows.extend((ln, item) for ln in hit[1])
    if not rows:
        rows = [([(DIM, ln)], None) for ln in _welcome(ctl, agent)]
    return _slice(ctl, agent.id, rows, height)


def _slice(ctl: "Controller", key: str, rows: Rows, height: int) -> Rows:
    total = len(rows)
    off = ctl.scroll.get(key, 0)
    if off > 0:  # keep the view still while new lines arrive below it
        off += total - ctl.last_total.get(key, total)
    off = max(0, min(off, max(0, total - height)))
    ctl.scroll[key], ctl.last_total[key] = off, total
    end = total - off
    out = rows[max(0, end - height): end]
    out = [([], None)] * (height - len(out)) + out  # newest content sits at the bottom
    if off > 0 and out:
        out[-1] = ([("reverse ansiyellow", f" ↓ {off} more lines · PgDn to follow ")], None)
    return out


def _task_rows(bt: BackgroundTask, width: int) -> Rows:
    color = {"running": "ansimagenta", "done": "ansigreen"}.get(bt.status, "ansired")
    lines: list[Line] = [
        [(f"bold {color}", f"⧗ task {bt.id}"), ("bold", f" · {bt.tool}: "), ("", bt.summary[:200])],
        [(DIM, f"  {bt.status} · {bt.elapsed:.0f}s · started by {bt.owner_id}"
               f"{' · auto-backgrounded' if bt.auto else ''}"
               f"{' · x / Ctrl-C: kill' if bt.status == 'running' else ''} · Esc: back")],
        [],
    ]
    for ln in clean(bt.output()).rstrip("\n").split("\n") if bt.output().strip() else ["(no output yet)"]:
        lines.extend(wrap_text(ln, "", width, first=("", "  "), hang="  "))
    if bt.status != "running":
        lines += [[], [(f"{color}", f"  — task {bt.status} —")]]
    return [(ln, None) for ln in lines]


def _welcome(ctl: "Controller", agent: Agent) -> list[str]:
    cfg = ctl.session.cfg
    if agent.is_main:
        return [
            "",
            "  voyager — type a task. Agents and background tasks appear under the prompt.",
            f"  model: {cfg.model} @ {cfg.base_url} (ctx {cfg.context_window // 1000}k, compacts at {int(cfg.compact_at * 100)}%)",
            f"  coder: claude -p --model {cfg.coder_model} · tool calls > {cfg.auto_background_secs:.0f}s go to the background",
            "  ↑/↓ history · ↓ on an empty prompt: agent/task list · Tab: next agent · Esc: back",
            "  click a tool call / thinking block to expand or collapse it · /help for commands",
            "  /voyager <name> <objective>: voyager mode, a long-running mission with its own workspace",
        ]
    return ["", f"  {agent.kind} agent {agent.id} ({agent.name}) has no output yet."]


# ------------------------------------------------------------ agent / task list
def list_height(ctl: "Controller") -> int:
    return min(len(ctl.entries()), MAX_LIST_ROWS)


def list_rows(ctl: "Controller", width: int) -> list[list[tuple[Any, ...]]]:
    entries = ctl.entries()
    n = len(entries)
    cur = ctl.list_index if ctl.list_mode else ctl.current_entry_index()
    start = max(0, min(cur - MAX_LIST_ROWS + 1, n - MAX_LIST_ROWS)) if n > MAX_LIST_ROWS else 0
    frame = int(time.monotonic() * 10)
    rows = []
    for i in range(start, min(n, start + MAX_LIST_ROWS)):
        kind, obj = entries[i]
        rows.append(_agent_row(ctl, obj, i, width, frame) if kind == "agent" else _task_row(ctl, obj, i, width, frame))
    return rows


def _row_style(ctl: "Controller", i: int, viewing: bool) -> tuple[str, str]:
    selected = ctl.list_mode and i == ctl.list_index
    head = f" {'❯' if selected else ('●' if viewing else ' ')} "
    return ("reverse" if selected else ("bold" if viewing else "")), head


def _agent_row(ctl: "Controller", a: Agent, i: int, width: int, frame: int) -> list[tuple[Any, ...]]:
    st = a.status
    if st == "running":
        glyph, gstyle = spinner(frame), "ansicyan"
    else:
        glyph, gstyle = GLYPHS.get(st, ("○", DIM))
    viewing = a.id == ctl.focus_id and not ctl.task_view
    base, head = _row_style(ctl, i, viewing)
    elapsed = f" {int(time.monotonic() - a.started_at)}s" if st == "running" and not a.is_main else ""
    label = st
    activity = a.activity or (a.last_report.strip().splitlines()[0] if a.last_report.strip() and st == "done" else "")
    name = a.name if a.is_main else f"{a.id} {a.name}"
    text_part = f"{name:<26} {a.kind:<6} {label}{elapsed}"
    used = get_cwidth(head) + 2 + get_cwidth(text_part)
    tail = _fit(f"  {activity}", max(0, width - used - 2)) if activity else ""
    h = ctl.row_handler("agent", a.id)
    return [
        (base, head, h), (f"{base} {gstyle}", f"{glyph} ", h), (base, _fit(text_part, width - 6), h),
        ("ansimagenta bold", " ●" if a.unseen else "", h), (base if base == "reverse" else f"{base} {DIM}", tail, h),
    ]


def _task_row(ctl: "Controller", bt: BackgroundTask, i: int, width: int, frame: int) -> list[tuple[Any, ...]]:
    glyph, gstyle = (spinner(frame), "ansimagenta") if bt.status == "running" else GLYPHS.get(bt.status, ("○", DIM))
    base, head = _row_style(ctl, i, ctl.task_view == bt.id)
    text_part = f"{bt.id} {bt.tool}: {bt.summary}"
    right = f"  {bt.status} {bt.elapsed:.0f}s"
    text_part = _fit(text_part, max(10, width - get_cwidth(head) - 2 - get_cwidth(right) - 2))
    h = ctl.row_handler("task", bt.id)
    return [(base, head, h), (f"{base} {gstyle}", f"{glyph} ", h), (base, text_part, h),
            (base if base == "reverse" else f"{base} {DIM}", right, h)]


# --------------------------------------------------------------- header / help
def header(ctl: "Controller", width: int) -> Line:
    s, a = ctl.session, ctl.focused
    left = f" voyager · session {s.id} "
    if ctl.picker is not None:
        right = " resume a conversation · Esc: cancel "
    elif ctl.task_view:
        right = f" viewing task {ctl.task_view} · Esc: back "
    elif a.is_main:
        right = f" viewing: main [{a.status}] "
    else:
        right = f" viewing: {a.id} {a.name} ({a.kind}) [{a.status}] · Esc: back to main "
    pad = max(1, width - get_cwidth(left) - get_cwidth(right))
    return [("reverse bold", left), ("reverse", " " * pad), ("reverse", right)]


def help_line(ctl: "Controller") -> Line:
    if ctl.flash and time.monotonic() < ctl.flash_until:
        return [("ansiyellow", " " + ctl.flash)]
    a = ctl.focused
    if ctl.picker is not None:
        return [(DIM, " type to filter · ↑/↓ select · Enter resume · Tab: this directory / all directories · Esc cancel")]
    if ctl.list_mode:
        return [(DIM, " ↑/↓ select · Enter view · x stop/kill · Esc cancel")]
    if ctl.task_view:
        return [(DIM, " watching a background task · x or Ctrl-C kills it · Esc back")]
    hint = " Enter send · ↑/↓ history · ↓ agents/tasks · Ctrl-C stop · Ctrl-D exit · /help"
    if not a.is_main:
        hint = f" typing here {'messages' if a.running else 'resumes'} {a.id} · Esc back to main · Ctrl-C stop · Tab next agent"
    return [(DIM, hint)]


def mode_line(ctl: "Controller") -> Line:
    """Under the chat bar: which mode this conversation is in, and what the mode says about itself."""
    mode = ctl.session.mode
    detail = mode.status(ctl.session.main)
    if mode.name == "voyager":
        name, _, rest = detail.partition(" · ")
        return [("bold ansimagenta", " ◆ voyager"), ("bold", f" · {name}"), (DIM, f" · {rest}")]
    return [("bold ansigreen", " ● chat"), (DIM, f" · {detail}")]
