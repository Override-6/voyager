"""Chat mode command: /chat leaves voyager mode."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..voyager.workspace import Workspace

if TYPE_CHECKING:
    from ..tui.controller import Controller


def chat_command(ctl: "Controller") -> None:
    """Back to chat mode: a new plain conversation in the launch directory."""
    cfg = ctl.session.cfg
    home = cfg.launch_cwd or Path.cwd()
    if Workspace.at(home, cfg.workspaces_dir) is not None:
        home = Path.home()
    ctl.switch_to(home, f"chat mode · {home}")
