"""The workspace method (METHOD.md) is pinned in the first user message and re-pinned after every compaction."""
import copy
import dataclasses

from conftest import run
from test_compaction import MSGS, FakeClient
from test_workspace import ws, wcfg  # noqa: F401  (fixtures)
from voyager.local import LocalAgent
from voyager.compaction import last_user_request, transcript_text
from voyager.session import Session


def test_method_is_not_in_the_system_prompt_but_in_the_first_message(wcfg):
    s = Session(wcfg)
    assert "# Phase 0: frame" not in s.main.system_prompt() and "first message" in s.main.system_prompt()
    m = s.main
    first = m._user_message("beat the ender dragon")
    assert first["content"][0]["text"].startswith("<pinned-method>") and "three horizons" in first["content"][0]["text"]
    assert first["content"][1]["text"] == "beat the ender dragon" and "{{" not in first["content"][0]["text"]
    m.messages.append(first)
    assert m._user_message("also: no mods") == {"role": "user", "content": "also: no mods"}  # pinned once
    assert LocalAgent(s, "a1", "w", parent_id="main").mode.pinned() == ""


def test_pinned_method_is_hidden_from_summary_and_last_request(wcfg):
    m = Session(wcfg).main
    msg = m._user_message("beat the ender dragon")
    assert last_user_request([msg]) == "beat the ender dragon"
    assert "pinned-method" not in transcript_text([msg]) and "USER: beat the ender dragon" in transcript_text([msg])


def test_method_survives_compaction(wcfg):
    client = FakeClient()
    m = Session(wcfg, client=client).main
    m.messages = [m._user_message("beat the ender dragon")] + [copy.deepcopy(x) for x in MSGS * 4]
    m.compact_requested = True
    run(m.compact())
    assert len(m.messages) == 1
    blocks = m.messages[0]["content"]
    assert blocks[0]["text"].startswith("<pinned-method>") and "trust PLAN.md" in blocks[1]["text"]
    assert "pinned-method" not in str(client.calls[0]["messages"])  # the summarizer never sees it
    m.compact_requested = True
    run(m.compact())
    assert sum("<pinned-method>" in str(b) for b in m.messages[0]["content"]) == 1  # not duplicated


def test_persona_opens_both_the_system_prompt_and_the_pinned_method(wcfg):
    m = Session(wcfg).main
    assert m.system_prompt().startswith("# Persona") and "{{" not in m.system_prompt()
    method = m.mode.pinned()
    assert method.startswith("<pinned-method>\n# Persona") and "{{" not in method
    plain = Session(dataclasses.replace(wcfg, cwd=wcfg.cwd.parent)).main.system_prompt()
    assert "# Persona" not in plain  # chat mode (chat/MAIN.md) is unchanged


def test_research_rules_are_pinned_and_checkpoint_asks_for_sources(wcfg):
    m = Session(wcfg).main
    method = m.mode.pinned()
    assert "Orient and look it up" in method and "knowledge/sources.md" in method
    assert "Never work from memory on something with a spec" in method and "Two failures means research" in method
    assert "Prior art first, every time" in method
    assert "sources.md" in m.mode._prompt("voyager/CHECKPOINT", round="0")
    assert "hypothesis" in m.mode.summary_note
