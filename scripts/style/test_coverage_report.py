"""The coverage report presents the ledger's committed coverage and report deltas."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, cast, final, override
from unittest import mock
from zoneinfo import ZoneInfo

import coverage_report
import rules
import style_ledger
import style_rules


@dataclass(frozen=True)
class ReportRuleRow:
    name: str
    tier: str
    current: str
    stale: str
    open: str
    awaiting_merge: str
    held: str
    done: str


class SavedRuleProgress(TypedDict):
    done: bool
    cells_total: int


class SavedReport(TypedDict):
    rules: dict[str, SavedRuleProgress]
    proposals: list[str]
    files_by_day: dict[str, int]


@final
class CoverageReportTests(unittest.TestCase):
    def test_undelivered_report_does_not_advance_baseline(self) -> None:
        rule = self.rule("read")
        self.sync()
        _ = self.report()
        with style_ledger.open_ledger(self.repo) as ledger:
            snapshot = ledger.database.parent / "last_report.json"
        before = snapshot.read_bytes()
        for path in self.sources:
            self.record(rule, path)
        with mock.patch("builtins.print", side_effect=InterruptedError("stop before delivery")):
            with self.assertRaises(InterruptedError):
                _ = coverage_report.main(["--repo", str(self.repo)])
        self.assertEqual(snapshot.read_bytes(), before)
        self.assertIn("1 rule gained", self.report().stdout)

    root: Path = Path()
    repo: Path = Path()
    rule_dir: Path = Path()
    tip: str = ""
    sources: tuple[str, ...] = tuple(
        f"crates/{crate}/src/{name}.rs"
        for crate in ("alpha", "beta") for name in ("lib", "other")
    )

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = self.root / "sample"
        self.rule_dir = self.root / "nate_style/rust"
        self.repo.mkdir()
        self.rule_dir.mkdir(parents=True)
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "STYLE_LEDGER_STATE_DIR": str(self.root / "state"),
            "STYLE_HISTORY_NATE_STYLE_DIR": str(self.rule_dir.parent),
            "USER_ZONE": "America/New_York",
        })))
        _ = self.enterContext(mock.patch.object(style_rules, "NATE_STYLE_DIR", self.rule_dir.parent))
        _ = self.git("init", "-q", "--initial-branch=main")
        _ = self.git("config", "user.name", "Coverage report tests")
        _ = self.git("config", "user.email", "coverage@example.invalid")
        _ = self.git("config", "commit.gpgsign", "false")
        self.write("Cargo.toml", '[workspace]\nmembers = ["crates/alpha", "crates/beta"]\n')
        for crate in ("alpha", "beta"):
            self.write(f"crates/{crate}/Cargo.toml", f'[package]\nname = "{crate}"\nversion = "0.1.0"\n')
        for source in self.sources:
            self.write(source, "pub fn value() -> u32 { 1 }\n")
        self.tip = self.commit("Create report workspace")

    def git(self, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), *arguments],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

    def write(self, relative: str, content: str) -> None:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(content)

    def commit(self, message: str) -> str:
        _ = self.git("add", "--all")
        _ = self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def rule(self, name: str, metadata: str = "tier: C\nfamily: names\ninput_scope: file\n") -> str:
        _ = (self.rule_dir / f"{name}.md").write_text(f"---\n{metadata}---\n# {name}\n")
        return f"global:rust/{name}.md"

    def sync(self, *, style_branch: str = "style/main") -> None:
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.set_tip(self.tip, style_branch)

    def record(self, rule: str, path: str, verdict: style_ledger.Verdict = "clean") -> None:
        definition = next(row for row in rules.load_rules(self.repo)
                          if row.rule_id == rule.removesuffix("#uncovered"))
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.record(rule, path, self.tip, definition.blob, verdict, ticket="report-1", worker="test",
                          input_scope=style_ledger.DeclaredInputScope.RULE)

    def report(self, *arguments: str, success: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("coverage_report.py")),
             "--repo", str(self.repo), *arguments],
            check=False, capture_output=True, text=True,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def rows(self, output: str) -> dict[str, ReportRuleRow]:
        rows: dict[str, ReportRuleRow] = {}
        for line in output.splitlines():
            if not line.startswith("|"):
                continue
            fields = [field.strip() for field in line.strip().strip("|").split("|")]
            if fields[0] == "Rule" or set(fields[0]) <= {"-", ":", " "}:
                continue
            self.assertEqual(len(fields), 8, line)
            row = ReportRuleRow(*fields)
            self.assertNotIn(row.name, rows, output)
            rows[row.name] = row
        return rows

    def held(self, rule: str, *, line: int = 1) -> str:
        definition = next(row for row in rules.load_rules(self.repo) if row.rule_id == rule)
        with style_ledger.open_ledger(self.repo) as ledger:
            return ledger.hold(rule, self.sources[0], line, "pub fn value() {}", "rename",
                               "value", "count", definition.blob, "report-1").finding_id

    def production_units(self, *units: str) -> None:
        doc = self.root / "sample-style/docs/sample-style-production.md"
        doc.parent.mkdir(parents=True, exist_ok=True)
        _ = doc.write_text("\n".join([
            "# Style production", "- **Kind:** style", "- **Branch point:** main", "",
            "## Units", "", "| Unit | Plan | Worktree | Branch |",
            "| --- | --- | --- | --- |",
            *(f"| {unit} | docs/{unit}.md | {self.root / unit} | main |" for unit in units), "",
        ]))


    def test_production_units_use_full_ids_without_a_document_and_mixed_ids_with_one(self) -> None:
        self.sync()
        with style_ledger.open_ledger(self.repo) as ledger:
            self.assertEqual(set(coverage_report.production(ledger, self.tip).units), {
                "sample-style-names-unit", "sample-style-types-unit",
                "sample-style-values-unit", "sample-style-modules-unit",
            })
            self.production_units("names-unit", "sample-style-types-unit", "feature-unit")
            self.assertEqual(coverage_report.production(ledger, self.tip).units,
                             ("names-unit", "sample-style-types-unit"))

    def test_report_finds_a_moved_ticket_by_the_declared_full_unit_id(self) -> None:
        rule = self.rule("read")
        self.sync()
        self.production_units("sample-style-names-unit")
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.ticket_add("names-unit-1", "names-unit", "read", "names", "crates/alpha",
                              list(self.sources[:2]), [rule])
            ledger.ticket_assign("names-unit-1", "main")
            _ = ledger.rename_unit("names-unit", "sample-style-names-unit")
        output = self.report("--rule", "read").stdout
        self.assertIn("names-unit-1", output)
        self.assertNotIn("Open tickets: none", output)

    def test_report_shows_each_widened_path_of_an_open_ticket_under_its_line(self) -> None:
        rule = self.rule("read")
        self.sync()
        self.production_units("names-unit")
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.ticket_add("names-unit-1", "names-unit", "read", "names", "crates/alpha",
                              list(self.sources[:2]), [rule])
            ledger.ticket_assign("names-unit-1", "main")
            database = ledger.database
        # Only a proposal ticket widens; the report shows the rows of any open ticket.
        with closing(sqlite3.connect(database)) as connection, connection:
            _ = connection.execute("INSERT INTO ticket_widened_paths VALUES (?,?,?,?,?)",
                                   ("names-unit-1", "crates/alpha/src/extra.rs", "the move needs a new file",
                                    "showrunner", "2026-10-25T23:06:34+00:00"))
        lines = self.report("--rule", "read").stdout.splitlines()
        opened = next(index for index, line in enumerate(lines) if line.startswith("names-unit-1 · assigned"))
        self.assertEqual(lines[opened + 1], "widened: crates/alpha/src/extra.rs — the move needs a new file "
                         + "(showrunner, 2026-10-25T23:06:34+00:00)")

    def test_manifest_structure_progress_counts_crates_while_file_structure_counts_files(self) -> None:
        manifest = self.rule("manifests", "tier: D\nfamily: modules\ninput_scope: manifest\n")
        structure = self.rule("structure", "tier: D\nfamily: modules\n")
        self.sync()
        self.record(manifest, "crates/alpha")
        self.record(structure, self.sources[0])
        rows = self.rows(self.report().stdout)
        self.assertEqual(rows["manifests"].current, "1 of 2 crates")
        self.assertEqual(rows["structure"].current, "1 of 4 files")

    def test_rows_cover_each_counted_tier_and_held_and_waived_judgments_still_finish_a_rule(self) -> None:
        _ = self.rule("lint", "tier: A\nmechanism: clippy\n")
        listed = self.rule("listed", "tier: B\nfamily: names\ninput_scope: file\npre_filter: value\n")
        read = self.rule("read")
        structure = self.rule("structure", "tier: D\nfamily: modules\n")
        _ = self.rule("process", "tier: E\n")
        self.sync()
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.lint_record(self.tip, "clippy", True, {})
        _ = self.held(read)
        for path in self.sources:
            self.record(listed, path)
            self.record(read, path, "held" if path == self.sources[0] else "waived")
        for path in self.sources:
            self.record(structure, path)

        output = self.report().stdout
        rows = self.rows(output)

        self.assertEqual({name: row.tier for name, row in rows.items()}, {
            "lint": "A", "listed": "B", "read": "C", "structure": "D",
        })
        self.assertEqual(rows["read"].current, "4 (100%)")
        self.assertEqual(rows["read"].held, "1")
        self.assertEqual(rows["read"].done, "yes, 1 held")
        self.assertEqual(rows["structure"].current, "4 of 4 files")
        self.assertEqual(rows["listed"].done, "yes")
        self.assertIn("Rules done: 4 of 4 (1 by lint)", output)

    def test_partial_rule_combines_both_components_without_a_pseudo_rule_row(self) -> None:
        partial = self.rule("partial", "tier: B\nfamily: names\ninput_scope: file\ncandidates:\n"
                            + "  kind: regex\n  pattern: value\n  surface: partial\n  uncovered: other names\n")
        self.sync()
        for path in self.sources:
            self.record(partial, path)
        first = self.report().stdout
        self.assertEqual(set(self.rows(first)), {"partial"})
        self.assertEqual(self.rows(first)["partial"].current, "4 (50%)")
        self.assertEqual(self.rows(first)["partial"].open, "4")
        self.assertTrue(self.rows(first)["partial"].done.startswith("no"), first)
        self.assertNotIn("#uncovered", first)
        for path in self.sources:
            self.record(partial + "#uncovered", path)
        completed = self.report().stdout
        self.assertEqual(self.rows(completed)["partial"].current, "8 (100%)")
        self.assertEqual(self.rows(completed)["partial"].done, "yes")
        _ = (self.rule_dir / "partial.md").write_text((self.rule_dir / "partial.md").read_text() + "New requirement\n")
        stale = self.report().stdout
        self.assertEqual(self.rows(stale)["partial"].stale, "8")
        self.assertEqual(self.rows(stale)["partial"].current, "0 (0%)")
        self.assertNotIn("#uncovered", stale)

    def test_zero_listed_sites_still_require_the_uncovered_read(self) -> None:
        partial = self.rule("zero-sites", "tier: B\nfamily: names\ninput_scope: file\ncandidates:\n"
                            + "  kind: regex\n  pattern: absent_marker\n  surface: partial\n  uncovered: other names\n")
        self.sync()
        initial = self.rows(self.report().stdout)["zero-sites"]
        self.assertEqual((initial.current, initial.open), ("0 (0%)", "4"))
        self.assertTrue(initial.done.startswith("no"))
        for path in self.sources:
            self.record(partial + "#uncovered", path)
        completed = self.report().stdout
        self.assertEqual(self.rows(completed)["zero-sites"].done, "yes")
        self.assertIn("Rules done: 1 of 1", completed)

    def test_header_numbers_and_local_times_match_the_ledger_summary(self) -> None:
        read = self.rule("read")
        self.sync()
        _ = self.report()
        for path in self.sources:
            self.record(read, path)
        finding = self.held(read)
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.file_held(finding, self.root / "proposal.md")
        self.write(self.sources[0], "pub fn changed() {}\n")
        self.tip = self.commit("Make one cell stale")
        self.sync()
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.handoff_add(self.tip)
            summary = ledger.summary()
        output = self.report().stdout
        self.assertIn(f"Rules done: {summary.rules_done} of {summary.rules_total}", output)
        percentage = f"{summary.cells_current_pct:.1f}".removesuffix(".0")
        self.assertIn(f"cells current {percentage}%", output)
        self.assertIn(f"stale {summary.stale}", output)
        self.assertTrue(output.startswith("sample-style — synced main "), output)
        self.assertIn(", 4 files", output.splitlines()[0])
        self.assertIn(summary.synced_tip[:7], output.splitlines()[0])
        self.assertIn(summary.synced_at.astimezone(ZoneInfo("America/New_York")).strftime("%H:%M %Z"), output.splitlines()[0])
        self.assertIsInstance(summary.waiting, style_ledger.PendingHandoffs)
        if isinstance(summary.waiting, style_ledger.PendingHandoffs):
            oldest = summary.waiting.oldest_pending_since.astimezone(ZoneInfo("America/New_York")).strftime("%H:%M")
            self.assertEqual(
                next(line for line in output.splitlines() if line.startswith("Waiting to merge back:")),
                f"Waiting to merge back: 1 checkpoint, oldest {oldest}",
            )
        self.assertIn(f"{summary.proposals} proposal filed", output)

    def test_header_rounds_two_of_three_current_cells_to_one_decimal(self) -> None:
        read = self.rule("read")
        (self.repo / self.sources[-1]).unlink()
        self.tip = self.commit("Keep three coverage cells")
        self.sync()
        for path in self.sources[:2]:
            self.record(read, path)

        output = self.report().stdout

        self.assertIn("cells current 66.7%", output.splitlines()[1])
        self.assertEqual(self.rows(output)["read"].current, "2 (66.7%)")

    def test_replacing_a_filed_proposal_counts_the_new_identity(self) -> None:
        read = self.rule("read")
        self.sync()
        first = self.held(read)
        duplicate = self.held(read, line=2)
        old_proposal = self.root / "old.md"
        new_proposal = self.root / "new.md"
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.file_held(first, old_proposal)
            ledger.file_held(duplicate, old_proposal)
            snapshot = ledger.database.parent / "last_report.json"
        initial = self.report().stdout
        self.assertIn("1 proposal filed.", initial.splitlines()[-1])
        stored = cast(SavedReport, json.loads(snapshot.read_text()))
        self.assertEqual(stored["proposals"], [str(old_proposal)])

        replacement = self.held(read, line=3)
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.waive_held(first, style_ledger.WaiveByPerson("nate"))
            ledger.waive_held(duplicate, style_ledger.WaiveByPerson("nate"))
            ledger.file_held(replacement, new_proposal)
        output = self.report().stdout

        self.assertEqual(output.splitlines()[-1],
                         'Since last report: 0 rules gained, 0 lost "done"; 1 proposal filed.')
        stored = cast(SavedReport, json.loads(snapshot.read_text()))
        self.assertEqual(stored["proposals"], [str(new_proposal)])

    def test_snapshot_with_integer_proposal_count_is_refused(self) -> None:
        _ = self.rule("read")
        self.sync()
        _ = self.report()
        with style_ledger.open_ledger(self.repo) as ledger:
            snapshot = ledger.database.parent / "last_report.json"
        stored = cast(SavedReport, json.loads(snapshot.read_text()))
        invalid = json.dumps({**stored, "proposals": 1}) + "\n"
        _ = snapshot.write_text(invalid)

        result = self.report(success=False)

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("invalid report snapshot", result.stderr)
        self.assertEqual(snapshot.read_text(), invalid)

    def test_all_legacy_catalog_has_an_empty_table_and_no_done_rules(self) -> None:
        _ = self.rule("legacy", "mechanism: llm\n")
        _ = self.rule("old-tool", "mechanism: clippy\n")
        self.sync()
        output = self.report().stdout
        self.assertIn("Rules done: 0 of 0", output)
        self.assertIn("Unclassified: 2 rules — pull nate_style", output)
        self.assertEqual(self.rows(output), {})
        self.assertIn("| Awaiting merge |", output)
        self.assertIn("Waiting to merge back: none", output)

    def test_mixed_catalog_counts_only_classified_coverage_rules(self) -> None:
        _ = self.rule("legacy", "mechanism: llm\n")
        _ = self.rule("read")
        _ = self.rule("process", "tier: E\n")
        self.sync()
        output = self.report().stdout
        self.assertEqual(set(self.rows(output)), {"read"})
        self.assertIn("Rules done: 0 of 1", output)
        self.assertIn("Unclassified: 1 rules — pull nate_style", output)
        lines = output.splitlines()
        header_index = next(index for index, line in enumerate(lines) if line.startswith("Rules done:"))
        self.assertEqual(lines[header_index + 1], "Unclassified: 1 rules — pull nate_style")

    def test_duplicate_stems_use_full_ids_and_ambiguous_filter_lists_them(self) -> None:
        global_rule = self.rule("shared")
        self.write("docs/alpha/style/shared.md", "---\ntier: C\nfamily: names\ninput_scope: file\n---\n# Shared\n")
        local_rule = "local:docs/alpha/style/shared.md"
        self.tip = self.commit("Add member rule")
        self.sync()
        self.assertEqual(set(self.rows(self.report().stdout)), {global_rule, local_rule})
        refused = self.report("--rule", "shared", success=False)
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        for rule in (global_rule, local_rule):
            self.assertIn(rule, refused.stderr)
        selected = self.report("--rule", local_rule).stdout
        self.assertEqual(set(self.rows(selected)), {local_rule})

    def test_rule_filter_includes_only_matching_assigned_or_checkpointed_tickets(self) -> None:
        selected = self.rule("selected")
        other = self.rule("other")
        self.sync()
        units = ("names-unit", "sample-style-names-unit", "other-style-names-unit", "types-unit")
        self.production_units(*units)
        with style_ledger.open_ledger(self.repo) as ledger:
            for number, rule in enumerate((selected, selected, selected, other), start=1):
                unit = units[number - 1]
                ticket = unit + "-1"
                ledger.ticket_add(ticket, unit, "read", "names", "crates/alpha", list(self.sources[:2]), [rule])
                if number != 3:
                    ledger.ticket_assign(ticket, "main")
                if number == 2:
                    definition = next(row for row in rules.load_rules(self.repo) if row.rule_id == rule)
                    for path in self.sources[:2]:
                        ledger.record(rule, path, self.tip, definition.blob, "clean", ticket=ticket, worker="test")
                    ledger.ticket_checkpoint(ticket, self.tip)
        output = self.report("--rule", "selected").stdout
        self.assertEqual(set(self.rows(output)), {"selected"})
        self.assertIn("names-unit-1", output)
        self.assertIn("sample-style-names-unit-1", output)
        self.assertNotIn("other-style-names-unit-1", output)
        self.assertNotIn("types-unit-1", output)

    def test_crate_filter_limits_cells_and_prints_the_oldest_open_ticket(self) -> None:
        read = self.rule("read")
        self.sync()
        self.production_units("names-unit", "sample-style-names-unit", "types-unit")
        for path in self.sources[:2]:
            self.record(read, path)
        with style_ledger.open_ledger(self.repo) as ledger:
            for unit, crate in (("names-unit", "alpha"), ("sample-style-names-unit", "alpha"), ("types-unit", "beta")):
                with mock.patch.object(style_ledger, "_now", return_value=(
                    "2026-10-01T12:00:00+00:00" if unit == "names-unit" else "2026-10-02T12:00:00+00:00"
                )):
                    ledger.ticket_add(unit + "-1", unit, "read", "names", "crates/" + crate,
                                      [f"crates/{crate}/src/lib.rs"], [read])
                ledger.ticket_assign(unit + "-1", "main")
        output = self.report("--crate", "crates/alpha").stdout
        row = self.rows(output)["read"]
        self.assertEqual((row.current, row.open, row.done), ("2 (100%)", "0", "yes"))
        oldest_line = next(line for line in output.splitlines() if "oldest" in line.lower())
        self.assertIn("names-unit-1", oldest_line)
        self.assertNotIn("types-unit-1", output)

    def test_report_counts_coverage_once_and_adds_one_pass_only_for_a_crate(self) -> None:
        read = self.rule("read")
        _ = self.rule("partial", "tier: B\nfamily: names\ninput_scope: file\ncandidates:\n"
                      + "  kind: regex\n  pattern: value\n  surface: partial\n  uncovered: other names\n")
        self.sync()
        for path in self.sources[:2]:
            self.record(read, path)
        with style_ledger.open_ledger(self.repo) as ledger:
            for crate, passes in (("", 1), ("crates/alpha", 2)):
                with self.subTest(crate=crate):
                    with (
                        mock.patch.object(ledger, "_coverage", wraps=cast(object, getattr(ledger, "_coverage"))) as coverage,
                        mock.patch.object(ledger, "status", side_effect=AssertionError("report parsed status text")),
                    ):
                        output = coverage_report.render_report(ledger, crate=crate)
                        self.assertEqual(coverage.call_count, passes)
                    row = self.rows(output)["read"]
                    self.assertEqual(row.current, "2 (100%)" if crate else "2 (50%)")
                    self.assertEqual(row.done, "yes" if crate else "no")
                    self.assertIn("Rules done: 0 of 2", output)

    def test_awaiting_merge_is_neither_current_nor_done_until_sync_after_merge(self) -> None:
        read = self.rule("read")
        self.sync()
        for path in self.sources[1:]:
            self.record(read, path)
        _ = self.git("checkout", "-qb", "style/main")
        self.write(self.sources[0], "pub fn corrected() {}\n")
        self.tip = self.commit("Judge corrected source on style branch")
        self.record(read, self.sources[0], "fixed")
        before = self.report().stdout
        row = self.rows(before)["read"]
        self.assertEqual((row.current, row.awaiting_merge, row.open), ("3 (75%)", "1", "0"))
        self.assertTrue(row.done.startswith("no"))
        self.assertIn("cells current 75%", before)
        self.assertIn("awaiting merge 1", before)
        _ = self.git("checkout", "-q", "main")
        _ = self.git("merge", "--ff-only", "style/main")
        self.sync()
        after = self.report().stdout
        row = self.rows(after)["read"]
        self.assertEqual((row.current, row.awaiting_merge, row.done), ("4 (100%)", "0", "yes"))
        self.assertIn("Rules done: 1 of 1", after)
        self.assertIn("awaiting merge 0", after)

    def test_snapshot_is_beside_the_temp_ledger_and_drives_gained_lost_and_proposal_deltas(self) -> None:
        gained = self.rule("gained")
        lost = self.rule("lost")
        self.sync()
        for path in self.sources:
            self.record(lost, path)
        _ = self.report()
        with style_ledger.open_ledger(self.repo) as ledger:
            snapshot = ledger.database.parent / "last_report.json"
        self.assertTrue(snapshot.is_relative_to(self.root / "state"), snapshot)
        self.assertTrue(snapshot.is_file(), snapshot)
        previous = snapshot.read_text()
        stored = cast(SavedReport, json.loads(previous))
        self.assertEqual(stored["rules"], {
            lost: {"done": True, "cells_total": 4},
            gained: {"done": False, "cells_total": 4},
        })
        self.assertEqual(stored["proposals"], [])
        self.write("crates/alpha/src/new.rs", "pub fn extra() {}\n")
        self.tip = self.commit("Add an unreviewed file")
        self.sync()
        for path in (*self.sources, "crates/alpha/src/new.rs"):
            self.record(gained, path)
        finding = self.held(gained)
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.file_held(finding, self.root / "proposal.md")
        changed = self.report().stdout
        self.assertEqual(changed.splitlines()[-1],
                         'Since last report: 1 rule gained, 1 lost "done" to new files; 1 proposal filed.')
        self.assertNotEqual(snapshot.read_text(), previous)
        repeated = self.report().stdout
        self.assertEqual(repeated.splitlines()[-1],
                         'Since last report: 0 rules gained, 0 lost "done"; 0 proposals filed.')


if __name__ == "__main__":
    _ = unittest.main()
