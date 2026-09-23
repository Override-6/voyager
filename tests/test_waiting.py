"""While the model reads its context (no token yet), the agent says so: activity in the UI, an event for monitors."""
import asyncio
import json
import os
import time
from types import SimpleNamespace

from voyager import monitor
from voyager.session import Session


class SlowStream:
    """A model that is still reading its context: no event arrives until `release` is set."""

    def __init__(self, release):
        self.release = release

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        return self._events()

    async def _events(self):
        await self.release.wait()
        return
        yield  # an (empty) async generator

    async def get_final_message(self):
        usage = SimpleNamespace(input_tokens=10, output_tokens=2, cache_read_input_tokens=0)
        return SimpleNamespace(usage=usage, content=[SimpleNamespace(type="text", text="hi")], stop_reason="end_turn")


def test_the_agent_says_it_is_waiting_for_the_model(cfg):
    async def go():
        release = asyncio.Event()
        client = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: SlowStream(release)))
        s = Session(cfg, client=client)
        s.main.messages = [{"role": "user", "content": "x" * 30_000}]  # a long conversation to read
        step = asyncio.create_task(s.main._stream_step())
        await asyncio.sleep(0.05)
        assert s.main.activity.startswith("waiting for the model (~") and "tokens of context" in s.main.activity
        rows = [json.loads(ln) for ln in (s.dir / "events.jsonl").read_text().splitlines()]
        assert rows[-1]["ev"] == "waiting" and rows[-1]["tokens"] > 5000
        state = monitor.state_line({"pid": os.getpid()}, rows, time.time())
        assert state.startswith("RUNNING") and "waiting for the model to start answering" in state
        assert "waiting for the model" in monitor.render(rows[-1], show_all=True) and monitor.render(rows[-1]) is None
        release.set()
        assistant, tool_uses, _ = await step
        assert assistant == [{"type": "text", "text": "hi"}] and tool_uses == []
    asyncio.run(go())
