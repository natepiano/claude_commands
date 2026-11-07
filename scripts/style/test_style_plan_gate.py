"""Gate exit statuses, formatter protocols and coverage verdicts."""

from __future__ import annotations

from scripts.style.ledger_test_support import ticket_owner_id
from scripts.production.cli_fixture import process_boundary

import io
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from typing import cast, final
from unittest import mock


from scripts.style import style_plan
from scripts.style import style_proposals
from scripts.production import proposal_issue
from scripts.production.issues import frontmatter

from scripts.lib import berth_board
from scripts.style import fake_fmt
from scripts.style import rules
from scripts.style import style_ledger

from scripts.style.style_plan_test_support import StylePlanFixture, unit_staff_id


@final
class StylePlanTests(StylePlanFixture):
    def test_gate_and_checkpoint_use_only_current_parameter_blockers(self) -> None:
        from scripts.style.test_approved_renames import PARAMETER_CHANGES
        for label, source, current, blocked in PARAMETER_CHANGES:
            with self.subTest(parameter=label):
                case = StylePlanTests()
                root = Path(case.enterContext(tempfile.TemporaryDirectory())) / "case"
                _ = case.enterContext(mock.patch.object(StylePlanTests, "fixture_root", root))
                case.setUp()
                try:
                    ticket, ledger, finding, issue, path = case.outer_binding_ticket(source)
                    case.write(path, current)
                    build_base = case.commit()
                    with closing(sqlite3.connect(ledger.database)) as connection, connection:
                        _ = connection.execute("UPDATE tickets SET base=? WHERE id=?", (build_base, ticket))
                    case.write(path, current.replace("|g| g", "|checkout_info| checkout_info"))
                    first = case.reference_gate(ticket)
                    if blocked:
                        self.assertEqual(first[0], 1, first)
                        self.assertIn("blocked by outer binding", first[1])
                    else:
                        self.assertEqual(first, (0, "approved proposal\n"))
                    before = ledger.database.read_bytes()
                    self.assertEqual(case.reference_gate(ticket), first)
                    self.assertEqual(ledger.database.read_bytes(), before)
                    checkpoint = case.commit()
                    ledger = case.ledger()
                    ledger.set_tip(checkpoint, "main")
                    ledger.ticket_checkpoint(ticket, checkpoint)
                    refused = ledger.ticket_merge(ticket)
                    reopened, = case.ledger().proposal_findings(issue)
                    self.assertEqual(reopened.finding_id, finding.finding_id)
                    if blocked:
                        self.assertEqual(len(refused), 1)
                        self.assertIsInstance(refused[0].cause, style_ledger.OuterBindingBlocked)
                        self.assertEqual(reopened.state, style_ledger.Filed(issue))
                    else:
                        self.assertEqual(refused, ())
                        self.assertIsInstance(reopened.state, style_ledger.Built)
                finally:
                    case.tearDown()
                    case.doCleanups()

    def test_unread_local_body_refuses_gate_and_checkpoint_after_a_signature_change(self) -> None:
        from scripts.style.test_approved_renames import UNREAD_LOCAL_BODIES
        for label, source in UNREAD_LOCAL_BODIES:
            with self.subTest(body=label):
                case = StylePlanTests()
                root = Path(case.enterContext(tempfile.TemporaryDirectory())) / "case"
                _ = case.enterContext(mock.patch.object(StylePlanTests, "fixture_root", root))
                case.setUp()
                try:
                    ticket, ledger, finding, issue, path = case.outer_binding_ticket(source)
                    current = source.replace("label()", "label(extra: usize)").replace("|g| g", "|checkout_info| checkout_info")
                    case.write(path, current)
                    first = case.reference_gate(ticket)
                    self.assertEqual(first[0], 1, first)
                    self.assertIn("containing item body unread", first[1])
                    before = ledger.database.read_bytes()
                    self.assertEqual(case.reference_gate(ticket), first)
                    self.assertEqual(ledger.database.read_bytes(), before)
                    checkpoint = case.commit()
                    ledger = case.ledger()
                    ledger.set_tip(checkpoint, "main")
                    ledger.ticket_checkpoint(ticket, checkpoint)
                    refused = ledger.ticket_merge(ticket)
                    self.assertEqual(len(refused), 1)
                    self.assertIsInstance(refused[0].cause, style_ledger.OuterBindingUnproved)
                    reopened, = case.ledger().proposal_findings(issue)
                    self.assertEqual(reopened.finding_id, finding.finding_id)
                    self.assertEqual(reopened.state, style_ledger.Filed(issue))
                    self.assertIsInstance(reopened.rename_disposition, style_ledger.OuterBindingUnproved)
                finally:
                    case.tearDown()
                    case.doCleanups()

    def test_proved_item_with_unread_body_passes_gate_and_checkpoint(self) -> None:
        from scripts.style.test_approved_renames import UNREAD_ITEM_BODIES
        for label, source in UNREAD_ITEM_BODIES:
            with self.subTest(body=label):
                case = StylePlanTests()
                root = Path(case.enterContext(tempfile.TemporaryDirectory())) / "case"
                _ = case.enterContext(mock.patch.object(StylePlanTests, "fixture_root", root))
                case.setUp()
                try:
                    ticket, ledger, finding, issue, path = case.outer_binding_ticket(source)
                    case.write(path, source.replace("fn g(", "fn checkout_info("))
                    self.assertEqual(case.reference_gate(ticket), (0, "approved proposal\n"))
                    checkpoint = case.commit()
                    ledger = case.ledger()
                    ledger.set_tip(checkpoint, "main")
                    ledger.ticket_checkpoint(ticket, checkpoint)
                    refused = ledger.ticket_merge(ticket)
                    self.assertFalse(any(isinstance(row.cause, style_ledger.OuterBindingBlocked | style_ledger.OuterBindingUnproved)
                                         for row in refused), refused)
                    current, = case.ledger().proposal_findings(issue)
                    self.assertEqual(current.finding_id, finding.finding_id)
                    self.assertNotIsInstance(current.rename_disposition, style_ledger.OuterBindingBlocked | style_ledger.OuterBindingUnproved)
                finally:
                    case.tearDown()
                    case.doCleanups()

    def test_generic_parameters_refuse_gate_and_checkpoint(self) -> None:
        from scripts.style.test_approved_renames import GENERIC_PARAMETER_BINDINGS
        self.assert_local_declarations_refuse_gate_and_checkpoint(GENERIC_PARAMETER_BINDINGS)

    def assert_local_declarations_refuse_gate_and_checkpoint(self, forms: tuple[tuple[str, str], ...]) -> None:
        for label, source in forms:
            with self.subTest(form=label):
                case = StylePlanTests()
                root = Path(case.enterContext(tempfile.TemporaryDirectory())) / "case"
                _ = case.enterContext(mock.patch.object(StylePlanTests, "fixture_root", root))
                case.setUp()
                try:
                    ticket, ledger, finding, issue, path = case.outer_binding_ticket(source)
                    with closing(sqlite3.connect(ledger.database)) as connection, connection:
                        _ = connection.execute("UPDATE tickets SET base='' WHERE id=?", (ticket,))
                    first = case.reference_gate(ticket)
                    self.assertEqual(first[0], 1, first)
                    before = ledger.database.read_bytes()
                    self.assertEqual(case.reference_gate(ticket), first)
                    self.assertEqual(ledger.database.read_bytes(), before)
                    case.write(path, re.sub(r"\bg\b", "checkout_info", source))
                    checkpoint = case.commit()
                    ledger = case.ledger()
                    ledger.set_tip(checkpoint, "main")
                    ledger.ticket_checkpoint(ticket, checkpoint)
                    refused = ledger.ticket_merge(ticket)
                    self.assertEqual(len(refused), 1)
                    self.assertIsInstance(refused[0].cause, style_ledger.OuterBindingBlocked | style_ledger.OuterBindingUnproved)
                    current, = case.ledger().proposal_findings(issue)
                    self.assertEqual(current.finding_id, finding.finding_id)
                    self.assertEqual(current.state, style_ledger.Filed(issue))
                finally:
                    case.tearDown()
                    case.doCleanups()

    def test_field_expression_bindings_refuse_gate_and_checkpoint(self) -> None:
        from scripts.style.test_approved_renames import FIELD_EXPRESSION_BINDINGS
        self.assert_local_declarations_refuse_gate_and_checkpoint(FIELD_EXPRESSION_BINDINGS)

    def test_gate_anchor_matrix_never_adopts_a_build_coordinate_or_current_sibling(self) -> None:
        from scripts.style.test_approved_renames import IDENTICAL_BINDING_ITEMS
        for probe in ("missing_anchor", "later_field", "removed", "raw_pair"):
            with self.subTest(probe=probe):
                case = StylePlanTests()
                root = Path(case.enterContext(tempfile.TemporaryDirectory())) / "case"
                _ = case.enterContext(mock.patch.object(StylePlanTests, "fixture_root", root))
                case.setUp()
                try:
                    source = IDENTICAL_BINDING_ITEMS
                    if probe == "raw_pair":
                        source = source.replace("|g| g", "|r#g| r#g", 1)
                    ticket, ledger, finding, issue, path = case.outer_binding_ticket(source)
                    current = source.replace("first()", "first(checkout_info: usize)")
                    comparison = ""
                    if probe == "later_field":
                        case.write(path, "struct Other {\n    g: usize,\n}\n")
                        comparison = case.commit()
                    if probe == "removed":
                        current = source.replace("map(|g| g + 1)", "consume(42)", 1)
                    if probe == "missing_anchor":
                        current = source[source.index("fn second"):]+source[:source.index("fn second")]
                    with closing(sqlite3.connect(ledger.database)) as connection, connection:
                        _ = connection.execute("UPDATE tickets SET base=? WHERE id=?", (comparison, ticket))
                        if probe == "missing_anchor":
                            _ = connection.execute("UPDATE held_findings SET anchor_commit='',ticket='missing-judgment' WHERE finding_id=?", (finding.finding_id,))
                        if probe == "raw_pair":
                            _ = connection.execute("UPDATE held_findings SET before='r#g' WHERE finding_id=?", (finding.finding_id,))
                    case.write(path, current)
                    first = case.reference_gate(ticket)
                    self.assertEqual(first[0], 1, first)
                    before = ledger.database.read_bytes()
                    self.assertEqual(case.reference_gate(ticket), first)
                    self.assertEqual(ledger.database.read_bytes(), before)
                    checkpoint = case.commit()
                    ledger = case.ledger()
                    ledger.set_tip(checkpoint, "main")
                    ledger.ticket_checkpoint(ticket, checkpoint)
                    refused = ledger.ticket_merge(ticket)
                    self.assertEqual(len(refused), 1)
                    current_finding, = case.ledger().proposal_findings(issue)
                    self.assertEqual(current_finding.state, style_ledger.Filed(issue))
                    self.assertIsInstance(current_finding.rename_disposition, style_ledger.OuterBindingBlocked | style_ledger.OuterBindingUnproved)
                finally:
                    case.tearDown()
                    case.doCleanups()

    def test_gate_and_checkpoint_cannot_admit_an_identical_sibling_instead_of_the_recorded_item(self) -> None:
        from scripts.style.test_approved_renames import IDENTICAL_BINDING_ITEMS
        source = IDENTICAL_BINDING_ITEMS
        ticket, ledger, finding, issue, path = self.outer_binding_ticket(source)
        edited = source.replace("first()", "first(checkout_info: usize)").replace("|g| g", "|checkout_info| checkout_info", 1)
        self.write(path, edited)
        code, output = self.reference_gate(ticket)
        self.assertEqual(code, 1, output)
        self.assertIn("rename blocked by outer binding 'checkout_info' at line 1", output)
        checkpoint = self.commit()
        ledger = self.ledger()
        ledger.set_tip(checkpoint, "main")
        ledger.ticket_checkpoint(ticket, checkpoint)
        refused = ledger.ticket_merge(ticket)
        self.assertEqual(len(refused), 1)
        self.assertIsInstance(refused[0].cause, style_ledger.OuterBindingBlocked)
        self.assertEqual(ledger.proposal_findings(issue)[0].finding_id, finding.finding_id)
        self.assertEqual(ledger.proposal_findings(issue)[0].state, style_ledger.Filed(issue))

    def test_gate_and_checkpoint_refuse_a_duplicate_recorded_item_path(self) -> None:
        from scripts.style.test_approved_renames import DUPLICATE_BINDING_ITEM_PATH
        source = DUPLICATE_BINDING_ITEM_PATH
        ticket, ledger, finding, issue, path = self.outer_binding_ticket(source)
        self.write(path, source.replace("|g| g", "|checkout_info| checkout_info", 1))
        code, output = self.reference_gate(ticket)
        self.assertEqual(code, 1, output)
        self.assertIn("recorded item path is not unique", output)
        checkpoint = self.commit()
        ledger = self.ledger()
        ledger.set_tip(checkpoint, "main")
        ledger.ticket_checkpoint(ticket, checkpoint)
        refused = ledger.ticket_merge(ticket)
        self.assertEqual(len(refused), 1)
        self.assertIsInstance(refused[0].cause, style_ledger.OuterBindingUnproved)
        self.assertEqual(ledger.proposal_findings(issue)[0].finding_id, finding.finding_id)
        self.assertEqual(ledger.proposal_findings(issue)[0].state, style_ledger.Filed(issue))

    def test_gate_and_checkpoint_accept_a_precisely_proved_format_capture_rename(self) -> None:
        from scripts.style.test_approved_renames import CAPTURE_BINDING_ITEM
        ticket, ledger, finding, issue, path = self.outer_binding_ticket(CAPTURE_BINDING_ITEM)
        edited = CAPTURE_BINDING_ITEM.replace("g =", "checkout_info =").replace("{g}", "{checkout_info}")
        self.write(path, edited)
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))
        checkpoint = self.commit()
        ledger = self.ledger()
        ledger.set_tip(checkpoint, "main")
        ledger.ticket_checkpoint(ticket, checkpoint)
        self.assertEqual(ledger.ticket_merge(ticket), ())
        self.assertEqual(ledger.proposal_findings(issue)[0].finding_id, finding.finding_id)
        self.assertIsInstance(ledger.proposal_findings(issue)[0].state, style_ledger.Built)

    def test_gate_and_checkpoint_read_a_later_outer_binding_from_the_current_item(self) -> None:
        source = "fn label() {\n    map(|g| g + 1)\n}\n"
        ticket, ledger, finding, issue, path = self.outer_binding_ticket(source)
        edited = source.replace("label()", "label(checkout_info: usize)").replace("|g| g", "|checkout_info| checkout_info")
        self.write(path, edited)
        first = self.reference_gate(ticket)
        self.assertEqual(first, (1, f"review: {finding.finding_id}: rename blocked by outer binding "
                                   + f"'checkout_info' at line 1 in {path}\n"))
        self.assertEqual(self.reference_gate(ticket), first)
        checkpoint = self.commit()
        ledger = self.ledger()
        ledger.set_tip(checkpoint, "main")
        ledger.ticket_checkpoint(ticket, checkpoint)
        refused = ledger.ticket_merge(ticket)
        self.assertEqual(len(refused), 1)
        self.assertIsInstance(refused[0].cause, style_ledger.OuterBindingBlocked)
        self.assertEqual(ledger.proposal_findings(issue)[0].state, style_ledger.Filed(issue))

    def test_gate_and_checkpoint_refuse_an_impl_inner_attribute(self) -> None:
        self.assert_inner_attribute_refuses_gate_and_checkpoint("impl")

    def test_gate_and_checkpoint_refuse_a_trait_inner_attribute(self) -> None:
        self.assert_inner_attribute_refuses_gate_and_checkpoint("trait")

    def assert_inner_attribute_refuses_gate_and_checkpoint(self, kind: str) -> None:
        from scripts.style.test_approved_renames import INHERITED_INNER_ATTRIBUTES
        source = next(source for label, source in INHERITED_INNER_ATTRIBUTES if label == kind)
        ticket, ledger, finding, issue, path = self.outer_binding_ticket(source)
        self.write(path, source.replace("|g| g", "|checkout_info| checkout_info"))
        code, output = self.reference_gate(ticket)
        self.assertEqual(code, 1, output)
        self.assertIn("unresolved attribute macro 'inject'", output)
        checkpoint = self.commit()
        ledger = self.ledger()
        ledger.set_tip(checkpoint, "main")
        ledger.ticket_checkpoint(ticket, checkpoint)
        refused = ledger.ticket_merge(ticket)
        self.assertEqual(len(refused), 1)
        self.assertIsInstance(refused[0].cause, style_ledger.OuterBindingUnproved)
        self.assertEqual(ledger.proposal_findings(issue)[0].finding_id, finding.finding_id)
        self.assertEqual(ledger.proposal_findings(issue)[0].state, style_ledger.Filed(issue))

    def test_gate_and_checkpoint_keep_stale_local_admission_terminal(self) -> None:
        source = "fn label(checkout_info: usize) {\n    map(|g| g + 1)\n}\n"
        ticket, ledger, finding, issue, path = self.outer_binding_ticket(source)
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE held_findings SET text=? WHERE finding_id=?",
                                   (finding.text + " // stale judgment", finding.finding_id))
        first = self.reference_gate(ticket)
        self.assertEqual(first, (1, f"review: {finding.finding_id}: rename blocked by outer binding "
                                    + f"'checkout_info' at line 1 in {path}\n"))
        self.assertEqual(self.reference_gate(ticket), first)
        ledger = self.ledger()
        current, = ledger.proposal_findings(issue)
        self.assertIsInstance(current.rename_disposition, style_ledger.OuterBindingBlocked)
        assert isinstance(current.rename_disposition, style_ledger.OuterBindingBlocked)
        self.assertEqual(current.state, style_ledger.Filed(issue))
        checkpoint = self.git("rev-parse", "HEAD")
        ledger.set_tip(checkpoint, "main")
        ledger.ticket_checkpoint(ticket, checkpoint)
        self.assertEqual(ledger.ticket_merge(ticket), (
            style_ledger.FindingNotBuilt(current.finding_id, current.rename_disposition),))
        self.assertEqual(ledger.proposal_findings(issue)[0].state, style_ledger.Filed(issue))

    def test_gate_refuses_an_inherited_inner_attribute_after_the_rename(self) -> None:
        source = "mod outer { #![inject] fn label() {\n    map(|g| g + 1)\n} }\n"
        ticket, ledger, finding, issue, path = self.outer_binding_ticket(source)
        self.write(path, source.replace("|g| g", "|checkout_info| checkout_info"))
        code, output = self.reference_gate(ticket)
        self.assertEqual(code, 1, output)
        self.assertIn("unresolved attribute macro 'inject'", output)
        current, = ledger.proposal_findings(issue)
        self.assertIsInstance(current.rename_disposition, style_ledger.OuterBindingUnproved)
        self.assertEqual(current.state, style_ledger.Filed(issue))
        self.assertEqual(current.finding_id, finding.finding_id)

    def test_gate_records_the_outer_binding_block_and_repeating_it_changes_nothing(self) -> None:
        source = "fn label(checkout_info: usize) -> usize {\n    map(|g| g + 1)\n}\n"
        ticket, ledger, finding, issue, path = self.outer_binding_ticket(source)
        first = self.reference_gate(ticket)
        self.assertEqual(first, (1, f"review: {finding.finding_id}: rename blocked by outer binding "
                                   + f"'checkout_info' at line 1 in {path}\n"))
        current, = ledger.proposal_findings(issue)
        self.assertIsInstance(current.rename_disposition, style_ledger.OuterBindingBlocked)
        self.assertEqual(current.state, style_ledger.Filed(issue))
        with closing(sqlite3.connect(ledger.database)) as connection:
            before = connection.execute("SELECT * FROM rename_outer_binding_blocks").fetchall()
        database = ledger.database.read_bytes()
        self.assertEqual(self.reference_gate(ticket), first)
        self.assertEqual(ledger.database.read_bytes(), database)
        with closing(sqlite3.connect(ledger.database)) as connection:
            self.assertEqual(connection.execute("SELECT * FROM rename_outer_binding_blocks").fetchall(), before)

    def test_gate_persists_an_unproved_attribute_outcome_and_is_idempotent(self) -> None:
        source = "#[inject] fn label() {\n    map(|g| g + 1)\n}\n"
        ticket, ledger, finding, issue, path = self.outer_binding_ticket(source)
        expected = (1, f"review: {finding.finding_id}: unresolved attribute macro 'inject' at line 1 in {path}\n")
        self.assertEqual(self.reference_gate(ticket), expected)
        current, = ledger.proposal_findings(issue)
        self.assertIsInstance(current.rename_disposition, style_ledger.OuterBindingUnproved)
        self.assertEqual(current.state, style_ledger.Filed(issue))
        before = ledger.database.read_bytes()
        self.assertEqual(self.reference_gate(ticket), expected)
        self.assertEqual(ledger.database.read_bytes(), before)
        self.assertEqual(self.ledger().named_held((finding.finding_id,))[0].rename_disposition, current.rename_disposition)

    def outer_binding_ticket(self, source: str) -> tuple[str, style_ledger.Ledger, style_ledger.HeldFinding, Path, str]:
        path = self.sources[0]
        self.write(path, source)
        self.commit_to_every_checkout()
        rule = self.rule("prefer-type-named-fields-and-bindings", "tier: C\nfamily: names\ninput_scope: file\n")
        issue = self.approved_decision(rule, (), "rename")
        ledger = self.ledger()
        blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule)
        ticket = ledger.next_ticket_id()
        judging = ledger.next_ticket_id()
        ledger.ticket_add(judging, ticket_owner_id("judging"), "read", "names", ".", [path], [rule])
        ledger.ticket_assign(judging, "main")
        _ = ledger.ticket_base(judging)
        finding = ledger.hold(rule, path, 2, source.splitlines()[1], "rename", "g", "checkout_info", blob, judging)
        ledger.file_held(finding.finding_id, issue)
        ledger.ticket_drop(judging)
        ledger.ticket_add(ticket, ticket_owner_id("names"), "proposal", "names", "crates/alpha", [path], [rule],
                          proposal=str(issue), finding_ids=(finding.finding_id,))
        ledger.ticket_assign(ticket, "main")
        _ = ledger.ticket_base(ticket)
        return ticket, ledger, finding, issue, path

    def approved_documentation_ticket(
        self, source: str = "//! The old module name.\n\n//! Other documentation.\nfn value() {}\n", line: int = 1,
    ) -> tuple[str, Path]:
        """A structure ticket reading an approved, filed documentation finding at its source line."""
        path = self.sources[0]
        self.write(path, source)
        self.commit_to_every_checkout()
        rule = self.rule("documented-module", "tier: D\nfamily: modules\ninput_scope: file\n")
        issue = self.approved_decision(rule, (), "restructure")
        ledger = self.ledger()
        ticket = "3001"
        ledger.ticket_add(ticket, ticket_owner_id("modules"), "structure", "modules", "crates/alpha", [path], [rule],
                          cells=((rule, path),))
        ledger.ticket_assign(ticket, "main")
        _ = ledger.ticket_base(ticket)
        blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule)
        finding = ledger.hold(rule, path, line, "The documentation names a removed module", "restructure",
                              "The old module name", "The current module name", blob, ticket)
        ledger.file_held(finding.finding_id, issue)
        ledger.record(rule=rule, path=path, commit=self.tip, verdict="held", detail="", ticket=ticket, worker="test",
                      rule_blob=blob, input_scope=style_ledger.DeclaredInputScope.RULE)
        return ticket, issue

    def test_approved_finding_doc_comment_edit_passes_and_repeated_gate_agrees(self) -> None:
        ticket, issue = self.approved_documentation_ticket()
        path = self.sources[0]
        _ = (self.repo / path).write_text((self.repo / path).read_text().replace("old module", "current module"))
        approval = issue.read_text()
        for actor in ("user", "rule global:rust/documented-module.md"):
            with self.subTest(approved_by=actor):
                _ = issue.write_text(approval.replace("plan: approved", f'plan: approved\napproved_by: "{actor}"'))
                first = self.reference_gate(ticket)
                self.assertEqual(first, (0, "mechanical\n"))
                self.assertEqual(self.reference_gate(ticket), first)

    def test_approved_doc_site_does_not_exempt_another_comment_in_the_same_file(self) -> None:
        ticket, _ = self.approved_documentation_ticket()
        target = self.repo / self.sources[0]
        _ = target.write_text(target.read_text().replace("old module", "current module"))
        self.assertEqual(self.reference_gate(ticket), (0, "mechanical\n"))
        _ = target.write_text(target.read_text().replace("Other documentation", "Changed unrelated documentation"))
        code, output = self.reference_gate(ticket)
        self.assertEqual(code, 1, output)
        self.assertIn("check 6: serialized, reflected, configured or documented name flagged", output)

    def test_doc_site_loses_its_exemption_when_approval_is_removed(self) -> None:
        ticket, issue = self.approved_documentation_ticket()
        target = self.repo / self.sources[0]
        _ = target.write_text(target.read_text().replace("old module", "current module"))
        self.assertEqual(self.reference_gate(ticket), (0, "mechanical\n"))
        _ = issue.write_text(re.sub(r"unit: [^\n]*\n", "", issue.read_text()))
        code, output = self.reference_gate(ticket)
        self.assertEqual(code, 1, output)
        self.assertIn("check 6: serialized, reflected, configured or documented name flagged", output)

    def test_approved_doc_site_does_not_exempt_a_serialized_name_change(self) -> None:
        ticket, _ = self.approved_documentation_ticket(
            '//! The old module name.\n\n#[serde(rename = "old")]\nstruct Value;\n')
        target = self.repo / self.sources[0]
        _ = target.write_text(target.read_text().replace("old module", "current module"))
        self.assertEqual(self.reference_gate(ticket), (0, "mechanical\n"))
        _ = target.write_text(target.read_text().replace('rename = "old"', 'rename = "new"'))
        code, output = self.reference_gate(ticket)
        self.assertEqual(code, 1, output)
        self.assertIn("check 6: serialized, reflected, configured or documented name flagged", output)

    def test_approved_doc_replacement_does_not_exempt_new_documentation_in_the_same_hunk(self) -> None:
        ticket, _ = self.approved_documentation_ticket("//! The old module name.\nfn value() {}\n")
        target = self.repo / self.sources[0]
        _ = target.write_text("//! The current module name.\n/// New documentation for the function.\nfn value() {}\n")
        code, output = self.reference_gate(ticket)
        self.assertEqual(code, 1, output)
        self.assertIn("check 6: serialized, reflected, configured or documented name flagged", output)

    def test_approved_doc_replacement_allows_an_adjacent_ordinary_comment_edit(self) -> None:
        ticket, _ = self.approved_documentation_ticket(
            "//! The old module name.\n// Old ordinary comment.\nfn value() {}\n")
        target = self.repo / self.sources[0]
        _ = target.write_text("//! The current module name.\n// Updated ordinary comment.\nfn value() {}\n")
        self.assertEqual(self.reference_gate(ticket), (0, "mechanical\n"))

    def test_approval_does_not_move_from_module_comment_to_function_documentation(self) -> None:
        ticket, _ = self.approved_documentation_ticket(
            "//! The old module name.\n// Old ordinary comment.\nfn value() {}\n")
        target = self.repo / self.sources[0]
        _ = target.write_text("// Updated ordinary comment.\n/// New function contract.\nfn value() {}\n")
        code, output = self.reference_gate(ticket)
        self.assertEqual(code, 1, output)
        self.assertIn("check 6: serialized, reflected, configured or documented name flagged", output)

    def test_approved_doc_edit_uses_its_position_among_duplicate_comments(self) -> None:
        ticket, _ = self.approved_documentation_ticket(
            "/// Same description.\nfn value() {}\n\n/// Same description.\nfn other() {}\n")
        target = self.repo / self.sources[0]
        _ = target.write_text(target.read_text().replace("/// Same description.", "/// Updated description.", 1))
        self.assertEqual(self.reference_gate(ticket), (0, "mechanical\n"))

    def test_approved_doc_edit_allows_an_ordinary_line_added_above_its_site(self) -> None:
        ticket, _ = self.approved_documentation_ticket(
            "// Old ordinary.\n//! Old module.\nfn value() {}\n", line=2)
        target = self.repo / self.sources[0]
        _ = target.write_text("// New ordinary.\n// Extra ordinary.\n//! New module.\nfn value() {}\n")
        self.assertEqual(self.reference_gate(ticket), (0, "mechanical\n"))

    def test_approved_doc_edit_allows_an_ordinary_line_removed_above_its_site(self) -> None:
        ticket, _ = self.approved_documentation_ticket(
            "// Old ordinary.\n//! Old module.\nfn value() {}\n", line=2)
        target = self.repo / self.sources[0]
        _ = target.write_text("//! New module.\nfn value() {}\n")
        self.assertEqual(self.reference_gate(ticket), (0, "mechanical\n"))

    def test_approved_block_doc_edit_ignores_ordinary_lines_but_not_an_extra_outer_doc(self) -> None:
        ticket, _ = self.approved_documentation_ticket(
            "// Old ordinary.\n/** Old module. */\nfn value() {}\n", line=2)
        target = self.repo / self.sources[0]
        replacement = "// New ordinary.\n// Extra ordinary.\n/** New module. */\n"
        _ = target.write_text(replacement + "fn value() {}\n")
        self.assertEqual(self.reference_gate(ticket), (0, "mechanical\n"))
        _ = target.write_text(replacement + "/// New function contract.\nfn value() {}\n")
        code, output = self.reference_gate(ticket)
        self.assertEqual(code, 1, output)
        self.assertIn("check 6: serialized, reflected, configured or documented name flagged", output)

    # Keep one gate case in a child interpreter with the shared queue fake.
    @process_boundary
    def test_structure_file_cells_gate_and_checkpoint_require_the_last_file_verdict(self) -> None:
        rule = self.rule("structure", "tier: D\nfamily: modules\n")
        second = self.rule("structure-other", "tier: D\nfamily: modules\n")
        self.sync()
        ledger = self.ledger()
        paths = self.sources[:2]
        ledger.ticket_add("3001", ticket_owner_id("modules"), "structure", "modules", "crates/alpha",
                          list(paths), [rule, second], cells=tuple((name, path) for name in (rule, second) for path in paths))
        ledger.ticket_assign("3001", "demo-style-names")
        _ = ledger.ticket_base("3001")
        for path in paths:
            self.record(second, path, self.tip, "3001")
        self.record(rule, paths[0], self.tip, "3001")
        result = subprocess.run([str(Path(__file__).resolve().parents[1] / "lib/py"), str(Path(style_plan.__file__)), "gate", "--repo", str(self.unit),
                                 "--ticket", "3001"], capture_output=True, text=True, check=False)
        self.assertEqual((result.returncode, result.stdout), (1, f"unjudged: {rule} {paths[1]}\n"))
        with self.assertRaisesRegex(style_ledger.LedgerError, f"unjudged cells: {rule} {paths[1]}"):
            ledger.ticket_checkpoint("3001", self.tip)
        self.record(rule, paths[1], self.tip, "3001")
        result = subprocess.run([str(Path(__file__).resolve().parents[1] / "lib/py"), str(Path(style_plan.__file__)), "gate", "--repo", str(self.unit),
                                 "--ticket", "3001"], capture_output=True, text=True, check=False)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "mechanical\n", ""))
        ledger.ticket_checkpoint("3001", self.tip)
        self.assertEqual(ledger.ticket_open(ticket_owner_id("modules"))[0]["state"], "checkpointed")

    def test_gate_reports_a_locked_ledger_as_an_error_and_a_rerun_gives_the_review(self) -> None:
        database = self.ledger().database
        _ = self.enterContext(mock.patch.object(style_ledger, "BUSY_TIMEOUT_SECONDS", 0.05))
        with closing(sqlite3.connect(database)) as holder:
            _ = holder.execute("BEGIN EXCLUSIVE")
            self.assertEqual(self.reference_gate("9"), (2, "error: database is locked\n"))
            holder.rollback()
        self.assertEqual(self.reference_gate("9"), (1, "review: unknown ticket 9\n"))

    def test_gate_passes_a_footprint_error_through_as_exit_two(self) -> None:
        rule = self.rule("structure", "tier: D\nfamily: modules\n")
        ledger = self.ledger()
        ledger.set_tip(self.tip)
        path = self.sources[0]
        ledger.ticket_add("3001", ticket_owner_id("modules"), "structure", "modules", "crates/alpha",
                          [path], [rule], cells=((rule, path),))
        ledger.ticket_assign("3001", "demo-style-names")
        _ = ledger.ticket_base("3001")
        self.record(rule, path, self.tip, "3001")
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE tickets SET base=? WHERE id=?", ("0" * 40, "3001"))
        result = subprocess.run([str(Path(__file__).resolve().parents[1] / "lib/py"), str(Path(style_plan.__file__)), "gate", "--repo", str(self.unit),
                                 "--ticket", "3001"], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertTrue(result.stdout.startswith("error: "), result.stdout)

    def test_coverage_gate_refuses_an_unformatted_changed_file(self) -> None:
        rule = self.rule("structure", "tier: D\nfamily: modules\n")
        ledger = self.ledger()
        ledger.set_tip(self.tip)
        path = self.sources[0]
        ledger.ticket_add("3001", ticket_owner_id("modules"), "structure", "modules", "crates/alpha",
                          [path], [rule], cells=((rule, path),))
        ledger.ticket_assign("3001", "demo-style-names")
        _ = ledger.ticket_base("3001")
        self.record(rule, path, self.tip, "3001")
        _ = (self.unit / path).write_text("pub fn value()  {}\n")
        self.unformatted(path)
        result = subprocess.run([str(Path(__file__).resolve().parents[1] / "lib/py"), str(Path(style_plan.__file__)), "gate", "--repo", str(self.unit),
                                 "--ticket", "3001"], capture_output=True, text=True, check=False)
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (1, f"mechanical\nreview: not formatted (rustfmt +nightly): {path}\n", ""))

    def test_structure_batches_cover_pending_files_once_keep_directories_and_isolate_large_files(self) -> None:
        rule = self.rule("structure", "tier: D\nfamily: modules\n")
        expected = set(self.sources[:2])
        for directory, count, lines in (("one", 4, 200), ("two", 4, 200), ("oversized", 8, 600)):
            for number in range(count):
                path = f"crates/alpha/src/{directory}/file{number}.rs"
                self.write(path, "// content\n" * lines)
                expected.add(path)
        large = "crates/alpha/src/large.rs"
        self.write(large, "// content\n" * 3001)
        expected.add(large)
        self.tip = self.commit()
        self.sync()
        batches = [ticket for ticket in self.candidates("modules") if ticket.crate == "crates/alpha"]
        self.assertGreater(len(batches), 1)
        paths = [path for ticket in batches for path in ticket.files]
        self.assertEqual(set(paths), expected)
        self.assertEqual(len(paths), len(expected))
        self.assertEqual([ticket.files for ticket in batches], sorted(ticket.files for ticket in batches))
        for ticket in batches:
            self.assertEqual(ticket.rules, (rule,))
            self.assertEqual(set(ticket.cells), {(rule, path) for path in ticket.files})
            if large in ticket.files:
                self.assertEqual(ticket.files, (large,))
            else:
                self.assertLessEqual(len(ticket.files), 6)
                self.assertLessEqual(sum(len(self.git("show", f"{self.tip}:{path}").splitlines())
                                         for path in ticket.files), 3000)
        for directory in ("one", "two"):
            self.assertEqual(sum(any(f"/{directory}/" in path for path in ticket.files) for ticket in batches), 1)

    def test_structure_batches_read_each_rules_listed_sites_once_per_crate_without_changing_tickets(self) -> None:
        first = self.rule("first", "tier: D\nfamily: modules\ncandidates:\n  kind: regex\n  pattern: first\n"
                          + "  surface: partial\n  uncovered: Read the module.\n")
        second = self.rule("second", "tier: D\nfamily: modules\ncandidates:\n  kind: regex\n  pattern: second\n"
                           + "  surface: partial\n  uncovered: Read the module.\n")
        self.write(self.sources[0], "pub fn first() {}\npub fn second() {}\n")
        for name in ("a", "b", "c", "d", "e", "f"):
            self.write(f"crates/alpha/src/nested/{name}.rs", "pub fn value() {}\n")
        self.write("crates/alpha/src/nested/a.rs", "pub fn second() {}\npub fn first() {}\n")
        self.tip = self.commit()
        self.sync()
        ledger = self.ledger()
        with mock.patch.object(ledger, "listed_sites", wraps=ledger.listed_sites) as listed_sites:
            tickets = style_plan.candidate_tickets(self.repo, ledger, style_plan.BusyFiles(self.board(), frozenset()),
                                                   "modules", base=self.base("modules"))
        self.assertEqual(tickets, (
            style_plan.TicketSelection("structure", "modules", "crates/alpha",
                ("crates/alpha/src/lib.rs", "crates/alpha/src/other.rs"), (first, second), sites=(
                    style_ledger.ListedSite(first, "crates/alpha/src/lib.rs", "crates/alpha", 1, "pub fn first() {}"),
                    style_ledger.ListedSite(second, "crates/alpha/src/lib.rs", "crates/alpha", 2, "pub fn second() {}"),
                ), cells=(
                    (first, "crates/alpha/src/lib.rs"), (first, "crates/alpha/src/other.rs"),
                    (second, "crates/alpha/src/lib.rs"), (second, "crates/alpha/src/other.rs"),
                )),
            style_plan.TicketSelection("structure", "modules", "crates/alpha", (
                "crates/alpha/src/nested/a.rs", "crates/alpha/src/nested/b.rs", "crates/alpha/src/nested/c.rs",
                "crates/alpha/src/nested/d.rs", "crates/alpha/src/nested/e.rs", "crates/alpha/src/nested/f.rs",
            ), (first, second), sites=(
                style_ledger.ListedSite(first, "crates/alpha/src/nested/a.rs", "crates/alpha", 2, "pub fn first() {}"),
                style_ledger.ListedSite(second, "crates/alpha/src/nested/a.rs", "crates/alpha", 1, "pub fn second() {}"),
            ), cells=(
                (first, "crates/alpha/src/nested/a.rs"), (first, "crates/alpha/src/nested/b.rs"),
                (first, "crates/alpha/src/nested/c.rs"), (first, "crates/alpha/src/nested/d.rs"),
                (first, "crates/alpha/src/nested/e.rs"), (first, "crates/alpha/src/nested/f.rs"),
                (second, "crates/alpha/src/nested/a.rs"), (second, "crates/alpha/src/nested/b.rs"),
                (second, "crates/alpha/src/nested/c.rs"), (second, "crates/alpha/src/nested/d.rs"),
                (second, "crates/alpha/src/nested/e.rs"), (second, "crates/alpha/src/nested/f.rs"),
            )),
            style_plan.TicketSelection("structure", "modules", "crates/beta",
                ("crates/beta/src/lib.rs", "crates/beta/src/other.rs"), (first, second), cells=(
                    (first, "crates/beta/src/lib.rs"), (first, "crates/beta/src/other.rs"),
                    (second, "crates/beta/src/lib.rs"), (second, "crates/beta/src/other.rs"),
                )),
        ))
        self.assertEqual(listed_sites.call_args_list, [
            mock.call(first, "crates/alpha"), mock.call(second, "crates/alpha"),
            mock.call(first, "crates/beta"), mock.call(second, "crates/beta"),
        ])

    def test_structure_manifest_cell_joins_first_batch_and_requires_its_crate_verdict(self) -> None:
        structure = self.rule("structure", "tier: D\nfamily: modules\n")
        manifest = self.rule("manifest", "tier: D\nfamily: modules\ninput_scope: manifest\n")
        self.sync()
        selected = self.candidates("modules")[0]
        self.assertIn("crates/alpha/Cargo.toml", selected.files)
        self.assertEqual(set(selected.cells), {(structure, path) for path in self.sources[:2]} | {(manifest, "crates/alpha")})
        ledger = self.ledger()
        ledger.ticket_add("3001", ticket_owner_id("modules"), "structure", "modules", selected.crate,
                          list(selected.files), list(selected.rules), cells=selected.cells)
        ledger.ticket_assign("3001", "demo-style-names")
        _ = ledger.ticket_base("3001")
        for path in self.sources[:2]:
            self.record(structure, path, self.tip, "3001")
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(style_plan.gate(self.unit, "3001"), 1)
        self.assertEqual(output.getvalue(), f"unjudged: {manifest} crates/alpha\n")
        self.record(manifest, "crates/alpha", self.tip, "3001")
        self.assertEqual(style_plan.gate(self.unit, "3001"), 0)
        phase = style_plan.phase_text(ledger.ticket_open(ticket_owner_id("modules"))[0], ledger.pinned_rule_texts("3001"),
                                      self.plan, "context")
        self.assertIn(f"Record `{manifest}` once with `--path crates/alpha` after reading the manifests.", phase)
        self.assertIn("Owned pending verdict cells (record only these rule/path pairs):", phase)
        for rule, path in selected.cells:
            self.assertIn(f"- `{rule}` · `{path}`", phase)

    def test_structure_batches_own_only_pending_pairs_and_manifest_only_work_is_selectable(self) -> None:
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        manifest = self.rule("manifest", "tier: D\nfamily: modules\ninput_scope: manifest\n")
        nested = "crates/alpha/src/nested/inner.rs"
        self.write(nested, "pub fn inner() {}\n")
        self.tip = self.commit()
        self.sync()
        for path in (*self.sources[:2], nested):
            self.record(structure, path, self.tip)
        batches = [ticket for ticket in self.candidates("modules") if ticket.crate == "crates/alpha"]
        self.assertEqual([(ticket.files, ticket.rules, ticket.cells) for ticket in batches], [
            (("crates/alpha/Cargo.toml",), (manifest,), ((manifest, "crates/alpha"),)),
        ])
        held_batches = [ticket for ticket in self.candidates("modules", ("crates/alpha/Cargo.toml",))
                        if ticket.crate == "crates/alpha"]
        self.assertEqual(held_batches, [])
        self.record(manifest, "crates/alpha", self.tip)
        self.write(self.sources[0], "pub fn changed() {}\n")
        self.tip = self.commit()
        self.sync()
        batch = next(ticket for ticket in self.candidates("modules") if ticket.crate == "crates/alpha")
        self.assertEqual((batch.files, batch.rules, batch.cells),
                         (tuple(self.sources[:2]), (structure,),
                          tuple((structure, path) for path in self.sources[:2])))
        self.assertNotIn(nested, batch.files)
        self.assertNotIn((structure, nested), batch.cells)

    def test_approved_proposal_gate_refuses_an_unformatted_changed_file_and_only_checks(self) -> None:
        ticket = self.approved_reference_ticket()
        self.unformatted(self.sources[0])
        self.assertEqual(self.reference_gate(ticket),
                         (1, f"review: not formatted (rustfmt +nightly): {self.sources[0]}\n"))
        self.assertEqual(self.rustfmt_calls(),
                         [f"+nightly --check --files-with-diff --edition 2021 {self.repo.resolve() / self.sources[0]}"])

    def test_approved_proposal_gate_names_each_unformatted_changed_file_sorted(self) -> None:
        ticket = self.approved_reference_ticket()
        widened = self.sources[3]
        self.write(widened, "pub fn moved() -> usize { 1 }\n")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(style_plan.main(["widen", "--repo", str(self.repo), "--ticket", ticket, "--files", widened,
                                              "--reason", "the move needs a new file", "--by", "showrunner"]), 0)
        self.unformatted(widened, self.sources[0])
        self.assertEqual(self.reference_gate(ticket), (1, "".join(
            f"review: not formatted (rustfmt +nightly): {path}\n" for path in (self.sources[0], widened))))

    def test_approved_proposal_gate_passes_an_unformatted_unchanged_file(self) -> None:
        ticket = self.approved_reference_ticket()
        self.unformatted(self.sources[2])
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))
        self.assertEqual(len(self.rustfmt_calls()), 1)

    def test_approved_proposal_gate_runs_no_rustfmt_without_a_changed_rust_file(self) -> None:
        ticket = self.approved_reference_ticket()
        _ = self.commit()
        self.fake_fmt(stderr="error: rustfmt ran\n", code=1)
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))
        self.assertEqual(self.rustfmt_calls(), [])

    def test_format_check_names_every_changed_rust_file_with_the_edition_of_its_package(self) -> None:
        self.write("Cargo.toml", '[workspace]\nmembers = ["crates/alpha", "crates/beta"]\n'
                   + '[workspace.package]\nedition = "2024"\n')
        self.write("crates/beta/Cargo.toml", '[package]\nname = "beta"\nversion = "0.1.0"\nedition.workspace = true\n')
        self.write("crates/gamma/Cargo.toml", '[package]\nname = "gamma"\nversion = "0.1.0"\n')
        self.write("crates/gamma/src/lib.rs", "pub fn gamma() {}\n")
        _ = self.commit()
        # No module tree reaches the orphan, the example or the build script; a deleted file is not named.
        changed = ("crates/alpha/src/orphan.rs", "crates/beta/build.rs", "crates/beta/examples/demo.rs",
                   self.sources[2], "crates/gamma/src/lib.rs")
        for path in changed:
            self.write(path, "fn  changed() {}\n")
        self.write("docs/notes.md", "Not Rust.\n")
        (self.repo / self.sources[1]).unlink()
        self.assertEqual(style_plan.format_check(self.repo), 0)
        root = self.repo.resolve()
        files = {edition: " ".join(str(root / path) for path in changed if path.startswith(crate))
                 for edition, crate in (("2015", "crates/gamma"), ("2021", "crates/alpha"), ("2024", "crates/beta"))}
        self.assertEqual(self.rustfmt_calls(), [f"+nightly --check --files-with-diff --edition {edition} {names}"
                                                for edition, names in files.items()])

    def test_format_check_refuses_a_changed_file_with_no_edition_as_a_failure_to_run(self) -> None:
        for path, manifest, message in (
                ("tools/loose.rs", "", "tools/loose.rs is in no package, so rustfmt has no edition for it"),
                (self.sources[2], '[package]\nname = "beta"\nedition.workspace = true\n',
                 f"{self.sources[2]}: its package has no edition that rustfmt can take"),
                (self.sources[2], '[package\n', "Cargo.toml: ")):
            with self.subTest(message=message):
                _ = self.git("checkout", "--", ".")
                _ = self.git("clean", "-fdq", "--", "crates", "tools")
                if manifest:
                    self.write("crates/beta/Cargo.toml", manifest)
                self.write(path, "fn changed() {}\n")
                with self.assertRaisesRegex(style_ledger.CheckCouldNotRun, re.escape(message)):
                    _ = style_plan.format_check(self.repo)
        self.assertEqual(self.rustfmt_calls(), [])

    def test_approved_proposal_gate_reads_rustfmt_by_exit_code_and_stdout_and_not_by_notes_on_stderr(self) -> None:
        ticket = self.approved_reference_ticket()
        notes = ("info: syncing channel updates for 'nightly-x86_64-unknown-linux-gnu'\n"
                 + 'Warning: the `version` option is deprecated. Use `style_edition="2024"` instead.\n')
        self.fake_fmt(stderr=notes)
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))
        self.fake_fmt("{cwd}/" + self.sources[0] + "\n", notes, 1)
        self.assertEqual(self.reference_gate(ticket),
                         (1, f"review: not formatted (rustfmt +nightly): {self.sources[0]}\n"))

    def test_approved_proposal_gate_reports_a_failed_format_check_as_exit_two(self) -> None:
        ticket = self.approved_reference_ticket()
        failed = "error: rustfmt +nightly --check failed: "
        for stdout, stderr, code, message in (
                ("", "error: this file contains an unclosed delimiter\n", 1,
                 failed + "error: this file contains an unclosed delimiter"),
                # A parse error in one file still lists the other unformatted files.
                ("{cwd}/" + self.sources[0] + "\n", "Warning: deprecated\nerror: expected item\n", 1,
                 failed + "error: expected item"),
                ("", "Error: file `x.rs` does not exist\n", 1, failed + "Error: file `x.rs` does not exist"),
                ("", "thread 'main' panicked\n", 101, failed + "thread 'main' panicked"),
                ("", "", 1, failed + "exit 1"),
                ("Usage: rustfmt\n", "", 1, failed + "Usage: rustfmt")):
            with self.subTest(message=message):
                self.fake_fmt(stdout, stderr, code)
                self.assertEqual(self.reference_gate(ticket), (2, message + "\n"))
        git = self.root / "git-only/git"
        git.parent.mkdir()
        git.symlink_to(cast(str, shutil.which("git")))
        with mock.patch.dict(os.environ, {"PATH": str(git.parent)}):
            self.assertEqual(self.reference_gate(ticket), (2, "error: cargo +nightly fmt --check could not run: "
                                                              + "[Errno 2] No such file or directory: 'cargo'\n"))

    def test_format_check_takes_the_cargo_verdict_for_visited_files_and_rustfmt_for_every_other_file(self) -> None:
        lib, other, orphan = self.sources[0], self.sources[1], "crates/alpha/src/orphan.rs"
        skipped = ("crates/alpha/src/kept/mod.rs", "crates/alpha/src/skipped.rs", "crates/alpha/src/skipped/deep.rs")
        self.write(lib, "#[rustfmt::skip]\nmod skipped;\n#[cfg_attr(rustfmt, rustfmt_skip)]\nmod kept;\n"
                   + "#[rustfmt::skip]\nmod orphan {}\nmod other;\n")
        _ = self.commit()
        for path in (other, orphan, *skipped):
            self.write(path, "fn  changed() {}\n")
        # cargo visits lib and other and refuses other. It skips the skipped modules and never reaches the orphan, which
        # an inline module of the same name does not declare, so rustfmt checks all four by name.
        self.fake_cargo(self.CARGO_NOTES + f"Formatting {{cwd}}/{lib}\nFormatting {{cwd}}/{other}\n{{cwd}}/{other}\n"
                        + "Spent 0.001 secs\n", 1)
        self.unformatted(orphan, skipped[1])
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(style_plan.format_check(self.repo), 1)
        self.assertEqual(output.getvalue(), f"review: not formatted (rustfmt +nightly): {orphan}\n"
                                            + f"review: not formatted (cargo +nightly fmt): {other}\n"
                                            + f"review: not formatted (rustfmt +nightly): {skipped[1]}\n")
        root = self.repo.resolve()
        self.assertEqual(fake_fmt.calls(self.root / "fmt", "cargo"), [" ".join(style_plan.CARGO_FMT_CHECK[1:])])
        names = " ".join(str(root / path) for path in (skipped[0], orphan, *skipped[1:]))
        self.assertEqual(self.rustfmt_calls(), [f"+nightly --check --files-with-diff --edition 2021 {names}"])

    def test_format_check_reads_only_visited_and_listed_lines_and_judges_the_rest_by_exit_code(self) -> None:
        other = self.sources[1]
        self.write(other, "fn changed() {}\n")
        visit = f"Formatting {{cwd}}/{other}\nA line rustfmt may add one day for {{cwd}}/{other}\nSpent 0.001 secs\n"
        self.fake_cargo(self.CARGO_NOTES + visit)
        self.assertEqual(style_plan.format_check(self.repo), 0)
        self.assertEqual(self.rustfmt_calls(), [])
        self.fake_cargo(self.CARGO_NOTES + visit, 1)
        message = "cargo +nightly fmt --check failed: [lib (2021)]"
        with self.assertRaisesRegex(style_ledger.CheckCouldNotRun, re.escape(message)):
            _ = style_plan.format_check(self.repo)
        self.assertEqual(self.rustfmt_calls(), [])

    def test_format_check_refuses_a_changed_symlink_by_its_own_path(self) -> None:
        spare, alias = "crates/alpha/src/spare.rs", "crates/alpha/src/alias.rs"
        self.write(spare, "fn  spare() {}\n")
        _ = self.commit()
        (self.repo / alias).symlink_to("spare.rs")
        self.fake_cargo(f"Formatting {{cwd}}/{spare}\n{{cwd}}/{spare}\n", 1)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(style_plan.format_check(self.repo), 1)
        self.assertEqual(output.getvalue(), f"review: not formatted (cargo +nightly fmt): {alias}\n")
        outside = self.root / "outside.rs"
        _ = outside.write_text("fn outside() {}\n")
        (self.repo / alias).unlink()
        (self.repo / alias).symlink_to(outside)
        with self.assertRaisesRegex(style_ledger.CheckCouldNotRun, f"{alias} resolves to .*, outside the repo"):
            _ = style_plan.format_check(self.repo)

    def test_approved_rename_refuses_unqualified_or_unapproved_names(self) -> None:
        ticket = self.approved_reference_ticket()
        for before, after in (("Self::OldName", "Self::NewName"), ("OldName", "NewName"),
                              ("Other::OldName", "Other::NewName"),
                              ("Outcome::OldName", "Outcome::NotApproved"),
                              ("use crate::m::Other::{OldName};", "use crate::m::Other::{NewName};")):
            with self.subTest(before=before):
                self.write(self.sources[1], before + "\n")
                _ = self.commit()
                self.write(self.sources[1], after + "\n")
                result, output = self.reference_gate(ticket)
                self.assertEqual(result, 1, output)
                self.assertIn(self.sources[1], output)

    def test_approved_rename_refuses_untracked_added_deleted_or_renamed_reference_files(self) -> None:
        self.write(self.sources[1], "fn read() { Outcome::OldName; }\n")
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        new_path = "crates/alpha/src/new.rs"
        self.write(new_path, "fn read() { Outcome::NewName; }\n")
        for staged in (False, True):
            with self.subTest(staged=staged):
                if staged:
                    _ = self.git("add", new_path)
                result, output = self.reference_gate(ticket)
                self.assertEqual(result, 1, output)
                self.assertIn(new_path, output)
        _ = self.git("reset", "--hard", "HEAD")
        _ = self.git("mv", self.sources[1], new_path)
        self.write(new_path, "fn read() { Outcome::NewName; }\n")
        result, output = self.reference_gate(ticket)
        self.assertEqual(result, 1, output)
        self.assertIn(new_path, output)
        _ = self.git("reset", "--hard", "HEAD")
        (self.repo / self.sources[1]).unlink()
        result, output = self.reference_gate(ticket)
        self.assertEqual(result, 1, output)
        self.assertIn(self.sources[1], output)

    def test_approved_rename_refuses_nonrename_or_invalid_approval_pairs(self) -> None:
        self.write(self.sources[1], "fn read() { Outcome::OldName; }\n")
        _ = self.commit()
        ticket = self.approved_reference_ticket((("Outcome::OldName", "Outcome::NewName"),
                                                 ("Other::Before", "Other::After")), ("rename", "type"))
        self.write(self.sources[1], "fn read() { Outcome::NewName; }\n")
        result, output = self.reference_gate(ticket)
        self.assertEqual(result, 1, output)
        self.assertIn(self.sources[1], output)

    def test_approved_rename_refuses_an_approval_without_a_parent(self) -> None:
        self.write(self.sources[1], "fn read() { Outcome::OldName; }\n")
        _ = self.commit()
        ticket = self.approved_reference_ticket((("OldName", "NewName"),))
        self.write(self.sources[1], "fn read() { Outcome::NewName; }\n")
        result, output = self.reference_gate(ticket)
        self.assertEqual(result, 1, output)
        self.assertIn(self.sources[1], output)

    def test_a_bare_rename_beside_a_qualified_pair_leaves_the_pair_approved_and_approves_nothing_itself(self) -> None:
        before = "fn read() { Outcome::OldName; helper(); }\n"
        self.write(self.sources[1], before)
        _ = self.commit()
        ticket = self.approved_reference_ticket((("Outcome::OldName", "Outcome::NewName"), ("helper", "assistant")))
        self.write(self.sources[1], before.replace("OldName", "NewName"))
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))
        for after in (before.replace("helper", "assistant"), before.replace("OldName", "NewName").replace("read", "load")):
            with self.subTest(after=after):
                self.write(self.sources[1], after)
                result, output = self.reference_gate(ticket)
                self.assertEqual(result, 1, output)
                self.assertIn(self.sources[1] + " (change is not an approved rename reference)", output)

    def test_approved_rename_refuses_a_pair_that_changes_the_parent_path(self) -> None:
        self.write(self.sources[1], "fn read() { Outcome::OldName; }\n")
        _ = self.commit()
        ticket = self.approved_reference_ticket((("a::Outcome::OldName", "b::Outcome::NewName"),))
        self.write(self.sources[1], "fn read() { Outcome::NewName; }\n")
        result, output = self.reference_gate(ticket)
        self.assertEqual(result, 1, output)
        self.assertIn(self.sources[1], output)

    def test_type_approval_still_allows_any_change_in_the_finding_file(self) -> None:
        ticket = self.approved_reference_ticket(kinds=("type",))
        self.write(self.sources[0], "// Approved type work may change more than a name.\nfn changed() {}\n")
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_type_approval_keeps_reference_files_outside_its_scope(self) -> None:
        self.write(self.sources[1], "fn read() { Outcome::OldName; }\n")
        _ = self.commit()
        ticket = self.approved_reference_ticket(kinds=("type",))
        self.write(self.sources[1], "fn read() { Outcome::NewName; }\n")
        result, output = self.reference_gate(ticket)
        self.assertEqual(result, 1, output)
        self.assertIn(self.sources[1], output)

    def test_restructure_approval_keeps_reference_files_outside_its_scope(self) -> None:
        self.write(self.sources[1], "fn read() { Outcome::OldName; }\n")
        _ = self.commit()
        ticket = self.approved_reference_ticket(kinds=("restructure",))
        self.write(self.sources[1], "fn read() { Outcome::NewName; }\n")
        result, output = self.reference_gate(ticket)
        self.assertEqual(result, 1, output)
        self.assertIn(self.sources[1], output)

    def test_footprint_gate_uses_the_declared_arguments_and_preserves_the_ticket_start(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.assertEqual(self.run_next()[0], 0)
        gate = next(line.strip("`") for line in self.plan.read_text().splitlines() if "style_plan.py gate" in line)
        arguments = shlex.split(gate)
        self.assertEqual([argument for argument in arguments if argument.startswith("--")], ["--repo", "--ticket"])
        self.assertEqual(arguments[arguments.index("--repo") + 1], ".")
        self.assertNotIn("--head", arguments)
        self.assertEqual(arguments[arguments.index("--ticket") + 1], "1")
        base = self.tip
        self.assertEqual(self.ledger().ticket_open(ticket_owner_id("names"))[0]["base"], base)
        self.assertEqual(self.git("for-each-ref", "refs/style-tickets"), "")
        expected = self.plan.read_bytes()
        _ = (self.unit / self.sources[0]).write_text("pub fn changed() {}\n")
        _ = self.git("add", self.sources[0], cwd=self.unit)
        _ = self.git("commit", "-qm", "Ticket change", cwd=self.unit)
        _ = self.plan.write_text(self.plan_header())
        self.assertEqual(self.run_next()[0], 0)
        self.assertEqual(self.ledger().ticket_open(ticket_owner_id("names"))[0]["base"], base)
        self.assertEqual(self.git("for-each-ref", "refs/style-tickets"), "")
        self.assertNotEqual(self.git("rev-parse", "HEAD", cwd=self.unit), self.tip)
        self.assertIn(gate, self.plan.read_text())
        self.assertEqual(self.plan.read_bytes(), expected)

    def test_busy_approved_files_wait_without_a_ticket_and_are_assigned_after_clearing(self) -> None:
        rule = self.rule("read")
        self.sync()
        for path in self.sources:
            self.record(rule, path, self.tip)
        folder = self.root / "issues"
        folder.mkdir()
        issue = folder / "approved.md"
        with style_ledger.open_ledger(self.repo) as ledger:
            blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule)
            finding = ledger.hold(rule, self.sources[0], 1, "value", "type", "u32", "u64", blob, "outside")
            ledger.file_held(finding.finding_id, issue)
        _ = issue.write_text('---\nstatus: open\nstage: backlog\ntags: [issue, proposal, style]\n'
                             + f'unit: {unit_staff_id("names")}\nplan: "{self.plan} Phase 1"\nstyle_rule: {rule}\nstyle_crate: crates/alpha\nstyle_rule_blob: {blob}\n---\n')
        self.proposals_field(folder)
        with (mock.patch.object(berth_board, "read_board", return_value=self.board(self.sources[0])),
              mock.patch.object(style_proposals, "proposal_command") as start,
              mock.patch.object(style_ledger.Ledger, "cells", autospec=True, side_effect=style_ledger.Ledger.cells) as coverage):
            code, output, errors = self.run_next()
            self.assertEqual(code, 3, errors)
            self.assertIn(f"waiting {issue}: {self.sources[0]} busy\n", output)
            self.assertEqual(self.count_tickets(), 0)
            self.assertEqual(frontmatter(issue)["stage"], ["backlog"])
            start.assert_not_called()
            self.assertEqual(coverage.call_count, 2)
        def start_issue(*args: str) -> None:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(proposal_issue.main(list(args)), 0)
        with mock.patch.object(style_proposals, "proposal_command", side_effect=start_issue):
            result = self.run_next()
        self.assertEqual(result[0], 0, result)
        ticket = self.ledger().ticket_open(ticket_owner_id("names"))[0]
        self.assertEqual((ticket["kind"], ticket["proposal"], ticket["files"]), ("proposal", str(issue), [self.sources[0]]))
        self.assertEqual(frontmatter(issue)["stage"], ["active"])

    def test_structure_gate_names_unjudged_cells_before_running_the_footprint_check(self) -> None:
        first = self.rule("structure-first", "tier: D\nfamily: modules\n")
        last = self.rule("structure-last", "tier: D\nfamily: modules\n")
        self.sync()
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.ticket_add("3001", ticket_owner_id("modules"), "structure", "modules", "crates/alpha", [], [last, first])
            ledger.ticket_assign("3001", "style/main")
            _ = ledger.ticket_base("3001")
        self.record(first, "crates/alpha", self.tip, "3001")
        output = io.StringIO()
        with (style_ledger.open_ledger(self.repo) as ledger,
              mock.patch.object(style_ledger, "open_ledger", return_value=ledger),
              mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as footprint,
              redirect_stdout(output)):
            self.assertEqual(style_plan.gate(self.repo, "3001"), 1)
            footprint.assert_not_called()
        self.assertEqual(output.getvalue(), f"unjudged: {last} crates/alpha\n")

        self.record(last, "crates/alpha", self.tip, "3001")
        with (style_ledger.open_ledger(self.repo) as ledger,
              mock.patch.object(style_ledger, "open_ledger", return_value=ledger),
              mock.patch.object(style_plan, "synced_style_branch", return_value=""),
              mock.patch.object(style_plan, "format_check", return_value=0),
              mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as footprint):
            self.assertEqual(style_plan.gate(self.repo, "3001"), 0)
            footprint.assert_called_once()
        arguments = cast(list[str], footprint.call_args.args[0])
        self.assertIn("style_footprint.py", arguments[1])
        self.assertEqual(arguments[arguments.index("--base") + 1], self.tip)

    def test_read_gate_allows_a_yielded_file_while_checking_the_remaining_owned_cells(self) -> None:
        first, last = self.rule("read-first"), self.rule("read-last")
        self.sync()
        ledger = self.ledger()
        ledger.ticket_add("1", ticket_owner_id("names"), "read", "names", "crates/alpha",
                          list(self.sources[:2]), [first, last])
        ledger.ticket_assign("1", "style/main")
        _ = ledger.ticket_base("1")
        before = {(cell.rule, cell.path): cell.state for cell in ledger.cells() if cell.path == self.sources[0]}
        for rule in (first, last):
            for path in self.sources[:2]:
                self.record(rule, path, self.tip, "1")
        _ = ledger.ticket_yield("1", self.sources[0])
        self.assertEqual({(cell.rule, cell.path): cell.state for cell in ledger.cells() if cell.path == self.sources[0]}, before)
        with (mock.patch.object(style_ledger, "open_ledger", return_value=ledger),
              mock.patch.object(style_plan, "synced_style_branch", return_value=""),
              mock.patch.object(style_plan, "format_check", return_value=0),
              mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as footprint):
            self.assertEqual(style_plan.gate(self.repo, "1"), 0)
            footprint.assert_called_once()
        with style_ledger.open_ledger(self.repo) as reopened:
            reopened.ticket_checkpoint("1", self.tip)
            self.assertEqual(reopened.ticket_open(ticket_owner_id("names"))[0]["state"], "checkpointed")

    def test_coverage_gate_uses_the_base_exposed_by_ticket_origin(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.assertEqual(self.run_next()[0], 0)
        ticket = self.ledger().ticket_open(ticket_owner_id("names"))[0]
        for rule in ticket["rules"]:
            for path in ticket["files"]:
                self.record(rule, path, self.tip, ticket["id"])
        origin = style_ledger.SelectedByCoverage("base-from-origin")
        with (style_ledger.open_ledger(self.repo) as ledger,
              mock.patch.object(style_ledger, "open_ledger", return_value=ledger),
              mock.patch.object(style_ledger.Ledger, "ticket_origin", return_value=origin),
              mock.patch.object(style_plan, "synced_style_branch", return_value=""),
              mock.patch.object(style_plan, "format_check", return_value=0),
              mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run):
            self.assertEqual(style_plan.gate(self.repo, "1"), 0)
        arguments = cast(list[str], run.call_args.args[0])
        self.assertEqual(arguments[arguments.index("--base") + 1], "base-from-origin")

    def test_coverage_gate_excludes_direct_branch_point_work_taken_in_from_the_style_branch(self) -> None:
        ticket = self.take_style_work_into_judged_ticket()
        result = subprocess.run(
            [str(Path(__file__).resolve().parents[1] / "lib/py"), str(Path(style_plan.__file__)), "gate", "--repo", str(self.unit), "--ticket", ticket["id"]],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "mechanical\n", ""))
        self.assertEqual(self.ledger().ticket_open(ticket_owner_id("names"))[0]["base"], ticket["base"])
        self.assertEqual(ticket["base"], self.tip)

    def test_coverage_gate_still_reviews_ticket_edits_after_taking_in_style_branch_work(self) -> None:
        ticket = self.take_style_work_into_judged_ticket()
        _ = (self.unit / ticket["files"][0]).write_text("pub fn value() { let count = 7; }\n")
        result = subprocess.run(
            [str(Path(__file__).resolve().parents[1] / "lib/py"), str(Path(style_plan.__file__)), "gate", "--repo", str(self.unit), "--ticket", ticket["id"]],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 1, result)
        self.assertIn("review: ", result.stdout)
        self.assertIn("check 6:", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_slice_remainders_balance_across_small_directories(self) -> None:
        paths: list[str] = []
        for index in range(30):
            path = f"crates/alpha/src/module{index:02}/file.rs"
            self.write(path, "// source\n" * 200)
            paths.append(path)
        tip = self.commit()
        slices = style_plan.read_slices(self.repo, tip, tuple(paths))
        self.assertEqual({path for group in slices for path in group}, set(paths))
        self.assertEqual(len(slices), 2)
        for group in slices:
            self.assertGreaterEqual(len(group), 10)
            self.assertLessEqual(len(group), 25)
            self.assertLessEqual(len(group) * 200, 5000)

    def test_oversized_directory_balances_with_a_small_trailing_directory(self) -> None:
        paths: list[str] = []
        for directory, count in (("large", 40), ("small", 6)):
            for index in range(count):
                path = f"crates/alpha/src/{directory}/file{index:02}.rs"
                self.write(path, "// source\n" * 200)
                paths.append(path)
        slices = style_plan.read_slices(self.repo, self.commit(), tuple(paths))
        self.assertEqual({path for group in slices for path in group}, set(paths))
        self.assertEqual(len(slices), 2)
        for group in slices:
            self.assertGreaterEqual(len(group), 10)
            self.assertLessEqual(len(group), 25)
            self.assertLessEqual(len(group) * 200, 5000)
        self.assertEqual(sum(any("/small/" in path for path in group) for group in slices), 1)


if __name__ == "__main__":
    _ = unittest.main()
