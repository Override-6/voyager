"""repl tool: named, persistent interpreter processes (python, node, shell) that keep their state between calls.

A long-lived connection (a database session, a network client, a debugger) costs one tool call per experiment instead of a
script, a fresh login and a disconnect each time. The processes belong to the session (`ReplManager`), outlive a
stopped turn, and die with the session. Drivers in repl_drivers/ run each chunk and print a done-marker after it.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

from .base import Tool, ToolContext, ToolError, clip
from .bash import _kill_group

MARK = "\x00REPL-DONE"
DRIVERS = Path(__file__).parent / "repl_drivers"
LANGS = {
    "python": lambda exe: [exe or sys.executable, "-u", str(DRIVERS / "python_driver.py")],
    "node": lambda exe: [exe or "node", str(DRIVERS / "node_driver.js")],
    "shell": lambda exe: [exe or "bash", "--norc", "--noprofile"],
}
DEFAULT_TIMEOUT = 20
MAX_TIMEOUT = 600
IDLE_KEEP = 200  # lines of output printed between calls (event handlers, timers) kept for the next call


class Repl:
    def __init__(self, name: str, lang: str, proc: asyncio.subprocess.Process) -> None:
        self.name, self.lang, self.proc = name, lang, proc
        self.ids = itertools.count(1)
        self.chunk: str | None = None  # id of the chunk running now
        self.lines: list[str] = []  # its output so far
        self.shown = 0  # lines of it already returned by a call that timed out
        self.idle: list[str] = []  # output that arrived while no chunk was running
        self.status = ""  # "ok" / "error" once the chunk is done
        self.done = asyncio.Event()
        self.emit: Callable[[str], None] = lambda _line: None
        self.reader = asyncio.get_running_loop().create_task(self._read())

    @property
    def alive(self) -> bool:
        return self.proc.returncode is None

    async def _read(self) -> None:
        assert self.proc.stdout is not None
        while raw := await self.proc.stdout.readline():
            line = raw.decode(errors="replace")
            before, found, rest = line.partition(MARK)
            if before.strip() or not found:
                self._take(before if found else line)
            if found and self.chunk is not None and rest.split()[:1] == [self.chunk]:
                self.status = (rest.split() + ["error"])[1]
                self.chunk = None
                self.done.set()
        await self.proc.wait()
        self.done.set()  # exited: nothing more will come

    def _take(self, line: str) -> None:
        if self.chunk is None:
            self.idle = (self.idle + [line])[-IDLE_KEEP:]
        else:
            self.lines.append(line)
            self.emit(line.rstrip("\n"))

    async def send(self, code: str, file: str = "") -> None:
        """Start a chunk. `file`: the code is that file's content, run as a module-like file (definitions, no main block)."""
        self.chunk, self.lines, self.shown, self.status = str(next(self.ids)), [], 0, ""
        self.done.clear()
        if self.lang == "shell":
            data = f"{code}\n__rc=$?; printf '\\0REPL-DONE %s %s\\n' {self.chunk} \"$([ $__rc -eq 0 ] && echo ok || echo error)\"\n"
        else:
            data = json.dumps({"id": self.chunk, "code": code, "file": file}) + "\n"
        assert self.proc.stdin is not None
        self.proc.stdin.write(data.encode())
        await self.proc.stdin.drain()

    async def execute(self, code: str, timeout: float, file: str = "") -> tuple[str, str]:
        """Run one chunk to its end, for the harness: (status, output). Status: ok, error, busy, timeout or exited."""
        if self.chunk is not None:
            return "busy", ""
        await self.send(code, file)
        try:
            async with asyncio.timeout(timeout):
                await self.done.wait()
        except TimeoutError:
            return "timeout", "".join(self.lines)
        return (self.status if self.alive else "exited"), "".join(self.lines)

    async def close(self) -> None:
        _kill_group(self.proc)
        await self.proc.wait()
        await asyncio.gather(self.reader, return_exceptions=True)


class ReplManager:
    """The session's REPLs, by name."""

    def __init__(self) -> None:
        self.repls: dict[str, Repl] = {}

    async def start(self, name: str, lang: str, exe: str | None, cwd: Path) -> Repl:
        proc = await asyncio.create_subprocess_exec(
            *LANGS[lang](exe), cwd=cwd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, start_new_session=True, limit=2**20,
        )
        self.repls[name] = Repl(name, lang, proc)
        return self.repls[name]

    async def close(self, name: str) -> None:
        if r := self.repls.pop(name, None):
            await r.close()

    def kill_all(self) -> None:
        """Synchronous (for /clear): kill now; each reader ends by itself at EOF."""
        for r in self.repls.values():
            _kill_group(r.proc)
        self.repls.clear()

    async def shutdown(self) -> None:
        await asyncio.gather(*(self.close(n) for n in list(self.repls)))


class ReplTool(Tool):
    def __init__(self) -> None:
        super().__init__(
            name="repl",
            description=(
                "Run code in a named, persistent interpreter that keeps its state (variables, imports, open "
                "connections) between calls: one call per experiment instead of a new script each time. The first call "
                "with a new name starts it. Output printed between calls (event handlers) is shown on the next call. "
                "Call with no code to collect new output, or to keep waiting for a chunk that outlived its timeout. "
                "node: top-level await works; python: so does await, and a final expression's value is printed."
            ),
            properties={
                "name": {"type": "string", "description": "REPL name, e.g. 'db'"},
                "code": {"type": "string", "description": "Code to run (empty: collect output / keep waiting)"},
                "lang": {"type": "string", "enum": list(LANGS), "description": "When starting it (default python)"},
                "interpreter": {"type": "string", "description": "When starting it: path of the interpreter binary"},
                "timeout": {"type": "integer", "description": f"Seconds to wait for the chunk (default {DEFAULT_TIMEOUT}); "
                            "it keeps running after that"},
                "restart": {"type": "boolean", "description": "Kill the REPL (losing its state) and start it again"},
                "load": {"type": "string", "description": "Files to load first (their definitions), a glob relative to "
                         "the working directory, e.g. 'tools/db/*.py': reloads saved skills into a fresh REPL"},
            },
            required=["name"],
        )

    def summary(self, args: dict[str, Any]) -> str:
        code = str(args.get("code") or "").strip().splitlines()
        return f"{args.get('name', '')}: {code[0][:150] if code else '(collect)'}"

    def preview(self, args: dict[str, Any], ctx: ToolContext) -> str | None:
        code = str(args.get("code") or "")
        return code if "\n" in code else None

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        manager: ReplManager = ctx.session.repls
        name, code = str(args["name"]), str(args.get("code") or "")
        r = manager.repls.get(name)
        lang = str(args.get("lang") or (r.lang if r else "python"))  # a restart keeps the language
        if lang not in LANGS:
            raise ToolError(f"lang must be one of {', '.join(LANGS)}")
        if args.get("restart"):
            await manager.close(name)
            r = None
        started = r is None or not r.alive
        if started:
            try:
                r = await manager.start(name, lang, args.get("interpreter"), ctx.cwd)
            except OSError as e:
                raise ToolError(f"cannot start the {lang} REPL: {e}")
        assert r is not None
        timeout = min(int(args.get("timeout") or DEFAULT_TIMEOUT), MAX_TIMEOUT)
        head = f"[started {r.lang} REPL '{r.name}']\n" if started else ""
        if args.get("load"):
            head += await _load(r, str(args["load"]), ctx.cwd, timeout)
        return await _run_chunk(r, code, timeout, ctx, head, manager)


async def _load(r: Repl, pattern: str, cwd: Path, timeout: float) -> str:
    """Run each matching file in the REPL as a file (its definitions); stop at the first one that fails."""
    files = sorted(p for p in cwd.glob(pattern) if p.is_file())
    if not files:
        raise ToolError(f"load: no file matches {pattern!r} in {cwd}")
    for p in files:
        rel = str(p.relative_to(cwd))
        status, out = await r.execute(p.read_text(errors="replace"), timeout, file=rel)
        if status != "ok":
            raise ToolError(clip(f"load: {rel} failed ({status})\n{out}"))
    return f"[loaded {', '.join(str(p.relative_to(cwd)) for p in files)}]\n"


async def _run_chunk(r: Repl, code: str, timeout: int, ctx: ToolContext, head: str, manager: ReplManager) -> str:
    earlier, r.idle = "".join(r.idle), []
    if earlier.strip():
        head += f"[output since the last call]\n{earlier}[end of that output]\n"
    if r.chunk is not None and code:
        raise ToolError(head + f"REPL '{r.name}' is still running the previous chunk: call it with no code to keep "
                        "waiting, or with restart=true to kill it (its state is lost).")
    if r.chunk is None:
        if not code:
            return head or "(no new output)"
        await r.send(code)
    r.emit, t0 = ctx.emit, time.monotonic()
    try:
        async with asyncio.timeout(timeout):
            await r.done.wait()
    except TimeoutError:
        new, r.shown = "".join(r.lines[r.shown:]), len(r.lines)
        return clip(head + new) + f"\n[still running after {timeout}s: call repl(name='{r.name}') with no code to keep waiting]"
    finally:
        r.emit = lambda _line: None
    out = clip(head + ("".join(r.lines[r.shown:]) or "(no output)\n"))
    if not r.alive:
        await manager.close(r.name)
        raise ToolError(f"{out}[the REPL exited (code {r.proc.returncode}); its state is lost, the next call starts a new one]")
    footer = f"[{r.name}: {r.status} · {time.monotonic() - t0:.1f}s]"
    if r.status != "ok":
        raise ToolError(f"{out}{footer}")
    return f"{out}{footer}"
