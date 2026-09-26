"""Versioned, side-effect-free history API used by 4top.

Native stores are always read-only. Root paths, host identity and cache location
are injected by the caller; importing this module never scans the user's home.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shlex
import stat
import unicodedata
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import _claude_user, _codex_user, _cursor_user, _injected, _pi_user
from .storage import atomic_json, file_lock, read_json

API_VERSION = 1
PARSER_VERSION = 2
PATTERNS = {
    "claude": ("projects/*/*.jsonl",),
    "codex": ("sessions/*/*/*/rollout-*.jsonl", "archived_sessions/rollout-*.jsonl"),
    "pi": ("sessions/*/*.jsonl",),
    "cursor": ("projects/*/agent-transcripts/*/*.jsonl",),
}
EXTRACTORS = {"claude": _claude_user, "codex": _codex_user, "pi": _pi_user,
              "cursor": _cursor_user}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_text(text: object, multiline: bool = False) -> str:
    """Render data as text, stripping terminal controls (including OSC payloads)."""
    import re
    value = str(text) if text is not None else ""
    value = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\|$)", "", value)
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
    value = re.sub(r"\x1b[PX^_].*?(?:\x1b\\|$)", "", value, flags=re.DOTALL)
    value = re.sub(r"\x1b.", "", value)
    result = []
    for character in value:
        category = unicodedata.category(character)
        if character in "\n\t\r":
            result.append("\n" if multiline and character == "\n" else " ")
        elif category not in ("Cc", "Cf", "Cs"):
            result.append(character)
    return "".join(result)


def query_terms(query: str, tolerant: bool = False) -> list[str]:
    try:
        terms = shlex.split(query)
    except ValueError:
        if not tolerant:
            raise ValueError("Unclosed quote in search query") from None
        terms = query.split()
    return [term.casefold() for term in terms if term]


def history_key(host_id: str, agent: str, root: str, native_id: Optional[str],
                file: str = "") -> str:
    identity = native_id if native_id is not None else "file:" + file
    return "h_" + hashlib.sha256("\0".join((host_id, agent, root, identity)).encode()).hexdigest()[:32]


def is_uuid(value: Optional[str]) -> bool:
    try:
        return str(uuid.UUID(value or "")) == value.lower()
    except (ValueError, AttributeError, TypeError):
        return False


@dataclass(frozen=True)
class Root:
    agent: str
    path: str

    def __post_init__(self):
        if self.agent not in PATTERNS:
            raise ValueError("Unknown history agent: " + self.agent)
        object.__setattr__(self, "path", str(Path(self.path).expanduser().resolve()))


@dataclass(frozen=True)
class HistoryRecord:
    key: str
    agent: str
    root: str
    native_id: Optional[str]
    cwd: str
    cwd_quality: str
    started: str
    last: str
    title: str
    file: str
    status: str = "available"
    problems: tuple[str, ...] = ()

    @property
    def can_resume(self) -> bool:
        return (self.agent == "pi" or (self.agent in ("claude", "codex")
                and is_uuid(self.native_id))) and self.cwd_quality == "native"

    def legacy(self) -> dict:
        return {name: getattr(self, name) for name in
                ("agent", "cwd", "started", "last", "title", "file")}


@dataclass
class ScanResult:
    records: list[HistoryRecord] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    observed_at: str = field(default_factory=utc_now)
    cancelled: bool = False


@dataclass
class Excerpt:
    lines: list[str]
    next_cursor: Optional[int]
    label: str = "History excerpt (file order; not a reconstruction of active context)"
    issues: list[str] = field(default_factory=list)


def open_source(path: str, root: str):
    """Walk beneath an approved canonical root with O_NOFOLLOW at every component."""
    absolute = Path(os.path.abspath(path))
    approved = Path(root)
    relative = absolute.relative_to(approved)
    if not relative.parts or ".." in relative.parts:
        raise PermissionError("Source is outside its approved root")
    fd = os.open(approved, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=fd)
            os.close(fd)
            fd = child
        source = os.open(relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                         dir_fd=fd)
    finally:
        os.close(fd)
    try:
        st = os.fstat(source)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            raise PermissionError("Source must be a regular file owned by the current user")
    except BaseException:
        os.close(source)
        raise
    return os.fdopen(source, "rb")


def _text_field(value) -> Optional[str]:
    if isinstance(value, str) and value and "\x00" not in value:
        return value
    return None


def _date(value: Optional[str], fallback: str) -> str:
    try:
        date = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
        if date.tzinfo is None:
            return fallback
        return date.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError):
        return fallback


def read_metadata(root: Root, file: str, host_id: str = "local", max_bytes: int = 2**21,
                  max_lines: int = 2000) -> tuple[HistoryRecord, list]:
    native_id = cwd = started = title = native_title = None
    problems = []
    saw_metadata = False
    used = 0
    exhausted = False
    with open_source(file, root.path) as handle:
        st = os.fstat(handle.fileno())
        signature = [API_VERSION, PARSER_VERSION, st.st_size, st.st_mtime_ns, st.st_dev, st.st_ino]
        for _ in range(max_lines):
            line = handle.readline(max_bytes - used + 1)
            if not line:
                exhausted = True
                break
            used += len(line)
            if used > max_bytes:
                problems.append("Metadata byte budget reached")
                break
            try:
                data = json.loads(line)
            except (ValueError, UnicodeError, RecursionError):
                if line.strip():
                    problems.append("Malformed or incomplete JSON line")
                continue
            if not isinstance(data, dict):
                continue
            if root.agent == "pi" and data.get("type") == "session":
                saw_metadata = True
                native_id = native_id or _text_field(data.get("id"))
                cwd = cwd or _text_field(data.get("cwd"))
                started = started or _text_field(data.get("timestamp"))
            elif root.agent == "codex" and data.get("type") == "session_meta":
                payload = data.get("payload")
                if isinstance(payload, dict):
                    saw_metadata = True
                    native_id = native_id or _text_field(payload.get("id") or payload.get("session_id"))
                    cwd = cwd or _text_field(payload.get("cwd"))
                    started = started or _text_field(payload.get("timestamp") or data.get("timestamp"))
            elif root.agent == "claude":
                # Claude 2.1.x writes a metadata prelude (last-prompt, mode,
                # permission-mode, attachment, file-history-snapshot, cost-state,
                # ai-title) and may never write a user/assistant message, for
                # example a session that was opened, renamed and quit. Every such
                # record still carries the sessionId, so identity comes from it
                # rather than from a message type that may never appear.
                if _text_field(data.get("sessionId")) or data.get("type") in ("user", "assistant", "queue-operation", "summary"):
                    saw_metadata = True
                    native_id = native_id or _text_field(data.get("sessionId"))
                    cwd = cwd or _text_field(data.get("cwd"))
                    started = started or _text_field(data.get("timestamp"))
                if native_title is None and data.get("type") in ("ai-title", "agent-name"):
                    native_title = _text_field(data.get("aiTitle") or data.get("agentName"))
            elif root.agent == "cursor" and data.get("role") in ("user", "assistant"):
                saw_metadata = True
            if not title:
                try:
                    candidate = EXTRACTORS[root.agent](line.decode("utf-8", "replace"))
                    if candidate and not _injected(candidate):
                        title = candidate
                except (ValueError, TypeError, AttributeError):
                    pass
            if title and (native_id and cwd or root.agent == "cursor"):
                exhausted = True
                break
        if not exhausted and used <= max_bytes:
            problems.append("Metadata line budget reached")
    if not saw_metadata:
        if not any("budget" in problem for problem in problems):
            raise ValueError("Unrecognized history format")
        problems.append("Format not established within metadata budget")
    if saw_metadata and root.agent == "claude" and not native_id and is_uuid(Path(file).stem):
        native_id = Path(file).stem
    if root.agent == "cursor":
        native_id = Path(file).stem
        name = Path(file).parent.parent.parent.name
        cwd = "/" + name.replace("-", "/") if name else ""
        quality = "inferred" if cwd else "missing"
    else:
        quality = "native" if cwd and Path(cwd).is_absolute() else "missing"
    if native_id and (len(native_id) > 200 or clean_text(native_id) != native_id):
        native_id = None
        problems.append("Invalid native identifier")
    if not native_id:
        problems.append("Native identifier unavailable")
    # A real user message outranks the native session title, which is only used
    # when the session has no user text at all.
    title = title or native_title
    if not title:
        problems.append("No user title within metadata budget")
    last = datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()
    return HistoryRecord(
        history_key(host_id, root.agent, root.path, native_id, file), root.agent,
        root.path, native_id, cwd or "", quality, _date(started, last), last,
        title or "", file, "partial" if problems else "available", tuple(dict.fromkeys(problems))
    ), signature


def source_paths(root: Root, pattern: str, issues: list[str], cancel=None):
    """Bounded glob traversal that reports unreadable directories instead of hiding them."""
    parts = pattern.split("/")

    def walk(fd, directory, remaining):
        if cancel is not None and cancel.is_set():
            return
        try:
            with os.scandir(fd) as entries:
                names = [entry.name for entry in entries if fnmatch.fnmatchcase(entry.name, remaining[0])]
            for name in names:
                if name == "subagents":
                    continue
                path = directory / name
                if len(remaining) == 1:
                    yield path  # open_source separately validates file type / ownership.
                    continue
                child = None
                try:
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                    dir_fd=fd)
                    yield from walk(child, path, remaining[1:])
                except (FileNotFoundError, NotADirectoryError):
                    # The pattern matched something that is not, or is no longer, a
                    # directory: a marker file, a regular file, a raced removal. It was
                    # never a place sessions live, so this is not a degraded source.
                    continue
                except OSError as exc:
                    issues.append(f"{root.agent}: directory {clean_text(name)} unavailable ({type(exc).__name__})")
                finally:
                    if child is not None:
                        os.close(child)
        except OSError as exc:
            issues.append(f"{root.agent}: directory scan unavailable ({type(exc).__name__})")

    try:
        fd = os.open(root.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return
    except NotADirectoryError:
        issues.append(f"{root.agent}: configured root is not a directory")
        return
    try:
        yield from walk(fd, Path(root.path), parts)
    finally:
        os.close(fd)


class HistoryIndex:
    def __init__(self, roots: list[Root], cache_path: Optional[Path] = None,
                 host_id: str = "local", max_bytes: int = 2**21, max_lines: int = 2000):
        self.roots = roots
        self.cache_path = cache_path
        self.host_id = host_id
        self.max_bytes = max_bytes
        self.max_lines = max_lines
        self._memory = {}
        self._cache_stat = None

    def scan(self, cancel=None) -> ScanResult:
        result = ScanResult()
        cache = self._memory
        disk_stat = None
        if self.cache_path:
            try:
                st = self.cache_path.lstat()
                disk_stat = (st.st_size, st.st_mtime_ns, st.st_ino)
            except OSError:
                pass
        must_read = bool(self.cache_path and (not cache or disk_stat != self._cache_stat))
        cache_failed = False
        if must_read:
            cache = {}
            try:
                cached = read_json(self.cache_path, {})
                if isinstance(cached, dict) and cached.get("schema") == API_VERSION:
                    cache = cached.get("entries", {})
                    if not isinstance(cache, dict):
                        cache = {}
            except (OSError, ValueError):
                cache_failed = True
                result.issues.append("Metadata cache unavailable; rebuilding without trusting it")
        updated = {}
        unique = {}
        for root in self.roots:
            try:
                for pattern in PATTERNS[root.agent]:
                    for path in source_paths(root, pattern, result.issues, cancel):
                        if cancel is not None and cancel.is_set():
                            result.cancelled = True
                            result.records = list(unique.values())
                            return result
                        if "subagents" in path.relative_to(root.path).parts:
                            continue
                        file = str(path)
                        try:
                            # Validate containment and inode even when cached metadata is reused.
                            with open_source(file, root.path) as source:
                                st = os.fstat(source.fileno())
                            sig = [API_VERSION, PARSER_VERSION, st.st_size, st.st_mtime_ns,
                                   st.st_dev, st.st_ino, self.host_id, self.max_bytes, self.max_lines]
                            old = cache.get(file, {})
                            record = None
                            if isinstance(old, dict) and old.get("signature") == sig:
                                try:
                                    value = old["record"].copy()
                                    value["problems"] = tuple(value.get("problems", ()))
                                    candidate = HistoryRecord(**value)
                                    if (candidate.file == file and candidate.root == root.path
                                            and candidate.agent == root.agent
                                            and candidate.key == history_key(self.host_id, root.agent,
                                                root.path, candidate.native_id, file)
                                            and all(isinstance(getattr(candidate, f), str) for f in
                                                ("cwd", "cwd_quality", "title", "last", "started", "status"))):
                                        record = candidate
                                except (KeyError, TypeError, AttributeError, ValueError):
                                    pass
                            if record is None:
                                record, parsed_sig = read_metadata(root, file, self.host_id,
                                                                   self.max_bytes, self.max_lines)
                                sig = parsed_sig + [self.host_id, self.max_bytes, self.max_lines]
                            updated[file] = {"signature": sig, "record": asdict(record)}
                            previous = unique.get(record.key)
                            if previous is None or record.last > previous.last:
                                unique[record.key] = record
                        except (OSError, ValueError, TypeError) as exc:
                            # OSError text contains the private path; our own ValueError text does not.
                            detail = clean_text(str(exc))[:80] if isinstance(exc, (ValueError, TypeError)) else ""
                            result.issues.append(f"{root.agent}: {clean_text(path.name)}: {type(exc).__name__}"
                                                 + (f" ({detail})" if detail else ""))
            except OSError as exc:
                result.issues.append(f"{root.agent} source unavailable: {type(exc).__name__}")
        result.records = sorted(unique.values(), key=lambda row: row.last, reverse=True)
        if self.cache_path and (updated != cache or cache_failed):
            try:
                with file_lock(self.cache_path.with_suffix(".lock")):
                    atomic_json(self.cache_path, {"schema": API_VERSION, "entries": updated})
            except (OSError, ValueError):
                result.issues.append("Cannot save metadata cache; history remains readable")
        self._memory = updated
        if self.cache_path:
            try:
                st = self.cache_path.lstat()
                self._cache_stat = (st.st_size, st.st_mtime_ns, st.st_ino)
            except OSError:
                self._cache_stat = None
        return result


def matches_metadata(record: HistoryRecord, terms: list[str]) -> bool:
    text = "\n".join((record.title, record.cwd, record.agent, record.key,
                      record.native_id or "")).casefold()
    return all(term in text for term in terms)


def _search_stream(handle, terms: list[str], cancel=None) -> tuple[bool, bool]:
    remaining = set(terms)
    truncated = False
    if not remaining:
        return True, False
    while True:
        if cancel is not None and cancel.is_set():
            return False, truncated
        line = handle.readline(2**21 + 1)
        if not line:
            return False, truncated
        if len(line) > 2**21:
            truncated = True
        text = line.decode("utf-8", "replace") if isinstance(line, bytes) else line
        try:
            text += "\n" + json.dumps(json.loads(text), ensure_ascii=False)
        except (ValueError, TypeError, RecursionError):
            pass
        text = text.casefold()
        remaining = {term for term in remaining if term not in text}
        if not remaining:
            return True, truncated


def contains_literal_file(path: str, terms: list[str]) -> bool:
    """Compatibility helper for session-ls --full, preserving its six-field output."""
    if isinstance(terms, str):
        terms = [terms.casefold()]
    try:
        with open(path, "rb") as handle:
            return _search_stream(handle, terms)[0]
    except OSError:
        return False


def search_full(records: list[HistoryRecord], query: str, cancel=None) -> ScanResult:
    result = ScanResult()
    terms = query_terms(query)
    for record in records:
        if cancel is not None and cancel.is_set():
            result.cancelled = True
            break
        try:
            with open_source(record.file, record.root) as handle:
                found, truncated = _search_stream(handle, terms, cancel)
            if found:
                result.records.append(record)
            if truncated:
                result.issues.append(f"{record.key}: oversized lines; matches may be incomplete")
        except OSError as exc:
            result.issues.append(f"{record.key}: source unavailable ({type(exc).__name__})")
        if cancel is not None and cancel.is_set():
            result.cancelled = True
            break
    return result


def excerpt(record: HistoryRecord, cursor: int = 0, max_bytes: int = 2**18,
            max_lines: int = 200) -> Excerpt:
    lines = []
    issues = []
    with open_source(record.file, record.root) as handle:
        handle.seek(max(0, cursor))
        start = handle.tell()
        while handle.tell() - start < max_bytes and len(lines) < max_lines:
            raw = handle.readline(max_bytes - (handle.tell() - start))
            if not raw:
                break
            try:
                data = json.loads(raw)
                if not isinstance(data, dict):
                    continue
            except (ValueError, UnicodeError, RecursionError):
                if raw.strip():
                    issues.append("Malformed / partial lines omitted")
                continue
            message = data.get("message") or data.get("payload") or data
            if not isinstance(message, dict):
                continue
            role = message.get("role") or data.get("role")
            content = message.get("content")
            if isinstance(content, list):
                text = " ".join(str(part.get("text", "")) for part in content
                                if isinstance(part, dict) and part.get("type") in
                                ("text", "input_text", "output_text"))
            elif isinstance(content, str):
                text = content
            else:
                continue
            if role in ("user", "assistant") and text:
                lines.append(clean_text(f"{role}: {text}", multiline=True))
        position = handle.tell()
        more = bool(handle.read(1))
    return Excerpt(lines, position if more else None, issues=list(dict.fromkeys(issues)))
