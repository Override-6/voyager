"""A step whose output is unusable (cut off, or a tool call leaked as text) is redone, not taken as the end of the turn."""
import asyncio
import copy
from types import SimpleNamespace

from voyager.session import Session

LEAKED = SimpleNamespace(type="text", text="</parameter>\n</function>\n</tool_call>")
DONE = SimpleNamespace(type="text", text="all done")
CALL = SimpleNamespace(type="tool_use", id="tu1", name="read_file", input={"path": "x"})


class Step:
    """A stream with no live events: only the final message matters here."""

    def __init__(self, calls, kw, final):
        calls.append(copy.deepcopy(kw))
        self.final = final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        return self._events()

    async def _events(self):
        return
        yield

    async def get_final_message(self):
        return self.final


def scripted(*steps):
    """A model that answers each request with the next (content, stop_reason) of `steps`."""
    calls: list[dict] = []
    todo = list(steps)

    def stream(**kw):
        content, stop = todo.pop(0)
        usage = SimpleNamespace(input_tokens=10, output_tokens=2, cache_read_input_tokens=0)
        return Step(calls, kw, SimpleNamespace(usage=usage, content=content, stop_reason=stop))
    return SimpleNamespace(messages=SimpleNamespace(stream=stream)), calls


def run_turn(cfg, *steps):
    client, calls = scripted(*steps)
    s = Session(cfg, client=client)
    asyncio.run(s.main.run_turn("go"))
    return s, calls


def last_user_text(call):
    content = call["messages"][-1]["content"]
    return content if isinstance(content, str) else " ".join(b.get("text", "") for b in content)


def test_a_tool_call_leaked_as_text_is_redone(cfg):
    s, calls = run_turn(cfg, ([LEAKED], "end_turn"), ([DONE], "end_turn"))
    assert len(calls) == 2 and "as plain text" in last_user_text(calls[1])
    assert s.main.messages[-1] == {"role": "assistant", "content": [{"type": "text", "text": "all done"}]}
    assert not any("</tool_call>" in str(m["content"]) for m in s.main.messages)  # the broken output is dropped
    assert any(i.meta.get("event") == "repair" for i in s.main.log.items)


def test_a_response_cut_off_at_max_tokens_is_redone(cfg):
    s, calls = run_turn(cfg, ([CALL], "max_tokens"), ([DONE], "end_turn"))
    assert len(calls) == 2 and "cut off at the output limit" in last_user_text(calls[1])
    assert s.main.messages[-1]["content"][0]["text"] == "all done"


def test_repairs_are_bounded(cfg):
    s, calls = run_turn(cfg, *[([LEAKED], "end_turn")] * 5)
    assert len(calls) == 3  # the first try and two redos, then the turn ends
