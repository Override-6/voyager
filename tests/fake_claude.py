#!/usr/bin/env python3
"""Stand-in for `claude -p ... --output-format stream-json`: emits the event shapes captured from the real CLI."""
import json, os, sys

argv = sys.argv[1:]
prompt = sys.stdin.read()
flag = "--resume" if "--resume" in argv else "--session-id"
sid = argv[argv.index(flag) + 1]
out = lambda o: print(json.dumps(o), flush=True)
out({"type": "system", "subtype": "init", "session_id": sid, "tools": ["Write"]})
se = lambda e: out({"type": "stream_event", "event": e})
se({"type": "message_start"})
se({"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "tu1", "name": "Write", "input": {}}})
for part in ('{"file_path": "a.py", ', '"content": "x=1"}'):
    se({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": part}})
se({"type": "content_block_stop", "index": 0})
out({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu1", "content": "File created"}]}})
se({"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}})
answer = f"{flag}={sid[:8]} key={'ANTHROPIC_API_KEY' in os.environ} base={'ANTHROPIC_BASE_URL' in os.environ} prompt={prompt}"
se({"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": answer}})
se({"type": "content_block_stop", "index": 1})
out({"type": "result", "subtype": "success", "is_error": False, "result": answer, "total_cost_usd": 0.01, "duration_ms": 5, "usage": {"input_tokens": 3, "output_tokens": 4}})
