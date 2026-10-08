#!/usr/bin/env python3
# 작성자: 최유준 / 작성 날짜: 2026/10/08
"""No-follow file access below a checkout root (hybrid-app #15). Standard library only.

An allowlist of path *strings* does not say where a file really lives: a symbolic link at the file
or at any directory on the way can point outside the checkout. Every helper here walks the path
with directory file descriptors (`O_DIRECTORY|O_NOFOLLOW` per component, then `O_NOFOLLOW` on the
file), so there is no check-then-use gap and nothing outside the root is ever read or written.

Rejected with code UNSAFE_PATH: a symlink at any component below the root, a non-directory parent,
a non-regular file, a hard-linked file (link count > 1). The root itself may be a symlink (a
workspace link). Without POSIX `O_NOFOLLOW` support the helpers refuse to run (PLATFORM_UNSUPPORTED).
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from typing import Optional


class SafeFsError(RuntimeError):
    """Fail-closed error. `code` is stable and safe to print; the message names only the relative path."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _require_nofollow() -> None:
    if not (hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY") and os.open in os.supports_dir_fd
            and os.mkdir in os.supports_dir_fd):
        raise SafeFsError("PLATFORM_UNSUPPORTED", "no-follow file access is not available on this platform")


def _unsafe(rel: str, why: str) -> SafeFsError:
    return SafeFsError("UNSAFE_PATH", f"{rel}: {why}")


def _split(rel: str) -> list:
    parts = rel.split("/") if isinstance(rel, str) else []
    if not parts or any(part in ("", ".", "..") for part in parts) or "\x00" in rel or "\\" in rel:
        raise SafeFsError("PATH_INVALID", f"{rel!r}: not a clean relative path")
    return parts


def open_no_follow(root: Path, rel: str, flags: int, make_dirs: bool = False) -> int:
    """Open `rel` below `root`; returns an fd. FileNotFoundError when a component is missing
    (unless `make_dirs`, which creates missing parent directories without following anything)."""
    _require_nofollow()
    parts = _split(rel)
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise SafeFsError("CHECKOUT_UNREADABLE", f"checkout root cannot be opened ({type(exc).__name__})")
    try:
        for part in parts[:-1]:
            try:
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not make_dirs:
                    raise
                try:
                    os.mkdir(part, 0o755, dir_fd=fd)
                    nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except OSError as exc:
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EEXIST):
                        raise _unsafe(rel, "a parent directory is a symbolic link or not a directory")
                    raise SafeFsError("INPUT_UNREADABLE", f"{rel}: cannot create parent ({type(exc).__name__})")
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise _unsafe(rel, "a parent directory is a symbolic link or not a directory")
                raise SafeFsError("INPUT_UNREADABLE", f"{rel}: cannot open parent ({type(exc).__name__})")
            os.close(fd)
            fd = nxt
        try:
            return os.open(parts[-1], flags | os.O_NOFOLLOW, 0o644, dir_fd=fd)
        except (FileNotFoundError, FileExistsError):
            raise
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
                raise _unsafe(rel, "the file is a symbolic link")
            raise SafeFsError("INPUT_UNREADABLE", f"{rel}: cannot open ({type(exc).__name__})")
    finally:
        os.close(fd)


def check_regular(fd: int, rel: str) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise _unsafe(rel, "not a regular file")
    if info.st_nlink != 1:
        raise _unsafe(rel, "the file has more than one hard link")


def read_text(root: Path, rel: str) -> Optional[str]:
    """UTF-8 text with line endings untouched, or None when the file does not exist."""
    try:
        fd = open_no_follow(root, rel, os.O_RDONLY)
    except FileNotFoundError:
        return None
    try:
        check_regular(fd, rel)
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return handle.read().decode("utf-8")
    finally:
        os.close(fd)


def verify_writable(root: Path, rel: str, make_dirs: bool = False) -> None:
    """Raise SafeFsError if `rel` could not be written safely (does not create or change the file)."""
    try:
        fd = open_no_follow(root, rel, os.O_RDONLY, make_dirs=False)
    except FileNotFoundError:
        return  # absent file / parent: creating it is safe if the parents are real directories
    try:
        check_regular(fd, rel)
    finally:
        os.close(fd)


def write_text(root: Path, rel: str, text: str, make_dirs: bool = False) -> None:
    """Create `rel` (exclusive) or rewrite an existing regular file; never follows a symlink."""
    data = text.encode("utf-8")
    try:
        fd = open_no_follow(root, rel, os.O_WRONLY | os.O_CREAT | os.O_EXCL, make_dirs=make_dirs)
    except FileExistsError:
        fd = open_no_follow(root, rel, os.O_WRONLY)  # verified below, before anything is truncated
        try:
            check_regular(fd, rel)
            os.ftruncate(fd, 0)
        except BaseException:
            os.close(fd)
            raise
    except FileNotFoundError:
        raise SafeFsError("INPUT_UNREADABLE", f"{rel}: parent directory does not exist")
    try:
        check_regular(fd, rel)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
    finally:
        os.close(fd)
