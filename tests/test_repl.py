"""repl: named interpreters that keep their state between calls, and die with the session."""
import asyncio
import shutil

import pytest

from voyager.session import Session
from voyager.tools import ToolContext, ToolError
from voyager.tools.repl import ReplTool

TOOL = ReplTool()


def with_session(cfg, body):
    async def go():
        s = Session(cfg)
        ctx = ToolContext(cwd=cfg.cwd, session=s)
        try:
            return await body(s, lambda **a: TOOL.run(a, ctx))
        finally:
            await s.repls.shutdown()
    return asyncio.run(go())


def test_python_state_persists_and_the_last_expression_is_shown(cfg):
    async def body(s, repl):
        first = await repl(name="py", code="import math\nx = 21")
        assert "[started python REPL 'py']" in first and "[py: ok" in first
        assert "42" in await repl(name="py", code="x * 2")
        assert "3.14" in await repl(name="py", code="import asyncio\nawait asyncio.sleep(0)\nround(math.pi, 2)")
        with pytest.raises(ToolError, match="ZeroDivisionError"):
            await repl(name="py", code="1/0")
        assert "21" in await repl(name="py", code="print(x)")  # an error does not lose the state
    with_session(cfg, body)


def test_a_slow_chunk_keeps_running_and_output_between_calls_is_kept(cfg):
    async def body(s, repl):
        await repl(name="py", code="import threading, time")
        out = await repl(name="py", code="print('start', flush=True); time.sleep(1.5); print('end')", timeout=1)
        assert "start" in out and "still running after 1s" in out
        with pytest.raises(ToolError, match="still running the previous chunk"):
            await repl(name="py", code="1")
        out = await repl(name="py", timeout=5)
        assert "end" in out and "start" not in out  # only what is new
        await repl(name="py", code="threading.Timer(0.1, lambda: print('tick', flush=True)).start()")
        await asyncio.sleep(0.4)
        assert "[output since the last call]\ntick" in await repl(name="py")
    with_session(cfg, body)


def test_shell_repl_and_restart(cfg):
    async def body(s, repl):
        await repl(name="sh", lang="shell", code="cd /tmp && export V=7")
        assert "/tmp 7" in await repl(name="sh", code='echo "$(pwd) $V"')
        with pytest.raises(ToolError, match="sh: error"):
            await repl(name="sh", code="false")
        out = await repl(name="sh", code='echo "[$V]"', restart=True)
        assert "started shell REPL" in out and "[]" in out
    with_session(cfg, body)


def test_an_exited_repl_is_reported_and_restarted(cfg):
    async def body(s, repl):
        with pytest.raises(ToolError, match="the REPL exited"):
            await repl(name="py", code="raise SystemExit(3)")
        assert "py" not in s.repls.repls
        assert "started python REPL" in await repl(name="py", code="1")
    with_session(cfg, body)


def test_the_session_kills_its_repls(cfg):
    async def body(s, repl):
        await repl(name="py", code="1")
        proc = s.repls.repls["py"].proc
        await s.repls.shutdown()
        assert proc.returncode is not None and not s.repls.repls
    with_session(cfg, body)


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_node_declarations_persist_with_and_without_await(cfg):
    async def body(s, repl):
        await repl(name="js", lang="node", code="const a = 2")
        await repl(name="js", lang="node", code="const b = await Promise.resolve(3)")
        assert "6" in await repl(name="js", code="a * b")
    with_session(cfg, body)
