"""Check fixed producer routing and shared fixtures for the count readers."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import cast, override
from unittest import mock

from scripts.production import change_review, change_routing, staff, unrouted_alert
from scripts.production.staff_test_support import fixture_member, fixture_runtime_id
from scripts.production.test_change_review import ProducerFixture, producer_rows


class StaffRoutingTests(unittest.TestCase):
    root: Path = Path()
    top: staff.StaffRow
    child: staff.StaffRow
    unit: staff.StaffRow
    alert: mock.Mock

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.top = staff.StaffRow(staff.StaffId(""), "showrunner", "", None, current_session=staff.StaffId(""))
        self.alert = mock.Mock()
        self.child = self.top
        self.unit = self.top

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.dict(os.environ, {"STAFF_FILE": str(self.root / "staff.json"),
            "CHANGE_REVIEW_DIR": str(self.root), "REVIEW_LINKS_ROOT": str(self.root / "links")}))
        self.alert = cast(mock.Mock, self.enterContext(mock.patch.object(unrouted_alert, "notify")))
        self.top = fixture_member("showrunner", name="top")
        self.child = fixture_member("showrunner", name="child", parent=self.top.id)
        self.unit = fixture_member("unit", name="builder", parent=self.child.id,
            worktree=staff.PresentWorktree("natedev", self.root / "builder"))

    def router(self, record: staff.StaffSnapshot | None = None) -> change_routing.StaffRouting:
        return change_routing.StaffRouting(record or staff.snapshot(), {})

    def test_a_stored_unit_id_routes_without_history_and_ignores_production_and_merge(self) -> None:
        row = {"#": "C1", "Producer": self.unit.id, "Production": "top", "Merged": "unknown"}
        owner = self.router().owner(row, self.root / "change-review - top.md")
        self.assertEqual(owner, change_routing.StaffRoutedChange(self.unit.id, self.child.id, "child"))

    def test_a_checkpoint_merge_routes_only_to_its_stored_producer_showrunner(self) -> None:
        row = {"#": "C403", "Producer": self.unit.id, "Merged": "11fb49ec",
               "Change": "A change outside the imported checkpoints", "Production": "top"}
        owner = self.router().owner(row, self.root / "change-review - top.md")
        self.assertEqual(owner, change_routing.StaffRoutedChange(self.unit.id, self.child.id, "child"))
        self.assertNotIsInstance(owner, change_routing.UnroutedChange)
        cast(mock.Mock, self.alert).assert_not_called()

    def test_an_empty_producer_uses_the_list_showrunner(self) -> None:
        self.assertEqual(self.router().owner({"#": "C1", "Merged": "abcdef12"}, self.root / "change-review - top.md"),
                         change_routing.ShowrunnerOwnChange(self.top.id, "top"))

    def test_an_unknown_producer_id_is_unrouted_and_alerts_with_the_row(self) -> None:
        identity = fixture_runtime_id("missing-producer")
        owner = self.router().owner({"#": "C777", "Producer": identity}, self.root / "change-review - top.md")
        self.assertIsInstance(owner, change_routing.UnroutedChange)
        assert isinstance(owner, change_routing.UnroutedChange)
        self.assertIn(identity, owner.note)
        self.assertEqual(owner.tab, "unrouted")
        self.assertEqual(cast(mock.Mock, self.alert).call_args.args[2]["#"], "C777")

    def test_renamed_and_retired_units_keep_their_producer_route(self) -> None:
        row = {"#": "C1", "Producer": self.unit.id}
        before = self.router().owner(row, self.root / "change-review - top.md")
        _ = staff.rename(self.unit.id, "renamed")
        _ = staff.set_worktree(self.unit.id, staff.RemovedWorktree("natedev", self.root / "builder"),
                                   expected=self.unit.worktree)
        self.assertEqual(self.router().owner(row, self.root / "change-review - top.md"), before)

    def test_a_showrunner_producer_owns_its_change(self) -> None:
        self.assertEqual(self.router().owner({"#": "C1", "Producer": self.child.id}, self.root / "change-review - top.md"),
                         change_routing.ShowrunnerOwnChange(self.child.id, "child"))

    def test_a_broken_parent_chain_stays_unrouted(self) -> None:
        record = staff.snapshot()
        broken = replace(record, rows=tuple(replace(row, parent=None) if row.id == self.unit.id else row
                                            for row in record.rows))
        owner = self.router(broken).owner({"#": "C1", "Producer": self.unit.id}, self.root / "change-review - top.md")
        self.assertIsInstance(owner, change_routing.UnroutedChange)
        assert isinstance(owner, change_routing.UnroutedChange)
        self.assertIn("No staff showrunner", owner.note)

    def test_the_owner_document_does_not_require_a_list_document(self) -> None:
        doc = self.root / "child-production.md"
        _ = doc.write_text("# Child")
        _ = staff.set_doc(self.child.id, staff.LiveDoc("natedev", doc))
        listing = self.root / "change-review - missing.md"
        assignment = self.router().owner_and_production(listing, {"#": "C1", "Producer": self.unit.id})
        self.assertEqual(assignment.production, doc)
        self.assertEqual(assignment.owner.tab, "child")
        _ = staff.set_doc(self.top.id, staff.LiveDoc("natedev", doc))
        assignment = self.router().owner_and_production(self.root / "change-review - top.md", {"#": "C2"})
        self.assertEqual(assignment.production, doc)
        self.assertEqual(assignment.owner.tab, "top")

    def test_an_unset_staff_document_uses_the_supplied_production_document(self) -> None:
        doc = self.root / "supplied.md"
        _ = doc.write_text("# Supplied")
        router = change_routing.StaffRouting(staff.snapshot(), {"child": doc})
        assignment = router.owner_and_production(self.root / "change-review - top.md", {"#": "C1", "Producer": self.unit.id})
        self.assertEqual(assignment.production, doc)
        self.assertEqual(assignment.owner.tab, "child")

    def test_a_missing_staff_document_uses_the_supplied_production_document(self) -> None:
        doc = self.root / "supplied.md"
        _ = doc.write_text("# Supplied")
        _ = staff.set_doc(self.child.id, staff.LiveDoc("natedev", self.root / "missing.md"))
        router = change_routing.StaffRouting(staff.snapshot(), {"child": doc})
        self.assertEqual(router.owner_and_production(self.root / "change-review - top.md",
                         {"#": "C1", "Producer": self.unit.id}).production, doc)

    def test_repeated_row_assignments_check_documents_once_per_showrunner(self) -> None:
        doc = self.root / "child-production.md"
        _ = doc.write_text("# Child")
        record = staff.snapshot()
        original = Path.is_file
        with mock.patch.object(Path, "is_file", autospec=True, side_effect=original) as checks:
            router = change_routing.StaffRouting(record, {"child": doc})
            for number in range(50):
                assignment = router.owner_and_production(self.root / "change-review - top.md",
                    {"#": f"C{number}", "Producer": self.unit.id})
                self.assertEqual(assignment.owner,
                    change_routing.StaffRoutedChange(self.unit.id, self.child.id, "child"))
                self.assertEqual(assignment.production, doc)
            self.assertLessEqual(checks.call_count, len(record.find(kind="showrunner")))

    def test_list_paths_read_each_document_only_on_the_first_access(self) -> None:
        documents = {name: self.root / f"{name}-production.md" for name in ("top", "child")}
        expected = {name: self.root / f"change-review - {name}.md" for name in documents}
        for name, doc in documents.items():
            _ = doc.write_text(f"# {name}\n- **Change review:** `{expected[name]}`\n")
        record = staff.snapshot()
        original = Path.read_text
        with mock.patch.object(Path, "read_text", autospec=True, side_effect=original) as reads:
            router = change_routing.StaffRouting(record, documents)
            self.assertEqual(reads.call_count, 0)
            self.assertEqual(router.list_paths, expected)
            self.assertEqual(reads.call_count, len(documents))
            self.assertEqual(router.list_paths, expected)
            self.assertEqual(reads.call_count, len(documents))

    def test_a_showrunner_without_either_document_has_an_unavailable_production(self) -> None:
        listing = self.root / "change-review - top.md"
        assignment = self.router().owner_and_production(listing, {"#": "C1", "Producer": self.unit.id})
        self.assertEqual(assignment.production, change_routing.UnavailableProduction(listing))

    def test_an_existing_staff_document_wins_over_the_supplied_document(self) -> None:
        supplied, authoritative = self.root / "supplied.md", self.root / "staff.md"
        _ = supplied.write_text("# Supplied")
        _ = authoritative.write_text("# Staff")
        _ = staff.set_doc(self.child.id, staff.LiveDoc("natedev", authoritative))
        router = change_routing.StaffRouting(staff.snapshot(), {"child": supplied})
        self.assertEqual(router.owner_and_production(self.root / "change-review - top.md",
                         {"#": "C1", "Producer": self.unit.id}).production, authoritative)

    def test_a_missing_showrunner_uses_the_supplied_document_under_the_list_name(self) -> None:
        supplied = self.root / "supplied.md"
        _ = supplied.write_text("# Supplied")
        router = change_routing.StaffRouting(staff.StaffSnapshot(()), {"missing": supplied})
        assignment = router.owner_and_production(self.root / "change-review - missing.md", {"#": "C1"})
        self.assertEqual(assignment.production, supplied)
        self.assertIsInstance(assignment.owner, change_routing.ShowrunnerOwnChange)

    def test_the_page_and_footer_count_each_producer_state_the_same_way(self) -> None:
        from scripts.production import footer_counts, showrunners
        from scripts.review_links import change_browser, change_page
        import change_review as cli_review

        docs = {name: self.root / f"{name}-production.md" for name in ("top", "child")}
        listing = self.root / "change-review - top.md"
        for name, doc in docs.items():
            _ = doc.write_text(f"# {name}\n- **Change review:** `{listing}`\n")
        _ = staff.set_doc(self.top.id, staff.LiveDoc("natedev", docs["top"]))
        _ = staff.set_doc(self.child.id, staff.LiveDoc("natedev", docs["child"]))
        retired = fixture_member("unit", name="retired", parent=self.child.id,
                                worktree=staff.RemovedWorktree("natedev", self.root / "removed"))
        _ = staff.rename(self.unit.id, "renamed")
        _ = change_review.append_rows(listing, [change_review.Visible(f"Change {i}", "Read", "Proof") for i in range(5)],
            date(2026, 11, 2), "", docs["top"],
            producers=[self.unit.id, change_review.UnknownProducer(), staff.StaffId(fixture_runtime_id("missing-producer")), retired.id, self.child.id])
        with (mock.patch.object(change_review, "LISTS", self.root), mock.patch.object(cli_review, "LISTS", self.root),
              mock.patch.object(showrunners, "timer_docs", return_value=docs)):
            prepared = change_browser.prepare(change_page.ReviewBrowser("secret"), change_page.DeliveryStatus(self.root / "links"))
            counts = {tab: sum(item.waiting and item.tab == tab for item in prepared.changes) for tab in prepared.queues}
            self.assertEqual(counts["top"], 1)
            self.assertEqual(counts["child"], 3)
            self.assertEqual(counts["unrouted"], 1)
            for name, doc in docs.items():
                self.assertEqual(footer_counts.changes(doc), counts[name])
                self.assertEqual(footer_counts.unrouted_changes(doc), counts["unrouted"])

    def test_saved_requests_leave_the_count_regardless_of_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            listing = root / "change-review - top.md"
            row = {"#": "C1"}
            record = change_routing.delivery_path(root, listing, "C1")
            for fields, expected in (({"validation": {"state": "sent", "attempt": "validation-attempt"}}, False),
                                     ({"validation": {"state": "failed", "attempt": "old"}}, False),
                                     ({"validation": {"state": "pending", "attempt": "pending"}}, False),
                                     ({"question": {"question_id": "q1", "text": "Why?", "delivery": {"state": "pending", "attempt": "pending"}}}, False),
                                     ({"question": {"question_id": "q1", "text": "Why?", "delivery": None}}, False),
                                     ({"question": {"question_id": "q1", "text": "Why?", "answer": "Because"}}, True)):
                with self.subTest(fields=fields):
                    _ = record.write_text(json.dumps(fields))
                    self.assertEqual(change_routing.waits_for_user(listing, row, root), expected)


    def test_only_named_fixture_rows_receive_producer_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.dict(os.environ, {"STAFF_FILE": str(root / "staff.json")}):
                listing = root / "change-review - top.md"
                _ = listing.write_text("## To review\n\n| # | Change | Merged | Production |\n"
                                       + "| --- | --- | --- | --- |\n"
                                       + "| C1 | Own change | 2026-10-01 | kid |\n"
                                       + "| C2 | Unit change | 2026-10-02 | kid |\n")
                docs = {name: root / f"{name}-production.md" for name in ("top", "kid")}
                producer_rows(root, docs, {listing: "top"}, selected_rows={listing: {"C2"}})
                own, unit = change_review.pending(listing)
                router = change_routing.StaffRouting(staff.snapshot(), docs)
                self.assertEqual(own["Merged"], "2026-10-01")
                self.assertIsInstance(router.owner(own, listing), change_routing.ShowrunnerOwnChange)
                self.assertEqual(router.owner(own, listing).tab, "top")
                self.assertIsInstance(router.owner(unit, listing), change_routing.StaffRoutedChange)
                self.assertEqual(router.owner(unit, listing).tab, "kid")

    def test_a_row_without_a_producer_counts_for_its_list_and_sends_to_its_showrunner(self) -> None:
        from scripts.production import footer_counts, showrunners
        from scripts.review_links import change_browser, change_page

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config"
            config.mkdir()
            source = Path(__file__).resolve().parents[2] / "config/review_links.conf"
            _ = (config / "review_links.conf").write_text(source.read_text().replace("delivery_pause_seconds=5", "delivery_pause_seconds=0"))
            environment = {"CONF_DIR": str(config), "STAFF_FILE": str(root / "staff.json"), "CHANGE_REVIEW_DIR": str(root),
                           "REVIEW_LINKS_ROOT": str(root / "links"),
                           "NEXT_STATE_DIR": str(root / "next"), "FEATURE_LOG_VAULT": str(root / "vault")}
            with mock.patch.dict(os.environ, environment):
                listing = root / "change-review - natedev.md"
                _ = listing.write_text("## To review\n\n| # | Change | Production |\n| --- | --- | --- |\n| C1 | Known | natedev |\n")
                docs = {name: root / f"{name}-production.md" for name in ("natedev", "builds")}
                producer_rows(root, docs, {listing: "natedev"})
                _ = change_review.append_rows(listing, [change_review.Visible("Unknown AWS change", "Read", "Proof")],
                                               date(2026, 11, 2), "e41fa2a4", docs["builds"])
                _ = listing.write_text(listing.read_text().replace("| C2 |", "| C777 |"))
                runner = staff.snapshot().by_path("builds")
                parent = staff.snapshot().by_path("natedev")
                assert runner is not None and parent is not None
                _ = staff.move(runner.id, parent.id)
                import change_review as cli_review
                with (mock.patch.object(change_review, "LISTS", root), mock.patch.object(cli_review, "LISTS", root),
                      mock.patch.object(showrunners, "timer_docs", return_value=docs)):
                    browser = change_page.ReviewBrowser("secret")
                    status = change_page.DeliveryStatus(root / "links")
                    prepared = change_browser.prepare(browser, status)
                    unknown = next(item for item in prepared.changes if item.row["#"] == "C777")
                    self.assertEqual(unknown.tab, "natedev")
                    self.assertIsInstance(unknown.page, change_page.AssignedReviewPage)
                    assert isinstance(unknown.page, change_page.AssignedReviewPage)
                    self.assertIsInstance(unknown.page.owner, change_routing.ShowrunnerOwnChange)
                    self.assertEqual(unknown.page.owner.showrunner_recipients, (parent.id,))
                    counts = {tab: sum(item.tab == tab and item.waiting for item in prepared.changes) for tab in prepared.queues}
                    self.assertEqual(counts.get("unrouted", 0), 0)
                    for tab, doc in docs.items():
                        self.assertEqual(counts[tab], footer_counts.changes(doc))
                    self.assertEqual(counts.get("unrouted", 0), footer_counts.unrouted_changes(docs["natedev"]))
                    self.assertNotIn("unrouted", footer_counts.counts_line(footer_counts.read_counts(docs["natedev"])) or "")
                    from urllib.parse import urlencode
                    fields = {"row": "natedev/C777", "token": change_browser.row_token(browser, "natedev/C777"),
                              "outcome": "works", "feedback": ""}
                    with mock.patch.object(change_page, "send_message") as send:
                        delivery, answer = change_browser.submit(browser, urlencode(fields), status)
                        change_page.deliver(delivery, status)
                    self.assertEqual(answer, "works")
                    send.assert_called_once()
                    self.assertEqual(send.call_args.args[0], f"session:{parent.current_session}")
                    self.assertIsInstance(status.slots(change_page.ReviewPage(listing, docs["builds"], (), ""), "C777").verdict,
                                          change_page.NoDelivery)


class MergeWriterRoutingTests(ProducerFixture):
    def test_a_gate_written_producer_routes_to_the_unit_and_receives_return_feedback(self) -> None:
        from scripts.production import merge_gate
        from scripts.review_links import change_page

        merged = self.merge()
        commits = change_review.merge_commits(self.repo, merged)
        with mock.patch.object(change_review, "drift", return_value=[]):
            merge_gate.record_changes(self.repo, self.listing, self.doc,
                                      change_review.publication_changes(commits, self.doc), "main")
        row = change_review.pending(self.listing)[0]
        owner = change_routing.StaffRouting(staff.snapshot(), {}).owner(row, self.listing)
        self.assertIsInstance(owner, change_routing.StaffRoutedChange)
        assert isinstance(owner, change_routing.StaffRoutedChange)
        self.assertEqual(owner.unit_id, self.unit.id)
        self.assertEqual(change_page.verdict_recipients(owner, "not works"), (self.unit.id, owner.showrunner_id))
