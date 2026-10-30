#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast, final, override
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import change_review
import footer_counts
import next_item
import proposal_issue
import proposals_folder
import showrunners
import showrunner_tree
from scripts.production import staff
from footer_counts import FooterCounts

LIST = """# Change review

## To review

| # | Change | How to check |
| --- | --- | --- |
| C1 | first | look |
| C2 | second | look |
"""

LEDGER = """# Plan backlog

- 2026-10-09 Open idea (a.md) — proposed
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
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"NEXT_STATE_DIR": str(self.root / "next")})))
        # The doc's own line names the list; no live showrunner's list is looked up.
        _ = self.enterContext(mock.patch.object(change_review, "showrunner_of",
                                                side_effect=change_review.ChangeReviewError("not running")))
        # No live update timer names a production below this one, unless a test says so.
        self.tree = {}
        self.below = {}
        _ = self.enterContext(mock.patch.object(showrunners, "timer_docs", side_effect=lambda: self.tree))
        _ = self.enterContext(mock.patch.object(showrunner_tree, "descendants_of",
                                                side_effect=self.productions_below))
        # Each production below a root reports to that root.
        _ = self.enterContext(mock.patch.object(showrunner_tree, "edges", side_effect=self.parents))

    def parents(self, slugs: set[str] | None = None, *,
                record: staff.StaffSnapshot | None = None) -> dict[str, str]:
        del slugs, record
        return {child: parent for parent, children in self.below.items() for child in children}

    def productions_below(self, slug: str, *,
                          record: staff.StaffSnapshot | None = None) -> list[str]:
        del record
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
        self.assertEqual(footer_counts.read_counts(doc, questions=1),
                         FooterCounts(changes=2, proposals=2, questions=1, efficiency=2))

    def test_proposed_backlog_line_counts_as_a_proposal_and_next_shows_it(self) -> None:
        ledger = self.root / "ledger.md"
        _ = ledger.write_text("# Plan backlog\n", encoding="utf-8")
        doc = self.doc(f"- **Improvement queue:** {ledger}")
        before = footer_counts.read_counts(doc)
        _ = ledger.write_text("- 2026-10-28 Faster tests (tests.md) — proposed\n", encoding="utf-8")
        counts = footer_counts.read_counts(doc)
        self.assertEqual(counts, before._replace(proposals=before.proposals + 1))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = next_item.main(["show", "--kind", "proposal", "--production", str(doc)])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), f"proposal\nFaster tests\n{ledger}\n")
        row = footer_counts.counts_line(counts)
        self.assertEqual(row, "For you: 1 proposal")
        self.assertNotIn("project", row or "")
        self.assertIn(ledger, footer_counts.sources(doc))

    def test_accepted_unsettled_backlog_line_changes_no_count_or_next_proposal(self) -> None:
        ledger = self.root / "ledger.md"
        _ = ledger.write_text("- 2026-10-28 Faster tests (tests.md) — proposed\n", encoding="utf-8")
        doc = self.doc(f"- **Improvement queue:** {ledger}")
        before = footer_counts.read_counts(doc)
        queue = next_item.read_queue("proposal", doc)
        _ = ledger.write_text(ledger.read_text(encoding="utf-8")
                              + "- 2026-10-28 New checks (checks.md) — accepted, build later\n",
                              encoding="utf-8")
        self.assertEqual(footer_counts.read_counts(doc), before)
        self.assertEqual(next_item.read_queue("proposal", doc), queue)

    def test_backlog_and_issue_count_matches_each_productions_own_next_proposals(self) -> None:
        folder = self.root / "issues"
        folder.mkdir()
        top = production_doc(self.root, "top", folder, "top-unit")
        child = production_doc(self.root, "child", folder, "child-unit")
        self.tree = {"top": top, "child": child}
        self.below = {"top": ["child"]}
        issue(folder, "own issue", "open", "proposal", "top-unit")
        issue(folder, "child issue", "open", "proposal", "child-unit")
        for doc, titles in ((top, ["Own plan"]), (child, ["Child plan", "Second child plan"])):
            ledger = doc.with_suffix(".ledger.md")
            _ = ledger.write_text("".join(f"- 2026-10-28 {title} — proposed\n" for title in titles),
                                  encoding="utf-8")
            _ = doc.write_text(doc.read_text(encoding="utf-8") + f"\n- **Improvement queue:** {ledger}\n",
                               encoding="utf-8")
            queue = next_item.read_queue("proposal", doc)
            self.assertEqual(footer_counts.proposals(doc), 1 + len(titles))
            self.assertEqual(footer_counts.proposals(doc), len(queue.entries))
            for title in titles:
                self.assertIn(f"{title}\n{ledger}", [entry.item for entry in queue.entries])

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

    def test_a_shared_folder_counts_only_the_productions_own_proposals(self) -> None:
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
        # The child's showrunner counts the child's proposals; the root never does (user, 2026-10-27).
        self.assertEqual(sorted(found.path.stem for found in proposals_folder.open_proposals(top)),
                         ["by the production", "own", "quoted link"])
        self.assertEqual(footer_counts.proposals(top), 3)
        self.assertEqual(footer_counts.proposals(child), 3)
        self.assertEqual(footer_counts.proposals(other), 2)
        self.assertNotIn(child, footer_counts.sources(top))
        self.assertNotIn(folder / "by a child.md", footer_counts.sources(top))

    def test_each_production_counts_its_own_rows_of_the_roots_change_list(self) -> None:
        listing = self.root / "change-review.md"
        _ = listing.write_text(LIST.replace("| How to check |", "| How to check | Production |")
                               .replace("| --- | --- | --- |", "| --- | --- | --- | --- |")
                               .replace("| look |\n| C2", "| look | top |\n| C2").replace("| second | look |", "| second | look | kid |")
                               + "| C3 | third | look | grandkid |\n| C4 | older | look | |\n", encoding="utf-8")
        top = self.root / "top-production.md"
        _ = top.write_text(f"# top\n\n- **Change review:** `{listing}`\n", encoding="utf-8")
        kid, grandkid = self.root / "kid-production.md", self.root / "grandkid-production.md"
        for doc in (kid, grandkid):
            _ = doc.write_text(f"# {doc.stem}\n", encoding="utf-8")
        self.tree = {"top": top, "kid": kid, "grandkid": grandkid}
        self.below = {"top": ["kid", "grandkid"]}
        # A row without a Production cell is the root's; a child with no list line reads the root's list.
        self.assertEqual([footer_counts.changes(doc) for doc in (top, kid, grandkid)], [2, 1, 1])
        self.assertIn(listing, footer_counts.sources(kid))

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
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(proposal_issue, "calling_showrunner", return_value="s"):
            code = proposal_issue.main(["approve", str(folder / "plain.md"), "--by", "user", "--showrunner", "s", "--unit", "u", "--plan", "p"])
        self.assertEqual(code, 0)
        self.assertEqual(footer_counts.proposals(doc), 0)

    def test_a_doc_without_the_lines_counts_nothing(self) -> None:
        self.assertEqual(footer_counts.read_counts(self.doc()), FooterCounts())
        missing = self.doc(f"- **Change review:** `{self.root / 'absent.md'}`",
                           f"- **Improvement queue:** {self.root / 'absent-ledger.md'}")
        self.assertEqual(footer_counts.read_counts(missing), FooterCounts())

    def test_the_line_names_each_count_once_and_leaves_out_zeros(self) -> None:
        self.assertEqual(footer_counts.counts_line(FooterCounts(changes=2, proposals=1, questions=1)),
                         "For you: 1 question, 2 changes, 1 proposal")
        self.assertEqual(footer_counts.counts_line(FooterCounts(changes=1, questions=2)),
                         "For you: 2 questions, 1 change")
        self.assertIsNone(footer_counts.counts_line(FooterCounts()))

    def test_the_line_orders_questions_changes_efficiency_then_proposals(self) -> None:
        self.assertEqual(footer_counts.counts_line(FooterCounts(changes=2, proposals=4, questions=5,
                                                                efficiency=130)),
                         "For you: 5 questions, 2 changes, 130 efficiency, 4 proposals")

    def test_each_count_of_one_takes_the_singular_and_efficiency_has_no_plural(self) -> None:
        self.assertEqual(footer_counts.counts_line(FooterCounts(1, 1, 1, 1)),
                         "For you: 1 question, 1 change, 1 efficiency, 1 proposal")
        self.assertEqual(footer_counts.counts_line(FooterCounts(efficiency=2)), "For you: 2 efficiency")

    def test_counts_add_across_productions(self) -> None:
        self.assertEqual(footer_counts.total([FooterCounts(1, 0, 0, 4), FooterCounts(0, 3, 0, 7)]),
                         FooterCounts(1, 3, 0, 11))
        self.assertEqual(footer_counts.total([]), FooterCounts())


if __name__ == "__main__":
    _ = unittest.main()
