"""Routing of the daemon-related command-line flags (called by cli._amain before anything else is built)."""

from __future__ import annotations

import argparse
import sys
from typing import Callable

from ..config import Config
from .backend import DaemonBackend, open_first
from .commands import cmd_attach_cli, cmd_detach, cmd_ps, cmd_send, cmd_stop, connect, pick_live
from .launcher import LaunchError


async def route(a: argparse.Namespace, build_config: Callable[[argparse.Namespace], Config]) -> int | None:
    """Run --ps / --stop / --send / --attach / --detach. None: not a daemon command, carry on with the normal flow."""
    base = Config()  # these only need to know where the sessions are
    if a.ps:
        return await cmd_ps(base)
    if a.stop:
        return await cmd_stop(base, a.stop)
    if a.send:
        if not a.prompt:
            print("error: --send needs the message as the prompt: voyager --send ID \"message\"", file=sys.stderr)
            return 2
        return await cmd_send(base, a.send, a.prompt)
    if a.attach is not None:
        if a.cli:
            return await cmd_attach_cli(base, a.attach or None)
        return await attach_tui(base, a.attach or None)
    if a.detach:
        try:
            cfg = build_config(a)
        except (FileNotFoundError, ValueError, OSError) as e:  # a bad --voyager name, a missing workspace
            print(f"error: {e}", file=sys.stderr)
            return 2
        return await cmd_detach(cfg, a.prompt, a.idle_exit or 0.0)
    return None


async def attach_tui(cfg: Config, ref: str | None) -> int:
    from ..tui import run_tui

    live = pick_live(cfg, ref)
    if live is None:
        return 2
    return await run_tui(await connect(cfg, live), backend=DaemonBackend())


async def tui_on_daemon(a: argparse.Namespace, cfg: Config, pick: bool) -> int:
    """The interactive TUI, with its session running in a daemon so that quitting can leave it working."""
    from ..tui import run_tui

    try:
        remote = await open_first(cfg, prompt=a.prompt, resume=a.resume or None, use_latest=a.continue_)
    except (LaunchError, FileNotFoundError, OSError, ValueError) as e:
        print(f"error: {e}\n(--local runs the session inside the TUI instead of in a background daemon)", file=sys.stderr)
        return 2
    return await run_tui(remote, pick=pick, backend=DaemonBackend())
