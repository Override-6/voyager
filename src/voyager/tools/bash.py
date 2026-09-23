"""bash tool: async subprocess, output lines streamed live via ctx.emit.

Cancellation (the user stops the agent) arrives as asyncio.CancelledError; the
`finally` kills the whole process group so nothing keeps running.
"""

from __future__ import annotations

import asyncio
import os
import signal
from typing import Any

from .base import Tool, ToolContext, ToolError, clip

DEFAULT_TIMEOUT = 600
MAX_TIMEOUT = 3600


class Bash(Tool):
    def __init__(self) -> None:
        super().__init__(
            name="bash",
            description=(
                "Run a shell command in the working directory and return combined stdout/stderr "
                "plus the exit code. Commands run non-interactively; do not start long-lived servers."
            ),
            properties={
                "command": {"type": "string", "description": "The shell command to run"},
                "timeout": {"type": "integer", "description": f"Timeout in seconds (default {DEFAULT_TIMEOUT}, max {MAX_TIMEOUT})"},
            },
            required=["command"],
            streams_output=True,
            backgroundable=True,
        )

    def summary(self, args: dict[str, Any]) -> str:
        return str(args.get("command", "")).strip().splitlines()[0][:200] if args.get("command") else ""

    def preview(self, args: dict[str, Any], ctx: ToolContext) -> str | None:
        cmd = str(args["command"])
        return cmd if "\n" in cmd else None  # single-line commands are already in the call header

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        cmd = str(args["command"])
        timeout = min(int(args.get("timeout") or DEFAULT_TIMEOUT), MAX_TIMEOUT)
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=ctx.cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
            limit=2**20,
        )
        lines: list[str] = []
        timed_out = False

        async def pump() -> None:
            assert proc.stdout is not None
            while raw := await proc.stdout.readline():
                line = raw.decode(errors="replace")
                lines.append(line)
                ctx.emit(line.rstrip("\n"))

        try:
            try:
                async with asyncio.timeout(timeout):
                    await pump()
                    await proc.wait()
            except TimeoutError:
                timed_out = True
        finally:  # also runs on CancelledError
            _kill_group(proc)
            await proc.wait()
        output = "".join(lines)
        if timed_out:
            raise ToolError(clip(output) + f"\n[timed out after {timeout}s and was killed]")
        result = clip(output) if output.strip() else "(no output)"
        return f"{result}\n[exit code {proc.returncode}]"


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
