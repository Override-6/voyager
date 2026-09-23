"""Workspaces: a long-lived folder per objective, owned and maintained by the agent.

    ~/.voyager/workspaces/<name>/
      OBJECTIVE.md   goal, definition of done, constraints, inputs
      PLAN.md        the three horizons (phases / current phase / now), kept by the agent
      tools/         the agent's codebase (tested, indexed scripts; shared code in tools/lib/)
      knowledge/     the agent's notes (one topic per file, with frontmatter)
      scratch/       throwaway scripts and raw outputs (gitignored, not indexed)

The conversation is compacted over time; the workspace is the durable memory. It is a git repository:
the harness commits it at checkpoints (end of a main turn, after a compaction, when a tool is saved).
A session is "in" a workspace when its cwd is one: nothing else is stored (see `Workspace.at`).
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,47}$")
PLAN_HEADINGS = ("## Phases", "## Current phase", "## Now", "## Blocked", "## Log")
PHASE_RE = re.compile(r"^\s*[-*]\s*\[([ x>])\]\s*(.+)$", re.IGNORECASE)
GIT_ID = ["-c", "user.name=voyager", "-c", "user.email=voyager@localhost", "-c", "commit.gpgsign=false"]

OBJECTIVE_TEMPLATE = """# Objective
{objective}

## Definition of done
(to be written in phase 0: checkable criteria that say the objective is reached)

## Constraints
(scope, limits, what must not be done)

## Inputs
(paths, URLs, accounts, anything the work starts from)
"""

PLAN_TEMPLATE = """# Plan

## Phases

## Current phase

## Now

## Blocked

## Log
"""

GITIGNORE = "scratch/\n__pycache__/\n*.pyc\n.venv/\nnode_modules/\n"


@dataclass
class Workspace:
    root: Path

    # ------------------------------------------------------------- lookup
    @classmethod
    def create(cls, workspaces_dir: Path, name: str, objective: str = "") -> "Workspace":
        if not NAME_RE.match(name):
            raise ValueError(f"bad workspace name {name!r}: use lowercase letters, digits, '.', '_' or '-'")
        root = workspaces_dir / name
        if root.exists():
            raise ValueError(f"workspace {name!r} already exists ({root})")
        for sub in ("tools/lib", "knowledge", "scratch"):
            (root / sub).mkdir(parents=True)
        text = objective.strip() or "(not written yet: the user states it in the first message)"
        (root / "OBJECTIVE.md").write_text(OBJECTIVE_TEMPLATE.format(objective=text))
        (root / "PLAN.md").write_text(PLAN_TEMPLATE)
        (root / ".gitignore").write_text(GITIGNORE)
        ws = cls(root)
        ws._git("init", "-q")
        ws._git("add", "-A")
        ws._git(*GIT_ID, "commit", "-q", "-m", "workspace created")
        return ws

    @classmethod
    def get(cls, workspaces_dir: Path, name: str) -> "Workspace | None":
        root = workspaces_dir / name
        return cls(root) if (root / "OBJECTIVE.md").is_file() else None

    @classmethod
    def at(cls, cwd: Path, workspaces_dir: Path) -> "Workspace | None":
        """The workspace `cwd` is in (its root or any folder below it), or None."""
        try:
            rel = Path(cwd).resolve().relative_to(Path(workspaces_dir).resolve())
        except ValueError:
            return None
        return cls.get(workspaces_dir, rel.parts[0]) if rel.parts else None

    @staticmethod
    def list_all(workspaces_dir: Path) -> list["Workspace"]:
        if not workspaces_dir.is_dir():
            return []
        found = [Workspace(d) for d in workspaces_dir.iterdir() if (d / "OBJECTIVE.md").is_file()]
        return sorted(found, key=lambda w: w.updated, reverse=True)

    # ------------------------------------------------------------- content
    @property
    def name(self) -> str:
        return self.root.name

    @property
    def tools_dir(self) -> Path:
        return self.root / "tools"

    @property
    def knowledge_dir(self) -> Path:
        return self.root / "knowledge"

    @property
    def updated(self) -> float:
        try:
            return max(p.stat().st_mtime for p in (self.root / "PLAN.md", self.root / "OBJECTIVE.md") if p.exists())
        except ValueError:
            return 0.0

    def read(self, rel: str) -> str:
        try:
            return (self.root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def objective_line(self) -> str:
        """First line of the objective text (under the '# Objective' heading)."""
        for line in self.read("OBJECTIVE.md").splitlines():
            if line.strip() and not line.startswith("#"):
                return line.strip()[:100]
        return ""

    def phases(self) -> list[tuple[str, str]]:
        """(mark, text) of each phase line in PLAN.md's '## Phases' section; mark is ' ', 'x' or '>'."""
        out, inside = [], False
        for line in self.read("PLAN.md").splitlines():
            if line.startswith("## "):
                inside = line.strip().lower() == "## phases"
            elif inside and (m := PHASE_RE.match(line)):
                out.append((m.group(1).lower(), m.group(2).strip()))
        return out

    def section(self, heading: str, text: str | None = None) -> list[str]:
        """Non-blank lines of a '## heading' section of PLAN.md (or of `text`)."""
        out, inside = [], False
        for line in (self.read("PLAN.md") if text is None else text).splitlines():
            if line.startswith("## "):
                inside = line.strip().lower() == heading.lower()
            elif inside and line.strip():
                out.append(line)
        return out

    def unfinished(self) -> bool:
        """PLAN.md has phases and at least one is not done."""
        phases = self.phases()
        return bool(phases) and any(m != "x" for m, _ in phases)

    def current_phase(self) -> str:
        phases = self.phases()
        cur = next((t for m, t in phases if m == ">"), None) or next((t for m, t in phases if m == " "), None)
        if cur is None:
            return "all phases done" if phases else "phase 0 (frame)"
        return cur.split(" — ")[0].split(" - exit")[0][:80]

    # ------------------------------------------------------------- git
    def _git(self, *args: str) -> bool:
        try:
            return subprocess.run(["git", *args], cwd=self.root, capture_output=True, timeout=30).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    async def head(self) -> str:
        """Current commit id ("" if none): a turn made progress when it moved."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "rev-parse", "HEAD", cwd=self.root, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
            )
            out, _ = await proc.communicate()
            return out.decode().strip() if proc.returncode == 0 else ""
        except OSError:
            return ""

    async def commit(self, message: str) -> bool:
        """`git add -A && git commit` if anything changed. Never raises: a failed commit must not stop the agent."""
        async def git(*args: str) -> tuple[int, str]:
            proc = await asyncio.create_subprocess_exec(
                "git", *args, cwd=self.root, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
            out, _ = await proc.communicate()
            return proc.returncode or 0, out.decode(errors="replace")

        try:
            if not (self.root / ".git").exists():
                await git("init", "-q")
            await git("add", "-A")
            code, _ = await git("diff", "--cached", "--quiet")
            if code == 0:
                return False  # nothing to commit
            code, _ = await git(*GIT_ID, "commit", "-q", "-m", message[:200])
            return code == 0
        except OSError:
            return False
