"""LocalAgent: the agent that runs on the local model (the main agent and `local` sub-agents).

Loop: stream one model step (rendering thinking / text / tool-call arguments live) ->
run the tool calls -> feed results (plus any queued messages) back -> repeat until
the model stops calling tools.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

import anthropic

from .agent import Agent, AgentError
from .compactor import CompactionMixin
from .log import Item
from .tools import ToolContext, ToolError
from .tools.base import clip

if TYPE_CHECKING:
    from .session import Session


class LocalAgent(CompactionMixin, Agent):
    kind = "local"

    def __init__(
        self, session: "Session", agent_id: str, name: str, *, parent_id: str | None = None, prompt: str = ""
    ) -> None:
        super().__init__(session, agent_id, name, parent_id=parent_id, prompt=prompt)
        self.ctx = ToolContext(cwd=session.cfg.cwd, agent=self, session=session)  # for previews only
        self._measured_tokens: int | None = None  # prompt+output tokens the server reported for the last call
        self._measured_len = 0  # len(self.messages) that measurement covers
        self.mode = session.mode.for_agent(self)  # chat or voyager behaviour (see mode.py)
        self._compact_retry_at = 0  # see CompactionMixin
        self.compactions = 0  # how many times this agent's context was compacted: its current *round* (transcript.py)

    @property
    def tools(self) -> dict[str, Any]:
        return self.session.tools_for(self)  # dynamic: MCP servers add tools when they first connect

    # ------------------------------------------------------------------ turn
    def submit(self, text: str, *, src: str = "user", shown: str | None = None) -> None:
        self.mode.on_submit(src)
        super().submit(text, src=src, shown=shown)

    async def run_turn(self, text: str) -> None:
        await self.mode.turn_start()
        if text:
            self.messages.append(self._user_message(text))
            self.mode.on_message()
        await self._steps(manual_only=not text and self.compact_requested)
        await self.mode.turn_end(bool(text))

    async def _steps(self, manual_only: bool) -> None:
        while True:  # no step limit: a turn ends when the model stops calling tools, or when the user stops it
            await self._maybe_compact()
            if manual_only or not self.messages or self.messages[-1]["role"] != "user":
                return  # a bare /compact: nothing to answer
            assistant, tool_uses, items = await self._stream_step()
            if assistant:
                self.messages.append({"role": "assistant", "content": assistant})
            self._measured_len = len(self.messages)
            if not tool_uses:
                return
            results = await self._run_tools(tool_uses, items)
            # Messages that arrived mid-turn ride along with the tool results.
            results += [{"type": "text", "text": t} for t in self.take_inbox()]
            self.messages.append({"role": "user", "content": results})

    # ------------------------------------------------------------ model call
    async def _stream_step(self) -> tuple[list[dict[str, Any]], list[Any], list[Item]]:
        cfg, log = self.session.cfg, self.log
        kwargs: dict[str, Any] = dict(
            model=cfg.model,
            max_tokens=cfg.max_tokens,
            system=self.system_prompt(),
            messages=self.messages,
            tools=[t.schema() for t in self.tools.values()],
        )
        if cfg.thinking_budget > 0:
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": cfg.thinking_budget}

        blocks: dict[int, Item] = {}  # content block index -> its log item
        tool_items: list[Item] = []
        args_chars: dict[int, int] = {}
        t0 = time.monotonic()
        ok = False
        # The model reads its whole context before the first token: after a resume, or once the server's cache was
        # lost, that can take minutes with nothing streaming. Say so, instead of looking hung.
        ctx_tokens = self.context_estimate()
        self.activity = f"waiting for the model (~{ctx_tokens // 1000}k tokens of context)…"
        self.session.events.emit(self, "waiting", tokens=ctx_tokens)
        try:
            async with self.session.client.messages.stream(**kwargs) as stream:
                async for ev in stream:
                    if ev.type == "content_block_start":
                        cb = ev.content_block
                        if cb.type == "thinking":
                            blocks[ev.index] = log.start("thinking")
                            self.activity = "thinking…"
                        elif cb.type == "text":
                            blocks[ev.index] = log.start("text")
                            self.activity = "writing…"
                        elif cb.type == "tool_use":
                            item = log.start("tool", name=cb.name, status="args", args_chars=0, args={})
                            blocks[ev.index] = item
                            tool_items.append(item)
                            args_chars[ev.index] = 0
                            self.activity = f"{cb.name}…"
                    elif ev.type == "content_block_delta" and ev.index in blocks:
                        d, item = ev.delta, blocks[ev.index]
                        if d.type == "thinking_delta":
                            log.delta(item, d.thinking)
                        elif d.type == "text_delta":
                            log.delta(item, d.text)
                        elif d.type == "input_json_delta":
                            args_chars[ev.index] += len(d.partial_json)
                            log.update(item, args_chars=args_chars[ev.index])
                    elif ev.type == "content_block_stop" and ev.index in blocks:
                        if blocks[ev.index].kind != "tool":
                            log.end(blocks[ev.index])
                final = await stream.get_final_message()
            ok = True
        except anthropic.APIConnectionError:
            raise AgentError(
                f"cannot reach the model server at {cfg.base_url}. "
                "Start it (the `voyager` launcher script does) or pass --base-url."
            )
        except anthropic.APIStatusError as e:
            raise AgentError(f"server returned {e.status_code}: {e.message}")
        except ValueError as e:  # SDK could not parse a streamed tool-call JSON at all
            raise AgentError(f"model produced unparseable tool-call JSON ({e})")
        finally:
            if not ok:  # failed or cancelled mid-stream: don't leave items looking live
                self._close_open(blocks.values())

        u = final.usage
        cached = getattr(u, "cache_read_input_tokens", 0) or 0
        new_in = (u.input_tokens or 0) + (getattr(u, "cache_creation_input_tokens", 0) or 0)
        self._measured_tokens = new_in + cached + (u.output_tokens or 0)  # what the next request will carry
        log.add("usage", _usage_text(new_in, cached, u.output_tokens or 0, self._measured_tokens, cfg.context_window,
                                     time.monotonic() - t0), ctx=self._measured_tokens, window=cfg.context_window)

        # History keeps only text + tool_use: thinking is not resent (saves context on a local model).
        assistant: list[dict[str, Any]] = []
        tool_uses: list[Any] = []
        for b in final.content:
            if b.type == "text" and b.text.strip():
                assistant.append({"type": "text", "text": b.text})
                self.last_report = b.text
            elif b.type == "tool_use":
                assistant.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
                tool_uses.append(b)

        if final.stop_reason == "max_tokens":
            log.add("error", f"response hit max_tokens ({cfg.max_tokens}); output may be cut off")
            for it in tool_items:  # a tool call cut off mid-JSON must not run
                log.update(it, status="error", result="cut off by max_tokens")
            assistant = [b for b in assistant if b["type"] != "tool_use"]
            tool_uses, tool_items = [], []
        elif not assistant:
            log.add("notice", "(model returned no text)")
        return assistant, tool_uses, tool_items

    def _close_open(self, items: Any) -> None:
        for it in items:
            if it.kind == "tool":
                if it.meta.get("status") == "args":
                    self.log.update(it, status="error", result="interrupted")
            elif not it.meta.get("done"):
                self.log.end(it)

    def system_prompt(self) -> str:
        return self.mode.system_prompt()

    def extra_state(self) -> dict[str, Any]:
        return {**self.mode.extra_state(), "compactions": self.compactions}

    def load_extra_state(self, extra: dict[str, Any]) -> None:
        self.compactions = extra.get("compactions", 0)
        self.mode.load_extra_state(extra)

    # ------------------------------------------------------------- tool exec
    async def _run_tools(self, tool_uses: list[Any], items: list[Item]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        try:
            for tu, item in zip(tool_uses, items):
                text, is_error = await self._execute(tu, item)
                results.append(_result(tu.id, text, is_error))
        except asyncio.CancelledError:
            # Keep history valid: every tool_use needs a result, even the interrupted ones.
            for tu, item in list(zip(tool_uses, items))[len(results):]:
                self.log.update(item, status="error", result="interrupted by the user")
                results.append(_result(tu.id, "Interrupted by the user.", True))
            self.messages.append({"role": "user", "content": results})
            raise
        return results

    async def _execute(self, tu: Any, item: Item) -> tuple[str, bool]:
        log = self.log
        tool = self.tools.get(tu.name)
        args = tu.input if isinstance(tu.input, dict) else {}
        log.update(item, args=args)
        if tool is None:
            return self._fail(item, f"Unknown tool {tu.name!r}. Available tools: {', '.join(self.tools)}")
        run_args = {k: v for k, v in args.items() if k != "run_in_background"}
        background = bool(args.get("run_in_background")) and tool.backgroundable
        try:
            tool.check_args(run_args if isinstance(tu.input, dict) else tu.input)
        except ToolError as e:
            return self._fail(item, str(e))
        log.update(item, summary=tool.summary(run_args))
        self.tool_count += 1
        self.activity = f"{tool.name}({tool.summary(run_args)[:40]})"

        cfg = self.session.cfg
        try:  # diff / multi-line command shown on the call (informational: nothing waits on it)
            preview = tool.preview(run_args, self.ctx)
        except Exception:
            preview = None
        if preview:
            log.update(item, preview=preview, preview_kind="diff" if tool.name in ("edit_file", "write_file") else "text")

        log.update(item, status="running")
        # A fresh context per call: a backgrounded call keeps streaming into its own item after the step ends.
        ctx = ToolContext(cwd=cfg.cwd, emit=lambda line: log.tool_live(item, line), agent=self, session=self.session)
        try:
            out, backgrounded = await self.session.tasks.run(self, tool, run_args, ctx, item, background)
        except ToolError as e:
            return self._fail(item, str(e))
        except Exception as e:  # a bug in a tool must not kill the session (CancelledError passes through)
            return self._fail(item, f"{type(e).__name__}: {e}")
        out = clip(out)
        self.mode.record_tool(tu.name, args, out)
        if backgrounded:  # TaskManager already set status="background"; the item finishes when the task does
            log.update(item, result=out)
        else:
            log.update(item, status="done", result=out)
        return out, False

    def _fail(self, item: Item, msg: str) -> tuple[str, bool]:
        self.log.update(item, status="error", result=msg)
        return msg, True


def _result(tool_use_id: str, content: str, is_error: bool) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if is_error:
        block["is_error"] = True
    return block


def _usage_text(new_in: int, cached: int, out: int, ctx: int, window: int, seconds: float) -> str:
    rate = f" · {out / seconds:.1f} tok/s" if seconds > 0 and out else ""
    return f"↳ {new_in} in (+{cached} cached) · {out} out{rate} · ctx {ctx / 1000:.1f}k/{window // 1000}k"
