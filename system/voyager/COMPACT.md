Summarize the conversation above for the agent that will continue it. Its durable state (OBJECTIVE.md, PLAN.md, tools/, knowledge/) is on disk and shown to it separately: do NOT restate the plan, the tool list or facts already written to knowledge/. Carry only the work in flight, thoroughly but without padding (under 2000 words in total). Write settled facts and observed results, never reasoning: no recalculation, no "wait" or "actually"; an unresolved point is one line under Open threads.
1. Latest user request: quote it verbatim.
2. Current step: which PLAN.md "Now" item was being worked on, and exactly where it stopped.
3. Not yet saved: results, values, errors and dead ends from this context that are NOT in the workspace files (say where they should go).
4. Open threads: hypotheses being tested, agents, background tasks (ids a1, t1, ...) and live REPLs (names, what they hold: connections, loaded skills) and what they are for.
5. Next action: the exact next tool call (tool and arguments), ready to make.
