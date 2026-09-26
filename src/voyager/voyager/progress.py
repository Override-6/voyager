"""Measured progress, by the harness: the mission's progress probe, run at every round end, and stall detection.

OBJECTIVE.md's `## Progress probe` is a shell command whose last output line holds a number that grows as the objective
gets closer (items obtained, tests passing, pages mapped...). "Git HEAD moved" is not progress: editing notes moves it.
The history lives in `scratch/.progress.json` (workspace-scoped: it survives sessions). When a few rounds pass with
no progress, the next round starts with a stall review (system/voyager/STALL.md) instead of more of the same.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import TYPE_CHECKING, Any

from ..tools.bash import _kill_group
from .workspace import NOT_BLOCKED_RE

if TYPE_CHECKING:
    from .workspace import Workspace

PATH = "scratch/.progress.json"
HEADING = "## Progress probe"
STALL_ROUNDS = 2  # rounds in a row without progress before a stall review
PROBE_TIMEOUT = 60
KEEP = 50
NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
CODE_RE = re.compile(r"`([^`]+)`")


def probe_command(ws: "Workspace") -> str:
    """The command in OBJECTIVE.md's probe section ("" when missing or still the template's placeholder)."""
    lines = [ln.strip() for ln in ws.section(HEADING, ws.read("OBJECTIVE.md")) if not ln.strip().startswith("```")]
    for line in lines:
        if m := CODE_RE.search(line):
            return m.group(1).strip()
        if not line.startswith("(") and not NOT_BLOCKED_RE.match(line):
            return line.lstrip("-* ").strip()
    return ""


async def run_probe(ws: "Workspace", command: str) -> tuple[float | None, str]:
    """(the number on the probe's last output line or None, that line or the failure)."""
    proc = await asyncio.create_subprocess_shell(
        command, cwd=ws.root, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, start_new_session=True)
    try:
        async with asyncio.timeout(PROBE_TIMEOUT):
            out, _ = await proc.communicate()
    except TimeoutError:
        return None, f"timed out after {PROBE_TIMEOUT}s"
    finally:
        _kill_group(proc)
        await proc.wait()
    last = ([ln.strip() for ln in out.decode(errors="replace").splitlines() if ln.strip()] or [""])[-1][:200]
    if proc.returncode != 0:
        return None, f"exit code {proc.returncode}: {last}"
    m = NUMBER_RE.search(last)
    return (float(m.group()) if m else None), (last if m else f"no number in the last line: {last!r}")


def history(ws: "Workspace") -> list[dict[str, Any]]:
    try:
        data = json.loads((ws.root / PATH).read_text())
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save(ws: "Workspace", rows: list[dict[str, Any]]) -> None:
    try:
        p = ws.root / PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(rows[-KEEP:]))
    except OSError:
        pass


def _first_now(ws: "Workspace") -> str:
    return next((ln.strip() for ln in ws.section("## Now") if "[x]" not in ln.lower()), "")


def _progressed(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """From round a to round b: the probe went up, or (no measurement) the first open Now item changed."""
    if a.get("value") is not None and b.get("value") is not None:
        return b["value"] > a["value"]
    return a.get("now") != b.get("now")


def stalled(rows: list[dict[str, Any]]) -> bool:
    """No progress over the last STALL_ROUNDS rounds, and no stall review sent during them."""
    recent = rows[-(STALL_ROUNDS + 1):]
    return (len(recent) == STALL_ROUNDS + 1 and not any(_progressed(a, b) for a, b in zip(recent, recent[1:]))
            and not any(r.get("review") for r in recent[1:-1]))


async def measure(ws: "Workspace", round_no: int) -> dict[str, Any]:
    """Run the probe at a round's end and record it. The row says whether this round calls for a stall review."""
    command = probe_command(ws)
    value, line = await run_probe(ws, command) if command else (None, "")
    row = {"round": round_no, "ts": round(time.time()), "value": value, "line": line, "now": _first_now(ws)}
    rows = history(ws) + [row]
    row["review"] = stalled(rows)
    _save(ws, rows)
    return row


def state_line(ws: "Workspace") -> str:
    """One line for the state block: the probe and its recent values."""
    command, rows = probe_command(ws), history(ws)[-8:]
    if not command:
        return (f"Progress probe: NOT DEFINED. Add a `{HEADING}` section to OBJECTIVE.md: one shell command, run from the "
                "workspace root, whose last output line is a number that grows as the objective gets closer.")
    values = " · ".join(f"r{r['round']} {fmt(r['value'])}" for r in rows) or "not measured yet (runs at each round end)"
    broken = f" (last run: {rows[-1]['line']})" if rows and rows[-1]["value"] is None else ""
    return f"Progress probe `{command}`: {values}{broken}"


def trend(rows: list[dict[str, Any]]) -> str:
    return " → ".join(fmt(r.get("value")) for r in rows[-(STALL_ROUNDS + 1):])


def fmt(v: float | None) -> str:
    return "?" if v is None else (str(int(v)) if float(v).is_integer() else f"{v:g}")
