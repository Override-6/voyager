"""The approach gate: knowledge/approach.md must record channels, >=3 approaches, prior art (URLs) and a decision."""
import dataclasses

import pytest

from conftest import run

from test_workspace import PLAN_OPEN, nudge_run, ws, wcfg  # noqa: F401  (fixtures)
from voyager.voyager.evidence import load, record
from voyager.voyager.approach import APPROACH_PATH, approach_status, gate_text
from voyager.session import Session
from voyager.voyager.state import state_block

LONG = "some reasoning that is long enough to count as filled in"
GOOD = f"""---
summary: how we reach the objective
confidence: likely
sources: none
updated: 2026-01-01
---
## Channels
{LONG}
## Approaches
- first approach, {LONG}
- second approach, {LONG}
- third approach, {LONG}
## Prior art
- https://example.org/project: {LONG}
## Decision
{LONG}
"""


def research(ws, searches=3, fetches=2, ws_searches=1):
    """What the harness records when the agent really looked things up."""
    for i in range(searches):
        record(ws, "web_search", {"query": f"q{i}"}, "1. Proj\n   https://example.org/project\n")
    for i in range(fetches):
        record(ws, "web_fetch", {"url": f"https://example.org/project/readme{i}"}, "# Readme")
    for _ in range(ws_searches):
        record(ws, "search_workspace", {"query": "x"}, "No match.")


def write(ws, text):
    (ws.knowledge_dir / "approach.md").write_text(text)


def test_status_walks_through_what_is_missing(ws):
    research(ws)
    assert "does not exist" in approach_status(ws)
    write(ws, GOOD.replace("## Channels", "## Chan"))
    assert "'## Channels'" in approach_status(ws)
    write(ws, GOOD.replace("- third approach", "third approach"))
    assert "fewer than 3" in approach_status(ws)
    write(ws, GOOD.replace("https://example.org/project", "some project"))
    assert "cites no URL" in approach_status(ws)
    write(ws, GOOD.replace(f"## Channels\n{LONG}", "## Channels\nshort"))
    assert "'## Channels'" in approach_status(ws)
    write(ws, GOOD)
    assert approach_status(ws) == "" and gate_text(ws) == ""


def test_the_research_must_really_have_happened(ws):
    write(ws, GOOD)
    assert "0 web_search" in approach_status(ws)
    research(ws, searches=2, fetches=2, ws_searches=1)
    assert "2 web_search" in approach_status(ws)
    research(ws, searches=1, fetches=0, ws_searches=0)  # now 3 searches, 4 fetches
    assert approach_status(ws) == ""


def test_workspace_search_and_real_urls_are_required(ws):
    write(ws, GOOD)
    research(ws, ws_searches=0)
    assert "never ran search_workspace" in approach_status(ws)
    record(ws, "search_workspace", {"query": "x"}, "")
    write(ws, GOOD.replace("https://example.org/project", "https://invented.dev/repo"))
    assert "came from your web searches" in approach_status(ws)
    write(ws, GOOD.replace("https://example.org/project", "http://www.example.org/project/#readme"))
    assert approach_status(ws) == ""  # scheme, www, fragment do not matter


def test_evidence_is_recorded_only_for_lookup_tools_and_survives_bad_files(ws):
    record(ws, "bash", {"command": "ls https://example.org/x"}, "https://example.org/x")
    assert load(ws).counts == {"web_search": 0, "web_fetch": 0, "search_workspace": 0} and load(ws).urls == []
    (ws.root / "scratch").mkdir(exist_ok=True)
    (ws.root / "scratch" / ".evidence.json").write_text("not json")
    assert load(ws).urls == []
    record(ws, "web_fetch", {"url": "https://a.example/p?x=1"}, "see https://b.example/q).")
    ev = load(ws)
    assert ev.counts["web_fetch"] == 1 and ev.knows("https://a.example/p") and ev.knows("https://b.example/q")


def test_state_block_shows_the_gate_until_the_approach_is_recorded(ws):
    assert APPROACH_PATH in state_block(ws, main=True) and "NOT RECORDED" in state_block(ws, main=True)
    assert "NOT RECORDED" not in state_block(ws, main=False)  # sub-agents don't decide the approach
    write(ws, GOOD)
    research(ws)
    assert "NOT RECORDED" not in state_block(ws, main=True)


def test_nudge_in_phase_0_points_at_the_gate_and_stops_once_recorded(monkeypatch, wcfg, ws):
    (ws.root / "PLAN.md").write_text("# Plan\n## Phases\n## Now\n")
    turns = nudge_run(monkeypatch, Session(wcfg), ws, progress=False)
    assert len(turns) == 2 and "NOT RECORDED" in turns[1]
    write(ws, GOOD)
    research(ws)
    assert len(nudge_run(monkeypatch, Session(wcfg), ws, progress=False)) == 1


def test_open_plan_nudge_mentions_the_gate_only_while_pending(monkeypatch, wcfg, ws):
    (ws.root / "PLAN.md").write_text(PLAN_OPEN)
    assert "NOT RECORDED" in nudge_run(monkeypatch, Session(wcfg), ws, progress=False)[1]
    write(ws, GOOD)
    research(ws)
    assert "NOT RECORDED" not in nudge_run(monkeypatch, Session(wcfg), ws, progress=False)[1]


def test_tool_calls_are_recorded_by_the_harness(monkeypatch, wcfg, ws):
    import httpx2
    from types import SimpleNamespace
    from voyager.tools import web
    monkeypatch.setattr(web, "make_client", lambda: httpx2.AsyncClient(transport=httpx2.MockTransport(
        lambda r: httpx2.Response(200, text='<a class="result__a" href="https://direct.example.org/b">B</a>')), follow_redirects=True))
    m = Session(wcfg).main
    item = m.log.start("tool", name="web_search")
    run(m._execute(SimpleNamespace(name="web_search", input={"query": "anything"}, id="1"), item))
    ev = load(ws)
    assert ev.counts["web_search"] == 1 and ev.knows("https://direct.example.org/b")
