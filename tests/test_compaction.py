import copy

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


def test_last_user_request_skips_tool_results(cfg):
    assert last_user_request(MSGS) == "fix the bug in calc.py"
    assert "fix the bug" in summary_message(cfg, "done stuff", "fix the bug")["content"]


def test_pinned_text_is_always_marked_so_it_stays_out_of_summaries(cfg):
    from voyager.compaction import pinned_block
    assert pinned_block("<pinned-method>\nx\n</pinned-method>")["text"].startswith("<pinned-method>")  # already tagged
    msg = summary_message(cfg, "s", "req", pinned="untagged rules")  # a mode pinning plain text
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


class ThinkingOnlyClient(FakeClient):
    """A model that spends its whole output budget thinking: the stream carries no summary text at all."""

    def stream(self, **kw):
        stream = FakeStream(self.calls, kw)
        stream.__class__ = type("Silent", (FakeStream,), {"text_stream": property(lambda self: _nothing())})
        return stream


async def _nothing():
    return
    yield


def test_the_summary_request_turns_thinking_off(cfg):
    from voyager.session import Session
    client = FakeClient()
    s = Session(cfg, client=client)
    s.main.messages = list(MSGS)
    run(s.main.compact())
    # llama-server ignores `thinking: disabled`; without its template switch the model thinks away the whole budget
    assert client.calls[0]["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}


def test_the_summary_may_use_all_the_room_the_context_window_leaves(cfg):
    import dataclasses
    from voyager.session import Session
    client = FakeClient()
    s = Session(cfg, client=client)
    s.main.messages = list(MSGS)
    run(s.main.compact())
    sent = client.calls[0]
    used = estimate_tokens(sent["messages"][0]["content"]) + estimate_tokens(sent["system"])
    assert sent["max_tokens"] > 60_000 and used + sent["max_tokens"] < cfg.context_window  # nearly the whole window, still inside it
    fixed = Session(dataclasses.replace(cfg, compact_max_tokens=3000), client=(c2 := FakeClient()))
    fixed.main.messages = list(MSGS)
    run(fixed.main.compact())
    assert c2.calls[0]["max_tokens"] == 3000  # an explicit setting wins


def test_an_empty_summary_keeps_the_history_and_is_not_retried_at_every_step(cfg):
    from voyager.session import Session
    client = ThinkingOnlyClient()
    s = Session(cfg, client=client)
    m = s.main
    m.messages = [copy.deepcopy(x) for x in MSGS * 4]
    over = int(cfg.context_window * cfg.compact_at) + 1
    m._measured_tokens, m._measured_len = over, len(m.messages)
    run(m._maybe_compact())
    assert len(client.calls) == 1 and len(m.messages) == 12  # failed: the history is untouched
    assert any("empty summary" in i.text for i in m.log.items if i.kind == "error")
    run(m._maybe_compact())
    assert len(client.calls) == 1  # the very next step does not redo (minutes of) work that just failed
    m._measured_tokens = over + 9000  # the context kept growing
    run(m._maybe_compact())
    assert len(client.calls) == 2  # ... and then it tries again
    m._measured_tokens = over
    m.compact_requested = True
    run(m._maybe_compact())
    assert len(client.calls) == 3  # a manual /compact always goes through


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
