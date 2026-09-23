"""Voyager mode's tools (only offered inside a workspace): save_tool, search_workspace.

save_tool is how a script becomes part of the agent's codebase: the harness checks the header, runs the
header's `example`, marks the tool verified or draft, and commits. The model never has to remember to test,
index or commit.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..tools.base import Tool, ToolContext, ToolError, clip
from ..tools.bash import _kill_group
from .state import REQUIRED_TOOL_KEYS, parse_header, search, set_status

EXAMPLE_TIMEOUT = 120
EXAMPLE_OUTPUT_CLIP = 3000

HEADER_HELP = '''Start the file with a header block (docstring or comments), e.g.:
"""
summary: <one line: what it does>
usage: uv run tools/<area>/<name>.py <args> [--options]
example: uv run tools/<area>/<name>.py <a real, quick argument set>
"""
Python dependencies go in a PEP 723 block (# /// script ... # ///) so `uv run` installs them.'''


def _workspace(ctx: ToolContext) -> Any:
    ws = getattr(ctx.session, "workspace", None)
    if ws is None:
        raise ToolError("no workspace is active")
    return ws


class SaveTool(Tool):
    def __init__(self) -> None:
        super().__init__(
            name="save_tool",
            description=(
                "Save a reusable script into the workspace codebase (tools/<area>/<name>.<ext>), creating or replacing "
                "it. The header's `example` command is run from the workspace root: exit code 0 marks the tool "
                "verified and commits it; otherwise it is kept as a draft and the output is returned so you can fix it "
                "and save again. Use it for scripts worth reusing; throwaway scripts go in scratch/ with write_file.\n"
                + HEADER_HELP
            ),
            properties={
                "path": {"type": "string", "description": "Path under tools/, e.g. tools/web/crawl_sitemap.py"},
                "content": {"type": "string", "description": "Full file content, header first"},
            },
            required=["path", "content"],
        )

    def summary(self, args: dict[str, Any]) -> str:
        return str(args.get("path", ""))

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        ws = _workspace(ctx)
        path = ctx.resolve(str(args["path"]))
        try:
            rel = path.relative_to(ws.tools_dir.resolve())
        except ValueError:
            raise ToolError(f"save_tool only writes under {ws.tools_dir}/ (got {path})")
        if rel.parts[0] == "lib":
            raise ToolError("tools/lib/ holds shared modules without an example: write them with write_file")
        content = str(args["content"])
        header = parse_header(content)
        if missing := [k for k in REQUIRED_TOOL_KEYS if not header.get(k)]:
            raise ToolError(f"header lacks {', '.join(missing)}.\n{HEADER_HELP}")

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(set_status(content, "draft"))  # kept even if the example hangs or fails
        if content.startswith("#!"):
            path.chmod(path.stat().st_mode | 0o111)
        code, output = await _run_example(header["example"], ws.root, ctx)
        shown = clip(output.strip() or "(no output)", EXAMPLE_OUTPUT_CLIP)
        name = path.relative_to(ws.root.resolve())
        if code != 0:
            why = "timed out" if code is None else f"exit code {code}"
            raise ToolError(f"{name} saved as DRAFT: its example failed ({why}).\n$ {header['example']}\n{shown}\n"
                            "Fix the tool (or the example) and call save_tool again.")
        path.write_text(set_status(content, "verified"))
        committed = await ws.commit(f"tool: {name} — {header['summary']}")
        return f"{name} saved and VERIFIED{' (committed)' if committed else ''}.\n$ {header['example']}\n{shown}"


async def _run_example(command: str, cwd: Any, ctx: ToolContext) -> tuple[int | None, str]:
    proc = await asyncio.create_subprocess_shell(
        command, cwd=cwd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, start_new_session=True, limit=2**20,
    )
    lines: list[str] = []

    async def pump() -> None:
        assert proc.stdout is not None
        while raw := await proc.stdout.readline():
            lines.append(raw.decode(errors="replace"))
            ctx.emit(lines[-1].rstrip("\n"))

    timed_out = False
    try:
        try:
            async with asyncio.timeout(EXAMPLE_TIMEOUT):
                await pump()
                await proc.wait()
        except TimeoutError:
            timed_out = True
    finally:
        _kill_group(proc)
        await proc.wait()
    return (None if timed_out else proc.returncode), "".join(lines)


class SearchWorkspace(Tool):
    def __init__(self) -> None:
        super().__init__(
            name="search_workspace",
            description=(
                "Search your workspace's tools (headers) and knowledge notes by keywords; returns the best matches "
                "as path — summary + the best matching line. Use it before doing something by hand (a tool may "
                "already do it) and before writing a note (one may already cover the topic)."
            ),
            properties={
                "query": {"type": "string", "description": "Keywords, e.g. 'sitemap crawl rate limit'"},
                "scope": {"type": "string", "enum": ["all", "tools", "knowledge"], "description": "Default: all"},
            },
            required=["query"],
        )

    def summary(self, args: dict[str, Any]) -> str:
        return str(args.get("query", ""))[:80]

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        hits = search(_workspace(ctx), str(args["query"]), str(args.get("scope") or "all"))
        return "\n".join(hits) if hits else "No match. (Nothing in tools/ or knowledge/ mentions these words.)"


WORKSPACE_TOOLS: list[Tool] = [SaveTool(), SearchWorkspace()]
