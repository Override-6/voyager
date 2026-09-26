"""What the agent sees of its workspace: tool/note indexes, lint problems, the state block, search.

The state block is rendered into the system prompt as a *snapshot* (see VoyagerAgentMode.state): refreshed
on a user message and after a compaction, not on every request, so the request prefix stays cacheable.
Each section has a character budget; going over cuts the section and raises a lint problem, which is what
pushes the agent to prune, merge and keep its workspace small.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path

from . import progress
from .approach import gate_text
from .workspace import PLAN_HEADINGS, Workspace

TOOL_KEYS = ("summary", "usage", "example", "check", "repl", "status")  # check, repl: optional (a live-verified skill)
REQUIRED_TOOL_KEYS = ("summary", "usage", "example")
STATUSES = ("draft", "verified", "deprecated")
CONFIDENCES = ("confirmed", "likely", "hypothesis")
CODE_EXTS = {".py", ".sh", ".bash", ".js", ".mjs", ".ts", ".rb", ".pl", ".lua", ".php", ".ps1"}
COMMENT_MARKS = ("#", "//", "--", ";", "*", "/*", "<!--", "REM ", "rem ")
HEADER_RE = re.compile(r"^\s*(?:#|//|--|;|\*|\"\"\"|''')?\s*(summary|usage|example|check|repl|status)\s*:\s*(.*?)\s*$", re.I)

BUDGETS = {"objective": 2500, "plan": 7000, "tools": 4000, "knowledge": 4000}
SUB_BUDGETS = {"objective": 1200, "tools": 3000, "knowledge": 3000}
MAX_LOG_LINES = 25
MAX_NOW_ITEMS = 10
SOFT_MAX_LINES, HARD_MAX_LINES = 300, 500  # size rule for every file of the agent's codebase (tools/, lib included)

SUB_AGENT_RULES = """# Workspace
You work inside the workspace **{name}** (your working directory). Its main agent owns `OBJECTIVE.md` and `PLAN.md`: never edit them.
- Reuse what exists: run the tools listed below (`search_workspace` finds more) instead of redoing their work by hand.
- If your task needs a script that is likely to be reused, save it with `save_tool` (it tests the header's `example` and indexes it). Throwaway scripts and raw outputs go in `scratch/`.
- Write what you learn to `knowledge/<area>/<topic>.md` (frontmatter: summary, confidence, sources, updated); update the existing note on a topic rather than creating a second one.
- In your final report, list every tool and note you created or changed."""


@dataclass
class ToolInfo:
    path: str  # relative to the workspace root
    header: dict[str, str]

    @property
    def status(self) -> str:
        return self.header.get("status", "draft").lower()


@dataclass
class NoteInfo:
    path: str
    meta: dict[str, str] | None  # None: no frontmatter


# ------------------------------------------------------------------ parsing
def _header_end(lines: list[str]) -> int:
    """Index just past the file's leading comment / docstring block (shebang and blank lines included)."""
    doc: str | None = None
    for i, line in enumerate(lines[:60]):
        s = line.strip()
        if doc:
            if doc in s:
                doc = None
            continue
        if s.startswith(('"""', "'''")):
            q = s[:3]
            if s.count(q) == 1:
                doc = q
            continue
        if not s or s.startswith(COMMENT_MARKS):
            continue
        return i
    return min(len(lines), 60)


def parse_header(text: str) -> dict[str, str]:
    """`key: value` lines in the file's leading comment / docstring block (never in the code below it)."""
    out: dict[str, str] = {}
    lines = text.splitlines()
    for line in lines[: _header_end(lines)]:
        if (m := HEADER_RE.match(line)) and m.group(1).lower() not in out:
            out[m.group(1).lower()] = m.group(2).strip().strip("\"'").strip()
    return out


def set_status(text: str, status: str) -> str:
    """Rewrite the header's status line (or add one after the example line)."""
    lines = text.splitlines(keepends=True)
    end = _header_end([ln.rstrip("\n") for ln in lines])
    for i, line in enumerate(lines[:end]):
        m = HEADER_RE.match(line.rstrip("\n"))
        if m and m.group(1).lower() == "status":
            lines[i] = line[: m.start(2)] + status + line[m.end(2):]
            return "".join(lines)
    for i, line in enumerate(lines[:end]):
        m = HEADER_RE.match(line.rstrip("\n"))
        if m and m.group(1).lower() == "example":
            prefix = line[: line.lower().index("example")]
            lines.insert(i + 1, f"{prefix}status: {status}\n")
            return "".join(lines)
    return text


def parse_frontmatter(text: str) -> dict[str, str] | None:
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    meta: dict[str, str] = {}
    for line in text[3:end].splitlines():
        key, sep, val = line.partition(":")
        if sep and key.strip() and not line.startswith((" ", "\t", "-")):
            meta[key.strip().lower()] = val.strip()
    return meta


# ------------------------------------------------------------------ scanning
def _files(base: Path, exts: set[str]) -> list[Path]:
    if not base.is_dir():
        return []
    return sorted(
        p for p in base.rglob("*")
        if p.is_file() and p.suffix in exts and not any(part.startswith((".", "__")) for part in p.relative_to(base).parts)
    )


def scan_tools(ws: Workspace) -> list[ToolInfo]:
    """Every code file under tools/ except the shared library (tools/lib/)."""
    out = []
    for p in _files(ws.tools_dir, CODE_EXTS):
        if p.relative_to(ws.tools_dir).parts[0] == "lib":
            continue
        out.append(ToolInfo(str(p.relative_to(ws.root)), parse_header(p.read_text(errors="replace"))))
    return out


def scan_notes(ws: Workspace) -> list[NoteInfo]:
    return [NoteInfo(str(p.relative_to(ws.root)), parse_frontmatter(p.read_text(errors="replace")))
            for p in _files(ws.knowledge_dir, {".md"})]


# ------------------------------------------------------------------ lint
def lint(ws: Workspace, tools: list[ToolInfo], notes: list[NoteInfo]) -> list[str]:
    problems = []
    for t in tools:
        if missing := [k for k in REQUIRED_TOOL_KEYS if not t.header.get(k)]:
            problems.append(f"{t.path}: header lacks {', '.join(missing)} (save it with save_tool)")
        elif t.status not in STATUSES:
            problems.append(f"{t.path}: status {t.status!r} is not one of {'/'.join(STATUSES)}")
    for p in _files(ws.tools_dir, CODE_EXTS):
        lines = len(p.read_text(errors="replace").splitlines())
        if lines > HARD_MAX_LINES:
            problems.append(f"{p.relative_to(ws.root)}: {lines} lines, over the {HARD_MAX_LINES}-line hard limit: split it now")
        elif lines > SOFT_MAX_LINES:
            problems.append(f"{p.relative_to(ws.root)}: {lines} lines (keep under {SOFT_MAX_LINES}): split it by responsibility")
    for n in notes:
        if n.meta is None:
            problems.append(f"{n.path}: no frontmatter (--- summary / confidence / sources / updated ---)")
        elif not n.meta.get("summary"):
            problems.append(f"{n.path}: frontmatter lacks summary")
        elif n.meta.get("confidence", "").lower() not in CONFIDENCES:
            problems.append(f"{n.path}: confidence must be one of {'/'.join(CONFIDENCES)}")
    plan = ws.read("PLAN.md")
    if missing := [h for h in PLAN_HEADINGS if h.lower() not in plan.lower()]:
        problems.append(f"PLAN.md is missing section(s) {', '.join(missing)}: restore the format")
    if len(plan) > BUDGETS["plan"]:
        problems.append(f"PLAN.md is {len(plan)} chars (budget {BUDGETS['plan']}): trim the Log, collapse done phases")
    if (n := len(ws.section("## Log", plan))) > MAX_LOG_LINES:
        problems.append(f"PLAN.md Log has {n} lines (max {MAX_LOG_LINES}): drop the oldest (git keeps history)")
    if (n := len(ws.section("## Now", plan))) > MAX_NOW_ITEMS:
        problems.append(f"PLAN.md Now has {n} items (max {MAX_NOW_ITEMS}): move later work to Current phase")
    return problems


# ------------------------------------------------------------------ rendering
def _cut(text: str, budget: int, what: str) -> str:
    text = text.strip()
    return text if len(text) <= budget else text[:budget] + f"\n… [{what} cut at {budget} chars]"


def _file(ws: Workspace, rel: str, budget: int) -> str:
    """A workspace file quoted verbatim: tags keep its own headings apart from the state block's."""
    return f'<file path="{rel}">\n{_cut(ws.read(rel), budget, rel)}\n</file>'


def _index(lines: list[str], budget: int, noun: str) -> str:
    out, used = [], 0
    for i, line in enumerate(lines):
        if used + len(line) > budget:
            out.append(f"… {len(lines) - i} more {noun}: use search_workspace")
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out) or "(none yet)"


def tools_index(tools: list[ToolInfo], budget: int) -> tuple[str, str]:
    live = [t for t in tools if t.status != "deprecated"]
    lines = [f"- {t.path} — {t.header.get('summary', '(no header)')}" + (f" (skill, repl {t.header['repl']})" if t.header.get("repl") else "")
             + ("" if t.status == "verified" else f" [{t.status}]") for t in live]
    drafts = sum(1 for t in live if t.status != "verified")
    title = f"{len(live)} tools" + (f", {drafts} not verified" if drafts else "") + \
        (f", {len(tools) - len(live)} deprecated (hidden)" if len(live) < len(tools) else "")
    return title, _index(lines, budget, "tools")


def notes_index(notes: list[NoteInfo], budget: int) -> tuple[str, str]:
    lines = []
    for n in notes:
        meta = n.meta or {}
        conf = meta.get("confidence", "").lower()
        lines.append(f"- {n.path} — {meta.get('summary') or '(no summary)'}" + (f" ({conf})" if conf and conf != "confirmed" else ""))
    return f"{len(notes)} notes", _index(lines, budget, "notes")


def state_block(ws: Workspace, *, main: bool, round_no: int = 0) -> str:
    tools, notes = scan_tools(ws), scan_notes(ws)
    b = BUDGETS if main else SUB_BUDGETS
    t_title, t_index = tools_index(tools, b["tools"])
    n_title, n_index = notes_index(notes, b["knowledge"])
    if not main:
        return (f"{SUB_AGENT_RULES.format(name=ws.name)}\n\n## Objective (for context)\n"
                f"{_file(ws, 'OBJECTIVE.md', b['objective'])}\n\n"
                f"## tools/ — {t_title}\n{t_index}\n\n## knowledge/ — {n_title}\n{n_index}")
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    parts = [
        f"# Workspace state — round {round_no}, snapshot of {now}",
        "This is the workspace as it was when the snapshot was taken; edits you made since then are in the conversation.",
        f"Current phase: {ws.current_phase()}" + ("" if ws.phases() else
            " — PLAN.md has no phases yet: start with \"Phase 0: frame the objective\" from your method (first message)."),
        *([gate] if (gate := gate_text(ws)) else []),
        progress.state_line(ws),
        f"## OBJECTIVE.md\n{_file(ws, 'OBJECTIVE.md', b['objective'])}",
        f"## PLAN.md\n{_file(ws, 'PLAN.md', b['plan'])}",
        f"## tools/ — {t_title}\n{t_index}",
        f"## knowledge/ — {n_title}\n{n_index}",
    ]
    if problems := lint(ws, tools, notes):
        parts.append("## Problems to fix (at the next checkpoint at the latest)\n" + "\n".join(f"- {p}" for p in problems[:15]))
    return "\n\n".join(parts)


# ------------------------------------------------------------------ search
def search(ws: Workspace, query: str, scope: str = "all", limit: int = 10) -> list[str]:
    """Rank tools (header) and notes (frontmatter) by query-term overlap; header/path hits weigh 3x body hits."""
    terms = [t for t in re.findall(r"[a-z0-9_]+", query.lower()) if len(t) > 1]
    if not terms:
        return []
    docs: list[tuple[str, str, str, str]] = []  # (path, summary, head text, body)
    if scope in ("all", "tools"):
        for t in scan_tools(ws):
            docs.append((t.path, t.header.get("summary", ""), t.path + " " + " ".join(t.header.values()), ws.read(t.path)))
    if scope in ("all", "knowledge"):
        for n in scan_notes(ws):
            meta = n.meta or {}
            docs.append((n.path, meta.get("summary", ""), n.path + " " + " ".join(meta.values()), ws.read(n.path)))
    scored = []
    for path, summary, head, body in docs:
        head_l, body_l = head.lower(), body.lower()
        score = sum(3 * (t in head_l) + min(body_l.count(t), 5) for t in terms)
        if score:
            best = max(body.splitlines() or [""], key=lambda ln: sum(t in ln.lower() for t in terms))
            scored.append((score, path, summary, best.strip()[:140]))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [f"{p} — {s or '(no summary)'}\n    {line}" for _, p, s, line in scored[:limit]]
