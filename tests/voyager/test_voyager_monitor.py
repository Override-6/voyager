"""Monitoring a voyager session: rounds and checkpoints as events, the mission summary in the digest."""
import copy
import io
import json

from conftest import run
from test_compaction import MSGS, FakeClient
from test_workspace import ws, wcfg  # noqa: F401  (fixtures)
from voyager import monitor
from voyager.session import Session
from voyager.voyager.monitor import summary


def test_checkpoint_compaction_and_round_are_events(wcfg, ws):
    s = Session(wcfg, client=FakeClient())
    m = s.main
    m.messages = [copy.deepcopy(x) for x in MSGS * 4]
    m._measured_tokens, m._measured_len = int(wcfg.context_window * 0.65), len(m.messages)
    run(m._maybe_compact())  # checkpoint
    m._measured_tokens = int(wcfg.context_window * 0.75)
    run(m._maybe_compact())  # compaction: round 0 -> 1
    rows = [json.loads(ln) for ln in (s.dir / "events.jsonl").read_text().splitlines()]
    evs = [r["ev"] for r in rows]
    assert evs.index("checkpoint") < evs.index("compact_start") < evs.index("compaction") < evs.index("round")
    comp = next(r for r in rows if r["ev"] == "compaction")
    assert comp["before"] > comp["after"] and comp["round"] == 1  # logged once the new round has started
    rnd = next(r for r in rows if r["ev"] == "round")
    assert rnd["round"] == 1 and "round 1 begins" in rnd["text"]
    assert json.loads((s.dir / "run.json").read_text())["workspace"] == "demo"
    out = io.StringIO()
    monitor.run_monitor(wcfg.sessions_dir, s.id, out=out)
    text = out.getvalue()
    assert "mode voyager · round 1" in text and "⚑ checkpoint requested" in text
    assert "⟳ compaction" in text and "━━ ◆ round 1 begins" in text
    assert "workspace demo" in text and "approach: NOT RECORDED" in text and "phase 0" in text


def test_summary_reads_the_workspace(ws):
    (ws.root / "PLAN.md").write_text(
        "# Plan\n## Phases\n- [x] 1. Recon — exit: a\n- [>] 2. Map — exit: b\n## Current phase\nx\n"
        "## Now\n- [ ] one\n- [ ] two\n- [ ] three\n- [ ] four\n- [ ] five\n## Blocked\n- need a key\n## Log\n")
    text = "\n".join(summary(ws))
    assert "plan: 1/2 phases done · current: 2. Map" in text and "  now: - [ ] one" in text and "… 1 more" in text
    assert "BLOCKED: - need a key" in text and "approach: NOT RECORDED" in text and "0 searches" in text
    assert "0 tools, 0 notes" in text and "workspace created" in text  # the first commit
