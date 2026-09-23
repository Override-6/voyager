"""Styled word-wrapping for the transcript. A Line is a list of (style, text) fragments."""

from __future__ import annotations

import re

from prompt_toolkit.utils import get_cwidth

Frag = tuple[str, str]
Line = list[Frag]

_TOKEN = re.compile(r"\s+|\S+")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean(text: str) -> str:
    """Strip ANSI escapes / control chars (tool output) that would corrupt the screen."""
    return _CTRL.sub("", _ANSI.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))).expandtabs(4)


def text_width(line: Line) -> int:
    return sum(get_cwidth(t) for _, t in line)


def wrap_line(frags: Line, width: int, hang: str = "") -> list[Line]:
    """Wrap one logical line to `width` columns; continuation lines start with `hang`."""
    width = max(width, 8)
    hw = get_cwidth(hang)
    if hw >= width - 4:
        hang, hw = "", 0
    out: list[Line] = []
    cur: Line = []
    col = 0

    def newline() -> None:
        nonlocal cur, col
        out.append(cur)
        cur, col = ([("", hang)] if hang else []), hw

    for style, text in frags:
        for tok in _TOKEN.findall(text):
            w = get_cwidth(tok)
            if tok.isspace():
                if col + w > width:
                    newline()
                elif not (col == hw and out):  # drop leading spaces on continuation lines
                    cur.append((style, tok))
                    col += w
                continue
            if col + w > width and col > hw:
                newline()
            while col + get_cwidth(tok) > width:  # a single word wider than the line: hard split
                piece, pw = "", 0
                for ch in tok:
                    cw = get_cwidth(ch)
                    if col + pw + cw > width:
                        break
                    piece, pw = piece + ch, pw + cw
                if not piece:
                    if col == hw:  # cannot fit even one char; emit as is
                        break
                    newline()
                    continue
                cur.append((style, piece))
                tok = tok[len(piece):]
                newline()
            cur.append((style, tok))
            col += get_cwidth(tok)
    out.append(cur)
    return out


def wrap_text(text: str, style: str, width: int, *, first: Frag | None = None, hang: str = "") -> list[Line]:
    """Wrap multi-line `text`; `first` is a prefix fragment on the first line, `hang` indents the rest."""
    lines: list[Line] = []
    for i, raw in enumerate(clean(text).split("\n")):
        frags: Line = [(style, raw)]
        if i == 0 and first:
            frags.insert(0, first)
        elif hang:
            frags.insert(0, ("", hang))
        lines.extend(wrap_line(frags, width, hang))
    return lines
