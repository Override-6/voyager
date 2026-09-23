"""Session daemons: the server pushes a session's changes to attached clients, whose replica behaves like a session."""
import asyncio
import dataclasses
import json
import os
from pathlib import Path

import pytest

from voyager.local import LocalAgent
from voyager.daemon import protocol, server as server_mod
from voyager.daemon.client import RemoteSession
from voyager.daemon.launcher import config_from_json, config_to_json, live_sessions
from voyager.daemon.server import SessionServer
from voyager.session import Session
from voyager.tasks import BackgroundTask


@pytest.fixture(autouse=True)
def fast_tick(monkeypatch):
    monkeypatch.setattr(server_mod, "TICK_SECS", 0.02)


async def attach(cfg, session=None):
    """A real session served on a socket, and a client attached to it."""
    s = session or Session(cfg)
    srv = SessionServer(s)
    await srv.start()
    rs = await RemoteSession(cfg, srv.path).connect()
    return s, srv, rs


async def settle(rs):
    """A round trip: the server wrote everything it had before answering, and the client applies messages in order."""
    await rs.request({"op": "status"})


async def close(s, srv, rs):
    rs.close()
    await srv.close()
    await s.shutdown()


def run(coro):
    return asyncio.run(coro)


def test_snapshot_carries_the_whole_transcript_and_live_events_follow(cfg):
    async def go():
        s = Session(cfg)
        s.main.log.add("user", "hello", src="user")
        tool = s.main.log.start("tool", name="bash", status="running", args={"command": "ls"})
        s.main.log.tool_live(tool, "line one")
        s, srv, rs = await attach(cfg, s)
        seen = []
        rs.hooks.append(lambda agent, ev, item, extra: seen.append((ev, item.kind)))
        before = rs.main.log.items
        assert [i.kind for i in before] == ["user", "tool"] and before[0].text == "hello"
        assert before[1].meta["live"] == ["line one"] and before[1].meta["args"] == {"command": "ls"}
        # live: streaming text, then the tool finishing
        text = s.main.log.start("text")
        s.main.log.delta(text, "ab")
        s.main.log.delta(text, "cd")
        s.main.log.end(text)
        s.main.log.tool_live(tool, "line two")
        s.main.log.update(tool, status="done", result="ok")
        await settle(rs)
        mirrored = rs.main.log.items
        assert [i.id for i in mirrored] == [i.id for i in s.main.log.items]
        assert mirrored[2].text == "abcd" and mirrored[2].meta["done"] is True
        assert mirrored[1].meta["live"] == ["line one", "line two"] and mirrored[1].meta["status"] == "done"
        assert ("start", "text") in seen and ("delta", "text") in seen and ("live", "tool") in seen
        assert mirrored[1].version > 0  # bumped on every change: the UI's render cache keys on it
        await close(s, srv, rs)
    run(go())


def test_state_flows_and_requests_reach_the_real_session(cfg, offline):
    async def go():
        s, srv, rs = await attach(cfg)
        s.main.activity = "bash(ls)"
        await asyncio.sleep(0.1)  # a state tick
        assert rs.main.activity == "bash(ls)" and rs.main.status == "idle" and rs.mode.name == "chat"
        rs.main.submit("do the thing")
        await settle(rs)
        await s.wait_idle()
        assert offline == [("main", "do the thing")]
        rs.set_thinking(7)
        await settle(rs)
        assert s.cfg.thinking_budget == 7 and rs.cfg.thinking_budget == 7
        s.main.log.add("user", "x", src="user")
        rs.reset()
        await settle(rs)
        assert rs.main.log.items == [] and s.main.log.items == []  # /clear re-syncs the replica
        await close(s, srv, rs)
    run(go())


def test_sub_agents_and_background_tasks_are_replicated(cfg, offline):
    async def go():
        s, srv, rs = await attach(cfg)
        sub = LocalAgent(s, "a1", "worker", parent_id="main", prompt="p")
        s.agents["a1"] = sub
        sub.log.add("user", "task for a1", src="main")
        sub.running = True
        item = s.main.log.start("tool", name="bash", status="background")
        s.tasks.tasks["t1"] = BackgroundTask("t1", "main", "bash", "sleep 9", item)
        s.main.log.tool_live(item, "tick")
        await asyncio.sleep(0.1)
        a1 = rs.agents["a1"]
        assert a1.name == "worker" and a1.parent_id == "main" and a1.running and not a1.is_main
        assert [i.text for i in a1.log.items] == ["task for a1"] and rs.resolve("worker") is a1 and rs.resolve("a") is a1
        bt = rs.tasks.get("t1")
        assert bt.status == "running" and bt.tool == "bash" and bt.output() == "tick" and bt.item.meta["live"] == ["tick"]
        sub.running = False
        sub.outcome = "done"
        s.tasks.tasks["t1"].status, s.tasks.tasks["t1"].result = "done", "final"
        await asyncio.sleep(0.1)
        assert a1.unseen and not a1.running and rs.tasks.get("t1").output() == "final"
        assert rs.tasks.kill("t1") is False  # already over: nothing to send
        await close(s, srv, rs)
    run(go())


async def close_when_stopped(srv):
    """What runner.serve does once a client asked for the shutdown."""
    await srv.stopped.wait()
    await srv.close()


def test_leaving_detaches_when_busy_and_shuts_down_when_idle(cfg, offline):
    async def go():
        s, srv, rs = await attach(cfg)
        closer = asyncio.create_task(close_when_stopped(srv))
        s.main.running = True
        await asyncio.sleep(0.1)
        assert await rs.leave() == "detached" and not srv.stopped.is_set()  # work in progress: leave it running
        rs2 = await RemoteSession(cfg, srv.path).connect()
        s.main.running = False
        await asyncio.sleep(0.1)
        assert await rs2.leave() == "stopped" and srv.stopped.is_set()  # idle: the session is ended
        await closer
        await s.shutdown()
        # an explicit choice wins over what the session is doing
        s, srv, rs = await attach(cfg)
        assert await rs.leave(keep=True) == "detached" and not srv.stopped.is_set()
        await close(s, srv, rs)
    run(go())


def test_daemon_files_registry_and_stale_entries(cfg):
    async def go():
        s, srv, rs = await attach(cfg)
        info = json.loads((s.dir / "daemon.json").read_text())
        assert info["pid"] == os.getpid() and info["id"] == s.id and Path(info["sock"]) == srv.path
        assert [x.id for x in live_sessions(cfg.sessions_dir)] == [s.id]
        stale = cfg.sessions_dir / "old-one"
        stale.mkdir()
        (stale / "daemon.json").write_text(json.dumps({**info, "id": "old-one", "pid": 2 ** 22 + 3}))
        assert [x.id for x in live_sessions(cfg.sessions_dir)] == [s.id]  # its process is gone
        await close(s, srv, rs)
        assert not (s.dir / "daemon.json").exists() and not srv.path.exists()
        assert live_sessions(cfg.sessions_dir) == []
    run(go())


def test_idle_daemon_exits_by_itself(cfg):
    async def go():
        s = Session(cfg)
        srv = SessionServer(s, idle_exit=0.1)
        await srv.start()
        await asyncio.wait_for(srv.stopped.wait(), 3)  # no client, no work
        await srv.close()
        s2 = Session(cfg)
        srv2 = SessionServer(s2, idle_exit=0.1)
        await srv2.start()
        rs = await RemoteSession(cfg, srv2.path).connect()
        await asyncio.sleep(0.4)
        assert not srv2.stopped.is_set()  # somebody is attached
        await close(s2, srv2, rs)
    run(go())


def test_config_survives_the_json_file_and_long_socket_paths_fall_back(cfg, tmp_path):
    cfg = dataclasses.replace(cfg, coder_cwd=tmp_path / "coder", max_nudges=3, compact_at=0.5)
    back = config_from_json(config_to_json(cfg))
    assert back == cfg and isinstance(back.coder_cwd, Path)
    short = tmp_path / "s"
    assert protocol.sock_path(short) == short / "daemon.sock"
    long = tmp_path / ("x" * 120)
    p = protocol.sock_path(long)
    assert len(str(p)) < 108 and p.name.startswith("voyager-") and p == protocol.sock_path(long)
