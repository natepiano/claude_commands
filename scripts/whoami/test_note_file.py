"""Behavioral contract for reading agent notes with unusable neighbors."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import final, override
from unittest.mock import patch

import note_file


@final
class NoteFileTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = self.enterContext(patch.object(note_file, "AGENTS_DIR", self.root))

    def test_non_utf8_note_reads_as_unavailable(self) -> None:
        path = self.root / "claude 1.md"
        _ = path.write_bytes(b"---\nlogin: \xff\n---\n")
        self.assertIsNone(note_file.read_note(path))

    def test_read_notes_skips_non_utf8_notes_and_keeps_readable_ones(self) -> None:
        broken = self.root / "claude 1.md"
        _ = broken.write_bytes(b"---\nlogin: \xff\n---\n")
        readable = [self.root / "claude 2.md", self.root / "codex 1.md"]
        for path in readable:
            _ = path.write_text("---\nlogin: readable@example.invalid\n---\n", encoding="utf-8")
        notes = note_file.read_notes()
        self.assertEqual([note.path for note in notes], readable)
        self.assertEqual([note.get("login") for note in notes], ["readable@example.invalid"] * 2)


if __name__ == "__main__":
    _ = unittest.main()
