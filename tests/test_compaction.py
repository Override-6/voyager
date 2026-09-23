from conftest import run
from voyager.compaction import estimate_tokens, last_user_request, summary_message, transcript_text
from voyager.config import load_system_prompt

MSGS = [
    {"role": "user", "content": "fix the bug in calc.py"},
    {"role": "assistant", "content": [{"type": "text", "text": "reading"}, {"type": "tool_use", "id": "1", "name": "read_file", "input": {"path": "calc.py"}}]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "1", "content": "x" * 5000}]},
]


def test_transcript_clips_tool_results_and_has_no_system_prompt():
    t = transcript_text(MSGS)
    assert "USER: fix the bug" in t and "called read_file" in t and len(t) < 2500


def test_last_user_request_skips_tool_results():
    assert last_user_request(MSGS) == "fix the bug in calc.py"
    assert "fix the bug" in summary_message("done stuff", "fix the bug")["content"]


def test_pinned_text_is_always_marked_so_it_stays_out_of_summaries():
    from voyager.compaction import pinned_block
    assert pinned_block("<pinned-method>\nx\n</pinned-method>")["text"].startswith("<pinned-method>")  # already tagged
    msg = summary_message("s", "req", pinned="untagged rules")  # a mode pinning plain text
    assert msg["content"][0]["text"] == "<pinned>\nuntagged rules\n</pinned>"
    assert "untagged rules" not in transcript_text([msg]) and last_user_request([msg]).startswith("[The earlier")


class FakeStream:
    def __init__(self, sink, kw):
        sink.append(kw)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    @property
    def text_stream(self):
        async def gen():
            for part in ("1. request: fix ", "the bug\n5. next: run tests"):
                yield part
        return gen()


class FakeClient:
    def __init__(self):
        self.calls = []
        self.messages = self

    def stream(self, **kw):
        return FakeStream(self.calls, kw)


def test_compact_replaces_discussion_and_keeps_system_prompt(cfg):
    from voyager.session import Session
    client = FakeClient()
    s = Session(cfg, client=client)
    main = s.main
    main.messages = list(MSGS)
    assert run(main.compact()) is True
    assert len(main.messages) == 1 and "<summary>" in main.messages[0]["content"] and "fix the bug in calc.py" in main.messages[0]["content"]
    # the summarizer saw only the discussion, never the agent's system prompt...
    sent = client.calls[0]
    assert "read_file" in sent["messages"][0]["content"] and "Role" not in sent["messages"][0]["content"]
    # ...and the agent's real system prompt is still what every later request carries
    assert "main agent of an interactive coding CLI" in main.system_prompt()
    assert main.system_prompt() == load_system_prompt(cfg, "MAIN", agent_id="main", agent_name="main")


def test_auto_trigger_uses_context_window(cfg):
    from voyager.session import Session
    s = Session(cfg, client=FakeClient())
    s.main.messages = list(MSGS) * 4
    s.main._measured_tokens, s.main._measured_len = int(cfg.context_window * cfg.compact_at) + 1, len(s.main.messages)
    run(s.main._maybe_compact())
    assert len(s.main.messages) == 1  # over 70% of 65536 -> compacted
    s.main.messages = list(MSGS) * 4
    s.main._measured_tokens, s.main._measured_len = 1000, len(s.main.messages)
    run(s.main._maybe_compact())
    assert len(s.main.messages) == 12  # well under the threshold -> untouched
    assert cfg.context_window == 65536 and estimate_tokens("x" * 320) == 100
