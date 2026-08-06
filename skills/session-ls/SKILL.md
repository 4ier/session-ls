---
name: session-ls
description: >-
  Cross-agent session history search tool. Use when the user wants to recall
  "did I do X before", "which session handled Y", find past conversations/
  sessions about a topic, or locate the file path of a session for further
  analysis. Covers local session stores of pi, codex, claude, and cursor.
  Usage: session-ls [KEYWORD] [-f] [-a AGENT] [-c CWD] [--since DATE]
  [--until DATE] [-n N] [-l] [--json]. With no keyword, lists all sessions
  (newest last-active first); with a keyword, searches titles (first real
  user message); -f searches full content (rg). Leave semantic judgment to
  the model: once candidates are found, read the raw files by path.
---

# session-ls

List and search historical sessions of all local coding agents
(pi / codex / claude / cursor).

## When to use

- The user asks "did I work on X before" / "which session handled Y"
- Cross-agent lookup of past tasks, debugging notes, design discussions
- Need the raw file path of a session for deeper analysis

## Basic usage

```bash
session-ls                      # all sessions, newest last-active first
session-ls KEYWORD              # search titles (first real user message), case-insensitive substring
session-ls KEYWORD -f           # full-content search (slow, GBs of logs, uses rg)
```

## Common combos

```bash
session-ls KEYWORD -a pi        # only one agent
session-ls KEYWORD -c SUBSTR    # only sessions under a cwd substring
session-ls KEYWORD --since 2026-08-01   # time filter
session-ls KEYWORD -n 10        # only the N newest
session-ls KEYWORD -l           # print file paths only, for piping
session-ls KEYWORD --json       # JSONL output (agent/cwd/started/last/title/file)
```

## Output notes

- Per line: agent, start time, last active, cwd, title (first real user message)
- Titles skip injected system context (e.g. codex `<recommended_plugins>`,
  AGENTS.md instructions)
- Cache at `~/.cache/session_ls_cache.json`, invalidated by size+mtime,
  repeat queries in milliseconds
- cursor sessions have no in-file timestamps, mtime is shown; cwd is
  reverse-derived from the directory name and may be imprecise (display only)

## Working with a model

```bash
session-ls KEYWORD -l | xargs head -1     # peek at raw heads of matches
session-ls KEYWORD --json | jq -r .file   # get file paths
```

Once you have paths, use a read tool on the session files to pull the
original request, decisions, and conclusions. Don't judge content by the
title alone; read the file for complex tasks.

## Caveats

- Pure local, no semantics: no RAG, no vectorization, no network
- Results are sorted by last-active, not relevance
- Use `-f` when needed, but GBs of logs take seconds; try title search first
