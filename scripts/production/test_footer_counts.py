#!/usr/bin/env python3
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import final, override
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import change_review
import footer_counts
from footer_counts import FooterCounts

LIST = """# Change review

## To review

| # | Change | How to check |
| --- | --- | --- |
| C1 | first | look |
| C2 | second | look |
"""

LEDGER = """# Plan backlog

- 2026-10-09 Open idea (a.md) — proposed (user via a unit)
- 2026-10-09 Waiting idea (b.md) — accepted 2026-10-12, build after the reviewers
- 2026-10-09 Handed out (c.md) — accepted 2026-10-12, assigned enh-showrunner-unit
- 2026-10-09 Done (d.md) — accepted 2026-10-09, applied (nixos b5c7d91)
- 2026-10-09 Shipped (e.md) — accepted 2026-10-10, built by message-unit
- 2026-10-11 Not taken (f.md) — declined for the production 2026-10-12
- 2026-10-11 Later (g.md) — deferred
"""


def issue(folder: Path, name: str, status: str, tags: str) -> None:
    _ = (folder / f"{name}.md").write_text(f"---\nstatus: {status}\ntags: [{tags}]\n---\n", encoding="utf-8")


@final
class FooterCountsTests(unittest.TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        # The doc's own line names the list; no live showrunner's list is looked up.
        _ = self.enterContext(mock.patch.object(change_review, "showrunner_of",
                                                side_effect=change_review.ChangeReviewError("not running")))

    def doc(self, *fields: str) -> Path:
        path = self.root / "production.md"
        _ = path.write_text("# Production\n\n" + "".join(f"{field}\n" for field in fields), encoding="utf-8")
        return path

    def test_each_count_is_read_from_the_source_its_doc_line_names(self) -> None:
        listing = self.root / "change-review.md"
        _ = listing.write_text(LIST, encoding="utf-8")
        folder = self.root / "issues"
        folder.mkdir()
        issue(folder, "wanted", "open", "proposal")
        issue(folder, "closed", "closed", "proposal")
        issue(folder, "bug", "open", "bug")
        ledger = self.root / "ledger.md"
        _ = ledger.write_text(LEDGER, encoding="utf-8")
        doc = self.doc(f"- **Change review:** `{listing}`", f"- **Proposals:** {folder}",
                       f"- **Improvement queue:** {ledger}")
        self.assertEqual(footer_counts.read_counts(doc, questions=1), FooterCounts(2, 1, 2, 1))

    def test_sources_name_each_file_a_count_reads_and_each_open_proposal(self) -> None:
        listing = self.root / "change-review.md"
        folder = self.root / "issues"
        folder.mkdir()
        issue(folder, "wanted", "open", "proposal")
        issue(folder, "closed", "closed", "proposal")
        ledger = self.root / "ledger.md"
        doc = self.doc(f"- **Change review:** `{listing}`", f"- **Proposals:** {folder}",
                       f"- **Improvement queue:** {ledger}")
        self.assertEqual(footer_counts.sources(doc), [doc, listing, folder, ledger, folder / "wanted.md"])
        self.assertEqual(footer_counts.sources(self.doc()), [self.root / "production.md"])

    def test_a_doc_without_the_lines_counts_nothing(self) -> None:
        self.assertEqual(footer_counts.read_counts(self.doc()), FooterCounts())
        missing = self.doc(f"- **Change review:** `{self.root / 'absent.md'}`",
                           f"- **Improvement queue:** {self.root / 'absent-ledger.md'}")
        self.assertEqual(footer_counts.read_counts(missing), FooterCounts())

    def test_the_line_names_each_count_once_and_leaves_out_zeros(self) -> None:
        self.assertEqual(footer_counts.counts_line(FooterCounts(2, 1, 3, 1)),
                         "For you: 2 changes to review, 1 proposal, 3 projects, 1 question")
        self.assertEqual(footer_counts.counts_line(FooterCounts(changes=1, questions=2)),
                         "For you: 1 change to review, 2 questions")
        self.assertIsNone(footer_counts.counts_line(FooterCounts()))

    def test_counts_add_across_productions(self) -> None:
        self.assertEqual(footer_counts.total([FooterCounts(1, 0, 2, 0), FooterCounts(0, 3, 1, 0)]),
                         FooterCounts(1, 3, 3, 0))
        self.assertEqual(footer_counts.total([]), FooterCounts())


if __name__ == "__main__":
    _ = unittest.main()
