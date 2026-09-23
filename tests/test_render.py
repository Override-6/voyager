from voyager.log import Item
from voyager.tui.render import item_lines
from voyager.tui.wrap import clean, text_width, wrap_text


def flat(lines):
    return ["".join(t for _, t in ln) for ln in lines]


def test_wrap_respects_width_and_hangs():
    lines = wrap_text("word " * 30 + "x" * 80, "", 30, first=("b", "⏺ "), hang="  ")
    assert all(text_width(ln) <= 30 for ln in lines)
    assert flat(lines)[0].startswith("⏺ ") and flat(lines)[1].startswith("  ")


def test_clean_strips_ansi_and_control_chars():
    assert clean("a\x1b[31mred\x1b[0m\tb\r\nc\x07") == "ared    b\nc"


def _tool(**meta):
    base = {"name": "write_file", "summary": "a.py", "status": "done", "args": {"path": "a.py", "content": "line1\n" + "long " * 40},
            "result": "\n".join(f"out {i}" for i in range(30))}
    return Item(1, "tool", meta={**base, **meta})


def test_tool_call_is_collapsed_by_default_and_expands_fully():
    item = _tool()
    short = flat(item_lines(item, 60, 0))
    assert any("click to expand" in ln for ln in short) and not any("Parameters" in ln for ln in short)
    item.meta["expanded"] = True
    full = flat(item_lines(item, 60, 0))
    assert any("Parameters" in ln for ln in full) and any("content:" in ln for ln in full)
    assert "out 29" in " ".join(full) and len(full) > len(short)
    item.meta["expanded"] = False
    assert flat(item_lines(item, 60, 0)) == short  # collapses back


def test_background_tool_shows_task_marker():
    item = _tool(status="background", task_id="t3", live=["a", "b"])
    assert any("task t3" in ln for ln in flat(item_lines(item, 60, 0)))
