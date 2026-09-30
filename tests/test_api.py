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


CODEX_NATIVE = "01a0cc63-68c1-7d42-903d-f1afefd56793"
CODEX_PARENT = "01a0ce81-fe83-79f0-a0b1-6e9d81767c43"


def make_codex(tmp_path, messages, meta=None, extra=()):
    """A Codex rollout: session_meta, optional extra records, then user messages,
    each a list of text parts as Codex writes them."""
    root = tmp_path / "codex"
    file = root / "sessions/2026/09/28" / f"rollout-2026-09-28T00-00-00-{CODEX_NATIVE}.jsonl"
    file.parent.mkdir(parents=True, exist_ok=True)
    payload = {"id": CODEX_NATIVE, "cwd": str(tmp_path), "timestamp": "2026-09-28T00:00:00Z",
               **(meta or {})}
    records = [{"type": "session_meta", "payload": payload}, *extra]
    for parts in messages:
        records.append({"type": "response_item", "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": part} for part in parts]}})
    file.write_text("\n".join(json.dumps(value) for value in records) + "\n")
    return Root("codex", str(root)), file


def codex_title(tmp_path, *messages):
    root, file = make_codex(tmp_path, messages)
    return read_metadata(root, str(file))[0].title


def test_injected_parts_of_one_message_do_not_hide_the_words_after_them(tmp_path):
    # Codex sends plugin lists, AGENTS.md and the environment as parts of the same
    # message as the request; filtering the joined text lost the request with them.
    assert codex_title(tmp_path, [
        "<recommended_plugins>\n- A\n</recommended_plugins>",
        "# AGENTS.md instructions for /srv\n<INSTRUCTIONS>be brief</INSTRUCTIONS>",
        "<environment_context>\n<cwd>/srv</cwd>\n</environment_context>",
        "fix the retry loop"]) == "fix the retry loop"


def test_a_message_of_only_injected_context_is_skipped(tmp_path):
    assert codex_title(tmp_path,
                       ["<environment_context>\n<cwd>/srv</cwd>\n</environment_context>"],
                       ["<image name=[Image #1] path=\"/x.png\">", "</image>", "what is in it"],
                       ["later message"]) == "what is in it"


@pytest.mark.parametrize("wrapped, expected", [
    ("# Files mentioned by the user:\n\n## spec.md: /srv/spec.md\n\n## My request for Codex:\n"
     "summarise the spec\n", "summarise the spec"),
    ("# Response annotations:\nItems...\n<response-annotations>[]</response-annotations>\n"
     "## My request:\nuse my notes\n", "use my notes"),
    ("# Files pasted by the user:\n\n## \"a pasted note\"\n\n## My request:\n",
     "# Files pasted by the user:\n\n## \"a pasted note\"\n\n## My request:"),
    ("# bridge conventions\nlong rules\n<user_input>\n{\"text\":\"is this a bug?\"}\n</user_input>",
     "is this a bug?"),
    ("<codex_internal_context source=\"goal\">\nContinue.\n<objective>\nship the fix\n"
     "</objective>\nrules\n</codex_internal_context>", "ship the fix"),
])
def test_the_persons_words_are_recovered_from_known_wrappers(tmp_path, wrapped, expected):
    assert codex_title(tmp_path, [wrapped]) == expected


def test_an_empty_bridge_message_does_not_fall_back_to_its_conventions(tmp_path):
    assert codex_title(tmp_path,
                       ["# bridge conventions\n<user_input>{\"text\":\"\"}</user_input>"],
                       ["real question"]) == "real question"


def test_paragraph_wrappers_and_doubled_links_are_tidied(tmp_path):
    assert codex_title(tmp_path, ["<user_input>{\"text\":\"<p> check this</p>\"}</user_input>"]) \
        == "check this"
    assert codex_title(tmp_path, ["[https://example.test/a](https://example.test/a) explain"]) \
        == "https://example.test/a explain"
    # A link whose text differs from its target is left as written.
    assert codex_title(tmp_path, ["[the issue](https://example.test/1)"]) \
        == "[the issue](https://example.test/1)"


def test_slash_command_echoes_are_not_titles(tmp_path):
    root, file = make_claude(tmp_path, [
        {"type": "user", "sessionId": CLAUDE_NATIVE, "cwd": str(tmp_path),
         "message": {"content": "<local-command-caveat>Caveat: generated</local-command-caveat>"}},
        {"type": "user", "sessionId": CLAUDE_NATIVE,
         "message": {"content": "<command-name>/model</command-name>\n<command-args></command-args>"}},
        {"type": "user", "sessionId": CLAUDE_NATIVE, "message": {"content": "real work"}},
    ])
    record, _ = read_metadata(root, str(file))
    assert record.title == "real work"


@pytest.mark.parametrize("meta, parent", [
    ({"source": {"subagent": {"other": "guardian"}}, "parent_thread_id": CODEX_PARENT,
      "thread_source": "guardian_review"}, CODEX_PARENT),
    ({"source": {"subagent": {"thread_spawn": {"parent_thread_id": CODEX_PARENT, "depth": 1}}},
      "thread_source": "subagent"}, CODEX_PARENT),
    ({"source": "vscode", "thread_source": "subagent"}, None),
])
def test_codex_subagents_are_marked_with_their_parent(tmp_path, meta, parent):
    root, file = make_codex(tmp_path, [["reviewing"]], meta)
    record, _ = read_metadata(root, str(file))
    assert record.subagent is True and record.parent == parent


def test_a_top_level_session_is_not_a_subagent(tmp_path):
    root, file = make_codex(tmp_path, [["hello"]], {"source": "vscode", "thread_source": "user"})
    record, _ = read_metadata(root, str(file))
    assert record.subagent is False and record.parent is None
    root, file = make_pi(tmp_path)
    assert read_metadata(root, str(file))[0].subagent is False


def test_a_later_session_meta_does_not_change_whose_session_this_is(tmp_path):
    # A forked or resumed rollout can carry the parent's session_meta further down;
    # the file's identity, and whether it is a subagent, come from the first one.
    later = {"type": "session_meta", "payload": {"id": CODEX_PARENT, "source": "vscode"}}
    root, file = make_codex(tmp_path, [["reviewing"]],
                            {"source": {"subagent": {"other": "guardian"}},
                             "parent_thread_id": CODEX_PARENT}, extra=[later])
    record, _ = read_metadata(root, str(file))
    assert record.native_id == CODEX_NATIVE and record.subagent and record.parent == CODEX_PARENT


def test_subagent_fields_survive_the_cache(tmp_path):
    root, file = make_codex(tmp_path, [["reviewing"]],
                            {"source": {"subagent": {"other": "guardian"}},
                             "parent_thread_id": CODEX_PARENT})
    cache = tmp_path / "cache.json"
    first = HistoryIndex([root], cache).scan().records
    again = HistoryIndex([root], cache).scan().records
    assert [(r.subagent, r.parent) for r in again] == [(r.subagent, r.parent) for r in first] \
        == [(True, CODEX_PARENT)]


# The end of a transcript: what the agent is doing, what was last asked, and on
# which branch.

def claude_turn(role, content, stop=None, branch="feat/x"):
    message = {"role": role, "content": content}
    if stop:
        message["stop_reason"] = stop
    return {"type": role, "sessionId": CLAUDE_NATIVE, "cwd": "/tmp", "gitBranch": branch,
            "timestamp": "2026-09-28T00:00:00Z", "message": message}


def test_claude_mid_turn_is_working_and_a_finished_turn_is_waiting(tmp_path):
    root, file = make_claude(tmp_path, [
        claude_turn("user", "fix the flaky retry test"),
        claude_turn("assistant", [{"type": "tool_use", "name": "Bash", "input": {}}]),
        claude_turn("user", [{"type": "tool_result", "content": "ok"}]),
        {"type": "attachment", "sessionId": CLAUDE_NATIVE},
    ])
    record, _ = read_metadata(root, str(file))
    assert record.activity == "working"
    assert record.last_request == "fix the flaky retry test", "a tool result is not a request"
    assert record.branch == "feat/x"

    with file.open("a") as handle:
        handle.write(json.dumps(claude_turn("assistant", [{"type": "text", "text": "done"}],
                                            stop="end_turn", branch="feat/y")) + "\n")
    record, _ = read_metadata(root, str(file))
    assert record.activity == "waiting" and record.branch == "feat/y"


def test_codex_turn_state_request_and_branch(tmp_path):
    root, file = make_codex(tmp_path, [["first ask"], ["# AGENTS.md instructions for /repo", "second ask"]],
                            meta={"git": {"branch": "codex/fix-retry"}})
    with file.open("a") as handle:
        handle.write(json.dumps({"type": "response_item", "payload": {"type": "function_call"}}) + "\n")
    record, _ = read_metadata(root, str(file))
    assert record.activity == "working"
    assert record.last_request == "second ask", "injected parts are not the request"
    assert record.branch == "codex/fix-retry"
    with file.open("a") as handle:
        handle.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}) + "\n")
    assert read_metadata(root, str(file))[0].activity == "waiting"


def test_pi_tool_call_is_working_and_plain_text_is_waiting(tmp_path):
    root, file = make_pi(tmp_path, title="look at the logs")
    with file.open("a") as handle:
        handle.write(json.dumps({"type": "message", "message": {
            "role": "assistant", "content": [{"type": "toolCall", "name": "bash"}]}}) + "\n")
    record, _ = read_metadata(root, str(file))
    assert record.activity == "working" and record.last_request == "look at the logs"
    with file.open("a") as handle:
        handle.write(json.dumps({"type": "message", "message": {
            "role": "assistant", "content": [{"type": "text", "text": "found it"}]}}) + "\n")
    assert read_metadata(root, str(file))[0].activity == "waiting"


def test_the_request_is_found_behind_megabytes_of_tool_output(tmp_path):
    # An agent working alone for hours buries the last request under tool output,
    # including single lines longer than a read window. Searching back must still
    # find it, and must never stall inside such a line.
    root, file = make_codex(tmp_path, [["the real request"]])
    huge = json.dumps({"type": "response_item", "payload": {
        "type": "function_call_output", "output": "x" * 1_500_000}})
    with file.open("a") as handle:
        for _ in range(2):
            handle.write(huge + "\n")
    record, _ = read_metadata(root, str(file))
    assert record.last_request == "the real request"
    assert record.activity == "working"


def test_records_from_before_the_tail_fields_still_load(tmp_path):
    from session_ls.api import HistoryRecord
    old = {"key": "k", "agent": "pi", "root": "/r", "native_id": None, "cwd": "/c",
           "cwd_quality": "native", "started": "s", "last": "l", "title": "t", "file": "/f"}
    record = HistoryRecord(**old)
    assert (record.activity, record.last_request, record.branch) == ("", "", "")


# Titles: a first message too short to say anything gives way to the session's own
# name, when it has one.

def claude_user(text):
    return {"type": "user", "cwd": "/tmp", "timestamp": "2026-09-24T00:00:00Z",
            "sessionId": CLAUDE_NATIVE, "entrypoint": "cli",
            "message": {"role": "user", "content": text}}


def claude_name(kind, value):
    field = {"custom-title": "customTitle", "ai-title": "aiTitle", "agent-name": "agentName"}[kind]
    return {"type": kind, field: value, "sessionId": CLAUDE_NATIVE}


@pytest.mark.parametrize("text,weak", [
    ("", True), ("   ", True), ("pwd", True), ("hi", True), ("exit", True), ("继续任务", True),
    ("ls  -la", True), ("continue", True), ("为什么500", False), ("fix the login", False),
    ("磁盘又要满了", False), ("refactors", False),
])
def test_weak_title_rule(text, weak):
    from session_ls.api import weak_title
    assert weak_title(text) is weak


def test_a_weak_claude_message_gives_way_to_the_latest_native_title(tmp_path):
    root, file = make_claude(tmp_path, [
        claude_name("ai-title", "first guess"), claude_user("hi"),
        claude_name("ai-title", "debug the upload retry"),
        claude_name("agent-name", "uploader"),
    ])
    assert read_metadata(root, str(file))[0].title == "debug the upload retry"


def test_a_claude_rename_outranks_the_generated_title(tmp_path):
    root, file = make_claude(tmp_path, [
        claude_user("pwd"), claude_name("custom-title", "upload work"),
        claude_name("ai-title", "debug the upload retry"),
    ])
    assert read_metadata(root, str(file))[0].title == "upload work"


def test_a_real_first_message_keeps_the_title(tmp_path):
    root, file = make_claude(tmp_path, [
        claude_user("fix the upload retry"), claude_name("custom-title", "upload work"),
    ])
    assert read_metadata(root, str(file))[0].title == "fix the upload retry"


def test_a_native_title_past_the_head_is_found_in_the_tail(tmp_path):
    filler = [claude_turn("assistant", [{"type": "text", "text": "x"}], stop="end_turn")] * 30
    root, file = make_claude(tmp_path, [claude_user("hi"), *filler,
                                        claude_name("ai-title", "late title")])
    assert read_metadata(root, str(file), max_lines=5)[0].title == "late title"


def test_a_weak_message_without_a_native_title_stays(tmp_path):
    root, file = make_claude(tmp_path, [claude_user("hi")])
    with file.open("a") as handle:
        handle.write('{"type": "assistant", "being written')  # read past the title
    record = read_metadata(root, str(file))[0]
    assert record.title == "hi" and record.status == "available", record.problems


def test_a_named_pi_session(tmp_path):
    root, file = make_pi(tmp_path, title="hi")
    with file.open("a") as handle:
        handle.write(json.dumps({"type": "session_info", "name": "tune the pool"}) + "\n")
    assert read_metadata(root, str(file))[0].title == "tune the pool"
    with file.open("a") as handle:
        handle.write(json.dumps({"type": "session_info", "name": ""}) + "\n")
    assert read_metadata(root, str(file))[0].title == "hi", "an emptied name is cleared"


def write_thread_names(root, *pairs):
    with (Path(root.path) / "session_index.jsonl").open("a") as handle:
        for native, name in pairs:
            handle.write(json.dumps({"id": native, "thread_name": name,
                                     "updated_at": "2026-09-28T00:00:00Z"}) + "\n")


def test_codex_thread_names_title_weak_sessions(tmp_path):
    root, file = make_codex(tmp_path, [["继续"]])
    index = HistoryIndex([root], tmp_path / "cache/data.json")
    assert [r.title for r in index.scan().records] == ["继续"]
    write_thread_names(root, (CODEX_NATIVE, "first name"), (CODEX_PARENT, "other"),
                       (CODEX_NATIVE, "retry the upload"))
    assert [r.title for r in index.scan().records] == ["retry the upload"], "the last name wins"
    assert [r.title for r in HistoryIndex([root], None).scan().records] == ["retry the upload"]
    write_thread_names(root, (CODEX_NATIVE, ""))
    assert [r.title for r in index.scan().records] == ["继续"], "an emptied name is cleared"


def test_codex_thread_names_do_not_replace_a_real_request(tmp_path):
    root, file = make_codex(tmp_path, [["fix the upload retry"]])
    write_thread_names(root, (CODEX_NATIVE, "upload"))
    assert [r.title for r in HistoryIndex([root]).scan().records] == ["fix the upload retry"]


# Scripted sessions: started with no person at a prompt.

@pytest.mark.parametrize("entrypoint,scripted", [("cli", False), ("sdk-cli", True),
                                                 ("sdk-ts", True), ("claude-vscode", False)])
def test_claude_scripted_sessions(tmp_path, entrypoint, scripted):
    root, file = make_claude(tmp_path, [{**claude_user("run the checks"), "entrypoint": entrypoint}])
    assert read_metadata(root, str(file))[0].scripted is scripted


@pytest.mark.parametrize("meta,scripted", [
    ({"originator": "codex_exec", "source": "exec"}, True),
    ({"originator": "codex_exec", "source": {"subagent": {"other": "x"}}}, True),
    ({"originator": "codex_sdk_ts", "source": "exec"}, True),
    ({"originator": "codex-tui", "source": "cli"}, False),
    ({"originator": "Codex Desktop", "source": "vscode"}, False),
])
def test_codex_scripted_sessions(tmp_path, meta, scripted):
    root, file = make_codex(tmp_path, [["run the checks"]], meta)
    assert read_metadata(root, str(file))[0].scripted is scripted


def test_scripted_survives_the_cache_and_defaults_false(tmp_path):
    root, file = make_codex(tmp_path, [["run"]], {"originator": "codex_exec", "source": "exec"})
    cache = tmp_path / "cache.json"
    HistoryIndex([root], cache).scan()
    assert [r.scripted for r in HistoryIndex([root], cache).scan().records] == [True]
    root, file = make_pi(tmp_path / "pi")
    assert read_metadata(root, str(file))[0].scripted is False
