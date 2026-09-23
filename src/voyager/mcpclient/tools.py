"""Wrap MCP tools as regular agent tools. Started lazily: the server launches on the first *call*."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

from mcp import types

from ..tools.base import Tool, ToolContext, ToolError, clip
from .server import McpServer, ToolDef

MAX_DESCRIPTION = 500
SUMMARY_KEYS = ("url", "query", "selector", "ref", "text", "expression", "path", "name")
_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}


def tool_name(server: str, tool: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", f"{server}__{tool}")[:64]


def schema_of(d: ToolDef) -> dict[str, Any]:
    s = dict(d.get("input_schema") or {})
    s.setdefault("type", "object")
    s.setdefault("properties", {})
    return s


class McpTool(Tool):
    def __init__(self, server: McpServer, d: ToolDef, out_dir: Path) -> None:
        self.server, self.tool, self.out_dir = server, d["name"], out_dir
        desc = (d.get("description") or "").strip()
        desc = desc if len(desc) <= MAX_DESCRIPTION else desc[:MAX_DESCRIPTION] + "…"
        schema = schema_of(d)
        super().__init__(
            name=tool_name(server.name, d["name"]),
            description=f"[MCP server '{server.name}'] {desc}",
            properties=schema["properties"],
            required=schema.get("required", []),
            raw_schema=schema,
        )

    def summary(self, args: dict[str, Any]) -> str:
        for k in SUMMARY_KEYS:
            if isinstance(args.get(k), (str, int)):
                return str(args[k]).splitlines()[0][:120]
        return ", ".join(f"{k}={str(v)[:30]}" for k, v in list(args.items())[:2])

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        if not self.server.connected:
            ctx.emit(f"starting MCP server '{self.server.name}'…")
        res = await self.server.call(self.tool, args)
        text = self._flatten(res)
        if res.is_error:
            raise ToolError(text or "the MCP tool reported an error")
        return text or "(no output)"

    def _flatten(self, res: types.CallToolResult) -> str:
        parts: list[str] = []
        for c in res.content:
            if isinstance(c, types.TextContent):
                parts.append(c.text)
            elif isinstance(c, types.ImageContent):  # the local model is text-only: keep the image on disk
                ext = _EXT.get(c.mime_type, "bin")
                self.out_dir.mkdir(parents=True, exist_ok=True)
                path = self.out_dir / f"{self.server.name}-{len(list(self.out_dir.glob('*')))}.{ext}"
                path.write_bytes(base64.b64decode(c.data))
                parts.append(f"[image saved to {path}]")
            else:
                parts.append(f"[{c.type} content omitted]")
        if not parts and res.structured_content:
            parts.append(json.dumps(res.structured_content, ensure_ascii=False))
        return clip("\n".join(parts))


class McpConnect(Tool):
    """Shown only for a server whose tool schemas are not cached yet (first ever use)."""

    def __init__(self, server: McpServer) -> None:
        self.server = server
        super().__init__(
            name=f"connect_{server.name}",
            description=f"Start the MCP server '{server.name}' and load its tools. Call it once, then use the tools it adds.",
            properties={},
        )

    def summary(self, args: dict[str, Any]) -> str:
        return self.server.name

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        ctx.emit(f"starting MCP server '{self.server.name}'…")
        await self.server.ensure()
        names = ", ".join(tool_name(self.server.name, d["name"]) for d in self.server.tool_defs)
        return f"Connected. New tools available now: {names}"
