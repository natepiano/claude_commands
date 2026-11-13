"""Check fixed producer routing and shared fixtures for the count readers."""
from __future__ import annotations

from scripts.agents import remote

import json
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import cast, override
from unittest import mock

from scripts.lib import conf
from scripts.lint.memory_test_support import config_environment, write_config_fixture
from scripts.production import change_review, change_routing, staff, unrouted_alert
from scripts.production.staff_test_support import fixture_member, fixture_runtime_id, production_staff_record
from scripts.production.test_change_review import ProducerFixture, producer_rows


class StaffRoutingTests(unittest.TestCase):
    root: Path = Path()
    top: staff.StaffRow
    child: staff.StaffRow
    unit: staff.StaffRow
    alert: mock.Mock

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.top = staff.StaffRow(staff.StaffId(""), "showrunner", "", staff.StaffRoot(), current_session=staff.StaffId(""))
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
            worktree=staff.PresentWorktree(remote.local_machine(), self.root / "builder"))

    def router(self, record: staff.StaffSnapshot | None = None) -> change_routing.StaffRouting:
        return change_routing.StaffRouting(record or staff.snapshot(), {})

    def configured_root_document(self) -> Path:
        doc = self.root / "top-production.md"
        _ = doc.write_text("# Top")
        _ = staff.set_doc(self.top.id, staff.LiveDoc(remote.local_machine(), doc))
        config = self.root / "config"
        _ = write_config_fixture(config / "review_links.conf", "unrouted_alert_recipient=top\n")
        _ = write_config_fixture(config / "change_review.conf")
        self.enterContext(mock.patch.dict(os.environ, {"CONF_DIR": str(config)}))
        return doc

    def test_a_stored_unit_id_routes_without_history_and_ignores_production_and_merge(self) -> None:
        row = {"#": "C1", "Producer": self.unit.id, "Production": self.top.id, "Merged": "unknown"}
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

    def test_a_production_cell_uses_the_unit_id_after_rename_and_transfer(self) -> None:
        row = {"#": "C1", "Production": self.unit.id}
        _ = staff.rename(self.unit.id, "renamed")
        _ = staff.move(self.unit.id, staff.ReportsTo(self.top.id))
        _ = fixture_member("unit", name="builder", parent=self.child.id,
                           runtime_id=fixture_runtime_id("replacement-builder"))
        self.assertEqual(self.router().owner(row, self.root / "change-review - child.md"),
                         change_routing.StaffRoutedChange(self.unit.id, self.top.id, "top"))

    def test_a_stored_production_name_goes_to_the_list_owner(self) -> None:
        row = {"#": "C1", "Production": self.child.name}
        owner = self.router().owner(row, self.root / "change-review - top.md")
        self.assertEqual(owner, change_routing.ShowrunnerOwnChange(self.top.id, "top"))
        cast(mock.Mock, self.alert).assert_not_called()

    def test_an_unknown_producer_id_is_unrouted_and_alerts_with_the_row(self) -> None:
        _ = self.enterContext(production_staff_record())
        _ = self.enterContext(mock.patch.object(change_routing, "configured_lists_directory", return_value=self.root))
        identity = fixture_runtime_id("missing-producer")
        owner = self.router().owner({"#": "C777", "Producer": identity}, self.root / "change-review - top.md")
        self.assertIsInstance(owner, change_routing.UnroutedChange)
        assert isinstance(owner, change_routing.UnroutedChange)
        self.assertIn(identity, owner.note)
        self.assertEqual(owner.tab, "unrouted")
        self.assertEqual(cast(mock.Mock, self.alert).call_args.args[2]["#"], "C777")

    def test_a_staff_override_record_sends_no_alert_for_a_real_list(self) -> None:
        with (mock.patch.object(change_routing, "configured_lists_directory", return_value=self.root),
              mock.patch.object(change_routing, "waits_for_user") as waiting):
            owner = self.router().owner({"#": "C9", "Producer": "unknown"},
                                        self.root / "change-review - top.md")
        self.assertIsInstance(owner, change_routing.UnroutedChange)
        cast(mock.Mock, self.alert).assert_not_called()
        waiting.assert_not_called()

    def test_a_scratch_router_sends_no_alert_after_the_staff_override_ends(self) -> None:
        router = self.router()
        with (mock.patch.dict(os.environ),
              mock.patch.object(change_routing, "configured_lists_directory", return_value=self.root)):
            del os.environ["STAFF_FILE"]
            owner = router.owner({"#": "C9", "Producer": "unknown"}, self.root / "change-review - top.md")
        self.assertIsInstance(owner, change_routing.UnroutedChange)
        cast(mock.Mock, self.alert).assert_not_called()

    def test_an_unreadable_staff_record_raises_and_sends_no_alert(self) -> None:
        record = self.root / "unreadable-staff.json"
        with mock.patch.dict(os.environ, {"STAFF_FILE": str(record)}):
            with self.assertRaises(staff.StaffAbsent):
                _ = self.router()
            _ = record.write_text("not JSON", encoding="utf-8")
            with self.assertRaises(staff.StaffUnreadable):
                _ = self.router()

    def test_one_router_reads_the_configured_alert_folder_once(self) -> None:
        _ = self.enterContext(production_staff_record())
        router = self.router()
        listing = self.root / "change-review - top.md"
        with mock.patch.object(change_routing, "configured_lists_directory", return_value=self.root) as configured:
            for number in range(3):
                owner = router.owner({"#": f"C{number}", "Producer": "unknown"}, listing)
                self.assertIsInstance(owner, change_routing.UnroutedChange)
            configured.assert_called_once_with()
        self.assertEqual(cast(mock.Mock, self.alert).call_count, 3)

    def test_a_fixture_list_outside_the_real_folder_sends_no_alert_and_writes_no_record(self) -> None:
        with mock.patch.object(unrouted_alert, "send_message") as send:
            owner = self.router().owner({"#": "C9", "Producer": "unknown"},
                                        self.root / "change-review - top.md")
        self.assertIsInstance(owner, change_routing.UnroutedChange)
        cast(mock.Mock, self.alert).assert_not_called()
        send.assert_not_called()
        self.assertFalse((self.root / "links/.review-link-unrouted").exists())
        self.assertFalse((self.root / "links").exists())

    def test_the_reader_environment_override_never_makes_a_fixture_list_real(self) -> None:
        with (mock.patch.dict(os.environ, {"CHANGE_REVIEW_DIR": str(self.root)}),
              mock.patch.object(unrouted_alert, "send_message") as send):
            self.assertEqual(change_routing.lists_directory(), self.root)
            owner = self.router().owner({"#": "C9", "Producer": "unknown"},
                                        self.root / "change-review - top.md")
        self.assertIsInstance(owner, change_routing.UnroutedChange)
        cast(mock.Mock, self.alert).assert_not_called()
        send.assert_not_called()
        self.assertFalse((self.root / "links/.review-link-unrouted").exists())
        self.assertFalse((self.root / "links").exists())

    def test_patching_only_the_review_reader_folder_sends_no_alert(self) -> None:
        with (mock.patch.object(change_review, "lists_directory", return_value=self.root),
              mock.patch.object(unrouted_alert, "send_message") as send):
            owner = self.router().owner({"#": "C9", "Producer": "unknown"},
                                        self.root / "change-review - top.md")
        self.assertIsInstance(owner, change_routing.UnroutedChange)
        cast(mock.Mock, self.alert).assert_not_called()
        send.assert_not_called()
        self.assertFalse((self.root / "links/.review-link-unrouted").exists())

    def test_renamed_and_retired_units_keep_their_producer_route(self) -> None:
        row = {"#": "C1", "Producer": self.unit.id}
        before = self.router().owner(row, self.root / "change-review - top.md")
        _ = staff.rename(self.unit.id, "renamed")
        _ = staff.set_worktree(self.unit.id, staff.RemovedWorktree(remote.local_machine(), self.root / "builder"),
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
        _ = staff.set_doc(self.child.id, staff.LiveDoc(remote.local_machine(), doc))
        listing = self.root / "change-review - missing.md"
        assignment = self.router().owner_and_production(listing, {"#": "C1", "Producer": self.unit.id})
        self.assertEqual(assignment.production, doc)
        self.assertEqual(assignment.owner.tab, "child")
        _ = staff.set_doc(self.top.id, staff.LiveDoc(remote.local_machine(), doc))
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
        _ = staff.set_doc(self.child.id, staff.LiveDoc(remote.local_machine(), self.root / "missing.md"))
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

    def test_a_showrunner_without_either_document_routes_to_the_root_when_available(self) -> None:
        listing = self.root / "change-review - top.md"
        doc = self.root / "top-production.md"
        _ = doc.write_text("# Top")
        _ = staff.set_doc(self.top.id, staff.LiveDoc(remote.local_machine(), doc))
        config = self.root / "config"
        config.mkdir()
        _ = (config / "review_links.conf").write_text("unrouted_alert_recipient=top\n")
        self.enterContext(mock.patch.dict(os.environ, {"CONF_DIR": str(config)}))
        row = {"#": "C1", "Producer": self.unit.id}
        assignment = self.router().owner_and_production(listing, row)
        self.assertEqual(assignment.production, doc)
        self.assertEqual(assignment.owner, change_routing.ShowrunnerOwnChange(self.top.id, "top"))
        doc.unlink()
        assignment = self.router().owner_and_production(listing, row)
        self.assertEqual(assignment.production, change_routing.UnavailableProduction(listing))
        self.assertEqual(assignment.owner, change_routing.StaffRoutedChange(self.unit.id, self.child.id, "child"))

    def test_an_orphan_list_routes_to_the_configured_root_with_an_available_document(self) -> None:
        listing = self.root / "change-review - orphan.md"
        doc = self.root / "top-production.md"
        _ = doc.write_text("# Top")
        _ = staff.set_doc(self.top.id, staff.LiveDoc(remote.local_machine(), doc))
        config = self.root / "config"
        config.mkdir()
        setting = config / "review_links.conf"
        _ = setting.write_text("unrouted_alert_recipient=top\n")
        self.enterContext(mock.patch.dict(os.environ, {"CONF_DIR": str(config)}))
        router = self.router()
        row = {"#": "C1"}
        original = change_routing.ShowrunnerOwnChange(change_routing.MissingShowrunner("orphan"), "orphan")
        expected = change_routing.ShowrunnerOwnChange(self.top.id, "top")
        with mock.patch.object(conf, "read", wraps=conf.read) as reads:
            for number in range(3):
                orphan = {"#": f"C{number + 1}"}
                assignment = router.owner_and_production(listing, orphan)
                self.assertEqual(assignment.owner, expected)
                self.assertEqual(assignment.production, doc)
                self.assertEqual(router.owner(orphan, listing), assignment.owner)
            reads.assert_called_once_with("review_links")
        normal = self.router()
        with mock.patch.object(conf, "read", wraps=conf.read) as reads:
            assignment = normal.owner_and_production(self.root / "change-review - top.md", row)
            self.assertEqual(assignment.production, doc)
            self.assertEqual(assignment.owner, change_routing.ShowrunnerOwnChange(self.top.id, "top"))
            self.assertEqual(normal.owner(row, self.root / "change-review - top.md"), assignment.owner)
            reads.assert_not_called()
        _ = listing.write_text(f'---\nproductions:\n  - "{doc}"\n---\n')
        documents = change_routing.production_documents({}, [("orphan", listing.read_text())])
        stamped = change_routing.StaffRouting(staff.snapshot(), documents)
        assignment = stamped.owner_and_production(listing, row)
        self.assertEqual(assignment, change_routing.ChangeAssignment(expected, doc))
        self.assertEqual(stamped.owner(row, listing), assignment.owner)
        _ = setting.write_text("unrouted_alert_recipient=missing\n")
        self.assertEqual(router.owner_and_production(listing, row).owner, expected)
        assignment = self.router().owner_and_production(listing, row)
        self.assertEqual(assignment, change_routing.ChangeAssignment(original, change_routing.UnavailableProduction(listing)))
        _ = setting.write_text("unrouted_alert_recipient=top\n")
        doc.unlink()
        assignment = self.router().owner_and_production(listing, row)
        self.assertEqual(assignment, change_routing.ChangeAssignment(original, change_routing.UnavailableProduction(listing)))

    def test_an_orphan_list_keeps_an_unknown_producer_unrouted_with_the_root_document(self) -> None:
        self.enterContext(production_staff_record())
        _ = self.enterContext(mock.patch.object(change_routing, "configured_lists_directory", return_value=self.root))
        listing = self.root / "change-review - orphan.md"
        identity = fixture_runtime_id("missing-producer")
        row = {"#": "C777", "Producer": identity}
        doc = self.configured_root_document()
        assignment = self.router().owner_and_production(listing, row)
        self.assertIsInstance(assignment.owner, change_routing.UnroutedChange)
        assert isinstance(assignment.owner, change_routing.UnroutedChange)
        self.assertIn(identity, assignment.owner.note)
        self.assertEqual(assignment.owner.tab, "unrouted")
        self.assertEqual(assignment.owner.showrunner_recipients, ())
        self.assertEqual(assignment.production, doc)
        cast(mock.Mock, self.alert).assert_called_once_with(self.root / "links", "orphan", row,
                                                          f"No staff record for {identity}", (identity,))

    def test_an_orphan_list_keeps_a_broken_parent_unrouted_with_the_root_document(self) -> None:
        doc = self.configured_root_document()
        record = staff.snapshot()
        broken = replace(record, rows=tuple(replace(row, parent=staff.StaffRoot()) if row.id == self.unit.id else row
                                            for row in record.rows))
        assignment = self.router(broken).owner_and_production(self.root / "change-review - orphan.md",
                                                              {"#": "C1", "Producer": self.unit.id})
        self.assertIsInstance(assignment.owner, change_routing.UnroutedChange)
        assert isinstance(assignment.owner, change_routing.UnroutedChange)
        self.assertIn("No staff showrunner", assignment.owner.note)
        self.assertEqual(assignment.production, doc)

    def test_an_unrouted_orphan_keeps_its_owner_when_the_root_document_disappears(self) -> None:
        doc = self.configured_root_document()
        listing = self.root / "change-review - orphan.md"
        row = {"#": "C1", "Producer": fixture_runtime_id("missing-producer")}
        available = self.router().owner_and_production(listing, row)
        self.assertEqual(available.production, doc)
        doc.unlink()
        unavailable = self.router().owner_and_production(listing, row)
        self.assertEqual(unavailable.owner, available.owner)
        self.assertIsInstance(unavailable.owner, change_routing.UnroutedChange)
        self.assertEqual(unavailable.production, change_routing.UnavailableProduction(listing))

    def test_the_page_and_footer_count_an_unrouted_orphan_under_unrouted(self) -> None:
        from scripts.production import footer_counts, showrunners
        from scripts.review_links import change_browser, change_page
        from scripts.production import change_review as cli_review

        doc = self.configured_root_document()
        listing = self.root / "change-review - orphan.md"
        _ = doc.write_text(f"# Top\n- **Change review:** `{listing}`\n")
        _ = listing.write_text("## To review\n\n| # | Change | Producer |\n| --- | --- | --- |\n"
                               + f"| C1 | Check the orphan change | {fixture_runtime_id('missing-producer')} |\n")
        with (mock.patch.object(change_review, "lists_directory", return_value=self.root),
              mock.patch.object(cli_review, "lists_directory", return_value=self.root),
              mock.patch.object(showrunners, "timer_docs", return_value={"top": doc})):
            prepared = change_browser.prepare(change_page.ReviewBrowser("secret"),
                                              change_page.DeliveryStatus(self.root / "links"))
            counts = {tab: sum(item.waiting and item.tab == tab for item in prepared.changes)
                      for tab in prepared.queues}
            self.assertEqual(counts["unrouted"], 1)
            self.assertEqual(counts["top"], 0)
            self.assertEqual(footer_counts.unrouted_changes(doc), counts["unrouted"])
            self.assertEqual(footer_counts.changes(doc), counts["top"])

    def test_an_existing_staff_document_wins_over_the_supplied_document(self) -> None:
        supplied, authoritative = self.root / "supplied.md", self.root / "staff.md"
        _ = supplied.write_text("# Supplied")
        _ = authoritative.write_text("# Staff")
        _ = staff.set_doc(self.child.id, staff.LiveDoc(remote.local_machine(), authoritative))
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
        from scripts.production import change_review as cli_review

        docs = {name: self.root / f"{name}-production.md" for name in ("top", "child")}
        listing = self.root / "change-review - top.md"
        for name, doc in docs.items():
            _ = doc.write_text(f"# {name}\n- **Change review:** `{listing}`\n")
        _ = staff.set_doc(self.top.id, staff.LiveDoc(remote.local_machine(), docs["top"]))
        _ = staff.set_doc(self.child.id, staff.LiveDoc(remote.local_machine(), docs["child"]))
        retired = fixture_member("unit", name="retired", parent=self.child.id,
                                worktree=staff.RemovedWorktree(remote.local_machine(), self.root / "removed"))
        _ = staff.rename(self.unit.id, "renamed")
        _ = change_review.append_rows(listing, [change_review.Visible(f"Change {i}", "Read", "Proof") for i in range(5)],
            date(2026, 11, 2), "", docs["top"],
            producers=[self.unit.id, change_review.UnknownProducer(), staff.StaffId(fixture_runtime_id("missing-producer")), retired.id, self.child.id], record=staff.snapshot())
        with (mock.patch.object(change_review, "lists_directory", return_value=self.root), mock.patch.object(cli_review, "lists_directory", return_value=self.root),
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

    def test_delivery_readers_keep_default_fallbacks_and_require_complete_collected_reads(self) -> None:
        listing = self.root / "change-review - top.md"
        row = {"#": "C1"}
        path = change_routing.delivery_path(self.root, listing, "C1")
        read_text = Path.read_text
        with mock.patch.dict(os.environ, {"REVIEW_LINKS_ROOT": str(self.root)}):
            for failure in ("unreadable", "invalid JSON", "invalid encoding"):
                with self.subTest(failure=failure):
                    _ = path.write_bytes(b"\xff" if failure == "invalid encoding" else b"{")

                    def read(source: Path, encoding: str | None = None, errors: str | None = None) -> str:
                        if source == path and failure == "unreadable":
                            raise PermissionError(str(path))
                        return read_text(source, encoding=encoding, errors=errors)

                    with mock.patch.object(Path, "read_text", read):
                        if failure != "invalid encoding":
                            self.assertTrue(change_routing.waits_for_user(listing, row, self.root))
                            self.assertEqual(change_routing.waiting_rows(listing, [row]), [row])
                        for single in (False, True):
                            sources: list[Path] = []
                            with self.assertRaises((OSError, ValueError)):
                                if single:
                                    _ = change_routing.waits_for_user(listing, row, self.root, sources=sources)
                                else:
                                    _ = change_routing.waiting_rows(listing, [row], sources=sources)
                            self.assertIn(path, sources)
            path.unlink()
            sources = []
            self.assertTrue(change_routing.waits_for_user(listing, row, self.root, sources=sources))
            self.assertIn(path, sources)

    def test_collected_legacy_document_reads_fail_while_default_readers_skip_them(self) -> None:
        doc = self.root / "child-production.md"
        listing = self.root / "legacy.md"
        _ = doc.write_text(f"- **Change review:** {listing}\n")
        read_text = Path.read_text

        def read(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
            if path == doc:
                raise PermissionError(str(doc))
            return read_text(path, encoding=encoding, errors=errors)

        with mock.patch.object(Path, "read_text", read):
            record = staff.snapshot()
            self.assertEqual(change_routing.StaffRouting(record, {"child": doc}).list_owner(listing), "legacy")
            sources: list[Path] = []
            with self.assertRaises(PermissionError):
                _ = change_routing.StaffRouting(record, {"child": doc}, sources=sources).list_owner(listing)
            self.assertIn(doc, sources)

    def test_missing_optional_source_folders_are_empty_and_unreadable_folders_raise(self) -> None:
        from scripts.production.issues import source_paths

        folder = self.root / "missing"
        sources: list[Path] = []
        self.assertEqual(source_paths(folder, "*.md", sources=sources), [])
        self.assertEqual(sources, [folder])
        with mock.patch.object(Path, "iterdir", side_effect=PermissionError(str(folder))):
            with self.assertRaises(PermissionError):
                _ = source_paths(folder, "*.md", sources=sources)

    def test_collected_routing_does_not_read_or_send_alerts(self) -> None:
        self.enterContext(production_staff_record())
        sources: list[Path] = []
        router = change_routing.StaffRouting(staff.snapshot(), {}, sources=sources)
        owner = router.owner({"#": "C1", "Producer": "missing"}, self.root / "change-review - top.md")
        self.assertIsInstance(owner, change_routing.UnroutedChange)
        cast(mock.Mock, self.alert).assert_not_called()


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
            config_env = config_environment(config)
            _ = write_config_fixture(config / "scratch.conf",
                                     f"visible_root={root / 'repro'}\nholds_file={root / 'holds.json'}")
            settings = Path(__file__).resolve().parents[2] / "config/change_review.conf"
            _ = (config / "change_review.conf").write_text(settings.read_text())
            source = Path(__file__).resolve().parents[2] / "config/review_links.conf"
            _ = (config / "review_links.conf").write_text(source.read_text().replace("delivery_pause_seconds=5", "delivery_pause_seconds=0"))
            environment = {**config_env, "STAFF_FILE": str(root / "staff.json"), "CHANGE_REVIEW_DIR": str(root),
                           "REVIEW_LINKS_ROOT": str(root / "links"),
                           "NEXT_STATE_DIR": str(root / "next"), "FEATURE_LOG_VAULT": str(root / "vault")}
            with mock.patch.dict(os.environ, environment):
                listing = root / "change-review - natedev.md"
                _ = listing.write_text("## To review\n\n| # | Change | Production |\n| --- | --- | --- |\n| C1 | Known | natedev |\n")
                docs = {name: root / f"{name}-production.md" for name in ("natedev", "builds")}
                producer_rows(root, docs, {listing: "natedev"})
                written = change_review.append_rows(listing, [change_review.Visible("Unknown AWS change", "Read", "Proof")],
                                               date(2026, 11, 2), "e41fa2a4", docs["builds"], record=staff.snapshot())
                _ = listing.write_text(listing.read_text().replace("| C2 |", "| C777 |"))
                runner = staff.snapshot().by_path("builds")
                parent = staff.snapshot().by_path("natedev")
                assert runner is not None and parent is not None
                _ = listing.write_text(listing.read_text().replace(
                    written[0].replace("| C2 |", "| C777 |"),
                    written[0].replace("| C2 |", "| C777 |").replace(str(runner.id), "builds")))
                _ = staff.move(runner.id, staff.ReportsTo(parent.id))
                from scripts.production import change_review as cli_review
                with (mock.patch.object(change_review, "lists_directory", return_value=root), mock.patch.object(cli_review, "lists_directory", return_value=root),
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

        _ = self.merge()
        merge_gate.record_changes(self.repo, self.listing, self.doc, "main")
        row = change_review.pending(self.listing)[0]
        owner = change_routing.StaffRouting(staff.snapshot(), {}).owner(row, self.listing)
        self.assertIsInstance(owner, change_routing.StaffRoutedChange)
        assert isinstance(owner, change_routing.StaffRoutedChange)
        self.assertEqual(owner.unit_id, self.unit.id)
        self.assertEqual(change_page.verdict_recipients(owner, "not works"), (self.unit.id, owner.showrunner_id))


class ChangeReviewConfigTests(unittest.TestCase):
    def test_delivery_root_reads_the_folder_setting_at_use(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = root / "change_review.conf"
            with mock.patch.dict(os.environ, {"CONF_DIR": str(root), "REVIEW_LINKS_ROOT": ""}):
                _ = settings.write_text(f"review_links_directory={root / 'first'}\n")
                self.assertEqual(change_routing.review_root(), root / "first")
                _ = settings.write_text(f"review_links_directory={root / 'second'}\n")
                self.assertEqual(change_routing.review_root(), root / "second")

    def test_config_keeps_the_existing_values(self) -> None:
        from scripts.lib import conf

        settings = conf.read(Path(__file__).resolve().parents[2] / "config/change_review.conf")
        for key, expected in {'lists_directory': '~/rust/hanadocs/change-review', 'review_links_directory': '~/.local/share/review-links'}.items():
            with self.subTest(key=key):
                self.assertEqual(settings.text(key), expected)
