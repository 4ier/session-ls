# Changelog

## Unreleased

- `<task-notification>` (Claude Code telling the agent a background task finished)
  and `<local-command-stderr>` are injected context, not the person's request, so
  they are no longer a title or the latest request.
- Repeated `HistoryIndex.scan` calls in one process are cheap and return exactly
  what a scan from nothing returns. A directory whose inode, mtime and ctime are
  unchanged keeps its listing (creating, removing or renaming an entry changes
  them); a listing is reused only once its directory has been still for
  `listing_settle_seconds` (2 s), so a coarse filesystem clock cannot hide a change.
  Every session file is still `lstat`ed on every scan, so an append is never missed;
  a file whose size, times, inode, mode and owner are unchanged since it was opened
  keeps its record, and only changed files are opened. The metadata cache file is
  rewritten at most every `persist_seconds` (60 s) while sessions are being written;
  records in memory are always current. On a store of 2798 sessions, a scan with
  nothing changed went from 207 ms to 12 ms of CPU, and one with an active session
  from 275 ms to 9 ms. No API change: callers keep calling `scan()`.
- A first message too short to say anything ("hi", "pwd", "exit", "继续", or
  nothing: at most 8 terminal columns once whitespace is collapsed, a CJK character
  counting two) gives way to the session's own name when it has one: Claude's
  latest `custom-title` (a rename), else `ai-title`, else `agent-name`; Pi's latest
  `session_info` name; Codex's latest `thread_name` for the session in
  `session_index.jsonl`. A longer first message is still the title. On the same
  store 44 titles changed (27 of them on top-level sessions); 39 had been empty.
- `HistoryRecord.scripted` marks sessions started with no person at a prompt: a
  Claude `entrypoint` of `sdk-*` (`claude -p`, the Agent SDK) and a Codex
  `session_meta` with `originator: codex_exec` or `source: exec`. Pi records no such
  marker. It defaults to false for records from older caches. On the same store:
  769 of 2455 Codex sessions (236 of them subagents) and 9 of 49 Claude sessions.
- `PARSER_VERSION` 5 re-reads cached metadata once.

## 0.2.3 — 2026-09-29

- `HistoryRecord.activity`, `last_request` and `branch`, read from the end of a
  transcript. `activity` is "working" while the agent is mid-turn (its last record
  is a tool call or result) and "waiting" once it has handed the turn back
  (Claude `end_turn`, Codex `task_complete`, a Pi reply without a tool call). The
  latest request skips injected context and tool results and is searched up to
  4 MB back, past single lines longer than the read window. The branch comes from
  Claude's `gitBranch` and Codex's `session_meta.git`. On a store of 1397 sessions:
  request found for 1331, branch for 1046; a cold scan took 18 s, a warm one 0.3 s.
- `PARSER_VERSION` 4 re-reads cached metadata once.

## 0.2.2 — 2026-09-28

- Titles are the first thing the person typed. A Codex message carries plugin lists,
  `AGENTS.md` and the environment as parts of the same message as the request, and
  filtering the joined text dropped the request with them; parts are now filtered one
  by one. Known wrappers are unwrapped instead of becoming the title: Codex desktop's
  `## My request:` (attached files, annotations, referenced chats), a thread goal's
  `<objective>`, and chat bridges' `<user_input>{"text": ...}`. Image wrappers,
  slash-command echoes, `<local-command-caveat>`, heartbeats, delegations and the
  guardian reviewer's prompt are injected context. On one real store of 2773
  sessions, 1134 titles changed, and every top-level title that began with an
  injected block now starts with the person's words.
- `HistoryRecord.subagent` and `HistoryRecord.parent` mark sessions another agent
  session started for itself (Codex `thread_spawn` workers and the guardian approval
  reviewer): 1377 of 2441 Codex sessions on the same store. Both default to "not a
  subagent", so existing callers and cached records read unchanged. Identity comes
  from the first `session_meta`, even when a forked file carries more.
- `PARSER_VERSION` 3 re-reads cached metadata once.

## 0.2.1

- Carries the versioned `session_ls.api` and `session_ls.storage` modules, the
  literal decoded-text search, and the parser fixes that had been developed in the
  4top checkout: Claude 2.1.x metadata-only session files are recognized instead of
  rejected, a non-directory matched by a history pattern is skipped instead of
  reported as an unavailable source, and both parsers carry `PARSER_VERSION` 2.
- The 0.2.0 release on PyPI was built from that checkout and its metadata pointed
  here; this release is built from this repository, which is now the package's home.
- `session-ls` is unchanged as an interface: six JSON fields, no dependencies.

## 0.1.0

- First release: list and search session history across pi, codex, claude and
  cursor, with full-text search and an independent, stdlib-only CLI.
