"""Skills: tools verified live in a running REPL (load, example, check), and command-line tools with a `check`."""
import asyncio
import dataclasses

import pytest

from voyager.session import Session
from voyager.tools import ToolContext, ToolError
from voyager.tools.repl import ReplTool
from voyager.voyager.state import parse_header, state_block
from voyager.voyager.tools import SaveTool
from voyager.voyager.workspace import Workspace

SKILL = '''"""
summary: Add items to the world's inventory.
usage: add(world, item, n)
example: add(world, 'plank', 4)
check: world.get('plank', 0) >= 4
repl: game
"""
def add(world, item, n):
    world[item] = world.get(item, 0) + n

if __name__ == "__main__":
    raise SystemExit("a loaded skill must not run its main block")
'''


@pytest.fixture
def ws(cfg):
    return Workspace.create(cfg.workspaces_dir, "demo", "Build things.")


def in_session(cfg, ws, body):
    async def go():
        s = Session(dataclasses.replace(cfg, cwd=ws.root))
        ctx = ToolContext(cwd=ws.root, session=s, agent=s.main)
        try:
            return await body(s, lambda **a: SaveTool().run(a, ctx), lambda **a: ReplTool().run(a, ctx))
        finally:
            await s.repls.shutdown()
    return asyncio.run(go())


def status_of(ws, rel):
    return parse_header((ws.root / rel).read_text())["status"]


def test_a_skill_is_verified_live_in_its_repl(cfg, ws):
    async def body(s, save, repl):
        with pytest.raises(ToolError, match="REPL 'game', which is not running"):
            await save(path="tools/game/add.py", content=SKILL)
        await repl(name="game", code="world = {}")
        out = await save(path="tools/game/add.py", content=SKILL)
        assert "VERIFIED" in out and ">>> check" in out and "True" in out
        assert "4" in await repl(name="game", code="world['plank']")  # the skill stays loaded, the example ran live
        assert "(skill, repl game)" in state_block(ws, main=True)

        wrong = SKILL.replace("+ n", "+ 0").replace("plank", "log")  # runs without error, but has no effect
        with pytest.raises(ToolError, match="check is not true"):
            await save(path="tools/game/add2.py", content=wrong)
        assert status_of(ws, "tools/game/add2.py") == "draft"
        with pytest.raises(ToolError, match="needs a `check:`"):
            await save(path="tools/game/add3.py", content=SKILL.replace("check: world.get('plank', 0) >= 4\n", ""))
    in_session(cfg, ws, body)


def test_a_fresh_repl_reloads_saved_skills(cfg, ws):
    async def body(s, save, repl):
        await repl(name="game", code="world = {}")
        await save(path="tools/game/add.py", content=SKILL)
        out = await repl(name="game", restart=True, load="tools/game/*.py", code="world = {}; add(world, 'x', 2); world")
        assert "[loaded tools/game/add.py]" in out and "{'x': 2}" in out
        with pytest.raises(ToolError, match="no file matches"):
            await repl(name="game", load="tools/nothing/*.py")
    in_session(cfg, ws, body)


def test_a_command_line_tool_check_must_pass(cfg, ws):
    tool = '"""\nsummary: Write a file.\nusage: python3 tools/w.py\nexample: python3 tools/w.py\ncheck: test -s out.txt\n"""\n'
    async def body(s, save, repl):
        with pytest.raises(ToolError, match=r"check failed \(exit code 1\)"):
            await save(path="tools/w.py", content=tool + "pass\n")
        out = await save(path="tools/w.py", content=tool + "open('out.txt', 'w').write('ok')\n")
        assert "VERIFIED" in out and "$ test -s out.txt" in out
    in_session(cfg, ws, body)
