"""Chat mode: plain coding / chat. The default; everything the core does with no mode-specific behaviour."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..compaction import COMPACT_INSTRUCTIONS
from ..config import load_system_prompt
from ..mode import AgentMode, Mode

if TYPE_CHECKING:
    from ..agent import Agent


class ChatAgentMode(AgentMode):
    name = "chat"
    compact_instructions = COMPACT_INSTRUCTIONS

    @property
    def prompt_name(self) -> str:  # type: ignore[override]
        return "MAIN" if self.agent.is_main else "LOCAL"

    def system_prompt(self) -> str:
        a = self.agent
        return load_system_prompt(a.session.cfg, self.prompt_name, agent_id=a.id, agent_name=a.name)


class ChatMode(Mode):
    name = "chat"

    def for_agent(self, agent: "Agent") -> AgentMode:
        return ChatAgentMode(agent)

    def status(self, agent: "Agent") -> str:
        return "plain coding / chat · /voyager <name> [objective] starts a long-running mission"
