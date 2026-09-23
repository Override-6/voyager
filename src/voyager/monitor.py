"""`voyager --monitor [SESSION_ID] [-f]`: watch a session from outside, live.

Reads what events.py writes (`events.jsonl` + `run.json` in the session folder), so it works on a session launched by
`-p` (e.g. in the background), on one running in a TUI, or on a finished one. Prints a header (state, mode, round,
context, and in voyager mode the mission's plan, approach gate and latest commits) and then the events: user messages,
assistant text, tool calls with duration and result, checkpoints, compactions, rounds, turn ends.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, TextIO

RESULT_LINE = 110
POLL_SECS = 0.5


# ------------------------------------------------------------------ locating
def find_session(sessions_dir: Path, ref: str | None) -> Path | None:
    """A session folder with events: by id (a unique prefix is enough), or the most recently active one."""
    if ref:
        found = [d for d in sessions_dir.glob(f"{ref}*/") if (d / "events.jsonl").is_file()]
        return found[0] if len(found) == 1 else None
    withs = [d for d in sessions_dir.glob("*/") if (d / "events.jsonl").is_file()]
    return max(withs, key=lambda d: (d / "events.jsonl").stat().st_mtime, default=None)


def read_run(d: Path) -> dict[str, Any]:
    try:
        return json.loads((d / "run.json").read_text())
    except (OSError, ValueError):
        return {}


def read_events(path: Path, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    """Complete lines after `offset`; a half-written last line is left for the next read."""
    try:
        with path.open("rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return [], offset
    complete = data[: data.rfind(b"\n") + 1]
    rows = []
    for raw in complete.splitlines():
        try:
            rows.append(json.loads(raw))
        except ValueError:
            continue
    return rows, offset + len(complete)


def alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ----------------------------------------------------------------- rendering
def _first_line(text: str) -> str:
    lines = [ln.strip() for ln in str(text).splitlines() if ln.strip()]
    line = lines[0] if lines else "(empty)"
    return line if len(line) <= RESULT_LINE else line[:RESULT_LINE] + "…"


def _k(n: Any) -> str:
    return f"{n / 1000:.0f}k" if isinstance(n, (int, float)) else "?"


def render(ev: dict[str, Any], *, show_start: bool = False, show_all: bool = False) -> str | None:
    """One line for an event, or None when it is too noisy for the default view."""
    e, who = ev.get("ev"), ev.get("agent", "main")
    stamp = time.strftime("%H:%M:%S", time.localtime(ev.get("ts", 0)))
    rnd = f" r{ev['round']}" if "round" in ev else ""
    head = f"{stamp}{rnd} " + ("" if who == "main" else f"[{who}] ")
    tool = f"{ev.get('name')}({ev.get('summary', '')})"
    body = {
        "start": f"▶ session start · {ev.get('mode')}" + (f" · workspace {ev['workspace']}" if ev.get("workspace") else ""),
        "user": f"❯ {ev.get('text', '')}" + (f"  (from {ev['src']})" if ev.get("src") not in (None, "user") else ""),
        "text": f"⏺ {_first_line(ev.get('text', ''))}",
        "tool_end": f"  {'✓' if ev.get('ok') else '✗'} {tool}" + (f" {ev['secs']}s" if ev.get("secs") is not None else "")
                    + f" ⎿ {_first_line(ev.get('result', ''))}",
        "tool_bg": f"  ⇢ {tool} → background",
        "compact_start": "⟳ compacting…",
        "compaction": f"⟳ compaction {_k(ev.get('before'))} → {_k(ev.get('after'))} tokens",
        "round": f"━━ {ev.get('text', '◆ new round')} ━━",
        "checkpoint": "⚑ checkpoint requested: saving state to the workspace",
        "notice": f"· {ev.get('text', '')}",
        "error": f"✗ ERROR {ev.get('text', '')}",
        "turn_end": f"── turn end ({ev.get('outcome') or 'ok'}) · {ev.get('tools', 0)} tool calls so far",
        "end": f"■ session end ({ev.get('outcome') or 'ok'})",
    }.get(e or "")
    if body is None and show_start and e == "tool_start":
        body = f"  ⏺ {tool} …"
    if body is None and show_all:
        body = {"usage": f"↳ ctx {_k(ev.get('ctx'))}/{_k(ev.get('window'))}", "thinking": f"✻ thinking ({ev.get('chars')} chars)",
                "tool_start": f"  ⏺ {tool} …", "waiting": f"  … waiting for the model (~{(ev.get('tokens') or 0) // 1000}k tokens of context)",
                "gen_start": f"  … model starts a {ev.get('name') or ev.get('kind')}",
                "gen": f"  … model still writing a {ev.get('kind')} ({ev.get('chars')} chars)"}.get(e or "")
    return head + body if body else None


def state_line(run: dict[str, Any], events: list[dict[str, Any]], now: float) -> str:
    last = events[-1] if events else {}
    age = now - last.get("ts", now)
    if last.get("ev") == "end":
        return f"FINISHED ({last.get('outcome') or 'ok'})"
    if not alive(run.get("pid")):
        return f"DEAD: process {run.get('pid')} is gone and no end event was written (killed or crashed), last event {age:.0f}s ago"
    if last.get("ev") == "turn_end" and last.get("agent") == "main":
        return f"IDLE, waiting for a message ({age:.0f}s)"
    pending = [e for e in events if e.get("ev") == "tool_start" and not any(
        x.get("ev") == "tool_end" and x.get("id") == e.get("id") and x.get("agent") == e.get("agent") for x in events)]
    doing = f" · in flight: {pending[-1].get('name')}({pending[-1].get('summary', '')})" if pending else ""
    if last.get("ev") == "waiting":  # the request is out and the model is still reading its context
        doing = f" · waiting for the model to start answering (~{last.get('tokens', 0) // 1000}k tokens of context to read)"
    if last.get("ev") in ("gen_start", "gen"):  # the model is mid-block: not a hang, just a long generation
        what = last.get("name") or last.get("kind")
        doing = f" · model is generating {what}" + (f" ({last['chars']} chars so far)" if "chars" in last else "")
    return f"RUNNING, last event {age:.0f}s ago{doing}"


def header(d: Path, run: dict[str, Any], events: list[dict[str, Any]]) -> list[str]:
    n = lambda ev: sum(1 for e in events if e.get("ev") == ev)  # noqa: E731
    usage = next((e for e in reversed(events) if e.get("ev") == "usage" and e.get("ctx")), None)
    ctx = f" · context {_k(usage['ctx'])}/{_k(usage['window'])} ({usage['ctx'] * 100 // max(1, usage['window'])}%)" if usage else ""
    rnd = next((e["round"] for e in reversed(events) if "round" in e), None)
    lines = [f"session {d.name} · mode {run.get('mode', '?')}" + (f" · round {rnd}" if rnd is not None else "") + ctx,
             f"state: {state_line(run, events, time.time())}",
             f"totals: {n('tool_end')} tool calls ({sum(1 for e in events if e.get('ev') == 'tool_end' and not e.get('ok'))} failed)"
             f" · {n('compaction')} compactions · {n('checkpoint')} checkpoints · {n('error')} errors · {n('user')} user messages",
             f"cwd: {run.get('cwd', '?')}"]
    if run.get("mode") == "voyager" and run.get("workspace"):
        from .voyager.monitor import summary  # voyager mode's part; imported here like mode.mode_for does
        from .voyager.workspace import Workspace

        ws = Workspace.get(Path(run["workspaces_dir"]), run["workspace"])
        lines += summary(ws) if ws else []
    return lines


# ------------------------------------------------------------------- driver
def run_monitor(sessions_dir: Path, ref: str | None, *, follow: bool = False, as_json: bool = False, tail: int = 40,
                show_all: bool = False, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    d = find_session(sessions_dir, ref)
    if d is None:
        print(f"error: no session with events{' matching ' + ref if ref else ''} in {sessions_dir} "
              "(events are written by sessions started after this feature existed)", file=sys.stderr)
        return 2
    run = read_run(d)
    path = d / "events.jsonl"
    events, offset = read_events(path)
    if not as_json:
        out.write("\n".join(header(d, run, events)) + "\n" + "─" * 60 + "\n")
    shown = events[-tail:] if tail > 0 else events
    _print(shown, out, as_json, show_start=follow, show_all=show_all)
    out.flush()
    if not follow:
        return 0
    quiet = 0
    try:
        while True:
            time.sleep(POLL_SECS)
            new, offset = read_events(path, offset)
            events += new
            _print(new, out, as_json, show_start=True, show_all=show_all)
            out.flush()
            if any(e.get("ev") == "end" for e in new):
                return 0
            quiet = 0 if new else quiet + 1
            if quiet >= 4 and not alive(run.get("pid")):  # 2 s of silence and nobody home: it died without an end event
                out.write("(process gone: no more events)\n")
                return 0
    except KeyboardInterrupt:
        return 0


def _print(events: list[dict[str, Any]], out: TextIO, as_json: bool, *, show_start: bool, show_all: bool) -> None:
    for ev in events:
        if as_json:
            out.write(json.dumps(ev, ensure_ascii=False) + "\n")
        elif line := render(ev, show_start=show_start, show_all=show_all):
            out.write(line + "\n")
