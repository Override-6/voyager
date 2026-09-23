"""CoderAgent: a Claude Sonnet agent driven ONLY through the `claude -p` CLI (never the API).

Each turn is one `claude -p --output-format stream-json` subprocess. Its JSONL events are
mapped onto the same log items the local agent produces, so the TUI renders both alike.
Turns after the first pass `--resume <session-id>`, which is what makes a Coder resumable.
Claude runs with full tool access (`bypassPermissions`) inside a per-agent scratch directory.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .agent import Agent, AgentError
from .config import CLAUDE_ENV_SCRUB, load_system_prompt
from .log import Item

if TYPE_CHECKING:
    from .session import Session

SUMMARY_KEYS = ("command", "file_path", "pattern", "url", "query", "description", "path")


class CoderAgent(Agent):
    kind = "coder"

    def __init__(
        self, session: "Session", agent_id: str, name: str, *, parent_id: str | None = None, prompt: str = ""
    ) -> None:
        super().__init__(session, agent_id, name, parent_id=parent_id, prompt=prompt)
        self.claude_session = str(uuid.uuid4())
        self.claude_started = False  # True once claude created the session (so --resume is valid)
        cfg = session.cfg
        self.sandbox: Path = cfg.coder_cwd or (session.dir / "coder" / agent_id)

    # ------------------------------------------------------------------ turn
    async def run_turn(self, text: str) -> None:
        cfg = self.session.cfg
        self.sandbox.mkdir(parents=True, exist_ok=True)
        argv = [
            cfg.claude_bin, "-p",
            "--output-format", "stream-json", "--include-partial-messages", "--verbose",
            "--model", cfg.coder_model,
            "--permission-mode", "bypassPermissions",
            "--system-prompt", load_system_prompt(cfg, "CODER", agent_id=self.id, agent_name=self.name),
            "--resume" if self.claude_started else "--session-id", self.claude_session,
        ]
        env = {k: v for k, v in os.environ.items() if k not in CLAUDE_ENV_SCRUB}
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=self.sandbox, env=env, limit=2**24,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            raise AgentError(f"`{cfg.claude_bin}` not found; install Claude Code or set VOYAGER_CLAUDE_BIN")
        assert proc.stdin and proc.stdout and proc.stderr
        stderr_task = asyncio.create_task(proc.stderr.read())
        state = _Stream()
        try:
            proc.stdin.write(text.encode())  # prompt via stdin: no argv length / leading-dash issues
            await proc.stdin.drain()
            proc.stdin.close()
            async for raw in proc.stdout:
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                self._on_event(ev, state)
            await proc.wait()
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            stderr = (await stderr_task).decode(errors="replace").strip()
            self._close_open(state)
        if state.error:
            raise AgentError(f"coder failed: {state.error}")
        if proc.returncode != 0 and not state.finished:
            raise AgentError(f"claude exited with {proc.returncode}: {stderr[-500:] or 'no output'}")

    # ---------------------------------------------------------------- events
    def _on_event(self, ev: dict[str, Any], st: "_Stream") -> None:
        log = self.log
        kind = ev.get("type")
        if kind == "system" and ev.get("subtype") == "init":
            self.claude_started = True
        elif kind == "stream_event" and not ev.get("parent_tool_use_id"):
            self._on_stream_event(ev["event"], st)
        elif kind == "user":  # tool results produced by claude's own tool execution
            content = ev.get("message", {}).get("content")
            for block in content if isinstance(content, list) else []:
                item = st.tools.get(block.get("tool_use_id", ""))
                if block.get("type") == "tool_result" and item:
                    failed = bool(block.get("is_error"))
                    log.update(item, status="error" if failed else "done", result=_flatten(block.get("content")))
        elif kind == "result":
            st.finished = True
            self.last_report = ev.get("result") or self.last_report
            if ev.get("is_error"):
                st.error = str(ev.get("result") or ev.get("subtype"))
            u = ev.get("usage", {})
            log.add(
                "usage",
                f"↳ ${ev.get('total_cost_usd', 0):.4f} · {u.get('input_tokens', 0)} in · "
                f"{u.get('output_tokens', 0)} out · {ev.get('duration_ms', 0) / 1000:.1f}s",
            )

    def _on_stream_event(self, e: dict[str, Any], st: "_Stream") -> None:
        log, etype = self.log, e.get("type")
        idx = e.get("index")
        if etype == "content_block_start":
            cb = e["content_block"]
            if cb["type"] == "thinking":
                st.blocks[idx] = log.start("thinking")
                self.activity = "thinking…"
            elif cb["type"] == "text":
                st.blocks[idx] = log.start("text")
                self.activity = "writing…"
            elif cb["type"] == "tool_use":
                item = log.start("tool", name=cb["name"], status="args", args_chars=0, args={})
                st.blocks[idx] = item
                st.tools[cb["id"]] = item
                st.json[idx] = ""
                st.tool_ids[idx] = cb["id"]
                self.activity = f"{cb['name']}…"
        elif etype == "content_block_delta" and idx in st.blocks:
            d, item = e["delta"], st.blocks[idx]
            if d["type"] == "thinking_delta":
                log.delta(item, d["thinking"])
            elif d["type"] == "text_delta":
                log.delta(item, d["text"])
            elif d["type"] == "input_json_delta":
                st.json[idx] += d["partial_json"]
                log.update(item, args_chars=len(st.json[idx]))
        elif etype == "content_block_stop" and idx in st.blocks:
            item = st.blocks[idx]
            if item.kind != "tool":
                log.end(item)
                return
            try:
                args = json.loads(st.json.get(idx) or "{}")
            except json.JSONDecodeError:
                args = {"_raw": st.json.get(idx, "")}
            self.tool_count += 1
            summary = next((str(args[k]) for k in SUMMARY_KEYS if k in args), "")
            log.update(item, args=args, summary=summary.splitlines()[0][:200] if summary else "", status="running")
            self.activity = f"{item.meta['name']}({summary[:40]})"

    def _close_open(self, st: "_Stream") -> None:
        for it in st.blocks.values():
            if it.kind == "tool":
                if it.meta.get("status") in ("args", "running"):
                    self.log.update(it, status="error", result="interrupted")
            elif not it.meta.get("done"):
                self.log.end(it)

    # ---------------------------------------------------------- session bits
    def report_extra(self) -> str:
        return f"Files it created are in its scratch directory: {self.sandbox}"

    def extra_state(self) -> dict[str, Any]:
        return {"claude_session": self.claude_session, "claude_started": self.claude_started, "sandbox": str(self.sandbox)}

    def load_extra_state(self, extra: dict[str, Any]) -> None:
        self.claude_session = extra.get("claude_session", self.claude_session)
        self.claude_started = extra.get("claude_started", False)
        if extra.get("sandbox"):
            self.sandbox = Path(extra["sandbox"])


class _Stream:
    """Per-turn parsing state."""

    def __init__(self) -> None:
        self.blocks: dict[int, Item] = {}
        self.tools: dict[str, Item] = {}  # tool_use id -> item
        self.tool_ids: dict[int, str] = {}
        self.json: dict[int, str] = {}
        self.finished = False
        self.error = ""


def _flatten(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) if isinstance(b, dict) else str(b) for b in content)
    return "" if content is None else str(content)
