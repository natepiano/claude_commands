#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
from __future__ import annotations

from scripts.agents import remote

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import cast, final, override
from unittest import mock


from scripts.production import change_review
from scripts.production import footer_counts
from scripts.production import next_item
from scripts.production import proposal_issue
from scripts.production import proposals_folder
from scripts.production.update_registration import QuestionStaff
from scripts.production import showrunners
from scripts.production import showrunner_tree
from scripts.production import staff
from scripts.production.test_change_review import producer_rows
from scripts.statusline import counts_row
from scripts.production.footer_counts import FooterCounts
from scripts.production.staff_test_support import (
    fixture_member, isolated_source_readers, isolated_staff_file, production_staff_record,
    recorded_showrunner, source_environment, unit_membership,
)

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
- 2026-10-09 Handed out (c.md) — accepted 2026-10-12, assigned enh-showrunner
- 2026-10-09 Done (d.md) — accepted 2026-10-09, applied (nixos b5c7d91)
- 2026-10-09 Shipped (e.md) — accepted 2026-10-10, built by message
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


def issue(folder: Path, name: str, status: str, tags: str, proposed_by: str, *fields: str) -> None:
    """Write an issue with a fixed proposer ID and the supplied fields."""
    extra = "".join(f"{field}\n" for field in fields)
    _ = (folder / f"{name}.md").write_text(f"---\nstatus: {status}\ntags: [{tags}]\nproposed_by: {proposed_by}\n{extra}---\n",
                                           encoding="utf-8")


def production_doc(folder: Path, name: str, proposals: Path, *units: str) -> Path:
    """A production doc that names the shared proposals folder, with staff rows for its units."""
    path = folder / f"{name}-production.md"
    _ = path.write_text(f"# {name}\n\n- **Proposals:** {proposals}\n", encoding="utf-8")
    _ = recorded_showrunner(name, path)
    for unit in units:
        _ = unit_membership(name, unit)
    return path


@final
class FooterCountsTests(unittest.TestCase):
    root: Path = Path()
    production: staff.StaffId = staff.StaffId("")
    tree: dict[str, Path] = {}  # each setUp replaces it
    below: dict[str, list[str]] = {}  # each setUp replaces it

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.root), "HERDR_BIN": "/nonexistent/herdr"})))
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, source_environment(self.root / "sources"))))
        _ = self.enterContext(isolated_source_readers())
        _ = fixture_member("showrunner", name="fixture")
        _ = self.enterContext(mock.patch.object(Path, "home", return_value=self.root))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"NEXT_STATE_DIR": str(self.root / "next")})))
        _ = self.enterContext(mock.patch.object(change_review, "lists_directory", return_value=self.root))
        self.production = fixture_member("showrunner", name="production",
            doc=staff.LiveDoc(remote.local_machine(), self.root / "production.md")).id
        # No live update timer names a production below this one, unless a test says so.
        self.tree = {}
        self.below = {}
        _ = self.enterContext(mock.patch.object(showrunners, "timer_docs", side_effect=lambda: self.tree))
        _ = self.enterContext(mock.patch.object(showrunner_tree, "descendants_of",
                                                side_effect=self.productions_below))
        # Each production below a root reports to that root.
        _ = self.enterContext(mock.patch.object(showrunner_tree, "edges", side_effect=self.parents))

    def parents(self, names: set[str] | None = None, *,
                record: staff.StaffSnapshot | None = None) -> dict[str, str]:
        del names, record
        return {child: parent for parent, children in self.below.items() for child in children}

    def productions_below(self, name: str, *,
                          record: staff.StaffSnapshot | None = None) -> list[str]:
        del record
        return self.below.get(name, [])

    def doc(self, *fields: str) -> Path:
        path = self.root / "production.md"
        _ = path.write_text("# Production\n\n" + "".join(f"{field}\n" for field in fields), encoding="utf-8")
        _ = recorded_showrunner("production", path)
        return path

    def test_a_footer_count_uses_one_staff_snapshot_for_scope_and_routing(self) -> None:
        listing = self.root / "change-review - production.md"
        _ = listing.write_text(LIST, encoding="utf-8")
        doc = self.doc(f"- **Change review:** `{listing}`")
        record = staff.snapshot()
        with mock.patch.object(staff, "snapshot", side_effect=[record]):
            self.assertEqual(footer_counts.changes(doc), 2)

    def test_one_count_read_routes_once_with_the_supplied_staff_snapshot(self) -> None:
        listing = self.root / "change-review - production.md"
        _ = listing.write_text(LIST, encoding="utf-8")
        doc = self.doc(f"- **Change review:** `{listing}`")
        record = staff.snapshot()
        with (mock.patch.object(change_review, "routed_review_entries", wraps=change_review.routed_review_entries) as routed,
              mock.patch.object(staff, "snapshot", side_effect=AssertionError("use the supplied snapshot"))):
            counts = footer_counts.read_counts(doc, record=record)
        self.assertEqual(counts.changes, 2)
        self.assertEqual(counts.unrouted, 0)
        routed.assert_called_once_with(listing, doc, record=record)

    def test_footer_counts_and_watches_questions_without_a_proposals_line(self) -> None:
        doc = self.root / "example-production.md"
        _ = doc.write_text("> **Status: PRODUCTION — running.** Example\n- **User zone:** UTC\n")
        owner = fixture_member("showrunner", name="renamed-owner", doc=staff.LiveDoc(remote.local_machine(), doc))
        state = self.root / "showrunner"
        folder = state / owner.id / "questions"
        folder.mkdir(parents=True)
        record = folder / "question-layout.md"
        fields = {"since": "2000-01-01T08:00", "text": "Choose the layout",
                  "question": "Keep this layout?", "asked_by": str(owner.id)}
        _ = record.write_text("---\nrecord_type: question\nstatus: open\n"
                              + f"production_id: {owner.id}\nasked_by: {owner.id}\n"
                              + f"registration: {json.dumps(fields)}\n---\n# Choose the layout\n")
        instance = self.root / f"notifier/showrunner-{owner.id}"
        instance.mkdir(parents=True)
        _ = (instance / "conf").write_text(
            f"TARGET=session:{owner.id}\nCHECK=zsh /x/production_check.sh {owner.id}\n")
        sources: list[Path] = []
        with mock.patch.dict(os.environ, {"SHOWRUNNER_STATE_DIR": str(state), "USER_ZONE": "UTC"}):
            line, due = counts_row.row(instance, sources)
        self.assertEqual(line, "For you: 1 question")
        self.assertEqual(due, "")
        self.assertIn(folder, sources)
        self.assertIn(record, sources)
        self.assertFalse((state / "outstanding/example.json").exists())

    def test_sources_use_the_supplied_staff_snapshot_for_lists_and_questions(self) -> None:
        listing = self.root / "change-review - production.md"
        _ = listing.write_text(LIST)
        doc = self.doc(f"- **Change review:** {listing}")
        identities = QuestionStaff.read()
        owner = identities.owner(doc)
        questions = Path(os.environ["SHOWRUNNER_STATE_DIR"]) / owner.id / "questions"
        with mock.patch.object(staff, "snapshot", side_effect=AssertionError("use the supplied snapshot")):
            self.assertEqual(footer_counts.sources(doc, identities=identities), [doc, listing, questions])

    def test_sources_keep_the_document_when_the_proposals_folder_is_unreadable(self) -> None:
        folder = self.root / "issues"
        folder.mkdir()
        doc = self.doc(f"- **Proposals:** {folder}")
        glob = Path.glob

        def read_folder(path: Path, pattern: str) -> object:
            if path == folder:
                raise PermissionError("The proposals folder is unreadable")
            return glob(path, pattern)

        with mock.patch.object(Path, "glob", side_effect=read_folder, autospec=True):
            sources = footer_counts.sources(doc)
        self.assertIn(doc, sources)
        self.assertIn(folder, sources)

    def test_each_count_is_read_from_the_source_its_doc_line_names(self) -> None:
        listing = self.root / "change-review - production.md"
        _ = listing.write_text(LIST, encoding="utf-8")
        folder = self.root / "issues"
        folder.mkdir()
        issue(folder, "wanted", "open", "proposal", self.production)
        issue(folder, "closed", "closed", "proposal", self.production)
        issue(folder, "bug", "open", "bug", self.production)
        ledger = self.root / "ledger.md"
        _ = ledger.write_text(LEDGER, encoding="utf-8")
        efficiency = self.root / "efficiency.md"
        _ = efficiency.write_text(EFFICIENCY_LEDGER, encoding="utf-8")
        doc = self.doc(f"- **Change review:** `{listing}`", f"- **Proposals:** {folder}",
                       f"- **Improvement queue:** {ledger}", f"- **Efficiency ledger:** `{efficiency}`")
        self.assertEqual(footer_counts.read_counts(doc, questions=1),
                         FooterCounts(changes=2, proposals=2, questions=1, efficiency=2))

    def test_a_source_counts_only_its_waiting_items_and_omits_unrouted_rows(self) -> None:
        own = unit_membership("production", "own").id
        other = unit_membership("production", "other").id
        empty = unit_membership("production", "empty").id
        listing = self.root / "change-review - production.md"
        _ = listing.write_text(
            "## To review\n\n| # | Change | How to check | Producer |\n"
            + "| --- | --- | --- | --- |\n"
            + f"| C1 | Own first | Read | {own} |\n| C2 | Own second | Read | {own} |\n"
            + f"| C3 | Other | Read | {other} |\n| C4 | Legacy | Read | |\n"
            + "| C5 | Unrouted | Read | missing-producer |\n")
        folder = self.root / "issues"
        folder.mkdir()
        for name, source, status in (("own first", own, "open"), ("own second", own, "open"),
                                     ("other", other, "open"), ("legacy", "", "open"), ("closed", own, "closed")):
            issue(folder, name, status, "proposal", source)
        backlog = self.root / "backlog.md"
        _ = backlog.write_text("- 2026-11-12 Legacy plan — proposed\n")
        ledger = self.root / "efficiency.md"
        efficiency_lines = (
            f"- 2026-11-12 config: Own first [gain/week: 12 min] [by: {own}] — proposed\n"
            + f"- 2026-11-12 config: Own second [by: {own}] — deferred\n"
            + f"- 2026-11-12 config: Other [by: {other}] — proposed\n"
            + "- 2026-11-12 config: Legacy — proposed\n"
            + f"- 2026-11-12 config: Done [by: {own}] — accepted, applied\n")
        doc = self.doc(f"- **Change review:** {listing}", f"- **Proposals:** {folder}",
                       f"- **Improvement queue:** {backlog}", f"- **Efficiency ledger:** {ledger}")
        for extra, full_efficiency, own_efficiency in (
                ("", 4, 2),
                (f"- 2026-11-12 config: T [by: {own}] — deferred; related item — done\n", 5, 3),
                (f"- 2026-11-12 config: T [by: {own}]  — proposed\n", 5, 3)):
            _ = ledger.write_text(efficiency_lines + extra)
            for source, expected in (
                    ("", FooterCounts(changes=4, proposals=4, efficiency=full_efficiency, unrouted=1)),
                    (own, FooterCounts(changes=2, proposals=2, efficiency=own_efficiency)),
                    (other, FooterCounts(changes=1, proposals=1, efficiency=1)),
                    (empty, FooterCounts())):
                with self.subTest(extra=extra, source=source):
                    self.assertEqual(footer_counts.read_counts(doc, source=source), expected)

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
            code = next_item.main(["show", "--kind", "proposal", "--production", "production"])
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
        top = production_doc(self.root, "top", folder, "top")
        child = production_doc(self.root, "child", folder, "child")
        self.tree = {"top": top, "child": child}
        self.below = {"top": ["child"]}
        issue(folder, "own issue", "open", "proposal", unit_membership("top", "top").id)
        issue(folder, "child issue", "open", "proposal", unit_membership("child", "child").id)
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
        listing = self.root / "change-review - production.md"
        folder = self.root / "issues"
        folder.mkdir()
        issue(folder, "wanted", "open", "proposal", self.production)
        issue(folder, "closed", "closed", "proposal", self.production)
        ledger = self.root / "ledger.md"
        doc = self.doc(f"- **Change review:** `{listing}`", f"- **Proposals:** {folder}",
                       f"- **Improvement queue:** {ledger}")
        self.assertEqual(footer_counts.sources(doc), [doc, listing, folder, ledger, folder / "wanted.md"])
        empty_doc = self.doc()
        owner = staff.snapshot().by_doc(remote.local_machine(), empty_doc)
        assert owner is not None
        question_folder = Path(os.environ["SHOWRUNNER_STATE_DIR"]) / owner.id / "questions"
        self.assertEqual(footer_counts.sources(empty_doc), [empty_doc, question_folder])

    def test_a_shared_folder_counts_only_the_productions_own_proposals(self) -> None:
        folder = self.root / "issues"
        folder.mkdir()
        top = production_doc(self.root, "tool-based-ui", folder, "trunk")
        child = production_doc(self.root, "cargo-liner-style", folder, "types")
        other = production_doc(self.root, "build-followups", folder, "followups")
        trunk = unit_membership("tool-based-ui", "trunk").id
        types = unit_membership("cargo-liner-style", "types").id
        for name, proposer in (("own", trunk), ("by the production", recorded_showrunner("tool-based-ui", top).id),
                               ("by a child", recorded_showrunner("cargo-liner-style", child).id), ("by a unit below", types),
                               ("named with its production", types), ("quoted link", f"'[[{trunk}]]'"),
                               ("other production", unit_membership("build-followups", "followups").id),
                               ("other unit in a known production", unit_membership("build-followups", "trunk").id),
                               ("no proposer", "")):
            issue(folder, name, "open", "issue, proposal", proposer)
        issue(folder, "closed own", "closed", "issue, proposal", trunk)
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
        listing = self.root / "change-review - top.md"
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
        producer_rows(self.root, self.tree, {listing: "top"})
        # A row without a Production cell is the root's; a child with no list line reads the root's list.
        self.assertEqual([footer_counts.changes(doc) for doc in (top, kid, grandkid)], [2, 1, 1])
        self.assertIn(listing, footer_counts.sources(kid))

    def test_routing_sources_watch_the_live_configuration_file(self) -> None:
        configuration = self.root / "settings"
        configuration.mkdir()
        source = configuration / "change_review.conf"
        _ = source.write_text(f"lists_directory={self.root}\nreview_links_directory={self.root / 'links'}\n")
        root_configuration = configuration / "review_links.conf"
        _ = root_configuration.write_text("unrouted_alert_recipient=top\n")
        with mock.patch.dict(os.environ, {"CONF_DIR": str(configuration)}):
            paths = footer_counts.routing_sources(staff.snapshot())
        self.assertIn(source, paths)
        self.assertIn(root_configuration, paths)
        self.assertIn(self.root, paths)
        self.assertIn(self.root / "links", paths)

    def test_a_missing_alert_setting_keeps_footer_and_next_routing_available(self) -> None:
        from scripts.production import change_routing, unrouted_alert

        _ = self.enterContext(mock.patch.object(change_routing, "configured_lists_directory", return_value=self.root))

        config = self.root / "config"
        config.mkdir()
        source = Path(__file__).resolve().parents[2] / "config/change_review.conf"
        _ = (config / "change_review.conf").write_text(source.read_text())
        _ = (config / "review_links.conf").write_text("unrouted_alert_recipient=top\n")
        self.enterContext(mock.patch.dict(os.environ, {"CONF_DIR": str(config)}))
        listing = self.root / "change-review - top.md"
        doc = self.root / "top-production.md"
        _ = doc.write_text(f"# Top\n- **Change review:** `{listing}`\n")
        self.tree = {"top": doc}
        top = fixture_member("showrunner", name="top", doc=staff.LiveDoc(remote.local_machine(), doc))
        one = fixture_member("unit", name="one", parent=top.id)
        _ = listing.write_text(LIST.replace("| How to check |", "| How to check | Producer |")
            .replace("| --- | --- | --- |", "| --- | --- | --- | --- |")
            .replace("| look |", f"| look | {one.id} |"))
        snapshot = staff.snapshot()
        from dataclasses import replace
        broken = replace(snapshot, rows=tuple(replace(row, parent=None) if row.id == one.id else row
                                               for row in snapshot.rows))
        with (mock.patch.object(staff, "snapshot", return_value=broken), production_staff_record(),
              mock.patch.object(unrouted_alert, "send_message") as send, contextlib.redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(footer_counts.changes(doc), 0)
            self.assertEqual(footer_counts.unrouted_changes(doc), 2)
            self.assertEqual(change_review.scoped_review_entries(listing, doc), [])
        send.assert_not_called()
        self.assertIn("review-links unrouted alert:", errors.getvalue())

    def test_rows_without_producers_count_for_the_list_showrunner_on_both_readers(self) -> None:
        from scripts.review_links import change_browser, change_page
        from scripts.production import change_review as page_review
        from scripts.review_links.test_change_page import BrowserForms

        listing = self.root / "change-review - top.md"
        _ = listing.write_text("## To review\n\n| # | Change | Production | How to check |\n"
                               + "| --- | --- | --- | --- |\n"
                               + "| C1 | First | top | Read |\n| C2 | Second | kid | Read |\n"
                               + "| C3 | Third | grandkid | Read |\n| C4 | Fourth | | Read |\n")
        self.tree = {name: self.root / f"{name}-production.md" for name in ("top", "kid", "grandkid")}
        self.below = {"top": ["kid", "grandkid"]}
        for name, doc in self.tree.items():
            _ = doc.write_text(f"# {name}\n- **Change review:** `{listing}`\n- **Showrunner checkout:** `{self.root}`\n")
            _ = fixture_member("showrunner", name=name, doc=staff.LiveDoc(remote.local_machine(), doc))
        with mock.patch.object(page_review, "lists_directory", return_value=self.root):
            forms = BrowserForms()
            forms.feed(change_browser.render(change_page.ReviewBrowser("secret"), change_page.DeliveryStatus(self.root)).decode())
        counts = [footer_counts.changes(doc) for doc in self.tree.values()]
        self.assertEqual(counts, [4, 0, 0])
        self.assertEqual(counts, [forms.counts[name] for name in self.tree])

    def test_orphan_rows_count_under_the_root_tab_before_and_after_a_document_is_stamped(self) -> None:
        from scripts.review_links import change_browser, change_page
        from scripts.production import change_review as page_review

        listing = self.root / "change-review - orphan.md"
        doc = self.root / "top-production.md"
        own_list = self.root / "change-review - top.md"
        _ = doc.write_text(f"# Top\n- **Change review:** `{own_list}`\n")
        _ = fixture_member("showrunner", name="top", doc=staff.LiveDoc(remote.local_machine(), doc))
        self.tree = {"top": doc}
        config = self.root / "config"
        config.mkdir()
        _ = (config / "review_links.conf").write_text("unrouted_alert_recipient=top\n")
        _ = (config / "change_review.conf").write_text(
            f"lists_directory={self.root}\nreview_links_directory={self.root / 'links'}\n")
        with (mock.patch.dict(os.environ, {"CONF_DIR": str(config)}),
              mock.patch.object(page_review, "lists_directory", return_value=self.root)):
            for document in (self.root / "missing-production.md", doc):
                with self.subTest(document=document):
                    _ = listing.write_text(f'---\nproductions:\n  - "{document}"\n---\n' + LIST)
                    prepared = change_browser.prepare(change_page.ReviewBrowser("token"),
                        change_page.DeliveryStatus(self.root / "links"))
                    waiting = [item for item in prepared.changes if item.waiting]
                    self.assertEqual([item.key for item in waiting], ["orphan/C1", "orphan/C2"])
                    self.assertEqual([item.tab for item in waiting], ["top", "top"])
                    self.assertEqual(footer_counts.change_counts(doc, staff.snapshot()), (2, 0))

    def test_rows_without_producers_in_independent_lists_stay_with_their_showrunners(self) -> None:
        from scripts.review_links import change_browser, change_page
        from scripts.production import change_review as page_review

        self.tree = {name: self.root / f"{name}-production.md" for name in ("one", "two")}
        listings = {self.root / f"change-review - {name}.md": name for name in self.tree}
        for listing in listings:
            _ = listing.write_text("## To review\n\n| # | Change |\n| --- | --- |\n| C1 | First |\n")
        producer_rows(self.root, self.tree, listings, selected_rows={})
        with mock.patch.object(page_review, "lists_directory", return_value=self.root):
            browser = change_page.ReviewBrowser("token")
            prepared = change_browser.prepare(browser, change_page.DeliveryStatus(self.root / "links"))
        count = sum(item.waiting and item.tab == "unrouted" for item in prepared.changes)
        self.assertEqual(count, 0)
        self.assertEqual([footer_counts.unrouted_changes(doc) for doc in self.tree.values()], [count, count])

    def test_the_proposal_count_leaves_out_approved_and_active_proposals(self) -> None:
        folder = self.root / "issues"
        folder.mkdir()
        approval = f'unit: "{unit_membership("production", "names").id}"'
        issue(folder, "waiting", "open", "issue, proposal", self.production, "stage: backlog")
        issue(folder, "approved at backlog", "open", "issue, proposal", self.production, "stage: backlog", approval)
        issue(folder, "approved at active", "open", "issue, proposal", self.production, "stage: active", approval)
        issue(folder, "started without an approval", "open", "issue, proposal", self.production, "stage: active")
        doc = self.doc(f"- **Proposals:** {folder}")
        self.assertEqual(len(proposals_folder.open_proposals(doc)), 4)
        self.assertEqual(footer_counts.proposals(doc), 1)
        self.assertEqual(footer_counts.counts_line(footer_counts.read_counts(doc)), "For you: 1 proposal")

    def test_an_approved_plain_proposal_leaves_the_footer_count(self) -> None:
        folder = self.root / "issues"
        folder.mkdir()
        issue(folder, "plain", "open", "issue, proposal", self.production, "stage: backlog")
        doc = self.doc(f"- **Proposals:** {folder}")
        self.assertEqual(footer_counts.proposals(doc), 1)
        unit = unit_membership("production", "u").id
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(proposal_issue, "calling_showrunner", return_value=self.production):
            code = proposal_issue.main(["approve", str(folder / "plain.md"), "--by", "user", "--unit", unit])
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
