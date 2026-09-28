# Changelog

## Unreleased

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
