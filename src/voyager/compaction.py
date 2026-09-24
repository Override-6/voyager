"""Context compaction for local-model agents (the local model has a limited context window).

Only the *discussion* (`agent.messages`) is compacted. The system prompt (chat/MAIN.md, voyager/MISSION.md, LOCAL.md) is never
part of that history: it is re-read and sent as the `system` parameter on every request, so it survives
compaction untouched. The summarizer itself never sees it either.

A mode may also *pin* a block of text (voyager mode pins its method) in the first user message, where a small model
follows it best; `summary_message` re-pins it at the top after every compaction.

Trigger: estimated context >= context_window * compact_at (default 65536 * 0.70), checked before each model call.

Modes hook in (see mode.py): voyager mode adds a checkpoint before compaction and its own summary (voyager/compaction.py).
"""

from __future__ import annotations

import json
from typing import Any

from .config import Config, load_system_prompt
from .tools.base import clip

CHARS_PER_TOKEN = 3.2  # conservative for code-heavy text
TOOL_RESULT_CLIP = 1200
TOOL_ARGS_CLIP = 400
LAST_REQUEST_MAX_CHARS = 4000
PINNED_PREFIX = "<pinned"  # marks a pinned block (e.g. <pinned-method>): never summarized nor quoted as a user request


def compact_system(cfg: Config) -> str:
    """The summarizer's system prompt (system/COMPACT_SYSTEM.md). The instructions, the wrapper that carries the summary
    into the next context and the checkpoint are files too: system/{chat,voyager}/COMPACT.md, system/SUMMARY.md, ..."""
    return load_system_prompt(cfg, "COMPACT_SYSTEM").strip()


def estimate_tokens(obj: Any) -> int:
    return int(len(json.dumps(obj, ensure_ascii=False, default=str)) / CHARS_PER_TOKEN)


def is_pinned(block: Any) -> bool:
    return isinstance(block, dict) and block.get("type") == "text" and block["text"].startswith(PINNED_PREFIX)


def pinned_block(text: str) -> dict[str, Any]:
    """A text block marked as pinned (wrapped in <pinned> unless it already opens with a <pinned...> tag)."""
    return {"type": "text", "text": text if text.startswith(PINNED_PREFIX) else f"<pinned>\n{text}\n</pinned>"}


def _without_pinned(content: Any) -> Any:
    if isinstance(content, str):
        return content
    return [b for b in content if not is_pinned(b)]


def transcript_text(messages: list[dict[str, Any]]) -> str:
    """Flatten the discussion to plain text (tool results clipped) for the summarizer."""
    out: list[str] = []
    for m in messages:
        m = {**m, "content": _without_pinned(m["content"])}
        who = "USER" if m["role"] == "user" else "ASSISTANT"
        content = m["content"]
        if isinstance(content, str):
            out.append(f"{who}: {content}")
            continue
        for b in content:
            t = b.get("type")
            if t == "text":
                out.append(f"{who}: {b['text']}")
            elif t == "tool_use":
                args = clip(json.dumps(b.get("input", {}), ensure_ascii=False), TOOL_ARGS_CLIP)
                out.append(f"ASSISTANT called {b['name']}({args})")
            elif t == "tool_result":
                body = b.get("content")
                if isinstance(body, list):
                    body = "\n".join(str(x.get("text", "")) for x in body if isinstance(x, dict))
                flag = " [error]" if b.get("is_error") else ""
                out.append(f"TOOL RESULT{flag}: {clip(str(body), TOOL_RESULT_CLIP)}")
    return "\n\n".join(out)


def last_user_request(messages: list[dict[str, Any]]) -> str:
    """The most recent real user message (not a tool result), kept verbatim across compaction."""
    for m in reversed(messages):
        if m["role"] != "user":
            continue
        c = _without_pinned(m["content"])
        text = c if isinstance(c, str) else "\n\n".join(
            b["text"] for b in c if b.get("type") == "text" and not b["text"].startswith("<checkpoint>")
        )
        if text.strip() and not (isinstance(c, list) and any(b.get("type") == "tool_result" for b in c) and not text.strip()):
            return text if len(text) <= LAST_REQUEST_MAX_CHARS else text[:LAST_REQUEST_MAX_CHARS] + "…"
    return ""


def summary_message(cfg: Config, summary: str, last_request: str, note: str = "", pinned: str = "") -> dict[str, Any]:
    """The user message that replaces the discussion. `note`: a mode's addendum; `pinned`: text to re-pin at the top."""
    text = load_system_prompt(cfg, "SUMMARY", extra={"summary": summary.strip()}).strip() + (f"\n\n{note}" if note else "")
    if last_request:
        text += f"\n\nLatest user request (verbatim; it may already be partly done, see the summary):\n{last_request}"
    if pinned:
        return {"role": "user", "content": [pinned_block(pinned), {"type": "text", "text": text}]}
    return {"role": "user", "content": text}
