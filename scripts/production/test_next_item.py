#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Tests for next_item.py: the one item /next shows from the four lists, and moving an item to the end."""
from __future__ import annotations

from scripts.agents import remote

import contextlib
import io
import os
import re
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import cast, final, override
from unittest import mock




from scripts.production import change_review
from scripts.production import footer_counts
from scripts.production import next_item
from scripts.production.production_doc import Production
from scripts.production import proposals_folder
from scripts.production import showrunners
from scripts.production import showrunner_tree
from scripts.production import staff, unit_lookup
from scripts.production.staff_test_support import (
    fixture_member, isolated_source_readers, isolated_staff_file, source_environment, unit_membership,
)
from scripts.production.test_change_review import producer_rows
from scripts.production.change_review import Visible
from scripts.production.next_item import NextError, Queue

from scripts.lib import urgency
from scripts.lib.urgency import Entry, Urgency

from scripts.nightly_review import candidates

PRODUCTION = Path.home() / "worktrees/demo-trunk/docs/plans/demo-production.md"
# The production an item in the tree test belongs to: each item's text holds `<name>-item`.
ITEM = re.compile(r"\b(\w+)-item\b")
LEDGER = """\
- 2026-10-05 rust/nateroids: Build CI tests without debug info [gain/week: 2.5 h red CI] — deferred
- 2026-10-12 rust/hana: Run the GPU tests 8 at a time [gain/week: 38 min waiting] — proposed
"""


def at(day: int) -> datetime:
    return datetime(2026, 10, day, 9, 0)


def run(*arguments: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = next_item.main(list(arguments))
    return code, out.getvalue(), err.getvalue()


def review_show(doc: Path) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = change_review.main(["show", "--production", unit_lookup.doc_owner(doc, staff.snapshot()).name])
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
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {**source_environment(self.root / "sources"),
                "HOME": str(self.root), "HERDR_BIN": "/nonexistent/herdr"})))
        _ = self.enterContext(isolated_source_readers())
        _ = fixture_member("showrunner", name="demo", doc=staff.LiveDoc(remote.local_machine(), PRODUCTION))
        with contextlib.ExitStack() as stack:
            _ = cast(object, stack.enter_context(mock.patch.dict(
                os.environ, {"NEXT_STATE_DIR": str(self.root / "next"), "NIGHTLY_REVIEW_NOTE": str(self.root / "note.md")})))
            _ = cast(object, stack.enter_context(mock.patch.object(candidates, "state_root", return_value=self.root)))
            _ = stack.enter_context(mock.patch.object(change_review, "lists_directory", return_value=self.root / "change-review"))
            self.addCleanup(stack.pop_all().close)
        self.queues = {
            "change": Queue("change-natedev", [Entry("C1", "C1 Old change", since=at(1)),
                                               Entry("C2", "C2 Later change", Urgency(urgency.LATER), since=at(2))]),
            "proposal": Queue("proposal-demo", [Entry("P1", "P1 A proposal", since=at(3), value=(9.0,))]),
            "efficiency": Queue("efficiency", []),
            "question": Queue("question-demo", [Entry("Q1", "demo: Which port?", Urgency(urgency.BLOCKS), since=at(4)),
                                                Entry("Q2", "demo: Newest question", since=at(5))]),
        }

    def read(self, kind: str, production: Path | None, below: bool = False,
             *, record: staff.StaffSnapshot | None = None) -> Queue:
        del production, below, record
        if kind not in self.queues:
            raise NextError(f"{kind}: not readable")
        return self.queues[kind]

    def patched(self) -> contextlib.AbstractContextManager[object]:
        stack = contextlib.ExitStack()
        _ = stack.enter_context(mock.patch.object(next_item, "read_queue", self.read))
        return stack








    def test_bare_next_shows_the_most_urgent_item_across_the_lists_and_keeps_it(self) -> None:
        with self.patched():
            self.assertEqual(run("show", "--production", unit_lookup.doc_owner(PRODUCTION, staff.snapshot()).name), (0, "question\ndemo: Which port?\n", ""))
            # A more urgent item arrives: the shown one stays until its answer.
            self.queues["change"].entries.append(Entry("C3", "C3 Marked", urgency.mark(at(6)), since=at(6)))
            self.assertEqual(run("show", "--production", unit_lookup.doc_owner(PRODUCTION, staff.snapshot()).name)[1], "question\ndemo: Which port?\n")
            # Answered, it leaves its list: the user's mark goes first.
            del self.queues["question"].entries[0]
            self.assertEqual(run("show", "--production", unit_lookup.doc_owner(PRODUCTION, staff.snapshot()).name)[1], "change\nC3 Marked\n")

    def test_oldest_and_newest_order_by_age_alone_or_within_a_kind(self) -> None:
        with self.patched():
            self.assertEqual(run("show", "--order", "oldest", "--production", unit_lookup.doc_owner(PRODUCTION, staff.snapshot()).name)[1], "change\nC1 Old change\n")
            self.assertEqual(run("show", "--order", "newest", "--production", unit_lookup.doc_owner(PRODUCTION, staff.snapshot()).name)[1],
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
            self.assertEqual(run("show", "--production", unit_lookup.doc_owner(PRODUCTION, staff.snapshot()).name),
                             (0, "change\nC1 Old change\n", "next_item: question: not readable\n"))
            self.assertEqual(run("show", "--kind", "question"), (2, "", "next_item: question: not readable\n"))

    def test_move_to_end_takes_the_shown_item_and_shows_nothing_after_it(self) -> None:
        with self.patched():
            self.assertEqual(run("move-to-end", "--kind", "question"),
                             (2, "", "next_item: no question that /next showed is waiting; show one first\n"))
            _ = run("show", "--production", unit_lookup.doc_owner(PRODUCTION, staff.snapshot()).name)
            self.assertEqual(run("move-to-end", "--kind", "question"), (0, "question: moved Q1 to the end\n", ""))
            # Normal and oldest: the change from day 1 goes before the proposal from day 3.
            self.assertEqual(run("show", "--production", unit_lookup.doc_owner(PRODUCTION, staff.snapshot()).name)[1], "change\nC1 Old change\n")
            self.assertEqual(run("show", "--kind", "question")[1], "question\ndemo: Newest question\n")
            self.assertEqual(run("show", "--kind", "question", "--order", "oldest")[1], "question\ndemo: Newest question\n")

    def test_move_to_end_uses_the_given_key_instead_of_the_pinned_item(self) -> None:
        queue = self.queues["change"]
        urgency.pin(queue.name, "C2")
        with self.patched():
            self.assertEqual(next_item.move_to_end("change", PRODUCTION, key="C1"),
                             "change: moved C1 to the end")
            self.assertEqual([entry.key for entry in urgency.queue_order(queue.entries, queue.name)], ["C2", "C1"])
            self.assertEqual(set(urgency.ended(queue.name)), {"C1"})
            self.assertEqual(urgency.pinned(queue.name), "C2")

    def test_move_to_end_refuses_a_key_outside_the_queue_without_moving_the_pin(self) -> None:
        queue = self.queues["change"]
        urgency.pin(queue.name, "C1")
        with self.patched(), self.assertRaisesRegex(NextError, "C999"):
            _ = next_item.move_to_end("change", PRODUCTION, key="C999")
        self.assertEqual(urgency.pinned(queue.name), "C1")
        self.assertEqual(urgency.ended(queue.name), {})

    def test_prepared_changes_keep_the_queue_order_and_move_without_reading_the_lists_again(self) -> None:
        prepared = change_review.ChangeQueue("change-demo", change_review.row_entries([
            {"#": "C1", "Change": "First", "Merged": "2026-10-01"},
            {"#": "C2", "Change": "Second", "Merged": "2026-10-02"},
        ]))
        with mock.patch.object(change_review, "change_queue", side_effect=AssertionError("list read")), \
                mock.patch.object(next_item, "read_queue", side_effect=AssertionError("queue read")):
            queue = next_item.change_queue(PRODUCTION, prepared=prepared)
            self.assertEqual([entry.item for entry in queue.entries], [
                "C1 First\n  How to check: \n  Merged: 2026-10-01", "C2 Second\n  How to check: \n  Merged: 2026-10-02"])
            self.assertEqual(next_item.move_to_end("change", PRODUCTION, key="C1", prepared=prepared),
                             "change: moved C1 to the end")
        self.assertEqual([entry.key for entry in urgency.queue_order(queue.entries, queue.name)], ["C2", "C1"])

    def test_the_efficiency_and_change_lists_read_their_real_files(self) -> None:
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        _ = unit_membership("demo", "fixture")
        _ = (self.root / "ledger.md").write_text(LEDGER, encoding="utf-8")
        doc = self.root / "docs" / "plans" / "demo-production.md"
        doc.parent.mkdir(parents=True)
        owner = staff.snapshot().by_path("demo")
        assert owner is not None
        _ = staff.set_doc(owner.id, staff.LiveDoc(remote.local_machine(), doc))
        _ = doc.write_text(f"# Demo\n\n## Production Context\n\n- **Efficiency ledger:** {self.root / 'ledger.md'}\n")
        line = LEDGER.splitlines()[0]
        self.assertEqual(run("show", "--kind", "efficiency", "--production", unit_lookup.doc_owner(doc, staff.snapshot()).name), (0, f"efficiency\n{line}\n", ""))
        self.assertEqual(run("move-to-end", "--kind", "efficiency", "--production", unit_lookup.doc_owner(doc, staff.snapshot()).name)[0], 0)
        self.assertEqual(run("show", "--kind", "efficiency", "--production", unit_lookup.doc_owner(doc, staff.snapshot()).name)[1],
                         f"efficiency\n{LEDGER.splitlines()[1]}\n")
        # A production whose doc names no Efficiency ledger has no efficiency items.
        bare = doc.with_name("bare-production.md")
        _ = fixture_member("showrunner", name="bare", doc=staff.LiveDoc(remote.local_machine(), bare))
        _ = bare.write_text("# Bare\n\n## Production Context\n\n- **Repository:** /nowhere\n")
        self.assertEqual(run("show", "--kind", "efficiency", "--production", unit_lookup.doc_owner(bare, staff.snapshot()).name)[1],
                         "no efficiency is waiting for you\n")
        path = self.root / "change-review" / "change-review - natedev.md"
        _ = change_review.append_rows(path, [Visible("Panels keep shields", "Run `hana`", "Each panel has its shield")],
                                      date(2026, 10, 15), "", doc, record=staff.snapshot())
        _ = doc.write_text(doc.read_text() + f"- **Change review:** `{path.with_name('change-review - demo.md')}`\n")
        self.assertEqual(run("show", "--kind", "change", "--production", "demo"), (0, "\n".join([
            "change", "natedev/C1 Panels keep shields", "  How to check: Run `hana`. You see: Each panel has its shield",
            "  Merged: 2026-10-15", "  Production: demo", ""]), ""))

    def test_each_kind_shows_its_own_production_and_with_all_below_each_lower_one_never_above(self) -> None:
        plans = self.root / "docs" / "plans"
        plans.mkdir(parents=True)
        issues_folder = self.root / "issues"
        issues_folder.mkdir()
        outstanding = self.root / "showrunner" / "outstanding"
        outstanding.mkdir(parents=True)
        names = ("top", "kid", "grand", "side")
        docs = {name: plans / f"{name}-production.md" for name in names}
        for name, doc in docs.items():
            _ = (self.root / f"{name}.md").write_text(f"- 2026-10-12 rust/{name}: {name}-item gain — proposed\n")
            _ = doc.write_text(f"# {name}\n\n- **Efficiency ledger:** {self.root / f'{name}.md'}\n"
                               + f"- **Proposals:** {issues_folder}\n")
            owner = fixture_member("showrunner", name=name, doc=staff.LiveDoc(remote.local_machine(), doc))
            _ = (issues_folder / f"{name}-item.md").write_text(
                f"---\nstatus: open\ntags: [issue, proposal]\nproposed_by: {owner.id}\ndate_created: 2026-10-01\n---\n")
        path = self.root / "change-review" / "change-review - natedev.md"
        # The side production is a root of its own, with its own list.
        side_list = path.with_name("change-review - side.md")
        for name in (*names, "gone"):
            _ = change_review.append_rows(side_list if name == "side" else path, [Visible(f"{name}-item change", "Run it", "It works")], date(2026, 10, 15),
                                          "", docs.get(name, docs["top"]), record=staff.snapshot())
        # A row without a production belongs to the list's root.
        root_id = unit_lookup.doc_owner(docs["top"], staff.snapshot()).id
        _ = path.write_text("\n".join(line.replace(f" {root_id} |", "  |") if line.startswith("| C5 |") else line
                                      for line in path.read_text().splitlines()) + "\n")
        producer_rows(self.root, docs, {path: "top", side_list: "side"})
        for name, doc in docs.items():
            parent_name = {"kid": "top", "grand": "kid"}.get(name)
            parent = next((row.id for row in staff.find(kind="showrunner") if row.name == parent_name), None)
            existing = staff.snapshot().by_path(name)
            if existing is not None:
                _ = staff.move(existing.id, staff.ReportsTo(parent) if parent is not None else staff.StaffRoot())
            owner = fixture_member("showrunner", name=name, parent=parent, doc=staff.LiveDoc(remote.local_machine(), doc))
            _ = (outstanding / f"{owner.id}.json").write_text(
                f'[{{"since": "2026-10-06T09:00", "text": "{name}-item"}}]')
            timer = Path(os.environ["NOTIFIER_STATE_DIR"]) / f"showrunner-{name}"
            timer.mkdir(parents=True)
            _ = (timer / "conf").write_text(f"TARGET=session:{owner.id}\nCHECK=zsh /x/production_check.sh {owner.id}\n")
        tree = {"top": ["top", "kid", "grand"], "kid": ["kid", "grand"], "grand": ["grand"], "side": ["side"]}

        def owner_docs(doc: Path, *, record: staff.StaffSnapshot | None = None) -> list[Path]:
            return [docs[name] for name in tree[unit_lookup.doc_owner(doc, record if record is not None else staff.snapshot()).name]]

        def the_list(production: str, *, record: staff.StaffSnapshot | None = None) -> Path:
            del record
            # Only the root's doc has a Change review line; its children read its list.
            if production in ("kid", "grand"):
                raise change_review.ChangeReviewError("no line")
            return side_list if production == "side" else path

        def production(owner: staff.StaffRow) -> Production:
            return Production(docs[owner.name], owner.name, "", self.root, self.root / "log.md", ZoneInfo("UTC"), owner)

        def shown(kind: str, name: str, below: bool = False) -> list[str]:
            queue = next_item.read_queue(kind, docs[name], below)
            return sorted(found[1] for entry in queue.entries if (found := ITEM.search(entry.item)))

        def root_of(name: str, *, record: staff.StaffSnapshot | None = None) -> str:
            del record
            return "top" if name in tree["top"] else name

        def descendants_of(name: str, *, record: staff.StaffSnapshot | None = None) -> list[str]:
            del record
            return tree[name][1:]

        with mock.patch.object(proposals_folder, "owner_docs", owner_docs), \
                mock.patch.object(showrunner_tree, "root_of", root_of), \
                mock.patch.object(showrunner_tree, "descendants_of", descendants_of), \
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
        top, kid = self.root / "top-production.md", self.root / "kid-production.md"
        root = fixture_member("showrunner", name="top", doc=staff.LiveDoc(remote.local_machine(), top))
        _ = fixture_member("showrunner", name="kid", parent=root.id, doc=staff.LiveDoc(remote.local_machine(), kid))
        _ = change_review.append_rows(path, [Visible("kid change", "Run it", "It works")], date(2026, 10, 15), "",
                                      kid, record=staff.snapshot())

        def the_list(production: str, *, record: staff.StaffSnapshot | None = None) -> Path:
            del record
            if production == "kid":
                raise change_review.ChangeReviewError(f"{kid} has no line")
            return path

        _ = top.write_text(f"# Top\n- **Change review:** `{path}`\n")
        _ = kid.write_text("# Kid\n")
        with mock.patch.object(change_review, "production_list", the_list), \
                mock.patch.object(showrunner_tree, "root_of", return_value="top"), \
                mock.patch.object(showrunners, "timer_docs", return_value={"top": top, "kid": kid}), \
                mock.patch.object(proposals_folder, "owner_docs", return_value=[kid]):
            self.assertEqual([entry.item.splitlines()[0] for entry in next_item.change_queue(kid).entries],
                             ["C1 kid change"])

    def attached_changes(self) -> dict[str, Path]:
        docs = {name: self.root / f"{name}-production.md" for name in ("top", "kid", "grand")}
        for name, doc in docs.items():
            _ = doc.write_text(f"# {name}\n")
        folder = self.root / "change-review"
        folder.mkdir()
        header = "## To review\n\n| # | Change | Merged | How to check | Production | Urgency |\n| --- | --- | --- | --- | --- | --- |\n"
        _ = (folder / "change-review - top.md").write_text(header +
            "| C1 | Root-only change | 2026-10-01 | Try root | | |\n" +
            "| C9 | Current child change | 2026-10-15 | Try current | kid | |\n" +
            "| C434 | Newest child change | 2026-10-29 | Try newest | kid | |\n" +
            "| C2 | Current grandchild change | 2026-10-20 | Try grandchild | grand | |\n")
        _ = (folder / "change-review - kid.md").write_text(header +
            "| C9 | Earlier child change | 2026-10-02 | Try earlier | | |\n" +
            "| C10 | Urgent child change | 2026-10-03 | Try urgent | kid | blocks |\n")
        _ = (folder / "change-review - grand.md").write_text(header +
            "| C1 | Earlier grandchild change | 2026-10-04 | Try earlier grandchild | | |\n")
        tree = {"top": ["kid", "grand"], "kid": ["grand"], "grand": []}

        def descendants(name: str, *, record: staff.StaffSnapshot | None = None) -> list[str]:
            del record
            return tree[name]

        _ = self.enterContext(mock.patch.object(showrunner_tree, "root_of", return_value="top"))
        _ = self.enterContext(mock.patch.object(showrunners, "timer_docs", return_value=docs))
        _ = self.enterContext(mock.patch.object(showrunner_tree, "descendants_of", descendants))
        _ = self.enterContext(mock.patch.object(change_review, "production_list",
                                               return_value=folder / "change-review - top.md"))
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        listings = {folder / f"change-review - {name}.md": name for name in docs}
        selected_rows = {path: {row["#"] for row in change_review.pending(path)} for path in listings}
        if self._testMethodName == "test_stopped_root_keeps_columnless_rows_when_own_list_becomes_current":
            selected_rows[folder / "change-review - top.md"].difference_update({"C9", "C434"})
        producer_rows(self.root, docs, listings, selected_rows=selected_rows)
        return docs

    def test_attached_earlier_rows_keep_list_names_in_show_move_and_count(self) -> None:
        doc = self.attached_changes()["kid"]
        lists = {path: path.read_bytes() for path in (self.root / "change-review").glob("*.md")}
        for order, expected in (("urgent", "kid/C10 Urgent child change"),
                                ("oldest", "kid/C9 Earlier child change"),
                                ("newest", "C434 Newest child change")):
            code, output, error = run("show", "--kind", "change", "--order", order, "--production", unit_lookup.doc_owner(doc, staff.snapshot()).name)
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(output.splitlines()[:2], ["change", expected])
        _ = run("show", "--kind", "change", "--order", "oldest", "--production", unit_lookup.doc_owner(doc, staff.snapshot()).name)
        self.assertEqual(run("move-to-end", "--kind", "change", "--production", unit_lookup.doc_owner(doc, staff.snapshot()).name),
                         (0, "change: moved kid/C9 to the end\n", ""))
        self.assertEqual(run("show", "--kind", "change", "--order", "oldest", "--production", unit_lookup.doc_owner(doc, staff.snapshot()).name)[1].splitlines()[1],
                         "kid/C10 Urgent child change")
        queue = next_item.change_queue(doc)
        self.assertEqual({entry.key for entry in queue.entries}, {"kid/C9", "kid/C10", "C9", "C434"})
        self.assertEqual(footer_counts.changes(doc), len(queue.entries))
        self.assertEqual(footer_counts.counts_line(footer_counts.read_counts(doc)), "For you: 4 changes")
        self.assertEqual(set(lists) & set(footer_counts.sources(doc)),
                         {change_review.list_file("top"), change_review.list_file("kid")})
        self.assertEqual({path: path.read_bytes() for path in lists}, lists)

    def test_all_below_merges_earlier_lists_once_and_keeps_pins_separate(self) -> None:
        docs = self.attached_changes()
        child_queue = next_item.change_queue(docs["kid"])
        child_below = next_item.change_queue(docs["kid"], True)
        root_below = next_item.change_queue(docs["top"], True)
        self.assertEqual({entry.key for entry in child_below.entries},
                         {"kid/C9", "kid/C10", "C9", "C434", "C2", "grand/C1"})
        self.assertEqual({entry.key for entry in root_below.entries},
                         {entry.key for entry in child_below.entries} | {"C1"})
        self.assertEqual(len(root_below.entries), 7)
        self.assertEqual(footer_counts.changes(docs["grand"]), 2)
        _ = run("show", "--kind", "change", "--order", "oldest", "--production", unit_lookup.doc_owner(docs["kid"], staff.snapshot()).name)
        _ = run("show", "--kind", "change", "--order", "newest", "--all-below", "--production", unit_lookup.doc_owner(docs["kid"], staff.snapshot()).name)
        self.assertEqual(urgency.pinned(child_queue.name), "kid/C9")
        self.assertEqual(urgency.pinned(child_below.name), "C434")
        self.assertEqual(run("move-to-end", "--kind", "change", "--all-below", "--production", unit_lookup.doc_owner(docs["kid"], staff.snapshot()).name),
                         (0, "change: moved C434 to the end\n", ""))
        self.assertEqual(urgency.pinned(child_queue.name), "kid/C9")

    def test_change_review_shows_the_earlier_row_pinned_by_next_and_the_same_queue_count(self) -> None:
        doc = self.attached_changes()["kid"]
        next_code, next_output, next_error = run("show", "--kind", "change", "--order", "oldest",
                                                  "--production", unit_lookup.doc_owner(doc, staff.snapshot()).name)
        self.assertEqual((next_code, next_error), (0, ""))
        self.assertEqual(next_output.splitlines()[1], "kid/C9 Earlier child change")
        code, output, error = review_show(doc)
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(output.splitlines()[0], next_output.splitlines()[1])
        self.assertEqual(output.splitlines()[-1], f"{footer_counts.changes(doc) - 1} more to review")
        self.assertEqual(footer_counts.changes(doc), 4)
        self.assertNotIn("Root-only", output)
        self.assertEqual(run("move-to-end", "--kind", "change", "--production", unit_lookup.doc_owner(doc, staff.snapshot()).name),
                         (0, "change: moved kid/C9 to the end\n", ""))
        self.assertEqual(review_show(doc)[1].splitlines()[0], "kid/C10 Urgent child change")

    def test_next_keeps_the_change_review_pin_after_another_row_ranks_first(self) -> None:
        doc = self.attached_changes()["kid"]
        code, output, error = review_show(doc)
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(output.splitlines()[0], "kid/C10 Urgent child change")
        earlier = change_review.list_file("kid")
        _ = earlier.write_text(earlier.read_text().replace("| blocks |", "| |"))
        next_code, next_output, next_error = run("show", "--kind", "change", "--production", unit_lookup.doc_owner(doc, staff.snapshot()).name)
        self.assertEqual((next_code, next_error), (0, ""))
        self.assertEqual(next_output.splitlines()[1], output.splitlines()[0])

    def test_change_review_leaves_one_unreadable_list_out_and_matches_next(self) -> None:
        doc = self.attached_changes()["kid"]
        broken = change_review.list_file("kid")
        _ = broken.write_bytes(b"\xff")
        code, output, error = review_show(doc)
        self.assertEqual(code, 0)
        self.assertEqual(output.splitlines()[0], "C9 Current child change")
        self.assertEqual(output.splitlines()[-1], "1 more to review")
        self.assertEqual(len(error.splitlines()), 1)
        self.assertIn(str(broken), error)
        next_code, next_output, next_error = run("show", "--kind", "change", "--production", unit_lookup.doc_owner(doc, staff.snapshot()).name)
        self.assertEqual(next_code, 0)
        self.assertEqual(next_output.splitlines()[1], output.splitlines()[0])
        self.assertEqual(next_error, error)

    def test_unreadable_earlier_list_keeps_current_rows_with_one_stderr_line(self) -> None:
        doc = self.attached_changes()["kid"]
        broken = self.root / "change-review/change-review - kid.md"
        _ = broken.write_bytes(b"\xff")
        code, output, error = run("show", "--kind", "change", "--order", "oldest", "--production", unit_lookup.doc_owner(doc, staff.snapshot()).name)
        self.assertEqual(code, 0)
        self.assertEqual(output.splitlines()[1], "C9 Current child change")
        self.assertEqual(len(error.splitlines()), 1)
        self.assertIn(str(broken), error)
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            count = footer_counts.changes(doc)
        self.assertEqual(count, 2)
        self.assertEqual(len(errors.getvalue().splitlines()), 1)

    def test_current_list_pin_still_moves_after_an_earlier_list_appears(self) -> None:
        doc = self.attached_changes()["kid"]
        earlier = change_review.list_file("kid")
        saved = earlier.read_bytes()
        earlier.unlink()
        self.assertEqual(run("show", "--kind", "change", "--order", "newest", "--production", unit_lookup.doc_owner(doc, staff.snapshot()).name)[1].splitlines()[1],
                         "C434 Newest child change")
        queue = next_item.change_queue(doc)
        self.assertEqual(urgency.pinned(queue.name), "C434")
        _ = earlier.write_bytes(saved)
        self.assertEqual(run("move-to-end", "--kind", "change", "--production", unit_lookup.doc_owner(doc, staff.snapshot()).name),
                         (0, "change: moved C434 to the end\n", ""))
        self.assertIn("C434", urgency.ended(queue.name))

    def test_current_list_moved_row_stays_last_after_an_earlier_list_appears(self) -> None:
        doc = self.attached_changes()["kid"]
        earlier = change_review.list_file("kid")
        saved = earlier.read_bytes()
        earlier.unlink()
        _ = run("show", "--kind", "change", "--order", "newest", "--production", unit_lookup.doc_owner(doc, staff.snapshot()).name)
        self.assertEqual(run("move-to-end", "--kind", "change", "--production", unit_lookup.doc_owner(doc, staff.snapshot()).name),
                         (0, "change: moved C434 to the end\n", ""))
        queue = next_item.change_queue(doc)
        self.assertIn("C434", urgency.ended(queue.name))
        _ = earlier.write_bytes(saved)
        code, output, error = run("show", "--kind", "change", "--order", "newest", "--production", unit_lookup.doc_owner(doc, staff.snapshot()).name)
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(output.splitlines()[1], "C9 Current child change")
        self.assertIn("C434", urgency.ended(queue.name))
        entries = urgency.queue_order(next_item.change_queue(doc).entries, queue.name)
        self.assertEqual(entries[-1].key, "C434")

    def test_stopped_root_keeps_columnless_rows_when_own_list_becomes_current(self) -> None:
        production_list = change_review.production_list
        doc = self.attached_changes()["kid"]
        own = change_review.list_file("kid")
        _ = doc.write_text(f"# Kid\n\n- **Change review:** {own}\n")
        stopped = change_review.ChangeReviewError("the root showrunner is not running")
        with mock.patch.object(change_review, "production_list", side_effect=production_list), \
                mock.patch.object(change_review, "showrunner_of", side_effect=stopped):
            self.assertEqual(change_review.scope_list(doc), own)
            code, output, error = run("show", "--kind", "change", "--order", "oldest", "--production", unit_lookup.doc_owner(doc, staff.snapshot()).name)
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(output.splitlines()[1], "C9 Earlier child change")
            self.assertEqual(footer_counts.changes(doc), 2)
            self.assertEqual(footer_counts.counts_line(footer_counts.read_counts(doc)), "For you: 2 changes")

    def test_bad_urgency_in_earlier_list_leaves_current_rows_in_show_and_count(self) -> None:
        doc = self.attached_changes()["kid"]
        broken = change_review.list_file("kid")
        _ = broken.write_text(broken.read_text().replace("| blocks |", "| invalid |"))
        code, output, error = run("show", "--kind", "change", "--order", "oldest", "--production", unit_lookup.doc_owner(doc, staff.snapshot()).name)
        self.assertEqual(code, 0)
        self.assertEqual(output.splitlines()[1], "C9 Current child change")
        self.assertEqual(len(error.splitlines()), 1)
        self.assertIn(str(broken), error)
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            queue = next_item.change_queue(doc)
        self.assertEqual({entry.key for entry in queue.entries}, {"C9", "C434"})
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            count = footer_counts.changes(doc)
        self.assertEqual(count, len(queue.entries))
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn(str(broken), errors.getvalue())


if __name__ == "__main__":
    _ = unittest.main()
