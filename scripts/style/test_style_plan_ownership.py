"""Busy rename references and declaring owners protect other work."""

from __future__ import annotations

import sqlite3
import sys
import unittest
from contextlib import closing
from pathlib import Path
from typing import final
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import style_proposals

import berth_board
import style_ledger

from style_plan_test_support import StylePlanFixture


@final
class StylePlanTests(StylePlanFixture):
    def test_decision_refill_prints_each_wait_once_and_builds_the_available_files(self) -> None:
        rule = self.rule("read")
        files = tuple(f"crates/alpha/src/piece_{index:02}.rs" for index in range(3))
        for path in files:
            self.write(path, "fn work() {}\n")
        self.commit_to_every_checkout()
        issue = self.approved_decision(rule, files, "rename")
        with (mock.patch.object(berth_board, "read_board", return_value=self.board(files[0], files[1])),
              mock.patch.object(style_proposals, "proposal_command")):
            code, output, errors = self.run_next()
        self.assertEqual(code, 0, errors)
        self.assertEqual(output.count(f"waiting {issue}: {files[0]} busy\nwaiting {issue}: {files[1]} busy\n"), 1)
        self.assertEqual(self.ledger().ticket_open("names-unit")[0]["files"], [files[2]])

    def test_decision_refill_names_its_busy_file_and_builds_its_free_file(self) -> None:
        rule = self.rule("read")
        files = ("crates/alpha/src/piece_00.rs", "crates/alpha/src/piece_01.rs")
        for path in files:
            self.write(path, "fn work() {}\n")
        self.commit_to_every_checkout()
        issue = self.approved_decision(rule, files, "rename")
        with (mock.patch.object(berth_board, "read_board", return_value=self.board(files[1])),
              mock.patch.object(style_proposals, "proposal_command")):
            code, output, errors = self.run_next()
        self.assertEqual(code, 0, errors)
        self.assertEqual(output.count(f"waiting {issue}: {files[1]} busy\n"), 1)
        self.assertNotIn(files[0], output)
        self.assertEqual(self.ledger().ticket_open("names-unit")[0]["files"], [files[0]])

    def test_decision_refill_holds_a_file_rename_whose_reference_file_is_busy_and_builds_the_next(self) -> None:
        rule = self.rule("read")
        issue = self.file_rename_decision(rule, (("crates/alpha/src/probe.rs", "Rename probe.rs to frame_log.rs"),
                                                 ("crates/alpha/src/gauge.rs", "Rename gauge.rs to meter.rs")))
        probe, gauge = (next(finding for finding in self.ledger().proposal_findings(issue) if finding.path == path)
                        for path in ("crates/alpha/src/probe.rs", "crates/alpha/src/gauge.rs"))
        with (mock.patch.object(berth_board, "read_board", return_value=self.board("crates/alpha/src/render.rs")),
              mock.patch.object(style_proposals, "proposal_command")):
            code, output, errors = self.run_next()
        self.assertEqual(code, 0, errors)
        self.assertEqual(output.count(f"waiting {issue}: {probe.finding_id} edits crates/alpha/src/render.rs, busy\n"), 1)
        self.assertNotIn(f"waiting {issue}: crates/alpha/src/probe.rs busy", output)
        ticket = self.ledger().ticket_open("names-unit")[0]
        self.assertEqual((ticket["files"], ticket["finding_ids"]), (["crates/alpha/src/gauge.rs"], [gauge.finding_id]))

    def test_decision_refill_builds_a_file_rename_whose_busy_file_names_no_renamed_module(self) -> None:
        rule = self.rule("read")
        issue = self.file_rename_decision(rule, (("crates/alpha/src/probe.rs", "Rename probe.rs to frame_log.rs"),))
        # The busy file holds the word probe, as a function name, not as the renamed module.
        with (mock.patch.object(berth_board, "read_board", return_value=self.board(self.sources[1])),
              mock.patch.object(style_proposals, "proposal_command")):
            code, output, errors = self.run_next()
        self.assertEqual(code, 0, errors)
        self.assertNotIn("waiting", output)
        ticket = self.ledger().ticket_open("names-unit")[0]
        self.assertEqual((ticket["files"], ticket["finding_ids"]),
                         (["crates/alpha/src/probe.rs"], [self.ledger().proposal_findings(issue)[0].finding_id]))

    def test_decision_refill_holds_a_file_rename_whose_busy_file_outside_the_crate_root_mounts_it(self) -> None:
        rule = self.rule("read")
        self.write("crates/alpha/tests/probe_tests.rs", '#[path = "../src/probe.rs"]\nmod probe;\n')
        issue = self.file_rename_decision(rule, (("crates/alpha/src/probe.rs", "Rename probe.rs to frame_log.rs"),))
        finding = self.ledger().proposal_findings(issue)[0]
        with (mock.patch.object(berth_board, "read_board", return_value=self.board("crates/alpha/tests/probe_tests.rs")),
              mock.patch.object(style_proposals, "proposal_command")):
            _, output, _ = self.run_next()
        self.assertIn(f"waiting {issue}: {finding.finding_id} edits crates/alpha/tests/probe_tests.rs, busy\n", output)

    def test_decision_refill_holds_a_qualified_rename_whose_markdown_reference_is_busy(self) -> None:
        rule = self.rule("read")
        self.write("docs/notes.md", "Use Outcome::Old.\n")
        self.commit_to_every_checkout()
        issue = self.approved_decision(rule, (self.sources[0],), "rename")
        finding = self.ledger().proposal_findings(issue)[0]
        with (mock.patch.object(berth_board, "read_board", return_value=self.board("docs/notes.md")),
              mock.patch.object(style_proposals, "proposal_command")):
            _, output, _ = self.run_next()
        self.assertIn(f"waiting {issue}: {finding.finding_id} edits docs/notes.md, busy\n", output)
        # The decision has no free finding, so refill plans coverage work instead.
        self.assertEqual(self.ledger().ticket_open("names-unit")[0]["kind"], "read")
        self.assertIsInstance(self.ledger().proposal_findings(issue)[0].build_ticket, style_ledger.NoBuildTicket)

    def test_refill_drops_a_queued_file_rename_ticket_whose_reference_file_is_busy(self) -> None:
        rule = self.rule("read")
        issue = self.file_rename_decision(rule, (("crates/alpha/src/probe.rs", "Rename probe.rs to frame_log.rs"),))
        ledger = self.ledger()
        finding = ledger.proposal_findings(issue)[0]
        ledger.ticket_add("names-unit-1", "names-unit", "proposal", "names", "crates/alpha", [finding.path], [rule],
                          proposal=str(issue), finding_ids=(finding.finding_id,))
        with (mock.patch.object(berth_board, "read_board", return_value=self.board("crates/alpha/src/render.rs")),
              mock.patch.object(style_proposals, "proposal_command")):
            _, output, _ = self.run_next()
        self.assertIn("dropped names-unit-1: files busy\n", output)
        self.assertIn(f"waiting {issue}: {finding.finding_id} edits crates/alpha/src/render.rs, busy\n", output)
        self.assertEqual(ledger.ticket_open("names-unit")[0]["kind"], "read")
        self.assertIsInstance(ledger.proposal_findings(issue)[0].build_ticket, style_ledger.NoBuildTicket)

    def test_decision_refill_holds_a_file_rename_whose_busy_file_names_it_only_at_the_unit_base(self) -> None:
        rule = self.rule("read")
        issue = self.file_rename_decision(rule, (("crates/alpha/src/probe.rs", "Rename probe.rs to frame_log.rs"),))
        finding = self.ledger().proposal_findings(issue)[0]
        # The unit's branch adds a caller of the renamed module after the synced tip.
        _ = self.unit_commit("crates/alpha/src/gauge.rs", "pub fn level() { crate::probe::sample(); }\n")
        with (mock.patch.object(berth_board, "read_board", return_value=self.board("crates/alpha/src/gauge.rs")),
              mock.patch.object(style_proposals, "proposal_command")):
            _, output, _ = self.run_next()
        self.assertIn(f"waiting {issue}: {finding.finding_id} edits crates/alpha/src/gauge.rs, busy\n", output)
        self.assertIsInstance(self.ledger().proposal_findings(issue)[0].build_ticket, style_ledger.NoBuildTicket)

    def test_decision_refill_holds_a_file_rename_whose_busy_file_declares_it_by_a_raw_name(self) -> None:
        issue = self.file_rename_decision(self.rule("read"), (("crates/alpha/src/probe.rs", "Rename probe.rs to frame_log.rs"),),
                                          (("crates/alpha/src/lib.rs", "mod gauge;\nmod r#probe;\nmod render;\n"),))
        finding = self.ledger().proposal_findings(issue)[0]
        output = self.refill_with_busy("crates/alpha/src/lib.rs")
        self.assertIn(f"waiting {issue}: {finding.finding_id} edits crates/alpha/src/lib.rs, busy\n", output)

    def test_decision_refill_holds_a_file_rename_declared_by_a_path_attribute_in_an_inline_module(self) -> None:
        self.assert_rename_waits_for_an_uncertain_path(
            "crates/alpha/src/lib.rs", 'mod gauge;\nmod render;\nmod shell {\n    #[path = "probe.rs"]\n    mod probe;\n}\n')

    def test_decision_refill_holds_a_file_rename_declared_by_a_cfg_attr_path(self) -> None:
        self.assert_rename_waits_for_an_uncertain_path(
            "crates/alpha/src/lib.rs", 'mod gauge;\n#[cfg_attr(test, path = "probe.rs")]\nmod probe;\nmod render;\n')

    def test_decision_refill_holds_a_file_rename_declared_in_a_folder_an_inline_module_mounts(self) -> None:
        self.assert_rename_waits_for_an_uncertain_path(
            "crates/alpha/src/lib.rs", 'mod gauge;\nmod render;\n#[path = "."]\nmod shell {\n    mod probe;\n}\n')

    def test_decision_refill_holds_a_file_rename_declared_by_an_escaped_path_string(self) -> None:
        # The string names probe.rs, but the file does not hold the word probe.
        self.assert_rename_waits_for_an_uncertain_path(
            "crates/alpha/src/lib.rs", 'mod gauge;\n#[path = "pro\\u{62}e.rs"]\nmod alias;\nmod render;\n')

    def test_decision_refill_holds_a_file_rename_declared_by_an_escaped_path_string_outside_the_crate(self) -> None:
        self.assert_rename_waits_for_an_uncertain_path(
            "crates/alpha/tests/probe_tests.rs", '#[path = "../src/pro\\u{62}e.rs"]\nmod alias;\n')

    def test_refill_drops_a_queued_file_rename_ticket_whose_owner_has_an_uncertain_path(self) -> None:
        lib = "crates/alpha/src/lib.rs"
        issue = self.file_rename_decision(self.rule("read"), (("crates/alpha/src/probe.rs", "Rename probe.rs to frame_log.rs"),),
                                          ((lib, 'mod gauge;\n#[cfg_attr(test, path = "probe.rs")]\nmod probe;\nmod render;\n'),))
        ledger = self.ledger()
        finding = ledger.proposal_findings(issue)[0]
        ledger.ticket_add("names-unit-1", "names-unit", "proposal", "names", "crates/alpha", [finding.path], [self.rule("read")],
                          proposal=str(issue), finding_ids=(finding.finding_id,))
        output = self.refill_with_busy()
        self.assertIn(f"dropped names-unit-1: {lib} has a path attribute the rename gate cannot read\n", output)
        self.assertIn(f"waiting {issue}: {finding.finding_id} {lib} has a path attribute the rename gate cannot read\n", output)
        self.assertIsInstance(ledger.proposal_findings(issue)[0].build_ticket, style_ledger.NoBuildTicket)

    def test_decision_refill_holds_a_file_rename_whose_own_file_has_an_uncertain_path(self) -> None:
        # Moving the file moves the folder that the inline module's `path` attribute reads from.
        self.assert_rename_waits_for_an_uncertain_path(
            "crates/alpha/src/probe.rs", 'pub fn sample() {}\nmod inner {\n    #[path = "probe/helper.rs"]\n    mod helper;\n}\n')

    def test_decision_refill_builds_a_type_finding_whose_declaring_owner_is_busy(self) -> None:
        issue, finding = self.owned_module_decision("crates/alpha/src/terminal/startup_tests.rs", "crates/alpha/src/terminal.rs",
                                                    "#[cfg(test)]\nmod startup_tests;\n", "type")
        output = self.refill_with_busy("crates/alpha/src/terminal.rs")
        self.assertNotIn(f"waiting {issue}", output)
        ticket = self.ledger().ticket_open("names-unit")[0]
        self.assertEqual((ticket["files"], ticket["finding_ids"]), (["crates/alpha/src/terminal/startup_tests.rs"], [finding]))

    def test_decision_gate_names_the_cause_for_a_ticket_made_before_decisions(self) -> None:
        ticket = self.approved_reference_ticket()
        with closing(sqlite3.connect(self.ledger().database)) as connection, connection:
            _ = connection.execute("UPDATE tickets SET finding_ids='[]' WHERE id=?", (ticket,))
            _ = connection.execute("UPDATE held_findings SET build_ticket=''")
        self.write(self.sources[0], "pub fn approved_change() {}\n")
        code, message = self.reference_gate(ticket)
        self.assertEqual(code, 1)
        self.assertEqual(message, f"review: proposal ticket {ticket}: made before decisions; "
                         + "drop it with style_ledger.py ticket drop, then run migrate\n")

if __name__ == "__main__":
    _ = unittest.main()
