#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from typing import final, override
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import change_review
import footer_counts
import proposal_issue
import proposals_folder
import showrunners
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

EFFICIENCY_LEDGER = """# Efficiency proposals

- 2026-10-19 config: Open idea — proposed; from config review
- 2026-10-19 messages: Later idea — deferred until next week
- 2026-10-19 rust: Shipped idea — accepted, applied
- 2026-10-19 config: Rejected idea — declined
- 2026-10-19 messages: Nothing found — no proposal
- 2026-10-19 config: Shipped group — accepted, applied; from Also found: X — proposed
- config: Undated idea — proposed
"""


def issue(folder: Path, name: str, status: str, tags: str, proposed_by: str = "production", *fields: str) -> None:
    """An issue with any extra frontmatter lines; its default proposer is the test doc's own production, `production.md`."""
    extra = "".join(f"{field}\n" for field in fields)
    _ = (folder / f"{name}.md").write_text(f"---\nstatus: {status}\ntags: [{tags}]\nproposed_by: {proposed_by}\n{extra}---\n",
                                           encoding="utf-8")




def production_doc(folder: Path, slug: str, proposals: Path, *units: str) -> Path:
    """A production doc that names the shared proposals folder and its units."""
    rows = "".join(f"| {unit} | `docs/plans/{unit}.md` |\n" for unit in units)
    path = folder / f"{slug}-production.md"
    _ = path.write_text(f"# {slug}\n\n- **Proposals:** {proposals}\n\n## Units\n\n| Unit | Plan |\n| --- | --- |\n{rows}",
                        encoding="utf-8")
    return path


@final
class FooterCountsTests(unittest.TestCase):
    root: Path = Path()
    tree: dict[str, Path] = {}  # each setUp replaces it
    below: dict[str, list[str]] = {}  # each setUp replaces it

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = self.enterContext(mock.patch.object(Path, "home", return_value=self.root))
        # The doc's own line names the list; no live showrunner's list is looked up.
        _ = self.enterContext(mock.patch.object(change_review, "showrunner_of",
                                                side_effect=change_review.ChangeReviewError("not running")))
        # No live update timer names a production below this one, unless a test says so.
        self.tree = {}
        self.below = {}
        _ = self.enterContext(mock.patch.object(showrunners, "timer_docs", side_effect=lambda: self.tree))
        _ = self.enterContext(mock.patch.object(showrunners, "descendants_of",
                                                side_effect=self.productions_below))

    def productions_below(self, slug: str) -> list[str]:
        return self.below.get(slug, [])

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
        efficiency = self.root / "efficiency.md"
        _ = efficiency.write_text(EFFICIENCY_LEDGER, encoding="utf-8")
        doc = self.doc(f"- **Change review:** `{listing}`", f"- **Proposals:** {folder}",
                       f"- **Improvement queue:** {ledger}", f"- **Efficiency ledger:** `{efficiency}`")
        self.assertEqual(footer_counts.read_counts(doc, questions=1), FooterCounts(2, 1, 2, 1, 2))

    def test_efficiency_counts_only_dated_proposed_and_deferred_first_statuses(self) -> None:
        ledger = self.root / "efficiency.md"
        _ = ledger.write_text(EFFICIENCY_LEDGER, encoding="utf-8")
        doc = self.doc(f"- **Efficiency ledger:** {ledger}")
        self.assertEqual(footer_counts.efficiency(doc), 2)

    def test_efficiency_without_a_readable_ledger_counts_nothing(self) -> None:
        for field in ("", f"- **Efficiency ledger:** {self.root / 'absent.md'}",
                      f"- **Efficiency ledger:** {self.root}"):
            with self.subTest(field=field):
                self.assertEqual(footer_counts.efficiency(self.doc(field)), 0)

    def test_a_ledger_that_is_not_utf8_counts_nothing(self) -> None:
        ledger = self.root / "binary.md"
        _ = ledger.write_bytes(b"- 2026-10-19 config: x \xe2\x80\x94 proposed\n\xff\n")
        doc = self.doc(f"- **Efficiency ledger:** {ledger}")
        self.assertEqual(footer_counts.efficiency(doc), 0)
        self.assertIn(ledger, footer_counts.sources(doc))

    def test_sources_include_the_efficiency_ledger_even_before_it_exists(self) -> None:
        ledger = self.root / "efficiency.md"
        doc = self.doc(f"- **Efficiency ledger:** `{ledger}`")
        self.assertIn(ledger, footer_counts.sources(doc))

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

    def test_a_shared_folder_counts_only_the_proposals_of_the_production_and_those_below_it(self) -> None:
        folder = self.root / "issues"
        folder.mkdir()
        for name, proposer in (("own", "trunk-unit"), ("by the production", "tool-based-ui"),
                               ("by a child", "cargo-liner-style"), ("by a unit below", "types-unit"),
                               ("named with its production", "types-unit (cargo-liner-style)"),
                               ("quoted link", "'[[trunk-unit]]'"), ("other production", "followups-unit"),
                               ("other unit in a known production", "trunk-unit (build-followups)"),
                               ("no proposer", "")):
            issue(folder, name, "open", "issue, proposal", proposer)
        issue(folder, "closed own", "closed", "issue, proposal", "trunk-unit")
        top = production_doc(self.root, "tool-based-ui", folder, "trunk-unit")
        child = production_doc(self.root, "cargo-liner-style", folder, "types-unit")
        other = production_doc(self.root, "build-followups", folder, "followups-unit")
        self.tree = {"tool-based-ui": top, "cargo-liner-style": child, "build-followups": other}
        self.below = {"tool-based-ui": ["cargo-liner-style"]}
        own = ["by a child", "by a unit below", "by the production", "named with its production", "own", "quoted link"]
        self.assertEqual(sorted(found.path.stem for found in proposals_folder.open_proposals(top)), own)
        self.assertEqual(footer_counts.proposals(top), 6)
        self.assertEqual(footer_counts.proposals(child), 3)
        self.assertEqual(footer_counts.proposals(other), 2)
        # A unit added below changes the count, so the child's doc is a source.
        self.assertIn(child, footer_counts.sources(top))
        self.assertNotIn(folder / "other production.md", footer_counts.sources(top))

    def test_the_proposal_count_leaves_out_approved_and_active_proposals(self) -> None:
        folder = self.root / "issues"
        folder.mkdir()
        approval = ('unit: "names-unit"', 'plan: "docs/plans/names.md"')
        issue(folder, "waiting", "open", "issue, proposal", "production", "stage: backlog")
        issue(folder, "approved at backlog", "open", "issue, proposal", "production", "stage: backlog", *approval)
        issue(folder, "approved at active", "open", "issue, proposal", "production", "stage: active", *approval)
        issue(folder, "started without an approval", "open", "issue, proposal", "production", "stage: active")
        doc = self.doc(f"- **Proposals:** {folder}")
        self.assertEqual(len(proposals_folder.open_proposals(doc)), 4)
        self.assertEqual(footer_counts.proposals(doc), 1)
        self.assertEqual(footer_counts.counts_line(footer_counts.read_counts(doc)), "For you: 1 proposal")

    def test_an_approved_plain_proposal_leaves_the_footer_count(self) -> None:
        folder = self.root / "issues"
        folder.mkdir()
        issue(folder, "plain", "open", "issue, proposal", "production", "stage: backlog")
        doc = self.doc(f"- **Proposals:** {folder}")
        self.assertEqual(footer_counts.proposals(doc), 1)
        with contextlib.redirect_stdout(io.StringIO()):
            code = proposal_issue.main(["approve", str(folder / "plain.md"), "--showrunner", "s", "--unit", "u", "--plan", "p"])
        self.assertEqual(code, 0)
        self.assertEqual(footer_counts.proposals(doc), 0)

    def test_a_doc_without_the_lines_counts_nothing(self) -> None:
        self.assertEqual(footer_counts.read_counts(self.doc()), FooterCounts())
        missing = self.doc(f"- **Change review:** `{self.root / 'absent.md'}`",
                           f"- **Improvement queue:** {self.root / 'absent-ledger.md'}")
        self.assertEqual(footer_counts.read_counts(missing), FooterCounts())

    def test_the_line_names_each_count_once_and_leaves_out_zeros(self) -> None:
        self.assertEqual(footer_counts.counts_line(FooterCounts(2, 1, 3, 1)),
                         "For you: 1 question, 2 changes, 1 proposal, 3 projects")
        self.assertEqual(footer_counts.counts_line(FooterCounts(changes=1, questions=2)),
                         "For you: 2 questions, 1 change")
        self.assertIsNone(footer_counts.counts_line(FooterCounts()))

    def test_the_line_orders_questions_changes_efficiency_proposals_then_projects(self) -> None:
        self.assertEqual(footer_counts.counts_line(FooterCounts(changes=2, proposals=4, projects=3, questions=5,
                                                                efficiency=130)),
                         "For you: 5 questions, 2 changes, 130 efficiency, 4 proposals, 3 projects")

    def test_each_count_of_one_takes_the_singular_and_efficiency_has_no_plural(self) -> None:
        self.assertEqual(footer_counts.counts_line(FooterCounts(1, 1, 1, 1, 1)),
                         "For you: 1 question, 1 change, 1 efficiency, 1 proposal, 1 project")
        self.assertEqual(footer_counts.counts_line(FooterCounts(efficiency=2)), "For you: 2 efficiency")

    def test_counts_add_across_productions(self) -> None:
        self.assertEqual(footer_counts.total([FooterCounts(1, 0, 2, 0, 4), FooterCounts(0, 3, 1, 0, 7)]),
                         FooterCounts(1, 3, 3, 0, 11))
        self.assertEqual(footer_counts.total([]), FooterCounts())


if __name__ == "__main__":
    _ = unittest.main()
