"""Voyager mode commands: /voyager (enter, create, list) and its long form /workspace."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..chat.commands import chat_command
from ..sessions_store import ago
from .workspace import Workspace

if TYPE_CHECKING:
    from ..tui.controller import Controller


def _listing(ctl: "Controller") -> str:
    """One row per workspace (the current one starred), or a hint when there are none."""
    cfg, current = ctl.session.cfg, ctl.session.workspace_name  # not `.workspace`: a daemon replica has only the name
    rows = [f"{'*' if w.name == current else ' '} {w.name:<24}{ago(w.updated):<10}{w.objective_line()[:70]}"
            for w in Workspace.list_all(cfg.workspaces_dir)]
    return "\n".join(rows) if rows else f"no workspaces yet in {cfg.workspaces_dir}"


def voyager_command(ctl: "Controller", rest: str) -> None:
    """/voyager <name> [objective]: open workspace <name> (create it when new). Given an objective, start working on it."""
    cfg = ctl.session.cfg
    name, _, objective = rest.strip().partition(" ")
    objective = objective.strip()
    if not name:
        ctl.focused.log.add("notice", "voyager mode: /voyager <name> [objective] (the objective starts the mission; /chat leaves)\n"
                            + _listing(ctl))
    elif name in ("off", "none"):
        chat_command(ctl)
    elif name == "new":
        ctl.say("usage: /voyager <name> [objective]  (no `new`: a workspace is created when the name is free)", 6)
    elif (ws := Workspace.get(cfg.workspaces_dir, name)) is not None:
        note = " · objective ignored: it is already set (OBJECTIVE.md); tell the agent in the chat" if objective else ""
        ctl.switch_to(ws.root, f"voyager · workspace {ws.name} · {ws.current_phase()} · /resume lists its conversations{note}")
    else:
        try:
            ws = Workspace.create(cfg.workspaces_dir, name, objective)
        except (ValueError, OSError) as e:
            ctl.say(str(e), 6)
            return
        ctl.switch_to(ws.root, f"voyager · workspace {ws.name} created" + ("" if objective else " · state the objective to start"),
                      first_message=objective or None)


def workspace_command(ctl: "Controller", rest: str) -> None:
    """/workspace [new <name> [objective] | <name> | none]: the long form of /voyager and /chat."""
    cfg = ctl.session.cfg
    sub, _, arg = rest.partition(" ")
    if not sub:
        ctl.focused.log.add("notice", _listing(ctl))
    elif sub == "new":
        name, _, objective = arg.strip().partition(" ")
        try:
            ws = Workspace.create(cfg.workspaces_dir, name, objective)
        except (ValueError, OSError) as e:
            ctl.say(str(e) if name else "usage: /workspace new <name> [objective]", 6)
            return
        hint = "state the objective to start phase 0" if not objective.strip() else "send a message to start phase 0"
        ctl.switch_to(ws.root, f"workspace {ws.name} created · {hint}")
    elif sub == "none":
        chat_command(ctl)
    elif (ws := Workspace.get(cfg.workspaces_dir, sub)) is not None:
        ctl.switch_to(ws.root, f"workspace {ws.name} · {ws.current_phase()} · /resume lists its conversations")
    else:
        ctl.say(f"no such workspace: {sub} (/workspace lists them, /workspace new <name> creates one)", 6)
