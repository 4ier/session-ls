"""Repeated scans reuse what has not changed, and still return what a scan from
nothing returns: after appends, new files and directories, removals, renames and
replacements."""
import json
import os
import uuid
from pathlib import Path

import pytest

import session_ls.api as api
from session_ls.api import HistoryIndex, Root


def write(path: Path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def pi_session(path: Path, text: str):
    write(path, [{"type": "session", "id": str(uuid.uuid4()), "cwd": "/tmp/p",
                  "timestamp": "2026-09-24T00:00:00Z"},
                 {"type": "message", "message": {"role": "user",
                                                 "content": [{"type": "text", "text": text}]}}])


def codex_session(path: Path, text: str, native=None):
    native = native or str(uuid.uuid4())
    write(path, [{"type": "session_meta", "payload": {"id": native, "cwd": "/tmp/c",
                                                      "timestamp": "2026-09-28T00:00:00Z"}},
                 {"type": "response_item", "payload": {"type": "message", "role": "user",
                  "content": [{"type": "input_text", "text": text}]}}])
    return native


def claude_session(path: Path, text: str):
    native = path.stem
    write(path, [{"type": "user", "sessionId": native, "cwd": "/tmp/a",
                  "timestamp": "2026-09-24T00:00:00Z", "message": {"role": "user", "content": text}}])


@pytest.fixture
def store(tmp_path):
    codex, claude, pi = tmp_path / "codex", tmp_path / "claude", tmp_path / "pi"
    for day in ("27", "28"):
        for n in range(3):
            codex_session(codex / f"sessions/2026/09/{day}/rollout-{day}-{n}.jsonl", f"codex {day} {n}")
    codex_session(codex / "archived_sessions/rollout-old.jsonl", "archived")
    for project in ("-a", "-b"):
        for n in range(2):
            claude_session(claude / f"projects/{project}/{uuid.uuid4()}.jsonl", f"claude {project} {n}")
    for project in ("x", "y"):
        pi_session(pi / f"sessions/{project}/run-{project}.jsonl", f"pi {project}")
    return tmp_path, [Root("codex", str(codex)), Root("claude", str(claude)), Root("pi", str(pi))]


def settle(base: Path):
    """Give every directory a distinct time in the past, as a real store has.

    Creating, removing or renaming an entry bumps its directory's mtime; on a
    coarse filesystem clock two changes can share one, which is why a listing is
    only reused once its directory has settled. Pinning times makes the test
    exercise reuse without depending on the clock's resolution.
    """
    settle.tick = getattr(settle, "tick", 1_600_000_000) + 10
    for directory, _, _ in os.walk(base):
        os.utime(directory, (settle.tick, settle.tick))


def full(roots):
    return HistoryIndex(roots, None, "host").scan()


def same(incremental, roots, opened=None):
    expected = full(roots)
    if opened is not None:
        opened.clear()
    got = incremental.scan()
    assert got.records == expected.records
    assert got.issues == expected.issues
    return got


def test_incremental_scans_match_a_full_scan(store, monkeypatch):
    base, roots = store
    settle(base)
    index = HistoryIndex(roots, base / "cache/data.json", "host")
    index.listing_settle_seconds = 0
    same(index, roots)

    opened = []
    real_open_source = api.open_source
    monkeypatch.setattr(api, "open_source", lambda *a: opened.append(a[0]) or real_open_source(*a))
    same(index, roots, opened)
    assert opened == [], "an unchanged store opens no file"
    listed = []
    real_scandir = os.scandir
    monkeypatch.setattr(api.os, "scandir", lambda fd: listed.append(fd) or real_scandir(fd))
    same(index, roots, listed)
    assert listed == [], "an unchanged store lists no directory"

    codex, claude, pi = (Path(root.path) for root in roots)
    day = codex / "sessions/2026/09/28"
    steps = [
        # An append changes neither the directory nor the file's name.
        lambda: write(day / "rollout-28-0.jsonl", [{"type": "response_item", "payload": {
            "type": "function_call"}}]),
        lambda: codex_session(day / "rollout-28-new.jsonl", "a new session"),
        lambda: codex_session(codex / "sessions/2026/09/29/rollout-29-0.jsonl", "a new day"),
        lambda: (day / "rollout-28-1.jsonl").unlink(),
        lambda: (day / "rollout-28-2.jsonl").rename(day / "rollout-28-renamed.jsonl"),
        lambda: (day / "rollout-28-renamed.jsonl").rename(
            codex / "sessions/2026/09/27/rollout-27-moved.jsonl"),
        lambda: (claude / "projects/-a").rename(claude / "projects/-c"),
        lambda: pi_session(pi / "sessions/x/replacement.tmp", "replaced")
        or os.replace(pi / "sessions/x/replacement.tmp", pi / "sessions/x/run-x.jsonl"),
        lambda: (pi / "sessions/y/run-y.jsonl").chmod(0),
        lambda: (pi / "sessions/y/run-y.jsonl").chmod(0o600),
        lambda: (codex / "archived_sessions").rename(codex / "archived_elsewhere"),
    ]
    for step in steps:
        step()
        settle(base)
        same(index, roots, opened)
        assert len(set(opened)) <= 2, "only what changed is opened"
    assert same(index, roots).records


def test_appends_are_seen_without_a_directory_change(store):
    base, roots = store
    settle(base)
    index = HistoryIndex(roots, None, "host")
    index.listing_settle_seconds = 0
    index.scan()
    file = Path(roots[2].path) / "sessions/x/run-x.jsonl"
    stamp = os.stat(file.parent).st_mtime_ns
    write(file, [{"type": "message", "message": {"role": "assistant", "content": [
        {"type": "toolCall", "name": "bash"}]}}])
    assert os.stat(file.parent).st_mtime_ns == stamp
    record = next(r for r in index.scan().records if r.file == str(file))
    assert record.activity == "working"


def test_a_directory_changed_just_now_is_listed_again(store, monkeypatch):
    base, roots = store
    index = HistoryIndex(roots, None, "host")  # default settle: these were created just now
    index.scan()
    listed = []
    real_scandir = os.scandir
    monkeypatch.setattr(api.os, "scandir", lambda fd: listed.append(fd) or real_scandir(fd))
    index.scan()
    assert listed, "a listing younger than the settle time is not trusted"


def test_the_cache_file_is_rewritten_at_most_once_per_interval(store):
    base, roots = store
    cache = base / "cache/data.json"
    index = HistoryIndex(roots, cache, "host")
    index.scan()
    stamp = cache.stat().st_mtime_ns
    file = Path(roots[2].path) / "sessions/x/run-x.jsonl"
    write(file, [{"type": "message", "message": {"role": "assistant", "content": [
        {"type": "toolCall", "name": "bash"}]}}])
    assert next(r for r in index.scan().records if r.file == str(file)).activity == "working"
    assert cache.stat().st_mtime_ns == stamp, "deferred"
    # Another process reading the older cache re-reads only the changed file.
    older = base / "cache/older.json"
    older.write_bytes(cache.read_bytes())
    older.chmod(0o600)
    assert HistoryIndex(roots, older, "host").scan().records == full(roots).records
    index.persist_seconds = 0
    index.scan()
    assert cache.stat().st_mtime_ns != stamp
