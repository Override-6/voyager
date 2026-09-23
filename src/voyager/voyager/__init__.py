"""Voyager mode: a long-running mission in a workspace (the agent keeps its own tools/, knowledge/ and PLAN.md).

Files: workspace.py (the folder + git), state.py (the state block in the system prompt, lint, search), approach.py +
evidence.py (the approach gate), compaction.py + nudge.py (checkpoints, rounds, keep-going), agent_mode.py (per-agent
behaviour), tools.py (save_tool, search_workspace), commands.py (/voyager), prompts.py. Prompts: system/voyager/.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..mode import AgentMode, Mode
from .agent_mode import VoyagerAgentMode
from .tools import WORKSPACE_TOOLS
from .workspace import Workspace

if TYPE_CHECKING:
    from ..agent import Agent
    from ..tools import Tool


class VoyagerMode(Mode):
    name = "voyager"

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def for_agent(self, agent: "Agent") -> AgentMode:
        return VoyagerAgentMode(agent, self.workspace)

    def tools(self) -> "dict[str, Tool]":
        return {t.name: t for t in WORKSPACE_TOOLS}

    def status(self, agent: "Agent") -> str:
        mode = getattr(agent, "mode", None)
        return f"{self.workspace.name} · {self.workspace.current_phase()} · round {getattr(mode, 'round', 0)} · /chat leaves"
