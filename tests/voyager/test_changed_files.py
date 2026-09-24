"""Key workspace files edited behind the agent's back are flagged after a compaction and on resume."""
import asyncio
import copy
import json

from conftest import run
from test_compaction import MSGS, FakeClient
from test_workspace import ws, wcfg  # noqa: F401  (fixtures)
from voyager.session import Session

PLAN = "# Plan\n## Phases\n- [>] 1. Recon — exit: x\n## Now\n- [ ] a\n## Blocked\n## Log\n"


def compact(m, cfg):
    """Run a full compaction (checkpoint first, then the summary) and return the summary message text."""
    m.messages = [copy.deepcopy(x) for x in MSGS * 4]
    m._measured_tokens, m._measured_len = int(cfg.context_window * 0.75), len(m.messages)
    run(m._maybe_compact())
    m._measured_tokens = int(cfg.context_window * 0.75)
    run(m._maybe_compact())
    return str(m.messages[0]["content"])


def test_edited_plan_and_approach_are_flagged_after_compaction(wcfg, ws):
    m = Session(wcfg, client=FakeClient()).main
    (ws.root / "PLAN.md").write_text(PLAN)
    (ws.root / "knowledge").mkdir(exist_ok=True)
    (ws.root / "knowledge" / "approach.md").write_text("Use the API.\n")
    text = compact(m, wcfg)
    assert "stopped): `PLAN.md`, `knowledge/approach.md`." in text and "read_file" in text
    assert "trust PLAN.md" in text  # the regular note is still there


def test_unchanged_files_add_no_note_and_the_baseline_resets(wcfg, ws):
    m = Session(wcfg, client=FakeClient()).main
    assert "changed while you were not looking" not in compact(m, wcfg)
    (ws.root / "PLAN.md").write_text(PLAN)
    assert "changed while you were not looking" in compact(m, wcfg)  # flagged once ...
    assert "changed while you were not looking" not in compact(m, wcfg)  # ... then the new round's baseline holds


def test_an_edit_while_stopped_shows_in_the_resume_message_and_continues(wcfg, ws, offline):
    async def go():
        s = Session(wcfg, session_id="q1")
        s.main.log.add("user", "go", src="user")
        s.main.messages += [{"role": "user", "content": "go"}, {"role": "assistant", "content": [{"type": "text", "text": "done"}]}]
        s.main.outcome = "done"
        (ws.root / "PLAN.md").write_text(PLAN.replace("## Blocked\n", "## Blocked\n- need a key\n"))  # blocked: would wait
        s.save()
        assert Session.load(wcfg, "q1").resume_work() is False  # nothing changed while it was down
        (ws.root / "PLAN.md").write_text(PLAN)  # the user unblocks it while the session is stopped
        back = Session.load(wcfg, "q1")
        assert back.resume_work() is True
        await back.wait_idle()
    asyncio.run(go())
    assert "`PLAN.md`" in offline[-1][1] and "trust PLAN.md" in offline[-1][1]


def test_old_saved_sessions_without_hashes_are_not_flagged(wcfg, ws):
    s = Session(wcfg, session_id="old")
    s.main.log.add("user", "go", src="user")
    s.main.messages.append({"role": "user", "content": "go"})
    s.save()
    d =json.loads(s.path.read_text())
    for a in d["agents"]:
        a["extra"].pop("files", None)
        a["extra"].pop("files_saved", None)
    s.path.write_text(json.dumps(d))
    (ws.root / "PLAN.md").write_text(PLAN)
    back = Session.load(wcfg, "old")
    assert "changed while you were not looking" not in back.main.mode.resume_note()
    assert back.main.mode._changed_while_stopped() == []
