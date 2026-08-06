#!/usr/bin/env python3
"""session-ls - list and search session history across coding agents.

Default: list every session (newest first). With a keyword: search session
titles (first real user message). Add --full to grep full content (slow).

Add a new agent by appending one entry to REGISTRY:
(name, glob, meta-parser, user-text-extractor).
- meta-parser(f, head) -> (cwd, started_iso) or None (started may be None
  if the format has no timestamp; file mtime is used instead)
- user-text-extractor(line) -> first real user message text or ''
  ('' = keep scanning; drives the early-exit read)
"""
import argparse, glob, json, os, re, shutil, signal, subprocess
from datetime import datetime, timezone

signal.signal(signal.SIGPIPE, signal.SIG_DFL)
HOME = os.path.expanduser("~")
CACHE = os.path.join(HOME, ".cache", "session_ls_cache.json")

def _injected(t):
    """True if this user message is injected context, not the user's own text."""
    t = t.lstrip()
    return t.startswith("<") or "AGENTS.md instructions" in t

# ---- user-text extractors: line -> first real user text or '' --------------

def _pi_user(line):
    m = json.loads(line).get("message", {}) or {}
    if m.get("role") != "user":
        return ""
    t = " ".join(p.get("text", "") for p in m.get("content", [])
                 if p.get("type") == "text").strip()
    return t if t and not _injected(t) else ""

def _codex_user(line):
    p = json.loads(line).get("payload", {}) or {}
    if p.get("role") != "user":
        return ""
    t = " ".join(c.get("text", "") for c in p.get("content", [])
                 if c.get("type") in ("input_text", "text")).strip()
    return t if t and not _injected(t) else ""

def _claude_user(line):
    d = json.loads(line)
    if d.get("type") != "user":
        return ""
    c = d.get("message", {}).get("content")
    t = c if isinstance(c, str) else " ".join(
        b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    return t.strip()

def _cursor_user(line):
    d = json.loads(line)
    if d.get("role") != "user":
        return ""
    t = " ".join(b.get("text", "") for b in d.get("message", {}).get("content", [])
                 if isinstance(b, dict) and b.get("type") == "text").strip()
    # strip the <timestamp>...</timestamp> / <user_query> wrappers
    return re.sub(r"^<timestamp>.*?</timestamp>\s*", "", t).replace(
        "<user_query>", "").replace("</user_query>", "").strip()

# ---- meta-parsers: (f, head) -> (cwd, started_iso) or None ------------------

def _pi(f, head):
    h = json.loads(head[0])
    if h.get("type") != "session":
        return None
    return h.get("cwd"), h["timestamp"]

def _codex(f, head):
    h = json.loads(head[0])
    p = h.get("payload", {}) if h.get("type") == "session_meta" else None
    if not p:
        return None
    return p.get("cwd"), p.get("timestamp") or h.get("timestamp")

def _claude(f, head):
    for line in head:
        d = json.loads(line)
        if d.get("type") == "user":
            return d.get("cwd"), d.get("timestamp")
    return None

def _cursor(f, head):
    # ~/.cursor/projects/<cwd-dir>/agent-transcripts/<id>/<id>.jsonl
    name = os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(f))))
    # ponytail: '-'->'/' decode is best-effort; dirs with '-' in a segment
    # decode wrong. Only affects cwd display, harmless for search.
    cwd = "/" + name.replace("-", "/") if not name[0].isdigit() else name
    return cwd, None  # no timestamps in cursor transcripts; use mtime

REGISTRY = [
    # (name, glob, meta-parser, user-text-extractor)
    ("pi",     os.path.join(HOME, ".pi/agent/sessions/*/*.jsonl"), _pi, _pi_user),
    ("codex",  os.path.join(HOME, ".codex/sessions/*/*/*/rollout-*.jsonl"),
     _codex, _codex_user),
    ("codex",  os.path.join(HOME, ".codex/archived_sessions/rollout-*.jsonl"),
     _codex, _codex_user),
    ("claude", os.path.join(HOME, ".claude/projects/*/*.jsonl"), _claude, _claude_user),
    ("cursor", os.path.join(HOME, ".cursor/projects/*/agent-transcripts/*/*.jsonl"),
     _cursor, _cursor_user),
]

def collect():
    files = []
    for name, pattern, parse, user_text in REGISTRY:
        for f in glob.glob(pattern):
            if name == "cursor":
                # keep only the main transcript, skip subagents/
                if os.path.basename(f)[:-6] != os.path.basename(os.path.dirname(f)):
                    continue
            files.append((name, parse, user_text, f))
    return files

def _load_cache():
    try:
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_cache(cache):
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    tmp = CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)
    os.replace(tmp, CACHE)

def parse_all(files):
    cache = _load_cache()
    new_cache, dirty, rows = {}, False, []
    for name, parse, user_text, f in files:
        try:
            st = os.stat(f)
            sig = [st.st_size, st.st_mtime]
            c = cache.get(f)
            if c and c["sig"] == sig:  # unchanged: reuse cached metadata
                new_cache[f] = c
                rows.append({k: v for k, v in c.items() if k != "sig"})
                continue
        except OSError:
            continue
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                head, title = [], ""
                for _ in range(2000):
                    line = fh.readline().strip()
                    if not line:
                        break
                    head.append(line)
                    title = user_text(line)
                    if title:
                        break  # title found, stop reading
        except Exception:
            continue
        r = parse(f, head) if head else None
        if not r:
            continue
        cwd, started = r
        if not title:
            for line in head[1:]:
                title = user_text(line)
                if title:
                    break
        mtime = datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()
        row = {"agent": name, "cwd": cwd or "", "started": started or mtime,
               "last": mtime, "title": title, "file": f}
        new_cache[f] = {**row, "sig": sig}
        dirty = True
        rows.append(row)
    if dirty:
        _save_cache(new_cache)
    return rows

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("keyword", nargs="?", default="",
                    help="search session titles (first user message)")
    ap.add_argument("-f", "--full", action="store_true",
                    help="grep full session content (slow)")
    ap.add_argument("-a", "--agent", help="only this agent")
    ap.add_argument("-c", "--cwd", help="only sessions under this cwd substring")
    ap.add_argument("--since", metavar="DATE", help="started on/after (YYYY-MM-DD)")
    ap.add_argument("--until", metavar="DATE", help="last active on/before (YYYY-MM-DD)")
    ap.add_argument("-n", "--limit", type=int, help="show only N newest")
    ap.add_argument("-l", "--list", action="store_true", help="print file paths only")
    ap.add_argument("--json", action="store_true", help="print rows as JSON lines")
    args = ap.parse_args()

    files = collect()

    if args.full and args.keyword:
        # ripgrep if available (much faster over GBs), else plain grep
        rg = shutil.which("rg")
        cmd = [rg, "-l", "-i", args.keyword, *[f for _, _, _, f in files]] if rg \
            else ["grep", "-l", "-i", args.keyword, *[f for _, _, _, f in files]]
        out = subprocess.run(cmd, capture_output=True, text=True).stdout
        keep = set(out.splitlines())
        files = [x for x in files if x[3] in keep]

    rows = parse_all(files)

    kw = args.keyword.lower()
    if kw and not args.full:
        rows = [r for r in rows if kw in r["title"].lower()]
    if args.agent:
        rows = [r for r in rows if r["agent"] == args.agent]
    if args.cwd:
        rows = [r for r in rows if args.cwd in r["cwd"]]
    if args.since:
        rows = [r for r in rows if r["started"][:10] >= args.since]
    if args.until:
        rows = [r for r in rows if r["last"][:10] <= args.until]

    rows.sort(key=lambda r: r["last"], reverse=True)
    if args.limit:
        rows = rows[:args.limit]

    if args.json:
        for r in rows:
            print(json.dumps(r, ensure_ascii=False))
    elif args.list:
        for r in rows:
            print(r["file"])
    else:
        print(f"{'AGENT':<7} {'STARTED':<20} {'LAST':<20}  "
              f"{'CWD':<42} TITLE")
        print("-" * 130)
        for r in rows:
            cwd = r["cwd"] or r["file"]
            print(f"{r['agent']:<7} {r['started'][:19]:<20} {r['last'][:19]:<20}  "
                  f"{cwd[:42]:<42} {r['title'][:60]}")
        print(f"\n{len(rows)} sessions")

if __name__ == "__main__":
    main()
