"""Per-agent voyager behaviour: rounds and checkpoints, the workspace snapshot, the pinned method, the nudge."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..config import load_system_prompt
from ..mode import AgentMode
from . import progress
from .approach import gate_text
from .compaction import CHECKPOINT_GAP, HARD_GAP
from .evidence import record
from .nudge import maybe_nudge
from .prompts import method_prompt, mission_prompt
from .state import state_block
from .watch import changed, changed_note, fingerprint

if TYPE_CHECKING:
    from ..agent import Agent
    from .workspace import Workspace


class VoyagerAgentMode(AgentMode):
    name = "voyager"
    RESUME_TEXT = "Your workspace state in the system prompt is current: trust PLAN.md over your memory of it, and take the first Now item."

    def __init__(self, agent: "Agent", ws: "Workspace") -> None:
        super().__init__(agent)
        self.ws = ws
        self.baseline: dict[str, str] | None = fingerprint(ws)  # key files as the current round started
        self._stopped: dict[str, str] | None = None  # key files as the session last saved (None: an old save, or new)
        self.round = 0  # compactions so far (one "round" of work per context)
        self.checkpointed = False  # the <checkpoint> request was sent this round
        self.state: str | None = None  # workspace state snapshot in the system prompt (None: take a new one)
        self.nudges = 0  # automatic "continue" messages since the user's last message
        self.idle_nudges = 0  # ... in a row without any workspace progress
        self._head = ""  # git HEAD at the start of the turn
        self._round_end_commit = ""  # workspace HEAD right after the last compaction's commit

    @property
    def prompt_name(self) -> str:  # type: ignore[override]
        return "MISSION" if self.agent.is_main else "LOCAL"

    # ---------------------------------------------------------------- prompts
    def _prompt(self, name: str, **extra: str) -> str:
        return load_system_prompt(self.agent.session.cfg, name, extra=extra).strip()

    @property
    def compact_instructions(self) -> str:  # type: ignore[override]
        return self._prompt("voyager/COMPACT")

    @property
    def summary_note(self) -> str:  # type: ignore[override]
        """Read before `after_compact` resets the baseline: names the key files edited during the round that ended."""
        note = changed_note(self.agent.session.cfg, changed(self.baseline, fingerprint(self.ws)))
        return "\n\n".join(p for p in (self._prompt("voyager/AFTER_COMPACT"), note) if p)

    def resume_note(self) -> str:
        """The files the user (or anything else) edited while the session was stopped."""
        note = changed_note(self.agent.session.cfg, self._changed_while_stopped())
        return " ".join(p for p in (self.RESUME_TEXT, note) if p)

    def _changed_while_stopped(self) -> list[str]:
        return changed(self._stopped, fingerprint(self.ws))

    def system_prompt(self) -> str:
        a = self.agent
        if self.state is None:  # a snapshot, not live: a stable prompt prefix between refreshes
            self.state = state_block(self.ws, main=a.is_main, round_no=self.round)
        return mission_prompt(a.session.cfg, self.ws, self.state, main=a.is_main, agent_id=a.id, agent_name=a.name)

    def pinned(self) -> str:
        """The method (METHOD.md) rides in the first user message of the main agent, where the model follows it best."""
        a = self.agent
        return method_prompt(a.session.cfg, self.ws, agent_id=a.id, agent_name=a.name) if a.is_main else ""

    # ------------------------------------------------------------------ state
    def extra_state(self) -> dict[str, Any]:
        """`files` is the round's baseline, `files_saved` the key files right now (what a resume compares against)."""
        return {"round": self.round, "checkpointed": self.checkpointed, "files": self.baseline, "files_saved": fingerprint(self.ws)}

    def load_extra_state(self, extra: dict[str, Any]) -> None:
        self.round = extra.get("round", 0)
        self.checkpointed = extra.get("checkpointed", False)
        if extra.get("files") is not None:  # saved by an older version otherwise: keep today's files, flag nothing
            self.baseline = extra["files"]
        self._stopped = extra.get("files_saved")

    def wants_continue(self) -> bool:
        """The plan is unfinished (or phase 0 has not chosen its approach yet) and nothing is waiting on the user.

        Key files edited while the session was stopped also count, even past a Blocked section: that edit is likely the answer."""
        a, ws = self.agent, self.ws
        if not a.is_main:
            return False
        return bool(self._changed_while_stopped()) or (
            not ws.blockers() and bool(ws.unfinished() or (not ws.phases() and gate_text(ws))))

    def on_submit(self, src: str) -> None:
        if src == "user":  # the user is steering again: the nudge budget starts over
            self.nudges = self.idle_nudges = 0

    def on_message(self) -> None:
        self.state = None  # a new message (user or notification): new workspace snapshot

    # ------------------------------------------------------------- compaction
    def defer_compaction(self, estimate: int, worth_it: bool) -> bool:
        """Checkpoint first: once per round, ask the agent to save its state, and let it do so before summarizing."""
        a, cfg = self.agent, self.agent.session.cfg
        if self.checkpointed or not worth_it:
            return False
        at = cfg.checkpoint_at if cfg.checkpoint_at is not None else max(0.0, cfg.compact_at - CHECKPOINT_GAP)
        if estimate < cfg.context_window * at or a.messages[-1]["role"] != "user":
            return False
        self._send_checkpoint()
        hard = int(cfg.context_window * min(0.95, cfg.compact_at + HARD_GAP))
        return not a.compact_requested and estimate < hard

    def _send_checkpoint(self) -> None:
        """Append the <checkpoint> request to the pending user message (usually tool results)."""
        last = self.agent.messages[-1]
        if isinstance(last["content"], str):
            last["content"] = [{"type": "text", "text": last["content"]}]
        last["content"].append({"type": "text", "text": self._prompt("voyager/CHECKPOINT", round=str(self.round))})
        self.checkpointed = True
        self.agent.log.add("notice", "⚑ checkpoint: asked the agent to save its state to the workspace before compaction",
                           event="checkpoint")

    async def after_compact(self) -> None:
        """End of a round: commit the workspace, start the next round with a fresh snapshot."""
        self.round += 1
        self.checkpointed = False
        self.state = None
        self.baseline = fingerprint(self.ws)
        await self.ws.commit(f"{self.agent.name} r{self.round - 1} compacted: {self.ws.current_phase()}")
        self._round_end_commit = await self.ws.head()
        if self.agent.is_main:
            await self._measure_progress()

    async def _measure_progress(self) -> None:
        """Run the progress probe for the round that ended; after rounds without progress, open the next with a review."""
        a = self.agent
        row = await progress.measure(self.ws, self.round - 1)
        if row["line"]:  # a probe is defined
            shown = progress.fmt(row["value"]) if row["value"] is not None else f"probe failed ({row['line']})"
            a.log.add("notice", f"◆ progress r{row['round']}: {shown}", event="progress", value=row["value"])
        if not row["review"]:
            return
        text = self._prompt("voyager/STALL", rounds=str(progress.STALL_ROUNDS), trend=progress.trend(progress.history(self.ws)),
                            now=row["now"] or "(none)")
        first = a.messages[0]
        if isinstance(first["content"], str):
            first["content"] = [{"type": "text", "text": first["content"]}]
        first["content"].append({"type": "text", "text": text})
        a.log.add("notice", f"⚠ no progress for {progress.STALL_ROUNDS} rounds: stall review requested", event="stall")

    def round_info(self) -> dict[str, Any]:
        """Where the mission stood when the round ended: phase, the workspace commit, and PLAN.md as it was."""
        return {"phase": self.ws.current_phase(), "commit": self._round_end_commit, "plan": self.ws.read("PLAN.md")}

    def on_compacted(self) -> None:
        self.agent.log.add("notice", f"◆ round {self.round} begins · {self.ws.current_phase()}", event="round")

    # ------------------------------------------------------------------- turn
    async def turn_start(self) -> None:
        self._head = await self.ws.head()

    async def turn_end(self, had_message: bool) -> None:
        a = self.agent
        await self.ws.commit(f"{a.name} r{self.round} turn end: {self.ws.current_phase()}")
        if a.is_main and had_message:
            maybe_nudge(self, progressed=await self.ws.head() != self._head)

    def record_tool(self, name: str, args: dict[str, Any], output: str) -> None:
        record(self.ws, name, args, output)
