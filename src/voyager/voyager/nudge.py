"""The "keep going" nudge: a main agent that ends its turn with an unfinished plan is sent back to work.

Bounded on purpose: per-user-message budget (`max_nudges`) and no second nudge in a row without workspace progress.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .approach import gate_text

if TYPE_CHECKING:
    from .agent_mode import VoyagerAgentMode

NUDGE = (
    "<continue>\nYou ended your turn, but the objective is not reached: PLAN.md has unfinished phases (current: {phase}), "
    "nothing is under Blocked, and no agent or task is running. Keep working: continue with the first Now item. "
    "If the objective is in fact reached, verify every Definition-of-done criterion, mark all phases [x] and write the "
    "final report; if you need the user, write it under Blocked. Only then end your turn.\n</continue>"
)


def maybe_nudge(m: "VoyagerAgentMode", progressed: bool) -> None:
    """The main agent ended its turn: if the plan is unfinished and nothing explains the stop, send it back to work.

    Not when: the plan is finished, something is under Blocked, a sub-agent or background task is still running (their
    notification will wake it), the user queued a message, the per-message budget is spent, or the previous nudge produced
    no workspace change (a stuck agent must not loop). An empty plan (phase 0) counts as unfinished while the approach
    gate is open.
    """
    a, ws, session = m.agent, m.ws, m.agent.session
    m.idle_nudges = 0 if progressed else m.idle_nudges
    busy = any(x.running for x in session.agents.values() if x is not a) or session.tasks.running()
    gate = gate_text(ws)
    if (a.inbox or busy or not (ws.unfinished() or (not ws.phases() and gate)) or ws.section("## Blocked")
            or m.nudges >= session.cfg.max_nudges or m.idle_nudges >= 1):
        return
    m.nudges += 1
    m.idle_nudges += 1
    a.submit(NUDGE.format(phase=ws.current_phase()) + ("\n" + gate if gate else ""), src="agent",
             shown=f"↻ plan unfinished ({ws.current_phase()}): continuing [{m.nudges}/{session.cfg.max_nudges}]")
