from conftest import run
from voyager.session import Session
from voyager.tools import ToolError
import pytest


def test_spawn_runs_in_background_and_notifies_parent(session, offline):
    async def go():
        child = session.spawn(session.main, "Find Auth Code!", "look around", "local")
        assert child.id == "a1" and child.name == "find-auth-code" and child.running
        await session.wait_idle()
        return child
    child = run(go())
    assert child.status == "done" and child.last_report == "report from a1"
    msgs = [t for i, t in offline if i == "main"]
    assert msgs and '<agent-notification agent="a1"' in msgs[0] and "report from a1" in msgs[0]


def test_resume_finished_agent_keeps_history(session, offline):
    async def go():
        child = session.spawn(session.main, "w", "first", "local")
        await session.wait_idle()
        child.submit("second")  # resume
        await session.wait_idle()
    run(go())
    assert [t for i, t in offline if i == "a1"] == ["first", "second"]


def test_limits_and_unknown_type(session, offline):
    session.cfg.max_running_agents = 1

    async def go():
        session.spawn(session.main, "a", "x")
        with pytest.raises(ToolError):
            session.spawn(session.main, "b", "y")
        with pytest.raises(ToolError):
            session.spawn(session.main, "c", "y", "gpt")
        await session.wait_idle()
    run(go())


def test_save_load_roundtrip(cfg, session, offline):
    async def go():
        session.main.messages.append({"role": "user", "content": "hi"})
        session.main.log.add("user", "hi")
        session.spawn(session.main, "w", "task", "coder")
        await session.wait_idle()
    run(go())
    session.save()
    loaded = Session.load(cfg, session.id)
    assert set(loaded.agents) == {"main", "a1"} and loaded.agents["a1"].kind == "coder"
    assert loaded.main.messages == session.main.messages
    assert loaded.agents["a1"].claude_session == session.agents["a1"].claude_session
    assert Session.load(cfg).id == session.id  # latest session for this cwd
