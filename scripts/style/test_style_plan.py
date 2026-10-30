"""Family ranks, pilot scopes and concurrent refill ownership."""

from __future__ import annotations

import fcntl
import io
import subprocess
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import IO, final
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import style_plan
import unit_plans
import style_proposals

import berth_board
import rules
import style_ledger

from style_plan_test_support import StylePlanFixture


# load 136 on natedev, 2026-10-29
TEST_HANG_GUARD_SECS = 300


@final
class StylePlanTests(StylePlanFixture):
    def test_style_family_accepts_exact_suffixes_and_returns_a_typed_nonstyle_result(self) -> None:
        families: tuple[rules.Family, ...] = ("names", "types", "values", "modules")
        for family in families:
            for unit in (f"{family}-unit", f"sample-style-{family}-unit", f"types-style-{family}-unit"):
                with self.subTest(unit=unit):
                    self.assertEqual(rules.style_family(unit), family)
        for unit in ("", "names", "feature-unit", "nonnames-unit", "names-units", "names-unit-extra"):
            with self.subTest(unit=unit):
                self.assertIsInstance(rules.style_family(unit), rules.NotStyleUnit)

    def test_family_plans_use_each_units_plan_cell_for_old_and_full_ids(self) -> None:
        expected: dict[rules.Family, str] = {}
        rows: list[str] = []
        families: tuple[rules.Family, ...] = ("names", "types", "values", "modules")
        for index, family in enumerate(families):
            unit = f"sample-style-{family}-unit" if index % 2 else f"{family}-unit"
            worktree = self.root / f"plan-{family}"
            relative = f"docs/style-production/units/retained-{family}.md"
            plan = worktree / relative
            plan.parent.mkdir(parents=True)
            expected[family] = f"# {family} unit settings\n"
            _ = plan.write_text(expected[family])
            _ = (plan.parent / f"{unit}.md").write_text("Do not read this path.\n")
            rows.append(f"| {unit} | {relative} | {worktree} | demo-style-{family} |")
        rows.append(f"| feature-unit | missing.md | {self.root / 'feature'} | feature |")
        prefix = self.doc.read_text().split("## Units", 1)[0]
        _ = self.doc.write_text(prefix + "## Units\n\n| Unit | Plan | Worktree | Branch |\n"
                                + "| --- | --- | --- | --- |\n" + "\n".join(rows) + "\n")

        self.assertEqual(dict(unit_plans.family_plan_texts(self.doc)), expected)

    def test_next_phase_keeps_the_declared_plan_path_for_old_and_full_ids(self) -> None:
        _ = self.rule("read")
        self.sync()
        original_doc = self.doc.read_text()
        retained = self.plan.with_name("retained-names-plan.md")
        _ = self.plan.rename(retained)
        for unit in ("names-unit", "sample-style-names-unit"):
            with self.subTest(unit=unit):
                _ = self.doc.write_text(original_doc.replace("| names-unit |", f"| {unit} |")
                                        .replace("units/names-unit.md", "units/retained-names-plan.md"))
                _ = retained.write_text(self.plan_header().replace("`names-unit`", f"`{unit}`"))
                result = style_plan.next_phase(unit, retained, self.repo)
                self.assertEqual(result, style_plan.PhaseWritten(f"{unit}-1"))
                ticket = self.ledger().ticket_open(unit)[0]
                self.assertEqual(ticket["family"], "names")
                self.assertEqual(ticket["files"], list(self.sources[:2]))
                self.assertIn(f"{unit}-1", retained.read_text())
                self.assertFalse(self.plan.exists())
                self.assertEqual(style_plan.next_phase(unit, retained, self.repo),
                                 style_plan.Resumed(f"{unit}-1"))
                self.ledger().ticket_drop(f"{unit}-1")

    def test_concurrent_refills_read_committed_tickets_and_choose_disjoint_files(self) -> None:
        # The families here share files, so no rank holds one family back for another.
        _ = self.enterContext(mock.patch.object(rules, "FAMILY_RANKS", ()))
        _ = self.rule("read")
        _ = self.rule("types-read", "tier: C\nfamily: types\ninput_scope: file\n")
        types = self.root / "types"
        _ = self.git("worktree", "add", "-q", "-b", "demo-style-types", str(types))
        types_plan = types / "docs/style-production/units/types-unit.md"
        types_plan.parent.mkdir(parents=True)
        _ = types_plan.write_text(self.plan_header().replace("names-unit", "types-unit"))
        _ = self.doc.write_text(self.doc.read_text() +
                                f"| types-unit | docs/style-production/units/types-unit.md | {types} | demo-style-types |\n")
        self.sync()
        names_candidates = self.candidates("names")
        types_candidates = self.candidates("types")
        self.assertTrue(set(names_candidates[0].files) & set(types_candidates[0].files))
        read_busy = style_plan.busy_files
        flock = fcntl.flock

        for selection_pass in (1, 2):
            with self.subTest(selection_pass=selection_pass):
                _ = self.plan.write_text(self.plan_header())
                _ = types_plan.write_text(self.plan_header().replace("names-unit", "types-unit"))
                first_read, second_read = threading.Event(), threading.Event()
                reads = {"names-unit": 0, "types-unit": 0}
                second_saw_committed_ticket: list[bool] = []

                def observe_lock(file: IO[str], operation: int) -> None:
                    try:
                        flock(file, operation | fcntl.LOCK_NB)
                    except BlockingIOError:
                        if first_read.is_set():
                            second_read.set()
                        flock(file, operation)

                def read_during_refill(
                    repo: Path, doc: Path, ledger: style_ledger.Ledger, unit: str, board: berth_board.Board,
                ) -> style_plan.BusyFiles:
                    busy = read_busy(repo, doc, ledger, unit, board)
                    reads[unit] += 1
                    if reads[unit] < selection_pass:
                        return style_plan.BusyFiles(board, frozenset(self.sources))
                    if unit == "names-unit":
                        first_read.set()
                        self.assertTrue(second_read.wait(TEST_HANG_GUARD_SECS), "second refill never contended or read")
                    else:
                        second_saw_committed_ticket.append(bool(ledger.ticket_open("names-unit")))
                        second_read.set()
                    return busy

                with (mock.patch.object(fcntl, "flock", side_effect=observe_lock),
                      mock.patch.object(style_plan, "busy_files", side_effect=read_during_refill),
                      ThreadPoolExecutor(max_workers=2) as pool):
                    first = pool.submit(style_plan.next_phase, "names-unit", self.plan, self.repo)
                    self.assertTrue(first_read.wait(TEST_HANG_GUARD_SECS), "first refill did not read busy files")
                    second = pool.submit(style_plan.next_phase, "types-unit", types_plan, self.repo)
                    first_result = first.result(timeout=TEST_HANG_GUARD_SECS)
                    second_result = second.result(timeout=TEST_HANG_GUARD_SECS)
                self.assertIsInstance(first_result, style_plan.PhaseWritten)
                self.assertIsInstance(second_result, style_plan.PhaseWritten)
                ledger = self.ledger()
                first_ticket = ledger.ticket_open("names-unit")[0]
                second_ticket = ledger.ticket_open("types-unit")[0]
                ledger.ticket_drop(first_ticket["id"])
                ledger.ticket_drop(second_ticket["id"])
                self.assertEqual(second_saw_committed_ticket, [True], "second read preceded the first ticket commit")
                self.assertTrue(first_ticket["files"])
                self.assertTrue(second_ticket["files"])
                self.assertTrue(set(first_ticket["files"]).isdisjoint(second_ticket["files"]))

    def test_a_lower_rank_cell_waits_while_a_higher_rank_cell_on_its_file_is_open(self) -> None:
        _ = self.types_unit()
        _ = self.rule("read")
        types = self.rule("types-read", "tier: C\nfamily: types\ninput_scope: file\n")
        self.sync()
        first = self.sources[0]
        for path in self.sources[1:]:
            self.record(types, path, self.tip)
        # A file with no open types work is planned as before.
        self.assertEqual(self.names_files(), set(self.sources[1:]))
        code, output, errors = self.run_next()
        self.assertEqual(code, 0, errors)
        self.assertEqual(output.splitlines().count(f"waiting names: {first} has open types work"), 1)
        self.assertNotIn(first, self.ledger().ticket_open("names-unit")[0]["files"])
        # The rank is read from the one table: with names ranked above types, names no longer waits.
        with mock.patch.object(rules, "FAMILY_RANKS", ("names", "types", "values")):
            self.assertEqual(self.names_files(), set(self.sources))

    def test_a_lower_rank_cell_waits_while_a_higher_rank_decision_on_its_file_is_unbuilt(self) -> None:
        _ = self.rule("read")
        types = self.rule("types-read", "tier: C\nfamily: types\ninput_scope: file\n")
        self.sync()
        for path in self.sources:
            self.record(types, path, self.tip)
        self.assertEqual(self.names_files(), set(self.sources))
        ledger = self.ledger()
        blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == types)
        finding = ledger.hold(types, self.sources[0], 1, "change", "type", "before", "after", blob, "outside")
        ledger.file_held(finding.finding_id, self.root / "types-decision.md")
        self.assertEqual(self.names_files(), set(self.sources[1:]))

    def test_a_queued_lower_rank_ticket_is_dropped_while_a_higher_rank_cell_on_its_file_is_open(self) -> None:
        _ = self.types_unit()
        rule = self.rule("read")
        types = self.rule("types-read", "tier: C\nfamily: types\ninput_scope: file\n")
        self.sync()
        first = self.sources[0]
        for path in self.sources[1:]:
            self.record(types, path, self.tip)
        self.ledger().ticket_add("names-unit-1", "names-unit", "read", "names", "crates/alpha", [first], [rule])
        code, output, errors = self.run_next()
        self.assertEqual(code, 0, errors)
        self.assertIn(f"dropped names-unit-1: {first} has open types work", output)
        self.assertNotIn(first, self.ledger().ticket_open("names-unit")[0]["files"])

    def test_a_filed_higher_rank_finding_holds_its_file_when_its_rule_has_no_cell(self) -> None:
        types = self.rule("types-read", "tier: C\nfamily: types\ninput_scope: file\n")
        self.sync()
        ledger = self.ledger()
        blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == types)
        finding = ledger.hold(types, self.sources[0], 1, "change", "type", "before", "after", blob, "outside")
        ledger.file_held(finding.finding_id, self.root / "types-decision.md")
        scopes: dict[rules.Family, unit_plans.PilotScope] = {"types": unit_plans.AllCrates(), "names": unit_plans.AllCrates()}
        self.assertEqual(set(style_proposals.ranked_work(ledger, (), scopes).waits("names")), {self.sources[0]})

    def test_full_id_queue_reads_retained_plan_settings_and_only_declared_units(self) -> None:
        _ = self.rule("read")
        _ = self.rule("types-read", "tier: C\nfamily: types\ninput_scope: file\n")
        self.sync()
        unit = "sample-style-names-unit"
        retained = self.plan.with_name("retained-names-plan.md")
        _ = self.plan.rename(retained)
        first = self.sources[-1]
        _ = retained.write_text(self.plan_header().replace("`names-unit`", f"`{unit}`").replace(
            "## Phases", f"- **First files:** {first}\n\n## Phases"))
        _ = self.doc.write_text(self.doc.read_text().replace("| names-unit |", f"| {unit} |")
                                .replace("units/names-unit.md", "units/retained-names-plan.md"))
        for selected in ([], ["--unit", unit]):
            with self.subTest(selected=selected):
                output = io.StringIO()
                with redirect_stdout(output):
                    code = style_plan.main(["queue", "--repo", str(self.repo), *selected])
                self.assertEqual(code, 0)
                lines = output.getvalue().splitlines()
                self.assertTrue(lines)
                self.assertTrue(all(line.startswith(f"{unit}: read ") for line in lines), lines)
                self.assertEqual(lines[0].split(" files: ")[1], first)
        self.assertEqual(self.count_tickets(), 0)

    def test_a_waived_only_cell_stops_refilling_until_its_file_or_rule_changes(self) -> None:
        rule = self.rule("read", "tier: C\nfamily: names\ninput_scope: crate\n")
        self.sync()
        ledger = self.ledger()
        blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule)
        path = self.sources[0]
        finding = ledger.hold(rule, path, 1, "Keep this name", "rename", "value", "amount", blob, "outside")
        ledger.record(rule, path, self.tip, blob, "held", ticket="outside", input_scope=style_ledger.DeclaredInputScope.RULE)
        result = subprocess.run([sys.executable, style_ledger.__file__, "held", "waive", "--repo", str(self.repo),
                                 "--id", finding.finding_id, "--person", "nate"], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.write(self.sources[1], "pub fn sibling_changed() {}\n")
        self.tip = self.commit()
        self.sync()
        self.unit_takes_tip()
        for other in self.sources[1:]:
            self.record(rule, other, self.tip)
        self.assertEqual([ticket.files for ticket in self.candidates()], [(path,)])
        refused = subprocess.run([sys.executable, style_ledger.__file__, "record", "--repo", str(self.repo),
                                  "--rule", rule, "--path", path, "--commit", self.tip, "--rule-blob", blob,
                                  "--verdict", "held", "--ticket", "outside", "--worker", "test", "--input-scope", "rule"],
                                 capture_output=True, text=True, check=False)
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        ledger.record(rule, path, self.tip, blob, "waived", ticket="outside")
        self.write("README.md", "Unrelated sync\n")
        self.tip = self.commit()
        self.sync()
        self.unit_takes_tip()
        self.assertEqual(self.candidates(), ())
        self.write(path, "pub fn changed() {}\n")
        self.tip = self.commit()
        self.sync()
        self.unit_takes_tip()
        self.assertIn(path, {file for ticket in self.candidates() for file in ticket.files})
        ledger.record(rule, path, self.tip, blob, "waived", ticket="outside")
        _ = (self.rule_dir / "read.md").write_text("---\ntier: C\nfamily: names\ninput_scope: crate\n---\nNew rule text.\n")
        self.sync()
        self.assertIn(path, {file for ticket in self.candidates() for file in ticket.files})

    def test_structure_work_order_shows_existing_holds_on_the_batch_files_only(self) -> None:
        rule = self.rule("structure", "tier: D\nfamily: modules\n")
        self.sync()
        ledger = self.ledger()
        blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule)
        held = ledger.hold(rule, self.sources[0], 1, "Split this module", "restructure", "before", "after", blob, "outside")
        waived = ledger.hold(rule, self.sources[0], 2, "Already waived", "restructure", "before", "after", blob, "outside")
        _ = ledger.decline_findings((waived.finding_id,), "declined", style_ledger.WaiveByPerson("user"))[waived.finding_id]
        outside = ledger.hold(rule, self.sources[2], 1, "Different batch", "restructure", "before", "after", blob, "outside")
        ledger.waive_held(outside.finding_id, style_ledger.WaiveByPerson("nate"))
        other_rule = self.rule("other-family")
        other_blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == other_rule)
        other = ledger.hold(other_rule, self.sources[0], 3, "Other rule", "rename", "before", "after", other_blob, "outside")
        ledger.waive_held(other.finding_id, style_ledger.WaiveByPerson("nate"))
        worktree = self.root / "modules"
        _ = self.git("worktree", "add", "-q", "-b", "demo-style-modules", str(worktree))
        plan = worktree / "docs/style-production/units/modules-unit.md"
        plan.parent.mkdir(parents=True)
        _ = plan.write_text(self.plan_header())
        self.assertIsInstance(style_plan.next_phase("modules-unit", plan, self.repo), style_plan.PhaseWritten)
        text = plan.read_text()
        self.assertIn(f"{held.finding_id} · {rule} · {self.sources[0]}:1 · Split this module", text)
        self.assertIn("do not hold these again", text)
        heading = "Waived by the user: do not hold these again. Record waived for a cell whose only findings are these."
        self.assertIn(heading, text)
        held_section, _, waived_section = text.partition(heading)
        self.assertNotIn(waived.finding_id, held_section)
        self.assertIn(waived.finding_id, waived_section)
        self.assertNotIn(other.finding_id, text)
        self.assertIn(f"{waived.finding_id} · {rule} · {self.sources[0]}:2 · Already waived", text)
        self.assertNotIn(outside.finding_id, text)
        self.assertIn("Judge these 2 files of `crates/alpha`", text)

    def test_structure_resume_keeps_waived_findings_after_the_pinned_rule_leaves_the_catalog(self) -> None:
        rule = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: manifest\n")
        self.sync()
        ledger = self.ledger()
        blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule)
        finding = ledger.hold(rule, self.sources[0], 1, "Keep this module", "restructure",
                              "before", "after", blob, "outside")
        ledger.waive_held(finding.finding_id, style_ledger.WaiveByPerson("nate"))
        plan = self.modules_unit()
        self.assertIsInstance(style_plan.next_phase("modules-unit", plan, self.repo), style_plan.PhaseWritten)
        expected = plan.read_bytes()
        self.assertIn(f"{finding.finding_id} · {rule} · {self.sources[0]}:1 · Keep this module", expected.decode())
        (self.rule_dir / "structure.md").unlink()
        _ = plan.write_text(self.plan_header())
        self.assertIsInstance(style_plan.next_phase("modules-unit", plan, self.repo), style_plan.Resumed)
        self.assertEqual(plan.read_bytes(), expected)

    def test_first_files_get_a_separate_read_ticket_before_paths_that_sort_earlier(self) -> None:
        _ = self.rule("read")
        self.sync()
        first = self.sources[1]
        _ = self.plan.write_text(self.plan_header().replace(
            "## Phases", f"- **First files:**  {first}  \n\n## Phases"))

        result = self.run_next()

        self.assertEqual(result[0], 0, result)
        self.assertEqual(self.ledger().ticket_open("names-unit")[0]["files"], [first])

    def test_first_files_rank_a_crate_ahead_of_more_stale_cells(self) -> None:
        rule = self.rule("read")
        self.sync()
        for path in self.sources[:2]:
            self.record(rule, path, self.tip)
            self.write(path, "pub fn revised() {}\n")
        self.tip = self.commit()
        self.sync()
        self.unit_takes_tip()
        self.assertEqual(self.candidates()[0].crate, "crates/alpha")
        first = self.sources[-1]
        _ = self.plan.write_text(self.plan_header().replace(
            "## Phases", f"- **First files:** {first}\n\n## Phases"))

        result = self.run_next()

        self.assertEqual(result[0], 0, result)
        self.assertEqual(self.ledger().ticket_open("names-unit")[0]["files"], [first])

    def test_first_files_without_a_line_keep_the_existing_ticket_order(self) -> None:
        _ = self.rule("read")
        self.sync()
        ledger = self.ledger()
        busy = style_plan.BusyFiles(self.board(), frozenset())
        expected = style_plan.candidate_tickets(self.repo, ledger, busy, "names", base=self.base())

        result = self.run_next()
        self.assertEqual(result[0], 0, result)
        self.assertEqual(ledger.ticket_open("names-unit")[0]["files"], list(expected[0].files))

    def test_first_files_with_no_pending_cell_leave_the_queue_unchanged(self) -> None:
        rule = self.rule("read")
        self.sync()
        first = self.sources[-1]
        self.record(rule, first, self.tip)
        expected = self.candidates()
        _ = self.plan.write_text(self.plan_header().replace(
            "## Phases", f"- **First files:** {first}, missing.rs\n\n## Phases"))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(style_plan.main(["queue", "--repo", str(self.repo), "--unit", "names-unit"]), 0)
        self.assertEqual([line.split(" files: ")[1] for line in output.getvalue().splitlines()],
                         [", ".join(ticket.files) for ticket in expected])

    def test_first_files_invalid_lines_refuse_before_assigning_or_editing_a_plan(self) -> None:
        _ = self.rule("read")
        self.sync()
        for line, message in (
            ("- **First files:** a.rs\n- **First files:** b.rs", "first files has more than one line"),
            ("- **First files:** /tmp/a.rs", "first files has absolute path /tmp/a.rs"),
            ("- **First files:** a.rs, a.rs", "first files has duplicate path a.rs"),
            ("- **First files:** a.rs, ", "first files has empty entry"),
        ):
            with self.subTest(line=line):
                _ = self.plan.write_text(self.plan_header().replace("## Phases", line + "\n\n## Phases"))
                before = self.plan.read_bytes()
                code, output, errors = self.run_next()
                self.assertEqual(code, 2, output)
                self.assertEqual(errors, f"style_plan: {message}\n")
                self.assertEqual(self.count_tickets(), 0)
                self.assertEqual(self.plan.read_bytes(), before)

    def test_first_files_queue_uses_the_unit_copy_and_matches_next_phase(self) -> None:
        _ = self.rule("read")
        self.sync()
        first = self.sources[-1]
        _ = self.plan.write_text(self.plan_header().replace(
            "## Phases", f"- **First files:** {first}\n\n## Phases"))
        template = self.style / "docs/style-production/units/names-unit.md"
        _ = template.write_text(self.plan_header().replace(
            "## Phases", f"- **First files:** {self.sources[0]}\n\n## Phases"))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(style_plan.main(["queue", "--repo", str(self.repo), "--unit", "names-unit"]), 0)
        self.assertEqual(output.getvalue().splitlines()[0].split(" files: ")[1], first)
        self.assertEqual(set(path for line in output.getvalue().splitlines()
                             for path in line.split(" files: ")[1].split(", ")), set(self.sources))
        result = self.run_next()
        self.assertEqual(result[0], 0, result)
        self.assertEqual(self.ledger().ticket_open("names-unit")[0]["files"], [first])

    def test_first_files_in_another_unit_are_checked_only_when_that_unit_is_selected(self) -> None:
        _ = self.rule("read")
        self.sync()
        types = self.root / "types"
        types_plan = types / "docs/style-production/units/types-unit.md"
        types_plan.parent.mkdir(parents=True)
        _ = types_plan.write_text(self.plan_header().replace(
            "## Phases", "- **First files:** a.rs, a.rs\n\n## Phases"))
        _ = self.doc.write_text(self.doc.read_text() +
                                f"| types-unit | docs/style-production/units/types-unit.md | {types} | types |\n")

        with self.subTest(selection="names queue"):
            output = io.StringIO()
            with redirect_stdout(output):
                code = style_plan.main(["queue", "--repo", str(self.repo), "--unit", "names-unit"])
            self.assertEqual(code, 0)
            self.assertIn("names-unit: read crates/alpha", output.getvalue())

        with self.subTest(selection="all queues"):
            errors = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(errors):
                code = style_plan.main(["queue", "--repo", str(self.repo)])
            self.assertEqual(code, 2)
            self.assertEqual(errors.getvalue(), "style_plan: first files has duplicate path a.rs\n")

        with self.subTest(selection="names next phase"):
            result = self.run_next()
            self.assertEqual(result[0], 0, result)
            self.assertEqual(self.ledger().ticket_open("names-unit")[0]["files"], list(self.sources[:2]))

    def test_unit_plan_scope_resolves_package_names_and_exhausts_only_that_crate(self) -> None:
        rule = self.rule("read")
        self.write("crates/beta/Cargo.toml", '[package]\nname = "cargo-handler"\nversion = "0.1.0"\n')
        self.tip = self.commit()
        self.sync()
        _ = self.plan.write_text(self.plan_header().replace("## Phases", "**Pilot scope:** cargo-handler\n\n## Phases"))
        result = self.run_next()
        self.assertEqual(result[0], 0, result)
        ledger = self.ledger()
        ticket = ledger.ticket_open("names-unit")[0]
        self.assertEqual(ticket["crate"], "crates/beta")
        self.assertEqual(set(ticket["files"]), set(self.sources[2:]))
        for path in ticket["files"]:
            self.record(rule, path, self.tip)
        ledger.ticket_drop(ticket["id"])
        result = self.run_next()
        self.assertEqual(result[0], 3, result)
        self.assertEqual(ledger.ticket_open("names-unit"), ())
        self.assertTrue(any(cell.crate == "crates/alpha" and cell.state == "open" for cell in ledger.cells()))

    def test_scope_is_read_from_the_unit_copy_even_when_showrunner_copy_differs(self) -> None:
        _ = self.rule("read")
        self.sync()
        showrunner_plan = self.style / "docs/style-production/units/names-unit.md"
        for showrunner_scope in ("", "**Pilot scope:** alpha\n\n"):
            with self.subTest(showrunner_scope=showrunner_scope):
                _ = showrunner_plan.write_text(self.plan_header().replace("## Phases", showrunner_scope + "## Phases"))
                _ = self.plan.write_text(self.plan_header().replace("## Phases", "**Pilot scope:** beta\n\n## Phases"))
                result = self.run_next()
                self.assertEqual(result[0], 0, result)
                ledger = self.ledger()
                ticket = ledger.ticket_open("names-unit")[0]
                self.assertEqual(ticket["crate"], "crates/beta")
                ledger.ticket_drop(ticket["id"])

    def test_no_scope_selects_work_in_every_crate(self) -> None:
        rule = self.rule("read")
        self.sync()
        selected: set[str] = set()
        for expected in ("crates/alpha", "crates/beta"):
            result = self.run_next()
            self.assertEqual(result[0], 0, result)
            ledger = self.ledger()
            ticket = ledger.ticket_open("names-unit")[0]
            self.assertEqual(ticket["crate"], expected)
            selected.add(ticket["crate"])
            for path in ticket["files"]:
                self.record(rule, path, self.tip)
            ledger.ticket_drop(ticket["id"])
        self.assertEqual(selected, {"crates/alpha", "crates/beta"})

    def test_comma_separated_scope_names_allow_each_named_member(self) -> None:
        _ = self.rule("read")
        self.sync()
        scope = unit_plans.parse_pilot_scope(
            self.plan_header().replace("## Phases", "**Pilot scope:** beta, alpha\n\n## Phases"), self.repo)
        ledger = self.ledger()
        tickets = style_plan.candidate_tickets(self.repo, ledger, style_plan.BusyFiles(self.board(), frozenset()), "names", scope,
                                               base=self.base())
        self.assertEqual({ticket.crate for ticket in tickets}, {"crates/alpha", "crates/beta"})

    def test_unknown_or_repeated_scope_is_refused_before_ticket_assignment(self) -> None:
        _ = self.rule("read")
        self.sync()
        for line, message in (
            ("**Pilot scope:** missing", "pilot scope names unknown crate missing"),
            ("**Pilot scope:** alpha\n**Pilot scope:** beta", "pilot scope has more than one scope line"),
        ):
            with self.subTest(line=line):
                _ = self.plan.write_text(self.plan_header().replace("## Phases", line + "\n\n## Phases"))
                before = self.plan.read_bytes()
                code, output, errors = self.run_next()
                self.assertEqual(code, 2, output)
                self.assertEqual(errors, f"style_plan: {message}\n")
                self.assertEqual(self.count_tickets(), 0)
                self.assertEqual(self.plan.read_bytes(), before)

    def test_queued_ticket_outside_pilot_scope_is_replaced_by_scoped_coverage(self) -> None:
        rule = self.rule("read")
        self.sync()
        ledger = self.ledger()
        ledger.ticket_add("names-unit-1", "names-unit", "read", "names", "crates/alpha", [self.sources[0]], [rule])
        _ = self.plan.write_text(self.plan_header().replace("## Phases", "**Pilot scope:** beta\n\n## Phases"))
        result = self.run_next()
        self.assertEqual(result[0], 0, result)
        self.assertIn("dropped names-unit-1: outside pilot scope", result[1])
        ticket = ledger.ticket_open("names-unit")[0]
        self.assertEqual(ticket["crate"], "crates/beta")
        self.assertNotEqual(ticket["id"], "names-unit-1")

    def test_outside_scope_approved_proposal_stays_approved_while_coverage_proceeds(self) -> None:
        rule = self.rule("read")
        self.sync()
        _ = self.plan.write_text(self.plan_header().replace("## Phases", "**Pilot scope:** beta\n\n## Phases"))
        folder = self.root / "issues"
        folder.mkdir()
        issue = folder / "approved.md"
        ledger = self.ledger()
        blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule)
        finding = ledger.hold(rule, self.sources[0], 1, "value", "rename", "old", "new", blob, "outside")
        ledger.file_held(finding.finding_id, issue)
        _ = issue.write_text('---\nstatus: open\nstage: backlog\ntags: [issue, proposal, style]\n'
                             + f'unit: names-unit\nplan: "{self.plan} Phase 1"\nstyle_rule: {rule}\nstyle_crate: crates/alpha\nstyle_rule_blob: {blob}\n---\n')
        _ = self.doc.write_text(self.doc.read_text().replace("## Units", f"- **Proposals:** {folder}\n\n## Units"))
        before = issue.read_bytes()
        with mock.patch.object(style_proposals, "proposal_command") as start:
            result = self.run_next()
        self.assertEqual(result[0], 0, result)
        self.assertIn(f"waiting {issue}: outside pilot scope", result[1])
        start.assert_not_called()
        ticket = ledger.ticket_open("names-unit")[0]
        self.assertEqual((ticket["kind"], ticket["crate"]), ("read", "crates/beta"))
        self.assertEqual(self.count_tickets(), 1)
        self.assertEqual(issue.read_bytes(), before)
        approved = style_proposals.approved_decisions(folder, "names-unit")
        self.assertEqual(tuple(item.path for item in approved), (issue,))
        self.assertEqual(ledger.held_findings(style_ledger.HeldFindingsInState("filed"))[0].finding_id, finding.finding_id)


if __name__ == "__main__":
    _ = unittest.main()
