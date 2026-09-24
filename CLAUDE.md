# voyager

The Voyager pattern for a local model served by llama-server through the Anthropic `/v1/messages` API. The point of the project is
**voyager mode**: long objectives run in **workspaces**, where the agent keeps its own codebase (`tools/`) and knowledge base
(`knowledge/`), plans on three horizons in `PLAN.md`, and survives any number of context compactions. Chat mode (no workspace, a plain
coding / chat assistant) is a secondary convenience: keep it working, but design decisions favour voyager mode. `README.md` is the
user-facing reference (features, commands, flags); this file is for working on the code.

## Commands
- Tests: `uv run pytest -q` (offline, ~2 s: fake model client, fake `claude`, fake MCP server; no network).
- Run: `./voyager [prompt]` (the launcher script; starts the model server if needed) or `uv run voyager [prompt]` (needs the model server); `-p` for non-interactive; `--voyager NAME` to start in voyager mode (workspace NAME; default is chat).
- Every session also keeps a complete, append-only record (`transcript.jsonl` + `rounds/*.json`, see `transcript.py`); anything an agent does must
  reach the log as an item so it lands there. Nothing reads it back yet.
- Monitor a running or finished session (from outside, no server needed): `uv run voyager --monitor [SESSION_ID] [-f]`; it reads the live
  `events.jsonl` every session writes (`events.py`). New user-visible agent behaviour should emit an event there (a `log.add` with `event=` meta).
- Code navigation: this repo is indexed by CodeGraph (`.codegraph/`); prefer `codegraph_explore` / `codegraph explore "<symbols>"`
  over grep. Run `codegraph sync` after large changes if the index looks stale.

## Quality rules
- **File size: keep every file under 300 lines. 500 lines is the hard maximum**, never to be exceeded. Past 300, split
  by responsibility (see how `compaction.py` / `compactor.py` or `voyager/workspace.py` / `voyager/state.py` are split) rather than growing the file.
- Every change comes with offline tests in `tests/` and leaves `uv run pytest` green. Never let a test reach the network,
  the real `claude` binary, or the real `~/.voyager` (the `cfg` fixture in `tests/conftest.py` points everything at `tmp_path`).
- Match the surrounding style: small functions, short docstrings that say *why*, comments only for non-obvious intent.
- Keep `README.md` in sync when behavior, commands or flags change.

## Architecture (src/voyager/)
- **One asyncio loop, no threads.** Agents are asyncio worker tasks (`agent.py`); a stop is `task.cancel()`; tools and
  `claude -p` are async subprocesses; the TUI is prompt_toolkit's asyncio app. Don't introduce threads or blocking calls
  in the loop (quick `subprocess.run` at creation time, as in `Workspace.create`, is the exception).
- `session.py` owns agents, spawning, parent notifications, save/load. `local.py` is the local-model loop
  (`run_turn` → `_steps` → `_stream_step` / `_run_tools`); `coder.py` drives `claude -p`.
- Tools: subclass `Tool` (`tools/base.py`), register in `tools/__init__.py`; voyager-only tools live in
  `voyager/tools.py` and are added by `Session.tools_for` through the mode. Expected failures raise `ToolError` (message goes to the model).
  Every tool schema is sent on every request: keep descriptions tight and the tool count low.
- **Two modes, one package each: `chat/` (plain coding / chat, the default) and `voyager/` (a long-running mission in a workspace).**
  `mode.py` defines the `Mode` / `AgentMode` hooks (system prompt, pinned text, checkpoint, compaction, turn end, tool recording);
  `local.py`, `compactor.py` and `session.py` call them and never ask which mode they are in. Anything voyager-specific goes
  in `voyager/`, never in the core files. Prompts and tests follow the same split (`system/chat|voyager/`, `tests/chat|voyager/`).
- **Sessions run in daemons; the TUI is a client.** `daemon/server.py` serves a real `Session` on a unix socket and pushes every log event and
  state change; `daemon/client.py` (`RemoteSession` + `replica.py`) rebuilds it on the client with the same surface the TUI reads (`agents`, `main`,
  `tasks`, `hooks`, `mode`, `cfg`, `leave`...). If the TUI starts reading something new from the session or an agent, add it to the protocol
  (`protocol.py`) and the replica, and cover it in `tests/daemon/`. `--local` (and the tests) use a plain in-process `Session` through `tui/backend.py`.
- System prompts are files in `system/` (`chat/MAIN.md`, `voyager/MISSION.md` + `METHOD.md` + `PERSONA.md`, and `LOCAL.md` sub-agents /
  `CODER.md` shared; the compaction prompts too: `COMPACT_SYSTEM.md`, `SUMMARY.md`, `{chat,voyager}/COMPACT.md`, `voyager/CHECKPOINT.md`,
  `voyager/AFTER_COMPACT.md`). Model-facing text goes in these files, never hardcoded in Python. They are re-read on every request; `{{placeholders}}` are filled by `config.load_system_prompt`.

## Voyager mode: invariants to preserve
- A session is in voyager mode (in a workspace) iff its cwd is inside `cfg.workspaces_dir` (`Workspace.at`); nothing else is persisted for it.
- The workspace state block at the end of the system prompt is a **snapshot** (`VoyagerAgentMode.state`), refreshed only on a
  new message and after a compaction, so the prompt prefix stays cacheable. Don't make it re-render on every request.
- Compaction in a workspace: `<checkpoint>` at `checkpoint_at`, compaction deferred up to `compact_at + 0.15`, then
  `round += 1`, git commit, fresh snapshot (`voyager/agent_mode.py`). The summary carries only in-flight work; durable state is on disk.
- The "keep going" nudge (`voyager/nudge.py`) must stay bounded: per-user-message budget (`max_nudges`) and no second
  nudge in a row without workspace progress (git HEAD moved).
- Git commits go through `Workspace.commit`, which never raises: a failed commit must never stop an agent.
- Anything the agent must obey goes in the prompts; anything that must *always* happen (testing a tool, indexing,
  committing, linting) is done by the harness, not left to the model.

## Gotchas
- Switching or resuming sessions: give the new session a copy of the config (`dataclasses.replace(cfg, cwd=...)`); mutating the
  shared `cfg.cwd` makes the old session save itself under the new directory.
- In tests, `copy.deepcopy(MSGS * 4)` keeps shared identity; copy each message (`[copy.deepcopy(m) for m in MSGS * 4]`)
  before mutating history.
- `Session.spawn` / `Agent.submit` need a running event loop; build sub-agents directly (`LocalAgent(s, "a1", ...)`) in sync tests.
