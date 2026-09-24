"""Voyager mode's side of compaction: the checkpoint before it and the note after it.

Compaction is also a *checkpoint*: at checkpoint_at (default compact_at - 0.10) the agent is asked to save its state to
the workspace (PLAN.md, knowledge/, tools/), so the summary only has to carry the work in flight. The texts are files
(system/voyager/CHECKPOINT.md, COMPACT.md, AFTER_COMPACT.md); see ../compaction.py for the generic part.
"""

from __future__ import annotations

CHECKPOINT_GAP = 0.10  # default checkpoint_at = compact_at - this
HARD_GAP = 0.15  # a pending checkpoint may delay compaction up to compact_at + this (at most 95% of the window)
