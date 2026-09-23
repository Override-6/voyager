"""Slash commands. `run_command` is called by the controller for any input starting with "/"."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..chat.commands import chat_command
from ..voyager.commands import voyager_command, workspace_command

if TYPE_CHECKING:
    from .controller import Controller

HELP = """Commands
  /agents                list agents and background tasks
  /agent <id|name>       watch an agent (also: click / ↓ list / Tab)      /main   back to the main agent
  /task <id>             watch a background task's output                 /kill <task-id>   kill a task
  /resume                browse saved conversations (type to filter, Enter resumes, Tab: all directories)
  /resume <session-id>   resume that conversation directly       /continue <agent> [msg]   resume a finished agent
  /stop [id]             stop the current turn of an agent (default: the one you are viewing)
  /mcp                   MCP servers: started lazily on the first tool call; tools are cached on disk
  /compact               compact this local-model agent's context now (auto at the configured threshold)
  /voyager <name> [objective]   voyager mode: a long-running mission in workspace <name> (created if new; the objective
                         starts it). /voyager alone lists workspaces      /chat   back to chat mode (plain coding / chat)
  /workspace ...         the same as /voyager, with `new <name> [objective]` and `none` spelled out
  /clear                 forget everything and start over
  /thinking <tokens>     set the thinking budget (0 = off)
  /detach                quit the TUI and leave the session running in the background (attach later: voyager --attach)
  /exit                  quit (also Ctrl-D): the session keeps running in the background if it still has work, else it is saved
  /exit stop             quit and end the session, whatever it is doing
Keys: ↑/↓ input history (saved on disk) · ↓ on empty prompt opens the list · Tab / Shift-Tab next / previous agent
      Esc back · Ctrl-C stop (twice to quit when idle) · PgUp/PgDn scroll · Ctrl-J newline · click a tool call to expand"""


def run_command(ctl: "Controller", text: str) -> None:
    cmd, _, rest = text.partition(" ")
    rest, s, a = rest.strip(), ctl.session, ctl.focused
    if cmd == "/help":
        a.log.add("notice", HELP)
    elif cmd == "/agents":
        rows = [f"{x.id:<6}{x.name:<26}{x.kind:<7}{x.status}" for x in s.ordered()]
        rows += [f"{t.id:<6}{t.tool + ': ' + t.summary[:40]:<33}task   {t.status} {t.elapsed:.0f}s" for t in s.tasks.tasks.values()]
        a.log.add("notice", "\n".join(rows))
    elif cmd in ("/agent", "/main"):
        target = s.resolve(rest or "main") if cmd == "/agent" else s.main
        ctl.focus(target.id) if target else ctl.say(f"no such agent: {rest}")
    elif cmd == "/task":
        ctl.open_task(rest) if s.tasks.get(rest) else ctl.say(f"no such task: {rest}")
    elif cmd == "/kill":
        bt = s.tasks.get(rest)
        ctl._kill_entry("task", bt) if bt else ctl.say(f"no such task: {rest}")
    elif cmd == "/stop":
        target = s.resolve(rest) if rest else a
        ctl._kill_entry("agent", target) if target else ctl.say(f"no such agent: {rest}")
    elif cmd in ("/resume", "/sessions"):
        ref, _, msg = rest.partition(" ")
        agent = s.resolve(ref) if ref else None
        if not ref:
            ctl.open_picker()
        elif agent is not None:  # /resume a1 [msg]: still resumes an agent of this conversation
            ctl.focus(agent.id)
            agent.submit(msg.strip() or "Continue.")
        else:
            ctl.resume_session(ref)
    elif cmd == "/continue":
        ref, _, msg = rest.partition(" ")
        agent = s.resolve(ref) if ref else None
        if agent is None:
            ctl.say("usage: /continue <agent-id> [message]")
        else:
            ctl.focus(agent.id)
            agent.submit(msg.strip() or "Continue.")
    elif cmd == "/compact":
        if a.kind != "local":
            ctl.say("only local-model agents compact here; a coder's context is managed by the claude CLI")
        else:
            a.request_compact()
    elif cmd == "/voyager":
        voyager_command(ctl, rest)
    elif cmd == "/chat":
        chat_command(ctl)
    elif cmd == "/workspace":
        workspace_command(ctl, rest)
    elif cmd == "/clear":
        s.reset()
        ctl.focus("main")
        ctl.cache.clear()
        ctl.scroll.clear()
    elif cmd == "/thinking":
        try:
            s.set_thinking(int(rest))
            ctl.say(f"thinking budget: {s.cfg.thinking_budget}")
        except ValueError:
            ctl.say("usage: /thinking <tokens>  (0 = off)")
    elif cmd == "/detach":
        if s.detachable:
            ctl.exit(keep=True)
        else:
            ctl.say("this session lives inside the TUI (--local) and cannot run in the background; /exit ends it", 6)
    elif cmd in ("/exit", "/quit"):
        ctl.exit(keep=False if rest == "stop" else None)
    else:
        ctl.say(f"unknown command {cmd} — /help")
