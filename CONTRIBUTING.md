# Contributing to session-ls

Thanks for wanting to help. session-ls is intentionally small: one module,
zero runtime dependencies, no index, no network. Keep it that way.

## Development setup

```bash
git clone https://github.com/4ier/session-ls
cd session-ls
python -m venv .venv && source .venv/bin/activate
pip install -e .
pip install pytest

session-ls -n 5        # smoke test against your own session stores
python -m pytest tests/
```

Tests are plain asserts and run with or without pytest
(`python3 tests/test_session_ls.py` works from a checkout).

## How session-ls works

Every run:

1. `collect()` globs each `REGISTRY` entry and produces
   `(name, meta_parser, user_text, path)` tuples.
2. `parse_all()` stats every file. Unchanged files (same size + mtime) are
   served from `~/.cache/session_ls_cache.json`; only changed/new files are
   opened.
3. For a new file it reads lines one at a time, stopping as soon as
   `user_text(line)` returns a non-empty string (the session title). The
   head of the file is handed to `meta_parser` for cwd + start time.
4. Filters, sort, output. `--full` greps the raw files with ripgrep (or
   plain grep) before step 2 so only matching files are parsed.

Everything is literal text matching. There is deliberately no index, no
embeddings, no semantic layer.

## Adding a new agent

Append one entry to `REGISTRY` at the bottom of
`src/session_ls/__init__.py`:

```python
REGISTRY = [
    # (name, glob, meta-parser, user-text-extractor)
    ("myagent", os.path.join(HOME, ".myagent/sessions/*.jsonl"),
     _myagent_meta, _myagent_user),
]
```

You provide two functions.

### 1. `meta_parser(f, head) -> (cwd, started_iso) | None`

Called once per session file with the path and the lines read so far.
Return the working directory and an ISO timestamp of when the session
started, or `None` to skip the file (e.g. it is not actually a session).

Rules:

- If the format has no usable start timestamp, return `None` for it -
  the file mtime is used instead.
- Only read from `head` (the first ~2000 lines). Do not seek around the
  file; `parse_all` only guarantees the head is available.

### 2. `user_text(line) -> str`

Called on every line as it is read. Return the first real user message of
the session, or `''` to keep reading. This both produces the title and
drives the early-exit: the file is never read past the first real user
message, which is what keeps listing fast over gigabytes of logs.

Rules:

- Skip anything that is not genuinely the user's own words. Codex injects
  `<recommended_plugins>`, `AGENTS.md instructions`, and environment
  blocks as user-role messages; `_injected()` detects those. If your agent
  does the same, reuse it.
- If the title carries wrappers (cursor wraps queries in
  `<timestamp>...</timestamp><user_query>...`), strip them before
  returning.

### Reference: the four built-in formats

| Agent | File layout | Meta | User text |
| --- | --- | --- | --- |
| pi | `~/.pi/agent/sessions/<cwd-encoded>/*.jsonl`; first line `{"type":"session","cwd":...,"timestamp":...}` | `_pi` | `_pi_user`: lines `message.role=="user"`, content `{type:"text"}` |
| codex | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`; first line `session_meta` with `payload.cwd/timestamp` | `_codex` | `_codex_user`: `payload.role=="user"`, content `{type:"input_text"}`; skips injected |
| claude | `~/.claude/projects/<cwd-encoded>/*.jsonl`; lines `{"type":"user",...,"cwd":...,"timestamp":...}`, content is a string or block array | `_claude` | `_claude_user`: content string or `{type:"text"}` blocks |
| cursor | `~/.cursor/projects/<cwd>/agent-transcripts/<id>/<id>.jsonl`; plain message stream, no timestamps | `_cursor` | `_cursor_user`: strips `<timestamp>`/`<user_query>` wrappers |

Things the built-ins handle that yours may not: sessions whose log is
only injected context (fall back to the first user text anyway), content
that is a bare string vs. a block array, and stores where only the main
transcript should count (cursor: skip `subagents/`).

## Testing a new agent

Add a fixture to `tests/test_session_ls.py` mirroring the real format
(the four existing fixtures show the expected shape), then assert title,
cwd, and the mtime fallback where applicable. The test sets
`session_ls.CACHE` to a temp path so it never touches your real cache.

Run `python -m pytest tests/` and keep the test pure - no real session
stores, no network.

## Pull request checklist

- Zero new runtime dependencies (stdlib only).
- No semantic layer, no index, no network calls.
- Listing stays fast: the early-exit read must still stop at the first
  real user message. Add the agent's user-text extractor, not a
  full-file scan.
- `REGISTRY` entry + fixture test included.
- If CLI output or flags changed, update `README.md` and
  `man/session-ls.1`.
- If the metadata cache schema changed, note it in the PR (the cache is
  keyed by size+mtime and self-heals, but stale entries are discarded).

## Releasing (maintainers)

1. Bump `version` in `pyproject.toml`.
2. Commit and push.
3. Tag and push:

   ```bash
   git tag v0.1.1
   git push origin v0.1.1
   ```

4. The `publish` workflow runs tests, builds the wheel/sdist, uploads to
   PyPI, and creates a GitHub release with auto-generated notes.

One-time setup on PyPI (only needed once, per project):

1. Create the project at https://pypi.org/manage/projects/ (name
   `session-ls`) - or let the first publish create it.
2. Add a trusted publisher: *Add a new publisher*, owner
   `4ier`, repository `session-ls`, workflow name `publish`.
   This replaces API tokens entirely; nothing is stored in the repo.

## Code of conduct

Be kind, be specific. That's it.
