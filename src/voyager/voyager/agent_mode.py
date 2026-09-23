"""Per-agent voyager behaviour: rounds and checkpoints, the workspace snapshot, the pinned method, the nudge."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..mode import AgentMode
from .approach import gate_text
from .compaction import CHECKPOINT, CHECKPOINT_GAP, HARD_GAP, WORKSPACE_COMPACT_INSTRUCTIONS, WORKSPACE_NOTE
from .evidence import record
from .nudge import maybe_nudge
from .prompts import method_prompt, mission_prompt
from .state import state_block

if TYPE_CHECKING:
    from ..agent import Agent
    from .workspace import Workspace


class VoyagerAgentMode(AgentMode):
    name = "voyager"
    compact_instructions = WORKSPACE_COMPACT_INSTRUCTIONS
    summary_note = WORKSPACE_NOTE
    resume_note = "Your workspace state in the system prompt is current: trust PLAN.md over your memory of it, and take the first Now item."

    def __init__(self, agent: "Agent", ws: "Workspace") -> None:
        super().__init__(agent)
        self.ws = ws
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
        return {"round": self.round, "checkpointed": self.checkpointed}

    def load_extra_state(self, extra: dict[str, Any]) -> None:
        self.round = extra.get("round", 0)
        self.checkpointed = extra.get("checkpointed", False)

    def wants_continue(self) -> bool:
        """The plan is unfinished (or phase 0 has not chosen its approach yet) and nothing is waiting on the user."""
        a, ws = self.agent, self.ws
        return a.is_main and not ws.section("## Blocked") and (ws.unfinished() or (not ws.phases() and bool(gate_text(ws))))

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
        last["content"].append({"type": "text", "text": CHECKPOINT.replace("{round}", str(self.round))})
        self.checkpointed = True
        self.agent.log.add("notice", "⚑ checkpoint: asked the agent to save its state to the workspace before compaction",
                           event="checkpoint")

    async def after_compact(self) -> None:
        """End of a round: commit the workspace, start the next round with a fresh snapshot."""
        self.round += 1
        self.checkpointed = False
        self.state = None
        await self.ws.commit(f"{self.agent.name} r{self.round - 1} compacted: {self.ws.current_phase()}")
        self._round_end_commit = await self.ws.head()

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
