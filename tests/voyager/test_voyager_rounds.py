"""A voyager round archive also says where the mission stood: phase, workspace commit, PLAN.md."""
import copy
import json

from conftest import run
from test_compaction import MSGS, FakeClient
from test_workspace import ws, wcfg  # noqa: F401  (fixtures)
from voyager.session import Session


def test_round_archive_records_the_mission_state(wcfg, ws):
    (ws.root / "PLAN.md").write_text("# Plan\n## Phases\n- [>] 1. Recon — exit: x\n## Now\n- [ ] look around\n")
    s = Session(wcfg, client=FakeClient())
    m = s.main
    m.messages = [copy.deepcopy(x) for x in MSGS * 4]
    m._measured_tokens, m._measured_len = int(wcfg.context_window * 0.75), len(m.messages)
    run(m._maybe_compact())  # checkpoint, then (over compact_at) the compaction in the same call sequence
    m._measured_tokens = int(wcfg.context_window * 0.75)
    run(m._maybe_compact())
    arch = json.loads((s.dir / "rounds" / "main-r000.json").read_text())
    info = arch["info"]
    assert info["phase"] == "1. Recon" and "look around" in info["plan"] and len(info["commit"]) == 40
    assert "workspace **demo**" in arch["system_prompt"] and "save_tool" in arch["tools"]
    assert m.compactions == 1 and m.mode.round == 1  # rounds and compactions count the same thing
