#!/usr/bin/env python3
"""Reading config/dailies.conf into one production's counts."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import override
from unittest import mock

from dailies_counts import CountsFailure, production_counts

LEDGER_RULE = r"— (proposed|deferred)$"


class DailiesCountsTests(unittest.TestCase):
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    config: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = self.root / "dailies.conf"
        self.enterContext(mock.patch.dict(os.environ, {"DAILIES_COUNTS_CONFIG": str(self.config),
                                                       "HOME": str(self.root)}))

    def configure(self, *lines: str) -> None:
        _ = self.config.write_text("# a comment\n\n" + "\n".join(lines) + "\n", encoding="utf-8")

    def test_counts_matching_lines_and_files_for_this_production_only(self) -> None:
        _ = (self.root / "ledger.md").write_text(
            "- a — proposed\n- b — accepted 2026-10-09\n- c — deferred\n", encoding="utf-8")
        proposals = self.root / "proposals"
        proposals.mkdir()
        for name in ("one.md", "two.md", "notes.txt"):
            _ = (proposals / name).write_text("", encoding="utf-8")
        (proposals / "folder.md").mkdir()
        self.configure(f"film.nightly-queue=lines ~/ledger.md {LEDGER_RULE}",
                       "film.proposals=files ~/proposals/*.md",
                       f"other.queue=lines ~/ledger.md {LEDGER_RULE}")
        self.assertEqual(production_counts("film"), [{"label": "nightly queue", "count": 2},
                                                     {"label": "proposals", "count": 2}])

    def test_counts_open_issues_with_the_tag_directly_in_the_folder(self) -> None:
        issues = self.root / "issues"
        (issues / "a proposal").mkdir(parents=True)
        for name, fields in (("open block", "status: open\ntags:\n  - issue\n  - proposal"),
                             ("open inline", 'status: "open"\ntags: [issue, proposal]'),
                             ("closed", "status: closed\ntags: [issue, proposal]"),
                             ("untagged", "status: open\ntags: [issue]"),
                             ("a proposal/notes", "status: open\ntags: [issue, proposal]")):
            _ = (issues / f"{name}.md").write_text(f"---\n{fields}\n---\nBody.\n", encoding="utf-8")
        self.configure("film.proposals=issues ~/issues proposal", "film.issues=issues ~/issues issue")
        self.assertEqual(production_counts("film"), [{"label": "proposals", "count": 2},
                                                     {"label": "issues", "count": 3}])

    def test_a_missing_source_reads_as_not_found(self) -> None:
        self.configure(f"film.queue=lines ~/absent.md {LEDGER_RULE}", "film.proposals=files ~/absent/*.md",
                       "film.open=issues ~/absent proposal")
        self.assertEqual(production_counts("film"), [{"label": "queue", "not_found": True},
                                                     {"label": "proposals", "not_found": True},
                                                     {"label": "open", "not_found": True}])

    def test_a_production_without_lines_or_a_config_has_no_counts(self) -> None:
        self.assertEqual(production_counts("film"), [])
        self.configure("other.proposals=files ~/*.md")
        self.assertEqual(production_counts("film"), [])

    def test_a_bad_line_names_its_number(self) -> None:
        for line, message in (("film.queue", "expected <production slug>.<label>"),
                              ("film.Queue=files ~/*.md", "expected <production slug>.<label>"),
                              ("film.queue=lines ~/ledger.md", "lines needs a file and a regex"),
                              ("film.queue=lines ~/ledger.md (", "bad regex"),
                              ("film.queue=files", "files needs a glob"),
                              ("film.queue=issues ~/issues", "issues needs a folder and a tag"),
                              ("film.queue=issues ~/issues two tags", "issues needs a folder and a tag"),
                              ("film.queue=words ~/x", "the count kind is lines, files or issues, not words")):
            with self.subTest(line=line):
                self.configure(line)
                with self.assertRaisesRegex(CountsFailure, f"dailies.conf line 3: {message}"):
                    _ = production_counts("film")


if __name__ == "__main__":
    _ = unittest.main()
