import json
import sys
from pathlib import Path

import pytest

from conftest import run
from voyager.mcpclient.config import load_config
from voyager.session import Session
from voyager.tools import ToolContext, ToolError

FAKE = str(Path(__file__).parent / "fake_mcp.py")


@pytest.fixture
def mcp_cfg(cfg, tmp_path):
    marker = tmp_path / "started"
    conf = tmp_path / "mcp.json"
    conf.write_text(json.dumps({"mcpServers": {"fake": {"command": sys.executable, "args": [FAKE],
                                                       "env": {"FAKE_MCP_STARTED": str(marker)}}}}))
    cfg.mcp_config, cfg.home = conf, tmp_path / "home"
    return cfg, marker


def _ctx(s):
    return ToolContext(cwd=s.cfg.cwd, session=s, agent=s.main)


def started(marker: Path) -> int:
    return len(marker.read_text().splitlines()) if marker.exists() else 0


def test_lazy_start_bootstrap_then_cached_schemas(mcp_cfg):
    cfg, marker = mcp_cfg

    async def first_run():
        s = Session(cfg)
        assert started(marker) == 0  # creating the session starts nothing
        assert list(s.mcp.tools()) == ["connect_fake"]  # no cached schemas yet: only the bootstrap tool
        out = await s.mcp.tools()["connect_fake"].run({}, _ctx(s))
        assert "fake__echo" in out and started(marker) == 1
        assert {"fake__echo", "fake__slow", "fake__fail"} <= set(s.mcp.tools())  # real tools appear right away
        await s.shutdown()
    run(first_run())
    assert (cfg.home / "mcp-cache" / "fake.json").is_file()

    async def second_run():
        marker.unlink()
        s = Session(cfg)
        tools = s.mcp.tools()
        assert "fake__echo" in tools and "connect_fake" not in tools  # schemas come from the cache...
        assert started(marker) == 0  # ...so the server is still NOT running
        assert tools["fake__echo"].schema()["input_schema"]["required"] == ["text"]
        res = await tools["fake__echo"].run({"text": "hi", "times": 2}, _ctx(s))  # first call starts it
        assert res == "hi hi" and started(marker) == 1
        assert await tools["fake__echo"].run({"text": "again"}, _ctx(s)) == "again" and started(marker) == 1  # reused
        await s.shutdown()
    run(second_run())


def test_tool_errors_become_tool_errors(mcp_cfg):
    cfg, _ = mcp_cfg

    async def go():
        s = Session(cfg)
        await s.mcp.tools()["connect_fake"].run({}, _ctx(s))
        tools = s.mcp.tools()
        with pytest.raises(ToolError, match="fail"):
            await tools["fake__fail"].run({}, _ctx(s))
        await s.shutdown()
    run(go())


def test_bad_server_reports_error_and_can_retry(cfg, tmp_path):
    conf = tmp_path / "mcp.json"
    conf.write_text(json.dumps({"mcpServers": {"bad": {"command": "/nonexistent/server", "startupTimeout": 5}}}))
    cfg.mcp_config, cfg.home = conf, tmp_path / "home"

    async def go():
        s = Session(cfg)
        with pytest.raises(ToolError, match="failed to start"):
            await s.mcp.tools()["connect_bad"].run({}, _ctx(s))
        with pytest.raises(ToolError, match="failed to start"):  # not wedged: the next call tries again
            await s.mcp.tools()["connect_bad"].run({}, _ctx(s))
        await s.shutdown()
    run(go())


def test_config_parsing(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "abc")
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"mcpServers": {
        "a b": {"command": "x", "args": ["--k", "${MY_TOKEN}"], "env": {"T": "${NOPE:-dflt}"}},
        "remote": {"url": "http://h/mcp", "headers": {"Authorization": "Bearer ${MY_TOKEN}"}},
        "off": {"command": "y", "disabled": True},
        "broken": {"args": []},
    }}))
    servers, problems = load_config(p)
    by = {s.name: s for s in servers}
    assert set(by) == {"a_b", "remote"} and by["a_b"].args == ["--k", "abc"] and by["a_b"].env == {"T": "dflt"}
    assert by["remote"].headers["Authorization"] == "Bearer abc"
    assert len(problems) == 1 and "broken" in problems[0]
    assert load_config(tmp_path / "missing.json") == ([], [])
    assert load_config(p.with_name("bad.json"))[1] == []
    (tmp_path / "bad.json").write_text("{nope")
    assert "bad.json" in load_config(tmp_path / "bad.json")[1][0]
