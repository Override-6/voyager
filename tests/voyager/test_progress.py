"""Measured progress: the probe in OBJECTIVE.md, run at each round end, and the stall review after rounds without progress."""
import asyncio
import dataclasses

import pytest

from voyager.session import Session
from voyager.voyager import progress
from voyager.voyager.state import state_block
from voyager.voyager.workspace import Workspace

PLAN = "# Plan\n## Phases\n- [>] 1. Build — exit: a\n## Current phase\n## Now\n- [x] done thing\n- [ ] {now}\n## Blocked\n## Log\n"


@pytest.fixture
def ws(cfg):
    return Workspace.create(cfg.workspaces_dir, "demo", "Collect things.")


def set_probe(ws, section):
    text = ws.read("OBJECTIVE.md").split(progress.HEADING)[0]
    (ws.root / "OBJECTIVE.md").write_text(text + progress.HEADING + "\n" + section + "\n")


def measure(ws, round_no, score=None, now="craft planks"):
    if score is not None:
        (ws.root / "score.txt").write_text(f"items: {score}\n")
    (ws.root / "PLAN.md").write_text(PLAN.format(now=now))
    return asyncio.run(progress.measure(ws, round_no))


@pytest.mark.parametrize("section, command", [
    ("", ""),
    ("(one shell command, run from the workspace root at every round end)", ""),  # the template placeholder
    ("`python3 count.py --items`", "python3 count.py --items"),
    ("```\ncat score.txt\n```", "cat score.txt"),
    ("- cat score.txt", "cat score.txt"),
])
def test_the_probe_command_is_read_from_the_objective(ws, section, command):
    set_probe(ws, section)
    assert progress.probe_command(ws) == command


def test_a_fresh_workspace_asks_for_a_probe(ws):
    assert progress.probe_command(ws) == ""
    assert "Progress probe: NOT DEFINED" in state_block(ws, main=True)


def test_a_stall_review_after_rounds_without_progress_and_not_twice_in_a_row(ws):
    set_probe(ws, "`cat score.txt`")
    assert measure(ws, 0, 1)["value"] == 1 and not measure(ws, 1, 3)["review"]
    assert not measure(ws, 2, 3)["review"]  # one round flat: not yet
    assert measure(ws, 3, 3)["review"]  # two rounds flat: review
    assert not measure(ws, 4, 3)["review"]  # the review just happened: give it a round or two
    assert measure(ws, 5, 3)["review"]
    assert not measure(ws, 6, 4)["review"]  # progress again
    assert "r6 4" in progress.state_line(ws) and "`cat score.txt`" in progress.state_line(ws)


def test_without_a_measurement_an_unchanged_first_now_item_is_a_stall(ws):
    for r in range(2):
        assert not measure(ws, r, now=f"step {r}")["review"]
    assert not measure(ws, 2, now="step 1")["review"]
    assert measure(ws, 3, now="step 1")["review"]


def test_a_broken_probe_is_reported(ws):
    set_probe(ws, "`echo no numbers here`")
    row = measure(ws, 0)
    assert row["value"] is None and "no number" in row["line"]
    set_probe(ws, "`exit 3`")
    assert "exit code 3" in measure(ws, 1)["line"] and "last run: exit code 3" in progress.state_line(ws)


def test_the_round_end_opens_the_next_round_with_the_review(cfg, ws):
    set_probe(ws, "`cat score.txt`")
    for r in range(2):
        measure(ws, r, 5)
    s = Session(dataclasses.replace(cfg, cwd=ws.root))
    s.main.messages = [{"role": "user", "content": "<summary>work in flight</summary>"}]
    s.main.mode.round = 2  # after_compact ends round 2
    asyncio.run(s.main.mode.after_compact())
    text = s.main.messages[0]["content"][-1]["text"]
    assert "<stall-review>" in text and "5 → 5 → 5" in text and "craft planks" in text
    events = [i.meta.get("event") for i in s.main.log.items]
    assert "progress" in events and "stall" in events
