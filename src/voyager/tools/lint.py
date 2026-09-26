"""Lint feedback for the file tools: after a Python file is written or edited, ruff's high-signal findings ride along.

Only syntax errors and pyflakes rules (undefined names, unused imports / variables, redefinitions): mistakes a model
makes and does not see, never style. Informational: the write already happened, the result is not an error.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

RULES = "E9,F"
MAX_LINES = 20
TIMEOUT = 10
FINDING_RE = re.compile(r":\d+:\d+: ")


def _ruff() -> str | None:
    try:
        from ruff.__main__ import find_ruff_bin
        return str(find_ruff_bin())
    except (ImportError, FileNotFoundError):
        return None


async def lint_note(path: Path, cwd: Path) -> str:
    """"" when the file is clean, not Python, or ruff is unavailable; otherwise a short block to append to the result."""
    if path.suffix != ".py" or not (ruff := _ruff()):
        return ""
    shown = str(path.relative_to(cwd)) if path.is_relative_to(cwd) else str(path)
    try:
        proc = await asyncio.create_subprocess_exec(
            ruff, "check", "--isolated", "--no-cache", "--select", RULES, "--output-format", "concise", shown,
            cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
    except OSError:
        return ""
    try:
        async with asyncio.timeout(TIMEOUT):
            out, _ = await proc.communicate()
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return ""
    findings = [ln.replace(" [*]", "") for ln in out.decode(errors="replace").splitlines() if FINDING_RE.search(ln)]
    if not findings:
        return ""
    more = f"\n… {len(findings) - MAX_LINES} more" if len(findings) > MAX_LINES else ""
    return f"\nlint (ruff): {len(findings)} problem(s)\n" + "\n".join(findings[:MAX_LINES]) + more
