#!/usr/bin/env python3
# 작성자: 최유준 / 작성 날짜: 2026/10/08
"""Unit tests for scripts/safe_fs.py (hybrid-app #15; temporary directories only)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import safe_fs as target  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "root"
        (self.root / "a/b").mkdir(parents=True)
        self.outside = self.base / "outside.txt"
        self.outside.write_text("OUTSIDE\n", encoding="utf-8")
        self.ext = self.base / "ext"
        self.ext.mkdir()

    def code(self, call, *args, **kw) -> str:
        with self.assertRaises(target.SafeFsError) as ctx:
            call(*args, **kw)
        return ctx.exception.code


class ReadTests(Base):
    def test_reads_exact_bytes_and_absent_is_none(self) -> None:
        (self.root / "a/b/f.txt").write_bytes("한글\r\nline\r\n".encode("utf-8"))
        self.assertEqual(target.read_text(self.root, "a/b/f.txt"), "한글\r\nline\r\n")  # CRLF untouched
        self.assertIsNone(target.read_text(self.root, "a/b/none.txt"))
        self.assertIsNone(target.read_text(self.root, "zz/none.txt"))

    def test_symlinks_hard_links_and_non_regular_files_are_rejected(self) -> None:
        (self.root / "a/link.txt").symlink_to(self.outside)
        self.assertEqual(self.code(target.read_text, self.root, "a/link.txt"), "UNSAFE_PATH")
        (self.root / "a/dangling.txt").symlink_to(self.base / "nope")
        self.assertEqual(self.code(target.read_text, self.root, "a/dangling.txt"), "UNSAFE_PATH")
        os.link(self.outside, self.root / "a/hard.txt")
        self.assertEqual(self.code(target.read_text, self.root, "a/hard.txt"), "UNSAFE_PATH")
        (self.root / "a/dir.txt").mkdir()
        self.assertEqual(self.code(target.read_text, self.root, "a/dir.txt"), "UNSAFE_PATH")

    def test_symlinked_parent_directories_are_rejected_at_any_depth(self) -> None:
        (self.ext / "f.txt").write_text("EXT", encoding="utf-8")
        (self.root / "x").symlink_to(self.ext)
        self.assertEqual(self.code(target.read_text, self.root, "x/f.txt"), "UNSAFE_PATH")
        (self.root / "a/y").symlink_to(self.ext)
        self.assertEqual(self.code(target.read_text, self.root, "a/y/f.txt"), "UNSAFE_PATH")
        (self.root / "file").write_text("x", encoding="utf-8")  # a file used as a directory
        self.assertEqual(self.code(target.read_text, self.root, "file/f.txt"), "UNSAFE_PATH")

    def test_root_may_be_a_symlink_but_not_a_missing_directory(self) -> None:
        link = self.base / "ws"
        link.symlink_to(self.root)
        (self.root / "a/f.txt").write_text("ok", encoding="utf-8")
        self.assertEqual(target.read_text(link, "a/f.txt"), "ok")
        self.assertEqual(self.code(target.read_text, self.base / "missing", "a/f.txt"), "CHECKOUT_UNREADABLE")

    def test_unclean_relative_paths_are_rejected(self) -> None:
        for rel in ("", "/abs", "../x", "a/../x", "a//b", "./a", "a\\b", "a/\x00"):
            with self.subTest(rel=rel):
                self.assertEqual(self.code(target.read_text, self.root, rel), "PATH_INVALID")

    def test_platform_without_nofollow_is_refused(self) -> None:
        with mock.patch.object(target.os, "supports_dir_fd", set()):
            self.assertEqual(self.code(target.read_text, self.root, "a/f.txt"), "PLATFORM_UNSUPPORTED")


class WriteTests(Base):
    def test_creates_then_rewrites_in_place_with_exact_bytes(self) -> None:
        target.write_text(self.root, "a/b/n.txt", "first\r\n")
        self.assertEqual((self.root / "a/b/n.txt").read_bytes(), b"first\r\n")
        target.write_text(self.root, "a/b/n.txt", "x")
        self.assertEqual((self.root / "a/b/n.txt").read_bytes(), b"x")  # truncated, not appended

    def test_make_dirs_creates_real_directories_only(self) -> None:
        target.write_text(self.root, "new/dir/f.txt", "t", make_dirs=True)
        self.assertEqual((self.root / "new/dir/f.txt").read_text(encoding="utf-8"), "t")
        self.assertEqual(self.code(target.write_text, self.root, "other/f.txt", "t"), "INPUT_UNREADABLE")
        self.assertFalse((self.root / "other").exists())

    def test_nothing_outside_is_ever_changed(self) -> None:
        (self.root / "a/link.txt").symlink_to(self.outside)
        self.assertEqual(self.code(target.write_text, self.root, "a/link.txt", "PWNED"), "UNSAFE_PATH")
        ghost = self.base / "ghost.txt"
        (self.root / "a/ghost.txt").symlink_to(ghost)
        self.assertEqual(self.code(target.write_text, self.root, "a/ghost.txt", "PWNED"), "UNSAFE_PATH")
        (self.root / "d").symlink_to(self.ext)
        self.assertEqual(self.code(target.write_text, self.root, "d/f.txt", "PWNED", make_dirs=True), "UNSAFE_PATH")
        self.assertEqual(self.code(target.write_text, self.root, "d/sub/f.txt", "PWNED", make_dirs=True), "UNSAFE_PATH")
        os.link(self.outside, self.root / "a/hard.txt")
        self.assertEqual(self.code(target.write_text, self.root, "a/hard.txt", "PWNED"), "UNSAFE_PATH")
        self.assertEqual(self.outside.read_text(encoding="utf-8"), "OUTSIDE\n")
        self.assertFalse(ghost.exists())
        self.assertEqual(list(self.ext.iterdir()), [])

    def test_verify_writable_does_not_modify_anything(self) -> None:
        target.verify_writable(self.root, "a/b/new.txt")  # absent: fine
        target.verify_writable(self.root, "nodir/new.txt")  # absent parent: fine
        self.assertFalse((self.root / "a/b/new.txt").exists())
        (self.root / "a/link.txt").symlink_to(self.outside)
        self.assertEqual(self.code(target.verify_writable, self.root, "a/link.txt"), "UNSAFE_PATH")
        (self.root / "d").symlink_to(self.ext)
        self.assertEqual(self.code(target.verify_writable, self.root, "d/f.txt"), "UNSAFE_PATH")


class SourceTests(unittest.TestCase):
    def test_module_has_no_network_process_or_secret_access(self) -> None:
        src = (Path(__file__).resolve().parent / "safe_fs.py").read_text(encoding="utf-8")
        for needle in ("import subprocess", "import urllib", "import socket", "os.environ", "os.system"):
            self.assertNotIn(needle, src, needle)


if __name__ == "__main__":
    unittest.main()
