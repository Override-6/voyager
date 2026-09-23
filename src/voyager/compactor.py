"""Auto-compaction for local-model agents (mixed into LocalAgent). Design notes: see compaction.py."""

from __future__ import annotations

from typing import Any

import anthropic

from .compaction import (
    COMPACT_SYSTEM, estimate_tokens, last_user_request, pinned_block, summary_message, transcript_text,
)


class CompactionMixin:
    # Provided by LocalAgent / Agent
    session: Any
    log: Any
    messages: list[dict[str, Any]]
    tools: dict[str, Any]
    is_main: bool
    activity: str
    compact_requested: bool
    _measured_tokens: int | None
    _measured_len: int
    mode: Any  # AgentMode: what differs between chat and voyager mode (see mode.py)
    compactions: int  # this agent's current round
    name: str

    def system_prompt(self) -> str:
        raise NotImplementedError

    def _user_message(self, text: str) -> dict[str, Any]:
        """A new user message; the first one of a conversation carries the mode's pinned text (if any) above it."""
        pinned = self.mode.pinned() if not self.messages else ""
        if not pinned:
            return {"role": "user", "content": text}
        return {"role": "user", "content": [pinned_block(pinned), {"type": "text", "text": text}]}

    def overhead_tokens(self) -> int:
        """System prompt + tool schemas: sent on every request, never compacted."""
        return estimate_tokens(self.system_prompt()) + estimate_tokens([t.schema() for t in self.tools.values()])

    def context_estimate(self) -> int:
        if self._measured_tokens is None:
            return self.overhead_tokens() + estimate_tokens(self.messages)
        return self._measured_tokens + estimate_tokens(self.messages[self._measured_len:])

    async def _maybe_compact(self) -> None:
        cfg = self.session.cfg
        limit = int(cfg.context_window * cfg.compact_at)
        estimate = self.context_estimate()
        worth_it = len(self.messages) >= 2 and estimate_tokens(self.messages) >= 1000  # guards tiny windows against thrash
        if self.mode.defer_compaction(estimate, worth_it):
            return  # e.g. voyager mode: let the agent save its state before its context is summarized
        if self.compact_requested or (worth_it and estimate >= limit):
            await self.compact()

    async def compact(self) -> bool:
        """Summarize the discussion (messages only) and replace it with the summary.

        The system prompt is not part of `messages`: it is re-read from system/*.md and sent with every
        request, so it is re-applied automatically after compaction.
        """
        cfg, log = self.session.cfg, self.log
        manual, self.compact_requested = self.compact_requested, False
        if len(self.messages) < (1 if manual else 2):
            return False
        before = self.context_estimate()
        old_messages, old_prompt = self.messages, self.system_prompt()  # what this round ran on, kept for the archive
        item = log.start("compact", before=before, after=0, manual=manual)
        self.activity = "compacting context…"
        summary = ""
        try:
            prompt = f"<conversation>\n{transcript_text(self.messages)}\n</conversation>\n\n{self.mode.compact_instructions}"
            async with self.session.client.messages.stream(
                model=cfg.model, max_tokens=cfg.compact_max_tokens, system=COMPACT_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                async for text in stream.text_stream:
                    log.delta(item, text)
                    summary += text
        except anthropic.APIError as e:  # summarizing failed: keep the history as it is and carry on
            log.add("error", f"compaction failed ({e}); continuing without it")
            return False
        finally:
            log.end(item)
        if not summary.strip():
            log.add("error", "compaction produced an empty summary; history kept")
            return False
        self.messages = [summary_message(summary, last_user_request(self.messages), self.mode.summary_note, self.mode.pinned())]
        self._measured_tokens, self._measured_len = None, 0
        await self.mode.after_compact()
        after = self.context_estimate()  # summary + system prompt + tools
        log.update(item, after=after)
        self.session.transcript.archive_round(  # the round that just ended, complete, before its messages are forgotten
            self, old_messages, summary, before=before, after=after, manual=manual, system_prompt=old_prompt,
            tools=list(self.tools), info=self.mode.round_info())
        self.compactions += 1  # a new round starts
        log.add("notice", f"⟳ context compacted: ~{before // 1000}k → ~{after // 1000}k tokens "
                          f"(system prompt {self.mode.prompt_name}.md re-applied)")
        self.mode.on_compacted()
        return True
