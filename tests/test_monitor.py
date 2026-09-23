"""Monitoring a session from outside: events.jsonl, run.json and `voyager --monitor`."""
import asyncio
import io
import json
import os
import threading
import time

from conftest import run
from voyager import cli, monitor
from voyager.session import Session


def events_of(s):
    return [json.loads(ln) for ln in (s.dir / "events.jsonl").read_text().splitlines()]


def drive_tool(s, name="bash", summary="ls", result="file.txt\nsecond", status="done", started=True):
    """What LocalAgent._execute does to the log for one tool call (started=False: rejected before it ran, like _fail)."""
    log = s.main.log
    item = log.start("tool", name=name, status="args", args={})
    log.update(item, summary=summary)
    if started:
        log.update(item, status="running")
    log.update(item, status=status, result=result)
    return item


def test_nothing_is_written_for_a_session_that_did_nothing(cfg):
    s = Session(cfg)
    run(s.shutdown())
    assert not s.dir.exists()


def test_events_and_run_json_are_written_live(cfg, offline):
    async def go():
        s = Session(cfg)
        s.main.submit("hello")
        drive_tool(s)
        drive_tool(s, name="read_file", summary="x.py", result="boom", status="error", started=False)
        s.main.log.add("usage", "↳ 5 in", ctx=31000, window=131000)
        s.main.log.add("text", "done")
        await s.wait_idle()
        return s

    s = asyncio.run(go())
    rows = events_of(s)  # not yet shut down: everything is already on disk
    assert [r["ev"] for r in rows][:2] == ["start", "user"] and rows[1]["text"] == "hello"
    ok, bad = [r for r in rows if r["ev"] == "tool_end"]
    assert ok["ok"] is True and ok["name"] == "bash" and ok["secs"] is not None and ok["result"].startswith("file.txt")
    assert bad["ok"] is False and "secs" not in bad and [r["ev"] for r in rows].count("tool_start") == 1
    assert any(r["ev"] == "usage" and r["ctx"] == 31000 for r in rows) and rows[-1]["ev"] == "turn_end"
    meta = json.loads((s.dir / "run.json").read_text())
    assert meta["pid"] == os.getpid() and meta["mode"] == "chat" and meta["workspace"] is None
    run(s.shutdown())
    assert events_of(s)[-1]["ev"] == "end"


def test_digest_header_and_events(cfg, offline, capsys):
    async def go():
        s = Session(cfg)
        s.main.submit("hello")
        drive_tool(s)
        await s.wait_idle()
        return s

    s = asyncio.run(go())
    out = io.StringIO()
    assert monitor.run_monitor(cfg.sessions_dir, None, out=out) == 0  # bare: the most recently active session
    text = out.getvalue()
    assert f"session {s.id} · mode chat" in text and "state: IDLE" in text and "1 tool calls (0 failed)" in text
    assert "❯ hello" in text and "✓ bash(ls)" in text and "⎿ file.txt" in text and "turn end" in text
    assert "tool_start" not in text and "⏺ bash(ls) …" not in text  # starts are noise unless following or --all
    out = io.StringIO()
    monitor.run_monitor(cfg.sessions_dir, s.id[:10], as_json=True, out=out)
    assert all(json.loads(ln)["ev"] for ln in out.getvalue().splitlines()) and "session " not in out.getvalue()
    out = io.StringIO()
    monitor.run_monitor(cfg.sessions_dir, s.id, show_all=True, out=out)
    assert "⏺ bash(ls) …" in out.getvalue()
    assert monitor.run_monitor(cfg.sessions_dir, "nope") == 2 and "no session with events" in capsys.readouterr().err


def test_state_finished_dead_and_in_flight(cfg):
    now = time.time()
    ev = lambda **k: {"ts": now, "agent": "main", **k}  # noqa: E731
    me = {"pid": os.getpid()}
    assert monitor.state_line(me, [ev(ev="end", outcome="done")], now).startswith("FINISHED")
    assert monitor.state_line({"pid": 2 ** 22 + 1}, [ev(ev="text")], now).startswith("DEAD")
    running = monitor.state_line(me, [ev(ev="tool_start", id=3, name="bash", summary="sleep 9")], now)
    assert running.startswith("RUNNING") and "in flight: bash(sleep 9)" in running
    done = [ev(ev="tool_start", id=3, name="bash", summary="x"), ev(ev="tool_end", id=3, name="bash")]
    assert "in flight" not in monitor.state_line(me, done, now)


def test_a_long_generation_shows_up_as_progress_not_as_a_hang(cfg, monkeypatch):
    from voyager import events
    monkeypatch.setattr(events, "GEN_EVERY", 0.0)
    s = Session(cfg)
    item = s.main.log.start("text")
    s.main.log.delta(item, "planning the ")
    s.main.log.delta(item, "whole mission")
    rows = events_of(s)
    assert [r["ev"] for r in rows] == ["start", "gen_start", "gen", "gen"] and rows[-1]["chars"] == len("planning the whole mission")
    state = monitor.state_line({"pid": os.getpid()}, rows, time.time())
    assert state.startswith("RUNNING") and "model is generating text (26 chars so far)" in state
    assert monitor.render(rows[1]) is None and "still writing a text" in monitor.render(rows[-1], show_all=True)  # quiet by default


def test_half_written_line_waits_for_the_next_read(tmp_path):
    p = tmp_path / "events.jsonl"
    p.write_text('{"ev": "a"}\n{"ev": "b"')
    rows, off = monitor.read_events(p)
    assert [r["ev"] for r in rows] == ["a"]
    p.write_text('{"ev": "a"}\n{"ev": "b"}\n')
    rows, _ = monitor.read_events(p, off)
    assert [r["ev"] for r in rows] == ["b"]


def test_follow_prints_new_events_and_stops_at_the_end(cfg, offline, monkeypatch):
    monkeypatch.setattr(monitor, "POLL_SECS", 0.02)

    async def go():
        s = Session(cfg)
        s.main.submit("hello")
        await s.wait_idle()
        return s

    s = asyncio.run(go())

    def later():
        time.sleep(0.15)
        s.events.emit(s.main, "text", text="late words")
        s.events.close()

    t = threading.Thread(target=later)
    t.start()
    out = io.StringIO()
    assert monitor.run_monitor(cfg.sessions_dir, s.id, follow=True, out=out) == 0
    t.join()
    assert "late words" in out.getvalue() and "session end" in out.getvalue()


def test_cli_flags_voyager_creates_the_workspace_and_monitor_needs_no_server(cfg, monkeypatch, capsys):
    def boom(url):
        raise AssertionError("no server needed")
    monkeypatch.setattr(cli, "server_context_window", boom)
    monkeypatch.setattr(cli, "Config", lambda: cfg)
    a = cli.build_parser().parse_args(["--voyager", "quest", "--workspaces-dir", str(cfg.workspaces_dir), "--context-window", "4096",
                                       "beat the boss"])
    built = cli.build_config(a)
    assert built.cwd == cfg.workspaces_dir / "quest" and "beat the boss" in (built.cwd / "OBJECTIVE.md").read_text()
    a = cli.build_parser().parse_args(["--monitor", "--json", "--tail", "5"])
    assert (a.monitor, a.json, a.tail) == ("", True, 5)
    assert asyncio.run(cli._amain(a)) == 2 and "no session with events" in capsys.readouterr().err  # no boom: no server
