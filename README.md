# session-ls

List and search session history across all coding agents on your machine:
pi, codex, claude, cursor.

```
$ session-ls 局域网
AGENT   STARTED              LAST                  CWD                     TITLE
pi      2026-07-17T14:42:45  2026-07-18T08:45:13   /Users/fourier           局域网中有一台android设备，上面跑了termux...
pi      2026-06-15T03:08:57  2026-06-15T03:31:55   /Users/fourier           为啥我局域网连接了linux-ap，跑lazycat vpn...
```

Sessions are read directly from each agent's local store, newest first. The
title of a session is its first real user message (injected context such as
codex `<recommended_plugins>` or `AGENTS.md` instructions is skipped).

## Design

- **Fast.** Metadata is cached in `~/.cache/session_ls_cache.json`, keyed by
  file size + mtime; unchanged files are never re-read. Listing ~1000
  sessions takes milliseconds. Full-text search uses `ripgrep` when
  available (fallback: `grep`).
- **Plain search, no semantics.** No index, no embeddings, no network.
  Matching is literal substring comparison. Decide what's relevant
  yourself — or hand the file paths to an LLM.
- **Lightweight, zero dependencies.** Pure stdlib, one module.
- **Extensible.** Adding another agent is one `REGISTRY` entry plus two
  small functions (see below).

## Install

```bash
pip install .            # from a checkout
pipx install .           # recommended: isolated environment
```

Requires Python >= 3.9. A man page (`session-ls(1)`) is installed alongside;
on macOS venvs, point `MANPATH` at the venv's `share/man` to see it.

## Usage

```
session-ls [KEYWORD] [OPTIONS]
```

| Option | Meaning |
| --- | --- |
| `KEYWORD` | search titles (first user message), case-insensitive substring |
| `-f, --full` | search full session content instead of titles (slow, uses rg/grep) |
| `-a, --agent` | only this agent: `pi`, `codex`, `claude`, `cursor` |
| `-c, --cwd` | only sessions under a cwd substring |
| `--since DATE` | started on/after (YYYY-MM-DD) |
| `--until DATE` | last active on/before (YYYY-MM-DD) |
| `-n, --limit N` | show only the N newest |
| `-l, --list` | print file paths only (for piping) |
| `--json` | JSON Lines output (keys: agent, cwd, started, last, title, file) |

### Examples

```bash
session-ls -n 10                              # ten most recent sessions
session-ls 局域网                              # title search
session-ls "immich 2283" -f                  # full-content search
session-ls -a pi -c nemo --since 2026-08-01   # filters combine
session-ls 局域网 -l | xargs head -1          # inspect raw matches
session-ls 局域网 --json | jq -r .file        # feed paths to other tools
```

## Supported agents

| Agent | Store | Timestamps |
| --- | --- | --- |
| pi | `~/.pi/agent/sessions/<encoded-cwd>/*.jsonl` | in file |
| codex | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` (+ `archived_sessions/`) | in file |
| claude | `~/.claude/projects/<encoded-cwd>/*.jsonl` | in file |
| cursor | `~/.cursor/projects/*/agent-transcripts/<id>/<id>.jsonl` | file mtime (none in file) |

## Adding an agent

Append an entry to `REGISTRY` in `src/session_ls/__init__.py`:

1. a glob of session files
2. `meta_parser(f, head) -> (cwd, started_iso) | None`
3. `user_text(line) -> first real user text | ''` (drives the early-exit read)

```python
REGISTRY = [
    # (name, glob, meta-parser, user-text-extractor)
    ("myagent", os.path.join(HOME, ".myagent/sessions/*.jsonl"),
     _myagent_meta, _myagent_user),
]
```

## Development

```bash
python -m pytest tests/        # plain asserts, also runnable via pytest
python -m session_ls ...       # run from a checkout
```

## License

MIT
