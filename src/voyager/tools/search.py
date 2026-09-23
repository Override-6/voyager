"""Search tools: glob, grep (pure Python, no ripgrep dependency)."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .base import Tool, ToolContext, ToolError, clip

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache", "dist", "build"}
MAX_MATCHES = 200


def _walk(root: Path):
    if root.is_file():
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for f in sorted(filenames):
            yield Path(dirpath) / f


class Glob(Tool):
    def __init__(self) -> None:
        super().__init__(
            name="glob",
            description="Find files by glob pattern (e.g. '**/*.py'). Returns paths sorted by most recently modified.",
            properties={
                "pattern": {"type": "string", "description": "Glob pattern, e.g. 'src/**/*.ts'"},
                "path": {"type": "string", "description": "Directory to search in. Default: working directory"},
            },
            required=["pattern"],
        )

    def summary(self, args: dict[str, Any]) -> str:
        return str(args.get("pattern", ""))

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        root = ctx.resolve(args.get("path") or ".")
        if not root.is_dir():
            raise ToolError(f"{root} is not a directory")
        hits = [p for p in root.glob(args["pattern"]) if p.is_file() and not (SKIP_DIRS & set(p.parts))]
        hits.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        if not hits:
            return "No files matched."
        shown = [os.path.relpath(p, ctx.cwd) for p in hits[:MAX_MATCHES]]
        more = f"\n… {len(hits) - MAX_MATCHES} more" if len(hits) > MAX_MATCHES else ""
        return "\n".join(shown) + more


class Grep(Tool):
    def __init__(self) -> None:
        super().__init__(
            name="grep",
            description="Search file contents with a regular expression. Returns `path:line: text` matches.",
            properties={
                "pattern": {"type": "string", "description": "Python regular expression"},
                "path": {"type": "string", "description": "File or directory to search. Default: working directory"},
                "glob": {"type": "string", "description": "Only search files whose name matches this glob, e.g. '*.py'"},
                "ignore_case": {"type": "boolean", "description": "Case-insensitive match"},
            },
            required=["pattern"],
        )

    def summary(self, args: dict[str, Any]) -> str:
        return str(args.get("pattern", ""))

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        try:
            rx = re.compile(args["pattern"], re.IGNORECASE if args.get("ignore_case") else 0)
        except re.error as e:
            raise ToolError(f"invalid regex: {e}")
        root = ctx.resolve(args.get("path") or ".")
        if not root.exists():
            raise ToolError(f"{root} does not exist")
        name_glob = args.get("glob")
        out: list[str] = []
        for f in _walk(root):
            if name_glob and not f.match(name_glob):
                continue
            try:
                with f.open(encoding="utf-8") as fh:
                    for n, line in enumerate(fh, 1):
                        if rx.search(line):
                            out.append(f"{os.path.relpath(f, ctx.cwd)}:{n}: {line.rstrip()[:300]}")
                            if len(out) >= MAX_MATCHES:
                                return clip("\n".join(out)) + f"\n… stopped at {MAX_MATCHES} matches"
            except (UnicodeDecodeError, OSError):
                continue  # binary / unreadable
        return clip("\n".join(out)) if out else "No matches."
