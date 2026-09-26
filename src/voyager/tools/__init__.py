"""Tool registry. Add a new tool: subclass Tool, instantiate it in BASE_TOOLS (or AGENT_TOOLS)."""

from __future__ import annotations

from .agents import AGENT_TOOLS
from .base import Tool, ToolContext, ToolError
from .bash import Bash
from .files import EditFile, ReadFile, WriteFile
from .repl import ReplTool
from .search import Glob, Grep
from .tasks import KillTask, Tasks
from .web import WebFetch, WebSearch

BASE_TOOLS: list[Tool] = [Bash(), ReplTool(), ReadFile(), WriteFile(), EditFile(), Glob(), Grep(), Tasks(), KillTask(), WebSearch(),
                          WebFetch()]


def tools_for(is_main: bool) -> dict[str, Tool]:
    """Sub-agents get the base tools only (no spawning: agents don't nest)."""
    return {t.name: t for t in (BASE_TOOLS + AGENT_TOOLS if is_main else BASE_TOOLS)}


__all__ = ["BASE_TOOLS", "AGENT_TOOLS", "Tool", "ToolContext", "ToolError", "tools_for"]
