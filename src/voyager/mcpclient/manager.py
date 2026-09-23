"""All configured MCP servers for a session; tool-schema cache; dynamic tool list."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..tools.base import Tool
from .config import ServerConfig, load_config
from .server import McpServer, describe_error
from .tools import McpConnect, McpTool

if TYPE_CHECKING:
    from ..session import Session


class McpManager:
    def __init__(self, session: "Session") -> None:
        self.session = session
        self.servers: dict[str, McpServer] = {}
        self.problems: list[str] = []
        path = session.cfg.mcp_config
        if path is None:
            return
        configs, self.problems = load_config(path)
        for sc in configs:
            srv = McpServer(sc, session.dir / f"mcp-{sc.name}.log")
            self._load_cache(srv)
            srv.on_tools = lambda s=srv: self.save_cache(s)
            self.servers[sc.name] = srv

    # ------------------------------------------------------------------ tools
    def tools(self) -> dict[str, Tool]:
        """Recomputed on every model request: a server that connected since then contributes its real tools."""
        out: dict[str, Tool] = {}
        out_dir = self.session.dir / "mcp"
        for srv in self.servers.values():
            if srv.tool_defs:
                for d in srv.tool_defs:
                    t = McpTool(srv, d, out_dir)
                    out[t.name] = t
            else:
                t = McpConnect(srv)
                out[t.name] = t
        return out

    # ------------------------------------------------------------------ cache
    def _cache_path(self, cfg: ServerConfig) -> Path:
        return self.session.cfg.home / "mcp-cache" / f"{cfg.name}.json"

    def _load_cache(self, srv: McpServer) -> None:
        try:
            data = json.loads(self._cache_path(srv.cfg).read_text())
        except (OSError, json.JSONDecodeError):
            return
        if data.get("fingerprint") == srv.cfg.fingerprint():
            srv.tool_defs, srv.from_cache = data.get("tools", []), True

    def save_cache(self, srv: McpServer) -> None:
        try:
            path = self._cache_path(srv.cfg)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"fingerprint": srv.cfg.fingerprint(), "tools": srv.tool_defs}, indent=1))
        except OSError:
            pass

    async def refresh(self) -> list[tuple[str, str]]:
        """Connect to every server once, cache its tools, disconnect (`--mcp-refresh`)."""
        results: list[tuple[str, str]] = []
        for srv in self.servers.values():
            try:
                await srv.ensure()
                results.append((srv.name, f"{len(srv.tool_defs)} tools: " + ", ".join(d["name"] for d in srv.tool_defs)))
            except Exception as e:
                results.append((srv.name, f"FAILED: {describe_error(e)}"))
            await srv.close()
        return results

    async def shutdown(self) -> None:
        for srv in self.servers.values():
            await srv.close()

    def describe(self) -> list[dict[str, Any]]:
        return [
            {"name": s.name, "state": "running" if s.connected else "lazy (not started)",
             "tools": len(s.tool_defs), "cached": s.from_cache, "target": s.cfg.url or s.cfg.command}
            for s in self.servers.values()
        ]
