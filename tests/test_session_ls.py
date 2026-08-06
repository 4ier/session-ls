#!/usr/bin/env python3
"""Parser checks for session-ls: run `pytest tests/` or `python3 -m pytest`."""
import json, os, sys, tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
import session_ls as s

s.CACHE = os.path.join(tempfile.mkdtemp(), "cache.json")  # don't touch real cache

def _write(fixtures):
    d = tempfile.mkdtemp()
    for name, lines in fixtures.items():
        with open(os.path.join(d, name), "w") as f:
            f.write("\n".join(lines) + "\n")
    return d

def _pi_lines(msgs):
    """Build a pi-style session file body from [(role, text), ...]."""
    return [json.dumps({"type": "message", "id": str(i),
                        "message": {"role": r, "content": [{"type": "text", "text": t}]}})
            for i, (r, t) in enumerate(msgs)]

def test_parsers():
    d = _write({
        "pi1.jsonl": [json.dumps({"type": "session", "id": "x", "timestamp": "2026-01-01T00:00:00Z",
                                  "cwd": "/tmp/proj"})] + _pi_lines([("user", "hello world"),
                                                                     ("assistant", "hi")]),
        "codex1.jsonl": [json.dumps({"type": "session_meta", "payload": {"session_id": "y",
                                    "timestamp": "2026-01-02T00:00:00Z", "cwd": "/tmp/other"}})] + [
            json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": "<recommended_plugins>boiler"}]}}),
            json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": "actual question here"}]}}),
        ],
        "claude1.jsonl": [
            json.dumps({"type": "queue-operation", "operation": "enqueue", "sessionId": "s1",
                        "timestamp": "2026-01-03T00:00:00Z", "content": "x"}),
            json.dumps({"type": "user", "timestamp": "2026-01-03T00:00:01Z", "cwd": "/tmp/claude-proj",
                        "message": {"role": "user", "content": "claude question"}}),
        ],
        "sess.jsonl": [
            json.dumps({"role": "user", "message": {"content": [{"type": "text",
                        "text": "<timestamp>Wed, Jun 10, 2026 2:52 PM (UTC+8)</timestamp>\n<user_query>\ncursor question\n</user_query>"}]}}),
        ],
        "junk.jsonl": "not json at all\n",
    })

    rows = s.parse_all([
        ("pi", s._pi, s._pi_user, os.path.join(d, "pi1.jsonl")),
        ("codex", s._codex, s._codex_user, os.path.join(d, "codex1.jsonl")),
        ("claude", s._claude, s._claude_user, os.path.join(d, "claude1.jsonl")),
        ("cursor", s._cursor, s._cursor_user, os.path.join(d, "sess.jsonl")),
        ("codex", s._codex, s._codex_user, os.path.join(d, "junk.jsonl")),
    ])
    assert len(rows) == 4, rows
    by_name = {r["agent"]: r for r in rows}
    assert by_name["pi"]["title"] == "hello world"
    assert by_name["codex"]["title"] == "actual question here"  # injected skipped
    assert by_name["claude"]["title"] == "claude question"
    assert by_name["claude"]["cwd"] == "/tmp/claude-proj"
    assert by_name["cursor"]["title"] == "cursor question"  # wrappers stripped
    assert by_name["cursor"]["started"] == by_name["cursor"]["last"]  # mtime fallback

    # cache round-trip: second parse_all must reuse cached rows unchanged
    rows2 = s.parse_all([("pi", s._pi, s._pi_user, os.path.join(d, "pi1.jsonl"))])
    assert rows2[0] == next(r for r in rows if r["agent"] == "pi")
