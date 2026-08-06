#!/usr/bin/env python3
"""Parser checks for session-ls: run `pytest tests/` or `python3 -m pytest`."""
import json, os, sys, tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
import session_ls as s

s.CACHE = os.path.join(tempfile.mkdtemp(), "cache.json")  # don't touch real cache

def _write(fixtures):
    d = tempfile.mkdtemp()
    for name, lines in fixtures.items():
        p = os.path.join(d, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
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


def test_claude_null_message_or_content():
    # claude logs sometimes carry user lines with message/content null;
    # they must be skipped, not crash the whole file
    d = _write({"c.jsonl": [
        json.dumps({"type": "user", "timestamp": "2026-01-01T00:00:00Z", "cwd": "/tmp/c",
                    "message": None}),
        json.dumps({"type": "user", "timestamp": "2026-01-01T00:00:01Z", "cwd": "/tmp/c",
                    "message": {"role": "user", "content": None}}),
        json.dumps({"type": "user", "timestamp": "2026-01-01T00:00:02Z", "cwd": "/tmp/c",
                    "message": {"role": "user", "content": "the real question"}}),
    ]})
    rows = s.parse_all([("claude", s._claude, s._claude_user, os.path.join(d, "c.jsonl"))])
    assert len(rows) == 1, rows
    assert rows[0]["title"] == "the real question"
    assert rows[0]["cwd"] == "/tmp/c"


def test_pi_session_without_timestamp():
    # missing timestamp falls back to file mtime instead of dropping the session
    d = _write({"p.jsonl": [
        json.dumps({"type": "session", "cwd": "/tmp/p"}),  # no timestamp
        json.dumps({"type": "message", "message": {"role": "user",
                    "content": [{"type": "text", "text": "hi"}]}}),
    ]})
    rows = s.parse_all([("pi", s._pi, s._pi_user, os.path.join(d, "p.jsonl"))])
    assert len(rows) == 1, rows
    assert rows[0]["title"] == "hi"
    assert rows[0]["started"] == rows[0]["last"]  # mtime fallback


def test_bad_line_does_not_kill_file():
    # one malformed line mid-log must not hide the whole session
    d = _write({"b.jsonl": [
        json.dumps({"type": "session", "timestamp": "2026-01-01T00:00:00Z", "cwd": "/tmp/b"}),
        "not json at all",
        json.dumps({"type": "message", "message": {"role": "user",
                    "content": [{"type": "text", "text": "survives"}]}}),
    ]})
    rows = s.parse_all([("pi", s._pi, s._pi_user, os.path.join(d, "b.jsonl"))])
    assert len(rows) == 1, rows
    assert rows[0]["title"] == "survives"


def test_bad_line_mid_head_claude():
    # same, for claude where the meta parser scans the whole head
    d = _write({"c2.jsonl": [
        "not json at all",
        json.dumps({"type": "user", "timestamp": "2026-01-01T00:00:00Z", "cwd": "/tmp/c2",
                    "message": {"role": "user", "content": "still here"}}),
    ]})
    rows = s.parse_all([("claude", s._claude, s._claude_user, os.path.join(d, "c2.jsonl"))])
    assert len(rows) == 1, rows
    assert rows[0]["title"] == "still here"


def test_non_list_content_is_skipped():
    # pi/codex user lines with content null or a bare string must not kill the file
    d = _write({
        "pi.jsonl": [json.dumps({"type": "session", "timestamp": "2026-01-01T00:00:00Z",
                                  "cwd": "/p"})] + [
            json.dumps({"type": "message", "message": {"role": "user", "content": None}}),
            json.dumps({"type": "message", "message": {"role": "user",
                        "content": [{"type": "text", "text": "pi ok"}]}}),
        ],
        "codex.jsonl": [json.dumps({"type": "session_meta", "payload": {"cwd": "/c",
                                    "timestamp": "2026-01-01T00:00:00Z"}})] + [
            json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user",
                        "content": "bare string"}}),
            json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": "codex ok"}]}}),
        ],
    })
    rows = s.parse_all([
        ("pi", s._pi, s._pi_user, os.path.join(d, "pi.jsonl")),
        ("codex", s._codex, s._codex_user, os.path.join(d, "codex.jsonl")),
    ])
    by_name = {r["agent"]: r for r in rows}
    assert by_name["pi"]["title"] == "pi ok"
    assert by_name["codex"]["title"] == "codex ok"


def test_injected_only_known_markers():
    # a real question starting with '<' is not injected context
    assert s._injected("<div> not rendering") is False
    assert s._injected("why does <a> do this") is False
    # known codex / claude injections are still skipped
    assert s._injected("<recommended_plugins>boiler") is True
    assert s._injected("<environment_details>...") is True
    assert s._injected("# AGENTS.md instructions") is True
    assert s._injected("see AGENTS.md instructions") is True


def test_cursor_collect_skips_subagents():
    d = _write({"agent-transcripts/main/main.jsonl": [
        json.dumps({"role": "user", "message": {"content": [{"type": "text", "text": "q"}]}})],
        "agent-transcripts/main/subagents/sub/sub.jsonl": [
        json.dumps({"role": "user", "message": {"content": [{"type": "text", "text": "sub"}]}})],
    })
    saved = s.REGISTRY
    try:
        s.REGISTRY = [
            ("cursor", os.path.join(d, "*", "*", "*.jsonl"), s._cursor, s._cursor_user),
            ("cursor", os.path.join(d, "*", "*", "*", "*", "*.jsonl"), s._cursor, s._cursor_user),
        ]
        files = s.collect()
    finally:
        s.REGISTRY = saved
    paths = [f for _, _, _, f in files]
    assert len(paths) == 1, paths
    assert paths[0].endswith("main.jsonl")


def test_parser_edge_branches():
    d = _write({
        # first line is not a session/session_meta line -> meta returns None -> skipped
        "pi-not-session.jsonl": [json.dumps({"type": "message", "message": {"role": "user",
            "content": [{"type": "text", "text": "orphan"}]}})],
        "codex-not-meta.jsonl": [json.dumps({"type": "response_item", "payload": {"type": "message",
            "role": "user", "content": [{"type": "input_text", "text": "orphan"}]}})],
        # claude user line with a LIST content (string content was already covered)
        "claude-list.jsonl": [json.dumps({"type": "user", "timestamp": "2026-01-01T00:00:00Z",
            "cwd": "/tmp/l", "message": {"role": "user",
            "content": [{"type": "text", "text": "list content"},
                         {"type": "tool_use", "id": "x"}]}})],
        # cursor: non-user line first, then a real user line
        "cursor-mixed.jsonl": [
            json.dumps({"role": "assistant", "message": {"content": [{"type": "text", "text": "answer"}]}}),
            json.dumps({"role": "user", "message": {"content": [{"type": "text", "text": "real question"}]}}),
        ],
        # claude file with no user line at all -> meta returns None -> skipped
        "claude-no-user.jsonl": [json.dumps({"type": "assistant", "message": {"role": "assistant",
            "content": "hi"}})],
    })
    rows = s.parse_all([
        ("pi", s._pi, s._pi_user, os.path.join(d, "pi-not-session.jsonl")),
        ("codex", s._codex, s._codex_user, os.path.join(d, "codex-not-meta.jsonl")),
        ("claude", s._claude, s._claude_user, os.path.join(d, "claude-list.jsonl")),
        ("cursor", s._cursor, s._cursor_user, os.path.join(d, "cursor-mixed.jsonl")),
        ("claude", s._claude, s._claude_user, os.path.join(d, "claude-no-user.jsonl")),
    ])
    assert "pi-not-session" not in [r["file"] for r in rows]     # meta None
    assert "codex-not-meta" not in [r["file"] for r in rows]     # meta None
    assert "claude-no-user" not in [r["file"] for r in rows]     # meta None
    by_file = {r["file"]: r for r in rows}
    assert by_file[os.path.join(d, "claude-list.jsonl")]["title"] == "list content"
    assert by_file[os.path.join(d, "cursor-mixed.jsonl")]["title"] == "real question"

    # cursor meta with a too-shallow path (no parent dirs): cwd unknown, still listed
    assert s._cursor("x.jsonl", []) == ("", None)


def test_no_title_within_head_limit():
    # a file with >= 2000 lines and no user text: the head read exhausts
    # the line budget without a title, but the row is still listed
    d = _write({"long.jsonl": [json.dumps({"type": "session", "timestamp": "2026-01-01T00:00:00Z",
                                            "cwd": "/long"})] + [
        json.dumps({"type": "message", "message": {"role": "assistant",
                    "content": [{"type": "text", "text": "filler"}]}}) for _ in range(2000)]})
    rows = s.parse_all([("pi", s._pi, s._pi_user, os.path.join(d, "long.jsonl"))])
    assert len(rows) == 1, rows
    assert rows[0]["title"] == ""
    assert rows[0]["cwd"] == "/long"


def test_corrupt_cache_does_not_crash():
    # a cache entry without 'sig' (older/foreign schema) must not raise
    import tempfile
    d = _write({"x.jsonl": [json.dumps({"type": "session", "timestamp": "2026-01-01T00:00:00Z",
                                         "cwd": "/x"}),
                             json.dumps({"type": "message", "message": {"role": "user",
                                         "content": [{"type": "text", "text": "t"}]}})]})
    old = s.CACHE
    s.CACHE = os.path.join(tempfile.mkdtemp(), "cache.json")
    try:
        with open(s.CACHE, "w") as f:
            json.dump({os.path.join(d, "x.jsonl"): {"agent": "pi", "title": "stale"}}, f)
        rows = s.parse_all([("pi", s._pi, s._pi_user, os.path.join(d, "x.jsonl"))])
        assert rows and rows[0]["title"] == "t"  # stale cache discarded, file re-parsed
    finally:
        s.CACHE = old

if __name__ == "__main__":
    for _n in sorted(n for n in dir() if n.startswith("test_")):
        globals()[_n]()
        print("ok", _n)
