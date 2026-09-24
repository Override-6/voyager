"""Voyager mode's side of compaction: the checkpoint, the summary of work in flight, the note after it.

Compaction is also a *checkpoint*: at checkpoint_at (default compact_at - 0.10) the agent is asked to save its state to
the workspace (PLAN.md, knowledge/, tools/), so the summary only has to carry the work in flight. See ../compaction.py
for the generic part.
"""

from __future__ import annotations

WORKSPACE_COMPACT_INSTRUCTIONS = """Summarize the conversation above for the agent that will continue it. Its durable state (OBJECTIVE.md, PLAN.md, tools/, knowledge/) is on disk and shown to it separately: do NOT restate the plan, the tool list or facts already written to knowledge/. Carry only the work in flight, thoroughly but without padding (under 2000 words in total):
1. Latest user request: quote it verbatim.
2. Current step: which PLAN.md "Now" item was being worked on, and exactly where it stopped.
3. Not yet saved: results, values, errors and dead ends from this context that are NOT in the workspace files (say where they should go).
4. Open threads: hypotheses being tested, agents and background tasks (ids a1, t1, ...) and what they are for.
5. Next action: the concrete next tool call or step."""

CHECKPOINT = (
    "<checkpoint>\nThe context will be compacted soon: this conversation will be replaced by a short summary. "
    "Finish the current action, then save your state to the workspace before continuing:\n"
    "1. PLAN.md: tick finished Now items, write the next ones, update Current phase, add one Log line "
    "(`YYYY-MM-DD r{round}: <what this round achieved>`).\n"
    "2. knowledge/: write every fact learned in this context that is not saved yet (with sources).\n"
    "3. tools/: promote any scratch script you will need again with save_tool.\n"
    "Test: someone with only the workspace files must be able to continue your work. Then carry on with the task: a checkpoint is not a stopping point, do not end your turn.\n"
    "</checkpoint>"
)

WORKSPACE_NOTE = (
    "\n\nYour workspace state in the system prompt (OBJECTIVE.md, PLAN.md, tools and knowledge indexes) was just "
    "refreshed: it is the source of truth. Where it disagrees with this summary, trust PLAN.md. "
    "Resume with the first Now item; the objective is not reached yet, so keep working without ending your turn."
)

CHECKPOINT_GAP = 0.10  # default checkpoint_at = compact_at - this
HARD_GAP = 0.15  # a pending checkpoint may delay compaction up to compact_at + this (at most 95% of the window)
