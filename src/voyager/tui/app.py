"""Full-screen layout + the async entry point of the TUI (runs on the same asyncio loop as the agents)."""

from __future__ import annotations

import math
import sys
from typing import Any

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.utils import get_cwidth

from . import panes
from .controller import Controller
from .keys import build_bindings

MAX_INPUT_ROWS = 6


def _size() -> Size:
    return get_app().output.get_size()


def _prompt(ctl: Controller) -> list[tuple[str, str]]:
    a = ctl.focused
    if ctl.picker is not None:
        return [("bold ansiyellow", "filter ❯ ")]
    if ctl.task_view:
        return [("bold ansimagenta", "⧗ ")]
    if a.is_main:
        return [("bold ansicyan", "❯ ")]
    return [("bold ansimagenta", f"[{a.id}] "), ("bold ansicyan", "❯ ")]


def _prompt_width(ctl: Controller) -> int:
    return sum(get_cwidth(t) for _, t in _prompt(ctl))


def _input_height(ctl: Controller, cols: int) -> int:
    pw = _prompt_width(ctl)
    rows = sum(max(1, math.ceil((get_cwidth(ln) + pw) / max(1, cols))) for ln in ctl.buffer.text.split("\n"))
    return max(1, min(rows, MAX_INPUT_ROWS))


def _heights(ctl: Controller) -> tuple[int, int, int]:
    """(transcript, input, list) heights; the transcript takes what is left of the screen."""
    size = _size()
    ih, lh = _input_height(ctl, size.columns), panes.list_height(ctl)
    return max(3, size.rows - (1 + 1 + ih + 1 + 1 + lh + 1)), ih, lh  # header, 2 separators, mode line, help line


def build_layout(ctl: Controller) -> Layout:
    def header() -> FormattedText:
        return FormattedText(panes.header(ctl, _size().columns))

    def transcript() -> FormattedText:
        cols = _size().columns
        rows = panes.view(ctl, cols - 1, _heights(ctl)[0])
        agent = None if ctl.task_view else ctl.focused
        out: list[tuple] = []
        for i, (line, item) in enumerate(rows):
            h = ctl.handler(agent, item)
            # fragments may already carry their own mouse handler (picker rows); others get the transcript one
            out.extend(f if len(f) == 3 else (f[0], f[1], h) for f in line) if line else out.append(("", " ", h))
            if i < len(rows) - 1:
                out.append(("", "\n"))
        return FormattedText(out)

    def agent_list() -> FormattedText:
        rows = panes.list_rows(ctl, _size().columns)
        out: list[tuple] = []
        for i, row in enumerate(rows):
            out.extend(row)
            if i < len(rows) - 1:
                out.append(("", "\n"))
        return FormattedText(out)

    def line_prefix(lineno: int, wrap_count: int) -> list[tuple[str, str]]:
        return _prompt(ctl) if lineno == 0 and wrap_count == 0 else [("", " " * _prompt_width(ctl))]

    sep = lambda: Window(height=1, char="─", style="ansibrightblack")  # noqa: E731
    input_win = Window(
        BufferControl(ctl.buffer), height=lambda: Dimension.exact(_heights(ctl)[1]),
        wrap_lines=True, get_line_prefix=line_prefix,
    )
    root = HSplit([
        Window(FormattedTextControl(header), height=1),
        Window(FormattedTextControl(transcript), height=lambda: Dimension.exact(_heights(ctl)[0])),
        sep(),
        input_win,
        sep(),
        Window(FormattedTextControl(lambda: FormattedText(panes.mode_line(ctl))), height=1),
        Window(FormattedTextControl(agent_list), height=lambda: Dimension.exact(_heights(ctl)[2])),
        Window(FormattedTextControl(lambda: FormattedText(panes.help_line(ctl))), height=1),
    ])
    return Layout(root, focused_element=input_win)


async def run_tui(session: Any, first_prompt: str | None = None, pick: bool = False, backend: Any = None) -> int:
    """Run the TUI on `session` (a Session in this process, or a daemon's RemoteSession: see daemon/client.py)."""
    ctl = Controller(session, backend)
    app: Application[int] = Application(
        layout=build_layout(ctl), key_bindings=build_bindings(ctl),
        full_screen=True, mouse_support=True, refresh_interval=0.1,
    )
    app.ttimeoutlen = 0.05  # a bare Esc must react immediately
    ctl.app = app
    if pick:
        ctl.open_picker()
    if first_prompt:
        session.main.submit(first_prompt)
    else:
        session.resume_work()  # --local --resume / -c: carry on where it stopped
    left = "stopped"
    try:
        await app.run_async()
    finally:  # the session may have been swapped by /resume; a crash of the TUI must not kill a daemon session's work
        left = await ctl.session.leave(ctl.keep)
    if left == "detached":
        print(f"session {ctl.session.id} keeps running in the background · attach: voyager --attach {ctl.session.id}"
              f" (--cli for text only) · stop: voyager --stop {ctl.session.id}", file=sys.stderr)
    elif ctl.session.path.is_file():
        print(f"session saved · resume with: voyager --resume {ctl.session.id}", file=sys.stderr)
    return 0
