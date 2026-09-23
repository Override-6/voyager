"""Chat mode: plain coding / chat. No workspace, no plan, no pinned method; MAIN.md / LOCAL.md."""

from conftest import run
from test_compaction import MSGS, FakeClient
from voyager.local import LocalAgent
from voyager.chat import ChatMode
from voyager.session import Session


def test_chat_is_the_default_mode_and_is_plain(cfg):
    s = Session(cfg)
    assert isinstance(s.mode, ChatMode) and s.workspace is None and s.mode.name == "chat"
    assert "save_tool" not in s.tools_for(s.main) and "search_workspace" not in s.tools_for(s.main)
    assert "main agent of an interactive coding CLI" in s.main.system_prompt() and "{{" not in s.main.system_prompt()
    sub = LocalAgent(s, "a1", "w", parent_id="main")
    assert "worker agent" in sub.system_prompt() and "{{" not in sub.system_prompt()


def test_chat_has_no_pinned_text_no_extra_state_and_no_checkpoint(cfg):
    client = FakeClient()
    m = Session(cfg, client=client).main
    assert m.mode.pinned() == "" and m.mode.extra_state() == {}
    assert m._user_message("hi") == {"role": "user", "content": "hi"}
    m.messages = [dict(x) for x in MSGS * 4]
    m._measured_tokens, m._measured_len = int(cfg.context_window * 0.65), len(m.messages)
    run(m._maybe_compact())  # voyager would checkpoint here; chat neither checkpoints nor compacts below compact_at
    assert not client.calls and len(m.messages) == 12
    m._measured_tokens = int(cfg.context_window * 0.75)
    run(m._maybe_compact())
    assert len(m.messages) == 1 and "trust PLAN.md" not in m.messages[0]["content"]
    assert "MAIN.md re-applied" in m.log.items[-1].text


def test_system_prompt_files_are_laid_out_by_mode(cfg):
    d = cfg.system_dir
    assert (d / "chat" / "MAIN.md").is_file() and (d / "LOCAL.md").is_file() and (d / "CODER.md").is_file()
    assert {p.name for p in (d / "voyager").iterdir()} == {"MISSION.md", "METHOD.md", "PERSONA.md"}
