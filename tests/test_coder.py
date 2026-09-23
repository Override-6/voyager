from conftest import run
from voyager.session import Session

def test_coder_streams_events_resumes_and_never_sees_api_env(cfg, monkeypatch, offline):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:8080")
    s = Session(cfg)

    async def go():
        a = s.spawn(s.main, "coder-job", "write x", "coder")
        await a._worker_task  # first turn
        a.submit("now more")  # resume: second claude -p with --resume
        await a._worker_task
        await s.wait_idle()  # main's (fake) turn woken by the notification
        return a
    a = run(go())
    tools = [i for i in a.log.items if i.kind == "tool"]
    assert tools[0].meta["name"] == "Write" and tools[0].meta["args"] == {"file_path": "a.py", "content": "x=1"}
    assert tools[0].meta["status"] == "done" and tools[0].meta["result"] == "File created"
    texts = [i.text for i in a.log.items if i.kind == "text"]
    assert "--session-id=" in texts[0] and "prompt=write x" in texts[0]
    assert "--resume=" in texts[1] and "prompt=now more" in texts[1]
    assert all("key=False base=False" in t for t in texts)  # scrubbed: it can only use the CLI's own login
    assert a.claude_started and a.sandbox.is_dir() and "scratch directory" in a.report_extra()
    assert a.last_report.endswith("prompt=now more")
