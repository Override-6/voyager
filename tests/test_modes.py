"""Chat mode vs voyager mode: /voyager, /chat, and the mode line under the chat bar."""
import asyncio
import dataclasses

from voyager.session import Session
from voyager.tui import panes
from voyager.tui.controller import Controller
from voyager.voyager.workspace import Workspace


def text(line):
    return "".join(t for _, t in line)


def saved_session(cfg, sid, last, outcome, where=None):
    """A saved session whose history ends with `last` ("user": unanswered, "assistant": answered) and a given outcome."""
    s = Session(where or cfg, session_id=sid)
    s.main.log.add("user", "do it", src="user")
    s.main.messages.append({"role": "user", "content": "do it"})
    if last == "assistant":
        s.main.messages.append({"role": "assistant", "content": [{"type": "text", "text": "done"}]})
    s.main.outcome = outcome
    s.save()
    return s


def test_a_resumed_session_goes_back_to_work_only_if_it_was_interrupted(cfg, offline):
    async def go():
        for sid, last, outcome, expect in [("s1", "assistant", "done", False),   # answered: nothing to do
                                           ("s2", "assistant", "stopped", True),  # stopped by the user
                                           ("s3", "user", "done", True),          # cut off before the model answered
                                           ("s4", "assistant", "error", True)]:   # failed
            saved_session(cfg, sid, last, outcome)
            back = Session.load(cfg, sid)
            assert back.resume_work() is expect, sid
            assert back.resume_work() is False  # once per resume
            await back.wait_idle()
            if expect:
                assert "resumed" in offline[-1][1] and "<resumed>" in offline[-1][1]
                assert any("session resumed" in i.text for i in back.main.log.items)
        assert Session(cfg).resume_work() is False  # a new session is not a resume
        # a mission with an unfinished plan continues even after a clean turn; a blocked one waits for the user
        ws = Workspace.create(cfg.workspaces_dir, "quest", "beat the boss")
        wcfg = dataclasses.replace(cfg, cwd=ws.root)
        saved_session(cfg, "m1", "assistant", "done", where=wcfg)
        assert Session.load(cfg, "m1").resume_work() is True  # phase 0: no approach recorded yet
        (ws.root / "PLAN.md").write_text("# Plan\n## Phases\n- [>] 1. Recon — exit: x\n## Now\n- [ ] a\n## Blocked\n- need a key\n## Log\n")
        saved_session(cfg, "m3", "assistant", "done", where=wcfg)  # saved after the edit: nothing changed while it was down
        assert Session.load(cfg, "m3").resume_work() is False
        saved_session(cfg, "m2", "assistant", "stopped", where=wcfg)
        (ws.root / "PLAN.md").write_text("# Plan\n## Phases\n- [>] 1. Recon — exit: x\n## Now\n- [ ] a\n## Blocked\n## Log\n")
        back = Session.load(cfg, "m2")
        assert back.resume_work() is True
        await back.wait_idle()
    asyncio.run(go())
    assert "trust PLAN.md" in offline[-1][1]  # the mission's resume note


def test_a_session_resumes_in_its_own_mode_wherever_it_is_resumed_from(cfg, tmp_path):
    ws = Workspace.create(cfg.workspaces_dir, "quest", "beat the boss")
    mission = Session(dataclasses.replace(cfg, cwd=ws.root), session_id="mission-1")
    mission.main.log.add("user", "go", src="user")
    mission.main.messages.append({"role": "user", "content": "go"})
    mission.save()
    chat = Session(cfg, session_id="chat-1")
    chat.main.log.add("user", "hello", src="user")
    chat.save()
    elsewhere = dataclasses.replace(cfg, cwd=tmp_path)  # resumed from an unrelated directory
    back = Session.load(elsewhere, mission.id)
    assert back.mode.name == "voyager" and back.workspace.name == "quest" and back.cfg.cwd == ws.root
    assert elsewhere.cwd == tmp_path  # the caller's config is untouched
    assert Session.load(elsewhere, chat.id).mode.name == "chat"
    ws.root.rename(ws.root.with_name("gone"))  # its folder disappeared: fall back to where we are, don't crash
    assert Session.load(elsewhere, mission.id).mode.name == "chat"


def test_mode_line_follows_the_conversation_cwd(cfg, offline):
    async def go():
        c = Controller(Session(cfg))
        assert "chat" in text(panes.mode_line(c)) and "/voyager" in text(panes.mode_line(c))
        c.command("/voyager quest")
        await c._swap_task
        line = text(panes.mode_line(c))
        assert "voyager" in line and "quest" in line and "phase 0" in line and "/chat" in line
        c.command("/chat")
        await c._swap_task
        assert c.session.workspace is None and "chat" in text(panes.mode_line(c))
    asyncio.run(go())


def test_voyager_creates_with_objective_starts_it_and_reopens(cfg, offline):
    async def go():
        c = Controller(Session(cfg))
        c.command("/voyager")
        assert "no workspaces yet" in c.session.main.log.items[-1].text
        c.command("/voyager quest beat the boss")
        await c._swap_task
        s = c.session
        assert s.workspace.name == "quest" and "beat the boss" in s.workspace.read("OBJECTIVE.md")
        assert s.main.inbox or s.main.running or s.main.messages  # the objective was submitted as the first message
        c.command("/voyager off")
        await c._swap_task
        c.command("/voyager quest something else")
        await c._swap_task
        assert c.session.workspace.name == "quest" and "objective ignored" in c.flash
        c.command("/voyager new x")
        assert "usage" in c.flash
        c.command("/voyager Bad Name")  # invalid names are refused, the conversation stays
        assert c.session.workspace.name == "quest"
    asyncio.run(go())
