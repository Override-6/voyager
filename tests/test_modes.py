"""Chat mode vs voyager mode: /voyager, /chat, and the mode line under the chat bar."""
import asyncio

from voyager.session import Session
from voyager.tui import panes
from voyager.tui.controller import Controller


def text(line):
    return "".join(t for _, t in line)


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
