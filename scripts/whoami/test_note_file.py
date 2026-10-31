"""Behavioral contract for reading agent notes with unusable neighbors."""

from __future__ import annotations

import sys
import os
import tempfile
import unittest
from pathlib import Path
from typing import cast, final, override
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.whoami import note_file


@final
class NoteFileTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = self.enterContext(patch.object(note_file, "AGENTS_DIR", self.root))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"HOME": str(self.root)})))
        _ = os.environ.pop("AGENT_NOTES_DIR", None)

    def test_non_utf8_note_reads_as_unavailable(self) -> None:
        path = self.root / "claude1.md"
        _ = path.write_bytes(b"---\nlogin: \xff\n---\n")
        self.assertIsInstance(note_file.read_note(path), note_file.UnreadableNote)

    def test_read_notes_skips_non_utf8_notes_and_keeps_readable_ones(self) -> None:
        broken = self.root / "claude1.md"
        _ = broken.write_bytes(b"---\nlogin: \xff\n---\n")
        readable = [self.root / "claude2.md", self.root / "codex1.md"]
        for path in readable:
            _ = path.write_text("---\nlogin: readable@example.invalid\n---\n", encoding="utf-8")
        notes = note_file.read_notes()
        self.assertEqual([note.path for note in notes], readable)
        self.assertEqual([note.get("login") for note in notes], ["readable@example.invalid"] * 2)

    def write_note(self, name: str, fields: str = "login: account@example.invalid\n") -> note_file.Note:
        path = self.root / name
        _ = path.write_text("---\n" + fields + "---\nKeep this body.\n")
        result = note_file.read_note(path)
        assert isinstance(result, note_file.Note)
        return result

    def test_account_stems_separate_numbered_accounts_from_unrelated_files(self) -> None:
        for stem, tool, number in (("codex2", "codex", 2), ("codex 2", "codex", 2),
                                   ("claude10", "claude", 10)):
            with self.subTest(stem=stem):
                parsed = note_file.parse_account_label(stem)
                self.assertIsInstance(parsed, note_file.NumberedAccountLabel)
                assert isinstance(parsed, note_file.NumberedAccountLabel)
                self.assertEqual((parsed.tool, parsed.number, parsed.label), (tool, number, f"{tool}{number}"))
        for stem in ("codex0", "codex", "default", "notes", "codex2.backup", "claude 01", "codex  2"):
            with self.subTest(stem=stem):
                parsed = note_file.parse_account_label(stem)
                self.assertIsInstance(parsed, note_file.InvalidAccountLabel)
                assert isinstance(parsed, note_file.InvalidAccountLabel)

    def test_numbered_notes_have_canonical_labels_and_codex_tool(self) -> None:
        codex = self.write_note("codex2.md")
        claude = self.write_note("claude10.md")
        self.assertEqual((codex.tool, codex.label), ("codex", "codex2"))
        self.assertEqual((claude.tool, claude.label), ("claude", "claude10"))
        _ = (self.root / "notes.md").write_text("---\nlogin: irrelevant@example.invalid\n---\n")
        self.assertIsInstance(note_file.read_note(self.root / "notes.md"), note_file.NotAccountNote)

    def test_note_names_validate_labels_and_do_not_create_files(self) -> None:
        self.assertEqual(note_file.note_name("codex2"), "codex2.md")
        self.assertEqual(note_file.note_name("codex 2"), "codex2.md")
        self.assertEqual(note_file.note_name("claude10"), "claude10.md")
        for label in ("default", "codex 0", "claude 01", "../../codex2"):
            with self.subTest(label=label), self.assertRaises(ValueError):
                _ = note_file.note_name(label)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_note_path_prefers_numbered_and_reads_legacy_only_when_alone(self) -> None:
        numbered, legacy = self.root / "codex2.md", self.root / "codex 2.md"
        self.assertEqual(note_file.note_path("codex2"), numbered)
        _ = self.write_note(legacy.name)
        self.assertEqual(note_file.note_path("codex2"), legacy)
        _ = self.write_note(numbered.name)
        self.assertEqual(note_file.note_path("codex2"), numbered)
        legacy.unlink()
        self.assertEqual(note_file.note_path("codex2"), numbered)

    def test_read_notes_deduplicates_labels_and_ignores_non_accounts(self) -> None:
        _ = self.write_note("codex 2.md", "login: old@example.invalid\n")
        _ = self.write_note("codex2.md", "login: new@example.invalid\n")
        _ = self.write_note("claude1.md")
        _ = (self.root / "notes.md").write_text("---\nlogin: not-an-account\n---\n")
        notes = note_file.read_notes()
        self.assertCountEqual([note.label for note in notes], ["claude1", "codex2"])
        chosen = next(note for note in notes if note.label == "codex2")
        self.assertEqual(chosen.get("login"), "new@example.invalid")
        self.assertEqual(chosen.path.name, "codex2.md")

    def test_read_notes_orders_account_numbers_numerically(self) -> None:
        for name in ("codex10.md", "codex2.md", "claude10.md", "claude2.md"):
            _ = self.write_note(name)
        self.assertEqual([note.label for note in note_file.read_notes()],
                         ["claude2", "claude10", "codex2", "codex10"])

    def test_only_legacy_note_is_discovered_once(self) -> None:
        _ = self.write_note("codex 2.md")
        self.assertEqual([(note.label, note.path.name) for note in note_file.read_notes()],
                         [("codex2", "codex 2.md")])

    def test_malformed_frontmatter_and_missing_account_file_have_distinct_results(self) -> None:
        path = self.root / "codex2.md"
        for text in ("No frontmatter.\n", "---\nlogin: never closed\n"):
            _ = path.write_text(text)
            self.assertIsInstance(note_file.read_note(path), note_file.MalformedNote)
        path.unlink()
        self.assertIsInstance(note_file.read_note(path), note_file.UnreadableNote)

    def test_list_fields_read_obsidian_blocks_flow_scalars_and_absence(self) -> None:
        note = self.write_note("claude1.md", "role:\n  - showrunner\n  - unit\nflow: [showrunner, unit]\nempty: []\nscalar: worker\nreturn:\n  - \"[[agents.base]]\"\n")
        self.assertEqual(note.get_list("role"), ("showrunner", "unit"))
        self.assertEqual(note.get_list("flow"), ("showrunner", "unit"))
        self.assertEqual(note.get_list("scalar"), ("worker",))
        self.assertEqual(note.get_list("empty"), ())
        self.assertEqual(note.get_list("absent"), ())

    def test_roles_or_live_sessions_determine_whether_a_note_is_in_use(self) -> None:
        for fields, expected in (("role:\n  - unit\nsessions: 0\n", True),
                                 ("role: []\nsessions: 2\n", True),
                                 ("role: []\nsessions: 0\n", False),
                                 ("sessions: unknown\n", False), ("", False)):
            with self.subTest(fields=fields):
                note = self.write_note("claude1.md", fields)
                self.assertEqual(note_file.in_use(note), expected)

    def test_note_directory_uses_relocation_or_the_current_module_default(self) -> None:
        relocated = self.root / "relocated"
        with patch.dict(os.environ, {"AGENT_NOTES_DIR": str(relocated)}):
            self.assertEqual(note_file.notes_dir(), relocated)
            self.assertEqual(note_file.note_path("claude1"), relocated / "claude1.md")
        changed = self.root / "changed-default"
        with patch.object(note_file, "AGENTS_DIR", changed):
            self.assertEqual(note_file.notes_dir(), changed)


if __name__ == "__main__":
    _ = unittest.main()
