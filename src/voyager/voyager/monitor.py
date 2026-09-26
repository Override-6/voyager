"""Voyager mode's part of `voyager --monitor`: where the mission stands, read straight from the workspace."""

from __future__ import annotations

import subprocess

from . import progress
from .approach import approach_status
from .evidence import load
from .state import scan_notes, scan_tools
from .workspace import Workspace

MAX_NOW = 4


def _git_log(ws: Workspace, n: int = 3) -> list[str]:
    try:
        out = subprocess.run(["git", "log", f"-{n}", "--format=%h %ar  %s"], cwd=ws.root, capture_output=True,
                             text=True, timeout=10)
        return out.stdout.strip().splitlines() if out.returncode == 0 else []
    except (OSError, subprocess.SubprocessError):
        return []


def summary(ws: Workspace) -> list[str]:
    """Lines describing the mission: phases, next actions, blockers, approach gate, research, workspace size, commits."""
    phases = ws.phases()
    done = sum(1 for mark, _ in phases if mark == "x")
    lines = [f"workspace {ws.name} ({ws.root})", f"objective: {ws.objective_line() or '(not written yet)'}",
             f"plan: {done}/{len(phases)} phases done · current: {ws.current_phase()}" if phases else "plan: phase 0 (no phases yet)"]
    now = [ln.strip() for ln in ws.section("## Now")]
    lines += [f"  now: {ln}" for ln in now[:MAX_NOW]] + ([f"  … {len(now) - MAX_NOW} more"] if len(now) > MAX_NOW else [])
    if blocked := ws.blockers():
        lines.append("BLOCKED: " + " ".join(ln.strip() for ln in blocked)[:300])
    why = approach_status(ws)
    c = load(ws).counts
    lines.append("approach: " + (f"NOT RECORDED ({why})" if why else "recorded") +
                 f" · research: {c['web_search']} searches, {c['web_fetch']} pages, {c['search_workspace']} workspace searches")
    lines.append(progress.state_line(ws)[:300])
    lines.append(f"workspace: {len(scan_tools(ws))} tools, {len(scan_notes(ws))} notes")
    lines += [f"  commit {ln}" for ln in _git_log(ws)]
    return lines
