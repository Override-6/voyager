"""Voyager mode's tools (only offered inside a workspace): save_tool, search_workspace.

save_tool is how a script becomes part of the agent's codebase: the harness checks the header, runs the
header's `example` (and `check`), marks the tool verified or draft, and commits. The model never has to remember to test,
index or commit. A *skill* (`repl:` in the header) is verified live: loaded into that running REPL, its example run
there, and its `check` expression must be true afterwards (the Voyager skill library).
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from ..tools.base import Tool, ToolContext, ToolError, clip
from ..tools.bash import _kill_group
from ..tools.lint import lint_note
from .state import REQUIRED_TOOL_KEYS, parse_header, search, set_status

EXAMPLE_TIMEOUT = 120
EXAMPLE_OUTPUT_CLIP = 3000

HEADER_HELP = '''Start the file with a header block (docstring or comments), e.g.:
"""
summary: <one line: what it does>
usage: uv run tools/<area>/<name>.py <args> [--options]
example: uv run tools/<area>/<name>.py <a real, quick argument set>
check: <optional: a shell command that exits 0 only if the example had its effect>
"""
Python dependencies go in a PEP 723 block (# /// script ... # ///) so `uv run` installs them.
A skill (functions used inside a live repl) adds `repl: <repl name>`: the file is loaded into that running REPL, `example`
is code run there (e.g. `add_user(db, "test")`), and `check` is an expression that must be true afterwards
(e.g. `count_users(db, "test") == 1`).'''
TRUE = {"true", "1"}


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
                "it. The header's `example` (then `check`, if any) is run from the workspace root: success marks the tool "
                "verified and commits it; otherwise it is kept as a draft and the output is returned so you can fix it "
                "and save again. Use it for scripts and skills worth reusing; throwaway scripts go in scratch/.\n"
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

        if header.get("repl") and not header.get("check"):
            raise ToolError("a skill (repl: in the header) needs a `check:` expression that is true only if the example "
                            "had its effect.\n" + HEADER_HELP)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(set_status(content, "draft"))  # kept even if the example hangs or fails
        if content.startswith("#!"):
            path.chmod(path.stat().st_mode | 0o111)
        name = path.relative_to(ws.root.resolve())
        lint = await lint_note(path, ws.root.resolve())
        run = _in_repl(ctx, header["repl"], str(name), content) if header.get("repl") else _in_shell(ws.root, ctx)
        report = []
        for step in ("load", "example", "check"):
            failed, shown = await run(step, header.get(step, ""))
            report.append(shown)
            if failed:
                raise ToolError(f"{name} saved as DRAFT: {failed}.\n" + "\n".join(report) +
                                "\nFix the tool (or its example / check) and call save_tool again." + lint)
        path.write_text(set_status(content, "verified"))
        committed = await ws.commit(f"tool: {name} — {header['summary']}")
        return f"{name} saved and VERIFIED{' (committed)' if committed else ''}.\n" + "\n".join(r for r in report if r) + lint


Step = Callable[[str, str], Awaitable[tuple[str, str]]]  # (stage, code) -> (why it failed or "", what to show)


def _in_shell(root: Any, ctx: ToolContext) -> Step:
    """A command-line tool: example and check are shell commands, run from the workspace root; exit 0 passes."""
    async def step(stage: str, cmd: str) -> tuple[str, str]:
        if stage == "load" or not cmd:
            return "", ""
        code, output = await _run_example(cmd, root, ctx)
        shown = f"$ {cmd}\n" + clip(output.strip() or "(no output)", EXAMPLE_OUTPUT_CLIP)
        if code != 0:
            return f"its {stage} failed ({'timed out' if code is None else f'exit code {code}'})", shown
        return "", shown
    return step


def _in_repl(ctx: ToolContext, name: str, rel: str, content: str) -> Step:
    """A skill: loaded into the live REPL `name`, its example run there, then its check must print true."""
    r = ctx.session.repls.repls.get(name)

    async def step(stage: str, code: str) -> tuple[str, str]:
        if r is None or not r.alive:
            return (f"it runs in the REPL '{name}', which is not running: start it with the repl tool, set up what the "
                    "example needs (connection, variables), then save again"), ""
        status, output = await r.execute(content if stage == "load" else code, EXAMPLE_TIMEOUT,
                                         file=rel if stage == "load" else "")
        shown = f">>> {stage}: {rel if stage == 'load' else code}\n" + clip(output.strip() or "(no output)", EXAMPLE_OUTPUT_CLIP)
        if status != "ok":
            return f"its {stage} failed ({status})", shown
        if stage == "check" and (output.strip().splitlines() or [""])[-1].strip().lower() not in TRUE:
            return "its check is not true", shown
        return "", "" if stage == "load" and not output.strip() else shown
    return step


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
