"""Model-facing agent tools (main agent only): spawn_agent, send_message, list_agents."""

from __future__ import annotations

from typing import Any

from .base import Tool, ToolContext, ToolError


class SpawnAgent(Tool):
    def __init__(self) -> None:
        super().__init__(
            name="spawn_agent",
            description=(
                "Start a background agent for a self-contained task. Returns immediately; you are "
                "notified automatically when it finishes (do not poll). type='local' is a local "
                "worker with your tools; type='coder' is a cloud coding model in an empty scratch "
                "directory that cannot see the project (obey the privacy rule for the Coder)."
            ),
            properties={
                "name": {"type": "string", "description": "Short kebab-case label, e.g. 'find-auth-code'"},
                "prompt": {
                    "type": "string",
                    "description": "Complete, self-contained task description. The agent cannot see this conversation.",
                },
                "type": {"type": "string", "enum": ["local", "coder"], "description": "Default: local"},
            },
            required=["name", "prompt"],
        )

    def summary(self, args: dict[str, Any]) -> str:
        return f"{args.get('type', 'local')}: {args.get('name', '')}"

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        kind = str(args.get("type") or "local")
        child = ctx.session.spawn(ctx.agent, str(args["name"]), str(args["prompt"]), kind)
        return (
            f"Spawned {kind} agent {child.id} ({child.name}); it is running in the background. "
            "You will be notified when it finishes. Do not poll: continue with other work or end your turn."
        )


class SendMessage(Tool):
    def __init__(self) -> None:
        super().__init__(
            name="send_message",
            description=(
                "Send a message to an existing agent. If it has finished, this resumes it with its full "
                "history; if it is still running, the message is delivered at its next step."
            ),
            properties={
                "agent_id": {"type": "string", "description": "Agent id (e.g. 'a1') or name"},
                "message": {"type": "string", "description": "What to tell the agent"},
            },
            required=["agent_id", "message"],
        )

    def summary(self, args: dict[str, Any]) -> str:
        return str(args.get("agent_id", ""))

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        target = ctx.session.resolve(str(args["agent_id"]))
        if target is None or target is ctx.agent:
            raise ToolError(f"no such agent {args['agent_id']!r}. Use list_agents.")
        was_running = target.running
        target.submit(str(args["message"]), src=ctx.agent.id)
        return f"Delivered to {target.id}; " + ("it is running, it will see it at its next step." if was_running else "it was resumed.")


class ListAgents(Tool):
    def __init__(self) -> None:
        super().__init__(
            name="list_agents",
            description="List agents with their type and status (running / done / error / stopped).",
            properties={},
        )

    def summary(self, args: dict[str, Any]) -> str:
        return ""

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        rows = [
            f"{a.id}  {a.name}  type={a.kind}  status={a.status}  tool_calls={a.tool_count}"
            for a in ctx.session.agents.values()
            if not a.is_main
        ]
        return "\n".join(rows) or "No sub-agents yet."


AGENT_TOOLS: list[Tool] = [SpawnAgent(), SendMessage(), ListAgents()]
