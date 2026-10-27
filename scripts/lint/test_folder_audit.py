#!/usr/bin/env python3
"""Tests for folder_audit.py on a fabricated ~/rust, temp folder and transcript set. A test moves
each folder's time back two days unless it wants the folder recent."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from typing import override

import folder_audit

DAY = 86_400


def folder(path: Path, age_days: float = 2) -> Path:
    path.mkdir(parents=True)
    then = time.time() - age_days * DAY
    os.utime(path, (then, then))
    return path


class FolderAuditTests(unittest.TestCase):
    root: Path = Path()
    rust: Path = Path()
    temp: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.rust = self.root / "rust"
        self.temp = self.root / "tmp"
        self.rust.mkdir()
        self.temp.mkdir()

    def test_the_roots_file_ignores_comments_and_blank_lines(self) -> None:
        roots = self.root / "rust_roots.txt"
        _ = roots.write_text("# top\nhana\n\nbevy  # engine\n  hana videos\n")
        self.assertEqual(folder_audit.expected_rust_names(roots), frozenset({"hana", "bevy", "hana videos"}))
        self.assertEqual(folder_audit.expected_rust_names(self.root / "missing.txt"), frozenset())

    def test_only_unexpected_idle_folders_are_loose(self) -> None:
        _ = folder(self.rust / "hana")
        _ = folder(self.rust / "repro")
        stray = folder(self.rust / "hana_p34d_alone")
        _ = folder(self.rust / "fresh_try", age_days=0.1)
        _ = (self.rust / "notes.md").write_text("a file, not a folder")
        (self.rust / "link").symlink_to(self.rust / "hana")
        for name in ("claude", "claude-1000", "systemd-private-x", ".tmpAbC123", "tmux-1000"):
            _ = folder(self.temp / name)
        loose_temp = folder(self.temp / "my-repro")
        loose = folder_audit.audit(self.rust, [self.temp], frozenset({"hana"}), time.time(), files=[])
        self.assertEqual([entry.path for entry in loose], [stray, loose_temp])
        self.assertEqual({entry.maker for entry in loose}, {""})

    def test_the_earliest_started_session_that_names_a_folder_is_its_maker(self) -> None:
        stray = folder(self.rust / "stray")
        other = folder(self.rust / "other")
        prefix = folder(self.rust / "stra")
        maker = self.root / "p" / "aaaa.jsonl"
        talker = self.root / "p" / "bbbb.jsonl"
        untimed = self.root / "p" / "cccc.jsonl"
        maker.parent.mkdir()
        _ = maker.write_text(f'{{"timestamp":"2026-10-20T10:00:00.000Z","input":{{"command":"mkdir -p {stray} && echo \\"x\\""}}}}\n')
        _ = talker.write_text(f'{{"timestamp":"2026-10-25T10:00:00.000Z","input":{{"command":"ls {stray} {other}/a"}}}}\n')
        _ = untimed.write_text(f'{{"toolUseResult":{{"stdout":"{other}"}},"input":{{"command":"cd {other}"}}}}\n')
        lister = self.root / "p" / "dddd.jsonl"
        _ = lister.write_text(f'{{"timestamp":"2026-10-01T10:00:00.000Z","toolUseResult":{{"stdout":"{stray}"}}}}\n')
        os.utime(untimed, (time.time(), time.time()))
        named = folder_audit.makers([stray, other, prefix, self.rust / "unnamed"], [maker, talker, untimed, lister])
        self.assertEqual(named, {stray: "aaaa", other: "bbbb"})

    def test_the_message_lists_each_folder_and_names_scratch(self) -> None:
        loose = [folder_audit.Loose(Path("/home/u/rust/a"), 0.0, "s"), folder_audit.Loose(Path("/tmp/b"), 0.0, "s")]
        text = folder_audit.message(loose)
        self.assertIn("/home/u/rust/a, /tmp/b", text)
        self.assertIn("scratch.py adopt", text)


if __name__ == "__main__":
    _ = unittest.main()
