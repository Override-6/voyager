"""Key bindings. Two modes: normal (typing / history / agent switching) and list (browsing agents & tasks)."""

from __future__ import annotations

from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.keys import Keys

from .controller import Controller


def build_bindings(ctl: Controller) -> KeyBindings:
    kb = KeyBindings()
    in_list = Condition(lambda: ctl.list_mode)
    in_picker = Condition(lambda: ctl.picker is not None)
    normal = ~in_list & ~in_picker
    buf = ctl.buffer

    # ---- normal mode --------------------------------------------------------
    @kb.add("enter", filter=normal, eager=True)
    def _(e: KeyPressEvent) -> None:
        ctl.submit()

    @kb.add("c-j", filter=normal)
    def _(e: KeyPressEvent) -> None:
        buf.insert_text("\n")

    @kb.add("up", filter=normal)
    def _(e: KeyPressEvent) -> None:
        buf.auto_up()  # moves up a line in a multi-line input, otherwise back through the saved history

    @kb.add("down", filter=normal)
    def _(e: KeyPressEvent) -> None:
        if not buf.text:
            ctl.enter_list()  # ↓ on an empty prompt opens the agent / task list under it
        else:
            buf.auto_down()

    @kb.add("escape", filter=normal, eager=True)
    def _(e: KeyPressEvent) -> None:
        ctl.back()

    @kb.add("tab", filter=normal)
    def _(e: KeyPressEvent) -> None:
        ctl.cycle(1)

    @kb.add("s-tab", filter=normal)
    def _(e: KeyPressEvent) -> None:
        ctl.cycle(-1)

    # ---- list mode ------------------------------------------------------------
    @kb.add("up", filter=in_list, eager=True)
    def _(e: KeyPressEvent) -> None:
        ctl.list_move(-1)

    @kb.add("down", filter=in_list, eager=True)
    def _(e: KeyPressEvent) -> None:
        ctl.list_move(1)

    @kb.add("enter", filter=in_list, eager=True)
    def _(e: KeyPressEvent) -> None:
        ctl.list_select()

    @kb.add("escape", filter=in_list, eager=True)
    def _(e: KeyPressEvent) -> None:
        ctl.list_mode = False

    @kb.add("x", filter=in_list, eager=True)
    def _(e: KeyPressEvent) -> None:
        ctl.list_kill()

    @kb.add(Keys.Any, filter=in_list)
    def _(e: KeyPressEvent) -> None:  # typing while the list is open goes back to the prompt
        ctl.list_mode = False
        if e.data and e.data.isprintable():
            buf.insert_text(e.data)

    # ---- always ---------------------------------------------------------------
    @kb.add("c-c")
    def _(e: KeyPressEvent) -> None:
        if ctl.list_mode:
            ctl.list_mode = False
        else:
            ctl.interrupt()

    @kb.add("c-d")
    def _(e: KeyPressEvent) -> None:
        if buf.text:
            buf.delete()
        else:
            ctl.exit()

    @kb.add("pageup")
    def _(e: KeyPressEvent) -> None:
        ctl.scroll_by(max(3, e.app.output.get_size().rows // 2))

    @kb.add("pagedown")
    def _(e: KeyPressEvent) -> None:
        ctl.scroll_by(-max(3, e.app.output.get_size().rows // 2))

    # ---- resume picker (registered last: wins over the always-on bindings above) ----
    @kb.add("up", filter=in_picker, eager=True)
    def _(e: KeyPressEvent) -> None:
        ctl.picker_move(-1)

    @kb.add("down", filter=in_picker, eager=True)
    def _(e: KeyPressEvent) -> None:
        ctl.picker_move(1)

    @kb.add("pageup", filter=in_picker, eager=True)
    def _(e: KeyPressEvent) -> None:
        ctl.picker_move(-5)

    @kb.add("pagedown", filter=in_picker, eager=True)
    def _(e: KeyPressEvent) -> None:
        ctl.picker_move(5)

    @kb.add("enter", filter=in_picker, eager=True)
    def _(e: KeyPressEvent) -> None:
        ctl.picker_select()

    @kb.add("escape", filter=in_picker, eager=True)
    def _(e: KeyPressEvent) -> None:
        ctl.close_picker()

    @kb.add("tab", filter=in_picker, eager=True)
    def _(e: KeyPressEvent) -> None:
        if ctl.picker:
            ctl.picker.toggle_dirs()

    return kb
