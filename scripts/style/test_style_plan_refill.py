"""Unmerged product changes hold only the affected selected work."""

from __future__ import annotations

from scripts.style.ledger_test_support import ticket_owner_id

import io
import os
import sqlite3
import subprocess
import traceback
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from typing import cast, final
from unittest import mock


from scripts.style import style_plan
from scripts.style import rules
from scripts.style import style_proposals

from scripts.lib import berth_board
from scripts.style import coverage_report
from scripts.style import style_ledger

from scripts.production import staff
from scripts.production.issues import frontmatter

from scripts.style.style_plan_test_support import StylePlanFixture
from scripts.style.unit_plans import AllCrates


@final
class StylePlanTests(StylePlanFixture):
    def test_restored_rule_body_returns_the_unchanged_held_cell_to_refill(self) -> None:
        rule = self.rule("read")
        self.sync()
        _ = self.held_proposal(rule, self.sources[0])
        rule_file = self.rule_dir / "read.md"
        original = rule_file.read_text()
        with self.ledger() as ledger:
            _ = rule_file.write_text(original + "Changed body.\n")
            _ = ledger.withdraw_superseded({item.rule_id: item for item in rules.load_rules(self.repo)})
        _ = rule_file.write_text(original)
        for path in self.sources[1:]:
            self.record(rule, path, self.tip)
        self.assertEqual(self.candidates(), (style_plan.TicketSelection("read", "names", "crates/alpha",
                                                                       (self.sources[0],), (rule,)),))

    def test_refill_uses_the_planners_catalog_snapshot(self) -> None:
        _ = self.rule("read")
        self.sync()
        with self.ledger() as ledger, ledger.finding_reads():
            context = style_plan.RefillContext(ledger)
            definitions = {rule.rule_id: rule for rule in rules.load_rules(self.repo)}
            cells = tuple(ledger.cells())
            busy = style_plan.BusyFiles(self.board(), frozenset())
            scopes: dict[rules.Family, AllCrates] = {"names": AllCrates()}
            base = self.base()
            with (mock.patch.object(ledger, "_held_rows", wraps=ledger._held_rows) as rows,  # pyright: ignore[reportPrivateUsage]
                  mock.patch.object(style_plan, "source_line_counts", wraps=style_plan.source_line_counts) as lengths):
                first = style_plan.ranked_candidates(
                    self.repo, ledger, busy, ("names",), AllCrates(), scopes, base, frozenset(),
                    definitions, cells, context=context)
                self.assertTrue(first)
                self.assertEqual(rows.call_count, 1)
                reads = lengths.call_count
                self.assertGreater(reads, 0)
                second = style_plan.ranked_candidates(
                    self.repo, ledger, busy, ("names",), AllCrates(), scopes, base, frozenset(),
                    definitions, cells, context=context)
                self.assertEqual(second, first)
                self.assertEqual((rows.call_count, lengths.call_count), (1, reads))
                definition = next(iter(definitions.values()))
                finding = ledger.hold(definition.rule_id, self.sources[0], 1, "pub fn alpha() {}", "rename",
                                      "alpha", "better_alpha", definition.blob, "outside")
                before = sum(not call.args for call in rows.call_args_list)
                _ = style_plan.ranked_candidates(
                    self.repo, ledger, busy, ("names",), AllCrates(), scopes, base, frozenset(),
                    definitions, cells, context=context)
                self.assertEqual(sum(not call.args for call in rows.call_args_list), before + 1)
                self.assertGreater(lengths.call_count, reads)
                self.assertIn(finding, context.held[(definition.rule_id, self.sources[0])])
                # An external commit also starts a new observation.
                with closing(sqlite3.connect(ledger.database)) as connection, connection:
                    _ = connection.execute("UPDATE held_findings SET state='waived' WHERE finding_id=?",
                                           (finding.finding_id,))
                before = sum(not call.args for call in rows.call_args_list)
                _ = style_plan.ranked_candidates(
                    self.repo, ledger, busy, ("names",), AllCrates(), scopes, base, frozenset(),
                    definitions, cells, context=context)
                self.assertEqual(sum(not call.args for call in rows.call_args_list), before + 1)
                self.assertNotIn((definition.rule_id, self.sources[0]), context.held)
            counts = ledger.line_counts(self.tip, self.sources)
            with mock.patch.object(subprocess, "run", side_effect=AssertionError("unexpected Git process")):
                self.assertEqual(ledger.line_counts(self.tip, self.sources), counts)
            with closing(sqlite3.connect(ledger.database)) as connection:
                self.assertGreater(cast(tuple[int], connection.execute("SELECT COUNT(*) FROM refill_objects").fetchone())[0], 0)
        load = rules.load_rules

        def shared_catalog(repo: Path | None) -> tuple[rules.Rule, ...]:
            self.assertNotIn("_refill_cells", [frame.name for frame in traceback.extract_stack()])
            return load(repo)

        with mock.patch.object(rules, "load_rules", side_effect=shared_catalog):
            result, output, errors = self.run_next()
        self.assertEqual(result, 0, output + errors)

    def test_rule_body_change_refills_unchanged_held_files_before_resuming(self) -> None:
        read = self.rule("read")
        self.sync()
        _ = self.held_proposal(read, self.sources[0])
        ledger = self.ledger()
        ledger.ticket_add("1", ticket_owner_id("names"), "read", "names", "crates/alpha", [self.sources[0]], [read])
        ledger.ticket_assign("1", "demo-style-names")
        rule_file = self.rule_dir / "read.md"
        _ = rule_file.write_text(rule_file.read_text() + "Changed body.\n")
        # No source edit: the old open-proposal shortcut must not hide this cell.
        for path in self.sources[1:]:
            self.record(read, path, self.tip)
        candidates = self.candidates()
        self.assertEqual(candidates, (style_plan.TicketSelection("read", "names", "crates/alpha",
                                                               (self.sources[0],), (read,)),))
        result, output, errors = self.run_next()
        self.assertEqual(result, 0, output + errors)
        self.assertNotIn("resumed 1", output)
        with closing(sqlite3.connect(ledger.database)) as connection:
            self.assertEqual(connection.execute("SELECT state FROM tickets WHERE id='1'").fetchone(), ("dropped",))
        self.assertIsInstance(ledger.held_findings(style_ledger.AllHeldFindings())[0].state, style_ledger.Withdrawn)
        self.assertEqual(ledger.ticket_open(ticket_owner_id("names"))[0]["files"], [self.sources[0]])

    def test_old_held_judgment_does_not_hide_a_current_body_finding(self) -> None:
        read = self.rule("read")
        self.sync()
        issue = self.held_proposal(read, self.sources[0])
        with self.ledger() as ledger, ledger.finding_reads():
            definitions = {rule.rule_id: rule for rule in rules.load_rules(self.repo)}
            more = ledger.hold(read, self.sources[0], 3, "another held defect", "rename", "before_more", "after_more",
                               definitions[read].blob, "outside")
            ledger.file_held(more.finding_id, issue)
            context = style_plan.RefillContext(ledger)
            cells = (style_ledger.Cell(read, self.sources[0], "crates/alpha", "C", "names", "stale"),
                     style_ledger.Cell(read, self.sources[1], "crates/alpha", "C", "names", "open"),
                     style_ledger.Cell(read, self.sources[2], "crates/beta", "C", "names", "open"))
            scopes: dict[rules.Family, AllCrates] = {family: AllCrates() for family in style_plan.FAMILIES}
            busy = style_plan.BusyFiles(self.board(), frozenset())
            with (mock.patch.object(style_plan, "held_site_cells", wraps=style_plan.held_site_cells) as filtered,
                  mock.patch.object(style_plan, "frontmatter", wraps=frontmatter) as parsed,
                  mock.patch.object(ledger, "newest_held_judgment", wraps=ledger.newest_held_judgment) as newest,
                  mock.patch.object(style_ledger, "rule_changed_since_judged",
                                    wraps=style_ledger.rule_changed_since_judged) as changed,
                  mock.patch.object(rules, "judged_text_unchanged", wraps=rules.judged_text_unchanged) as matched,
                  mock.patch.object(context.reader, "blob", wraps=context.reader.blob) as blobs):
                first = style_plan.ranked_candidates(
                    self.repo, ledger, busy, style_plan.FAMILIES, AllCrates(), scopes, style_plan.NoUnitBranch(),
                    frozenset(), definitions, cells, context=context)
                self.assertTrue(first)
                self.assertTrue(all(self.sources[0] not in ticket.files for ticket in first))
                second = style_plan.ranked_candidates(
                    self.repo, ledger, busy, style_plan.FAMILIES, AllCrates(), scopes, style_plan.NoUnitBranch(),
                    frozenset(), definitions, cells, context=context)
                self.assertEqual(second, first)
                self.assertEqual((filtered.call_count, parsed.call_count, newest.call_count, changed.call_count,
                                  matched.call_count, blobs.call_count), (1, 1, 1, 1, 1, 1))
                original = issue.read_text()
                _ = issue.write_text(original.replace("status: open", "status: closed"))
                after_close = style_plan.ranked_candidates(
                    self.repo, ledger, busy, style_plan.FAMILIES, AllCrates(), scopes, style_plan.NoUnitBranch(),
                    frozenset(), definitions, cells, context=context)
                self.assertTrue(any(self.sources[0] in ticket.files for ticket in after_close))
                self.assertEqual((filtered.call_count, parsed.call_count), (2, 2))
                self.assertEqual((newest.call_count, changed.call_count, matched.call_count, blobs.call_count),
                                 (1, 1, 1, 1))
                ledger.finding_need(more.finding_id, self.sources[1])
                after_write = style_plan.ranked_candidates(
                    self.repo, ledger, busy, style_plan.FAMILIES, AllCrates(), scopes, style_plan.NoUnitBranch(),
                    frozenset(), definitions, cells, context=context)
                self.assertEqual(after_write, after_close)
                self.assertEqual((filtered.call_count, parsed.call_count), (3, 2))
                self.assertEqual((newest.call_count, changed.call_count, matched.call_count), (2, 2, 2))
                _ = issue.write_text(original)
                restored = style_plan.ranked_candidates(
                    self.repo, ledger, busy, style_plan.FAMILIES, AllCrates(), scopes, style_plan.NoUnitBranch(),
                    frozenset(), definitions, cells, context=context)
                self.assertEqual(restored, first)
                self.assertEqual((filtered.call_count, parsed.call_count), (4, 3))
                self.assertEqual(len(context.proposal_statuses), 1)
        rule_file = self.rule_dir / "read.md"
        _ = rule_file.write_text(rule_file.read_text() + "Changed body.\n")
        ledger = self.ledger()
        definitions = {rule.rule_id: rule for rule in rules.load_rules(self.repo)}
        _ = ledger.withdraw_superseded(definitions)
        finding = ledger.hold(read, self.sources[0], 2, "current-body finding", "rename", "old", "new",
                              definitions[read].blob, "outside")
        ledger.file_held(finding.finding_id, issue)
        for path in self.sources[1:]:
            self.record(read, path, self.tip)
        self.assertEqual(self.candidates(), (style_plan.TicketSelection("read", "names", "crates/alpha",
                                                                       (self.sources[0],), (read,)),))

    def test_repair_first_does_not_read_the_board_or_change_the_plan(self) -> None:
        text = self.plan.read_text() + "\n### Phase 3 — repair round 1 · status: todo\nFix a review finding.\n"
        _ = self.plan.write_text(text)
        with mock.patch.object(berth_board, "read_board", side_effect=AssertionError("board read")):
            self.assertEqual(self.run_next(), (0, "repair first\n", ""))
        self.assertEqual(self.plan.read_text(), text)
        self.assertFalse((self.root / "state").exists())

    def test_unknown_board_stops_next_phase_and_queue_without_writing_or_recording(self) -> None:
        _ = self.rule("read")
        self.sync()
        before = self.plan.read_bytes()
        with mock.patch.object(berth_board, "read_board", return_value=berth_board.BoardUnavailable("unreadable")):
            self.assertEqual(self.run_next(), (4, "board unavailable: unreadable\n", ""))
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(style_plan.main(["queue", "--repo", str(self.repo)]), 4)
            self.assertEqual(output.getvalue(), "board unavailable: unreadable\n")
        self.assertEqual(self.plan.read_bytes(), before)
        self.assertEqual(self.count_tickets(), 0)
        self.assertFalse((self.plan.parent.parent / "rules").exists())

    def test_refill_holds_the_selected_ticket_when_main_touches_its_file(self) -> None:
        _ = self.rule("read")
        self.sync()
        before = self.plan.read_bytes()
        self.write(self.sources[0], "pub fn changed_on_main() {}\n")
        _ = self.commit()
        self.assertEqual(self.run_next(), (
            3, f"waiting for sync: read crates/alpha: main touched {self.sources[0]}\n", ""))
        self.assertEqual(self.plan.read_bytes(), before)
        self.assertEqual(self.count_tickets(), 0)
        self.assertEqual([row[0] for row in self.sync_wait_rows()],
                         [str(next(row.id for row in staff.initial_snapshot().find(kind="unit") if row.name == "names"))])

    def test_refill_starts_on_the_old_synced_tip_when_main_touches_only_another_crate(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.write(self.sources[2], "pub fn other_crate_on_main() {}\n")
        _ = self.commit()
        self.assertEqual(self.run_next(), (0, "phase written: 1\n", ""))
        ticket = self.ledger().ticket_open(ticket_owner_id("names"))[0]
        self.assertEqual((ticket["crate"], ticket["base"]), ("crates/alpha", self.tip))
        self.assertEqual(ticket["files"], list(self.sources[:2]))
        self.assertEqual(self.sync_wait_rows(), [])

    def test_refill_starts_when_only_crate_and_workspace_build_inputs_change(self) -> None:
        _ = self.rule("read")
        self.sync()
        for path in ("crates/alpha/Cargo.toml", "crates/alpha/build.rs", "Cargo.toml", "Cargo.lock"):
            content = (self.repo / path).read_text() if (self.repo / path).exists() else ""
            self.write(path, content + "# incoming build input\n")
        _ = self.commit()
        self.assertEqual(self.run_next(), (0, "phase written: 1\n", ""))
        ticket = self.ledger().ticket_open(ticket_owner_id("names"))[0]
        self.assertEqual((ticket["files"], ticket["base"]), (list(self.sources[:2]), self.tip))
        self.assertEqual(self.sync_wait_rows(), [])

    def test_ticket_sync_check_ignores_lockfile_changes_for_rust_files(self) -> None:
        changes = style_plan.MainChanges(self.tip, frozenset({"Cargo.lock"}))
        self.assertEqual(style_plan.ticket_sync_check(changes, self.sources[:2]), style_plan.SyncNotNeeded())

    def test_ticket_sync_check_ignores_crate_manifest_and_build_script_changes_for_rust_files(self) -> None:
        changes = style_plan.MainChanges(self.tip, frozenset({"crates/alpha/Cargo.toml", "crates/alpha/build.rs"}))
        self.assertEqual(style_plan.ticket_sync_check(changes, self.sources[:2]), style_plan.SyncNotNeeded())

    def test_ticket_sync_check_waits_for_explicitly_listed_build_inputs(self) -> None:
        for path in ("Cargo.toml", "Cargo.lock", "crates/alpha/Cargo.toml", "crates/alpha/build.rs"):
            with self.subTest(path=path):
                changes = style_plan.MainChanges(self.tip, frozenset({path}))
                self.assertEqual(style_plan.ticket_sync_check(changes, (path,)),
                                 style_plan.SyncNeeded(f"main touched {path}"))

    def test_ticket_sync_check_waits_for_a_listed_rust_file_without_crate_metadata(self) -> None:
        path = self.sources[0]
        changes = style_plan.MainChanges(self.tip, frozenset({path}))
        self.assertEqual(style_plan.ticket_sync_check(changes, self.sources[:2]),
                         style_plan.SyncNeeded(f"main touched {path}"))

    def test_main_changes_does_not_read_the_workspace_for_touched_paths(self) -> None:
        self.write("README.md", "Main changed an unrelated file.\n")
        main = self.commit()
        ledger = self.ledger()
        with mock.patch.object(coverage_report, "workspace", side_effect=AssertionError("workspace read")):
            self.assertEqual(style_plan.main_changes(ledger, "main", self.tip),
                             style_plan.MainChanges(main, frozenset({"README.md"})))

    def test_refill_starts_after_a_rename_in_another_crate(self) -> None:
        _ = self.rule("read")
        self.sync()
        _ = self.git("mv", self.sources[2], "crates/beta/src/moved.rs")
        _ = self.commit()
        self.assertEqual(self.run_next(), (0, "phase written: 1\n", ""))
        self.assertEqual(self.ledger().ticket_open(ticket_owner_id("names"))[0]["base"], self.tip)
        self.assertEqual(self.sync_wait_rows(), [])

    def test_refill_starts_after_a_copy_in_another_crate(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.write("crates/beta/src/copied.rs", (self.repo / self.sources[2]).read_text())
        _ = self.commit()
        self.assertEqual(self.run_next(), (0, "phase written: 1\n", ""))
        self.assertEqual(self.ledger().ticket_open(ticket_owner_id("names"))[0]["base"], self.tip)
        self.assertEqual(self.sync_wait_rows(), [])

    def test_main_changes_reads_a_weak_copy_as_an_added_path(self) -> None:
        source = self.sources[2]
        shared = "".join(f"pub fn shared_{index:02d}() {{}}\n" for index in range(10))
        self.write(source, shared)
        self.commit_to_every_checkout()
        copied = "crates/beta/src/copied.rs"
        self.write(copied, shared + "".join(f"pub fn added_{index:02d}() {{}}\n" for index in range(8)))
        main = self.commit()
        detected = self.git("log", "--format=", "--name-status", "-M", "-C", "--find-copies-harder",
                            f"{self.tip}..{main}")
        self.assertRegex(detected, rf"^C0[56]\d\t{source}\t{copied}$")
        self.assertEqual(style_plan.main_changes(self.ledger(), "main", self.tip),
                         style_plan.MainChanges(main, frozenset({copied})))

    def test_main_changes_reads_both_rename_paths(self) -> None:
        old, new = self.sources[2], "crates/beta/src/moved.rs"
        _ = self.git("mv", old, new)
        main = self.commit()
        self.assertEqual(style_plan.main_changes(self.ledger(), "main", self.tip),
                         style_plan.MainChanges(main, frozenset({old, new})))

    def test_ticket_sync_check_uses_only_the_copy_destination(self) -> None:
        source, copied = self.sources[2], "crates/beta/src/copied.rs"
        self.write(copied, (self.repo / source).read_text())
        _ = self.commit()
        changes = style_plan.main_changes(self.ledger(), "main", self.tip)
        for path in (source, self.sources[0], copied):
            with self.subTest(path=path):
                expected = style_plan.SyncNeeded(f"main touched {copied}") if path == copied else style_plan.SyncNotNeeded()
                self.assertEqual(style_plan.ticket_sync_check(changes, (path,)), expected)

    def test_ticket_sync_check_uses_both_rename_paths(self) -> None:
        old, new = self.sources[2], "crates/beta/src/moved.rs"
        _ = self.git("mv", old, new)
        _ = self.commit()
        changes = style_plan.main_changes(self.ledger(), "main", self.tip)
        for path in (old, new, self.sources[0], self.sources[3]):
            with self.subTest(path=path):
                expected = style_plan.SyncNeeded(f"main touched {path}") if path in (old, new) else style_plan.SyncNotNeeded()
                self.assertEqual(style_plan.ticket_sync_check(changes, (path,)), expected)

    def test_ticket_sync_check_ignores_unlisted_build_inputs_after_copies_and_renames(self) -> None:
        ledger = self.ledger()
        for operation, path in (("copy", "Cargo.lock"), ("copy", "crates/alpha/build.rs"),
                                ("rename", "Cargo.toml"), ("rename", "crates/alpha/Cargo.toml")):
            with self.subTest(operation=operation, path=path):
                _ = self.git("reset", "--hard", self.tip)
                if operation == "copy":
                    self.write(path, (self.repo / self.sources[2]).read_text())
                else:
                    _ = self.git("mv", path, path + ".old")
                _ = self.commit()
                changes = style_plan.main_changes(ledger, "main", self.tip)
                self.assertEqual(style_plan.ticket_sync_check(changes, self.sources[:1]), style_plan.SyncNotNeeded())
                self.assertEqual(style_plan.ticket_sync_check(changes, (path,)),
                                 style_plan.SyncNeeded(f"main touched {path}"))

    def test_refill_holds_when_the_synced_tip_is_missing(self) -> None:
        _ = self.rule("read")
        self.sync()
        with mock.patch.object(style_plan, "synced_tip", return_value="0" * 40):
            code, output, errors = self.run_next()
        self.assertEqual(code, 3, errors)
        self.assertIn("waiting for sync: touched set unknown:", output)
        self.assertEqual(self.count_tickets(), 0)

    def test_refill_holds_when_the_synced_tip_and_main_diverge(self) -> None:
        _ = self.rule("read")
        self.sync()
        _ = (self.style / "style.txt").write_text("style tip\n")
        _ = self.git("add", "style.txt", cwd=self.style)
        _ = self.git("commit", "-qm", "Style tip", cwd=self.style)
        self.tip = self.git("rev-parse", "HEAD", cwd=self.style)
        self.sync()
        self.write("README.md", "main tip\n")
        _ = self.commit()
        self.assertEqual(self.run_next(), (
            3, "waiting for sync: touched set unknown: synced tip is not an ancestor of main\n", ""))

    def test_refill_checks_every_main_commit_even_when_the_edit_was_reverted(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.write(self.sources[0], "pub fn temporary() {}\n")
        _ = self.commit()
        self.write(self.sources[0], "pub fn value() {}\n")
        _ = self.commit()
        self.assertEqual(self.run_next(), (
            3, f"waiting for sync: read crates/alpha: main touched {self.sources[0]}\n", ""))

    def test_refill_preserves_a_queued_ticket_when_main_touches_its_file(self) -> None:
        rule = self.rule("read")
        self.sync()
        ledger = self.ledger()
        ledger.ticket_add("1", ticket_owner_id("names"), "read", "names", "crates/alpha", [self.sources[0]], [rule])
        self.write(self.sources[0], "pub fn changed_on_main() {}\n")
        _ = self.commit()
        self.assertEqual(self.run_next(), (
            3, f"waiting for sync: read crates/alpha: main touched {self.sources[0]}\n", ""))
        self.assertEqual([ticket["id"] for ticket in ledger.queued_tickets(ticket_owner_id("names"))], ["1"])
        self.assertEqual(ledger.ticket_open(ticket_owner_id("names")), ())

    def test_refill_holds_the_selected_decision_before_starting_it(self) -> None:
        rule = self.rule("read")
        self.sync()
        issue = self.approved_decision(rule, self.sources[:1], "rename")
        self.write(self.sources[0], "pub fn changed_on_main() {}\n")
        _ = self.commit()
        with mock.patch.object(style_proposals, "proposal_command") as start:
            self.assertEqual(self.run_next(), (
                3, f"waiting for sync: proposal crates/alpha {rule}: main touched {self.sources[0]}\n", ""))
        start.assert_not_called()
        self.assertEqual(self.count_tickets(), 0)
        self.assertEqual(self.ledger().proposal_findings(issue)[0].build_ticket, style_ledger.NoBuildTicket())

    def test_refill_starts_the_selected_decision_when_only_the_lockfile_changes(self) -> None:
        _ = self.doCleanups()
        for change in ("lockfile", "recorded need"):
            with self.subTest(change=change):
                case = StylePlanTests()
                case.setUp()
                try:
                    rule = case.rule("read")
                    case.sync()
                    issue = case.approved_decision(rule, case.sources[:1],
                                                   "restructure" if change == "recorded need" else "rename")
                    ledger = case.ledger()
                    finding, = ledger.proposal_findings(issue)
                    need = case.sources[2]
                    busy: tuple[str, ...] = ()
                    if change == "recorded need":
                        ledger.ticket_add("7001", ticket_owner_id("names"), "proposal", "names", "crates/alpha",
                                          [finding.path], [rule], proposal=str(issue), finding_ids=(finding.finding_id,))
                        ledger.finding_need(finding.finding_id, need)
                        ledger.finding_need(finding.finding_id, finding.path)
                        edits = style_plan.EditedFiles(case.repo, ledger, style_plan.NoUnitBranch())
                        footprint = edits.of(finding)
                        self.assertEqual((footprint[0], footprint[-1], footprint.count(finding.path)), (finding.path, need, 1))
                        self.assertIn(need, edits.of_ticket(ledger.ticket_get("7001")))
                        ledger.ticket_drop("7001")
                        self.assertEqual(ledger.finding_needs(finding.finding_id), tuple(sorted((finding.path, need))))
                        ledger.record(rule=rule, path=finding.path, commit=case.tip, verdict="held", detail="",
                                      ticket="outside", worker="test", rule_blob=finding.rule_blob,
                                      input_scope=style_ledger.DeclaredInputScope.RULE)
                        for path in case.sources[1:]:
                            case.record(rule, path, case.tip)
                        busy = (need,)
                    else:
                        case.write("Cargo.lock", "# incoming lockfile update\n")
                        _ = case.commit()
                    with (mock.patch.object(berth_board, "read_board", return_value=case.board(*busy)),
                          mock.patch.object(style_proposals, "proposal_command") as start):
                        code, output, errors = case.run_next()
                    if change == "recorded need":
                        start.assert_not_called()
                        self.assertEqual(code, 3, output + errors)
                        self.assertEqual(errors, "")
                        self.assertIn(f"waiting {issue}: {finding.path} edits {need}, busy (1 finding)\n", output)
                        self.assertEqual(case.count_tickets(), 1)
                        self.assertEqual(ledger.ticket_open(ticket_owner_id("names")), ())
                        self.assertEqual(ledger.named_held((finding.finding_id,))[0].build_ticket, style_ledger.NoBuildTicket())
                    else:
                        self.assertEqual(code, 0, errors + output)
                        start.assert_called_once()
                        ticket = ledger.ticket_open(ticket_owner_id("names"))[0]
                        self.assertEqual((ticket["kind"], ticket["proposal"], ticket["files"], ticket["base"]),
                                         ("proposal", str(issue), list(case.sources[:1]), case.tip))
                        self.assertEqual(case.sync_wait_rows(), [])
                finally:
                    case.doCleanups()

    def test_refill_holds_a_rename_when_main_adds_a_reference(self) -> None:
        rule = self.rule("read")
        issue = self.file_rename_decision(rule, (("crates/alpha/src/probe.rs", "Rename probe.rs to frame_log.rs"),))
        path = "crates/alpha/src/gauge.rs"
        self.write(path, "pub fn level() { crate::probe::sample(); }\n")
        _ = self.commit()
        before = self.plan.read_bytes()
        with mock.patch.object(style_proposals, "proposal_command") as start:
            self.assertEqual(self.run_next(), (
                3, f"waiting for sync: proposal crates/alpha {rule}: main touched {path}\n", ""))
        start.assert_not_called()
        self.assertEqual(self.count_tickets(), 0)
        self.assertEqual(self.plan.read_bytes(), before)
        self.assertEqual(self.ledger().proposal_findings(issue)[0].build_ticket, style_ledger.NoBuildTicket())

    def test_refill_preserves_a_queued_rename_when_main_adds_a_reference(self) -> None:
        rule = self.rule("read")
        issue = self.file_rename_decision(rule, (("crates/alpha/src/probe.rs", "Rename probe.rs to frame_log.rs"),))
        ledger = self.ledger()
        finding = ledger.proposal_findings(issue)[0]
        ledger.ticket_add("1", ticket_owner_id("names"), "proposal", "names", "crates/alpha", [finding.path], [rule],
                          proposal=str(issue), finding_ids=(finding.finding_id,))
        path = "crates/alpha/src/gauge.rs"
        self.write(path, "pub fn level() { crate::probe::sample(); }\n")
        _ = self.commit()
        with mock.patch.object(style_proposals, "proposal_command") as start:
            self.assertEqual(self.run_next(), (
                3, f"waiting for sync: proposal crates/alpha: main touched {path}\n", ""))
        start.assert_not_called()
        self.assertEqual([ticket["id"] for ticket in ledger.queued_tickets(ticket_owner_id("names"))], ["1"])
        self.assertEqual(ledger.ticket_open(ticket_owner_id("names")), ())

    def test_refill_holds_when_the_selected_footprint_at_main_cannot_be_read(self) -> None:
        rule = self.rule("read")
        _ = self.file_rename_decision(rule, (("crates/alpha/src/probe.rs", "Rename probe.rs to frame_log.rs"),))
        self.write(self.sources[2], "pub fn unrelated() {}\n")
        main = self.commit()
        original = style_plan.EditedFiles.of

        def footprint(edits: style_plan.EditedFiles, finding: style_ledger.HeldFinding) -> tuple[str, ...]:
            if edits.base == style_plan.UnitBranchTip(main):
                raise style_ledger.CheckCouldNotRun("main footprint unavailable")
            return original(edits, finding)

        with (mock.patch.object(style_plan.EditedFiles, "of", footprint),
              mock.patch.object(style_proposals, "proposal_command") as start):
            self.assertEqual(self.run_next(), (
                3, f"waiting for sync: proposal crates/alpha {rule}: touched set unknown: "
                + "cannot read selected footprint at main: main footprint unavailable\n", ""))
        start.assert_not_called()
        self.assertEqual(self.count_tickets(), 0)

    def test_refill_holds_when_main_touches_a_filename_with_a_carriage_return(self) -> None:
        rule = self.rule("read")
        path = "crates/alpha/src/line\rbreak.rs"
        self.write(path, "pub fn before() {}\n")
        self.commit_to_every_checkout()
        ledger = self.ledger()
        ledger.ticket_add("1", ticket_owner_id("names"), "read", "names", "crates/alpha", [path], [rule])
        self.write(path, "pub fn after() {}\n")
        _ = self.commit()
        self.assertEqual(self.run_next(), (
            3, f"waiting for sync: read crates/alpha: main touched {path}\n", ""))
        self.assertEqual([ticket["id"] for ticket in ledger.queued_tickets(ticket_owner_id("names"))], ["1"])
        self.assertEqual(ledger.ticket_open(ticket_owner_id("names")), ())

    def test_main_changes_refuses_a_path_that_cannot_cross_the_ledger_text_boundary(self) -> None:
        path = os.fsdecode(b"crates/alpha/src/non_utf8_\xff.rs")
        self.write(path, "pub fn new_file() {}\n")
        _ = self.commit()
        self.assertIsInstance(style_plan.main_changes(self.ledger(), "main", self.tip), style_plan.UnknownMainChanges)

    def test_refill_selects_work_despite_an_unpushed_local_branch_point_merge(self) -> None:
        _ = self.rule("read")
        self.sync()
        origin = self.root / "origin.git"
        _ = self.git("init", "-q", "--bare", str(origin))
        _ = self.git("remote", "add", "origin", str(origin))
        _ = self.git("push", "-q", "origin", "main")
        _ = (self.style / "README.md").write_text("Style checkpoint.\n")
        _ = self.git("add", "README.md", cwd=self.style)
        _ = self.git("commit", "-qm", "Style checkpoint", cwd=self.style)
        _ = self.git("merge", "--no-ff", "-qm", "Unpushed integration", "style/main")

        self.assertEqual(self.run_next(), (0, "phase written: 1\n", ""))

        self.assertEqual(self.count_tickets(), 1)
        self.assertEqual(self.sync_wait_rows(), [])

    def test_refill_selects_work_after_an_unrelated_local_branch_point_merge_is_pushed(self) -> None:
        _ = self.rule("read")
        self.sync()
        origin = self.root / "origin.git"
        _ = self.git("init", "-q", "--bare", str(origin))
        _ = self.git("remote", "add", "origin", str(origin))
        _ = self.git("push", "-q", "origin", "main")
        _ = (self.style / "README.md").write_text("Style checkpoint.\n")
        _ = self.git("add", "README.md", cwd=self.style)
        _ = self.git("commit", "-qm", "Style checkpoint", cwd=self.style)
        _ = self.git("merge", "--no-ff", "-qm", "Integration", "style/main")
        _ = self.git("push", "-q", "origin", "main")
        self.assertEqual(self.run_next(), (0, "phase written: 1\n", ""))
        self.assertEqual(self.ledger().ticket_open(ticket_owner_id("names"))[0]["base"], self.tip)
        self.assertEqual(self.sync_wait_rows(), [])

    def test_refill_selects_work_after_sync_merges_the_branch_point_into_a_distinct_style_tip(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.write(self.sources[0], "pub fn incoming_branch_point_work() {}\n")
        branch_tip = self.commit()
        self.assertEqual(self.run_next(), (
            3, f"waiting for sync: read crates/alpha: main touched {self.sources[0]}\n", ""))
        _ = self.git("merge", "--no-ff", "-qm", "Sync branch point", "main", cwd=self.style)
        self.tip = self.git("rev-parse", "HEAD", cwd=self.style)
        self.assertNotEqual(self.tip, branch_tip)
        self.sync()
        _ = self.git("merge", "-q", "--ff-only", "style/main", cwd=self.unit)
        self.assertEqual(self.run_next(), (0, "phase written: 1\n", ""))
        self.assertEqual(self.count_tickets(), 1)
        self.assertEqual(self.sync_wait_rows(), [])

    def test_assigned_ticket_resumes_unchanged_while_the_branch_point_is_unsynced(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.assertEqual(self.run_next(), (0, "phase written: 1\n", ""))
        before = self.plan.read_bytes()
        ticket = self.ledger().ticket_open(ticket_owner_id("names"))[0]
        self.write("README.md", "Branch point moved during an assigned ticket.\n")
        _ = self.commit()
        with mock.patch.object(berth_board, "read_board", side_effect=AssertionError("board read on resume")):
            self.assertEqual(self.run_next(), (0, "resume 1\n", ""))
        self.assertEqual(self.plan.read_bytes(), before)
        self.assertEqual(self.ledger().ticket_open(ticket_owner_id("names"))[0], ticket)
        self.assertEqual(self.count_tickets(), 1)
        self.assertEqual(self.sync_wait_rows(), [])

if __name__ == "__main__":
    _ = unittest.main()
