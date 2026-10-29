#!/usr/bin/env python3
"""Tests for next_item.py: the one item /next shows from the four lists, and moving an item to the end."""
from __future__ import annotations

import contextlib
import io
import os
import re
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import TYPE_CHECKING, cast, final, override
from unittest import mock

import change_review
import next_item
from production_units import Production
import proposals_folder
import showrunners
from change_review import Visible
from next_item import NextError, Queue

# Importable once next_item has put scripts/lib and scripts/nightly_review on the path.
import urgency
from urgency import Entry, Urgency

if TYPE_CHECKING:
    from ..nightly_review import candidates
else:
    import candidates

PRODUCTION = Path.home() / "worktrees/demo-trunk/docs/plans/demo-production.md"
# The production an item in the tree test belongs to: each item's text holds `<slug>-item`.
ITEM = re.compile(r"\b(\w+)-item\b")
LEDGER = """\
- 2026-10-05 rust/nateroids: Build CI tests without debug info [gain/week: 2.5 h red CI] — deferred
- 2026-10-12 rust/hana: Run the GPU tests 8 at a time [gain/week: 38 min waiting] — proposed
"""


def at(day: int) -> datetime:
    return datetime(2026, 10, day, 9, 0)


def demo_slug(doc: Path) -> str:
    del doc
    return "demo"


def run(*arguments: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = next_item.main(list(arguments))
    return code, out.getvalue(), err.getvalue()


@final
class NextItemTests(unittest.TestCase):
    root: Path = Path()
    queues: dict[str, Queue] = {}

    @override
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        with contextlib.ExitStack() as stack:
            _ = cast(object, stack.enter_context(mock.patch.dict(
                os.environ, {"NEXT_STATE_DIR": str(self.root / "next"), "NIGHTLY_REVIEW_NOTE": str(self.root / "note.md")})))
            _ = cast(object, stack.enter_context(mock.patch.object(candidates, "ROOT", self.root)))
            self.addCleanup(stack.pop_all().close)
        self.queues = {
            "change": Queue("change-natedev", [Entry("C1", "C1 Old change", since=at(1)),
                                               Entry("C2", "C2 Later change", Urgency(urgency.LATER), since=at(2))]),
            "proposal": Queue("proposal-demo", [Entry("P1", "P1 A proposal", since=at(3), value=(9.0,))]),
            "efficiency": Queue("efficiency", []),
            "question": Queue("question-demo", [Entry("Q1", "demo: Which port?", Urgency(urgency.BLOCKS), since=at(4)),
                                                Entry("Q2", "demo: Newest question", since=at(5))]),
        }

    def read(self, kind: str, production: Path | None, below: bool = False) -> Queue:
        del production, below
        if kind not in self.queues:
            raise NextError(f"{kind}: not readable")
        return self.queues[kind]

    def patched(self) -> contextlib.AbstractContextManager[object]:
        stack = contextlib.ExitStack()
        _ = stack.enter_context(mock.patch.object(next_item, "read_queue", self.read))
        _ = stack.enter_context(mock.patch.object(next_item, "slug_of", demo_slug))
        return stack

    def test_bare_next_shows_the_most_urgent_item_across_the_lists_and_keeps_it(self) -> None:
        with self.patched():
            self.assertEqual(run("show", "--production", str(PRODUCTION)), (0, "question\ndemo: Which port?\n", ""))
            # A more urgent item arrives: the shown one stays until its answer.
            self.queues["change"].entries.append(Entry("C3", "C3 Marked", urgency.mark(at(6)), since=at(6)))
            self.assertEqual(run("show", "--production", str(PRODUCTION))[1], "question\ndemo: Which port?\n")
            # Answered, it leaves its list: the user's mark goes first.
            del self.queues["question"].entries[0]
            self.assertEqual(run("show", "--production", str(PRODUCTION))[1], "change\nC3 Marked\n")

    def test_oldest_and_newest_order_by_age_alone_or_within_a_kind(self) -> None:
        with self.patched():
            self.assertEqual(run("show", "--order", "oldest", "--production", str(PRODUCTION))[1], "change\nC1 Old change\n")
            self.assertEqual(run("show", "--order", "newest", "--production", str(PRODUCTION))[1],
                             "question\ndemo: Newest question\n")
            self.assertEqual(run("show", "--kind", "change", "--order", "newest")[1], "change\nC2 Later change\n")
            # The newest question stays current until its answer, also in the urgent order.
            self.assertEqual(run("show", "--kind", "question")[1], "question\ndemo: Newest question\n")
            del self.queues["question"].entries[1]
            self.assertEqual(run("show", "--kind", "question")[1], "question\ndemo: Which port?\n")

    def test_a_kind_shows_its_most_urgent_item_and_an_empty_list_says_so(self) -> None:
        with self.patched():
            self.assertEqual(run("show", "--kind", "change"), (0, "change\nC1 Old change\n", ""))
            self.assertEqual(run("show", "--kind", "efficiency"), (0, "no efficiency is waiting for you\n", ""))

    def test_a_list_that_cannot_be_read_is_left_out_with_one_line(self) -> None:
        del self.queues["question"]
        with self.patched():
            self.assertEqual(run("show", "--production", str(PRODUCTION)),
                             (0, "change\nC1 Old change\n", "next_item: question: not readable\n"))
            self.assertEqual(run("show", "--kind", "question"), (2, "", "next_item: question: not readable\n"))

    def test_move_to_end_takes_the_shown_item_and_shows_nothing_after_it(self) -> None:
        with self.patched():
            self.assertEqual(run("move-to-end", "--kind", "question"),
                             (2, "", "next_item: no question that /next showed is waiting; show one first\n"))
            _ = run("show", "--production", str(PRODUCTION))
            self.assertEqual(run("move-to-end", "--kind", "question"), (0, "question: moved Q1 to the end\n", ""))
            # Normal and oldest: the change from day 1 goes before the proposal from day 3.
            self.assertEqual(run("show", "--production", str(PRODUCTION))[1], "change\nC1 Old change\n")
            self.assertEqual(run("show", "--kind", "question")[1], "question\ndemo: Newest question\n")
            self.assertEqual(run("show", "--kind", "question", "--order", "oldest")[1], "question\ndemo: Newest question\n")

    def test_the_efficiency_and_change_lists_read_their_real_files(self) -> None:
        _ = (self.root / "ledger.md").write_text(LEDGER, encoding="utf-8")
        doc = self.root / "docs" / "plans" / "demo-production.md"
        doc.parent.mkdir(parents=True)
        _ = doc.write_text(f"# Demo\n\n## Production Context\n\n- **Efficiency ledger:** {self.root / 'ledger.md'}\n")
        line = LEDGER.splitlines()[0]
        self.assertEqual(run("show", "--kind", "efficiency", "--production", str(doc)), (0, f"efficiency\n{line}\n", ""))
        self.assertEqual(run("move-to-end", "--kind", "efficiency", "--production", str(doc))[0], 0)
        self.assertEqual(run("show", "--kind", "efficiency", "--production", str(doc))[1],
                         f"efficiency\n{LEDGER.splitlines()[1]}\n")
        # A production whose doc names no Efficiency ledger has no efficiency items.
        bare = doc.with_name("bare-production.md")
        _ = bare.write_text("# Bare\n\n## Production Context\n\n- **Repository:** /nowhere\n")
        self.assertEqual(run("show", "--kind", "efficiency", "--production", str(bare))[1],
                         "no efficiency is waiting for you\n")
        path = self.root / "change-review" / "change-review - natedev.md"
        _ = change_review.append_rows(path, [Visible("Panels keep shields", "Run `hana`", "Each panel has its shield")],
                                      date(2026, 10, 15), "", PRODUCTION)
        def the_list(production: str) -> Path:
            del production
            return path

        with mock.patch.object(change_review, "production_list", the_list):
            self.assertEqual(run("show", "--kind", "change", "--production", str(PRODUCTION)), (0, "\n".join([
                "change", "C1 Panels keep shields", "  How to check: Run `hana`. You see: Each panel has its shield",
                "  Merged: 2026-10-15", "  Production: demo", ""]), ""))

    def test_each_kind_shows_its_own_production_and_with_all_below_each_lower_one_never_above(self) -> None:
        plans = self.root / "docs" / "plans"
        plans.mkdir(parents=True)
        issues_folder = self.root / "issues"
        issues_folder.mkdir()
        outstanding = self.root / "showrunner" / "outstanding"
        outstanding.mkdir(parents=True)
        slugs = ("top", "kid", "grand", "side")
        docs = {slug: plans / f"{slug}-production.md" for slug in slugs}
        for slug, doc in docs.items():
            _ = (self.root / f"{slug}.md").write_text(f"- 2026-10-12 rust/{slug}: {slug}-item gain — proposed\n")
            _ = doc.write_text(f"# {slug}\n\n- **Efficiency ledger:** {self.root / f'{slug}.md'}\n"
                               + f"- **Proposals:** {issues_folder}\n")
            _ = (issues_folder / f"{slug}-item.md").write_text(
                f"---\nstatus: open\ntags: [issue, proposal]\nproposed_by: {slug}\ndate_created: 2026-10-01\n---\n")
            _ = (outstanding / f"{slug}.json").write_text(f'[{{"since": "2026-10-06T09:00", "text": "{slug}-item"}}]')
        path = self.root / "change-review" / "change-review - natedev.md"
        # The side production is a root of its own, with its own list.
        side_list = path.with_name("change-review - side.md")
        for slug in (*slugs, "gone"):
            _ = change_review.append_rows(side_list if slug == "side" else path, [Visible(f"{slug}-item change", "Run it", "It works")], date(2026, 10, 15),
                                          "", plans / f"{slug}-production.md")
        # A row written before the Production column belongs to the list's root.
        _ = path.write_text(path.read_text(encoding="utf-8").replace(" gone |", "  |"), encoding="utf-8")
        tree = {"top": ["top", "kid", "grand"], "kid": ["kid", "grand"], "grand": ["grand"], "side": ["side"]}

        def owner_docs(doc: Path) -> list[Path]:
            return [docs[slug] for slug in tree[proposals_folder.slug_of(doc)]]

        def the_list(production: str) -> Path:
            # Only the root's doc has a Change review line; its children read its list.
            if proposals_folder.slug_of(Path(production)) in ("kid", "grand"):
                raise change_review.ChangeReviewError("no line")
            return side_list if proposals_folder.slug_of(Path(production)) == "side" else path

        def production(doc: Path) -> Production:
            return Production(doc, proposals_folder.slug_of(doc), "", self.root, self.root / "log.md", ZoneInfo("UTC"))

        def shown(kind: str, slug: str, below: bool = False) -> list[str]:
            queue = next_item.read_queue(kind, docs[slug], below)
            return sorted(found[1] for entry in queue.entries if (found := ITEM.search(entry.item)))

        def root_of(slug: str) -> str:
            return "top" if slug in tree["top"] else slug

        def descendants_of(slug: str) -> list[str]:
            return tree[slug][1:]

        with mock.patch.object(proposals_folder, "owner_docs", owner_docs), \
                mock.patch.object(showrunners, "root_of", root_of), \
                mock.patch.object(showrunners, "descendants_of", descendants_of), \
                mock.patch.object(showrunners, "timer_docs", return_value=docs), \
                mock.patch.object(change_review, "production_list", the_list), \
                mock.patch.object(next_item, "read_production", production), \
                mock.patch.dict(os.environ, {"SHOWRUNNER_STATE_DIR": str(self.root / "showrunner")}):
            for kind in next_item.KINDS:
                # Each showrunner shows its own production: never the one above, and not the ones below.
                self.assertEqual(shown(kind, "top"), ["gone", "top"] if kind == "change" else ["top"], kind)
                self.assertEqual(shown(kind, "kid"), ["kid"], kind)
                self.assertEqual(shown(kind, "grand"), ["grand"], kind)
                self.assertEqual(shown(kind, "side"), ["side"], kind)
                # all-below adds every production below, at any depth, and keeps its own pin.
                self.assertEqual(shown(kind, "top", below=True),
                                 sorted(["grand", "kid", "top", *(["gone"] if kind == "change" else [])]), kind)
                self.assertEqual(shown(kind, "kid", below=True), ["grand", "kid"], kind)
                self.assertEqual(shown(kind, "grand", below=True), ["grand"], kind)
                self.assertNotEqual(next_item.read_queue(kind, docs["kid"]).name,
                                    next_item.read_queue(kind, docs["kid"], True).name, kind)
            # Each showrunner pins its own change row, so one /next never moves the other's.
            self.assertNotEqual(next_item.change_queue(docs["top"]).name, next_item.change_queue(docs["kid"]).name)

    def test_a_child_with_no_change_review_line_reads_its_roots_list(self) -> None:
        path = self.root / "change-review - top.md"
        _ = change_review.append_rows(path, [Visible("kid change", "Run it", "It works")], date(2026, 10, 15), "",
                                      self.root / "kid-production.md")
        top, kid = self.root / "top-production.md", self.root / "kid-production.md"

        def the_list(production: str) -> Path:
            if Path(production) == kid:
                raise change_review.ChangeReviewError(f"{kid} has no line")
            return path

        with mock.patch.object(change_review, "production_list", the_list), \
                mock.patch.object(showrunners, "root_of", return_value="top"), \
                mock.patch.object(showrunners, "timer_docs", return_value={"top": top, "kid": kid}), \
                mock.patch.object(proposals_folder, "owner_docs", return_value=[kid]):
            self.assertEqual([entry.item.splitlines()[0] for entry in next_item.change_queue(kid).entries],
                             ["C1 kid change"])


if __name__ == "__main__":
    _ = unittest.main()
