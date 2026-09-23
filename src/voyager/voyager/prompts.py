"""Voyager mode's prompts: the persona and the method (system/voyager/), filled in for a workspace."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..config import Config, load_system_prompt

if TYPE_CHECKING:
    from .workspace import Workspace


def persona(cfg: Config) -> str:
    path = cfg.system_dir / "voyager" / "PERSONA.md"
    return path.read_text(encoding="utf-8").strip() + "\n\n" if path.is_file() else ""


def mission_prompt(cfg: Config, ws: "Workspace", state: str, *, main: bool, agent_id: str, agent_name: str) -> str:
    """The system prompt: MISSION.md (opening with the persona) for the main agent, LOCAL.md for sub-agents."""
    extra = {"ws_name": ws.name, "workspace_state": state, "persona": persona(cfg)}
    return load_system_prompt(cfg, "MISSION" if main else "LOCAL", agent_id=agent_id, agent_name=agent_name, extra=extra)


def method_prompt(cfg: Config, ws: "Workspace", *, agent_id: str, agent_name: str) -> str:
    """METHOD.md, to be pinned in the first user message of the main agent."""
    extra = {"ws_name": ws.name, "persona": persona(cfg)}
    return load_system_prompt(cfg, "METHOD", agent_id=agent_id, agent_name=agent_name, extra=extra).strip()
