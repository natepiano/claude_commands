"""Clean cell guards and corrections for merged tickets."""

from __future__ import annotations

from scripts.style.ledger_test_support import ticket_owner_id

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import cast, final

from scripts.style import rules
from scripts.style import style_ledger
from scripts.style.ledger_test_support import LedgerTestCase


@final
class HeldCellCorrectionTests(LedgerTestCase):
    ticket = "1"

    def correction_fixture(self, *, hold: bool = True, merge: bool = True) -> tuple[str, str, Path]:
        rule = self.rule("correction")
        path = self.sources[0]
        with self.ledger() as ledger:
            ledger.set_tip(self.initial_tip, "main")
            ledger.ticket_add(self.ticket, ticket_owner_id("names"), "read", "names", "crates/alpha", [path], [rule])
            ledger.ticket_assign(self.ticket, "main")
            _ = ledger.ticket_base(self.ticket)
            ledger.record(rule, path, self.initial_tip, self.blob(rule), "clean", ticket=self.ticket, worker="judge")
            if hold:
                finding = ledger.hold(rule, path, 1, "pub fn value() -> u32 { 1 }", "restructure", "before", "after",
                                      self.blob(rule), self.ticket)
                ledger.file_held(finding.finding_id, self.root / "decision.md")
            if merge:
                ledger.ticket_checkpoint(self.ticket, self.initial_tip)
                _ = ledger.ticket_merge(self.ticket)
            database = ledger.database
        return rule, path, database

    def correction(self, rule: str, path: str, *, dry_run: bool = False, success: bool = True) -> str:
        arguments = ("correct-clean-to-held", "--ticket", self.ticket, "--rule", rule, "--path", path)
        result = self.cli(*arguments, *(("--dry-run",) if dry_run else ()), success=success)
        if not success:
            self.assertNotEqual(result.returncode, 0)
        return result.stdout + result.stderr

    def start_judgment(self, ledger: style_ledger.Ledger, rule: str, path: str, ticket: str, family: rules.Family = "names") -> str:
        owner = "first-clean-" + str(int(ticket) - 50000) if ticket in ("50001", "50002") else "judge"
        ledger.ticket_add(ticket, ticket_owner_id(owner), "read", family, "crates/alpha", [path], [rule])
        ledger.ticket_assign(ticket, "main")
        _ = ledger.ticket_base(ticket)
        return ticket

    def test_clean_refuses_unfiled_and_filed_rows_without_writing(self) -> None:
        rule = self.rule("open")
        with self.ledger() as ledger:
            ledger.set_tip(self.initial_tip)
            ticket = self.start_judgment(ledger, rule, self.sources[0], "4001")
            first = ledger.hold(rule, self.sources[0], 1, "first", "restructure", "before", "after", self.blob(rule), ticket)
            second = ledger.hold(rule, self.sources[0], 2, "second", "restructure", "before", "after", self.blob(rule), ticket)
            ledger.record(rule, self.sources[0], self.initial_tip, self.blob(rule), "held", ticket=ticket)
            for filed in (False, True):
                with self.subTest(filed=filed):
                    if filed:
                        ledger.file_held(first.finding_id, self.root / "decision.md")
                    before = ledger.database.read_bytes()
                    with self.assertRaisesRegex(style_ledger.LedgerError, "cannot record clean with open held rows") as refusal:
                        ledger.record(rule, self.sources[0], self.initial_tip, self.blob(rule), "clean", ticket="outside")
                    self.assertIn(first.finding_id, str(refusal.exception))
                    self.assertIn(second.finding_id, str(refusal.exception))
                    self.assertEqual(ledger.database.read_bytes(), before)

    def test_first_clean_refuses_a_new_tickets_open_rows_without_writing(self) -> None:
        for index, filed in enumerate((False, True), 1):
            with self.subTest(filed=filed), self.ledger() as ledger:
                rule = self.rule(f"first-clean-{index}")
                path = self.sources[0]
                ledger.set_tip(self.initial_tip)
                ticket = self.start_judgment(ledger, rule, path, str(50000 + index))
                finding = ledger.hold(rule, path, 1, "required change", "restructure", "before", "after",
                                      self.blob(rule), ticket)
                if filed:
                    ledger.file_held(finding.finding_id, self.root / f"decision-{index}.md")
                self.assertEqual([row for row in self.judgment_rows(ledger.database) if row[1] == rule], [])
                before = ledger.database.read_bytes()
                for _ in (1, 2):
                    with self.assertRaisesRegex(style_ledger.LedgerError, finding.finding_id):
                        ledger.record(rule, path, self.initial_tip, self.blob(rule), "clean", ticket=ticket)
                    self.assertEqual(ledger.database.read_bytes(), before)

    def test_newer_clean_with_changed_inputs_records_and_withdraws(self) -> None:
        for index, changed in enumerate(("file", "rule"), 1):
            with self.subTest(changed=changed), self.ledger() as ledger:
                rule = self.rule(f"changed-{changed}")
                path = self.sources[0]
                base = self.git("rev-parse", "HEAD")
                ledger.set_tip(base)
                ticket = self.start_judgment(ledger, rule, path, f"{4000 + index}")
                finding = ledger.hold(rule, path, 1, "required change", "restructure", "before", "after", self.blob(rule), ticket)
                ledger.record(rule, path, base, self.blob(rule), "held", ticket=ticket)
                ledger.ticket_drop(ticket)
                if changed == "file":
                    self.write(path, "pub fn changed() -> u32 { 2 }\n")
                    tip = self.commit("Change the judged file")
                else:
                    rule_path = self.rule_dir / "changed-rule.md"
                    _ = rule_path.write_text(rule_path.read_text() + "A new rule condition.\n")
                    tip = base
                ledger.set_tip(tip)
                try:
                    ledger.record(rule, path, tip, self.blob(rule), "clean", ticket="newer")
                except style_ledger.LedgerError as error:
                    self.fail(f"changed inputs must permit a new clean judgment: {error}")
                definitions = {definition.rule_id: definition for definition in rules.load_rules(self.repo)}
                result = ledger.withdraw_superseded(definitions)
                self.assertIn(finding.finding_id, tuple(item.finding.finding_id for item in result.withdrawn))
                current, = ledger.named_held((finding.finding_id,))
                self.assertIsInstance(current.state, style_ledger.Withdrawn)
                rows = self.judgment_rows(ledger.database)
                self.assertEqual([row[7] for row in rows if row[1] == rule], ["held", "clean"])

    def test_clean_uses_the_existing_manifest_cell_mapping(self) -> None:
        rule = self.rule("manifest", "tier: D\nfamily: modules\ninput_scope: manifest\n")
        with self.ledger() as ledger:
            ledger.set_tip(self.initial_tip)
            ticket = self.start_judgment(ledger, rule, "crates/alpha", "4001", "modules")
            finding = ledger.hold(rule, self.sources[0], 1, "split", "restructure", "before", "after", self.blob(rule), ticket)
            ledger.record(rule, "crates/alpha", self.initial_tip, self.blob(rule), "held", ticket=ticket)
            before = ledger.database.read_bytes()
            with self.assertRaisesRegex(style_ledger.LedgerError, finding.finding_id):
                ledger.record(rule, "crates/alpha", self.initial_tip, self.blob(rule), "clean", ticket="outside")
            self.assertEqual(ledger.database.read_bytes(), before)
            ledger.record(rule, "crates/beta", self.initial_tip, self.blob(rule), "clean", ticket="outside")

    def test_correction_records_evidence_preserves_inputs_and_is_idempotent(self) -> None:
        rule, path, database = self.correction_fixture()
        original = self.judgment_rows(database)[0]
        output = self.correction(rule, path)
        self.assertIn("corrected:", output)
        corrected = self.judgment_rows(database)[0]
        self.assertEqual(corrected[:7], original[:7])
        self.assertEqual(corrected[7], "held")
        self.assertEqual(corrected[8:], original[8:])
        with self.ledger() as reopened:
            findings = reopened.held_findings(style_ledger.HeldFindingsInState("filed"))
            self.assertEqual(len(findings), 1)
            self.assertEqual(reopened.latest_ticket_judgments(self.ticket)[0].verdict, "held")
        with closing(sqlite3.connect(database)) as connection:
            events = cast(list[tuple[int, str, str, str, str, str, str, str]], connection.execute(
                "SELECT * FROM held_cell_corrections").fetchall())
            self.assertEqual(connection.execute("SELECT state FROM tickets WHERE id=?", (self.ticket,)).fetchone(), ("merged",))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][:6], (original[0], self.ticket, rule, path, "clean", "held"))
        self.assertEqual(json.loads(events[0][6]), [findings[0].finding_id])
        self.assertTrue(events[0][7])
        before = database.read_bytes()
        self.assertIn("already corrected:", self.correction(rule, path))
        self.assertEqual(database.read_bytes(), before)
        result = self.cli("record", "--ticket", self.ticket, "--rule", rule, "--path", path,
                          "--commit", self.initial_tip, "--verdict", "held", "--worker", "judge", success=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot judge from merged", result.stderr)
        self.assertEqual(database.read_bytes(), before)

    def test_correction_cannot_forge_a_held_cell_without_live_rows(self) -> None:
        rule, path, database = self.correction_fixture(hold=False)
        before = database.read_bytes()
        self.assertIn("without open held rows", self.correction(rule, path, success=False))
        self.assertEqual(database.read_bytes(), before)
        with self.ledger() as ledger:
            other_rule = self.rule("other")
            _ = ledger.hold(other_rule, path, 1, "other rule", "restructure", "before", "after", self.blob(other_rule), "outside")
            finding = ledger.hold(rule, path, 1, "waived", "restructure", "before", "after", self.blob(rule), "outside")
            ledger.waive_held(finding.finding_id, style_ledger.WaiveByPerson("Nate"))
        before = database.read_bytes()
        self.assertIn("without open held rows", self.correction(rule, path, success=False))
        self.assertEqual(database.read_bytes(), before)

    def test_preview_reads_the_previous_schema_without_migration_or_writes(self) -> None:
        rule, path, database = self.correction_fixture()
        with closing(sqlite3.connect(database)) as connection, connection:
            _ = connection.execute("DROP TABLE ticket_counter")
            _ = connection.execute("DROP TABLE held_cell_corrections")
            _ = connection.execute("DROP TABLE rename_dispositions")
            _ = connection.execute("DROP TABLE rename_outer_binding_blocks")
            _ = connection.execute("UPDATE schema_version SET version=32")
        before = database.read_bytes()
        output = self.correction(rule, path, dry_run=True)
        self.assertIn("would correct:", output)
        self.assertIn("clean -> held", output)
        self.assertEqual(database.read_bytes(), before)
        self.assertEqual(self.judgment_rows(database)[0][7], "clean")
        self.assertIn("corrected:", self.correction(rule, path))
        before = database.read_bytes()
        self.assertIn("already corrected:", self.correction(rule, path, dry_run=True))
        self.assertEqual(database.read_bytes(), before)

    def test_correction_requires_a_merged_clean_cell(self) -> None:
        rule, path, database = self.correction_fixture(merge=False)
        before = database.read_bytes()
        self.assertIn("requires a merged ticket", self.correction(rule, path, success=False))
        self.assertEqual(database.read_bytes(), before)
        with self.ledger() as ledger:
            ledger.ticket_checkpoint(self.ticket, self.initial_tip)
            _ = ledger.ticket_merge(self.ticket)
        before = database.read_bytes()
        self.assertIn("no recorded cell", self.correction(rule, self.sources[1], success=False))
        self.assertEqual(database.read_bytes(), before)
        with closing(sqlite3.connect(database)) as connection, connection:
            _ = connection.execute("UPDATE judgments SET verdict='fixed'")
        before = database.read_bytes()
        self.assertIn("correction requires clean, found fixed", self.correction(rule, path, success=False))
        self.assertEqual(database.read_bytes(), before)
