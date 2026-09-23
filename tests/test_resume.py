import dataclasses
import os
import time

import pytest

from conftest import run
from voyager.session import Session
from voyager.sessions_store import list_saved
from voyager.tui.controller import Controller
from voyager.tui.picker import picker_rows


def make_saved(cfg, sid, text, age=0):
    s = Session(cfg, session_id=sid)
    s.main.log.add("user", text, src="user")
    s.main.messages.append({"role": "user", "content": text})
    s.save()
    t = time.time() - age
    os.utime(s.path, (t, t))
    return s


def flat(rows):
    return "\n".join("".join(f[1] for f in line) for line, _ in rows)


def test_empty_sessions_are_not_saved(cfg):
    s = Session(cfg)
    s.save()
    assert not s.dir.exists()


def test_list_saved_orders_filters_and_reads_legacy_sessions(cfg, tmp_path):
    make_saved(cfg, "20260101-000001", "fix the calc bug", age=100)
    make_saved(cfg, "20260101-000002", "write the readme", age=10)
    other = dataclasses.replace(cfg, cwd=tmp_path / "elsewhere")
    make_saved(other, "20260101-000003", "unrelated project", age=1)
    here = list_saved(cfg.sessions_dir, cwd=str(cfg.cwd))
    assert [i.title for i in here] == ["write the readme", "fix the calc bug"]  # newest first, this directory only
    assert len(list_saved(cfg.sessions_dir)) == 3  # all directories
    assert [i.id for i in list_saved(cfg.sessions_dir, cwd=str(cfg.cwd), exclude="20260101-000002")] == ["20260101-000001"]
    assert here[0].matches("README") and not here[0].matches("calc")
    (cfg.sessions_dir / "20260101-000001" / "meta.json").unlink()  # saved by an older version: still listed
    assert {i.title for i in list_saved(cfg.sessions_dir, cwd=str(cfg.cwd))} == {"write the readme", "fix the calc bug"}


def test_load_by_prefix_latest_and_ambiguity(cfg):
    make_saved(cfg, "20260101-000001", "a", age=50)
    make_saved(cfg, "20260202-000001", "b", age=5)
    assert Session.load(cfg, "20260202").id == "20260202-000001"  # unique prefix
    assert Session.load(cfg).id == "20260202-000001"  # latest for this directory
    with pytest.raises(FileNotFoundError, match="ambiguous"):
        Session.load(cfg, "2026")
    with pytest.raises(FileNotFoundError):
        Session.load(cfg, "nope")


def test_picker_filters_and_swaps_the_running_conversation(cfg, offline):
    make_saved(cfg, "20260101-000001", "fix the calc bug", age=100)
    make_saved(cfg, "20260101-000002", "write the readme", age=10)

    async def go():
        current = Session(cfg)  # fresh, empty
        ctl = Controller(current)
        ctl.open_picker()
        text = flat(picker_rows(ctl, 100, 20))
        assert "write the readme" in text and "fix the calc bug" in text and "❯" in text
        assert [f for line, _ in picker_rows(ctl, 100, 20) for f in line if len(f) == 3]  # clickable rows
        ctl.buffer.text = "calc"  # the input box is the filter
        assert [i.title for i in ctl.picker.visible(ctl.buffer.text)] == ["fix the calc bug"]
        ctl.picker_select()
        await ctl._swap_task
        assert ctl.session.id == "20260101-000001" and ctl.picker is None and ctl.focus_id == "main"
        assert ctl.session.main.messages == [{"role": "user", "content": "fix the calc bug"}]
        assert ctl.session.main.log.items[0].text == "fix the calc bug"
        assert not current.path.exists()  # the empty session we left was not saved
        assert ctl in [ctl] and any(h for h in ctl.session.hooks)  # new session is wired to the UI
        ctl.resume_session("does-not-exist")  # a failed load keeps the current conversation
        await ctl._swap_task
        assert ctl.session.id == "20260101-000001" and "cannot resume" in ctl.flash
    run(go())


def test_resume_command_variants(cfg, offline):
    make_saved(cfg, "20260101-000009", "old chat", age=30)

    async def go():
        s = Session(cfg)
        ctl = Controller(s)
        ctl.command("/resume")
        assert ctl.picker is not None
        ctl.close_picker()
        s.spawn(s.main, "helper", "do it")
        await s.wait_idle()
        ctl.command("/resume a1 once more")  # an agent id still resumes that agent
        await s.wait_idle()
        assert [t for i, t in offline if i == "a1"] == ["do it", "once more"] and ctl.session is s
        ctl.command("/continue a1 and again")
        await s.wait_idle()
        assert [t for i, t in offline if i == "a1"][-1] == "and again"
        ctl.command("/resume 20260101-000009")  # a session id loads that conversation
        await ctl._swap_task
        assert ctl.session.id == "20260101-000009"
    run(go())
