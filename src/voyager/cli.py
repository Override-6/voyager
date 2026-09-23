"""Command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .config import Config, server_context_window
from .session import Session
from .voyager.workspace import Workspace


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="voyager", description="Agentic CLI for a local model: chat and coding, or long-running missions.")
    p.add_argument("prompt", nargs="?", help="optional first message (interactive mode keeps running)")
    p.add_argument("-p", "--print", dest="print_mode", action="store_true",
                   help="non-interactive: run the prompt, stream to stdout, wait for all agents, exit")
    p.add_argument("--resume", nargs="?", const="", metavar="SESSION_ID",
                   help="resume a saved conversation: with an id, that one; bare, browse them in the TUI (-p: the latest)")
    p.add_argument("-c", "--continue", dest="continue_", action="store_true", help="resume the latest conversation for this directory")
    p.add_argument("--base-url", help="model server URL (env VOYAGER_URL, default http://127.0.0.1:8080)")
    p.add_argument("--model", help="model name sent to the server (informational for llama-server)")
    p.add_argument("--max-tokens", type=int)
    p.add_argument("--thinking-budget", type=int, help="thinking token budget; 0 disables thinking")
    p.add_argument("--context-window", type=int,
                   help="context size in tokens (default: read from the server's /props, 65536 if unreachable)")
    p.add_argument("--compact-at", type=float, help="auto-compact at this fraction of the context window (default 0.70)")
    p.add_argument("--bg-after", type=float, help="move tool calls running longer than N seconds to the background (0 = never; default 30)")
    p.add_argument("--mcp-config", type=Path, help="MCP servers file (default: app/mcp.json; env VOYAGER_MCP_CONFIG)")
    p.add_argument("--mcp-refresh", action="store_true", help="start every configured MCP server once, cache its tool list, exit")
    p.add_argument("--cwd", type=Path, help="working directory (default: current)")
    p.add_argument("--coder-model", help="model alias passed to `claude --model` (default: sonnet)")
    p.add_argument("--coder-cwd", type=Path, help="directory the Coder agent works in (default: per-agent scratch dir)")
    p.add_argument("--system-dir", type=Path, help="system prompts folder: chat/MAIN.md, voyager/{MISSION,METHOD,PERSONA}.md, LOCAL.md, CODER.md")
    p.add_argument("--voyager", metavar="NAME", help="voyager mode: work in workspace NAME (created if new, with the prompt as its "
                   "objective; the prompt starts the mission). Default mode is chat")
    p.add_argument("--workspace", metavar="NAME", help="work in this existing workspace (cwd = its folder); --voyager also creates")
    p.add_argument("--monitor", nargs="?", const="", metavar="SESSION_ID",
                   help="watch a session from outside: state, rounds, tool calls (bare: the most recently active one)")
    p.add_argument("-f", "--follow", action="store_true", help="--monitor: keep printing new events until the session ends")
    p.add_argument("--json", action="store_true", help="--monitor: raw JSON lines (events.jsonl) instead of the digest")
    p.add_argument("--tail", type=int, default=40, help="--monitor: show the last N events (0 = all; default 40)")
    p.add_argument("--all", action="store_true", help="--monitor: also show thinking, context usage and tool starts")
    d = p.add_argument_group("background sessions (daemons)")
    d.add_argument("--detach", action="store_true", help="start the prompt as a session in the background, print its id, return")
    d.add_argument("--attach", nargs="?", const="", metavar="SESSION_ID", help="attach to a running session (bare: the newest); TUI, or text only with --cli")
    d.add_argument("--cli", action="store_true", help="--attach: text only, no TUI (lines you type are sent to the session)")
    d.add_argument("--ps", action="store_true", help="list the running session daemons")
    d.add_argument("--send", metavar="SESSION_ID", help="send the prompt as a message to a running session, return")
    d.add_argument("--stop", metavar="SESSION_ID", help="shut a running session down (it is saved and can be resumed)")
    d.add_argument("--local", action="store_true", help="TUI: run the session inside the TUI process (it ends when you quit) instead of in a daemon")
    d.add_argument("--idle-exit", type=float, metavar="SECS", help="--detach: the daemon exits after SECS without clients and without work (default: never)")
    p.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)  # internal: this process is a session daemon
    p.add_argument("--session-id", help=argparse.SUPPRESS)
    p.add_argument("--config-file", help=argparse.SUPPRESS)
    p.add_argument("--max-nudges", type=int, help="workspace: automatic 'continue' messages per user message when the plan is unfinished (default 20; 0 = off)")
    p.add_argument("--workspaces-dir", type=Path, help="where workspaces live (env VOYAGER_WORKSPACES, default ~/.voyager/workspaces)")
    return p


def build_config(a: argparse.Namespace) -> Config:
    cfg = Config()
    if a.base_url:
        cfg.base_url = a.base_url
    if a.model:
        cfg.model = a.model
    if a.max_tokens:
        cfg.max_tokens = a.max_tokens
    if a.thinking_budget is not None:
        cfg.thinking_budget = a.thinking_budget
    if a.context_window:
        cfg.context_window = a.context_window
    else:
        served = server_context_window(cfg.base_url)
        if served:
            cfg.context_window = served
        else:
            print(f"warning: could not read the context size from {cfg.base_url}/props; "
                  f"assuming {cfg.context_window} (use --context-window to set it)", file=sys.stderr)
    if a.compact_at:
        cfg.compact_at = a.compact_at
    if a.bg_after is not None:
        cfg.auto_background_secs = a.bg_after
    if a.mcp_config:
        cfg.mcp_config = a.mcp_config.resolve()
    if a.cwd:
        cfg.cwd = a.cwd.resolve()
    if a.coder_model:
        cfg.coder_model = a.coder_model
    if a.coder_cwd:
        cfg.coder_cwd = a.coder_cwd.resolve()
    if a.system_dir:
        cfg.system_dir = a.system_dir.resolve()
    if a.workspaces_dir:
        cfg.workspaces_dir = a.workspaces_dir.resolve()
    if a.max_nudges is not None:
        cfg.max_nudges = a.max_nudges
    cfg.launch_cwd = cfg.cwd
    if a.voyager:
        ws = Workspace.get(cfg.workspaces_dir, a.voyager) or Workspace.create(cfg.workspaces_dir, a.voyager, a.prompt or "")
        cfg.cwd = ws.root
    elif a.workspace:
        ws = Workspace.get(cfg.workspaces_dir, a.workspace)
        if ws is None:
            raise FileNotFoundError(f"no workspace {a.workspace!r} in {cfg.workspaces_dir} (create it with /workspace new)")
        cfg.cwd = ws.root
    return cfg


async def _amain(a: argparse.Namespace) -> int:
    if a.serve:  # this process is a session daemon (started by --detach or by the TUI)
        from .daemon.runner import serve

        return await serve(a)
    if a.monitor is not None:  # no model server needed: it only reads the session folder
        from .monitor import run_monitor

        return run_monitor(Config().sessions_dir, a.monitor or None, follow=a.follow, as_json=a.json, tail=a.tail, show_all=a.all)
    from .daemon.entry import route, tui_on_daemon

    if (code := await route(a, build_config)) is not None:
        return code
    if not (a.print_mode or a.mcp_refresh or a.local):  # the default: the TUI attached to a session in a background daemon
        try:
            cfg = build_config(a)
        except (FileNotFoundError, ValueError, OSError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        return await tui_on_daemon(a, cfg, pick=a.resume == "")
    try:
        cfg = build_config(a)
        pick = a.resume == "" and not a.print_mode  # bare --resume in the TUI: browse instead of guessing
        load = a.continue_ or (a.resume is not None and not pick)
        session = Session.load(cfg, a.resume or None) if load else Session(cfg)
    except (FileNotFoundError, ValueError, OSError) as e:  # ValueError: a bad --voyager name
        print(f"error: {e}", file=sys.stderr)
        return 2
    for problem in session.mcp.problems:
        print(f"mcp config warning: {problem}", file=sys.stderr)
    if a.mcp_refresh:
        for name, result in await session.mcp.refresh():
            print(f"{name}: {result}")
        await session.mcp.shutdown()  # not session.shutdown(): a refresh must not save an empty session
        return 0
    if a.print_mode:
        if not a.prompt:
            print("error: -p needs a prompt", file=sys.stderr)
            return 2
        from .plain import run_plain

        print(f"session {session.id} · {session.mode.name} mode · monitor: voyager --monitor {session.id} -f", file=sys.stderr)
        return await run_plain(session, a.prompt)
    from .tui import run_tui

    return await run_tui(session, a.prompt, pick=pick)


def main() -> None:
    args = build_parser().parse_args()
    try:
        code = asyncio.run(_amain(args))
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)


if __name__ == "__main__":
    main()
