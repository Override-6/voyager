#!/usr/bin/env python3
"""Tiny stdio MCP server for tests."""
import os
import time

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("fake")
open(os.environ.get("FAKE_MCP_STARTED", os.devnull), "a").write("started\n")  # lets tests see *when* it launched


@mcp.tool()
def echo(text: str, times: int = 1) -> str:
    """Echo the text back."""
    return " ".join([text] * times)


@mcp.tool()
def slow(seconds: float) -> str:
    """Sleep, then return."""
    time.sleep(seconds)
    return "woke"


@mcp.tool()
def fail() -> str:
    """Always raises."""
    raise ValueError("nope")


if __name__ == "__main__":
    if port := os.environ.get("FAKE_MCP_HTTP_PORT"):
        mcp.run(transport="streamable-http", host="127.0.0.1", port=int(port))
    else:
        mcp.run()
