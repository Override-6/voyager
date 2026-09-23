"""The permanent record: transcript.jsonl and rounds/*.json survive compaction, /clear and resume."""
import copy
import json

from conftest import run
from test_compaction import MSGS, FakeClient
from voyager.session import Session


def rows_of(s):
    return [json.loads(ln) for ln in (s.dir / "transcript.jsonl").read_text().splitlines()]


def tool_call(s, name="bash", summary="ls", result="ok", status="done", agent=None, args=None):
    log = (agent or s.main).log
    item = log.start("tool", name=name, status="args", args=args or {})
    log.update(item, summary=summary)
    log.update(item, status="running")
    log.tool_live(item, "streamed line")
    log.update(item, status=status, result=result)
    return item


def compact_once(s, client_tokens=0.75):
    m = s.main
    m.messages = [copy.deepcopy(x) for x in MSGS * 4]
    m._measured_tokens, m._measured_len = int(s.cfg.context_window * client_tokens), len(m.messages)
    run(m._maybe_compact())


def test_items_are_recorded_in_full_when_they_finish(cfg):
    s = Session(cfg)
    big = "x" * 5000
    s.main.log.add("user", "please do it", src="user")
    th = s.main.log.start("thinking")
    s.main.log.delta(th, "hmm ")
    s.main.log.delta(th, "let me think")
    assert [r["kind"] for r in rows_of(s)] == ["user"]  # the thinking is not finished: not written yet
    s.main.log.end(th)
    tool_call(s, result=big, args={"command": "echo " + big})
    text = s.main.log.start("text")
    s.main.log.delta(text, "the answer")
    s.main.log.end(text)
    rows = rows_of(s)
    assert [r["kind"] for r in rows] == ["user", "thinking", "tool", "text"]
    assert rows[1]["text"] == "hmm let me think" and rows[3]["text"] == "the answer"
    tool = rows[2]
    assert tool["meta"]["result"] == big and tool["meta"]["args"]["command"].endswith(big)  # nothing clipped
    assert "live" not in tool["meta"] and "expanded" not in tool["meta"] and tool["agent"] == "main" and tool["round"] == 0


def test_a_backgrounded_tool_is_written_once_when_it_ends(cfg):
    s = Session(cfg)
    item = s.main.log.start("tool", name="bash", status="running", summary="sleep 99")
    s.main.log.update(item, status="background", task_id="t1")
    assert not (s.dir / "transcript.jsonl").exists()
    s.main.log.update(item, status="done", result="finally")
    s.main.log.update(item, result="again")  # later noise
    rows = rows_of(s)
    assert len(rows) == 1 and rows[0]["meta"]["result"] == "finally" and rows[0]["meta"]["status"] == "done"


def test_a_compaction_archives_the_round_and_starts_the_next_one(cfg):
    client = FakeClient()
    s = Session(cfg, client=client)
    tool_call(s, result="before compaction")
    old = [copy.deepcopy(x) for x in MSGS * 4]
    compact_once(s)
    arch = json.loads((s.dir / "rounds" / "main-r000.json").read_text())
    assert arch["round"] == 0 and arch["agent"] == "main" and arch["messages"] == old  # the exact model-facing history
    assert arch["summary"].startswith("1. request") and arch["tokens_before"] > arch["tokens_after"]
    assert "main agent of an interactive coding CLI" in arch["system_prompt"] and "bash" in arch["tools"] and arch["info"] == {}
    assert s.main.compactions == 1 and s.main.extra_state()["compactions"] == 1
    tool_call(s, result="after compaction")
    rows = rows_of(s)
    by_result = {r["meta"].get("result"): r for r in rows if r["kind"] == "tool"}
    assert by_result["before compaction"]["round"] == 0 and by_result["after compaction"]["round"] == 1
    comp = next(r for r in rows if r["kind"] == "compact")
    assert comp["round"] == 0 and comp["text"].startswith("1. request") and comp["meta"]["after"] < comp["meta"]["before"]
    compact_once(s)  # a second compaction: its own file, the first one untouched
    assert sorted(p.name for p in (s.dir / "rounds").iterdir()) == ["main-r000.json", "main-r001.json"]


def test_record_survives_clear_and_resume(cfg):
    s = Session(cfg, client=FakeClient())
    s.main.log.add("user", "first task", src="user")
    s.main.messages.append({"role": "user", "content": "first task"})
    compact_once(s)
    s.reset()  # /clear
    s.main.log.add("user", "second task", src="user")
    s.save()
    assert [r["text"] for r in rows_of(s) if r["kind"] == "user"] == ["first task", "second task"]
    again = Session.load(cfg, s.id)
    assert again.main.compactions == 1  # the round number carries on where it stopped
    again.main.log.add("user", "third task", src="user")
    assert [r["text"] for r in rows_of(again) if r["kind"] == "user"] == ["first task", "second task", "third task"]
    assert rows_of(again)[-1]["round"] == 1


def test_a_task_with_several_compactions_can_be_rebuilt_from_the_files_alone(cfg):
    s = Session(cfg, client=FakeClient())
    for n in range(3):  # three rounds, two compactions: the model only ever sees the last round
        s.main.log.add("user", f"step {n}", src="user")
        tool_call(s, "bash", f"cmd {n}", f"result {n}" * 200)
        if n < 2:
            compact_once(s)
    rows = rows_of(s)
    assert [r["text"] for r in rows if r["kind"] == "user"] == ["step 0", "step 1", "step 2"]
    assert [r["round"] for r in rows if r["kind"] == "user"] == [0, 1, 2]
    assert [r["meta"]["result"] for r in rows if r["kind"] == "tool"] == [f"result {n}" * 200 for n in range(3)]  # unclipped
    assert [r["kind"] for r in rows].count("compact") == 2
    assert sorted(p.name for p in (s.dir / "rounds").iterdir()) == ["main-r000.json", "main-r001.json"]
    assert all(json.loads(p.read_text())["messages"] for p in (s.dir / "rounds").iterdir())
