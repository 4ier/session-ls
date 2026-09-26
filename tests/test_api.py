import json
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import pytest

from session_ls.api import (
    HistoryIndex,
    Root,
    clean_text,
    contains_literal_file,
    excerpt,
    history_key,
    open_source,
    query_terms,
    read_metadata,
    search_full,
)
from session_ls.storage import StorageError, atomic_json, file_lock, read_json


def make_pi(tmp_path, title="中文 [red]literal.*", native=None):
    root = tmp_path / "pi"
    file = root / "sessions/cwd/run.jsonl"
    file.parent.mkdir(parents=True, exist_ok=True)
    records = [{"type": "session", "id": native or str(uuid.uuid4()), "cwd": str(tmp_path),
                "timestamp": "2026-09-24T00:00:00Z"},
               {"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": title}]}}]
    file.write_text("\n".join(json.dumps(value) for value in records) + "\n")
    return Root("pi", str(root)), file


def test_import_does_not_change_sigpipe_or_touch_files(tmp_path):
    code = "import signal; a=signal.getsignal(signal.SIGPIPE); import session_ls,session_ls.api; assert signal.getsignal(signal.SIGPIPE)==a"
    result = subprocess.run([sys.executable, "-c", code], env={**os.environ, "HOME": str(tmp_path)}, capture_output=True)
    assert result.returncode == 0
    assert list(tmp_path.iterdir()) == []


def test_api_identity_unicode_and_literal_search(tmp_path):
    root, file = make_pi(tmp_path)
    index = HistoryIndex([root], tmp_path / "cache/data.json", "host")
    result = index.scan()
    assert len(result.records) == 1 and not result.issues
    row = result.records[0]
    assert row.native_id and row.cwd_quality == "native" and row.can_resume
    assert row.key == history_key("host", "pi", root.path, row.native_id)
    assert search_full(result.records, "中文").records
    assert search_full(result.records, '"literal.*"').records
    assert not search_full(result.records, '"literal.+"').records
    assert contains_literal_file(str(file), "中文")
    assert not contains_literal_file(str(file), "no-such-term")
    assert "[red]" in excerpt(row).lines[0]


def test_query_terms_are_literal_and_quoted():
    assert query_terms('retry "HELLO WORLD" 中文') == ["retry", "hello world", "中文"]
    with pytest.raises(ValueError):
        query_terms('"unfinished')
    assert query_terms('"unfinished', tolerant=True)


@pytest.mark.parametrize("value", ["\x1b[31mred\x1b[0m", "a\x1b]52;c;YWJj\x07b", "a\u202eb", "a\x00b", "a\x1bPbad\x1b\\b"])
def test_control_sequences_are_inert(value):
    cleaned = clean_text(value)
    assert "\x1b" not in cleaned and "\u202e" not in cleaned and "\x00" not in cleaned
    assert "YWJj" not in cleaned


def test_half_lines_blank_lines_and_cache_invalidation(tmp_path):
    root, file = make_pi(tmp_path)
    file.write_text(file.read_text().replace('\n', '\n\n') + '{"incomplete":')
    index = HistoryIndex([root], tmp_path / "cache/data.json", "host")
    first = index.scan()
    assert len(first.records) == 1
    cache = tmp_path / "cache/data.json"
    stamp = cache.stat().st_mtime_ns
    assert index.scan().records == first.records
    assert cache.stat().st_mtime_ns == stamp
    file.unlink()
    assert index.scan().records == []


def test_bad_cache_rebuilds(tmp_path):
    root, file = make_pi(tmp_path)
    cache = tmp_path / "cache/data.json"
    index = HistoryIndex([root], cache, "host")
    index.scan()
    cache.write_text("{broken")
    result = index.scan()
    assert len(result.records) == 1
    assert isinstance(read_json(cache), dict)


def test_partial_budget_never_fabricates_absence(tmp_path):
    root, file = make_pi(tmp_path)
    record, _ = read_metadata(root, str(file), "host", max_bytes=60, max_lines=1)
    assert record is not None and record.status == "partial"


def test_source_symlink_and_escape_rejected(tmp_path):
    root, file = make_pi(tmp_path)
    outside = tmp_path / "outside.jsonl"
    outside.write_text("secret")
    link = file.parent / "linked.jsonl"
    link.symlink_to(outside)
    with pytest.raises((OSError, ValueError)):
        with open_source(str(link), root.path):
            pass
    with pytest.raises((OSError, ValueError)):
        with open_source(str(outside), root.path):
            pass
    result = HistoryIndex([root], None, "host").scan()
    assert len(result.records) == 1


def test_same_native_id_in_different_roots_stays_distinct(tmp_path):
    native = str(uuid.uuid4())
    root1, _ = make_pi(tmp_path / "a", native=native)
    root2, _ = make_pi(tmp_path / "b", native=native)
    result = HistoryIndex([root1, root2], None, "host").scan()
    assert len({r.key for r in result.records}) == 2


def test_atomic_permissions_symlinks_and_locks(tmp_path):
    target = tmp_path / "private/state.json"
    atomic_json(target, {"ok": 1})
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.parent.stat().st_mode & 0o777 == 0o700
    assert read_json(target) == {"ok": 1}
    linked_parent = tmp_path / "alias"
    linked_parent.symlink_to(target.parent, target_is_directory=True)
    with pytest.raises(StorageError):
        atomic_json(linked_parent / "other.json", {})
    lock = target.parent / "x.lock"
    with file_lock(lock):
        with pytest.raises(StorageError):
            with file_lock(lock, timeout=.05):
                pass


def test_parallel_cache_writers_preserve_valid_json(tmp_path):
    root, _ = make_pi(tmp_path)
    cache = tmp_path / "cache/index.json"
    failures = []
    def writer():
        try:
            for _ in range(4):
                assert len(HistoryIndex([root], cache, "host").scan().records) == 1
        except Exception as exc:
            failures.append(exc)
    threads = [threading.Thread(target=writer) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not failures
    assert isinstance(read_json(cache), dict)


def test_cancelled_search_is_marked_partial(tmp_path):
    root, _ = make_pi(tmp_path)
    records = HistoryIndex([root], None, "host").scan().records
    cancel = threading.Event()
    cancel.set()
    assert search_full(records, "中文", cancel).cancelled


def test_fifo_source_is_rejected_without_blocking(tmp_path):
    root, _ = make_pi(tmp_path)
    fifo = Path(root.path) / "sessions/cwd/fifo.jsonl"
    os.mkfifo(fifo)
    with pytest.raises(PermissionError):
        with open_source(str(fifo), root.path):
            pass
    result = HistoryIndex([root], None, "host").scan()
    assert result.issues and len(result.records) == 1


def test_source_not_written_and_no_network(tmp_path, monkeypatch):
    import socket
    root, source = make_pi(tmp_path)
    before = source.read_bytes()
    def forbidden(*args, **kwargs):
        raise AssertionError("network access")
    monkeypatch.setattr(socket, "socket", forbidden)
    result = HistoryIndex([root], None, "host").scan()
    search_full(result.records, "中文")
    excerpt(result.records[0])
    assert source.read_bytes() == before


def test_cache_write_failure_does_not_hide_history(tmp_path, monkeypatch):
    import session_ls.api as api
    root, _ = make_pi(tmp_path)
    def disk_full(*args, **kwargs):
        raise OSError(28, "disk full")
    monkeypatch.setattr(api, "atomic_json", disk_full)
    result = HistoryIndex([root], tmp_path / "cache/data.json", "host").scan()
    assert len(result.records) == 1
    assert any("Cannot save" in issue for issue in result.issues)


def test_unreadable_child_directory_is_reported_not_silently_hidden(tmp_path, monkeypatch):
    import session_ls.api as api
    root, _ = make_pi(tmp_path)
    (Path(root.path) / "sessions" / "denied").mkdir()
    original = api.os.open
    def guarded(path, *args, **kwargs):
        if str(path) == "denied" and kwargs.get("dir_fd") is not None:
            raise PermissionError("injected unreadable directory")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(api.os, "open", guarded)
    result = HistoryIndex([root]).scan()
    assert len(result.records) == 1
    assert any("denied" in issue and "PermissionError" in issue for issue in result.issues)


def test_cancellation_during_last_file_is_reported(tmp_path, monkeypatch):
    import session_ls.api as api
    root, _ = make_pi(tmp_path)
    rows = HistoryIndex([root]).scan().records
    stop = threading.Event()
    def cancelled(*args):
        stop.set()
        return False, False
    monkeypatch.setattr(api, "_search_stream", cancelled)
    assert search_full(rows, "anything", stop).cancelled


CLAUDE_NATIVE = "11112222-3333-4444-5555-666677778888"


def make_claude(tmp_path, records):
    root = tmp_path / "claude"
    file = root / "projects/-tmp-project" / f"{CLAUDE_NATIVE}.jsonl"
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("\n".join(json.dumps(value) for value in records) + "\n")
    return Root("claude", str(root)), file


def test_claude_metadata_only_session_is_recognized(tmp_path):
    # Claude 2.1.x prepends metadata records; a session that was opened and quit
    # never writes a user/assistant message. It must still list, not error out.
    root, file = make_claude(tmp_path, [
        {"type": "last-prompt", "leafUuid": "c450dde6", "sessionId": CLAUDE_NATIVE},
        {"type": "mode", "mode": "normal", "sessionId": CLAUDE_NATIVE},
        {"type": "attachment", "cwd": str(tmp_path), "timestamp": "2026-09-23T03:25:31.930Z",
         "sessionId": CLAUDE_NATIVE},
    ])
    record, _ = read_metadata(root, str(file))
    assert record.native_id == CLAUDE_NATIVE
    assert record.cwd == str(tmp_path)
    assert record.started == "2026-09-23T03:25:31.930000+00:00"
    assert record.status == "partial"  # no user text, but a real session


def test_claude_native_title_is_the_fallback(tmp_path):
    root, file = make_claude(tmp_path, [
        {"type": "ai-title", "aiTitle": "review sandbox changes", "sessionId": CLAUDE_NATIVE},
        {"type": "agent-name", "agentName": "reviewer", "sessionId": CLAUDE_NATIVE},
    ])
    record, _ = read_metadata(root, str(file))
    assert record.title == "review sandbox changes"


def test_claude_user_text_outranks_native_title(tmp_path):
    root, file = make_claude(tmp_path, [
        {"type": "ai-title", "aiTitle": "generated title", "sessionId": CLAUDE_NATIVE},
        {"type": "user", "cwd": str(tmp_path), "timestamp": "2026-09-24T00:00:00Z",
         "sessionId": CLAUDE_NATIVE, "message": {"role": "user", "content": "my own words"}},
    ])
    record, _ = read_metadata(root, str(file))
    assert record.title == "my own words"


def test_unrecognized_format_still_reports_the_reason(tmp_path):
    root, file = make_claude(tmp_path, [{"type": "something-else", "payload": 1}])
    with pytest.raises(ValueError, match="Unrecognized history format"):
        read_metadata(root, str(file))


def test_scan_reports_no_issue_for_metadata_only_claude(tmp_path):
    root, file = make_claude(tmp_path, [
        {"type": "last-prompt", "leafUuid": "c450dde6", "sessionId": CLAUDE_NATIVE},
        {"type": "attachment", "cwd": str(tmp_path), "sessionId": CLAUDE_NATIVE},
    ])
    result = HistoryIndex([root], tmp_path / "cache/data.json", "host").scan()
    assert [record.native_id for record in result.records] == [CLAUDE_NATIVE]
    assert result.issues == []


def test_scan_issue_names_the_reason_not_only_the_exception_type(tmp_path):
    root, file = make_claude(tmp_path, [{"type": "something-else", "payload": 1}])
    result = HistoryIndex([root], tmp_path / "cache/data.json", "host").scan()
    assert result.records == []
    assert result.issues and "ValueError" in result.issues[0]
    assert "Unrecognized history format" in result.issues[0]
    assert str(tmp_path) not in result.issues[0]  # never leak the private path


def test_a_file_matching_the_pattern_is_skipped_silently(tmp_path):
    # Real stores contain marker files next to project directories. Matching one is
    # not a degraded source, so it must not be reported on every scan.
    root, file = make_pi(tmp_path)
    (file.parent.parent.parent / "sessions" / "not-a-project").write_text("marker\n")
    result = HistoryIndex([Root("pi", str(root.path))], tmp_path / "cache/data.json", "host").scan()
    assert len(result.records) == 1
    assert result.issues == []


def test_a_root_that_is_not_a_directory_is_reported_once(tmp_path):
    target = tmp_path / "not-a-dir"
    target.write_text("marker\n")
    result = HistoryIndex([Root("pi", str(target))], tmp_path / "cache/data.json", "host").scan()
    assert result.records == []
    assert result.issues == ["pi: configured root is not a directory"]


def test_unreadable_directory_is_still_reported(tmp_path, monkeypatch):
    root, file = make_pi(tmp_path)
    (file.parent.parent.parent / "sessions" / "closed").mkdir()
    real_open = os.open
    target = str(file.parent.parent.parent / "sessions" / "closed")

    def refusing(path, *args, **kwargs):
        if str(path) == "closed" or str(path) == target:
            raise PermissionError(13, "Permission denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", refusing)
    index = HistoryIndex([Root("pi", str(root.path))], tmp_path / "cache/data.json", "host")
    result = index.scan()
    assert any("PermissionError" in issue for issue in result.issues), result.issues
