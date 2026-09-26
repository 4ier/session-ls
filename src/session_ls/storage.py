"""Private, atomic local files. No filesystem access at import time."""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import stat
import tempfile
import time
from pathlib import Path


class StorageError(OSError):
    """Untrusted path, incompatible data, or unavailable local storage."""


def private_dir(path: Path) -> Path:
    """Create only this directory tree; reject links and shared owned leaf directories."""
    path = Path(os.path.abspath(path))
    # Permit only the OS-owned macOS /tmp and /var aliases; reject user-controlled
    # symlink ancestors instead of silently redirecting state writes.
    for ancestor in [*reversed(path.parents), path]:
        if ancestor.is_symlink():
            if str(ancestor) in ("/tmp", "/var") and ancestor.lstat().st_uid == 0:
                continue
            raise StorageError("State path contains a symbolic link")
    # Ancestors may be OS-managed (/tmp, /home). Never chmod them.
    missing = []
    node = path
    while not node.exists() and not node.is_symlink():
        missing.append(node)
        node = node.parent
    if node.is_symlink():
        raise StorageError("State path must not be a symbolic link")
    for child in reversed(missing):
        try:
            child.mkdir(mode=0o700)
        except FileExistsError:
            pass
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise StorageError("State directory must be a directory owned by the current user")
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise StorageError("State directory must have mode 0700 (refusing shared directory)")
    return path


def _check_file(fd: int) -> None:
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
        raise StorageError("Local state must be a regular file owned by the current user")
    if st.st_mode & 0o077:
        raise StorageError("Local state file must have mode 0600")


def read_json(path: Path, default=None):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        return default
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        _check_file(handle.fileno())
        if os.fstat(handle.fileno()).st_size > 64 * 1024 * 1024:
            raise StorageError("Local metadata file exceeds 64 MiB safety budget")
        try:
            return json.load(handle)
        except RecursionError:
            raise ValueError("Local metadata is too deeply nested") from None


def atomic_json(path: Path, value) -> None:
    parent = private_dir(path.parent)
    if path.is_symlink():
        raise StorageError("Refusing to replace a symbolic link")
    if path.exists():
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            raise StorageError("Refusing to replace a non-owned state file")
    fd, name = tempfile.mkstemp(prefix=".4top-", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


@contextlib.contextmanager
def file_lock(path: Path, timeout: float = 10):
    private_dir(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        _check_file(fd)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise StorageError("Another 4top operation holds this lock; retry later")
                time.sleep(0.025)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
