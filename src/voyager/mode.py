"""Modes: how a conversation behaves. There are two, one package each:

* `chat/`    plain coding / chat (the default): no workspace, no plan, no method.
* `voyager/` a long-running mission in a workspace: persona and method, three-horizon plan, approach gate,
             checkpoints and rounds, keep-going nudges.

A session is in voyager mode iff its cwd is inside a workspace (nothing else is persisted for it), so `mode_for` can
recover the mode of a resumed conversation. A `Mode` is per session; each local-model agent gets its own `AgentMode`
(per-agent state: round, checkpoint flag, nudge budget ...). The base classes here do nothing special on purpose:
core code (local.py, compactor.py, session.py) calls these hooks and never asks which mode it is in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .agent import Agent
    from .config import Config
    from .tools import Tool
    from .voyager.workspace import Workspace


class AgentMode:
    """Per-agent hooks. Every default is a no-op; a mode overrides what it needs."""

    name = "chat"
    prompt_name = "MAIN"  # the system prompt file (for the "re-applied" notice)
    compact_instructions = ""  # what the summarizer is asked for (a mode sets it)
    summary_note = ""  # appended to the summary message that replaces the discussion
    resume_note = ""  # added to the message that sends a resumed session back to work

    def __init__(self, agent: "Agent") -> None:
        self.agent = agent

    def system_prompt(self) -> str:
        raise NotImplementedError

    def pinned(self) -> str:
        """Text pinned at the top of the first user message (and re-pinned after each compaction); "" for none."""
        return ""

    def extra_state(self) -> dict[str, Any]:
        return {}

    def load_extra_state(self, extra: dict[str, Any]) -> None:
        pass

    def wants_continue(self) -> bool:
        """A resumed session whose last turn ended normally still has work to do (e.g. an unfinished plan)."""
        return False

    def on_submit(self, src: str) -> None:
        """A message is queued for the agent (src: "user" or "agent")."""

    def on_message(self) -> None:
        """A message is about to be appended to the discussion."""

    def defer_compaction(self, estimate: int, worth_it: bool) -> bool:
        """Called before every model call; True: hold the compaction back this time (e.g. a checkpoint is pending)."""
        return False

    async def after_compact(self) -> None:
        """The discussion was just replaced by its summary."""

    def round_info(self) -> dict[str, Any]:
        """Extra facts about the round that just ended, stored in its archive (transcript.py)."""
        return {}

    def on_compacted(self) -> None:
        """The compaction has been fully logged (after_compact and the "context compacted" notice came first)."""

    async def turn_start(self) -> None:
        pass

    async def turn_end(self, had_message: bool) -> None:
        pass

    def record_tool(self, name: str, args: dict[str, Any], output: str) -> None:
        """A tool call of this agent succeeded."""


class Mode:
    """Per-session mode."""

    name = "chat"
    workspace: "Workspace | None" = None

    def for_agent(self, agent: "Agent") -> AgentMode:
        raise NotImplementedError

    def tools(self) -> "dict[str, Tool]":
        """Tools this mode adds to every agent's."""
        return {}

    def status(self, agent: "Agent") -> str:
        """Detail shown on the mode line under the chat bar."""
        return ""


def mode_for(cfg: "Config") -> Mode:
    from .chat import ChatMode  # imported here: both packages import this module
    from .voyager import VoyagerMode
    from .voyager.workspace import Workspace

    ws = Workspace.at(cfg.cwd, cfg.workspaces_dir)
    return VoyagerMode(ws) if ws is not None else ChatMode()
