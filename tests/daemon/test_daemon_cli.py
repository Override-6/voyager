"""The command-line and TUI sides of the daemon: --ps / --send / --stop / --detach, text replay, the TUI on a replica."""
import asyncio
import contextlib
import io
import os
import re
import signal

import pytest

from conftest import run
from test_daemon import attach, close, close_when_stopped, fast_tick, settle  # noqa: F401  (fast_tick: autouse fixture)
from voyager import cli
from voyager.daemon import commands as C
from voyager.daemon.launcher import live_sessions, spawn
from voyager.daemon.client import RemoteSession
from voyager.plain import PlainPrinter
from voyager.session import Session
from voyager.tui import panes
from voyager.tui.controller import Controller


def test_ps_send_and_stop_talk_to_a_running_session(cfg, offline, capsys):
    async def go():
        s, srv, rs = await attach(cfg)
        closer = asyncio.create_task(close_when_stopped(srv))
        assert await C.cmd_ps(cfg) == 0
        assert await C.cmd_send(cfg, s.id[:8], "hello from the cli") == 0
        await s.wait_idle()
        assert offline == [("main", "hello from the cli")]
        assert await C.cmd_send(cfg, "nope", "x") == 2
        assert await C.cmd_stop(cfg, s.id) == 0 and srv.stopped.is_set()
        await closer
        rs.close()
        await s.shutdown()
    run(go())
    out, err = capsys.readouterr()
    assert "chat" in out and "idle" in out and "ID" in out
    assert "sent to" in err and "no running session matches 'nope'" in err and "stopped" in err


def test_nothing_running_is_explained(cfg, capsys):
    assert run(C.cmd_ps(cfg)) == 0 and "no running session daemons" in capsys.readouterr().out
    assert run(C.cmd_attach_cli(cfg, None)) == 2
    assert "start one with: voyager --detach" in capsys.readouterr().err


def test_text_attach_replays_the_recent_transcript(cfg):
    async def go():
        s = Session(cfg)
        s.main.log.add("user", "first question", src="user")
        item = s.main.log.start("tool", name="bash", status="running", summary="ls")
        s.main.log.update(item, status="done", result="a.txt\nb.txt")
        text = s.main.log.start("text")
        s.main.log.delta(text, "the answer")
        s.main.log.end(text)
        s, srv, rs = await attach(cfg, s)
        out = io.StringIO()
        C.replay(PlainPrinter(out), rs.main)
        await close(s, srv, rs)
        return out.getvalue()
    text = re.sub(r"\x1b\[[0-9;]*m", "", run(go()))  # the printer colours its output
    assert "❯ first question" in text and "⏺ bash(ls)" in text and "⎿ b.txt" in text and "⏺ the answer" in text


def test_tui_runs_on_a_replica_like_on_a_session(cfg, offline):
    async def go():
        s = Session(cfg)
        s.main.log.add("user", "what is in here?", src="user")
        s, srv, rs = await attach(cfg, s)
        ctl = Controller(rs)
        rows = ["".join(seg[1] for seg in row) for row, _ in panes.view(ctl, 100, 10)]
        assert any("what is in here?" in r for r in rows)
        assert [k for k, _ in ctl.entries()] == ["agent"] and ctl.focused is rs.main
        assert "chat" in "".join(t for _, t in panes.mode_line(ctl))
        assert "main" in "".join(t for row in panes.list_rows(ctl, 100) for _, t, *_ in row)
        # typing goes to the real session
        ctl.buffer.text = "typed in the tui"
        ctl.submit()
        await settle(rs)
        await s.wait_idle()
        assert offline == [("main", "typed in the tui")]
        # /detach and /exit choose what quitting does to the daemon session
        ctl.command("/detach")
        assert ctl.keep is True
        ctl.command("/exit")
        assert ctl.keep is None
        ctl.command("/exit stop")
        assert ctl.keep is False
        ctl.command("/thinking 9")
        await settle(rs)
        assert s.cfg.thinking_budget == 9
        await close(s, srv, rs)
    run(go())


def test_workspace_listing_works_on_a_replica(cfg, offline):
    from voyager.voyager.workspace import Workspace

    async def go():
        Workspace.create(cfg.workspaces_dir, "recon", "Enumerate the API.")
        s, srv, rs = await attach(cfg, Session(cfg))
        ctl = Controller(rs)
        for cmd in ("/workspace", "/voyager"):  # they read the current workspace, which a replica only knows by name
            ctl.command(cmd)
            assert "  recon" in ctl.focused.log.items[-1].text
        ctl.command("/workspace nope")
        assert "no such workspace" in ctl.flash
        await close(s, srv, rs)
    run(go())


def test_a_local_session_cannot_detach(cfg):
    async def go():
        ctl = Controller(Session(cfg))
        ctl.command("/detach")
        assert ctl.keep is None and "cannot run in the background" in ctl.flash
        assert await ctl.session.leave(keep=True) == "stopped"  # nothing to detach from
    run(go())


def test_the_real_daemon_process_starts_serves_and_stops(cfg, capsys):
    """One real `voyager --serve` subprocess (no prompt, so no model call)."""
    async def go():
        live = await spawn(cfg)
        try:
            assert [x.id for x in live_sessions(cfg.sessions_dir)] == [live.id] and live.mode == "chat"
            rs = await RemoteSession(cfg, live.sock).connect()
            assert rs.id == live.id and rs.main.status == "idle"
            st = (await rs.request({"op": "status"}))["status"]
            assert st["state"] == "idle" and st["clients"] == 1
            assert await rs.leave(keep=False) == "stopped"
            for _ in range(100):  # the daemon removes its files on the way out
                if not live_sessions(cfg.sessions_dir):
                    break
                await asyncio.sleep(0.1)
            assert live_sessions(cfg.sessions_dir) == [] and not live.sock.exists()
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.kill(live.pid, signal.SIGTERM)
    run(go())


def test_resuming_a_saved_session_starts_a_daemon_for_it_then_reuses_that_one(cfg):
    from voyager.daemon.backend import DaemonBackend, open_first, resolve_saved
    saved = Session(cfg, session_id="20260101-101010")
    saved.main.log.add("user", "remember me", src="user")
    saved.main.messages += [{"role": "user", "content": "remember me"}, {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}]
    saved.main.outcome = "done"  # a finished conversation: resuming it must not send the (real) model anywhere
    saved.save()
    assert resolve_saved(cfg, "2026010") == "20260101-101010" and resolve_saved(cfg, None) == "20260101-101010"
    with pytest.raises(FileNotFoundError):
        resolve_saved(cfg, "nope")

    async def go():
        backend, live = DaemonBackend(), None
        try:
            rs = await backend.load_session(cfg, "2026010")
            live = live_sessions(cfg.sessions_dir)[0]
            assert rs.id == "20260101-101010" and [i.text for i in rs.main.log.items] == ["remember me"]
            again = await open_first(cfg, prompt=None, resume="20260101-101010", use_latest=False)
            assert again.id == rs.id and len(live_sessions(cfg.sessions_dir)) == 1  # attached, not started twice
            rs.close(), again.close()
        finally:
            if live:
                os.kill(live.pid, signal.SIGTERM)
    run(go())


def test_resume_attaches_to_a_live_session_that_was_never_saved(cfg, offline):
    from voyager.daemon.backend import open_first

    async def go():
        s, srv, rs = await attach(cfg, Session(cfg))  # still in its first turn: no session.json yet
        assert not (s.dir / "session.json").exists()
        again = await open_first(cfg, prompt=None, resume=s.id, use_latest=False)
        assert again.id == s.id
        again.close()
        await close(s, srv, rs)
    run(go())


def test_cli_flags_for_daemons_parse_and_route(cfg, monkeypatch, capsys):
    p = cli.build_parser()
    a = p.parse_args(["--detach", "--voyager", "quest", "--idle-exit", "600", "beat the boss"])
    assert (a.detach, a.voyager, a.idle_exit, a.prompt) == (True, "quest", 600.0, "beat the boss")
    a = p.parse_args(["--attach", "--cli"])
    assert (a.attach, a.cli) == ("", True)
    assert p.parse_args(["--attach", "2026"]).attach == "2026" and p.parse_args(["--stop", "x"]).stop == "x"
    monkeypatch.setattr("voyager.daemon.entry.Config", lambda: cfg)
    assert run(cli._amain(p.parse_args(["--ps"]))) == 0
    assert run(cli._amain(p.parse_args(["--send", "x"]))) == 2  # the message is missing
    assert "needs the message" in capsys.readouterr().err
    assert run(cli._amain(p.parse_args(["--stop", "nope"]))) == 2
