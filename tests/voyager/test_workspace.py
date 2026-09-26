import asyncio
import copy
import dataclasses
import subprocess

import pytest

from conftest import run
from test_compaction import MSGS, FakeClient
from voyager.local import LocalAgent
from voyager.session import Session
from voyager.tools import ToolContext, ToolError
from voyager.voyager.tools import SaveTool, SearchWorkspace
from voyager.voyager.workspace import Workspace
from voyager.voyager.state import lint, parse_frontmatter, parse_header, scan_notes, scan_tools, set_status, state_block

TOOL = '''#!/usr/bin/env python3
"""
summary: Say hello to someone.
usage: python3 tools/demo/hello.py <name>
example: python3 tools/demo/hello.py world
"""
import sys
status: str = "not a header line"
print("hello", sys.argv[1])
'''

NOTE = "---\nsummary: How the login form works\nconfidence: likely\nsources: https://x.test\nupdated: 2026-09-23\n---\nThe login form posts a csrf token.\n"


@pytest.fixture
def ws(cfg):
    return Workspace.create(cfg.workspaces_dir, "demo", "Map the target's login flow.")


@pytest.fixture
def wcfg(cfg, ws):
    return dataclasses.replace(cfg, cwd=ws.root)


def git_log(ws):
    return subprocess.run(["git", "log", "--format=%s"], cwd=ws.root, capture_output=True, text=True).stdout


def test_create_layout_lookup_and_first_commit(cfg, ws):
    for rel in ("OBJECTIVE.md", "PLAN.md", "tools/lib", "knowledge", "scratch", ".gitignore"):
        assert (ws.root / rel).exists()
    assert ws.objective_line() == "Map the target's login flow."
    assert Workspace.at(ws.root / "tools", cfg.workspaces_dir).root == ws.root  # any folder inside counts
    assert Workspace.at(cfg.cwd, cfg.workspaces_dir) is None
    assert [w.name for w in Workspace.list_all(cfg.workspaces_dir)] == ["demo"]
    assert "workspace created" in git_log(ws)
    assert ws.current_phase() == "phase 0 (frame)"
    with pytest.raises(ValueError):
        Workspace.create(cfg.workspaces_dir, "demo")
    with pytest.raises(ValueError):
        Workspace.create(cfg.workspaces_dir, "Bad Name")


def test_phases_from_plan(ws):
    (ws.root / "PLAN.md").write_text(
        "# Plan\n## Phases\n- [x] 1. Recon — exit: x\n- [>] 2. Map login — exit: y\n- [ ] 3. Report\n## Now\n- [ ] a\n"
    )
    assert ws.phases() == [("x", "1. Recon — exit: x"), (">", "2. Map login — exit: y"), (" ", "3. Report")]
    assert ws.current_phase() == "2. Map login"


def test_header_parsing_stays_in_the_header_block():
    h = parse_header(TOOL)
    assert h == {"summary": "Say hello to someone.", "usage": "python3 tools/demo/hello.py <name>",
                 "example": "python3 tools/demo/hello.py world"}  # the `status: str` code line is not the header
    added = set_status(TOOL, "draft")
    assert parse_header(added)["status"] == "draft" and 'status: str = "not a header line"' in added
    assert parse_header(set_status(added, "verified"))["status"] == "verified"
    assert parse_header("# summary: x\n# usage: y\n# example: z\n# status: verified\nset -e\n# summary: no")["summary"] == "x"


def test_frontmatter_lint_and_index_budget(ws):
    assert parse_frontmatter(NOTE)["confidence"] == "likely" and parse_frontmatter("no fm") is None
    (ws.knowledge_dir / "web").mkdir()
    (ws.knowledge_dir / "web" / "login.md").write_text(NOTE)
    (ws.knowledge_dir / "bad.md").write_text("just text")
    (ws.tools_dir / "raw.py").write_text("print(1)\n")
    (ws.tools_dir / "lib" / "util.py").write_text("X = 1\n" * 501)  # the shared library is not indexed, but is linted
    (ws.root / "PLAN.md").write_text("# Plan\n## Phases\n## Now\n" + "- [ ] x\n" * 12)
    tools, notes = scan_tools(ws), scan_notes(ws)
    assert [t.path for t in tools] == ["tools/raw.py"]
    problems = "\n".join(lint(ws, tools, notes))
    for expected in ("tools/raw.py: header lacks", "knowledge/bad.md: no frontmatter", "missing section(s) ## Current phase",
                     "Now has 12 items", "tools/lib/util.py: 501 lines, over the 500-line hard limit"):
        assert expected in problems
    block = state_block(ws, main=True, round_no=3)
    assert "round 3" in block and "knowledge/web/login.md — How the login form works (likely)" in block
    assert "Problems to fix" in block
    for i in range(200):
        (ws.tools_dir / f"t{i:03}.py").write_text(f'"""\nsummary: tool number {i} ' + "x" * 40 + '\nusage: u\nexample: e\nstatus: verified\n"""\n')
    assert "more tools: use search_workspace" in state_block(ws, main=True)
    sub = state_block(ws, main=False)
    assert "never edit them" in sub and "## PLAN.md" not in sub


def ctx_for(session):
    return ToolContext(cwd=session.cfg.cwd, session=session)


def test_save_tool_verifies_drafts_and_commits(wcfg, ws):
    s = Session(wcfg)
    assert {"save_tool", "search_workspace"} <= set(s.tools_for(s.main))
    out = run(SaveTool().run({"path": "tools/demo/hello.py", "content": TOOL}, ctx_for(s)))
    assert "VERIFIED" in out and "hello world" in out
    assert parse_header((ws.root / "tools/demo/hello.py").read_text())["status"] == "verified"
    assert "tool: tools/demo/hello.py" in git_log(ws)

    broken = TOOL.replace("hello.py world", "hello.py")  # the example forgets the argument: IndexError
    with pytest.raises(ToolError, match="DRAFT"):
        run(SaveTool().run({"path": "tools/demo/hello.py", "content": broken}, ctx_for(s)))
    assert parse_header((ws.root / "tools/demo/hello.py").read_text())["status"] == "draft"
    with pytest.raises(ToolError, match="only writes under"):
        run(SaveTool().run({"path": "scratch/x.py", "content": TOOL}, ctx_for(s)))
    with pytest.raises(ToolError, match="header lacks example"):
        run(SaveTool().run({"path": "tools/x.py", "content": '"""\nsummary: s\nusage: u\n"""\n'}, ctx_for(s)))


def test_search_ranks_header_hits_first(wcfg, ws):
    s = Session(wcfg)
    (ws.knowledge_dir / "login.md").write_text(NOTE)
    (ws.tools_dir / "hello.py").write_text(TOOL)
    out = run(SearchWorkspace().run({"query": "login csrf"}, ctx_for(s)))
    assert out.startswith("knowledge/login.md — How the login form works") and "csrf token" in out
    assert run(SearchWorkspace().run({"query": "hello", "scope": "knowledge"}, ctx_for(s))).startswith("No match")
    assert "tools/hello.py" in run(SearchWorkspace().run({"query": "hello", "scope": "tools"}, ctx_for(s)))


def test_mission_prompt_snapshot_is_stable_until_refreshed(wcfg, ws, offline):
    s = Session(wcfg)
    p1 = s.main.system_prompt()
    assert "workspace **demo**" in p1 and "Map the target's login flow." in p1 and "{{" not in p1
    (ws.knowledge_dir / "new.md").write_text(NOTE)
    assert s.main.system_prompt() == p1  # a snapshot: the prefix stays stable between refreshes
    s.main.mode.state = None  # what a new user message / compaction does
    assert "knowledge/new.md" in s.main.system_prompt()
    sub = LocalAgent(s, "a1", "w", parent_id="main")
    assert "never edit them" in sub.system_prompt() and "{{" not in sub.system_prompt()


def test_checkpoint_before_compaction_then_new_round(wcfg, ws):
    client = FakeClient()
    s = Session(wcfg, client=client)
    m = s.main
    m.messages = [copy.deepcopy(x) for x in MSGS * 4]

    def at(frac):
        m._measured_tokens, m._measured_len = int(wcfg.context_window * frac), len(m.messages)

    at(0.65)  # past checkpoint_at (0.60), under compact_at (0.70)
    run(m._maybe_compact())
    assert m.mode.checkpointed and m.messages[-1]["content"][-1]["text"].startswith("<checkpoint>")
    assert len(m.messages) == 12 and not client.calls
    run(m._maybe_compact())
    assert sum("<checkpoint>" in str(x) for x in m.messages) == 1  # once per round
    (ws.root / "PLAN.md").write_text("# Plan\n## Phases\n- [>] 1. Recon — exit: x\n")
    at(0.75)
    run(m._maybe_compact())
    assert len(m.messages) == 1 and m.mode.round == 1 and not m.mode.checkpointed
    assert "work in flight" in client.calls[0]["messages"][0]["content"]
    assert "trust PLAN.md" in m.messages[0]["content"][-1]["text"] and "<checkpoint>" not in m.messages[0]["content"][-1]["text"]
    assert "main r0 compacted: 1. Recon" in git_log(ws)
    extra = m.extra_state()
    assert {k: extra[k] for k in ("round", "checkpointed", "compactions")} == {"round": 1, "checkpointed": False, "compactions": 1}
    assert set(extra["files"]) == {"OBJECTIVE.md", "PLAN.md", "knowledge/approach.md"}


def test_checkpoint_defers_compaction_once_unless_near_full(wcfg):
    s = Session(wcfg, client=FakeClient())
    m = s.main
    m.messages = [copy.deepcopy(x) for x in MSGS * 4]
    m._measured_tokens, m._measured_len = int(wcfg.context_window * 0.75), len(m.messages)
    run(m._maybe_compact())
    assert m.mode.checkpointed and len(m.messages) == 12  # over compact_at, but the agent first gets to save
    run(m._maybe_compact())
    assert len(m.messages) == 1
    m.messages = [copy.deepcopy(x) for x in MSGS * 4]
    m._measured_tokens, m._measured_len = int(wcfg.context_window * 0.9), len(m.messages)
    run(m._maybe_compact())
    assert m.mode.round == 2 and len(m.messages) == 1  # near full: checkpoint and compaction in the same step


def test_workspace_command_new_open_none(cfg, offline):
    from voyager.tui.controller import Controller

    async def go():
        c = Controller(Session(cfg))
        c.command("/workspace")
        assert "no workspaces yet" in c.session.main.log.items[-1].text
        c.command("/workspace new recon Enumerate the API.")
        await c._swap_task
        assert c.session.workspace.name == "recon" and c.session.cfg.cwd == cfg.workspaces_dir / "recon"
        assert "Enumerate the API." in c.session.main.system_prompt()
        c.command("/workspace none")
        await c._swap_task
        assert c.session.workspace is None and c.session.cfg.cwd == cfg.cwd
        c.command("/workspace recon")
        await c._swap_task
        assert c.session.workspace.name == "recon"
        c.command("/workspace nope")
        assert "no such workspace" in c.flash
        c.command("/workspace")
        assert "* recon" in c.session.main.log.items[-1].text
    asyncio.run(go())


PLAN_OPEN = "# Plan\n## Phases\n- [x] 1. Recon — exit: a\n- [>] 2. Map — exit: b\n## Current phase\n## Now\n- [ ] x\n## Blocked\n## Log\n"


def nudge_run(monkeypatch, s, ws, *, progress: bool, first="go"):
    """Drive real run_turn with a fake model loop; returns the texts of the turns the main agent ran."""
    turns: list[str] = []

    async def fake_steps(self, manual_only):
        turns.append(self.messages[-1]["content"] if self.messages else "")
        if progress:
            (ws.knowledge_dir / f"n{len(list(ws.knowledge_dir.iterdir()))}.md").write_text(NOTE)

    monkeypatch.setattr(LocalAgent, "_steps", fake_steps)

    async def go():
        s.main.submit(first)
        await s.wait_idle()
    asyncio.run(go())
    return turns


def test_nudge_once_when_the_agent_stops_without_progress(monkeypatch, wcfg, ws):
    (ws.root / "PLAN.md").write_text(PLAN_OPEN)
    s = Session(wcfg)
    turns = nudge_run(monkeypatch, s, ws, progress=False)
    assert len(turns) == 2 and turns[1].startswith("<continue>") and "current: 2. Map" in turns[1]
    assert any("plan unfinished" in i.text for i in s.main.log.items)


def test_nudges_repeat_while_progressing_up_to_the_budget(monkeypatch, wcfg, ws):
    (ws.root / "PLAN.md").write_text(PLAN_OPEN)
    s = Session(dataclasses.replace(wcfg, max_nudges=3))
    turns = nudge_run(monkeypatch, s, ws, progress=True)
    assert len(turns) == 4 and s.main.mode.nudges == 3
    # a new user message resets the budget
    turns = nudge_run(monkeypatch, s, ws, progress=True, first="more")
    assert len(turns) == 4


@pytest.mark.parametrize("plan", [
    PLAN_OPEN.replace("## Blocked\n", "## Blocked\n- need VPN credentials\n"),  # blocked
    PLAN_OPEN.replace("[>] 2.", "[x] 2."),  # done
])
def test_no_nudge_when_blocked_or_done(monkeypatch, wcfg, ws, plan):
    (ws.root / "PLAN.md").write_text(plan)
    assert len(nudge_run(monkeypatch, Session(wcfg), ws, progress=False)) == 1


@pytest.mark.parametrize("placeholder", ["- (None yet — the hard risk is resolved.)", "None.", "- n/a"])
def test_a_placeholder_under_blocked_does_not_stop_the_nudge(monkeypatch, wcfg, ws, placeholder):
    (ws.root / "PLAN.md").write_text(PLAN_OPEN.replace("## Blocked\n", f"## Blocked\n{placeholder}\n"))
    assert ws.blockers() == [] and len(nudge_run(monkeypatch, Session(wcfg), ws, progress=False)) == 2


def test_no_nudge_while_waiting_for_a_sub_agent_or_outside_a_workspace(monkeypatch, cfg, wcfg, ws):
    (ws.root / "PLAN.md").write_text(PLAN_OPEN)
    s = Session(wcfg)
    sub = LocalAgent(s, "a1", "w", parent_id="main")
    sub.running = True  # still working: its notification will wake the main agent
    s.agents["a1"] = sub
    turns: list[str] = []

    async def fake_steps(self, manual_only):
        turns.append("t")

    monkeypatch.setattr(LocalAgent, "_steps", fake_steps)
    asyncio.run(s.main.run_turn("go"))
    assert turns == ["t"] and not s.main.inbox
    assert len(nudge_run(monkeypatch, Session(cfg), ws, progress=False)) == 1
