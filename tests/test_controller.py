from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from prompt_toolkit.data_structures import Point

from voyager.session import Session
from voyager.tui.controller import Controller


def click(h):
    h(MouseEvent(Point(0, 0), MouseEventType.MOUSE_UP, MouseButton.LEFT, frozenset()))


def test_history_persists_across_restarts_and_dedupes(cfg, offline):
    c = Controller(Session(cfg))
    for text in ("first thing", "second thing", "second thing"):
        c.buffer.text = text
        c.buffer.reset(append_to_history=True)
    c2 = Controller(Session(cfg))
    assert list(c2.buffer.history.load_history_strings()) == ["second thing", "first thing"]  # newest first, no dup


def test_click_toggles_tool_call(cfg):
    s = Session(cfg)
    c = Controller(s)
    item = s.main.log.add("tool", name="bash", status="done", args={"command": "ls"})
    h = c.handler(s.main, item)
    click(h)
    assert item.meta["expanded"] is True
    click(h)
    assert item.meta["expanded"] is False


def test_navigation_and_commands(cfg, offline):
    import asyncio

    async def go():
        s = Session(cfg)
        c = Controller(s)
        s.spawn(s.main, "w", "task")
        c.enter_list()
        c.list_move(1)
        c.list_select()
        assert c.focus_id == "a1"
        c.back()
        assert c.focus_id == "main"
        c.command("/agent nope")
        assert "no such agent" in c.flash
        await s.wait_idle()
        c.command("/resume a1 again")
        assert c.focus_id == "a1"
        await s.wait_idle()
        return [t for i, t in offline if i == "a1"]
    assert asyncio.run(go()) == ["task", "again"]
