from pathlib import Path

import pytest

from conftest import run
from voyager.tools import ToolContext, ToolError
from voyager.tools.files import EditFile, WriteFile


def ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(cwd=tmp_path)


def test_write_file_rejects_empty_content(tmp_path):
    target = tmp_path / "notes" / "hack-1.2.3.4.md"
    for content in ("", "   ", "\n\n"):
        with pytest.raises(ToolError, match="empty"):
            run(WriteFile().run({"path": str(target), "content": content}, ctx(tmp_path)))
    assert not target.exists()  # never partially created


def test_write_file_creates_parent_dirs_with_real_content(tmp_path):
    target = tmp_path / "notes" / "hack-1.2.3.4.md"
    out = run(WriteFile().run({"path": str(target), "content": "# findings\nport 22 open\n"}, ctx(tmp_path)))
    assert target.read_text() == "# findings\nport 22 open\n" and "Created" in out
    out2 = run(WriteFile().run({"path": str(target), "content": "overwritten\n"}, ctx(tmp_path)))
    assert target.read_text() == "overwritten\n" and "Overwrote" in out2


def test_edit_file_can_still_delete_text_to_empty_string(tmp_path):
    # only write_file's *creation* of an empty file is blocked; editing a section down to "" is legitimate
    target = tmp_path / "a.py"
    target.write_text("keep\nDROP_ME\n")
    run(EditFile().run({"path": str(target), "old_string": "DROP_ME\n", "new_string": ""}, ctx(tmp_path)))
    assert target.read_text() == "keep\n"
