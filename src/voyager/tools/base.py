"""Tool base class. A tool = JSON schema for the model + a Python `run` + UI hooks."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

MAX_RESULT_CHARS = 20_000


class ToolError(Exception):
    """Raised by a tool for an expected failure; the message goes back to the model."""


@dataclass
class ToolContext:
    cwd: Path
    # Live output channel (e.g. bash stdout lines) so the UI can show progress mid-run.
    emit: Callable[[str], None] = lambda _line: None
    agent: Any = None  # the Agent running this tool (agent tools need it)
    session: Any = None

    def resolve(self, path: str) -> Path:
        p = Path(path).expanduser()
        return (p if p.is_absolute() else self.cwd / p).resolve()


@dataclass
class Tool:
    name: str
    description: str
    properties: dict[str, Any]
    required: list[str] = field(default_factory=list)
    streams_output: bool = False  # run() already showed its output through ctx.emit
    backgroundable: bool = False  # accepts run_in_background and may be auto-backgrounded
    raw_schema: dict[str, Any] | None = None  # full JSON schema, used as-is (MCP tools)

    def schema(self) -> dict[str, Any]:
        if self.raw_schema is not None:
            return {"name": self.name, "description": self.description, "input_schema": self.raw_schema}
        props = dict(self.properties)
        if self.backgroundable:
            props["run_in_background"] = {
                "type": "boolean",
                "description": "Run in the background and get notified when it finishes. Use for long-running "
                "commands (builds, installs, test suites). Calls taking over 30s are backgrounded automatically.",
            }
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": {
                "type": "object",
                "properties": props,
                "required": self.required,
            },
        }

    # -- to override -------------------------------------------------------
    async def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        raise NotImplementedError

    def summary(self, args: dict[str, Any]) -> str:
        """One-line description of the call, shown as `name(summary)`."""
        return ", ".join(f"{k}={str(v)[:60]!r}" for k, v in args.items())

    def preview(self, args: dict[str, Any], ctx: ToolContext) -> str | None:
        """Optional diff/preview text shown before asking permission."""
        return None

    # -- helpers -----------------------------------------------------------
    def check_args(self, args: dict[str, Any]) -> None:
        if not isinstance(args, dict):
            raise ToolError(f"arguments must be a JSON object, got {type(args).__name__}")
        missing = [k for k in self.required if k not in args]
        if missing:
            raise ToolError(f"missing required argument(s): {', '.join(missing)}")


def clip(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [truncated {len(text) - limit} chars]"
