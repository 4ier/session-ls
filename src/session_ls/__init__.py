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
import argparse
import glob
import json
import os
import re
import signal

# The CLI tests replace this module attribute to prove that nothing shells out, so
# the name has to exist even though the parser itself never spawns a process.
import subprocess  # noqa: F401
import tempfile
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
CACHE = os.path.join(HOME, ".cache", "session_ls_cache.json")
PARSER_VERSION = 3  # invalidates cached metadata whenever parsing changes

# A user message often opens with context the client injected, not text the person
# typed: environment and plugin lists, AGENTS.md, attached-image wrappers, slash
# command echoes, and the prompts one agent writes for another. A part that starts
# with one of these is skipped, and the title comes from the rest.
_INJECTED_PREFIXES = ("<recommended_plugins>", "<environment_details>", "<environment_context>",
                      "<user_instructions>", "<system-reminder>", "# AGENTS.md",
                      "<codex_delegation>", "<codex_internal_context", "<heartbeat>",
                      "<subagent_notification>", "<user_action>", "<in-app-browser-context",
                      "<external_codex_apps_", "<turn_aborted>", "<image ", "</image>",
                      "<local-command-caveat>", "<local-command-stdout>", "<command-name>",
                      "<command-message>", "The following is the Codex agent history",
                      "<task-notification>", "<local-command-stderr>")

# Some clients wrap the person's words together with context in one text part. The
# words are recovered from the wrapper instead of losing the whole message:
# (how the part starts, where the person's own text is, keep the part if that is empty).
_OWN_TEXT = (
    # Codex desktop puts attached files, response annotations or referenced chats
    # first and the request itself under a fixed heading. An empty request means the
    # person only pasted or attached something, and that is what they sent.
    (("# Files mentioned by the user", "# Files pasted by the user", "# Response annotations",
      "## Referenced chats"), re.compile(r"## My request(?: for Codex)?:\s*(.*)\Z", re.S), True),
    # A thread goal: the objective is the person's own words.
    (('<codex_internal_context source="goal">',),
     re.compile(r"<objective>\s*(.*?)\s*</objective>", re.S), False),
    # Chat bridges (e.g. Feishu/Lark) send conventions first and the message last, as
    # <user_input>{"text": ...}</user_input>.
    (None, re.compile(r"<user_input>\s*(.*?)\s*</user_input>", re.S), False),
)


def _injected(t):
    """True if this user message is injected context, not the user's own text."""
    t = t.lstrip()
    return t.startswith(_INJECTED_PREFIXES) or "AGENTS.md instructions" in t


def _own_text(t):
    """The person's words inside a known wrapper; None if t is not wrapped."""
    for starts, pattern, keep_if_empty in _OWN_TEXT:
        if starts is not None and not t.startswith(starts):
            continue
        found = pattern.findall(t)
        if not found:
            continue
        text = found[-1].strip()
        if text.startswith("{"):
            try:
                value = json.loads(text)
            except ValueError:
                value = None
            if isinstance(value, dict) and isinstance(value.get("text"), str):
                text = value["text"].strip()
        return text or (t if keep_if_empty else "")
    return None


def _title(parts):
    """Join the parts of one user message that the person actually wrote."""
    kept = []
    for part in parts:
        t = part.strip() if isinstance(part, str) else ""
        if not t:
            continue
        own = _own_text(t)
        if own is None:
            own = "" if _injected(t) else t
        if own.startswith("<p>"):
            # Rich-text bridges wrap a plain message in paragraph tags.
            own = re.sub(r"</?p>", " ", own).strip()
        if own:
            kept.append(own)
    # A pasted link becomes [url](url) in some clients; the title needs it once.
    return re.sub(r"\[(https?://[^\]\s]+)\]\(\1\)", r"\1", " ".join(kept)).strip()


# ---- user-text extractors: line -> first real user text or '' --------------

def _pi_user(line):
    m = json.loads(line).get("message", {}) or {}
    if m.get("role") != "user":
        return ""
    content = m.get("content")
    if not isinstance(content, list):
        return ""
    return _title(p.get("text", "") for p in content
                  if isinstance(p, dict) and p.get("type") == "text")

def _codex_user(line):
    p = json.loads(line).get("payload", {}) or {}
    if p.get("role") != "user":
        return ""
    content = p.get("content")
    if not isinstance(content, list):
        return ""
    return _title(c.get("text", "") for c in content
                  if isinstance(c, dict) and c.get("type") in ("input_text", "text"))

def _claude_user(line):
    d = json.loads(line)
    if d.get("type") != "user":
        return ""
    c = (d.get("message") or {}).get("content")
    if isinstance(c, str):
        return _title([c])
    if isinstance(c, list):
        return _title(b.get("text", "") for b in c
                      if isinstance(b, dict) and b.get("type") == "text")
    return ""

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
    return h.get("cwd"), h.get("timestamp")

def _codex(f, head):
    h = json.loads(head[0])
    p = h.get("payload", {}) if h.get("type") == "session_meta" else None
    if not p:
        return None
    return p.get("cwd"), p.get("timestamp") or h.get("timestamp")

def _claude(f, head):
    # Claude 2.1.x prepends metadata records and a session may hold no
    # user/assistant message at all (opened, renamed, quit). Every such record
    # still carries the sessionId, so identity comes from that, not from a
    # message type that may never be written.
    found = None
    for line in head:
        try:
            d = json.loads(line)
        except ValueError:
            continue  # one bad line must not hide the session
        if not (d.get("sessionId") or d.get("type") in ("user", "assistant", "summary")):
            continue
        if found is None:
            found = [None, None]
        found[0] = found[0] or d.get("cwd")
        found[1] = found[1] or d.get("timestamp")
        if found[0] and found[1]:
            break
    return tuple(found) if found else None


def _native_title(head):
    """The native session title, used only when the session has no user text."""
    for line in head:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") in ("ai-title", "agent-name"):
            return (d.get("aiTitle") or d.get("agentName") or "").strip()
    return ""

def _cursor(f, head):
    # ~/.cursor/projects/<cwd-dir>/agent-transcripts/<id>/<id>.jsonl
    name = os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(f))))
    if not name:
        return "", None  # path too shallow (e.g. a fixture); cwd unknown
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
            if name == "cursor" and "/subagents/" in f:
                continue  # keep only main transcripts, skip subagent ones
            files.append((name, parse, user_text, f))
    return files

def _load_cache():
    try:
        with open(CACHE, encoding="utf-8") as f:
            data = json.load(f)
        # A cache written by different parsing rules must not be trusted.
        return data if isinstance(data, dict) and data.get("__parser__") == PARSER_VERSION else {}
    except Exception:
        return {}

def _save_cache(cache):
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".session-ls-", dir=os.path.dirname(CACHE))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CACHE)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def parse_all(files):
    cache = _load_cache()
    new_cache, dirty, rows = {}, False, []
    for name, parse, user_text, f in files:
        try:
            st = os.stat(f)
            sig = [st.st_size, st.st_mtime, PARSER_VERSION]
            c = cache.get(f)
            if c and c.get("sig") == sig:  # unchanged: reuse cached metadata
                new_cache[f] = c
                rows.append({k: v for k, v in c.items() if k != "sig"})
                continue
        except OSError:
            continue
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                head, title = [], ""
                for _ in range(2000):
                    raw = fh.readline()
                    if not raw:
                        break
                    line = raw.strip()
                    if not line:
                        continue
                    head.append(line)
                    try:
                        title = user_text(line)
                    except Exception:
                        title = ""  # one malformed line must not hide the session
                    if title:
                        break
                if not title:
                    title = _native_title(head)
        except Exception:
            continue
        try:
            r = parse(f, head) if head else None
        except Exception:
            continue
        if not r:
            continue
        cwd, started = r
        mtime = datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()
        row = {"agent": name, "cwd": cwd or "", "started": started or mtime,
               "last": mtime, "title": title, "file": f}
        new_cache[f] = {**row, "sig": sig}
        dirty = True
        rows.append(row)
    if dirty:
        _save_cache({"__parser__": PARSER_VERSION, **new_cache})
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

    if args.full and args.keyword and files:
        # Literal, decoded matching. No subprocess, option ambiguity, or ARG_MAX limit.
        from .api import contains_literal_file
        files = [entry for entry in files
                 if contains_literal_file(entry[3], [args.keyword.casefold()])]

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


def cli():
    """Console entry point; signal policy belongs here, never at import time."""
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    main()
