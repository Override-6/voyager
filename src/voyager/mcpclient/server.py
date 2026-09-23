"""One MCP server connection, started LAZILY on the first tool call.

The connection lives in a dedicated asyncio task (the SDK's anyio scopes must be entered and exited in the
same task); tool calls from any agent's task go through the shared ClientSession. If the server dies, the
next call simply starts it again.
"""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Callable

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from ..tools.base import ToolError
from .config import ServerConfig

ToolDef = dict[str, Any]  # {"name", "description", "input_schema"}


def describe_error(e: BaseException) -> str:
    """Flatten anyio ExceptionGroups into one readable line."""
    if isinstance(e, BaseExceptionGroup):
        return "; ".join(describe_error(x) for x in e.exceptions)
    return f"{type(e).__name__}: {e}" if str(e) else type(e).__name__


class McpServer:
    def __init__(self, cfg: ServerConfig, log_path: Path) -> None:
        self.cfg = cfg
        self.log_path = log_path  # the server's stderr goes here, never to the terminal (it would corrupt the TUI)
        self.tool_defs: list[ToolDef] = []  # from the on-disk cache until the first live connection
        self.from_cache = False
        self.on_tools: Callable[[], None] | None = None  # called whenever a live connection (re)learns the tools
        self._session: ClientSession | None = None
        self._runner: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[None] | None = None
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return self.cfg.name

    @property
    def connected(self) -> bool:
        return self._session is not None

    # ------------------------------------------------------------------ connect
    async def ensure(self) -> ClientSession:
        async with self._lock:
            if self._session is not None:
                return self._session
            loop = asyncio.get_running_loop()
            self._ready, self._stop = loop.create_future(), asyncio.Event()
            self._runner = loop.create_task(self._run(self._ready, self._stop))
            try:
                await asyncio.wait_for(asyncio.shield(self._ready), self.cfg.startup_timeout)
            except asyncio.TimeoutError:
                await self._abort()
                raise ToolError(f"MCP server '{self.name}' did not start within {self.cfg.startup_timeout:.0f}s (see {self.log_path})")
            except asyncio.CancelledError:
                await self._abort()
                raise
            except Exception as e:
                await self._abort()
                raise ToolError(f"MCP server '{self.name}' failed to start: {describe_error(e)} (see {self.log_path})")
            assert self._session is not None
            return self._session

    async def _run(self, ready: "asyncio.Future[None]", stop: asyncio.Event) -> None:
        errlog = None
        try:
            async with AsyncExitStack() as stack:
                if self.cfg.url:
                    client = create_mcp_http_client(headers=self.cfg.headers or None)
                    streams = await stack.enter_async_context(streamable_http_client(self.cfg.url, http_client=client))
                else:
                    self.log_path.parent.mkdir(parents=True, exist_ok=True)
                    errlog = stack.enter_context(open(self.log_path, "a", encoding="utf-8"))
                    params = StdioServerParameters(
                        command=self.cfg.command, args=self.cfg.args, cwd=self.cfg.cwd,
                        env={**os.environ, **self.cfg.env},  # full env (DISPLAY, PATH, ...) + the config's overrides
                    )
                    streams = await stack.enter_async_context(stdio_client(params, errlog=errlog))
                session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
                await session.initialize()
                self.tool_defs = await self._list_tools(session)
                self.from_cache = False
                if self.on_tools:
                    self.on_tools()
                self._session = session
                if not ready.done():
                    ready.set_result(None)
                await stop.wait()
        except BaseException as e:
            if not ready.done():
                if isinstance(e, asyncio.CancelledError):
                    ready.cancel()
                else:
                    ready.set_exception(e if isinstance(e, Exception) else RuntimeError(describe_error(e)))
            if isinstance(e, asyncio.CancelledError):
                raise
        finally:
            self._session = None

    @staticmethod
    async def _list_tools(session: ClientSession) -> list[ToolDef]:
        defs: list[ToolDef] = []
        cursor: str | None = None
        while True:
            page = await session.list_tools(cursor=cursor) if cursor else await session.list_tools()
            defs += [{"name": t.name, "description": t.description or "", "input_schema": t.input_schema} for t in page.tools]
            cursor = getattr(page, "next_cursor", None)
            if not cursor:
                return defs

    async def _abort(self) -> None:
        if self._runner and not self._runner.done():
            self._runner.cancel()
            await asyncio.gather(self._runner, return_exceptions=True)

    # -------------------------------------------------------------------- calls
    async def call(self, tool: str, args: dict[str, Any]) -> types.CallToolResult:
        session = await self.ensure()
        try:
            res = await session.call_tool(tool, args, read_timeout_seconds=self.cfg.timeout)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            raise ToolError(f"MCP call {self.name}.{tool} failed: {describe_error(e)}")
        if not isinstance(res, types.CallToolResult):
            raise ToolError(f"MCP server '{self.name}' asked for something this client does not support ({type(res).__name__})")
        return res

    async def close(self) -> None:
        if self._runner and not self._runner.done():
            self._stop.set()
            try:
                await asyncio.wait_for(asyncio.shield(self._runner), 5)
            except (asyncio.TimeoutError, Exception):
                await self._abort()
        self._session = None
