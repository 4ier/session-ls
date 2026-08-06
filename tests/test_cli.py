#!/usr/bin/env python3
"""End-to-end CLI tests for session-ls: run `pytest tests/` or
`python3 -m pytest` (plain asserts, also runnable one file at a time)."""
import contextlib, io, json, os, sys, tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
import session_ls as s

def _pi_session(cwd, ts, msgs):
    lines = [json.dumps({"type": "session", "id": "x", "timestamp": ts, "cwd": cwd})]
    for i, (role, text) in enumerate(msgs):
        lines.append(json.dumps({"type": "message", "id": str(i),
                                 "message": {"role": role,
                                             "content": [{"type": "text", "text": text}]}}))
    return lines

def _make_store(files):
    """files: {relative_path: [lines...]} under a temp dir; sets mtime per file
    to match its session timestamp so 'last' is deterministic."""
    d = tempfile.mkdtemp()
    for rel, lines in files.items():
        p = os.path.join(d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write("\n".join(lines) + "\n")
        ts = datetime.fromisoformat(lines[0].split('"timestamp":')[1].split('"')[1]
                                    .replace("Z", "+00:00"))
        os.utime(p, (ts.timestamp(), ts.timestamp()))
    return d

def _run(args, registry, cache=None):
    old_reg, old_cache, old_argv = s.REGISTRY, s.CACHE, sys.argv
    s.REGISTRY = registry
    if cache:
        s.CACHE = cache
    try:
        sys.argv = ["session-ls"] + args
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            s.main()
        return buf.getvalue()
    finally:
        s.REGISTRY, s.CACHE, sys.argv = old_reg, old_cache, old_argv

def _pi_registry(d):
    return [("pi", os.path.join(d, "*.jsonl"), s._pi, s._pi_user)]

def _store():
    return _make_store({
        "a.jsonl": _pi_session("/home/u/web", "2026-01-01T00:00:00Z", [("user", "fix login loop")]),
        "b.jsonl": _pi_session("/home/u/api", "2026-01-02T00:00:00Z", [("user", "tune postgres pool")]),
        "c.jsonl": _pi_session("/home/u/web", "2026-01-03T00:00:00Z", [("user", "deploy web app")]),
    })

def test_full_with_no_files_does_not_shell_out():
    # regression: rg/grep with no file args would scan the CWD (rg) or
    # block on stdin (grep). With zero sessions we must not spawn them.
    called = []
    def fake_run(cmd, **kw):
        called.append(cmd)
        raise AssertionError("must not shell out when there are no files")
    real = s.subprocess.run
    s.subprocess.run = fake_run
    try:
        out = _run(["--full", "anything"], [])
    finally:
        s.subprocess.run = real
    assert called == []
    assert "0 sessions" in out

def test_full_matches_content_only():
    d = _store()
    def fake_run(cmd, **kw):
        # rg says only b.jsonl contains the keyword
        return type("R", (), {"stdout": os.path.join(d, "b.jsonl") + "\n"})()
    real = s.subprocess.run
    s.subprocess.run = fake_run
    try:
        out = _run(["-f", "postgres"], _pi_registry(d))
    finally:
        s.subprocess.run = real
    assert "tune postgres pool" in out
    assert "fix login loop" not in out
    assert "deploy web app" not in out

def test_title_search_and_filters():
    d = _store()
    reg = _pi_registry(d)
    assert "1 sessions" in _run(["web"], reg)          # titles only
    assert "0 sessions" in _run(["nomatch"], reg)
    assert "3 sessions" in _run(["-a", "pi"], reg)
    assert "0 sessions" in _run(["-a", "codex"], reg)
    assert "2 sessions" in _run(["-c", "/web"], reg)   # cwd substring
    assert "2 sessions" in _run(["--since", "2026-01-02"], reg)
    assert "2 sessions" in _run(["--until", "2026-01-02"], reg)
    out = _run(["-n", "1"], reg)
    assert "1 sessions" in out and "deploy web app" in out  # newest last-active first

def test_list_and_json_output():
    d = _store()
    reg = _pi_registry(d)
    paths = _run(["-l", "web"], reg).splitlines()
    assert len(paths) == 1 and paths[0].endswith("c.jsonl")
    rows = [json.loads(l) for l in _run(["--json"], reg).splitlines()]
    assert [r["title"] for r in rows] == ["deploy web app", "tune postgres pool", "fix login loop"]
    assert set(rows[0]) == {"agent", "cwd", "started", "last", "title", "file"}
    # --json + -l: --json wins (checked first in main())
    lines = _run(["--json", "-l"], reg).splitlines()
    assert len(lines) == 3 and lines[0].startswith("{")

def test_unicode_title_survives_json():
    d = _make_store({"u.jsonl": _pi_session("/tmp/u", "2026-01-01T00:00:00Z",
                                            [("user", "修复登录跳转 loop")])})
    out = _run(["--json"], _pi_registry(d))
    assert "修复登录跳转" in out  # not \uXXXX-escaped
    out = _run([], _pi_registry(d))
    assert "修复登录跳转" in out

def test_cache_roundtrip_and_invalidation():
    d = _make_store({"a.jsonl": _pi_session("/p", "2026-01-01T00:00:00Z",
                                            [("user", "first title")])})
    cache = os.path.join(tempfile.mkdtemp(), "cache.json")
    reg = _pi_registry(d)
    out1 = _run([], reg, cache=cache)
    assert "first title" in out1
    assert os.path.exists(cache)
    out2 = _run([], reg, cache=cache)          # unchanged: served from cache
    assert out1 == out2
    p = os.path.join(d, "a.jsonl")
    with open(p) as f:
        content = f.read()
    with open(p, "w") as f:
        f.write(content.replace("first title", "second title"))
    out3 = _run([], reg, cache=cache)          # changed: must re-parse
    assert "second title" in out3
    assert "first title" not in out3

def test_deleted_file_disappears():
    d = _store()
    reg = _pi_registry(d)
    os.remove(os.path.join(d, "b.jsonl"))
    assert "2 sessions" in _run([], reg)


def test_stat_and_open_failures_skipped():
    d = _store()
    # broken symlink: glob matches, os.stat raises -> file skipped
    os.remove(os.path.join(d, "a.jsonl"))
    os.symlink(os.path.join(d, "no-such-target.jsonl"), os.path.join(d, "a.jsonl"))
    # directory named *.jsonl: stat succeeds, open raises -> file skipped
    os.makedirs(os.path.join(d, "dir.jsonl"))
    out = _run([], _pi_registry(d))
    assert "2 sessions" in out  # only b.jsonl and c.jsonl survive
    assert "a.jsonl" not in out and "dir.jsonl" not in out

if __name__ == "__main__":
    for _n in sorted(n for n in dir() if n.startswith("test_")):
        globals()[_n]()
        print("ok", _n)
