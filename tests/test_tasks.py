import asyncio

from conftest import run
from voyager.log import Item
from voyager.tools import Tool, ToolContext, ToolError


class Slow(Tool):
    def __init__(self, secs=0.3, fail=False):
        super().__init__(name="slow", description="", properties={}, backgroundable=True)
        self.secs, self.fail = secs, fail

    async def run(self, args, ctx):
        ctx.emit("working")
        await asyncio.sleep(self.secs)
        if self.fail:
            raise ToolError("boom")
        return "finished ok"


def _call(session, tool, background=False):
    item = session.main.log.start("tool", name="slow", status="running")
    ctx = ToolContext(cwd=session.cfg.cwd, emit=lambda l: session.main.log.tool_live(item, l), agent=session.main, session=session)
    return item, session.tasks.run(session.main, tool, {}, ctx, item, background)


def test_fast_call_stays_in_foreground(session, offline):
    session.cfg.auto_background_secs = 1

    async def go():
        item, co = _call(session, Slow(0.01))
        return await co
    assert run(go()) == ("finished ok", False)


def test_slow_call_auto_backgrounds_and_notifies_owner(session, offline):
    session.cfg.auto_background_secs = 0.05

    async def go():
        item, co = _call(session, Slow(0.2))
        text, bg = await co
        assert bg and "task t1" in text and item.meta["status"] == "background"
        await session.wait_idle()  # waits for the task AND the turn its notification starts
        return item
    item = run(go())
    assert item.meta["status"] == "done" and item.meta["result"] == "finished ok"
    assert any("<task-notification" in t and "finished ok" in t for _, t in offline)


def test_explicit_background_and_kill(session, offline):
    async def go():
        item, co = _call(session, Slow(5), background=True)
        text, bg = await co
        assert bg and session.tasks.kill("t1")
        await session.wait_idle()
    run(go())
    assert session.tasks.get("t1").status == "killed"
    assert not any("<task-notification" in t for _, t in offline)  # a deliberate kill doesn't wake the agent


def test_failed_background_task_reports_error(session, offline):
    async def go():
        item, co = _call(session, Slow(0.05, fail=True), background=True)
        await co
        await session.wait_idle()
    run(go())
    assert session.tasks.get("t1").status == "error"
    assert any('status="error"' in t and "boom" in t for _, t in offline)


def test_notification_tail_starts_on_a_line_boundary(session, offline):
    class Chatty(Tool):
        def __init__(self):
            super().__init__(name="chatty", description="", properties={}, backgroundable=True)

        async def run(self, args, ctx):
            return "\n".join(f"line number {i} of output" for i in range(1, 401))

    async def go():
        item, co = _call(session, Chatty(), background=True)
        await co
        await session.wait_idle()
    run(go())
    note = next(t for _, t in offline if "<task-notification" in t)
    body = note.split(":\n", 1)[1]
    assert "last lines only" in note and body.splitlines()[0].startswith("line number ")  # not "number 286 of o..."
    assert "line number 400 of output" in note
