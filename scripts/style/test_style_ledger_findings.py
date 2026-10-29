"""Held findings, outside-rename authority, decisions, and waivers."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unittest
from collections.abc import Callable
from contextlib import closing
from typing import cast, final
from unittest import mock
import style_ledger
from approved_renames import AppliedInCode, Folder, NoAppliedCheck, RenameNotApplied
import declarations

from ledger_test_support import (LedgerTestCase, PARENT_FILE, MOVED_FILE, TWIN_FILE, PARENT)


@final
class StyleLedgerFindingsTests(LedgerTestCase):
    def test_proposal_merge_sets_built_at_once(self) -> None:
        rule, issue, ids = self.decision_findings()
        ledger = self.ledger()
        ledger.ticket_add("names-1", "names", "proposal", "names", "crates/alpha", [self.sources[0]], [rule],
                          proposal=str(issue), finding_ids=ids[:1])
        ledger.ticket_assign("names-1", "main")
        self.write(self.sources[0], "pub enum Outcome { New }\n")
        tip = self.commit("Apply approved rename")
        ledger.set_tip(tip, "main")
        ledger.ticket_checkpoint("names-1", tip)
        with mock.patch.object(style_ledger, "_now", return_value="2026-10-27T10:00:00+00:00"):
            self.assertEqual(ledger.ticket_merge("names-1"), ())
        self.assert_build_time_survives_reading(ledger, ids[0])
        with mock.patch.object(style_ledger, "_now", return_value="2026-10-29T12:00:00+00:00"):
            _ = ledger.ticket_merge("names-1")
        self.assertEqual(self.built_time(ledger, ids[0]), "2026-10-27T10:00:00+00:00")


    def test_declaration_follow_sets_built_at_once(self) -> None:
        ledger = self.ledger()
        finding_id = self.renamed_parent(ledger)
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT.replace("Parent", "Renamed")})
        with mock.patch.object(style_ledger, "_now", return_value="2026-10-27T10:00:00+00:00"):
            outcome, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(outcome, style_ledger.DeclarationRenamed)
        self.assert_build_time_survives_reading(ledger, finding_id)
        with mock.patch.object(style_ledger, "_now", return_value="2026-10-29T12:00:00+00:00"):
            _ = self.follow_approved(ledger, [finding_id])
        self.assertEqual(self.built_time(ledger, finding_id), "2026-10-27T10:00:00+00:00")


    def test_held_built_sets_built_at_once_and_reopening_clears_it(self) -> None:
        ledger, finding_id, _, _ = self.checkpointed_type_findings()
        with mock.patch.object(style_ledger, "_now", return_value="2026-10-27T10:00:00+00:00"):
            ledger.mark_built(finding_id)
        self.assert_build_time_survives_reading(ledger, finding_id)
        with mock.patch.object(style_ledger, "_now", return_value="2026-10-29T12:00:00+00:00"):
            ledger.mark_built(finding_id)
        self.assertEqual(self.built_time(ledger, finding_id), "2026-10-27T10:00:00+00:00")
        ledger.return_to_filed({finding_id: style_ledger.ReadByPerson("reader")})
        self.assertEqual(self.built_time(ledger, finding_id), "")


    def test_mark_built_at_merge_sets_built_at_once(self) -> None:
        _, _, ids = self.decision_findings()
        self.write(self.sources[0], "pub enum Outcome { New }\n")
        tip = self.commit("Apply rename at merge")
        ledger = self.ledger()
        ledger.set_tip(tip, "main")
        with mock.patch.object(style_ledger, "_now", return_value="2026-10-27T10:00:00+00:00"):
            _ = ledger.mark_built_at_merge(ids[:1], tip, style_ledger.NotReadByPerson())
        self.assert_build_time_survives_reading(ledger, ids[0])
        with mock.patch.object(style_ledger, "_now", return_value="2026-10-29T12:00:00+00:00"):
            with self.assertRaisesRegex(style_ledger.LedgerError, "cannot mark a built finding built"):
                _ = ledger.mark_built_at_merge(ids[:1], tip, style_ledger.ReadByPerson("other"))
        self.assertEqual(self.built_time(ledger, ids[0]), "2026-10-27T10:00:00+00:00")


    def test_withdrawing_a_built_duplicate_clears_its_built_at(self) -> None:
        _, issue, ids = self.decision_findings()
        self.write(self.sources[0], "pub enum Outcome { New }\n")
        tip = self.commit("Apply rename at merge")
        ledger = self.ledger()
        ledger.set_tip(tip, "main")
        with mock.patch.object(style_ledger, "_now", return_value="2026-10-27T10:00:00+00:00"):
            _ = ledger.mark_built_at_merge(ids[:1], tip, style_ledger.NotReadByPerson())
        self.assertEqual(self.built_time(ledger, ids[0]), "2026-10-27T10:00:00+00:00")
        ledger.withdraw_duplicates((style_ledger.BareDuplicate(ids[0], ids[1], issue),))
        self.assertEqual(self.built_time(ledger, ids[0]), "")
        states = {finding.finding_id: finding.state for finding in ledger.proposal_findings(issue)}
        self.assertEqual(states[ids[0]], style_ledger.Withdrawn(issue, style_ledger.DuplicateOf(ids[1])))


    def test_record_held_refuses_a_cell_with_no_findings(self) -> None:
        rule = self.rule("read")
        self.set_tip(self.initial_tip)
        result = self.cli("record", "--rule", rule, "--path", self.sources[0], "--commit", self.initial_tip,
                          "--rule-blob", self.blob(rule), "--verdict", "held", "--ticket", "outside", "--worker", "test", success=False)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(rule, result.stderr)
        self.assertIn(self.sources[0], result.stderr)
        self.assertIn("held add", result.stderr)
        self.assertEqual(self.states(rule)[self.sources[0]], "open")


    def test_record_held_names_inactive_findings_and_keeps_the_users_waiver(self) -> None:
        rule = self.rule("read")
        self.set_tip(self.initial_tip)
        finding = self.hold_finding(rule, 1)
        _ = self.cli("held", "waive", "--id", finding, "--person", "nate")
        result = self.cli("record", "--rule", rule, "--path", self.sources[0], "--commit", self.initial_tip,
                          "--rule-blob", self.blob(rule), "--verdict", "held", "--ticket", "outside", "--worker", "test", success=False)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        for words in (rule, self.sources[0], finding, "waived", "user's decision", "held add", "record waived"):
            self.assertIn(words, result.stderr)
        self.assertIsInstance(self.ledger().named_held((finding,))[0].state, style_ledger.Waived)
        self.assertEqual(self.states(rule)[self.sources[0]], "open")


    def test_record_held_accepts_unfiled_and_filed_findings_only_on_the_matching_cell(self) -> None:
        rule = self.rule("read")
        other = self.rule("other")
        self.set_tip(self.initial_tip)
        finding = self.hold_finding(rule, 1)
        self.record(rule, self.sources[0], self.initial_tip, "held")
        ledger = self.ledger()
        ledger.file_held(finding, self.root / "decision.md")
        self.record(rule, self.sources[0], self.initial_tip, "held", ticket="filed-judge")
        self.assertEqual(self.states(rule)[self.sources[0]], "current")
        for wrong_rule, wrong_path in ((rule, self.sources[1]), (other, self.sources[0])):
            with self.subTest(rule=wrong_rule, path=wrong_path):
                with self.assertRaises(style_ledger.LedgerError):
                    ledger.record(wrong_rule, wrong_path, self.initial_tip, self.blob(wrong_rule), "held")


    def test_record_held_reports_built_and_withdrawn_findings_without_counting_them_as_live(self) -> None:
        rule = self.rule("read")
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        built, withdrawn = self.hold_finding(rule, 1), self.hold_finding(rule, 2)
        ledger.file_held(built, self.root / "decision.md")
        ledger.file_held(withdrawn, self.root / "decision.md")
        self.record(rule, self.sources[0], self.initial_tip, "exception")
        with closing(sqlite3.connect(ledger.database)) as connection:
            _ = connection.execute("UPDATE held_findings SET state='built' WHERE finding_id=?", (built,))
            _ = connection.execute("UPDATE held_findings SET state='withdrawn',withdrawn_by=(SELECT MAX(id) FROM judgments) "
                                   + "WHERE finding_id=?", (withdrawn,))
            connection.commit()
        with self.assertRaises(style_ledger.LedgerError) as refused:
            ledger.record(rule, self.sources[0], self.initial_tip, self.blob(rule), "held")
        for words in (built, "built", withdrawn, "withdrawn"):
            self.assertIn(words, str(refused.exception))
        self.assertEqual(ledger.latest_held_judgments(), {})


    def test_record_held_maps_manifest_findings_to_their_crate_cell(self) -> None:
        rule = self.rule("manifest", "tier: D\nfamily: modules\ninput_scope: manifest\n")
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        _ = ledger.hold(rule, self.sources[0], 1, "Change manifest", "restructure", "before", "after", self.blob(rule), "outside")
        ledger.record(rule, "crates/alpha", self.initial_tip, self.blob(rule), "held")
        self.assertEqual(self.states(rule)["crates/alpha"], "current")
        with self.assertRaises(style_ledger.LedgerError):
            ledger.record(rule, "crates/beta", self.initial_tip, self.blob(rule), "held")


    def test_unwaiving_a_finding_restores_a_worker_recorded_waived_verdict_to_held(self) -> None:
        rule = self.rule("read")
        self.set_tip(self.initial_tip)
        finding = self.hold_finding(rule, 1)
        _ = self.cli("held", "waive", "--id", finding, "--person", "nate")
        self.record(rule, self.sources[0], self.initial_tip, "waived")
        ledger = self.ledger()
        self.assertIsInstance(ledger.newest_held_judgment(rule, self.sources[0]), style_ledger.NoHeldJudgment)
        self.assertEqual(ledger.unwaive_held((finding,), style_ledger.WaiveUndoneBy("ana")), (finding,))
        self.assertIsInstance(ledger.named_held((finding,))[0].state, style_ledger.Unfiled)
        self.assertEqual(ledger.latest_held_judgments(), {(rule, self.sources[0]): self.initial_tip})


    def test_preview_ledger_migrates_and_changes_only_a_temporary_copy(self) -> None:
        rule = self.rule("preview")
        database = self.root / "state/sample/ledger.sqlite"
        database.parent.mkdir(parents=True)
        issue = self.root / "decision.md"
        with closing(sqlite3.connect(database)) as connection:
            for number, migration in sorted(style_ledger.MIGRATIONS.items()):
                if number <= 10:
                    _ = connection.executescript(migration)
            _ = connection.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
            _ = connection.execute("INSERT INTO schema_version VALUES (10)")
            _ = connection.execute("INSERT INTO held_findings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                   ("legacy", rule, self.sources[0], 2, "change", "rename", "old", "new", self.blob(rule),
                                    "read-1", "filed", str(issue), "created", "updated"))
            connection.commit()
        original = database.read_bytes()
        with style_ledger.preview_ledger(self.repo) as ledger:
            preview_database = ledger.database
            finding, = ledger.proposal_findings(issue)
            self.assertEqual(finding.before, "old")
            self.assertIsInstance(finding.build_ticket, style_ledger.NoBuildTicket)
            ledger.waive_held(finding.finding_id, style_ledger.WaiveByPerson("nate"))
            self.assertIsInstance(ledger.proposal_findings(issue)[0].state, style_ledger.Waived)
        self.assertEqual(database.read_bytes(), original)
        self.assertFalse(preview_database.exists())
        with closing(sqlite3.connect(database)) as connection:
            version = cast(tuple[int], connection.execute("SELECT version FROM schema_version").fetchone())
            state = cast(tuple[str], connection.execute("SELECT state FROM held_findings").fetchone())
        self.assertEqual(version, (10,))
        self.assertEqual(state, ("filed",))


    def test_preview_ledger_does_not_create_missing_state(self) -> None:
        state = self.root / "state"
        with style_ledger.preview_ledger(self.repo) as ledger:
            self.assertEqual(ledger.proposal_tickets(), ())
            preview_database = ledger.database
        self.assertFalse(state.exists())
        self.assertFalse(preview_database.exists())


    def test_decision_merge_builds_only_its_ticket_findings(self) -> None:
        rule, issue, ids = self.decision_findings()
        ledger = self.ledger()
        ledger.ticket_add("names-1", "names", "proposal", "names", "crates/alpha", [self.sources[0]], [rule],
                          proposal=str(issue), finding_ids=ids[:1])
        ledger.ticket_assign("names-1", "main")
        origin = ledger.ticket_origin("names-1")
        self.assertEqual(origin, style_ledger.ApprovedProposal(issue, "names", ids[:1]))
        self.write(self.sources[0], "pub enum Outcome { New }\n")
        applied = self.commit("Rename Old to New")
        ledger.set_tip(applied, "main")
        ledger.ticket_checkpoint("names-1", applied)
        self.assertEqual(ledger.ticket_merge("names-1"), ())
        findings = ledger.proposal_findings(issue)
        self.assertIsInstance(findings[0].state, style_ledger.Built)
        self.assertEqual(findings[1].state, style_ledger.Filed(issue))
        self.assertIsInstance(findings[1].build_ticket, style_ledger.NoBuildTicket)


    def test_decision_merge_builds_an_applied_rename_and_files_an_unapplied_one_again(self) -> None:
        rule, issue, ids = self.decision_findings()
        ledger = self.ledger()
        ledger.ticket_add("names-1", "names", "proposal", "names", "crates/alpha", list(self.sources[:2]), [rule],
                          proposal=str(issue), finding_ids=ids)
        ledger.ticket_assign("names-1", "main")
        self.write(self.sources[0], "pub enum Outcome { New }\n")
        self.write(self.sources[1], "pub enum Outcome { Old }\n")
        checkpoint = self.commit("Rename one of two")
        ledger.set_tip(checkpoint, "main")
        ledger.ticket_checkpoint("names-1", checkpoint)
        output = self.cli("ticket", "merge", "--id", "names-1").stdout
        self.assertEqual(output, f"not built {ids[1]}: rename not applied in {self.sources[1]}\n")
        built, unbuilt = self.ledger().proposal_findings(issue)
        self.assertIsInstance(built.state, style_ledger.Built)
        self.assertEqual(unbuilt.state, style_ledger.Filed(issue))
        self.assertIsInstance(unbuilt.build_ticket, style_ledger.NoBuildTicket)


    def test_binding_merge_keeps_unapplied_and_unproved_findings_filed(self) -> None:
        source = "fn f() {\n    let old = input;\n    consume(old);\n    let second = input;\n    consume(second);\n}\n"
        ledger, issue, _ = self.binding_ticket(source, ((2, "old", "fresh"), (4, "second", "next")))
        # One declaration changed without its use; the other rename was never attempted.
        self.write(self.sources[0], source.replace("let old", "let fresh"))
        checkpoint = self.commit("Incomplete rename")
        findings = ledger.proposal_findings(issue)
        by_old = {f.before: ledger.applied_check(f, checkpoint) for f in findings}
        self.assertEqual(by_old, {"old": NoAppliedCheck("rename"), "second": RenameNotApplied(self.sources[0])})
        ledger.set_tip(checkpoint, "main")
        ledger.ticket_checkpoint("names-1", checkpoint)
        _ = ledger.ticket_merge("names-1")
        self.assertTrue(all(isinstance(f.state, style_ledger.Filed) for f in ledger.proposal_findings(issue)))


    def test_binding_check_needs_a_build_ticket_with_a_saved_base(self) -> None:
        source = "fn f() {\n    let old = input;\n    consume(old);\n}\n"
        ledger, issue, _ = self.binding_ticket(source, ((2, "old", "fresh"),))
        self.write(self.sources[0], source.replace("old", "fresh"))
        checkpoint = self.commit("Rename without a saved base")
        finding, = ledger.proposal_findings(issue)
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE tickets SET base='' WHERE id='names-1'")
        self.assertEqual(ledger.applied_check(finding, checkpoint), NoAppliedCheck("rename"))
        ledger.ticket_drop("names-1")
        finding, = ledger.proposal_findings(issue)
        self.assertIsInstance(finding.build_ticket, style_ledger.NoBuildTicket)
        self.assertEqual(ledger.applied_check(finding, checkpoint), NoAppliedCheck("rename"))


    def test_a_file_rename_check_reads_which_file_is_at_the_commit(self) -> None:
        finding, _ = self.file_rename_finding()
        old, new = "crates/alpha/src/ledger/lock.rs", "crates/alpha/src/ledger/mutation_lock.rs"
        unapplied = self.git("rev-parse", "HEAD")
        _ = self.git("mv", old, new)
        applied = self.commit("Rename the lock module")
        self.write(old, "pub struct MutationLock;\n")
        both = self.commit("Copy the lock module back")
        _ = self.git("rm", "-q", old, new)
        neither = self.commit("Remove both files")
        ledger = self.ledger()
        self.assertEqual(ledger.applied_check(finding, unapplied), RenameNotApplied(old))
        self.assertEqual(ledger.applied_check(finding, applied), AppliedInCode())
        self.assertEqual(ledger.applied_check(finding, both), NoAppliedCheck("rename"))
        self.assertEqual(ledger.applied_check(finding, neither), NoAppliedCheck("rename"))
        # A commit git cannot read is an error, never a missing file.
        with self.assertRaises(style_ledger.CheckCouldNotRun):
            _ = ledger.applied_check(finding, "0" * 40)


    def test_a_folder_at_the_commit_reads_as_a_folder(self) -> None:
        self.write("ledger/lock/guard.rs", "pub struct Guard;\n")
        commit = self.commit("Add the guard module")
        self.assertEqual(self.ledger()._file_at(commit, "ledger/lock"), Folder())  # pyright: ignore[reportPrivateUsage]


    def test_decision_merge_builds_a_file_rename_only_when_it_is_applied(self) -> None:
        finding, issue = self.file_rename_finding()
        ledger = self.ledger()
        for ticket in ("names-1", "names-2"):
            ledger.ticket_add(ticket, "names", "proposal", "names", "crates/alpha", [finding.path], [finding.rule],
                              proposal=str(issue), finding_ids=(finding.finding_id,))
            ledger.ticket_assign(ticket, "main")
            if ticket == "names-2":
                _ = self.git("mv", finding.path, "crates/alpha/src/ledger/mutation_lock.rs")
                _ = self.commit("Rename the lock module")
            checkpoint = self.git("rev-parse", "HEAD")
            ledger.set_tip(checkpoint, "main")
            ledger.ticket_checkpoint(ticket, checkpoint)
            output = self.cli("ticket", "merge", "--id", ticket).stdout
            held, = self.ledger().proposal_findings(issue)
            if ticket == "names-1":
                self.assertEqual(output, f"not built {finding.finding_id}: rename not applied in {finding.path}\n")
                self.assertEqual(held.state, style_ledger.Filed(issue))
                self.assertIsInstance(held.build_ticket, style_ledger.NoBuildTicket)
            else:
                self.assertEqual(output, "")
                self.assertIsInstance(held.state, style_ledger.Built)


    def test_decision_merge_leaves_a_type_finding_filed_until_a_person_marks_it_built(self) -> None:
        rule, issue, _ = self.decision_findings()
        ledger = self.ledger()
        finding = ledger.hold(rule, self.sources[0], 3, "value", "type", "u32", "u64", self.blob(rule), "outside")
        ledger.file_held(finding.finding_id, issue)
        ledger.ticket_add("names-1", "names", "proposal", "names", "crates/alpha", [self.sources[0]], [rule],
                          proposal=str(issue), finding_ids=(finding.finding_id,))
        ledger.ticket_assign("names-1", "main")
        ledger.set_tip(self.initial_tip, "main")
        ledger.ticket_checkpoint("names-1", self.initial_tip)
        output = self.cli("ticket", "merge", "--id", "names-1").stdout
        self.assertEqual(output, f"not built {finding.finding_id}: type has no applied check; mark it built with "
                         + f"style_ledger.py held built --repo {ledger.repo} --id {finding.finding_id} after checking\n")
        held = next(item for item in self.ledger().proposal_findings(issue) if item.finding_id == finding.finding_id)
        self.assertEqual(held.state, style_ledger.Filed(issue))
        # The finding keeps its merged ticket, so refill does not take it again.
        self.assertEqual(held.build_ticket, style_ledger.BuildTicket("names-1"))
        _ = self.cli("held", "built", "--id", finding.finding_id)
        held = next(item for item in self.ledger().proposal_findings(issue) if item.finding_id == finding.finding_id)
        self.assertIsInstance(held.state, style_ledger.Built)


    def test_a_rerun_of_ticket_merge_prints_the_findings_that_wait_for_a_person_again(self) -> None:
        rule, issue, _ = self.decision_findings()
        ledger = self.ledger()
        finding = ledger.hold(rule, self.sources[0], 3, "value", "type", "u32", "u64", self.blob(rule), "outside")
        ledger.file_held(finding.finding_id, issue)
        ledger.ticket_add("names-1", "names", "proposal", "names", "crates/alpha", [self.sources[0]], [rule],
                          proposal=str(issue), finding_ids=(finding.finding_id,))
        ledger.ticket_assign("names-1", "main")
        ledger.set_tip(self.initial_tip, "main")
        ledger.ticket_checkpoint("names-1", self.initial_tip)
        line = (f"not built {finding.finding_id}: type has no applied check; mark it built with "
                + f"style_ledger.py held built --repo {ledger.repo} --id {finding.finding_id} after checking\n")
        self.assertEqual(self.cli("ticket", "merge", "--id", "names-1").stdout, line)
        self.assertEqual(self.cli("ticket", "merge", "--id", "names-1").stdout, line)
        _ = self.cli("held", "built", "--id", finding.finding_id)
        self.assertEqual(self.cli("ticket", "merge", "--id", "names-1").stdout, "")


    def test_held_replace_reopens_the_same_finding_and_restores_its_waived_cell(self) -> None:
        rule = self.rule("replace")
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        ledger.ticket_add("read-1", "read", "read", "names", "crates/alpha", [self.sources[0]], [rule])
        ledger.ticket_assign("read-1", "main")
        finding_id = self.hold_finding(rule, 1, ticket="read-1")
        ledger.waive_held(finding_id, style_ledger.WaiveByUnit(ledger.named_held((finding_id,))[0].ticket))
        self.record(rule, self.sources[0], self.initial_tip, "waived", ticket="read-1")
        before = ledger.named_held((finding_id,))[0]
        result = self.cli("held", "replace", "--id", finding_id, "--after", "Outcome::Correct")
        self.assertEqual(result.stdout, finding_id + "\n")
        finding, = ledger.named_held((finding_id,))
        self.assertEqual((finding.finding_id, finding.after, finding.state, finding.build_ticket),
                         (finding_id, "Outcome::Correct", style_ledger.Unfiled(), style_ledger.NoBuildTicket()))
        self.assertEqual((finding.rule, finding.path, finding.line, finding.text, finding.kind, finding.before,
                          finding.rule_blob, finding.ticket, finding.waive_undo),
                         (before.rule, before.path, before.line, before.text, before.kind, before.before,
                          before.rule_blob, before.ticket, before.waive_undo))
        self.assertEqual(finding.waive_provenance, style_ledger.WaiveProvenance.UNIT)
        self.assertEqual(ledger.latest_held_judgments(), {(rule, self.sources[0]): self.initial_tip})
        self.record(rule, self.sources[0], self.initial_tip, "held", ticket="read-1")


    def test_held_add_keeps_a_waive_until_explicit_replace(self) -> None:
        rule = self.rule("replace")
        self.set_tip(self.initial_tip)
        finding_id = self.hold_finding(rule, 1)
        ledger = self.ledger()
        ledger.waive_held(finding_id, style_ledger.WaiveByUnit(ledger.named_held((finding_id,))[0].ticket))
        before = self.held_rows(ledger.database)
        self.assertEqual(self.hold_finding(rule, 1), finding_id)
        self.assertEqual(self.held_rows(ledger.database), before)
        _ = self.cli("held", "replace", "--id", finding_id, "--after", "corrected")
        self.assertEqual(ledger.named_held((finding_id,))[0].after, "corrected")


    def test_hold_of_a_different_change_at_a_held_site_gets_its_own_row(self) -> None:
        rule = self.rule("change-ids")
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        blob = self.blob(rule)
        change: tuple[style_ledger.FindingKind, str, str] = ("rename", "old", "new")
        others: tuple[tuple[str, tuple[style_ledger.FindingKind, str, str]], ...] = (
            ("after", ("rename", "old", "other")), ("kind", ("type", "old", "new")),
            ("before", ("rename", "older", "new")))

        def hold(line: int, held: tuple[style_ledger.FindingKind, str, str]) -> style_ledger.HeldFinding:
            return ledger.hold(rule, self.sources[0], line, "value", *held, blob, "outside")

        def finding_id(fields: tuple[str | int, ...]) -> str:
            return hashlib.sha256(json.dumps(fields, separators=(",", ":")).encode()).hexdigest()[:16]

        for line, (name, other) in enumerate(others, 1):
            with self.subTest(differs=name):
                site = (rule, self.sources[0], line, "value", blob)
                # The same change twice is one row at today's id, so a row from before this change keeps its id.
                today = finding_id(site)
                self.assertEqual(hold(line, change).finding_id, today)
                rows = self.held_rows(ledger.database)
                self.assertEqual(hold(line, change).finding_id, today)
                self.assertEqual(self.held_rows(ledger.database), rows)
                second = hold(line, other)
                self.assertEqual((second.finding_id, second.kind, second.before, second.after),
                                 (finding_id((*site, *other)), *other))
                count = len(self.held_rows(ledger.database))
                self.assertEqual((hold(line, change).finding_id, hold(line, other).finding_id),
                                 (today, second.finding_id))
                self.assertEqual(len(self.held_rows(ledger.database)), count)
                kept, = ledger.named_held((today,))
                self.assertEqual((kept.kind, kept.before, kept.after), change)


    def test_hold_returns_its_change_when_another_writer_changes_the_row_after_the_commit(self) -> None:
        rule = self.rule("change-race")
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        held = cast(Callable[[style_ledger.Ledger, str], style_ledger.HeldFinding],
                    getattr(style_ledger.Ledger, "_held"))
        waiting: list[str] = []

        def held_with_another_writer(self_: style_ledger.Ledger, finding_id: str) -> style_ledger.HeldFinding:
            # Another writer changes the row now if no lock stops it, else right after the commit.
            try:
                with closing(sqlite3.connect(ledger.database, timeout=0)) as connection, connection:
                    _ = connection.execute("UPDATE held_findings SET after='other' WHERE finding_id=?",
                                           (finding_id,))
            except sqlite3.OperationalError:
                waiting.append(finding_id)
            return held(self_, finding_id)

        def hold() -> style_ledger.HeldFinding:
            return ledger.hold(rule, self.sources[0], 1, "value", "rename", "old", "new", self.blob(rule), "outside")

        first = hold()
        # The second call returns the existing row; the third inserts a row at the second id.
        for path in ("existing row", "insert"):
            with self.subTest(path=path):
                with mock.patch.object(style_ledger.Ledger, "_held", held_with_another_writer):
                    finding = hold()
                self.assertEqual(finding.after, "new")
                self.assertEqual(len(waiting), 1)
                with closing(sqlite3.connect(ledger.database)) as connection, connection:
                    _ = connection.execute("UPDATE held_findings SET after='other' WHERE finding_id=?",
                                           (waiting.pop(),))
                self.assertEqual(ledger.named_held((finding.finding_id,))[0].after, "other")
        self.assertEqual(len(self.held_rows(ledger.database)), 2)
        self.assertEqual(ledger.named_held((first.finding_id,))[0].after, "other")


    def test_held_replace_refuses_an_unknown_id(self) -> None:
        result = self.cli("held", "replace", "--id", "missing", "--after", "new", success=False)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), "style_ledger: unknown held finding missing")


    def test_decline_keeps_a_nonempty_actor_when_it_overrides_a_unit_waive(self) -> None:
        rule = self.rule("decline-actor")
        self.set_tip(self.initial_tip)
        finding_id = self.hold_finding(rule, 1)
        ledger = self.ledger()
        finding, = ledger.named_held((finding_id,))
        ledger.waive_held(finding_id, style_ledger.WaiveByUnit(finding.ticket))
        _ = ledger.decline_findings((finding_id,), "Keep this name", style_ledger.WaiveByPerson("user"))
        with closing(sqlite3.connect(ledger.database)) as connection:
            self.assertEqual(connection.execute("SELECT waive_provenance,waived_by FROM held_findings WHERE finding_id=?",
                                                (finding_id,)).fetchone(), ("person", "user"))
        self.assertEqual(ledger.named_held((finding_id,))[0].waive_actor, style_ledger.WaiveByPerson("user"))


    def test_decline_rejects_a_unit_and_keeps_person_and_ledger_waives_protected(self) -> None:
        rule = self.rule("decline-protection")
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        for line, actor in enumerate((style_ledger.WaiveByPerson("ana"), style_ledger.WaiveByLedger()), 1):
            with self.subTest(actor=actor):
                finding_id = self.hold_finding(rule, line)
                finding, = ledger.named_held((finding_id,))
                ledger.waive_held(finding_id, actor)
                before = ledger.database.read_bytes()
                invalid_actor = cast(style_ledger.WaiveByPerson | style_ledger.WaiveByLedger,
                                     cast(object, style_ledger.WaiveByUnit(finding.ticket)))
                with self.assertRaisesRegex(style_ledger.LedgerError, "decline requires a person or the ledger"):
                    _ = ledger.decline_findings((finding_id,), "Keep this name", invalid_actor)
                self.assertEqual(ledger.database.read_bytes(), before)
                _ = ledger.decline_findings((finding_id,), "Keep this name", actor)
                self.assertEqual(ledger.named_held((finding_id,))[0].waive_actor, actor)
                before = ledger.database.read_bytes()
                with self.assertRaisesRegex(style_ledger.LedgerError, finding_id):
                    _ = ledger.replace_held(finding_id, "corrected")
                self.assertEqual(ledger.database.read_bytes(), before)


    def test_held_replace_refuses_a_ledger_waive_without_writing(self) -> None:
        rule = self.rule("ledger-waive")
        self.set_tip(self.initial_tip)
        finding_id = self.hold_finding(rule, 1)
        ledger = self.ledger()
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE held_findings SET state='waived',waive_provenance='ledger',waived_by='ledger' WHERE finding_id=?",
                                   (finding_id,))
        for person in ((), ("--person", "ana")):
            with self.subTest(person=person):
                before = ledger.database.read_bytes()
                result = self.cli("held", "replace", "--id", finding_id, "--after", "corrected", *person, success=False)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(finding_id, result.stderr)
                self.assertIn("ledger", result.stderr)
                self.assertEqual(ledger.database.read_bytes(), before)


    def test_held_replace_preserves_person_waives_and_replaces_only_a_unit_waive(self) -> None:
        rule = self.rule("waive-origin")
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        person_id = self.hold_finding(rule, 1)
        issue = self.root / "declined.md"
        ledger.file_held(person_id, issue)
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE held_findings SET person_read_by='ana' WHERE finding_id=?", (person_id,))
        self.record(rule, self.sources[0], self.initial_tip, "held")
        _ = ledger.decline_findings((person_id,), "Keep the old name", style_ledger.WaiveByPerson("user"))
        # A declined decision can also waive a new, unfiled finding before any judgment exists.
        unfiled_id = self.hold_finding(rule, 2)
        _ = ledger.decline_findings((unfiled_id,), "Keep this name too", style_ledger.WaiveByPerson("user"))
        for finding_id in (person_id, unfiled_id):
            with self.subTest(finding_id=finding_id):
                before = ledger.database.read_bytes()
                result = self.cli("held", "replace", "--id", finding_id, "--after", "corrected", success=False)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(finding_id, result.stderr)
                self.assertIn("person", result.stderr)
                self.assertEqual(ledger.database.read_bytes(), before)
        unit_id = self.hold_finding(rule, 3)
        ledger.waive_held(unit_id, style_ledger.WaiveByUnit("external-1"))
        result = self.cli("held", "replace", "--id", unit_id, "--after", "corrected")
        self.assertEqual(result.stdout, unit_id + "\n")
        unit, = ledger.named_held((unit_id,))
        self.assertEqual((unit.after, unit.state), ("corrected", style_ledger.Unfiled()))


    def test_held_waive_records_the_caller_and_replace_protects_a_person(self) -> None:
        rule = self.rule("explicit-waive")
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        person_id = self.hold_finding(rule, 1)
        _ = self.cli("held", "waive", "--id", person_id, "--person", "ana")
        person, = ledger.named_held((person_id,))
        self.assertEqual(person.waive_actor, style_ledger.WaiveByPerson("ana"))
        for flags in ((), ("--person", "ana")):
            with self.subTest(flags=flags):
                before = ledger.database.read_bytes()
                result = self.cli("held", "replace", "--id", person_id, "--after", "corrected", *flags, success=False)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(person_id, result.stderr)
                self.assertIn("person", result.stderr)
                self.assertEqual(ledger.database.read_bytes(), before)
        unit_id = self.hold_finding(rule, 2)
        # The caller supplies the ticket even when the finding has already been filed.
        ledger.file_held(unit_id, self.root / "unit-waive.md")
        _ = self.cli("held", "waive", "--id", unit_id, "--ticket", "external-1")
        unit, = ledger.named_held((unit_id,))
        self.assertEqual(unit.waive_actor, style_ledger.WaiveByUnit("external-1"))
        _ = self.cli("held", "replace", "--id", unit_id, "--after", "corrected")
        replaced, = ledger.named_held((unit_id,))
        self.assertEqual((replaced.finding_id, replaced.after, replaced.state),
                         (unit_id, "corrected", style_ledger.Unfiled()))


    def test_held_waive_requires_exactly_one_nonblank_actor(self) -> None:
        rule = self.rule("waive-actor")
        finding_id = self.hold_finding(rule, 1)
        ledger = self.ledger()
        for flags in ((), ("--ticket", "outside", "--person", "ana"),
                      ("--ticket", " "), ("--person", " "), ("--ticket", "another-ticket")):
            with self.subTest(flags=flags):
                before = ledger.database.read_bytes()
                result = self.cli("held", "waive", "--id", finding_id, *flags, success=False)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                if not flags:
                    self.assertIn("one of the arguments --ticket --person is required", result.stderr)
                elif "--ticket" in flags and "--person" in flags:
                    self.assertIn("not allowed with argument --ticket", result.stderr)
                elif flags[-1] == "another-ticket":
                    self.assertIn("did not judge this finding", result.stderr)
                else:
                    self.assertIn("requires a", result.stderr)
                self.assertEqual(ledger.database.read_bytes(), before)


    def test_a_persons_explicit_waive_protects_an_existing_unit_waive(self) -> None:
        rule = self.rule("repeat-waive")
        finding_id = self.hold_finding(rule, 1)
        ledger = self.ledger()
        ledger.waive_held(finding_id, style_ledger.WaiveByUnit("external-1"))
        ledger.waive_held(finding_id, style_ledger.WaiveByPerson("ana"))
        before = ledger.database.read_bytes()
        ledger.waive_held(finding_id, style_ledger.WaiveByUnit("external-1"))
        self.assertEqual(ledger.database.read_bytes(), before)
        finding, = ledger.named_held((finding_id,))
        self.assertEqual(finding.waive_actor, style_ledger.WaiveByPerson("ana"))
        with self.assertRaisesRegex(style_ledger.LedgerError, finding_id + ".*person"):
            _ = ledger.replace_held(finding_id, "corrected")
        self.assertEqual(ledger.database.read_bytes(), before)


    def test_held_replace_requires_and_records_a_person_for_an_unrecorded_waive(self) -> None:
        rule = self.rule("legacy-replace")
        self.set_tip(self.initial_tip)
        finding_id = self.hold_finding(rule, 1)
        ledger = self.ledger()
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE held_findings SET state='waived',waive_provenance='unrecorded' WHERE finding_id=?",
                                   (finding_id,))
        self.record(rule, self.sources[0], self.initial_tip, "waived")
        for flags in ((), ("--person", " ")):
            with self.subTest(flags=flags):
                before = ledger.database.read_bytes()
                result = self.cli("held", "replace", "--id", finding_id, "--after", "corrected", *flags, success=False)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(ledger.database.read_bytes(), before)
        result = self.cli("held", "replace", "--id", finding_id, "--after", "corrected", "--person", "ana")
        self.assertEqual(result.stdout, finding_id + "\n")
        finding, = ledger.named_held((finding_id,))
        self.assertEqual((finding.finding_id, finding.after, finding.state, finding.waive_undo),
                         (finding_id, "corrected", style_ledger.Unfiled(), style_ledger.WaiveUndoneBy("ana")))
        self.assertEqual(ledger.latest_held_judgments(), {(rule, self.sources[0]): self.initial_tip})


    def test_held_replace_refuses_each_non_waived_state_without_writes(self) -> None:
        _, _, ids = self.decision_findings()
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        for state in ("unfiled", "filed", "built", "withdrawn"):
            with self.subTest(state=state):
                with closing(sqlite3.connect(ledger.database)) as connection, connection:
                    _ = connection.execute("UPDATE held_findings SET state=? WHERE finding_id=?", (state, ids[0]))
                before = self.held_rows(ledger.database)
                result = self.cli("held", "replace", "--id", ids[0], "--after", "new", success=False)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stderr.strip(), f"style_ledger: {ids[0]}: cannot replace a {state} finding")
                self.assertEqual(self.held_rows(ledger.database), before)


    def test_held_replace_refuses_legacy_waives_without_unit_provenance(self) -> None:
        rule = self.rule("legacy-waive")
        self.set_tip(self.initial_tip)
        finding_id = self.hold_finding(rule, 1)
        ledger = self.ledger()
        ledger.waive_held(finding_id, style_ledger.WaiveByUnit(ledger.named_held((finding_id,))[0].ticket))
        database = ledger.database
        ledger.close()
        with closing(sqlite3.connect(database)) as connection, connection:
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN waive_provenance")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN waived_by")
            _ = connection.execute("DROP TABLE ticket_widened_paths")
            _ = connection.execute("ALTER TABLE tickets DROP COLUMN reservation_session_dir")
            _ = connection.execute("ALTER TABLE tickets DROP COLUMN reservation_phase")
            _ = connection.execute("ALTER TABLE processed_tips DROP COLUMN reason")
            _ = connection.execute("DROP TABLE refill_results")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN built_at")
            _ = connection.execute("UPDATE schema_version SET version=18")
        migrated = self.ledger()
        finding, = migrated.named_held((finding_id,))
        self.assertEqual(finding.waive_provenance, style_ledger.WaiveProvenance.UNRECORDED)
        before = database.read_bytes()
        with self.assertRaisesRegex(style_ledger.LedgerError, finding_id + ".*unrecorded"):
            _ = migrated.replace_held(finding_id, "corrected")
        self.assertEqual(database.read_bytes(), before)
        migrated.waive_held(finding_id, style_ledger.WaiveByUnit("external-1"))
        self.assertEqual(migrated.named_held((finding_id,))[0].waive_provenance, style_ledger.WaiveProvenance.UNRECORDED)


    def test_held_replace_does_not_create_or_overwrite_other_cell_verdicts(self) -> None:
        rule = self.rule("replace")
        self.set_tip(self.initial_tip)
        finding_id = self.hold_finding(rule, 1)
        ledger = self.ledger()
        for verdict in ("missing", "clean", "exception"):
            with self.subTest(verdict=verdict):
                if verdict != "missing":
                    self.record(rule, self.sources[0], self.initial_tip, verdict)
                ledger.waive_held(finding_id, style_ledger.WaiveByUnit(ledger.named_held((finding_id,))[0].ticket))
                _ = self.cli("held", "replace", "--id", finding_id, "--after", "Outcome::Correct")
                self.assertEqual(ledger.latest_held_judgments(), {})
                self.assertEqual(self.states(rule)[self.sources[0]], "open" if verdict == "missing" else "current")


    def test_held_add_of_a_built_row_names_reopen_only_when_its_rename_is_not_in_the_code(self) -> None:
        rule, issue, ids = self.decision_findings()
        self.write(self.sources[0], "pub enum Outcome { New }\n")
        self.write(self.sources[1], "pub enum Outcome { Old }\n")
        tip = self.commit("Rename one of two")
        ledger = self.ledger()
        ledger.set_tip(tip, "main")
        # Merges before the applied check marked each finding built without reading the code.
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE held_findings SET state='built'")
        before = self.held_rows(ledger.database)
        hint = f"built, but not in the code at {tip[:7]}: run style_proposals.py reopen --all (issue {issue})\n"
        for index, (finding_id, notice) in enumerate(((ids[0], ""), (ids[1], hint))):
            result = self.cli(
                "held", "add", "--rule", rule, "--path", self.sources[index], "--line", str(index + 1),
                "--text", "required change", "--kind", "rename", "--before", "Outcome::Old",
                "--after", "Outcome::New", "--rule-blob", self.blob(rule), "--ticket", "outside",
            )
            self.assertEqual((result.stdout, result.stderr), (finding_id + "\n", notice))
        self.assertEqual(self.held_rows(ledger.database), before)
        self.assertEqual([finding.state for finding in self.ledger().proposal_findings(issue)],
                         [style_ledger.Built(issue), style_ledger.Built(issue)])


    def test_held_add_of_a_built_row_that_a_person_read_names_no_reopen(self) -> None:
        rule, issue, ids = self.decision_findings()
        self.write(self.sources[1], "pub enum Outcome { Old }\n")
        tip = self.commit("Keep Old")
        ledger = self.ledger()
        ledger.set_tip(tip, "main")
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE held_findings SET state='built'")
        ledger.record_person_applied({ids[1]: style_ledger.ReadByPerson("nate")})
        result = self.cli(
            "held", "add", "--rule", rule, "--path", self.sources[1], "--line", "2", "--text", "required change",
            "--kind", "rename", "--before", "Outcome::Old", "--after", "Outcome::New", "--rule-blob", self.blob(rule),
            "--ticket", "outside",
        )
        self.assertEqual((result.stdout, result.stderr), (ids[1] + "\n", ""))
        self.assertEqual(self.ledger().proposal_findings(issue)[1].reading, style_ledger.ReadByPerson("nate"))


    def test_a_ticket_merge_that_finds_the_rename_applied_clears_a_person_reading(self) -> None:
        rule, issue, ids = self.decision_findings()
        ledger = self.ledger()
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE held_findings SET state='built' WHERE finding_id=?", (ids[0],))
        ledger.return_to_filed({ids[0]: style_ledger.ReadByPerson("nate")})
        self.assertEqual(ledger.proposal_findings(issue)[0].reading, style_ledger.ReadByPerson("nate"))
        ledger.ticket_add("names-1", "names", "proposal", "names", "crates/alpha", [self.sources[0]], [rule],
                          proposal=str(issue), finding_ids=ids[:1])
        ledger.ticket_assign("names-1", "main")
        self.write(self.sources[0], "pub enum Outcome { New }\n")
        checkpoint = self.commit("Rename Old to New")
        ledger.set_tip(checkpoint, "main")
        ledger.ticket_checkpoint("names-1", checkpoint)
        self.assertEqual(ledger.ticket_merge("names-1"), ())
        held = ledger.proposal_findings(issue)[0]
        self.assertEqual((held.finding_id, held.state, held.reading),
                         (ids[0], style_ledger.Built(issue), style_ledger.NotReadByPerson()))


    def test_held_built_refuses_a_finding_with_no_merged_build_ticket(self) -> None:
        _, issue, ids = self.decision_findings()
        result = self.cli("held", "built", "--id", ids[0], success=False)
        self.assertEqual((result.returncode, result.stderr.strip()), (2, f"style_ledger: {ids[0]}: no merged build ticket"))
        self.assertEqual(self.ledger().proposal_findings(issue)[0].state, style_ledger.Filed(issue))


    def test_held_built_refuses_a_rename_not_applied_at_its_build_ticket_checkpoint(self) -> None:
        rule, issue, ids = self.decision_findings()
        ledger = self.ledger()
        ledger.ticket_add("names-1", "names", "proposal", "names", "crates/alpha", [self.sources[1]], [rule],
                          proposal=str(issue), finding_ids=ids[1:])
        ledger.ticket_assign("names-1", "main")
        self.write(self.sources[1], "pub enum Outcome { Old }\n")
        checkpoint = self.commit("Keep Old")
        ledger.set_tip(checkpoint, "main")
        ledger.ticket_checkpoint("names-1", checkpoint)
        _ = ledger.ticket_merge("names-1")
        with self.assertRaisesRegex(style_ledger.LedgerError, re.escape(f"{ids[1]}: no merged build ticket")):
            ledger.mark_built(ids[1])
        # A row that names a merged ticket at whose checkpoint the rename is not applied.
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE held_findings SET build_ticket='names-1' WHERE finding_id=?", (ids[1],))
        with self.assertRaisesRegex(style_ledger.LedgerError,
                                    re.escape(f"{ids[1]}: rename not applied in {self.sources[1]} at names-1 checkpoint")):
            ledger.mark_built(ids[1])
        self.assertEqual(ledger.proposal_findings(issue)[1].state, style_ledger.Filed(issue))


    def test_held_built_at_a_merge_refuses_a_commit_off_the_synced_branch(self) -> None:
        _, issue, ids = self.decision_findings()
        self.write(self.sources[0], "pub enum Outcome { New }\n")
        tip = self.commit("Rename Old to New")
        self.ledger().set_tip(self.initial_tip, "main")
        for merge, error in ((tip, f"{tip} is not on the synced branch at {self.initial_tip[:7]}"), ("missing", "missing: no such commit")):
            with self.subTest(merge=merge):
                result = self.cli("held", "built", "--id", ids[0], "--commit", merge, success=False)
                self.assertEqual((result.returncode, result.stderr.strip()), (2, f"style_ledger: {error}"))
        self.assertEqual(self.ledger().proposal_findings(issue)[0].state, style_ledger.Filed(issue))


    def test_held_built_at_a_merge_refuses_a_merge_older_than_the_commit_the_finding_was_judged_at(self) -> None:
        rule, _, ids = self.decision_findings()
        self.write(self.sources[2], "pub fn judged() {}\n")
        judged = self.commit("Judge the findings")
        ledger = self.ledger()
        ledger.set_tip(judged, "main")
        ledger.ticket_add("names-1", "names", "read", "names", "crates/alpha", [self.sources[0]], [rule])
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE tickets SET checkpoint=? WHERE id='names-1'", (judged,))
            _ = connection.execute("UPDATE held_findings SET ticket='names-1' WHERE finding_id=?", (ids[0],))
        before = self.held_rows(ledger.database)
        result = self.cli("held", "built", "--id", ids[0], "--commit", self.initial_tip, "--by", "nate", success=False)
        self.assertEqual((result.returncode, result.stderr.strip()),
                         (2, f"style_ledger: {ids[0]}: {self.initial_tip} is older than {judged[:7]}, where the finding was judged"))
        self.assertEqual(self.held_rows(ledger.database), before)


    def test_held_built_at_a_merge_refuses_every_id_when_one_is_refused(self) -> None:
        rule, issue, ids = self.decision_findings()
        self.write(self.sources[0], "pub enum Outcome { New }\n")
        self.write(self.sources[1], "pub enum Outcome { Old }\n")
        tip = self.commit("Rename one of two")
        ledger = self.ledger()
        ledger.set_tip(tip, "main")
        third = ledger.hold(rule, self.sources[2], 1, "required change", "type", "u32", "u64", self.blob(rule), "outside")
        ledger.file_held(third.finding_id, issue)
        ledger.ticket_add("names-1", "names", "proposal", "names", "crates/beta", [self.sources[2]], [rule],
                          proposal=str(issue), finding_ids=(third.finding_id,))
        before = self.held_rows(ledger.database)
        for names, error in (((ids[0], ids[1]), f"{ids[1]}: rename not applied in {self.sources[1]} at {tip}"),
                             ((ids[0], "zzzz"), "no finding zzzz"),
                             ((ids[0], third.finding_id), f"{third.finding_id}: build ticket names-1 still holds the finding")):
            with self.subTest(names=names):
                result = self.cli("held", "built", *(part for name in names for part in ("--id", name)), "--commit", tip,
                                  "--by", "nate", success=False)
                self.assertEqual((result.returncode, result.stderr.strip()), (2, f"style_ledger: {error}"))
                self.assertEqual(self.held_rows(ledger.database), before)


    def test_held_built_at_a_merge_marks_an_applied_rename_built_and_clears_a_reading(self) -> None:
        _, issue, ids = self.decision_findings()
        self.write(self.sources[0], "pub enum Outcome { New }\n")
        tip = self.commit("Rename Old to New")
        ledger = self.ledger()
        ledger.set_tip(tip, "main")
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE held_findings SET state='built' WHERE finding_id=?", (ids[0],))
        ledger.return_to_filed({ids[0]: style_ledger.ReadByPerson("nate")})
        result = self.cli("held", "built", "--id", ids[0][:8], "--commit", tip, "--by", "ana")
        self.assertEqual(result.stdout, f"built {ids[0]} at {tip[:7]}\n")
        held = {finding.finding_id: (finding.state, finding.reading) for finding in self.ledger().proposal_findings(issue)}
        self.assertEqual(held, {ids[0]: (style_ledger.Built(issue), style_ledger.NotReadByPerson()),
                                ids[1]: (style_ledger.Filed(issue), style_ledger.NotReadByPerson())})


    def test_held_built_at_a_merge_needs_a_person_for_a_type_finding_and_records_who_read_it(self) -> None:
        rule, issue, _ = self.decision_findings()
        ledger = self.ledger()
        ledger.set_tip(self.initial_tip, "main")
        finding = ledger.hold(rule, self.sources[2], 1, "value", "type", "u32", "u64", self.blob(rule), "outside")
        ledger.file_held(finding.finding_id, issue)
        refusal = self.cli("held", "built", "--id", finding.finding_id, "--commit", "main", success=False)
        self.assertEqual(refusal.stderr.strip(), f"style_ledger: {finding.finding_id}: type has no applied check at main; "
                         + "pass --by after reading the code")
        for arguments, error in ((("--commit", "main", "--by", "  "), "held built --by requires a name"),
                                 (("--by", "nate"), "held built --by requires --commit"),
                                 (("--id", finding.finding_id), "held built takes more than one --id only with --commit"),
                                 (("--id", " ", "--commit", "main"), "held built --id requires a finding id")):
            with self.subTest(arguments=arguments):
                result = self.cli("held", "built", "--id", finding.finding_id, *arguments, success=False)
                self.assertEqual(result.returncode, 2)
                self.assertIn(error, result.stderr)
        self.assertEqual(next(item for item in self.ledger().proposal_findings(issue) if item.finding_id == finding.finding_id).state, style_ledger.Filed(issue))
        result = self.cli("held", "built", "--id", finding.finding_id, "--commit", "main", "--by", " nate ")
        self.assertEqual(result.stdout, f"built {finding.finding_id} at {self.initial_tip[:7]}, read by nate\n")
        held = next(item for item in self.ledger().proposal_findings(issue) if item.finding_id == finding.finding_id)
        self.assertEqual((held.state, held.reading), (style_ledger.Built(issue), style_ledger.ReadByPerson("nate")))


    def test_held_built_at_a_merge_refuses_a_merge_without_the_build_ticket_checkpoint(self) -> None:
        ledger, finding_id, _, checkpoint = self.checkpointed_type_findings()
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE tickets SET finding_ids='[]' WHERE id='names-1'")
        before = self.held_rows(ledger.database)
        result = self.cli("held", "built", "--id", finding_id, "--commit", self.initial_tip, "--by", "nate",
                          success=False)
        self.assertEqual((result.returncode, result.stderr.strip()),
                         (2, f"style_ledger: {finding_id}: {self.initial_tip} does not contain names-1's checkpoint "
                          + checkpoint[:7]))
        self.assertEqual(self.held_rows(ledger.database), before)
        result = self.cli("held", "built", "--id", finding_id, "--commit", checkpoint, "--by", "nate")
        self.assertEqual(result.stdout, f"built {finding_id} at {checkpoint[:7]}, read by nate\n")


    def test_held_built_at_a_merge_refuses_a_merge_without_a_listing_ticket_checkpoint(self) -> None:
        ledger, finding_id, _, checkpoint = self.checkpointed_type_findings()
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE held_findings SET build_ticket='' WHERE finding_id=?", (finding_id,))
        before = self.held_rows(ledger.database)
        result = self.cli("held", "built", "--id", finding_id, "--commit", self.initial_tip, "--by", "nate",
                          success=False)
        self.assertEqual((result.returncode, result.stderr.strip()),
                         (2, f"style_ledger: {finding_id}: {self.initial_tip} does not contain names-1's checkpoint "
                          + checkpoint[:7]))
        self.assertEqual(self.held_rows(ledger.database), before)


    def test_held_built_at_a_merge_rereads_the_linked_tickets_under_the_write_lock(self) -> None:
        ledger, finding_id, _, checkpoint = self.checkpointed_type_findings()
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE tickets SET state='assigned',checkpoint='' WHERE id='names-1'")
            _ = connection.execute("UPDATE held_findings SET build_ticket='' WHERE finding_id=?", (finding_id,))
        # Another writer checkpoints and merges the linked ticket after the checkpoint check read it.
        self.assert_refused_after_a_write_during_the_check(
            ledger, finding_id, "UPDATE tickets SET state='merged',checkpoint=? WHERE id='names-1'", (checkpoint,),
            f"{finding_id}: {self.initial_tip} does not contain names-1's checkpoint {checkpoint[:7]}",
        )


    def test_held_built_at_a_merge_rereads_the_judging_ticket_under_the_write_lock(self) -> None:
        ledger, finding_id, judged = self.judged_type_finding()
        # The judging ticket checkpoints at a later commit after the judged-commit check read its start.
        self.assert_refused_after_a_write_during_the_check(
            ledger, finding_id, "UPDATE tickets SET checkpoint=? WHERE id='names-1'", (judged,),
            f"{finding_id}: {self.initial_tip} is older than {judged[:7]}, where the finding was judged",
        )


    def test_held_built_at_a_merge_decides_from_the_rows_it_read(self) -> None:
        ledger, finding_id, judged = self.judged_type_finding()
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE tickets SET checkpoint=? WHERE id='names-1'", (judged,))
        before = self.held_rows(ledger.database)
        judged_commit = style_ledger.Ledger.judged_commit

        def judged_while_another_writer_flips(self_: style_ledger.Ledger, finding: style_ledger.HeldFinding,
                                              ) -> style_ledger.JudgedCommit | style_ledger.NoJudgedCommit:
            # Another writer clears the checkpoint for this one read and puts it back before the write lock.
            with closing(sqlite3.connect(ledger.database)) as connection, connection:
                _ = connection.execute("UPDATE tickets SET checkpoint='' WHERE id='names-1'")
            try:
                return judged_commit(self_, finding)
            finally:
                with closing(sqlite3.connect(ledger.database)) as connection, connection:
                    _ = connection.execute("UPDATE tickets SET checkpoint=? WHERE id='names-1'", (judged,))

        with (mock.patch.object(style_ledger.Ledger, "judged_commit", judged_while_another_writer_flips),
              self.assertRaisesRegex(style_ledger.LedgerError, re.escape(
                  f"{finding_id}: {self.initial_tip} is older than {judged[:7]}, where the finding was judged"))):
            _ = ledger.mark_built_at_merge((finding_id,), self.initial_tip, style_ledger.ReadByPerson("nate"))
        self.assertEqual(self.held_rows(ledger.database), before)


    def test_held_built_at_a_merge_ignores_the_checkpoint_of_a_dropped_ticket(self) -> None:
        ledger, finding_id, _, _ = self.checkpointed_type_findings()
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE tickets SET state='dropped' WHERE id='names-1'")
        result = self.cli("held", "built", "--id", finding_id, "--commit", self.initial_tip, "--by", "nate")
        self.assertEqual(result.stdout, f"built {finding_id} at {self.initial_tip[:7]}, read by nate\n")


    def test_held_built_at_a_merge_writes_no_id_when_one_lacks_a_checkpoint(self) -> None:
        ledger, finding_id, other_id, checkpoint = self.checkpointed_type_findings()
        before = self.held_rows(ledger.database)
        result = self.cli("held", "built", "--id", finding_id, "--id", other_id, "--commit", self.initial_tip,
                          "--by", "nate", success=False)
        self.assertEqual((result.returncode, result.stderr.strip()),
                         (2, f"style_ledger: {finding_id}: {self.initial_tip} does not contain names-1's checkpoint "
                          + checkpoint[:7]))
        self.assertEqual(self.held_rows(ledger.database), before)


    def test_unwaive_moves_waived_findings_to_unfiled_with_no_move_and_the_held_list_shows_who(self) -> None:
        _, issue, ids = self.decision_findings()
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        ledger.move_issue_findings(issue, {ids[0]: self.root / "moved.md"})
        for finding_id in ids:
            ledger.waive_held(finding_id, style_ledger.WaiveByPerson("nate"))
        person = style_ledger.WaiveUndoneBy("nate")
        self.assertEqual(ledger.unwaive_held((ids[0][:8], ids[1], ids[1][:6]), person), tuple(sorted(ids)))
        unfiled = ledger.held_findings(style_ledger.HeldFindingsInState("unfiled"))
        self.assertEqual({(finding.finding_id, finding.state, finding.waive_undo) for finding in unfiled},
                         {(finding_id, style_ledger.Unfiled(), person) for finding_id in ids})
        self.assertEqual(ledger.proposal_findings(issue), ())
        # A resumed migrate does not take the finding again with an empty decision.
        self.assertEqual(ledger.moved_findings(issue), {})
        listed = cast(list[dict[str, object]], json.loads(self.cli("held", "list", "--json").stdout))
        self.assertEqual({(finding["finding_id"], finding["state"], finding["unwaived_by"]) for finding in listed},
                         {(finding_id, "unfiled", "nate") for finding_id in ids})
        lines = {line.split("\t")[0]: line.split("\t")[3:] for line in self.cli("held", "list").stdout.splitlines()}
        self.assertEqual(lines, {finding_id: ["unfiled", "unwaived by nate"] for finding_id in ids})
        # A new waive keeps the record of who undid the old one.
        ledger.waive_held(ids[0], style_ledger.WaiveByPerson("nate"))
        self.assertEqual(ledger.held_findings(style_ledger.HeldFindingsInState("waived"))[0].waive_undo, person)


    def test_unwaive_holds_the_cell_that_a_decline_waived_so_a_second_decline_waives_it_again(self) -> None:
        rule, _, ids = self.decision_findings()
        self.set_tip(self.initial_tip)
        for path in self.sources[:2]:
            self.record(rule, path, self.initial_tip, "held")
        ledger = self.ledger()
        self.assertIsInstance(ledger.decline_findings(ids[:1], "Keep the name", style_ledger.WaiveByPerson("user"))[ids[0]], style_ledger.CellWaived)
        self.assertEqual(next(row for row in ledger.rule_counts(style_ledger.WholeRepository()) if row.rule_id == rule).held, 1)
        _ = ledger.unwaive_held(ids[:1], style_ledger.WaiveUndoneBy("nate"))
        self.assertEqual(next(row for row in ledger.rule_counts(style_ledger.WholeRepository()) if row.rule_id == rule).held, 2)
        self.assertIn((rule, self.sources[0]), ledger.latest_held_judgments())
        self.assertIsInstance(ledger.decline_findings(ids[:1], "Keep the name", style_ledger.WaiveByPerson("user"))[ids[0]], style_ledger.CellWaived)
        # The new waive keeps the record of who undid the old one.
        waived, = ledger.held_findings(style_ledger.HeldFindingsInState("waived"))
        self.assertEqual((waived.finding_id, waived.waive_undo), (ids[0], style_ledger.WaiveUndoneBy("nate")))


    def test_unwaive_refuses_a_finding_judged_under_an_older_rule_text(self) -> None:
        rule, _, ids = self.decision_findings()
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        ledger.waive_held(ids[0], style_ledger.WaiveByPerson("nate"))
        definition = self.rule_dir / "decision.md"
        _ = definition.write_text(definition.read_text() + "New rule text.\n")
        self.assertNotEqual(self.blob(rule), ledger.held_findings(style_ledger.HeldFindingsInState("waived"))[0].rule_blob)
        before = self.held_rows(ledger.database)
        with self.assertRaisesRegex(style_ledger.LedgerError, re.escape(f"{ids[0]}: rule changed since judged")):
            _ = ledger.unwaive_held(ids[:1], style_ledger.WaiveUndoneBy("nate"))
        self.assertEqual(self.held_rows(ledger.database), before)


    def test_unwaive_refuses_every_id_when_one_is_refused(self) -> None:
        rule, issue, ids = self.decision_findings()
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        ledger.waive_held(ids[0], style_ledger.WaiveByPerson("nate"))
        built = ledger.hold(rule, self.sources[2], 1, "required change", "type", "u32", "u64", self.blob(rule), "outside")
        ledger.file_held(built.finding_id, issue)
        ledger.ticket_add("names-1", "names", "proposal", "names", "crates/beta", [self.sources[2]], [rule],
                          proposal=str(issue), finding_ids=(built.finding_id,))
        ledger.waive_held(built.finding_id, style_ledger.WaiveByPerson("nate"))
        ledger.ticket_add("read-1", "read", "read", "names", "crates/beta", [self.sources[3]], [rule])
        ledger.ticket_assign("read-1", "main")
        judged = ledger.hold(rule, self.sources[3], 1, "required change", "type", "u32", "u64", self.blob(rule), "read-1")
        ledger.waive_held(judged.finding_id, style_ledger.WaiveByPerson("nate"))
        twin = ids[1][:8] + "0" * 8
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("INSERT INTO held_findings SELECT ?,rule,path,line,text,kind,before,after,rule_blob,ticket,"
                                   + "state,proposal,created_at,updated_at,build_ticket,moved_from,withdrawn_by,person_read_by,"
                                   + "duplicate_of,unwaived_by,followed_from,followed_from_line,followed_at,declaration_gone_at,waive_provenance,waived_by,built_at FROM held_findings "
                                   + "WHERE finding_id=?", (twin, ids[1]))
        before = self.held_rows(ledger.database)
        for name, error in (
            (ids[1], f"{ids[1]}: cannot unwaive a filed finding"),
            ("zzzz", "no finding zzzz"),
            (ids[1][:8], f"{ids[1][:8]} matches 2 findings"),
            (built.finding_id, f"{built.finding_id}: build ticket names-1 still holds the finding"),
            (judged.finding_id, f"{judged.finding_id}: assigned ticket read-1 still holds the finding"),
        ):
            with self.subTest(name=name), self.assertRaisesRegex(style_ledger.LedgerError, re.escape(error)):
                _ = ledger.unwaive_held((ids[0], name), style_ledger.WaiveUndoneBy("nate"))
            self.assertEqual(self.held_rows(ledger.database), before)


    def test_an_unwaived_finding_with_a_dropped_build_ticket_files_into_a_proposal_again(self) -> None:
        rule, issue, ids = self.decision_findings()
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        ledger.ticket_add("names-1", "names", "proposal", "names", "crates/alpha", [self.sources[0]], [rule],
                          proposal=str(issue), finding_ids=ids[:1])
        ledger.waive_held(ids[0], style_ledger.WaiveByPerson("nate"))
        ledger.ticket_drop("names-1")
        with self.assertRaisesRegex(style_ledger.LedgerError, re.escape(f"{ids[0]}: cannot file finding in state waived")):
            ledger.file_held(ids[0], issue)
        self.assertEqual(ledger.unwaive_held(ids[:1], style_ledger.WaiveUndoneBy("nate")), ids[:1])
        unfiled, = ledger.held_findings(style_ledger.HeldFindingsInState("unfiled"))
        self.assertEqual((unfiled.finding_id, unfiled.build_ticket), (ids[0], style_ledger.NoBuildTicket()))
        ledger.file_held(ids[0], issue)
        ledger.ticket_add("names-2", "names", "proposal", "names", "crates/alpha", [self.sources[0]], [rule],
                          proposal=str(issue), finding_ids=ids[:1])
        filed = next(finding for finding in ledger.proposal_findings(issue) if finding.finding_id == ids[0])
        self.assertEqual((filed.state, filed.build_ticket, filed.waive_undo),
                         (style_ledger.Filed(issue), style_ledger.BuildTicket("names-2"), style_ledger.WaiveUndoneBy("nate")))


    def test_decision_drop_releases_findings_for_another_ticket(self) -> None:
        rule, issue, ids = self.decision_findings()
        ledger = self.ledger()
        ledger.ticket_add("names-1", "names", "proposal", "names", "crates/alpha", [self.sources[0]], [rule],
                          proposal=str(issue), finding_ids=ids[:1])
        ledger.ticket_assign("names-1", "main")
        ledger.ticket_drop("names-1")
        finding = ledger.proposal_findings(issue)[0]
        self.assertEqual(finding.state, style_ledger.Filed(issue))
        self.assertIsInstance(finding.build_ticket, style_ledger.NoBuildTicket)
        ledger.ticket_add("names-2", "names", "proposal", "names", "crates/alpha", [self.sources[0]], [rule],
                          proposal=str(issue), finding_ids=ids[:1])
        self.assertEqual(ledger.proposal_findings(issue)[0].build_ticket, style_ledger.BuildTicket("names-2"))
        self.assertEqual(ledger.ticket_origin("names-1"), style_ledger.ApprovedProposal(issue, "names", ids[:1]))


    def test_decision_ticket_claim_is_atomic_and_refuses_other_issues_and_live_claims(self) -> None:
        rule, issue, ids = self.decision_findings()
        ledger = self.ledger()
        for proposal in (str(self.root / "wrong.md"), str(issue)):
            if proposal == str(issue):
                ledger.ticket_add("names-1", "names", "proposal", "names", "crates/alpha", [self.sources[0]], [rule],
                                  proposal=str(issue), finding_ids=ids[:1])
                ledger.ticket_assign("names-1", "main")
            with self.subTest(proposal=proposal), self.assertRaises(style_ledger.LedgerError):
                ledger.ticket_add("other-1", "other", "proposal", "names", "crates/alpha", list(self.sources[:2]), [rule],
                                  proposal=proposal, finding_ids=ids)
            self.assertEqual(ledger.queued_tickets("other"), ())
            self.assertIsInstance(ledger.proposal_findings(issue)[1].build_ticket, style_ledger.NoBuildTicket)


    def test_decision_move_issue_findings_is_one_transaction_and_idempotent(self) -> None:
        rule, issue, ids = self.decision_findings()
        ledger = self.ledger()
        target = self.root / "combined.md"
        moves = {identity: target for identity in ids}
        with self.assertRaisesRegex(style_ledger.LedgerError, "not filed"):
            ledger.move_issue_findings(self.root / "wrong.md", moves)
        for state in ("queued", "assigned"):
            ticket = f"names-{state}"
            ledger.ticket_add(f"{ticket}-1", ticket, "proposal", "names", "crates/alpha", [self.sources[1]], [rule],
                              proposal=str(issue), finding_ids=ids[1:])
            if state == "assigned":
                ledger.ticket_assign(f"{ticket}-1", "main")
            with self.subTest(state=state), self.assertRaisesRegex(style_ledger.LedgerError, "live build ticket"):
                ledger.move_issue_findings(issue, moves)
            # The first finding has no ticket, but the refused second one stops the whole move.
            self.assertEqual(tuple(finding.finding_id for finding in ledger.proposal_findings(issue)), ids)
            ledger.ticket_drop(f"{ticket}-1")
        ledger.move_issue_findings(issue, moves)
        ledger.move_issue_findings(issue, moves)
        self.assertEqual(tuple(finding.finding_id for finding in ledger.proposal_findings(target)), ids)
        self.assertEqual(ledger.proposal_findings(issue), ())
        ledger.waive_held(ids[1], style_ledger.WaiveByPerson("nate"))
        with self.assertRaisesRegex(style_ledger.LedgerError, "not filed"):
            ledger.move_issue_findings(target, {ids[1]: issue})


    def test_drop_candidates_removes_every_candidate_with_the_exact_idea(self) -> None:
        rule = self.rule("read")
        self.add_ticket("names-1", [rule], unit="names")
        ledger = self.ledger()
        for path, idea in ((self.sources[0], "Consider a new rule"), (self.sources[1], "Consider a new rule"),
                           (self.sources[2], "Consider a new rule.")):
            ledger.record_candidate("names-1", path, 1, idea)
        self.assertEqual(ledger.drop_candidates("Consider a new rule"), 2)
        self.assertEqual(tuple(candidate.idea for candidate in ledger.rule_candidates()), ("Consider a new rule.",))
        with self.assertRaisesRegex(style_ledger.LedgerError, "no rule candidate"):
            _ = ledger.drop_candidates("Consider a new rule")


    def test_decision_candidate_cli_stores_idea_without_a_held_finding(self) -> None:
        rule = self.rule("read")
        self.add_ticket("names-1", [rule], unit="names")
        result = self.cli("candidate", "--ticket", "names-1", "--site", self.sources[0] + ":3", "--idea", "Consider a new rule")
        self.assertEqual(result.stdout, "")
        ledger = self.ledger()
        candidate, = ledger.rule_candidates()
        self.assertEqual((candidate.ticket, candidate.unit, candidate.path, candidate.line, candidate.idea),
                         ("names-1", "names", self.sources[0], 3, "Consider a new rule"))
        self.assertTrue(candidate.created_at)
        self.assertEqual(ledger.held_findings(style_ledger.AllHeldFindings()), ())
        refused = self.cli("candidate", "--ticket", "missing-1", "--site", self.sources[0] + ":3", "--idea", "Another idea", success=False)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("unknown ticket", refused.stderr)
        self.assertEqual(ledger.rule_candidates(), (candidate,))


    def test_withdrawal_migration_thirteen_keeps_version_twelve_findings_filed(self) -> None:
        rule = self.rule("read")
        database = self.root / "version-twelve.sqlite"
        issue = self.root / "decision.md"
        with closing(sqlite3.connect(database)) as connection:
            for number, migration in sorted(style_ledger.MIGRATIONS.items()):
                if number <= 12:
                    _ = connection.executescript(migration)
            _ = connection.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
            _ = connection.execute("INSERT INTO schema_version VALUES (12)")
            _ = connection.execute("INSERT INTO held_findings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                   ("legacy", rule, self.sources[0], 2, "change", "rename", "old", "new", self.blob(rule),
                                    "read-1", "filed", str(issue), "created", "updated", "", ""))
            connection.commit()
        with style_ledger.Ledger(self.repo, database) as ledger:
            finding, = ledger.proposal_findings(issue)
            self.assertEqual(finding.state, style_ledger.Filed(issue))
            self.assertEqual(ledger.held_findings(style_ledger.HeldFindingsInState("withdrawn")), ())
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute("SELECT version FROM schema_version").fetchone(), (max(style_ledger.MIGRATIONS),))
            self.assertEqual(connection.execute("SELECT withdrawn_by FROM held_findings").fetchone(), (0,))


    def test_person_read_migration_fourteen_keeps_version_thirteen_findings_not_read_by_a_person(self) -> None:
        rule = self.rule("read")
        database = self.root / "version-thirteen.sqlite"
        issue = self.root / "decision.md"
        with closing(sqlite3.connect(database)) as connection:
            for number, migration in sorted(style_ledger.MIGRATIONS.items()):
                if number <= 13:
                    _ = connection.executescript(migration)
            _ = connection.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
            _ = connection.execute("INSERT INTO schema_version VALUES (13)")
            _ = connection.execute("INSERT INTO held_findings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                   ("legacy", rule, self.sources[0], 2, "change", "rename", "old", "new", self.blob(rule),
                                    "read-1", "built", str(issue), "created", "updated", "", "", 0))
            connection.commit()
        with style_ledger.Ledger(self.repo, database) as ledger:
            finding, = ledger.proposal_findings(issue)
            self.assertEqual((finding.state, finding.reading), (style_ledger.Built(issue), style_ledger.NotReadByPerson()))
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute("SELECT version FROM schema_version").fetchone(), (max(style_ledger.MIGRATIONS),))
            self.assertEqual(connection.execute("SELECT person_read_by FROM held_findings").fetchone(), ("",))


    def test_an_unfiled_finding_withdrawn_by_a_newer_judgment_reads_and_lists_as_judged_again(self) -> None:
        finding_id = self.hold_finding(self.rule("read"), 2)
        ledger = self.ledger()
        with closing(sqlite3.connect(ledger.database)) as connection:
            _ = connection.execute("UPDATE held_findings SET state='withdrawn',withdrawn_by=7 WHERE finding_id=?",
                                   (finding_id,))
            connection.commit()
        lines = {line.split("\t")[0]: line.split("\t")[3:] for line in self.cli("held", "list").stdout.splitlines()}
        self.assertEqual(lines[finding_id], ["withdrawn", "judged again in judgment 7"])
        finding, = ledger.held_findings(style_ledger.HeldFindingsInState("withdrawn"))
        self.assertEqual(finding.state, style_ledger.Withdrawn(style_ledger.NoDecision(), style_ledger.JudgedAgain(7)))
        listed = cast(list[dict[str, object]], json.loads(self.cli("held", "list", "--json").stdout))
        self.assertEqual(listed[0]["withdrawn_by"], 7)


    def test_duplicate_migration_fifteen_keeps_version_fourteen_withdrawals_by_their_exception(self) -> None:
        rule = self.rule("read")
        database = self.root / "version-fourteen.sqlite"
        issue = self.root / "decision.md"
        with closing(sqlite3.connect(database)) as connection:
            for number, migration in sorted(style_ledger.MIGRATIONS.items()):
                if number <= 14:
                    _ = connection.executescript(migration)
            _ = connection.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
            _ = connection.execute("INSERT INTO schema_version VALUES (14)")
            _ = connection.execute("INSERT INTO held_findings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                   ("legacy", rule, self.sources[0], 2, "change", "rename", "old", "new", self.blob(rule),
                                    "read-1", "withdrawn", str(issue), "created", "updated", "", "", 7, ""))
            connection.commit()
        with style_ledger.Ledger(self.repo, database) as ledger:
            finding, = ledger.proposal_findings(issue)
            self.assertEqual(finding.state, style_ledger.Withdrawn(issue, style_ledger.ExceptionVerdict(7)))
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute("SELECT version FROM schema_version").fetchone(), (max(style_ledger.MIGRATIONS),))
            self.assertEqual(connection.execute("SELECT withdrawn_by,duplicate_of FROM held_findings").fetchone(), (7, ""))


    def test_unwaive_migration_sixteen_keeps_version_fifteen_waived_findings_not_unwaived(self) -> None:
        rule = self.rule("read")
        database = self.root / "version-fifteen.sqlite"
        issue = self.root / "decision.md"
        with closing(sqlite3.connect(database)) as connection:
            for number, migration in sorted(style_ledger.MIGRATIONS.items()):
                if number <= 15:
                    _ = connection.executescript(migration)
            _ = connection.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
            _ = connection.execute("INSERT INTO schema_version VALUES (15)")
            _ = connection.execute("INSERT INTO held_findings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                   ("legacy", rule, self.sources[0], 2, "change", "rename", "old", "new", self.blob(rule),
                                    "read-1", "waived", str(issue), "created", "updated", "", "", 0, "", ""))
            connection.commit()
        with style_ledger.Ledger(self.repo, database) as ledger:
            finding, = ledger.proposal_findings(issue)
            self.assertEqual((finding.state, finding.waive_undo), (style_ledger.Waived(), style_ledger.NoWaiveUndo()))
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute("SELECT version FROM schema_version").fetchone(), (max(style_ledger.MIGRATIONS),))
            self.assertEqual(connection.execute("SELECT unwaived_by FROM held_findings").fetchone(), ("",))


    def test_follow_migration_seventeen_keeps_version_sixteen_findings_not_followed(self) -> None:
        rule = self.rule("read")
        database = self.root / "version-sixteen.sqlite"
        issue = self.root / "decision.md"
        with closing(sqlite3.connect(database)) as connection:
            for number, migration in sorted(style_ledger.MIGRATIONS.items()):
                if number <= 16:
                    _ = connection.executescript(migration)
            _ = connection.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
            _ = connection.execute("INSERT INTO schema_version VALUES (16)")
            _ = connection.execute("INSERT INTO held_findings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                   ("legacy", rule, self.sources[0], 2, "change", "rename", "old", "new", self.blob(rule),
                                    "read-1", "withdrawn", str(issue), "created", "updated", "", "", 7, "", "", ""))
            connection.commit()
        with style_ledger.Ledger(self.repo, database) as ledger:
            finding, = ledger.proposal_findings(issue)
            self.assertEqual((finding.state, finding.follow),
                             (style_ledger.Withdrawn(issue, style_ledger.ExceptionVerdict(7)), style_ledger.NotFollowed()))
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute("SELECT version FROM schema_version").fetchone(), (max(style_ledger.MIGRATIONS),))
            self.assertEqual(connection.execute("SELECT followed_from,followed_from_line,followed_at,declaration_gone_at "
                                                + "FROM held_findings").fetchone(), ("", 0, "", ""))


    def test_hold_of_a_followed_finding_site_returns_the_followed_finding(self) -> None:
        rule, issue, ids = self.decision_findings()
        ledger = self.ledger()
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE held_findings SET path=?,line=7,followed_from=?,followed_from_line=1,followed_at=? "
                                   + "WHERE finding_id=?", (self.sources[2], self.sources[0], self.initial_tip, ids[0]))
        count = len(ledger.held_findings(style_ledger.AllHeldFindings()))
        held = ledger.hold(rule, self.sources[2], 8, "required change", "rename", "Outcome::Old", "Outcome::New",
                           self.blob(rule), "read-2")
        self.assertEqual((held.finding_id, held.state, held.line), (ids[0], style_ledger.Filed(issue), 7))
        self.assertEqual(len(ledger.held_findings(style_ledger.AllHeldFindings())), count)


    def test_held_findings_keep_two_sites_per_cell_and_preserve_idempotent_state(self) -> None:
        rule_id = self.rule("read")
        self.set_tip(self.initial_tip)
        first = self.hold_finding(rule_id, 1)
        second = self.hold_finding(rule_id, 2)
        self.assertNotEqual(first, second)
        self.assertRegex(first, r"^[0-9a-f]{16}$")
        self.assertEqual(self.hold_finding(rule_id, 1), first)
        ledger = self.ledger()
        self.assertEqual({finding.finding_id for finding in ledger.held_findings(style_ledger.AllHeldFindings())}, {first, second})
        self.assertTrue(all(isinstance(finding.state, style_ledger.Unfiled) for finding in ledger.held_findings(style_ledger.AllHeldFindings())))
        original = next(finding for finding in ledger.held_findings(style_ledger.AllHeldFindings()) if finding.finding_id == first)
        duplicate = ledger.hold(rule_id, self.sources[0], 1, "pub fn old_name_1() {}", "type",
                                "different before", "different after", self.blob(rule_id), "another-ticket")
        # A different change at the same site is its own row, and the first row keeps its change.
        self.assertNotEqual(duplicate.finding_id, first)
        self.assertEqual((duplicate.kind, duplicate.before, duplicate.after),
                         ("type", "different before", "different after"))
        self.assertEqual(ledger.named_held((first,))[0], original)
        proposal = self.root / "issues/rename.md"
        for finding in (first, second, duplicate.finding_id):
            for _ in range(2):
                _ = self.cli("held", "file", "--id", finding, "--proposal", str(proposal))
        self.assertEqual(self.summary_report()["proposals"], 1)
        self.assertEqual(self.hold_finding(rule_id, 1), first)
        filed = ledger.held_findings(style_ledger.AllHeldFindings())
        self.assertTrue(all(isinstance(finding.state, style_ledger.Filed) for finding in filed))
        for finding in filed:
            if isinstance(finding.state, style_ledger.Filed):
                self.assertEqual(finding.state.proposal, proposal)
        refused = self.cli("held", "file", "--id", first, "--proposal", str(self.root / "other.md"), success=False)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("filed", refused.stderr)
        for _ in range(2):
            _ = self.cli("held", "waive", "--id", first, "--person", "nate")
        refused = self.cli("held", "file", "--id", first, "--proposal", str(proposal), success=False)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("waived", refused.stderr)
        self.assertEqual(self.hold_finding(rule_id, 1), first)
        self.assertEqual(sum(isinstance(finding.state, style_ledger.Waived) for finding in ledger.held_findings(style_ledger.AllHeldFindings())), 1)
        filtered = cast(list[dict[str, object]], json.loads(self.cli("held", "list", "--state", "waived", "--json").stdout))
        self.assertEqual([finding["finding_id"] for finding in filtered], [first])


    def test_held_can_waive_unfiled_and_rejects_unknown_unclassified_or_wrong_pin(self) -> None:
        rule_id = self.rule("read")
        legacy = self.rule("legacy", "mechanism: llm\n")
        self.set_tip(self.initial_tip)
        self.add_ticket("read-1", [rule_id])
        blob = self.blob(rule_id)
        finding = self.hold_finding(rule_id, 1, ticket="read-1")
        for _ in range(2):
            _ = self.cli("held", "waive", "--id", finding, "--person", "nate")
        self.assertIsInstance(self.ledger().held_findings(style_ledger.AllHeldFindings())[0].state, style_ledger.Waived)
        for invalid, supplied in ((legacy, self.blob(legacy)), ("global:rust/missing.md", blob), (rule_id, "wrong-blob")):
            with self.subTest(rule=invalid, blob=supplied):
                refused = self.cli(
                    "held", "add", "--rule", invalid, "--path", self.sources[0], "--line", "3",
                    "--text", "old", "--kind", "rename", "--before", "old", "--after", "new",
                    "--rule-blob", supplied, "--ticket", "read-1", success=False,
                )
                self.assertEqual(refused.returncode, 2)
                self.assertIn(invalid, refused.stderr)
        self.assertEqual(len(self.ledger().held_findings(style_ledger.AllHeldFindings())), 1)


    def test_pinned_rule_bytes_survive_edits_reopen_and_pseudo_rules_share_the_parent(self) -> None:
        partial = self.rule(
            "partial", "tier: B\nfamily: names\ncandidates:\n  kind: regex\n  pattern: value\n"
            + "  surface: partial\n  uncovered: other names\n",
        )
        path = self.rule_dir / "partial.md"
        original = path.read_bytes() + b"Exact bytes: \xc3\xa9\r\n"
        _ = path.write_bytes(original)
        blob = self.blob(partial)
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.ticket_add("names-unit-1", "names-unit", "read", "names", "crates/alpha",
                              list(self.sources[:2]), [partial, partial + "#uncovered"])
        _ = path.write_text(path.read_text() + "Changed after ticket creation\n")

        with style_ledger.open_ledger(self.repo) as ledger:
            # A repeated add must preserve the old ticket, including its pinned bytes.
            ledger.ticket_add("names-unit-1", "names-unit", "read", "names", "crates/alpha",
                              list(self.sources[:2]), [partial, partial + "#uncovered"])
            texts = ledger.pinned_rule_texts("names-unit-1")

        self.assertEqual(texts, (
            style_ledger.PinnedRuleText(partial, blob, original),
            style_ledger.PinnedRuleText(partial + "#uncovered", blob, original),
        ))
        with closing(sqlite3.connect(self.root / "state/sample/ledger.sqlite")) as connection:
            stored = cast(list[tuple[str, bytes]], connection.execute("SELECT blob,text FROM rule_texts").fetchall())
        self.assertEqual(stored, [(blob, original)])


    def test_local_argument_attributes_only_block_conditional_identity(self) -> None:
        for attribute in ("#[derive(Clone)]", "#[derive(Clone)]\n", "#[cfg(test)]", "#[cfg_attr(test, derive(Clone))]"):
            with self.subTest(attribute=attribute):
                with style_ledger.Ledger(self.repo, self.root / f"argument-attribute-{len(attribute)}.db") as ledger:
                    source = (f"{attribute} struct Arg;\nstruct Holder<T>(T);\n"
                              + "impl crate::a::Holder<Arg> {\n"
                              + "    fn original(&self) -> u32 {\n        7\n    }\n}\n")
                    line = source.splitlines().index("    fn original(&self) -> u32 {") + 1
                    _ = self.commit_tip(ledger, {PARENT_FILE: source})
                    _, (finding_id,) = self.judge_parent(ledger, ((line, "fn original(&self) -> u32 {"),), kind="restructure")
                    _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("fn original", "fn renamed")})
                    before = self.held_rows(ledger.database)
                    outcome, = self.follow_approved(ledger, [finding_id])
                    if "cfg" in attribute:
                        self.assertIsInstance(outcome, style_ledger.OutsideRenameUnresolved)
                        self.assertIn(style_ledger.OutsideFailure.WRONG_OWNER, repr(outcome))
                        self.assertEqual(self.held_rows(ledger.database), before)
                    else:
                        self.assertIsInstance(outcome, style_ledger.DeclarationMoved)
                        row = next(row for row in ledger.held_findings(style_ledger.AllHeldFindings()) if row.finding_id == finding_id)
                        self.assertEqual((row.path, row.line, row.state),
                                         (PARENT_FILE, line, style_ledger.Filed(self.root / "decision.md")))


    def test_a_typed_closures_retained_parameter_follows_its_renamed_function_under_lock(self) -> None:
        ledger = self.ledger()
        source = ("fn original() {\n    common_one();\n    common_two();\n"
                  + "    let handler: fn(u32) =\n        move |answers| {\n"
                  + "            consume(answers);\n        };\n    common_three();\n    common_four();\n}\n")
        _ = self.commit_tip(ledger, {PARENT_FILE: source})
        _, (finding_id,) = self.judge_parent(ledger, ((5, "move |answers| {"),), kind="restructure")
        original, = ledger.held_findings(style_ledger.AllHeldFindings())
        tip = self.commit_tip(ledger, {PARENT_FILE: source.replace("fn original", "fn renamed")})
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(outcome, style_ledger.DeclarationMoved)
        row, = ledger.held_findings(style_ledger.AllHeldFindings())
        self.assertEqual((row.finding_id, row.path, row.line, row.state, row.text, row.before, row.after),
                         (original.finding_id, PARENT_FILE, 5, original.state, original.text, original.before, original.after))
        self.assertEqual(row.follow, style_ledger.FollowedDeclaration(PARENT_FILE, 5, tip))
        before = self.held_rows(ledger.database)
        _ = self.follow_approved(ledger, [finding_id])
        self.assertEqual(self.held_rows(ledger.database), before)


    def test_retained_children_follow_a_renamed_function_with_changed_closure_call_arguments(self) -> None:
        source = """    fn uncached_completed_rows_arrive_before_hierarchy_discovery_is_released() {
        struct GatedHierarchy {
            entered: Sender<()>,
            release: Mutex<Receiver<()>>,
        }
        impl HierarchyRunner for GatedHierarchy {
            fn run(&self, _: &str, _: Duration) -> Result<HierarchyEdges, HierarchyError> {
                self.entered.send(()).expect("signal discovery");
                self.release
                    .lock()
                    .expect("release lock")
                    .recv()
                    .expect("release discovery");
                Err(HierarchyError::TimedOut)
            }
        }
        let (entered, waiting) = mpsc::channel();
        let (release, released) = mpsc::channel();
        let expected = vec![session_row("worker", 2)];
        let rows = expected.clone();
        let updates = spawn_with_test_source(
            HierarchyCommand::new(HierarchySource::Command("fixture".to_string())),
            Arc::new(GatedHierarchy {
                entered,
                release: Mutex::new(released),
            }),
            move |answers| {
                answers
                    .publish(ScanStage::Completed, rows.clone())
                    .expect("publish completion");
            },
        );
        waiting
            .recv_timeout(Duration::from_millis(200))
            .expect("discovery starts");
        let early = updates.try_recv();
        release
            .send(())
            .expect("release discovery before assertions");
        assert_eq!(
            early.expect("uncached completed rows precede discovery"),
            CensusUpdate::Local {
                rows:      expected.clone(),
                hierarchy: HierarchyEdges::default().into(),
                stage:     ScanStage::Classified,
            }
        );
        assert_eq!(
            updates
                .recv_timeout(Duration::from_millis(200))
                .expect("failed discovery answers"),
            noted(expected, HierarchyNotice::Failed(HierarchyError::TimedOut))
        );
    }
"""
        target = """    fn uncached_first_publication_respects_its_budget_and_holds_claude_rows() {
        struct GatedHierarchy {
            entered: Sender<()>,
            release: Mutex<Receiver<()>>,
        }
        impl HierarchyRunner for GatedHierarchy {
            fn run(&self, _: &str, _: Duration) -> Result<HierarchyEdges, HierarchyError> {
                self.entered.send(()).expect("signal discovery");
                self.release
                    .lock()
                    .expect("release lock")
                    .recv()
                    .expect("release discovery");
                Err(HierarchyError::TimedOut)
            }
        }
        let (entered, waiting) = mpsc::channel();
        let (release, released) = mpsc::channel();
        let expected = vec![session_row("worker", 2)];
        let rows = expected.clone();
        let budget = HIERARCHY_FIRST_PUBLISH_BUDGET / 100;
        let deadline = budget + HIERARCHY_FIRST_PUBLISH_BUDGET / 5;
        let (finish, details) = mpsc::channel();
        let started = Instant::now();
        let updates = spawn_with_test_source_and_budget(
            HierarchyCommand::new(HierarchySource::Command("fixture".to_string())),
            Arc::new(GatedHierarchy {
                entered,
                release: Mutex::new(released),
            }),
            budget,
            move |answers| {
                answers
                    .publish(ScanStage::Completed, rows.clone())
                    .expect("publish completion");
                details
                    .recv()
                    .expect("keep the scan open for the late answer");
            },
        );
        waiting
            .recv_timeout(Duration::from_millis(200))
            .expect("discovery starts");
        let early = updates.recv_timeout(deadline);
        let elapsed = started.elapsed();
        eprintln!("bounded first publication: {} ms", elapsed.as_millis());
        release
            .send(())
            .expect("release discovery before assertions");
        assert!(elapsed < deadline, "first publication took {elapsed:?}");
        assert_eq!(
            early.expect("safe rows publish inside the startup budget"),
            CensusUpdate::Local {
                rows:      Vec::new(),
                hierarchy: HierarchySnapshot::default(),
                stage:     ScanStage::Completed,
            }
        );
        let late = updates.recv_timeout(deadline);
        finish.send(()).expect("release details before assertions");
        assert_eq!(
            late.expect("failed discovery answers during detail reads"),
            noted(expected, HierarchyNotice::Failed(HierarchyError::TimedOut))
        );
    }
"""
        ledger = self.ledger()
        _ = self.commit_tip(ledger, {PARENT_FILE: source})
        _, findings = self.judge_parent(ledger, ((19, "let rows = expected.clone();"), (27, "move |answers| {")), kind="restructure")
        before = {row.finding_id: row for row in ledger.held_findings(style_ledger.AllHeldFindings())}
        _ = self.commit_tip(ledger, {PARENT_FILE: target})
        outcomes = self.follow_approved(ledger, list(findings))
        self.assertEqual(len(outcomes), 2)
        for outcome, line in zip(outcomes, (19, 32), strict=True):
            self.assertIsInstance(outcome, style_ledger.DeclarationMoved)
            moved = cast(style_ledger.DeclarationMoved, outcome)
            self.assertEqual((moved.path, moved.line), (PARENT_FILE, line))
            row = next(row for row in ledger.held_findings(style_ledger.AllHeldFindings()) if row.finding_id == moved.finding.finding_id)
            original = before[row.finding_id]
            self.assertEqual((row.state, row.before, row.after, row.text, row.ticket),
                             (original.state, original.before, original.after, original.text, original.ticket))
            self.assertIsInstance(row.follow, style_ledger.FollowedDeclaration)
        again = self.follow_approved(ledger, list(findings))
        self.assertTrue(all(isinstance(outcome, style_ledger.DeclarationInPlace) for outcome in again))


    def test_an_impl_member_inherits_only_a_proven_outside_trait_rename(self) -> None:
        for fails in (False, True):
            with self.subTest(body_fails=fails):
                with style_ledger.Ledger(self.repo, self.root / f"outside-trait-{fails}.db") as ledger:
                    source = ("pub struct Type;\ntrait OldTrait {\n    fn child(&self);\n    const ONE: u32 = 1;\n"
                              + "    const TWO: u32 = 2;\n}\nimpl OldTrait for Type {\n    fn child(&self) { 7 }\n}\n")
                    _ = self.commit_tip(ledger, {PARENT_FILE: source})
                    _, (finding_id,) = self.judge_parent(ledger, ((8, "7"),), kind="restructure")
                    target = source.replace("OldTrait", "NewTrait")
                    if fails:
                        target = target.replace("const ONE: u32 = 1", "const THREE: u32 = 3").replace("const TWO: u32 = 2", "const FOUR: u32 = 4")
                    _ = self.commit_tip(ledger, {PARENT_FILE: target})
                    before = self.held_rows(ledger.database)
                    outcome, = self.follow_approved(ledger, [finding_id])
                    if fails:
                        self.assertIsInstance(outcome, style_ledger.OutsideRenameUnresolved)
                        self.assertEqual(self.held_rows(ledger.database), before)
                        self.assertIn(style_ledger.OutsideFailure.BODY_FAILED, repr(outcome))
                    else:
                        self.assertIsInstance(outcome, style_ledger.DeclarationMoved)
                        row, = ledger.held_findings(style_ledger.AllHeldFindings())
                        self.assertEqual((row.line, row.state), (8, style_ledger.Filed(self.root / "decision.md")))
                        self.assertIsInstance(row.follow, style_ledger.FollowedDeclaration)


    def test_different_qualified_impl_types_do_not_share_an_outside_method_rename(self) -> None:
        ledger = self.ledger()
        source = ("mod one { pub struct Type; }\nmod two { pub struct Type; }\nimpl one::Type {\n"
                  + "    fn original(&self) -> u32 {\n        1\n    }\n}\n")
        _ = self.commit_tip(ledger, {PARENT_FILE: source})
        _, (finding_id,) = self.judge_parent(ledger, ((4, "fn original(&self) -> u32 {"),), kind="restructure")
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("impl one::Type", "impl two::Type").replace("fn original", "fn renamed")})
        before = self.held_rows(ledger.database)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(outcome, style_ledger.OutsideRenameUnresolved)
        self.assertEqual(self.held_rows(ledger.database), before)
        self.assertIn(style_ledger.OutsideFailure.WRONG_OWNER, repr(outcome))


    def test_removed_parameters_and_fields_do_not_inherit_an_outside_owner_rename(self) -> None:
        cases = (
            ("fn original(\n    input: u32,\n) -> u32 {\n    let retained = 1;\n    input + retained\n}\n",
             "fn renamed(\n    replacement: u32,\n) -> u32 {\n    let retained = 1;\n    replacement + retained\n}\n"),
            ("struct Original {\n    selected: u32,\n    common_one: u32,\n    common_two: u32,\n}\n",
             "struct Renamed {\n    replacement: u64,\n    common_one: u32,\n    common_two: u32,\n}\n"),
        )
        for index, (source, target) in enumerate(cases):
            with self.subTest(source=source):
                with style_ledger.Ledger(self.repo, self.root / f"outside-removed-child-{index}.db") as ledger:
                    _ = self.commit_tip(ledger, {PARENT_FILE: source})
                    _, (finding_id,) = self.judge_parent(ledger, ((2, source.splitlines()[1]),), kind="restructure")
                    _ = self.commit_tip(ledger, {PARENT_FILE: target})
                    before = self.held_rows(ledger.database)
                    outcome, = self.follow_approved(ledger, [finding_id])
                    self.assertIsInstance(outcome, style_ledger.OutsideRenameUnresolved)
                    self.assertEqual(self.held_rows(ledger.database), before)
                    self.assertIn(style_ledger.OutsideFailure.CHILD_UNPROVED, repr(outcome))


    def test_a_closure_parameter_does_not_follow_into_a_different_closure_under_lock(self) -> None:
        ledger = self.ledger()
        source = ("fn original() {\n    common_one();\n    common_two();\n    consume(move |answers| {\n"
                  + "        first(answers);\n    });\n    consume(move |other| {\n        second(other);\n"
                  + "    });\n    common_three();\n    common_four();\n}\n")
        _ = self.commit_tip(ledger, {PARENT_FILE: source})
        _, (finding_id,) = self.judge_parent(ledger, ((4, "consume(move |answers| {"),), kind="restructure")
        target = (source.replace("fn original", "fn renamed").replace("|answers|", "|removed|")
                  .replace("first(answers)", "first(removed)").replace("|other|", "|answers|")
                  .replace("second(other)", "second(answers)"))
        _ = self.commit_tip(ledger, {PARENT_FILE: target})
        before = self.held_rows(ledger.database)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(outcome, style_ledger.OutsideRenameUnresolved)
        self.assertEqual(self.held_rows(ledger.database), before)
        self.assertIn(style_ledger.OutsideFailure.CHILD_UNPROVED, repr(outcome))


    def test_a_deleted_closures_parameter_does_not_follow_into_the_surviving_call_under_lock(self) -> None:
        ledger = self.ledger()
        source = ("fn original() {\n    common_one();\n    common_two();\n    first(move |answers| {\n"
                  + "        consume(answers);\n    });\n    second(move |other| {\n        consume(other);\n"
                  + "    });\n    common_three();\n    common_four();\n}\n")
        target = ("fn renamed() {\n    common_one();\n    common_two();\n    second(move |answers| {\n"
                  + "        consume(answers);\n    });\n    common_three();\n    common_four();\n}\n")
        _ = self.commit_tip(ledger, {PARENT_FILE: source})
        _, (finding_id,) = self.judge_parent(ledger, ((4, "first(move |answers| {"),), kind="restructure")
        _ = self.commit_tip(ledger, {PARENT_FILE: target})
        before = self.held_rows(ledger.database)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(outcome, style_ledger.OutsideRenameUnresolved)
        self.assertIn(style_ledger.OutsideFailure.CHILD_UNPROVED, repr(outcome))
        self.assertEqual(self.held_rows(ledger.database), before)


    def test_a_filed_and_an_unfiled_finding_follow_a_proven_outside_rename(self) -> None:
        for index, (filed, destination) in enumerate((
            (False, PARENT_FILE), (True, PARENT_FILE), (False, MOVED_FILE), (True, MOVED_FILE),
        )):
            with self.subTest(filed=filed, destination=destination):
                ledger = style_ledger.Ledger(self.repo, self.root / f"outside-{index}.db")
                source = "fn original() {\n    first();\n    second();\n    third();\n    fourth();\n}\n"
                target = source.replace("original", "renamed").replace("third();", "changed_third();").replace("fourth();", "changed_fourth();")
                _ = self.commit_tip(ledger, {PARENT_FILE: source, MOVED_FILE: "// empty\n", TWIN_FILE: target})
                hold = self.judge_parent if filed else self.hold_parent
                _, (finding_id,) = hold(ledger, ((1, "fn original() {"),), kind="restructure", before="keep this request", after="use a type")
                original, = ledger.held_findings(style_ledger.AllHeldFindings())
                changes: dict[str, str | None] = {PARENT_FILE: "// owner moved\n", destination: target,
                                                   TWIN_FILE: target + "// changed pre-existing twin\n"}
                tip = self.commit_tip(ledger, changes)
                outcome, = self.follow_approved(ledger, [finding_id])
                self.assertIsInstance(outcome, style_ledger.DeclarationMoved)
                database = ledger.database
                ledger.close()
                with style_ledger.Ledger(self.repo, database) as reopened:
                    row, = reopened.held_findings(style_ledger.AllHeldFindings())
                    self.assertEqual((row.finding_id, row.path, row.line, row.state, row.before, row.after,
                                      row.rule_blob, row.kind, row.text, row.ticket, row.reading, row.follow),
                                     (finding_id, destination, 1, original.state, original.before, original.after,
                                      original.rule_blob, original.kind, original.text, original.ticket, original.reading,
                                      style_ledger.FollowedDeclaration(PARENT_FILE, 1, tip)))
                    before = self.held_rows(database)
                    again, = self.follow_approved(reopened, [finding_id])
                    self.assertIsInstance(again, style_ledger.DeclarationInPlace)
                    self.assertEqual(self.held_rows(database), before)


    def test_an_outside_rename_needs_every_check_and_reports_its_mapped_declaration(self) -> None:
        source = "fn original() {\n    first();\n    second();\n    third();\n}\n"
        good = source.replace("original", "renamed")
        cases: tuple[tuple[str, str, dict[str, str | None], bool], ...] = (
            ("only call uses the old name", source, {PARENT_FILE: good + "fn caller() { original(); }\n"}, True),
            ("old declaration only in another crate", source, {PARENT_FILE: good, self.sources[2]: source}, True),
            ("one of three", source, {PARENT_FILE: good.replace("second();", "different();").replace("third();", "other();")}, False),
            ("changed keyword", "struct Original {\n    first: u32,\n    second: u32,\n}\n", {PARENT_FILE: "enum Renamed {\n    First,\n    Second,\n}\n"}, False),
            ("wrong impl owner", "impl Type {\n    fn original() {\n        first();\n        second();\n    }\n}\n", {PARENT_FILE: "impl Other {\n    fn renamed() {\n        first();\n        second();\n    }\n}\n"}, False),
            ("wrong implemented trait", "impl crate::First for Type {\n    fn original() {\n        first();\n        second();\n    }\n}\n", {PARENT_FILE: "impl crate::Second for Type {\n    fn renamed() {\n        first();\n        second();\n    }\n}\n"}, False),
            ("ambiguous mapped line", source, {PARENT_FILE: "fn renamed() { first(); second(); third(); } fn another() { first(); second(); third(); }\n"}, False),
            ("empty", "fn original() {}\n", {PARENT_FILE: "fn renamed() {}\n"}, False),
            ("semicolon has no body", "type Original = u32;\nfn nearby() { first(); }\n", {PARENT_FILE: "type Renamed = u32;\nfn nearby() { first(); }\n"}, False),
            ("one-line body changed", "fn original() { first(); }\n", {PARENT_FILE: "fn renamed() { other(); }\n"}, False),
            ("repeated lines count once", "fn original() {\n    same();\n    same();\n    same();\n}\n", {PARENT_FILE: "fn renamed() {\n    same();\n    other();\n    last();\n}\n"}, False),
            ("deleted site and zero targets", source, {PARENT_FILE: None}, False),
            ("two passing files", source, {PARENT_FILE: "// removed\n", MOVED_FILE: good, TWIN_FILE: good}, False),
            ("two items in one gained file", source, {PARENT_FILE: "// removed\n", MOVED_FILE: good + good.replace("renamed", "another")}, False),
            ("different crate only", source, {PARENT_FILE: "// removed\n", self.sources[2]: good}, False),
            ("original file counts beside gained file", source, {PARENT_FILE: "// mapped away\nfn unrelated() {}\n" + good, MOVED_FILE: good}, False),
        )
        for index, (name, old, changes, follows) in enumerate(cases):
            with self.subTest(case=name):
                with style_ledger.Ledger(self.repo, self.root / f"outside-limit-{index}.db") as ledger:
                    _ = self.commit_tip(ledger, {PARENT_FILE: old, MOVED_FILE: "// empty\n", TWIN_FILE: "// empty\n",
                                               self.sources[2]: source if name == "old declaration only in another crate" else "// empty\n"})
                    site_line = 2 if name.startswith("wrong ") else 1
                    _, (finding_id,) = self.hold_parent(ledger, ((site_line, old.splitlines()[site_line - 1]),), kind="restructure")
                    _ = self.commit_tip(ledger, changes)
                    before = self.held_rows(ledger.database)
                    outcome, = self.follow_approved(ledger, [finding_id])
                    if follows:
                        self.assertIsInstance(outcome, style_ledger.DeclarationMoved)
                        if isinstance(outcome, style_ledger.DeclarationMoved):
                            self.assertEqual(outcome.path, PARENT_FILE)
                    else:
                        self.assertEqual(self.held_rows(ledger.database), before)
                        self.assertIsInstance(outcome, style_ledger.OutsideRenameUnresolved)
                        self.assertEqual(ledger.held_findings(style_ledger.AllHeldFindings())[0].state, style_ledger.Unfiled())
                        if isinstance(outcome, style_ledger.OutsideRenameUnresolved):
                            evidence = outcome.evidence
                            if name == "deleted site and zero targets":
                                self.assertIsInstance(evidence.mapped, declarations.UnmappedSite)
                                self.assertIsInstance(evidence.candidates, style_ledger.NoOutsideCandidates)
                            elif name == "ambiguous mapped line":
                                self.assertEqual(evidence.mapped, declarations.UnmappedSite(declarations.MappingFailure.AMBIGUOUS))
                            elif PARENT_FILE in changes and changes[PARENT_FILE] != "// removed\n":
                                self.assertIsInstance(evidence.mapped, declarations.MappedDeclaration)
                            if isinstance(evidence.candidates, style_ledger.OutsideCandidates):
                                paths = [item.path for item in evidence.candidates.items]
                                self.assertEqual(paths, sorted(paths))
                                if MOVED_FILE in changes:
                                    self.assertIn(MOVED_FILE, paths)
                                if name == "one of three":
                                    candidate, = evidence.candidates.items
                                    self.assertEqual((candidate.unchanged, candidate.old_nonblank_count), (1, 3))
                                    self.assertIn(style_ledger.OutsideFailure.BODY_FAILED, candidate.checks)
                                if name == "two passing files":
                                    self.assertEqual({item.path for item in evidence.candidates.items if not item.checks},
                                                     {MOVED_FILE, TWIN_FILE})
        for index, old_elsewhere in enumerate((source, "#[cfg(test)] mod tests {\n" + source + "}\n")):
            with self.subTest(old_elsewhere=old_elsewhere):
                with style_ledger.Ledger(self.repo, self.root / f"outside-old-{index}.db") as ledger:
                    _ = self.commit_tip(ledger, {PARENT_FILE: source, MOVED_FILE: old_elsewhere, TWIN_FILE: "// empty\n"})
                    _, (finding_id,) = self.judge_parent(ledger, ((1, "fn original() {"),), kind="restructure")
                    _ = self.commit_tip(ledger, {PARENT_FILE: good})
                    before = self.held_rows(ledger.database)
                    outcome, = self.follow_approved(ledger, [finding_id])
                    self.assertNotIsInstance(outcome, style_ledger.DeclarationMoved)
                    self.assertEqual(self.held_rows(ledger.database), before)

        for filed in (False, True):
            with self.subTest(preexisting_destination=True, filed=filed):
                with style_ledger.Ledger(self.repo, self.root / f"outside-preexisting-{filed}.db") as ledger:
                    _ = self.commit_tip(ledger, {PARENT_FILE: source, MOVED_FILE: good, TWIN_FILE: "// empty\n"})
                    hold = self.judge_parent if filed else self.hold_parent
                    _, (finding_id,) = hold(ledger, ((1, "fn original() {"),), kind="restructure")
                    _ = self.commit_tip(ledger, {PARENT_FILE: None, MOVED_FILE: good + "// edited twin\n"})
                    before = self.held_rows(ledger.database)
                    outcome, = self.follow_approved(ledger, [finding_id])
                    self.assertIsInstance(outcome, style_ledger.OutsideRenameUnresolved)
                    self.assertEqual(self.held_rows(ledger.database), before)
                    self.assertIn(MOVED_FILE, repr(outcome))
                    self.assertIn("declaration not gained", repr(outcome))


    def test_children_follow_only_when_their_renamed_owner_is_proven(self) -> None:
        cases = (
            ("let", "fn original() {\n    let rows = collect();\n    publish(rows);\n    finish();\n}\n", 2, "rows", True),
            ("closure", "fn original() {\n    serve(move |answers| {\n        publish(answers);\n    });\n    finish();\n}\n", 2, "answers", True),
            ("method", "struct Original {\n    value: u32,\n}\nimpl Original {\n    fn child(&self) {\n        first();\n        second();\n    }\n}\n", 5, "child", True),
            ("trait associated item", "trait Original {\n    const CHILD: u32 = 7;\n    fn child() { first(); }\n}\n", 2, "CHILD", True),
            ("child renamed", "fn original() {\n    let rows = collect();\n    publish(rows);\n    finish();\n}\n", 2, "rows", False),
            ("owner still declared", "struct Original {\n    value: u32,\n}\nimpl Original {\n    fn child(&self) { first(); }\n}\n", 5, "child", False),
            ("call belongs to function", "fn original() {\n    rows();\n    finish();\n}\n", 2, "rows", True),
            ("shadowed binding", "fn original() {\n    let rows = collect();\n    { let rows = other(); publish(rows); }\n    finish();\n}\n", 2, "rows", False),
        )
        for index, (name, source, line, child, follows) in enumerate(cases):
            for moved in (False, True):
                with self.subTest(case=name, moved=moved):
                    with style_ledger.Ledger(self.repo, self.root / f"outside-child-{index}-{moved}.db") as ledger:
                        _ = self.commit_tip(ledger, {PARENT_FILE: source, MOVED_FILE: "// empty\n", TWIN_FILE: "// empty\n"})
                        _, (finding_id,) = self.judge_parent(ledger, ((line, source.splitlines()[line - 1].strip()),),
                                                           before=child, after="a better name", kind="restructure")
                        target = source.replace("original", "renamed").replace("Original", "Renamed")
                        if name == "child renamed":
                            target = target.replace("rows", "entries")
                        if name == "owner still declared":
                            target += "struct Original;\n"
                        destination = MOVED_FILE if moved else PARENT_FILE
                        changes: dict[str, str | None] = {PARENT_FILE: "// owner moved\n", destination: target}
                        tip = self.commit_tip(ledger, changes)
                        before = self.held_rows(ledger.database)
                        outcome, = self.follow_approved(ledger, [finding_id])
                        if follows:
                            self.assertIsInstance(outcome, style_ledger.DeclarationMoved)
                            row, = ledger.held_findings(style_ledger.AllHeldFindings())
                            self.assertEqual((row.path, row.line, row.state, row.follow),
                                             (destination, line, style_ledger.Filed(self.root / "decision.md"),
                                              style_ledger.FollowedDeclaration(PARENT_FILE, line, tip)))
                        else:
                            self.assertNotIsInstance(outcome, style_ledger.DeclarationMoved)
                            self.assertEqual(self.held_rows(ledger.database), before)


    def test_outside_follow_preserves_existing_follow_authority_and_write_rechecks(self) -> None:
        ledger = self.ledger()
        source = "fn original() {\n    first();\n    second();\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: source})
        _, (finding_id,) = self.hold_parent(ledger, ((1, "fn original() {"),), kind="restructure")
        read = ledger.synced_commit()
        planned = self.commit_tip(ledger, {PARENT_FILE: source.replace("original", "renamed")})
        before = self.held_rows(ledger.database)
        with self.assertRaisesRegex(style_ledger.LedgerError, "synced tip moved"):
            _ = ledger.follow_declarations([finding_id], read, lambda _: True)
        self.assertEqual(self.held_rows(ledger.database), before)
        original_read = declarations.read_declarations
        changed = False

        def read_during_sync(text: str) -> declarations.DeclaredFile:
            nonlocal changed
            if not changed:
                changed = True
                _ = self.commit_tip(ledger, {MOVED_FILE: "fn helper() {}\n"})
            return original_read(text)

        with (mock.patch.object(style_ledger, "read_declarations", read_during_sync),
              self.assertRaisesRegex(style_ledger.LedgerError, "synced tip moved")):
            _ = ledger.follow_declarations([finding_id], planned, lambda _: True)
        self.assertEqual(self.held_rows(ledger.database), before)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(outcome, style_ledger.DeclarationMoved)

        with style_ledger.Ledger(self.repo, self.root / "outside-row-recheck.db") as other:
            _ = self.commit_tip(other, {PARENT_FILE: source, MOVED_FILE: "// empty\n", TWIN_FILE: "// empty\n"})
            _, (finding_id,) = self.hold_parent(other, ((1, "fn original() {"),), kind="restructure")
            _ = self.commit_tip(other, {PARENT_FILE: source.replace("original", "renamed")})
            judged = style_ledger.Ledger.judged_commit
            wrote = False

            def read_during_row_change(reader: style_ledger.Ledger, finding: style_ledger.HeldFinding) -> style_ledger.JudgedCommit | style_ledger.NoJudgedCommit:
                nonlocal wrote
                if not wrote:
                    wrote = True
                    with closing(sqlite3.connect(other.database)) as connection, connection:
                        _ = connection.execute("UPDATE held_findings SET text='fresh request' WHERE finding_id=?", (finding_id,))
                return judged(reader, finding)

            with mock.patch.object(style_ledger.Ledger, "judged_commit", read_during_row_change):
                outcome, = self.follow_approved(other, [finding_id])
            self.assertIsInstance(outcome, style_ledger.DeclarationMoved)
            row, = other.held_findings(style_ledger.AllHeldFindings())
            self.assertEqual(row.text, "fresh request")


    def test_live_outside_rename_examples_follow_only_the_two_retained_children(self) -> None:
        ledger = self.ledger()
        old_body = ["    let rows = collect();", "    serve(move |answers| publish(answers));"]
        old_body.extend(f"    step_{index}();" for index in range(50))
        new_body = old_body[:43] + [f"    replacement_{index}();" for index in range(9)]
        old_name = "uncached_completed_rows_arrive_before_hierarchy_discovery_is_released"
        new_name = "uncached_first_publication_respects_its_budget_and_holds_claude_rows"
        source = f"fn {old_name}() {{\n" + "\n".join(old_body) + "\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: source})
        _, ids = self.judge_parent(ledger, ((2, old_body[0].strip()), (3, old_body[1].strip())), kind="restructure")
        target = f"fn {new_name}() {{\n" + "\n".join(new_body) + "\n}\n"
        tip = self.commit_tip(ledger, {PARENT_FILE: "// relocated\n" + target})
        outcomes = self.follow_approved(ledger, list(ids))
        self.assertEqual([type(outcome) for outcome in outcomes], [style_ledger.DeclarationMoved] * 2)
        rows = ledger.held_findings(style_ledger.AllHeldFindings())
        self.assertEqual([(row.finding_id, row.line, row.state, row.follow) for row in rows],
                         [(finding_id, line + 1, style_ledger.Filed(self.root / "decision.md"),
                           style_ledger.FollowedDeclaration(PARENT_FILE, line, tip)) for finding_id, line in zip(ids, (2, 3))])
        cases = (
            ("device", "fn device_line() { Device::new() }\n", "fn total_line() { Total::new() }\n"),
            ("CPU test", "fn original() {\n" + "\n".join(f"    check_{i}();" for i in range(8)) + "\n}\n",
             "fn renamed() {\n" + "\n".join([f"    check_{i}();" for i in range(3)] + [f"    changed_{i}();" for i in range(5)]) + "\n}\n"),
            ("internal docs count", "struct CratesIoInfo {\n    /// old one\n    a: u32,\n    /// old two\n    b: u32,\n    /// old three\n    /// old four\n    c: u32,\n}\n",
             "struct CratesIoRelease {\n    /// new one\n    a: u32,\n    /// new two\n    b: u32,\n    /// new three\n    /// new four\n    c: u32,\n}\n"),
            ("changed keyword", "struct CratesIoFields {\n" + "\n".join(f"    field_{i}: u32," for i in range(9)) + "\n}\n",
             "enum CratesIoSection {\n    Value,\n}\n"),
        )
        for index, (name, old, after) in enumerate(cases):
            with self.subTest(case=name):
                with style_ledger.Ledger(self.repo, self.root / f"live-limit-{index}.db") as other:
                    _ = self.commit_tip(other, {PARENT_FILE: old, MOVED_FILE: "// empty\n", TWIN_FILE: "// empty\n"})
                    _, (finding_id,) = self.judge_parent(other, ((1, old.splitlines()[0]),), kind="restructure")
                    destination = PARENT_FILE if name == "changed keyword" else MOVED_FILE
                    changes: dict[str, str | None] = {PARENT_FILE: after} if destination == PARENT_FILE else {PARENT_FILE: "// removed\n", MOVED_FILE: after}
                    _ = self.commit_tip(other, changes)
                    before = self.held_rows(other.database)
                    outcome, = self.follow_approved(other, [finding_id])
                    self.assertIsInstance(outcome, style_ledger.OutsideRenameUnresolved)
                    self.assertEqual(self.held_rows(other.database), before)
                    self.assertIn(destination, repr(outcome))
                    if isinstance(outcome, style_ledger.OutsideRenameUnresolved):
                        if name == "changed keyword":
                            self.assertEqual(outcome.evidence.mapped, declarations.MappedDeclaration(
                                PARENT_FILE, 1, declarations.Declaration("enum", "CratesIoSection", declarations.ModuleItem()),
                            ))
                        candidates = outcome.evidence.candidates
                        if isinstance(candidates, style_ledger.OutsideCandidates):
                            counts = {(item.unchanged, item.old_nonblank_count) for item in candidates.items if item.path == destination}
                            if name == "changed keyword":
                                self.assertIn((0, 9), counts)
                            elif name == "internal docs count":
                                self.assertIn((3, 7), counts)
                            elif name == "CPU test":
                                self.assertIn((3, 8), counts)
                            elif name == "device":
                                self.assertIn((0, 1), counts)

        for index, name in enumerate(("account name", "LocalAnswers", "renders_primary_marker")):
            with self.subTest(case=name):
                with style_ledger.Ledger(self.repo, self.root / f"live-existing-{index}.db") as other:
                    if name == "renders_primary_marker":
                        old = "fn renders_primary_marker() {\n    primary();\n    label();\n}\n"
                        existing = old.replace("renders_primary_marker", "worktree_entry_label")
                        target = existing + "// absorbed into the old label function\n"
                    else:
                        old = f"struct {name.replace(' ', '_')} {{\n    value: u32,\n}}\n"
                        existing = old
                        target = old.replace(name.replace(" ", "_"), "Different")
                    _ = self.commit_tip(other, {PARENT_FILE: old, MOVED_FILE: existing, TWIN_FILE: "// empty\n"})
                    _, (finding_id,) = self.judge_parent(other, ((1, old.splitlines()[0]),), kind="restructure")
                    _ = self.commit_tip(other, {PARENT_FILE: target if name != "renders_primary_marker" else "// removed\n",
                                              MOVED_FILE: target if name == "renders_primary_marker" else existing})
                    before = self.held_rows(other.database)
                    outcome, = self.follow_approved(other, [finding_id])
                    self.assertNotIsInstance(outcome, style_ledger.DeclarationMoved)
                    self.assertEqual(self.held_rows(other.database), before)


    def test_a_test_module_glob_does_not_block_a_file_level_trait_impl(self) -> None:
        for index, destination in enumerate((PARENT_FILE, MOVED_FILE)):
            with self.subTest(destination=destination):
                ledger = style_ledger.Ledger(self.repo, self.root / f"module-glob-{index}.db")
                self.addCleanup(ledger.close)
                source = (
                    "use crate::c::Trait;\nimpl Trait for Type {\n    fn method(&self) { 7 }\n}\n"
                    "#[cfg(test)]\nmod tests { use super::*; }\n"
                )
                _ = self.commit_tip(ledger, {PARENT_FILE: source, MOVED_FILE: "pub fn helper() {}\n",
                                           TWIN_FILE: "pub trait Trait { fn method(&self); }\n"})
                _, (finding_id,) = self.judge_parent(ledger, ((3, "7"),), kind="restructure")
                changes: dict[str, str | None]
                if destination == PARENT_FILE:
                    changes = {PARENT_FILE: source + "pub fn unrelated() {}\n"}
                else:
                    changes = {PARENT_FILE: "pub fn unrelated() {}\n", MOVED_FILE: source}
                tip = self.commit_tip(ledger, changes)
                before = self.held_rows(ledger.database)
                outcome, = self.follow_approved(ledger, [finding_id])
                if destination == PARENT_FILE:
                    self.assertIsInstance(outcome, style_ledger.DeclarationInPlace)
                    self.assertEqual(self.held_rows(ledger.database), before)
                else:
                    self.assertIsInstance(outcome, style_ledger.DeclarationMoved)
                    row, = ledger.held_findings(style_ledger.HeldFindingsInState("filed"))
                    self.assertEqual((row.finding_id, row.path, row.line, row.state, row.follow),
                                     (finding_id, destination, 3, style_ledger.Filed(self.root / "decision.md"),
                                      style_ledger.FollowedDeclaration(PARENT_FILE, 3, tip)))


    def test_a_block_import_of_the_trait_name_does_not_block_the_file_level_impl(self) -> None:
        ledger = self.ledger()
        source = (
            "use other::Hittable;\nimpl Hittable<Target> for Pane {\n    fn method(&self) { 7 }\n}\n"
            "#[cfg(test)]\nmod tests { use other::Hittable; }\n"
        )
        _ = self.commit_tip(ledger, {PARENT_FILE: source})
        _, (finding_id,) = self.hold_parent(ledger, ((3, "7"),), kind="restructure")
        _ = self.commit_tip(ledger, {PARENT_FILE: source + "pub fn unrelated() {}\n"})
        before = self.held_rows(ledger.database)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(outcome, style_ledger.DeclarationInPlace)
        self.assertEqual(self.held_rows(ledger.database), before)


    def test_a_relative_trait_import_resolves_from_the_file_module(self) -> None:
        cases = (
            ("crates/alpha/src/nested/a.rs", "use super::Trait;", "crate::nested::Trait", "same file"),
            ("crates/alpha/src/nested/a.rs", "use self::inner::Trait;", "crate::nested::a::inner::Trait", "same file"),
            ("crates/alpha/src/nested/deeper/a.rs", "use super::super::Trait;", "crate::nested::Trait", "same file"),
            ("crates/alpha/src/nested/mod.rs", "use self::Trait;", "crate::nested::Trait", "same file"),
            ("crates/alpha/src/nested/a.rs", "use super::Trait;", "crate::nested::Trait", "move"),
            ("crates/alpha/src/nested/a.rs", "use super::Trait;", "crate::different::Trait", "different trait"),
            ("crates/alpha/src/nested/a.rs", "", "self::inner::Trait", "direct header"),
            ("crates/alpha/src/nested/a.rs", "", "super::Trait", "direct header"),
        )
        for index, (path, imported, rooted, action) in enumerate(cases):
            with self.subTest(path=path, imported=imported, action=action, rooted=rooted):
                ledger = style_ledger.Ledger(self.repo, self.root / f"relative-trait-{index}.db")
                self.addCleanup(ledger.close)
                trait = rooted if action == "direct header" else "Trait"
                source = f"{imported}\nimpl {trait} for Type {{\n    fn method(&self) {{ 7 }}\n}}\n"
                parents: dict[str, str | None] = {
                    "crates/alpha/src/lib.rs": "mod nested;\n",
                    "crates/alpha/src/nested/mod.rs": "mod a; mod b; mod deeper;\n",
                    "crates/alpha/src/nested/deeper/mod.rs": "mod a;\n",
                }
                parents[path] = source
                _ = self.commit_tip(ledger, parents)
                _, (finding_id,) = self.judge_parent(ledger, ((3, "7"),), path=path, kind="restructure")
                if action == "direct header":
                    absolute = "crate::nested::a::inner::Trait" if rooted.startswith("self") else "crate::nested::Trait"
                    after = source.replace(rooted, absolute)
                else:
                    after = source.replace(imported, f"use {rooted};")
                destination = "crates/alpha/src/nested/b.rs" if action == "move" else path
                changes: dict[str, str | None] = {destination: after}
                if destination != path:
                    changes[path] = "pub fn unrelated() {}\n"
                tip = self.commit_tip(ledger, changes)
                before = self.held_rows(ledger.database)
                outcome, = self.follow_approved(ledger, [finding_id])
                if action == "different trait":
                    self.assertIsInstance(outcome, (style_ledger.TraitIdentityUncertain, style_ledger.DeclarationGoneFromFile))
                    self.assertEqual(self.held_rows(ledger.database), before)
                elif action == "move":
                    self.assertIsInstance(outcome, style_ledger.DeclarationMoved)
                    row = next(row for row in ledger.held_findings(style_ledger.AllHeldFindings())
                               if row.finding_id == finding_id)
                    self.assertEqual((row.path, row.line, row.state, row.follow),
                                     (destination, 3, style_ledger.Filed(self.root / "decision.md"),
                                      style_ledger.FollowedDeclaration(path, 3, tip)))
                else:
                    self.assertIsInstance(outcome, style_ledger.DeclarationInPlace)
                    self.assertEqual(self.held_rows(ledger.database), before)


    def test_block_and_relative_import_limits_keep_the_finding(self) -> None:
        cases = (
            (PARENT_FILE, "#[cfg(test)]\nmod tests {\nuse super::*;\nimpl Trait for Type {\n    fn method(&self) { 7 }\n}\n}\n", 5),
            (PARENT_FILE, "use crate::c::*;\nimpl Trait for Type {\n    fn method(&self) { 7 }\n}\n", 3),
            (PARENT_FILE, "#[cfg(test)] use crate::c::Trait;\nimpl Trait for Type {\n    fn method(&self) { 7 }\n}\n", 3),
            (PARENT_FILE, "use super::super::Trait;\nimpl Trait for Type {\n    fn method(&self) { 7 }\n}\n", 3),
            ("crates/alpha/examples/a.rs", "use self::Trait;\nimpl Trait for Type {\n    fn method(&self) { 7 }\n}\n", 3),
            (PARENT_FILE, "mod tests {\nuse crate::c::Trait;\nimpl Trait for Type {\n    fn method(&self) { 7 }\n}\n}\n", 4),
            (PARENT_FILE, "mod tests {\nimpl self::Trait for Type {\n    fn method(&self) { 7 }\n}\n}\n", 3),
            (PARENT_FILE, "fn container() {\nuse crate::c::*;\nimpl Trait for Type {\n    fn method(&self) { 7 }\n}\n}\n", 4),
        )
        for index, (path, source, line) in enumerate(cases):
            with self.subTest(path=path, source=source):
                ledger = style_ledger.Ledger(self.repo, self.root / f"trait-import-limit-{index}.db")
                self.addCleanup(ledger.close)
                _ = self.commit_tip(ledger, {path: source, TWIN_FILE: "pub trait Trait {}\n"})
                _, (finding_id,) = self.hold_parent(ledger, ((line, "7"),), path=path, kind="restructure")
                _ = self.commit_tip(ledger, {path: source + "pub fn unrelated() {}\n"})
                before = self.held_rows(ledger.database)
                outcome, = self.follow_approved(ledger, [finding_id])
                self.assertIsInstance(outcome, style_ledger.TraitIdentityUncertain)
                self.assertEqual(self.held_rows(ledger.database), before)


    def test_same_line_built_qualifiers_keep_enclosing_aliases_uncertain(self) -> None:
        sites = (
            "mod imports { use crate::other::Other as Trait; pub fn caller() { Trait::old(); } }",
            "pub fn caller() { use crate::other::Other as Trait; Trait::old(); }",
            "pub fn caller() { Trait::old(); } mod imports { use crate::other::Other as Trait; pub fn caller() { Trait::old(); } }",
        )
        for index, site in enumerate(sites):
            with self.subTest(site=site):
                ledger = style_ledger.Ledger(self.repo, self.root / f"same-line-{index}.db")
                self.addCleanup(ledger.close)
                source = "impl crate::b::Trait for Type {\n    fn old(&self) { 7 }\n}\n"
                _ = self.commit_tip(ledger, {PARENT_FILE: source, MOVED_FILE: "pub trait Trait {}\n",
                                           TWIN_FILE: site + "\n"})
                _, (finding_id,) = self.judge_parent(ledger, ((2, "7"),), kind="restructure")
                _ = self.built_finding(ledger, TWIN_FILE, "Trait::old", "Trait::new")
                _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("fn old", "fn new")})
                before = self.held_rows(ledger.database)
                outcome, = self.follow_approved(ledger, [finding_id])
                self.assertIsInstance(outcome, style_ledger.TraitIdentityUncertain)
                self.assertEqual(self.held_rows(ledger.database), before)


    def test_unproved_module_layouts_keep_relative_trait_findings_unchanged(self) -> None:
        root = "crates/alpha/src/"
        cases: tuple[tuple[str, str, dict[str, str]], ...] = (
            ("nested/main.rs", "crate::nested::Trait", {"lib.rs": "mod nested;", "nested/mod.rs": "pub mod main;"}),
            ("nested/lib.rs", "crate::nested::Trait", {"lib.rs": "mod nested;", "nested/mod.rs": "pub mod lib;"}),
            ("bin/tool.rs", "crate::bin::tool::Trait", {}),
            ("actual.rs", "crate::actual::Trait", {"lib.rs": '#[path="actual.rs"] mod renamed;'}),
            ("missing/a.rs", "crate::missing::a::Trait", {}),
            ("guarded.rs", "crate::guarded::Trait", {"lib.rs": "#[cfg(test)] mod guarded;"}),
            ("custom.rs", "crate::custom::Trait", {"lib.rs": "mod custom;"}),
        )
        for index, (relative, rooted, parents) in enumerate(cases):
            with self.subTest(relative=relative):
                ledger = style_ledger.Ledger(self.repo, self.root / f"layout-{index}.db")
                self.addCleanup(ledger.close)
                source = "impl self::Trait for Type {\n    fn method(&self) { 7 }\n}\n"
                path = root + relative
                files: dict[str, str | None] = {root + name: text + "\n" for name, text in parents.items()}
                files[path] = source
                if relative == "custom.rs":
                    files["crates/alpha/Cargo.toml"] = ('[package]\nname="alpha"\nversion="0.1.0"\n'
                                                       '[lib]\npath="src/custom.rs"\n')
                _ = self.commit_tip(ledger, files)
                _, (finding_id,) = self.hold_parent(ledger, ((2, "7"),), path=path, kind="restructure")
                _ = self.commit_tip(ledger, {path: source.replace("self::Trait", rooted)})
                before = self.held_rows(ledger.database)
                outcome, = self.follow_approved(ledger, [finding_id])
                self.assertIsInstance(outcome, style_ledger.TraitIdentityUncertain)
                self.assertEqual(self.held_rows(ledger.database), before)


    def test_a_nested_main_module_does_not_move_a_finding_to_its_parent_trait(self) -> None:
        ledger = self.ledger()
        root = "crates/alpha/src/"
        path = root + "nested/main.rs"
        destination = root + "destination.rs"
        source = "impl self::Trait for Type {\n    fn method(&self) { 7 }\n}\n"
        _ = self.commit_tip(ledger, {root + "lib.rs": "mod nested; mod destination;\n",
                                   root + "nested/mod.rs": "pub mod main;\n", path: source})
        _, (finding_id,) = self.judge_parent(ledger, ((2, "7"),), path=path, kind="restructure")
        _ = self.commit_tip(ledger, {path: "pub fn unrelated() {}\n",
                                   destination: source.replace("self::Trait", "crate::nested::Trait")})
        before = self.held_rows(ledger.database)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(outcome, style_ledger.TraitIdentityUncertain)
        self.assertEqual(self.held_rows(ledger.database), before)


    def test_a_plain_main_module_chain_keeps_relative_shortcuts_identity(self) -> None:
        ledger = self.ledger()
        root = "crates/alpha/src/"
        path = root + "tui/keys/output.rs"
        destination = root + "tui/keys/other.rs"
        source = "use super::Shortcuts;\nimpl Shortcuts for Pane {\n    fn method(&self) { 7 }\n}\n"
        _ = self.commit_tip(ledger, {
            root + "lib.rs": None,
            root + "main.rs": "mod tui;\n",
            root + "tui/mod.rs": "mod keys;\n",
            root + "tui/keys.rs": "mod output; mod other; pub trait Shortcuts {}\n",
            path: source,
            destination: "pub fn unrelated() {}\n",
        })
        _, (finding_id,) = self.judge_parent(ledger, ((3, "7"),), path=path, kind="restructure")
        _ = self.commit_tip(ledger, {path: "pub fn unrelated() {}\n", destination: source.replace(
            "super::Shortcuts", "crate::tui::keys::Shortcuts")})
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(outcome, style_ledger.DeclarationMoved)
        row = next(row for row in ledger.held_findings(style_ledger.AllHeldFindings()) if row.finding_id == finding_id)
        self.assertEqual((row.path, row.line, row.state), (destination, 3, style_ledger.Filed(self.root / "decision.md")))



if __name__ == "__main__":
    _ = unittest.main()
