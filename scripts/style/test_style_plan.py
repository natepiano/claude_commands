"""Selection respects product work and recovery preserves pinned instructions."""

from __future__ import annotations

import io
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import cast, final, override
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import approved_renames
import style_footprint
import style_plan
import style_proposals
import proposal_issue
from issues import frontmatter

import berth_board
import rules
import style_ledger
import style_rules


@final
class StylePlanTests(unittest.TestCase):
    root: Path = Path()
    repo: Path = Path()
    style: Path = Path()
    unit: Path = Path()
    plan: Path = Path()
    doc: Path = Path()
    rule_dir: Path = Path()
    tip: str = ""
    sources: tuple[str, ...] = tuple(f"crates/{crate}/src/{name}.rs" for crate in ("alpha", "beta") for name in ("lib", "other"))

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = self.enterContext(mock.patch.object(Path, "home", return_value=self.root))
        self.repo = self.root / "sample"
        self.style = self.root / "sample-style"
        self.unit = self.root / "sample-style-names"
        self.rule_dir = self.root / "nate_style/rust"
        self.repo.mkdir()
        self.rule_dir.mkdir(parents=True)
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.root),
            "STYLE_LEDGER_STATE_DIR": str(self.root / "state"),
            "STYLE_HISTORY_NATE_STYLE_DIR": str(self.rule_dir.parent),
        })))
        _ = self.enterContext(mock.patch.object(style_rules, "NATE_STYLE_DIR", self.rule_dir.parent))
        _ = self.git("init", "-q", "--initial-branch=main")
        _ = self.git("config", "user.name", "Planner tests")
        _ = self.git("config", "user.email", "planner@example.invalid")
        self.write("Cargo.toml", '[workspace]\nmembers = ["crates/alpha", "crates/beta"]\n')
        for crate in ("alpha", "beta"):
            self.write(f"crates/{crate}/Cargo.toml", f'[package]\nname = "{crate}"\nversion = "0.1.0"\n')
        for path in self.sources:
            self.write(path, "pub fn value() {}\n")
        self.tip = self.commit()
        _ = self.git("worktree", "add", "-q", "-b", "style/main", str(self.style))
        _ = self.git("worktree", "add", "-q", "-b", "demo-style-names", str(self.unit))
        _ = (self.repo / ".git/info/exclude").write_text("/docs/style-production/\n/docs/*production.md\n")
        self.doc = self.style / "docs/sample-style-production.md"
        parent = self.repo / "docs/product-production.md"
        parent.parent.mkdir(parents=True)
        _ = parent.write_text(self.production("main", self.repo, ""))
        self.doc.parent.mkdir(parents=True)
        _ = self.doc.write_text(self.production("style/main", self.style, str(parent)))
        self.plan = self.unit / "docs/style-production/units/names-unit.md"
        self.plan.parent.mkdir(parents=True)
        _ = self.plan.write_text(self.plan_header())
        showrunner_plan = self.style / "docs/style-production/units/names-unit.md"
        showrunner_plan.parent.mkdir(parents=True)
        _ = showrunner_plan.write_text(self.plan_header() + "Showrunner copy differs.\n")
        _ = self.enterContext(mock.patch.object(berth_board, "read_board", return_value=self.board()))
        # Renumber edits the real vault, so no test runs it.
        _ = self.enterContext(mock.patch.object(style_proposals, "renumber"))

    def production(self, branch: str, checkout: Path, parent: str) -> str:
        return "\n".join([
            "# Production", f"- **Merge branch:** {branch}", f"- **Showrunner checkout:** {checkout}",
            "- **Log:** docs/log.md", "- **Branch point:** main", f"- **Parent production:** {parent}", "", "## Units", "",
            "| Unit | Plan | Worktree | Branch |", "| --- | --- | --- | --- |",
            *([f"| names-unit | docs/style-production/units/names-unit.md | {self.unit} | demo-style-names |",
               f"| modules-unit | docs/style-production/units/modules-unit.md | {self.root / 'modules'} | demo-style-modules |"] if parent else []), "",
        ])

    def plan_header(self) -> str:
        return "\n".join([
            "# Standing names", f"> **Production: sample-style** — unit `names-unit`; production doc `{self.doc}`",
            "> **Standing:** style_plan.py", "", "## Delegation Context", "",
            "- **Project:** sample", "- **Test:** verify.sh test <package>", "", "## Phases", "",
        ])

    def board(self, *blocked: str) -> berth_board.Board:
        return berth_board.Board(tuple(
            berth_board.Reservation(str(index), "refs/heads/main", self.repo, "product", (path,), "active", "blocking")
            for index, path in enumerate(blocked)
        ))

    def git(self, *args: str, cwd: Path | None = None) -> str:
        return subprocess.run(["git", "-C", str(cwd or self.repo), *args], capture_output=True, text=True, check=True).stdout.strip()

    def write(self, path: str, text: str) -> None:
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        _ = target.write_text(text)

    def commit(self) -> str:
        _ = self.git("add", "--all")
        _ = self.git("commit", "-qm", "Fixture")
        return self.git("rev-parse", "HEAD")

    def rule(self, name: str, metadata: str = "tier: C\nfamily: names\ninput_scope: file\n") -> str:
        _ = (self.rule_dir / (name + ".md")).write_text(f"---\n{metadata}---\n# {name}\nOriginal instructions.\n")
        return "global:rust/" + name + ".md"

    def ledger(self) -> style_ledger.Ledger:
        ledger = style_ledger.open_ledger(self.repo)
        self.addCleanup(ledger.close)
        return ledger

    def sync(self) -> None:
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.set_tip(self.tip, "style/main")

    def candidates(self, family: rules.Family = "names", blocked: tuple[str, ...] = ()) -> tuple[style_plan.TicketSelection, ...]:
        ledger = self.ledger()
        board = self.board(*blocked)
        busy = style_plan.busy_files(self.repo, self.doc, ledger, family + "-unit", board)
        return style_plan.candidate_tickets(self.repo, ledger, busy, family, base=self.base(family))

    def base(self, family: rules.Family = "names") -> style_plan.UnitBase:
        return style_plan.unit_base(self.repo, f"demo-style-{family}")

    def modules_unit(self) -> Path:
        """Add the modules unit's worktree and branch at the main tip, and return its plan."""
        worktree = self.root / "modules"
        _ = self.git("worktree", "add", "-q", "-b", "demo-style-modules", str(worktree))
        plan = worktree / "docs/style-production/units/modules-unit.md"
        plan.parent.mkdir(parents=True)
        _ = plan.write_text(self.plan_header())
        return plan

    def unit_takes_tip(self) -> None:
        """The style branch takes the synced tip, and the names unit merges the style branch."""
        _ = self.git("merge", "-q", "--ff-only", self.tip, cwd=self.style)
        _ = self.git("merge", "-q", "--ff-only", "style/main", cwd=self.unit)

    def run_next(self) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = style_plan.main(["next-phase", "--unit", "names-unit", "--plan", str(self.plan), "--repo", str(self.repo)])
        return code, output.getvalue(), errors.getvalue()

    def record(self, rule: str, path: str, commit: str, ticket: str = "outside") -> None:
        with style_ledger.open_ledger(self.repo) as ledger:
            blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule.removesuffix("#uncovered"))
            ledger.record(rule=rule, path=path, commit=commit, verdict="clean", detail="", ticket=ticket, worker="test", rule_blob=blob,
                          input_scope=style_ledger.DeclaredInputScope.RULE)

    def held_proposal(self, rule: str, path: str) -> Path:
        issue = self.root / "proposal.md"
        with style_ledger.open_ledger(self.repo) as ledger:
            blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule)
            finding = ledger.hold(rule, path, 1, "held defect", "restructure", "before", "after", blob, "outside")
            ledger.file_held(finding.finding_id, issue)
            ledger.record(rule=rule, path=path, commit=self.tip, verdict="held", detail="", ticket="outside",
                          worker="test", rule_blob=blob, input_scope=style_ledger.DeclaredInputScope.RULE)
        _ = issue.write_text(f"---\nstatus: open\nstyle_findings: [{finding.finding_id}]\n---\n")
        return issue

    def count_tickets(self) -> int:
        with closing(sqlite3.connect(self.ledger().database)) as connection:
            rows = cast(list[tuple[int]], connection.execute("SELECT COUNT(*) FROM tickets").fetchall())
        return rows[0][0]

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

        self.assertEqual(dict(style_plan.family_plan_texts(self.doc)), expected)

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

        for selection_pass in (1, 2):
            with self.subTest(selection_pass=selection_pass):
                _ = self.plan.write_text(self.plan_header())
                _ = types_plan.write_text(self.plan_header().replace("names-unit", "types-unit"))
                first_read, second_read = threading.Event(), threading.Event()
                reads = {"names-unit": 0, "types-unit": 0}
                second_saw_committed_ticket: list[bool] = []

                def read_during_refill(
                    repo: Path, doc: Path, ledger: style_ledger.Ledger, unit: str, board: berth_board.Board,
                ) -> style_plan.BusyFiles:
                    busy = read_busy(repo, doc, ledger, unit, board)
                    reads[unit] += 1
                    if reads[unit] < selection_pass:
                        return style_plan.BusyFiles(board, frozenset(self.sources))
                    if unit == "names-unit":
                        first_read.set()
                        _ = second_read.wait(1)
                    else:
                        second_saw_committed_ticket.append(bool(ledger.ticket_open("names-unit")))
                        second_read.set()
                    return busy

                with (mock.patch.object(style_plan, "busy_files", side_effect=read_during_refill),
                      ThreadPoolExecutor(max_workers=2) as pool):
                    first = pool.submit(style_plan.next_phase, "names-unit", self.plan, self.repo)
                    self.assertTrue(first_read.wait(5), "first refill did not read busy files")
                    second = pool.submit(style_plan.next_phase, "types-unit", types_plan, self.repo)
                    first_result, second_result = first.result(timeout=10), second.result(timeout=10)
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

    def test_long_read_windows_balance_each_part_and_cover_the_test_module_separately(self) -> None:
        text = "//! Module docs\n" + "// production\n" * 759 + "#[cfg(test)]\nmod tests {\n" + "// test\n" * 599 + "}\n"
        windows = style_plan.read_windows(text)
        self.assertEqual(windows, (
            style_plan.ReadWindow(1, 254), style_plan.ReadWindow(255, 507), style_plan.ReadWindow(508, 760),
            style_plan.ReadWindow(761, 961), style_plan.ReadWindow(962, 1162), style_plan.ReadWindow(1163, 1362),
        ))
        self.assertEqual([line for window in windows for line in range(window.start, window.end + 1)],
                         list(range(1, len(text.splitlines()) + 1)))
        self.assertTrue(all(window.end - window.start + 1 <= 300 for window in windows))

    def test_short_read_windows_keep_tests_apart_and_leave_files_without_tests_whole(self) -> None:
        cases = (
            ("//! Docs\nfn value() {}\n#[cfg(test)]\nmod tests {}\n",
             (style_plan.ReadWindow(1, 2), style_plan.ReadWindow(3, 4))),
            ("//! Docs\nfn value() {}", (style_plan.ReadWindow(1, 2),)),
            ("#[cfg(test)]\nmod tests {}\n", (style_plan.ReadWindow(1, 2),)),
            ("", ()),
            ("fn value() {\n#[cfg(test)]\nmod nested {}\n}\n", (style_plan.ReadWindow(1, 4),)),
            ("/*\n#[cfg(test)]\nmod example {}\n*/\nfn value() {}\n", (style_plan.ReadWindow(1, 5),)),
            ('const TEXT: &str = r#"\n#[cfg(test)]\nmod example {}\n"#;\n', (style_plan.ReadWindow(1, 4),)),
            ("#[cfg(test)]\nfn helper() {}\n#[cfg(test)]\nmod tests {}\n",
             (style_plan.ReadWindow(1, 2), style_plan.ReadWindow(3, 4))),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(style_plan.read_windows(text), expected)

    def test_read_windows_find_test_modules_past_outer_attributes_and_visibility(self) -> None:
        items = (
            ("#[allow(\n    clippy::expect_used,\n    clippy::unwrap_used\n)]\nmod tests {}", True),
            ('#[expect(clippy::panic, reason = "tests may panic")]\nmod tests {}', True),
            ("pub(crate) mod tests {}", True),
            ("pub mod tests {}", True),
            ("pub(super) mod tests {}", True),
            ("pub(in crate::helpers) mod tests {}", True),
            ('#[allow(dead_code)]\n#[example([nested], reason = "]")]\npub(crate) mod tests {}', True),
            ("#[allow(dead_code)]\nfn helper() {}", False),
            ("#![allow(dead_code)]\nmod tests {}", False),
        )
        for item, is_module in items:
            with self.subTest(item=item):
                text = "//! Docs\nfn value() {}\n#[cfg(test)]\n" + item + "\n"
                end = len(text.splitlines())
                expected = ((style_plan.ReadWindow(1, 2), style_plan.ReadWindow(3, end))
                            if is_module else (style_plan.ReadWindow(1, end),))
                self.assertEqual(style_plan.read_windows(text), expected)

    def test_written_read_phase_lists_base_commit_windows_and_requires_module_docs_before_code(self) -> None:
        _ = self.rule("read")
        self.write(self.sources[0], "//! Module docs\n" + "// production\n" * 599
                   + "#[cfg(test)]\nmod tests {\n" + "// tests\n" * 298 + "}\n")
        self.tip = self.commit()
        for checkout in (self.style, self.unit):
            _ = self.git("merge", "--ff-only", "main", cwd=checkout)
        self.sync()
        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))
        text = self.plan.read_text()
        self.assertIn(f"- `{self.sources[0]}` — lines 1-300, 301-600, 601-751, 752-901\n", text)
        self.assertIn(f"- `{self.sources[1]}` — lines 1-1\n", text)
        instructions = (
            "Judge window by window: take the files under **Files** in order, and each file's windows in order.",
            "read exactly that line range in full", "against every rule above, line by line",
            "giving its last line the same attention as its first",
            "module docs (the `//!` block and any comment before the first item)",
            "judge them against every rule before any code",
            "Note each window's findings before opening the next window, each distinct defect as its own finding",
            "A judge holds no finding and records no cell: it lists its findings in its summary.",
            "A cell is recorded only after both judges have judged it",
            "clean only when both judges call it clean.",
        )
        for instruction in instructions:
            self.assertIn(instruction, text)
        _ = (self.unit / self.sources[0]).write_text("// Working tree now differs from the ticket base.\n")
        self.assertEqual(self.run_next(), (0, "resume names-unit-1\n", ""))
        self.assertEqual(self.plan.read_text(), text)

    def test_written_read_phase_holds_a_site_under_each_broken_rule_before_recording_verdicts(self) -> None:
        _ = self.rule("read")
        _ = self.rule("another-read")
        self.sync()
        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))
        self.assertIn(
            "A site that breaks more than one rule above is held under each rule it breaks, once per rule, "
            + "even when one change fixes both: each rule's cell records its own verdict.\n"
            + "The unit director holds the union of both judges' findings, one finding per defect: "
            + "a defect both judges hold is held once, and a site that breaks more than one rule is held under each rule it breaks.\n"
            + "A cell is recorded only after both judges have judged it: held when either judge holds a finding in it, ",
            self.plan.read_text())

    def test_written_phase_names_read_files_and_explains_refill_without_reads(self) -> None:
        _ = self.rule("read", "tier: C\nfamily: names\ninput_scope: crate\n")
        self.sync()
        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))
        ticket_id = self.ledger().ticket_open("names-unit")[0]["id"]
        self.assertIn(
            f"Record every judged cell with `style_ledger.py record --ticket {ticket_id}`; "
            + "the ledger takes its pinned rule blob.\n"
            + "On a crate or module cell of a Rust source file, a `clean`, `exception` or `held` verdict must have `--input-scope`.\n"
            + "Use `--input-scope file` when the verdict rests on that file alone. It stays current when other files change.\n"
            + "Use `--input-scope rule --reads <files>` when the verdict rests on other files; name each file you read for it. "
            + "Without `--reads`, the cell comes back after any change in its crate or module.\n",
            self.plan.read_text(),
        )

    def test_refill_offers_rule_cell_again_only_when_a_named_read_file_changes(self) -> None:
        rule = self.rule("read", "tier: C\nfamily: names\ninput_scope: crate\n")
        legacy = self.rule("legacy-read", "tier: C\nfamily: names\ninput_scope: crate\n")
        path = self.sources[0]
        read = "crates/alpha/src/b.rs"
        self.write(read, "pub fn read() {}\n")
        self.tip = self.commit()
        self.unit_takes_tip()
        self.sync()
        self.assertTrue(any(rule in ticket.rules and path in ticket.files for ticket in self.candidates()))
        result = subprocess.run(
            [sys.executable, str(Path(style_ledger.__file__)), "record", "--repo", str(self.repo),
             "--rule", rule, "--path", path, "--commit", self.tip, "--rule-blob",
             next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule),
             "--verdict", "clean", "--ticket", "outside", "--worker", "test", "--input-scope", "rule",
             "--reads", read], capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.record(legacy, path, self.tip)
        self.assertFalse(any(rule in ticket.rules and path in ticket.files for ticket in self.candidates()))
        self.assertFalse(any(legacy in ticket.rules and path in ticket.files for ticket in self.candidates()))

        self.write(self.sources[1], "pub fn unrelated() {}\n")
        self.tip = self.commit()
        self.unit_takes_tip()
        self.sync()
        candidates = self.candidates()
        self.assertFalse(any(rule in ticket.rules and path in ticket.files for ticket in candidates))
        self.assertTrue(any(legacy in ticket.rules and path in ticket.files for ticket in candidates))

        self.write(read, "pub fn changed() {}\n")
        self.tip = self.commit()
        self.unit_takes_tip()
        self.sync()
        self.assertTrue(any(rule in ticket.rules and path in ticket.files for ticket in self.candidates()))

    def test_written_names_read_phase_uses_two_blind_judges_for_small_and_large_slices(self) -> None:
        rule = self.rule("read")
        files = list(self.sources[:2]) + [f"crates/alpha/src/extra_{index}.rs" for index in range(11)]
        for path in files[2:]:
            self.write(path, "pub fn extra() {}\n")
        self.tip = self.commit()
        for checkout in (self.style, self.unit):
            _ = self.git("merge", "--ff-only", "main", cwd=checkout)
        self.sync()
        ledger = self.ledger()
        instructions = (
            "Two judges read this ticket, blind to each other: each judges every window of every file against every rule, "
            + "and neither reads the other's findings, board posts or edits until both have finished. Neither judge edits code while judging.",
            "Note each window's findings before opening the next window, each distinct defect as its own finding "
            + "(rule, path, line, text, kind, before and after); never stop at a window's first finding. "
            + "A judge holds no finding and records no cell: it lists its findings in its summary.",
            "The unit director holds the union of both judges' findings, one finding per defect: a defect both judges hold is held once, "
            + "and a site that breaks more than one rule is held under each rule it breaks.",
            "A cell is recorded only after both judges have judged it: held when either judge holds a finding in it, "
            + "clean only when both judges call it clean.",
            "Once both judges have finished, the first judge applies the union's mechanical fixes.",
        )
        for number, count in enumerate((2, 13), 1):
            with self.subTest(files=count):
                ticket_id = f"names-unit-{number}"
                ledger.ticket_add(ticket_id, "names-unit", "read", "names", "crates/alpha", files[:count], [rule])
                ticket = ledger.queued_tickets("names-unit")[-1]
                ledger.ticket_assign(ticket_id, "demo-style-names")
                style_plan.write_phase(ledger, ticket, self.plan, self.plan_header())
                ledger.ticket_drop(ticket_id)
                text = self.plan.read_text()
                self.assertIn(
                    "**Workers:**\n2 judges, blind to each other; each judges every window of every file, "
                    + "then the first judge applies the mechanical fixes.\n", text)
                for instruction in instructions:
                    self.assertIn(instruction + "\n", text)
                self.assertEqual(sorted(text.index(instruction) for instruction in instructions),
                                 [text.index(instruction) for instruction in instructions])
                self.assertNotIn("Hold that window's findings", text)
                self.assertNotIn("Record a file's verdict for every rule only after", text)
                self.assertNotIn("2 writers; split files by subtree.", text)
                self.assertIn(f"style_ledger.py held add --ticket {ticket_id}", text)
                self.assertIn(f"style_ledger.py record --ticket {ticket_id}", text)

    def test_written_name_bindings_and_types_read_phases_keep_single_judge_holding_and_recording(self) -> None:
        plain = self.rule("name-bindings-to-match-parameters", "tier: B\nfamily: names\ncandidates:\n"
                          + "  kind: regex\n  pattern: absent\n  surface: partial\n  uncovered: Read every binding.\n")
        types = self.rule("read-types", "tier: C\nfamily: types\ninput_scope: file\n")
        self.sync()
        ledger = self.ledger()
        for number, (family, rule) in enumerate((("names", plain), ("names", plain + "#uncovered"), ("types", types)), 1):
            with self.subTest(rule=rule):
                unit = family + "-unit"
                ticket_id = f"{unit}-{number}"
                ledger.ticket_add(ticket_id, unit, "read", family, "crates/alpha", list(self.sources[:2]), [rule])
                ticket = ledger.queued_tickets(unit)[-1]
                ledger.ticket_assign(ticket_id, "demo-style-names")
                style_plan.write_phase(ledger, ticket, self.plan, self.plan_header())
                ledger.ticket_drop(ticket_id)
                text = self.plan.read_text()
                self.assertIn(
                    "Hold that window's findings before opening the next window. "
                    + "Hold each distinct defect as its own finding; never stop at a window's first finding.\n"
                    + "A site that breaks more than one rule above is held under each rule it breaks, once per rule, "
                    + "even when one change fixes both: each rule's cell records its own verdict.\n"
                    + "Record a file's verdict for every rule only after its last window has been judged, "
                    + "with `record --rule <rule> --path <file>` for each rule/file cell.\n", text)
                self.assertIn("**Workers:**\n1 writer + 1 tester; the tester opens as impl "
                              + "and re-reads the diff against the pinned rules.\n", text)
                self.assertNotIn("Two judges read this ticket", text)
                self.assertNotIn("The unit director holds the union", text)

    def test_written_names_structure_phase_keeps_its_file_judging_and_workers(self) -> None:
        rule = self.rule("structure", "tier: D\nfamily: modules\n")
        self.sync()
        ledger = self.ledger()
        ledger.ticket_add("names-unit-1", "names-unit", "structure", "names", "crates/alpha",
                          list(self.sources[:2]), [rule], cells=tuple((rule, path) for path in self.sources[:2]))
        ticket = ledger.queued_tickets("names-unit")[0]
        ledger.ticket_assign("names-unit-1", "demo-style-names")
        style_plan.write_phase(ledger, ticket, self.plan, self.plan_header())
        text = self.plan.read_text()
        self.assertIn("Judge file by file: take each file under **Files** in turn", text)
        self.assertIn("Record each file's verdict for every rule above as soon as that file has been judged against it", text)
        self.assertIn("**Workers:**\n1 writer + 1 tester; the tester opens as impl "
                      + "and re-reads the diff against the pinned rules.\n", text)
        self.assertNotIn("Two judges read this ticket", text)
        self.assertNotIn("The unit director holds the union", text)

    def test_written_structure_phase_holds_a_multi_rule_defect_once_without_read_holding_guidance(self) -> None:
        rule = self.rule("structure", "tier: D\nfamily: modules\n")
        self.sync()
        ledger = self.ledger()
        ledger.ticket_add("modules-unit-1", "modules-unit", "structure", "modules", "crates/alpha",
                          list(self.sources[:2]), [rule], cells=tuple((rule, path) for path in self.sources[:2]))
        ticket = ledger.queued_tickets("modules-unit")[0]
        text = style_plan.phase_text(ticket, ledger.pinned_rule_texts(ticket["id"]), self.plan,
                                     style_plan.delegation_context(self.plan.read_text()))
        self.assertNotIn("A site that breaks more than one rule above", text)
        self.assertIn(
            "A defect several rules name is held once, under the rule it breaks most directly, "
            + "naming the others in its text.", text)

    def test_structure_phase_asks_when_to_split_each_file_only_when_the_rule_is_pinned(self) -> None:
        structure = self.rule("structure", "tier: D\nfamily: modules\n")
        split = self.rule("when-to-split-a-module", "tier: D\nfamily: modules\n")
        self.sync()
        ledger = self.ledger()
        ledger.ticket_add("modules-unit-1", "modules-unit", "structure", "modules", "crates/alpha",
                          list(self.sources[:2]), [structure, split],
                          cells=tuple((rule, path) for rule in (structure, split) for path in self.sources[:2]))
        ticket = ledger.queued_tickets("modules-unit")[0]
        texts = ledger.pinned_rule_texts(ticket["id"])
        context = style_plan.delegation_context(self.plan.read_text())
        baseline = style_plan.phase_text(ticket, tuple(pin for pin in texts if pin.rule_id != split),
                                         self.plan, context)
        self.assertNotIn("type clusters", baseline)
        ownership_guidance = (
            "A split finding names the split alone: each test, type or function that belongs with an owner "
            + "in another file is its own finding under the rule that places it, even when the split moves it."
        )
        self.assertNotIn(ownership_guidance, baseline)
        guidance = (
            "For every file, name its type clusters (each type with the impls and functions that serve it) "
            + "and its test domains (groups of tests that exercise separate behaviour), then decide against "
            + "`when-to-split-a-module` whether the file should split, and hold a finding when it should. "
            + "Ask this of every file, also one where other rules already hold findings: moving or reordering "
            + "code under another rule does not answer whether the file should split. " + ownership_guidance
        )
        for suffix in ("", "#uncovered"):
            with self.subTest(surface=suffix):
                pins = tuple(style_ledger.PinnedRuleText(pin.rule_id + suffix, pin.blob, pin.text)
                             if pin.rule_id == split else pin for pin in texts)
                text = style_plan.phase_text(ticket, pins, self.plan, context)
                self.assertEqual(text.count(guidance), 1)
                self.assertEqual(text.count(ownership_guidance), 1)
                self.assertIn(
                    "Hold every distinct defect as its own finding. Never fold several defects into one crate-level finding, "
                    + "and never stop at a file's first finding.\n" + guidance + "\n", text)
                rule_line = next(line for line in text.splitlines() if line.startswith(f"- `{split + suffix}`:"))
                self.assertEqual(text.replace(rule_line + "\n", "").replace(guidance + "\n", ""), baseline)

    def test_written_structure_phase_keeps_its_file_list_and_procedure_without_read_windows(self) -> None:
        rule = self.rule("structure", "tier: D\nfamily: modules\n")
        self.sync()
        plan = self.modules_unit()
        self.assertEqual(style_plan.next_phase("modules-unit", plan, self.repo), style_plan.PhaseWritten("modules-unit-1"))
        text = plan.read_text()
        self.assertEqual(text.split("**Files:**\n", 1)[1].split("\n**Workers:**", 1)[0],
                         "\n".join(f"- `{path}`" for path in self.sources[:2]) + "\n")
        self.assertEqual(text.split("Read only these pinned rule texts.", 1)[1].split("\nProduct work wins.", 1)[0],
                         " A #uncovered rule is the parent rule's uncovered read.\n"
                         + "Judge file by file: take each file under **Files** in turn and judge it against every rule above, "
                         + "with that rule's listed sites in it and its uncovered surface; read the uncovered instructions in the pinned text.\n"
                         + "Before judging a file, read its sibling files in the same module and its direct callers "
                         + "(search the crate for its types and functions), so placement and ownership are judged against the code around it.\n"
                         + "Hold every distinct defect as its own finding. Never fold several defects into one crate-level finding, "
                         + "and never stop at a file's first finding.\n"
                         + "A defect that spans files is held once, under the file that should change. A defect several rules name is held once, "
                         + "under the rule it breaks most directly, naming the others in its text.\n"
                         + "Record each file's verdict for every rule above as soon as that file has been judged against it, "
                         + "with `record --path <file>`; the gate refuses this ticket while any file lacks one.")
        self.assertNotIn("window", text)
        self.assertEqual(self.ledger().ticket_open("modules-unit")[0]["rules"], [rule])

    def test_written_names_phase_explains_conversions_pass_through_to_the_call_parameter(self) -> None:
        rule = self.rule("name-bindings-to-match-parameters")
        self.sync()
        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))
        self.assertEqual(self.ledger().ticket_open("names-unit")[0]["rules"], [rule])
        self.assertIn(
            "uncovered read.\n"
            + "A conversion between the binding and the call passes through: `map_or`, `From::from`, `.into()`, "
            + "an accessor such as `.path()` or `.as_str()`, and a method that returns the same value in another form, "
            + "such as `.to_local_def_id()`; name the binding after the parameter the converted value fills. "
            + "A conversion's own parameter, such as the `value` of `From::from` or the `name` "
            + "of a newtype constructor such as `ThemeId::new(name)`, is never the target and never a second target: "
            + "follow the converted value to the parameter it fills. "
            + "A method that takes arguments or looks up another value (`target.local_upstream(root)`) "
            + "is no accessor: its result is another value. "
            + "A binding fills several parameters only when that same value fills them: "
            + 'a value derived from it (`dir.join("config")`) is another value, '
            + "and a later binding of the same name, as in a second loop, is another binding. "
            + "A call inside a macro's arguments counts, and an enum tuple variant wrapping the binding "
            + "(`Pane::App(id)`) passes through like a conversion. A binding of the same name declared "
            + "later in an inner block, such as a loop or closure body, would only shadow the renamed "
            + "binding; it is no collision unless that inner block also reads the renamed binding. "
            + "A parameter with a single-letter name, such as `x` and `y` in `Position::new(x, y)`, "
            + "gives no name to match: report no finding for it. "
            + "A binding placed as one element of an array or slice literal (`&[item]`) "
            + "does not fill the collection parameter: its name names one element. "
            + "The rule renames a binding that already exists: report no finding whose fix only adds a binding, "
            + "whether it names an inline expression, literal, array or field projection, "
            + "or adds a scoped alias that keeps the old name.\nJudge window by window:",
            self.plan.read_text())

    def test_written_uncovered_names_phase_explains_conversions_pass_through_to_the_call_parameter(self) -> None:
        rule = self.rule("name-bindings-to-match-parameters", "tier: B\nfamily: names\ncandidates:\n"
                         + "  kind: regex\n  pattern: absent\n  surface: partial\n  uncovered: Read every binding.\n")
        self.sync()
        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))
        self.assertEqual(self.ledger().ticket_open("names-unit")[0]["rules"], [rule + "#uncovered"])
        self.assertIn(
            "uncovered read.\n"
            + "A conversion between the binding and the call passes through: `map_or`, `From::from`, `.into()`, "
            + "an accessor such as `.path()` or `.as_str()`, and a method that returns the same value in another form, "
            + "such as `.to_local_def_id()`; name the binding after the parameter the converted value fills. "
            + "A conversion's own parameter, such as the `value` of `From::from` or the `name` "
            + "of a newtype constructor such as `ThemeId::new(name)`, is never the target and never a second target: "
            + "follow the converted value to the parameter it fills. "
            + "A method that takes arguments or looks up another value (`target.local_upstream(root)`) "
            + "is no accessor: its result is another value. "
            + "A binding fills several parameters only when that same value fills them: "
            + 'a value derived from it (`dir.join("config")`) is another value, '
            + "and a later binding of the same name, as in a second loop, is another binding. "
            + "A call inside a macro's arguments counts, and an enum tuple variant wrapping the binding "
            + "(`Pane::App(id)`) passes through like a conversion. A binding of the same name declared "
            + "later in an inner block, such as a loop or closure body, would only shadow the renamed "
            + "binding; it is no collision unless that inner block also reads the renamed binding. "
            + "A parameter with a single-letter name, such as `x` and `y` in `Position::new(x, y)`, "
            + "gives no name to match: report no finding for it. "
            + "A binding placed as one element of an array or slice literal (`&[item]`) "
            + "does not fill the collection parameter: its name names one element. "
            + "The rule renames a binding that already exists: report no finding whose fix only adds a binding, "
            + "whether it names an inline expression, literal, array or field projection, "
            + "or adds a scoped alias that keeps the old name.\nJudge window by window:",
            self.plan.read_text())

    def test_written_names_phase_traces_accessors_and_distinguishes_derived_values_and_later_bindings(self) -> None:
        _ = self.rule("name-bindings-to-match-parameters")
        self.sync()
        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))
        text = self.plan.read_text()
        self.assertIn("an accessor such as `.path()` or `.as_str()`", text)
        self.assertIn("such as `.to_local_def_id()`", text)
        self.assertIn(
            "A method that takes arguments or looks up another value (`target.local_upstream(root)`) "
            + "is no accessor: its result is another value.", text)
        self.assertIn(
            "A binding fills several parameters only when that same value fills them: "
            + 'a value derived from it (`dir.join("config")`) is another value, '
            + "and a later binding of the same name, as in a second loop, is another binding.",
            text)

    def test_written_names_phase_traces_calls_in_macros_and_variants_and_allows_inner_shadowing(self) -> None:
        _ = self.rule("name-bindings-to-match-parameters")
        self.sync()
        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))
        text = self.plan.read_text()
        self.assertIn("A call inside a macro's arguments counts", text)
        self.assertIn("an enum tuple variant wrapping the binding (`Pane::App(id)`)", text)
        self.assertIn(
            "A binding of the same name declared later in an inner block, such as a loop or closure body, "
            + "would only shadow the renamed binding; it is no collision "
            + "unless that inner block also reads the renamed binding.", text)

    def test_written_names_phase_limits_findings_to_existing_bindings_and_checks_accessor_and_shadowing_exceptions(self) -> None:
        _ = self.rule("name-bindings-to-match-parameters")
        self.sync()
        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))
        text = self.plan.read_text()
        for guidance in (
            "is no accessor: its result is another value",
            "no collision unless that inner block also reads the renamed binding",
            "report no finding whose fix only adds a binding",
        ):
            with self.subTest(guidance=guidance):
                self.assertIn(guidance, text)

    def test_written_names_phase_excludes_single_letter_parameters_and_array_elements_before_rename_guidance(self) -> None:
        _ = self.rule("name-bindings-to-match-parameters")
        self.sync()
        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))
        text = self.plan.read_text()
        exclusions = (
            "A parameter with a single-letter name, such as `x` and `y` in `Position::new(x, y)`, "
            + "gives no name to match: report no finding for it.",
            "A binding placed as one element of an array or slice literal (`&[item]`) "
            + "does not fill the collection parameter: its name names one element.",
        )
        for guidance in exclusions:
            with self.subTest(guidance=guidance):
                self.assertIn(guidance, text)
                self.assertLess(text.index(guidance), text.index("The rule renames a binding that already exists"))
        self.assertIn(exclusions[0] + " " + exclusions[1] + " The rule renames a binding that already exists", text)

    def test_written_names_phase_never_targets_a_conversion_parameter_before_following_the_call(self) -> None:
        _ = self.rule("name-bindings-to-match-parameters")
        self.sync()
        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))
        text = self.plan.read_text()
        guidance = (
            "A conversion's own parameter, such as the `value` of `From::from` or the `name` "
            + "of a newtype constructor such as `ThemeId::new(name)`, is never the target and never a second target: "
            + "follow the converted value to the parameter it fills."
        )
        self.assertIn(guidance, text)
        self.assertIn(
            "name the binding after the parameter the converted value fills. "
            + guidance + " A method that takes arguments", text)

    def test_written_names_phase_for_other_rules_keeps_the_existing_spec_without_conversion_guidance(self) -> None:
        _ = self.rule("name-bindings-to-match-parameters-extra")
        self.sync()
        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))
        text = self.plan.read_text()
        self.assertIn("uncovered read.\nJudge window by window:", text)
        self.assertNotIn("A conversion between the binding and the call", text)

    def prepare_binary_crate(self) -> None:
        (self.repo / self.sources[0]).unlink()
        self.write("crates/alpha/src/main.rs", "fn main() {}\n")
        self.tip = self.commit()
        for checkout in (self.style, self.unit):
            _ = self.git("merge", "--ff-only", "main", cwd=checkout)
        self.sync()

    def test_written_read_phase_explains_binary_only_items_are_not_public_outside_the_crate(self) -> None:
        _ = self.rule("read")
        self.prepare_binary_crate()
        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))
        self.assertIn(
            "Read only these pinned rule texts. A #uncovered rule is the parent rule's uncovered read.\n"
            + "`crates/alpha` is binary-only (no library target): none of its items, `pub` or not, "
            + "is public outside the crate.\nJudge window by window:", self.plan.read_text())

    def test_written_read_phase_for_a_default_library_keeps_the_existing_spec(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))
        self.assertIn(
            "Read only these pinned rule texts. A #uncovered rule is the parent rule's uncovered read.\n"
            + "Judge window by window:", self.plan.read_text())
        self.assertNotIn("binary-only", self.plan.read_text())

    def test_written_read_phase_for_a_virtual_root_does_not_claim_a_binary_crate(self) -> None:
        _ = self.rule("read")
        self.write("root.rs", "pub fn root_helper() {}\n")
        self.tip = self.commit()
        for checkout in (self.style, self.unit):
            _ = self.git("merge", "--ff-only", "main", cwd=checkout)
        self.sync()
        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))
        ticket = self.ledger().ticket_open("names-unit")[0]
        self.assertEqual((ticket["crate"], ticket["files"]), (".", ["root.rs"]))
        self.assertIn("- `root.rs` — lines 1-1\n", self.plan.read_text())
        self.assertNotIn("binary-only", self.plan.read_text())

    def test_written_read_phase_recognizes_a_custom_library_target_without_src_lib(self) -> None:
        _ = self.rule("read")
        self.write("crates/alpha/Cargo.toml", '[package]\nname = "alpha"\nversion = "0.1.0"\n'
                   + '[lib]\npath = "library.rs"\n')
        self.write("crates/alpha/library.rs", "pub fn library() {}\n")
        self.prepare_binary_crate()
        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))
        self.assertNotIn("binary-only", self.plan.read_text())
        self.assertIn("uncovered read.\nJudge window by window:", self.plan.read_text())

    def test_written_read_phase_uses_base_targets_when_a_library_is_added_to_the_worktree(self) -> None:
        _ = self.rule("read")
        self.prepare_binary_crate()
        _ = (self.unit / "crates/alpha/src/lib.rs").write_text("pub fn new_library() {}\n")
        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))
        self.assertIn("`crates/alpha` is binary-only (no library target)", self.plan.read_text())
        text = self.plan.read_text()
        self.assertEqual(self.run_next(), (0, "resume names-unit-1\n", ""))
        self.assertEqual(self.plan.read_text(), text)


    def test_structure_file_cells_gate_and_checkpoint_require_the_last_file_verdict(self) -> None:
        rule = self.rule("structure", "tier: D\nfamily: modules\n")
        second = self.rule("structure-other", "tier: D\nfamily: modules\n")
        self.sync()
        ledger = self.ledger()
        paths = self.sources[:2]
        ledger.ticket_add("modules-unit-1", "modules-unit", "structure", "modules", "crates/alpha",
                          list(paths), [rule, second], cells=tuple((name, path) for name in (rule, second) for path in paths))
        ledger.ticket_assign("modules-unit-1", "demo-style-names")
        _ = ledger.ticket_base("modules-unit-1")
        for path in paths:
            self.record(second, path, self.tip, "modules-unit-1")
        self.record(rule, paths[0], self.tip, "modules-unit-1")
        result = subprocess.run([sys.executable, str(Path(style_plan.__file__)), "gate", "--repo", str(self.unit),
                                 "--ticket", "modules-unit-1"], capture_output=True, text=True, check=False)
        self.assertEqual((result.returncode, result.stdout), (1, f"unjudged: {rule} {paths[1]}\n"))
        with self.assertRaisesRegex(style_ledger.LedgerError, f"unjudged cells: {rule} {paths[1]}"):
            ledger.ticket_checkpoint("modules-unit-1", self.tip)
        self.record(rule, paths[1], self.tip, "modules-unit-1")
        result = subprocess.run([sys.executable, str(Path(style_plan.__file__)), "gate", "--repo", str(self.unit),
                                 "--ticket", "modules-unit-1"], capture_output=True, text=True, check=False)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "mechanical\n", ""))
        ledger.ticket_checkpoint("modules-unit-1", self.tip)
        self.assertEqual(ledger.ticket_open("modules-unit")[0]["state"], "checkpointed")

    def test_gate_reports_a_locked_ledger_as_an_error_and_a_rerun_gives_the_review(self) -> None:
        database = self.ledger().database
        _ = self.enterContext(mock.patch.object(style_ledger, "BUSY_TIMEOUT_SECONDS", 0.05))
        with closing(sqlite3.connect(database)) as holder:
            _ = holder.execute("BEGIN EXCLUSIVE")
            self.assertEqual(self.reference_gate("names-unit-9"), (2, "error: database is locked\n"))
            holder.rollback()
        self.assertEqual(self.reference_gate("names-unit-9"), (1, "review: unknown ticket names-unit-9\n"))

    def test_gate_passes_a_footprint_error_through_as_exit_two(self) -> None:
        rule = self.rule("structure", "tier: D\nfamily: modules\n")
        ledger = self.ledger()
        ledger.set_tip(self.tip)
        path = self.sources[0]
        ledger.ticket_add("modules-unit-1", "modules-unit", "structure", "modules", "crates/alpha",
                          [path], [rule], cells=((rule, path),))
        ledger.ticket_assign("modules-unit-1", "demo-style-names")
        _ = ledger.ticket_base("modules-unit-1")
        self.record(rule, path, self.tip, "modules-unit-1")
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE tickets SET base=? WHERE id=?", ("0" * 40, "modules-unit-1"))
        result = subprocess.run([sys.executable, str(Path(style_plan.__file__)), "gate", "--repo", str(self.unit),
                                 "--ticket", "modules-unit-1"], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertTrue(result.stdout.startswith("error: "), result.stdout)

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
        ledger.ticket_add("modules-unit-1", "modules-unit", "structure", "modules", selected.crate,
                          list(selected.files), list(selected.rules), cells=selected.cells)
        ledger.ticket_assign("modules-unit-1", "demo-style-names")
        _ = ledger.ticket_base("modules-unit-1")
        for path in self.sources[:2]:
            self.record(structure, path, self.tip, "modules-unit-1")
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(style_plan.gate(self.unit, "modules-unit-1"), 1)
        self.assertEqual(output.getvalue(), f"unjudged: {manifest} crates/alpha\n")
        self.record(manifest, "crates/alpha", self.tip, "modules-unit-1")
        self.assertEqual(style_plan.gate(self.unit, "modules-unit-1"), 0)
        phase = style_plan.phase_text(ledger.ticket_open("modules-unit")[0], ledger.pinned_rule_texts("modules-unit-1"),
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
        self.assertFalse(any(ticket.crate == "crates/alpha" for ticket in
                             self.candidates("modules", ("crates/alpha/Cargo.toml",))))
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
        scope = style_plan.parse_pilot_scope(
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

    def test_reserved_unmerged_dirty_and_other_open_ticket_files_are_excluded(self) -> None:
        rule = self.rule("read")
        self.sync()
        product = self.root / "product-unit"
        _ = self.git("worktree", "add", "-q", "-b", "product-change", str(product))
        _ = self.git("mv", self.sources[1], "crates/alpha/src/renamed.rs", cwd=product)
        _ = self.git("add", "--all", cwd=product)
        _ = self.git("commit", "-qm", "Product", cwd=product)
        _ = (product / self.sources[2]).write_text("dirty\n")
        parent = self.repo / "docs/product-production.md"
        _ = parent.write_text(parent.read_text() + f"| product-unit | docs/product.md | {product} | product-change |\n")
        ledger = self.ledger()
        ledger.ticket_add("modules-unit-1", "modules-unit", "read", "names", "crates/beta", [self.sources[3]], [rule])
        ledger.ticket_assign("modules-unit-1", "demo-style-modules")
        self.assertEqual(self.candidates(blocked=(self.sources[0],)), ())
        ledger.ticket_drop("modules-unit-1")
        self.assertEqual(tuple(path for ticket in self.candidates(blocked=(self.sources[0],)) for path in ticket.files), (self.sources[3],))

    def test_slices_preserve_directories_and_split_oversized_directories_with_bounds(self) -> None:
        files: list[str] = []
        for directory, count in (("one", 12), ("two", 12), ("large", 30)):
            for index in range(count):
                path = f"crates/alpha/src/{directory}/file{index:02}.rs"
                self.write(path, "// source\n" * 200)
                files.append(path)
        tip = self.commit()
        slices = style_plan.read_slices(self.repo, tip, tuple(reversed(files)))
        self.assertEqual(slices, style_plan.read_slices(self.repo, tip, tuple(files)))
        self.assertEqual({path for group in slices for path in group}, set(files))
        for group in slices:
            self.assertGreaterEqual(len(group), 10)
            self.assertLessEqual(len(group), 25)
            self.assertLessEqual(len(group) * 200, 5000)
        for directory in ("one", "two"):
            self.assertEqual(sum(any(f"/{directory}/" in path for path in group) for group in slices), 1)

    def test_refill_skips_unchanged_held_read_cells_under_open_proposals_after_other_crate_edits(self) -> None:
        read = self.rule("read", "tier: C\nfamily: names\ninput_scope: crate\n")
        self.sync()
        _ = self.held_proposal(read, self.sources[0])
        self.write(self.sources[1], "pub fn changed() {}\n")
        self.tip = self.commit()
        self.sync()
        for path in self.sources[1:]:
            self.record(read, path, self.tip)

        self.assertEqual(next(cell.state for cell in self.ledger().cells(read) if cell.path == self.sources[0]), "stale")
        self.assertEqual(self.candidates(), ())
        self.assertEqual(self.run_next(), (3, "waiting for sync\n", ""))
        self.assertEqual(self.count_tickets(), 0)

    def test_open_proposal_structure_cells_neither_reissue_nor_block_other_families_when_the_file_is_unchanged(self) -> None:
        structure = self.rule("structure", "tier: D\nfamily: modules\n")
        read = self.rule("read")
        self.sync()
        _ = self.held_proposal(structure, self.sources[0])
        self.write(self.sources[1], "pub fn changed() {}\n")
        self.tip = self.commit()
        self.sync()
        for path in self.sources[1:]:
            self.record(structure, path, self.tip)
            self.record(read, path, self.tip)
        self.assertEqual(next(cell.state for cell in self.ledger().cells(structure) if cell.path == self.sources[0]), "stale")
        self.assertEqual(self.candidates("modules"), ())
        self.assertEqual(self.candidates(), (
            style_plan.TicketSelection("read", "names", "crates/alpha", (self.sources[0],), (read,)),
        ))
        with mock.patch.object(berth_board, "read_board", return_value=self.board(self.sources[0])):
            self.assertEqual(self.run_next(), (3, "waiting for sync\n", ""))
        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))
        self.assertEqual(self.ledger().ticket_open("names-unit")[0]["files"], [self.sources[0]])

    def test_refill_reissues_a_held_cell_under_an_open_proposal_when_its_own_file_changes(self) -> None:
        read = self.rule("read", "tier: C\nfamily: names\ninput_scope: crate\n")
        self.sync()
        _ = self.held_proposal(read, self.sources[0])
        self.write(self.sources[0], "pub fn changed() {}\n")
        self.tip = self.commit()
        self.sync()
        self.unit_takes_tip()
        for path in self.sources[1:]:
            self.record(read, path, self.tip)

        self.assertEqual(self.candidates(), (
            style_plan.TicketSelection("read", "names", "crates/alpha", (self.sources[0],), (read,)),
        ))

    def test_refill_reissues_an_unchanged_held_cell_as_soon_as_its_proposal_is_not_open(self) -> None:
        read = self.rule("read", "tier: C\nfamily: names\ninput_scope: crate\n")
        self.sync()
        issue = self.held_proposal(read, self.sources[0])
        self.write(self.sources[1], "pub fn changed() {}\n")
        self.tip = self.commit()
        self.sync()
        self.unit_takes_tip()
        for path in self.sources[1:]:
            self.record(read, path, self.tip)
        self.assertEqual(self.candidates(), ())

        for status in ("closed", "approved", "declined"):
            with self.subTest(status=status):
                _ = issue.write_text(f"---\nstatus: {status}\n---\n")
                self.assertEqual(self.candidates(), (
                    style_plan.TicketSelection("read", "names", "crates/alpha", (self.sources[0],), (read,)),
                ))

    def test_refill_still_issues_stale_cells_that_were_never_held(self) -> None:
        read = self.rule("read", "tier: C\nfamily: names\ninput_scope: crate\n")
        self.sync()
        self.record(read, self.sources[0], self.tip)
        self.write(self.sources[1], "pub fn changed() {}\n")
        self.tip = self.commit()
        self.sync()
        self.unit_takes_tip()
        for path in self.sources[1:]:
            self.record(read, path, self.tip)

        self.assertEqual(self.candidates(), (
            style_plan.TicketSelection("read", "names", "crates/alpha", (self.sources[0],), (read,)),
        ))

    def test_refill_requires_every_finding_on_a_held_cell_to_have_an_open_proposal(self) -> None:
        read = self.rule("read", "tier: C\nfamily: names\ninput_scope: crate\n")
        self.sync()
        _ = self.held_proposal(read, self.sources[0])
        self.write(self.sources[1], "pub fn changed() {}\n")
        self.tip = self.commit()
        self.sync()
        self.unit_takes_tip()
        for path in self.sources[1:]:
            self.record(read, path, self.tip)
        self.assertEqual(self.candidates(), ())

        ledger = self.ledger()
        blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == read)
        finding = ledger.hold(read, self.sources[0], 1, "another defect", "rename", "before", "after", blob, "outside")
        expected = (style_plan.TicketSelection("read", "names", "crates/alpha", (self.sources[0],), (read,)),)
        self.assertEqual(self.candidates(), expected)
        second = self.root / "second-proposal.md"
        _ = second.write_text("---\nstatus: open\n---\n")
        ledger.file_held(finding.finding_id, second)
        self.assertEqual(self.candidates(), ())
        _ = second.write_text("---\nstatus: closed\n---\n")
        self.assertEqual(self.candidates(), expected)

    def test_refill_uses_the_latest_verdict_instead_of_an_older_held_judgment(self) -> None:
        read = self.rule("read", "tier: C\nfamily: names\ninput_scope: crate\n")
        self.sync()
        _ = self.held_proposal(read, self.sources[0])
        self.record(read, self.sources[0], self.tip, ticket="later-clean")
        self.write(self.sources[1], "pub fn changed() {}\n")
        self.tip = self.commit()
        self.sync()
        self.unit_takes_tip()
        for path in self.sources[1:]:
            self.record(read, path, self.tip)

        self.assertEqual(self.candidates(), (
            style_plan.TicketSelection("read", "names", "crates/alpha", (self.sources[0],), (read,)),
        ))

    def held_cell_with_final_findings(self) -> tuple[str, tuple[style_plan.TicketSelection, ...]]:
        """Hold crate alpha's lib.rs under an open proposal, then change only another file of its crate."""
        read = self.rule("read", "tier: C\nfamily: names\ninput_scope: crate\n")
        self.sync()
        _ = self.held_proposal(read, self.sources[0])
        self.write(self.sources[1], "pub fn changed() {}\n")
        self.tip = self.commit()
        self.sync()
        self.unit_takes_tip()
        for path in self.sources[1:]:
            self.record(read, path, self.tip)
        return read, (style_plan.TicketSelection("read", "names", "crates/alpha", (self.sources[0],), (read,)),)

    def hold_more(self, read: str, *states: str) -> None:
        """Add one finding to crate alpha's lib.rs in each state."""
        ledger = self.ledger()
        blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == read)
        for number, state in enumerate(states, len(ledger.held_findings(style_ledger.AllHeldFindings())) + 1):
            finding = ledger.hold(read, self.sources[0], number, f"defect {number}", "rename", "before", "after", blob, "outside")
            if state == "unfiled":
                continue
            ledger.file_held(finding.finding_id, self.root / "proposal.md")
            with closing(sqlite3.connect(ledger.database)) as connection:
                _ = connection.execute("UPDATE held_findings SET state=?,withdrawn_by=? WHERE finding_id=?",
                                       (state, int(state == "withdrawn"), finding.finding_id))
                connection.commit()

    def test_held_cell_skip_ignores_waived_findings_beside_filed_ones(self) -> None:
        read, expected = self.held_cell_with_final_findings()
        self.hold_more(read, "waived")
        self.assertEqual(self.candidates(), ())
        self.hold_more(read, "unfiled")
        self.assertEqual(self.candidates(), expected)

    def test_held_cell_with_only_final_findings_is_not_skipped(self) -> None:
        read, expected = self.held_cell_with_final_findings()
        self.hold_more(read, "waived", "built", "withdrawn")
        with closing(sqlite3.connect(self.ledger().database)) as connection:
            _ = connection.execute("UPDATE held_findings SET state='built' WHERE state='filed'")
            connection.commit()
        self.assertEqual({type(finding.state) for finding in self.ledger().held_findings(style_ledger.AllHeldFindings())},
                         {style_ledger.Waived, style_ledger.Built, style_ledger.Withdrawn})
        self.assertEqual(self.candidates(), expected)

    def judged_at_unit_tip_then_style_moves(self) -> tuple[str, str]:
        """Judge crate alpha's lib.rs at the unit tip, merge that into the style branch, then change crate alpha there.

        It returns the rule and the unit tip.
        """
        read = self.rule("read", "tier: C\nfamily: names\ninput_scope: crate\n")
        self.sync()
        for path in self.sources[1:]:
            self.record(read, path, self.tip)
        _ = (self.unit / self.sources[0]).write_text("pub fn judged() {}\n")
        _ = self.git("commit", "-qam", "Ticket work", cwd=self.unit)
        unit_tip = self.git("rev-parse", "HEAD", cwd=self.unit)
        self.record(read, self.sources[0], unit_tip)
        _ = self.git("merge", "-q", "--ff-only", "demo-style-names", cwd=self.style)
        self.assertEqual(self.cell_state(read, self.sources[0]), "awaiting merge")
        _ = (self.style / self.sources[1]).write_text("pub fn other_unit() {}\n")
        _ = self.git("commit", "-qam", "Another unit's work in the same crate", cwd=self.style)
        return read, unit_tip

    def cell_state(self, rule: str, path: str) -> style_ledger.CellState:
        return next(cell.state for cell in self.ledger().cells(rule) if cell.path == path)

    def test_refill_says_merge_the_style_branch_first_when_each_cell_left_was_judged_at_the_unit_tip(self) -> None:
        read, unit_tip = self.judged_at_unit_tip_then_style_moves()
        self.assertEqual(self.cell_state(read, self.sources[0]), "stale")
        self.seed_sync_wait()

        self.assertEqual(self.candidates(), ())
        self.assertEqual(self.run_next(),
                         (6, f"merge the style branch first: 1 cell judged at {unit_tip[:7]} wait for style/main\n", ""))
        self.assertEqual(self.count_tickets(), 0)
        self.assertEqual(self.sync_wait_rows(), [])
        self.assertEqual(self.cell_state(read, self.sources[0]), "stale")

        _ = self.git("merge", "-q", "--ff-only", "style/main", cwd=self.unit)
        merged = self.git("rev-parse", "HEAD", cwd=self.unit)
        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))
        ticket = self.ledger().ticket_open("names-unit")[0]
        self.assertEqual((ticket["files"], ticket["base"]), ([self.sources[0]], merged))
        self.record(read, self.sources[0], merged, ticket="names-unit-1")
        self.assertEqual(self.cell_state(read, self.sources[0]), "awaiting merge")

    def test_refill_issues_a_stale_cell_whose_inputs_at_the_unit_tip_changed_since_its_judgment(self) -> None:
        read, _ = self.judged_at_unit_tip_then_style_moves()
        _ = (self.unit / "crates/alpha/README.md").write_text("Unit notes.\n")
        _ = self.git("add", "crates/alpha/README.md", cwd=self.unit)
        _ = self.git("commit", "-qm", "Unit change in the same crate", cwd=self.unit)

        self.assertEqual(self.candidates(), (
            style_plan.TicketSelection("read", "names", "crates/alpha", (self.sources[0],), (read,)),
        ))

    def judged_on_all_sources_then_main_changes_crate_alpha(self) -> str:
        """Judge every source at the unit tip with a crate-scope rule, then change crate alpha on main and sync."""
        read = self.rule("read", "tier: C\nfamily: names\ninput_scope: crate\n")
        self.sync()
        for path in self.sources:
            self.record(read, path, self.tip)
        self.write(self.sources[1], "pub fn changed() {}\n")
        self.tip = self.commit()
        self.sync()
        self.assertEqual(self.cell_state(read, self.sources[0]), "stale")
        return read

    def test_refill_waits_for_sync_until_the_style_tip_changes_a_cell_judged_at_the_unit_tip(self) -> None:
        read = self.judged_on_all_sources_then_main_changes_crate_alpha()
        unit_tip = self.git("rev-parse", "HEAD", cwd=self.unit)
        self.assertEqual(self.candidates(), ())

        self.assertEqual(self.run_next(), (3, "waiting for sync\n", ""))
        self.assertEqual([row[0] for row in self.sync_wait_rows()], ["names-unit"])
        _ = self.git("merge", "-q", "--ff-only", "style/main", cwd=self.unit)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.unit), unit_tip)
        self.assertEqual(self.run_next(), (3, "waiting for sync\n", ""))
        self.assertEqual(self.count_tickets(), 0)

        _ = self.git("merge", "-q", "--ff-only", self.tip, cwd=self.style)
        self.assertEqual(self.run_next(),
                         (6, f"merge the style branch first: 2 cells judged at {unit_tip[:7]} wait for style/main\n", ""))
        self.assertEqual(self.sync_wait_rows(), [])
        _ = self.git("merge", "-q", "--ff-only", "style/main", cwd=self.unit)
        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))
        ticket = self.ledger().ticket_open("names-unit")[0]
        self.assertEqual((ticket["files"], ticket["rules"], ticket["base"]), (list(self.sources[:2]), [read], self.tip))

    def test_refill_names_a_style_branch_missing_on_origin(self) -> None:
        _ = self.judged_at_unit_tip_then_style_moves()
        origin = self.root / "origin.git"
        _ = self.git("init", "-q", "--bare", str(origin))
        _ = self.git("remote", "add", "origin", str(origin))
        _ = self.git("push", "-q", "origin", "main")

        code, output, errors = self.run_next()
        self.assertEqual((code, output), (2, ""))
        self.assertIn("cannot be resolved; fetch origin first", errors)
        self.assertEqual(self.count_tickets(), 0)

    def test_refill_without_a_stored_style_branch_waits_for_sync(self) -> None:
        read = self.rule("read", "tier: C\nfamily: names\ninput_scope: crate\n")
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.set_tip(self.tip)
        for path in self.sources:
            self.record(read, path, self.tip)
        self.write(self.sources[1], "pub fn changed() {}\n")
        self.tip = self.commit()
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.set_tip(self.tip)
        self.assertEqual(self.cell_state(read, self.sources[0]), "stale")
        self.assertEqual(self.candidates(), ())

        self.assertEqual(self.run_next(), (3, "waiting for sync\n", ""))

    def test_refill_waits_for_sync_when_the_unit_tip_is_not_in_the_style_tip(self) -> None:
        read = self.rule("read", "tier: C\nfamily: names\ninput_scope: crate\n")
        self.sync()
        for path in self.sources:
            self.record(read, path, self.tip)
        _ = (self.unit / self.sources[2]).write_text("pub fn unit_only() {}\n")
        _ = self.git("commit", "-qam", "Unit change in crate beta only", cwd=self.unit)
        _ = (self.style / self.sources[1]).write_text("pub fn changed() {}\n")
        _ = self.git("commit", "-qam", "Change crate alpha", cwd=self.style)
        self.tip = self.git("rev-parse", "HEAD", cwd=self.style)
        self.sync()
        self.assertEqual(self.cell_state(read, self.sources[0]), "stale")
        self.assertEqual(self.candidates(), ())

        self.assertEqual(self.run_next(), (3, "waiting for sync\n", ""))

    def test_refill_issues_a_cell_whose_newest_judgment_is_not_in_the_unit_tip(self) -> None:
        read = self.rule("read", "tier: C\nfamily: names\ninput_scope: crate\n")
        self.sync()
        _ = (self.style / self.sources[2]).write_text("pub fn beta_only() {}\n")
        _ = self.git("commit", "-qam", "Change crate beta only", cwd=self.style)
        self.record(read, self.sources[0], self.git("rev-parse", "HEAD", cwd=self.style))
        _ = (self.style / self.sources[1]).write_text("pub fn changed() {}\n")
        _ = self.git("commit", "-qam", "Change crate alpha", cwd=self.style)
        self.tip = self.git("rev-parse", "HEAD", cwd=self.style)
        self.sync()
        for path in self.sources[1:]:
            self.record(read, path, self.tip)
        self.assertEqual(self.cell_state(read, self.sources[0]), "stale")

        self.assertEqual(self.candidates(), (
            style_plan.TicketSelection("read", "names", "crates/alpha", (self.sources[0],), (read,)),
        ))

    def test_refill_issues_a_cell_whose_newest_judgment_is_not_in_the_style_branch(self) -> None:
        read = self.rule("read", "tier: C\nfamily: names\ninput_scope: crate\n")
        self.sync()
        _ = (self.unit / self.sources[2]).write_text("pub fn beta_only() {}\n")
        _ = self.git("commit", "-qam", "Unit change in crate beta only", cwd=self.unit)
        self.record(read, self.sources[0], self.git("rev-parse", "HEAD", cwd=self.unit))
        self.write(self.sources[1], "pub fn changed() {}\n")
        self.tip = self.commit()
        self.sync()
        _ = self.git("merge", "-q", "--ff-only", self.tip, cwd=self.style)
        for path in self.sources[1:]:
            self.record(read, path, self.tip)
        self.assertEqual(self.cell_state(read, self.sources[0]), "stale")

        self.assertEqual(self.candidates(), (
            style_plan.TicketSelection("read", "names", "crates/alpha", (self.sources[0],), (read,)),
        ))

    def test_names_unit_waits_for_a_stale_structure_cell_judged_in_its_tip(self) -> None:
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        read = self.rule("read")
        self.sync()
        for path in self.sources:
            self.record(structure, path, self.tip)
        for path in self.sources[1:]:
            self.record(read, path, self.tip)
        self.write(self.sources[0], "pub fn changed() {}\n")
        self.tip = self.commit()
        self.sync()
        _ = self.git("merge", "-q", "--ff-only", self.tip, cwd=self.style)
        self.assertEqual(self.cell_state(structure, self.sources[0]), "stale")
        self.assertEqual(self.cell_state(read, self.sources[0]), "open")

        self.assertEqual(self.candidates(), ())
        self.assertEqual(self.run_next(), (3, "waiting for structure: crates/alpha\n", ""))

    def test_modules_unit_merges_the_style_branch_first_for_a_structure_cell_judged_at_its_tip(self) -> None:
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        self.sync()
        plan = self.modules_unit()
        unit_tip = self.tip
        for path in self.sources:
            self.record(structure, path, self.tip)
        self.write(self.sources[0], "pub fn changed() {}\n")
        self.tip = self.commit()
        self.sync()
        _ = self.git("merge", "-q", "--ff-only", self.tip, cwd=self.style)
        self.assertEqual(self.cell_state(structure, self.sources[0]), "stale")
        self.assertEqual(self.candidates("modules"), ())

        self.assertEqual(style_plan.next_phase("modules-unit", plan, self.repo),
                         style_plan.MergeStyleBranchFirst(2, unit_tip, "refs/heads/style/main"))
        _ = self.git("merge", "-q", "--ff-only", "style/main", cwd=self.root / "modules")
        self.assertEqual(style_plan.next_phase("modules-unit", plan, self.repo), style_plan.PhaseWritten("modules-unit-1"))
        ticket = self.ledger().ticket_open("modules-unit")[0]
        self.assertEqual((ticket["kind"], ticket["files"], ticket["base"]), ("structure", list(self.sources[:2]), self.tip))

    def test_structure_precedes_family_reads_and_retains_partial_rule_sites(self) -> None:
        read = self.rule("read")
        structure = self.rule("structure", "tier: D\nfamily: modules\ncandidates:\n  kind: regex\n  pattern: value\n  surface: partial\n  uncovered: Read the crate layout.\n")
        self.sync()
        self.assertEqual(self.candidates(), ())
        tickets = self.candidates("modules")
        self.assertEqual(len(tickets), 2)
        self.assertEqual(tickets[0].kind, "structure")
        self.assertEqual(tickets[0].rules, (structure,))
        self.assertTrue(tickets[0].sites)
        preview = io.StringIO()
        with mock.patch.object(berth_board, "read_board", return_value=self.board(self.sources[0])):
            with redirect_stdout(preview):
                self.assertEqual(style_plan.main(["queue", "--repo", str(self.repo)]), 0)
        self.assertTrue(preview.getvalue().startswith("modules-unit: structure"))
        self.assertNotIn(self.sources[0], preview.getvalue())
        self.assertIn(self.sources[1], preview.getvalue())
        ledger = self.ledger()
        for path in self.sources:
            self.record(structure, path, self.tip)
        reads = self.candidates()
        self.assertTrue(reads)
        self.assertEqual({rule for ticket in reads for rule in ticket.rules}, {read})
        self.assertFalse(any(cell.rule.endswith("#uncovered") for cell in ledger.cells(rule=structure)))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(style_plan.main(["queue", "--repo", str(self.repo)]), 0)
        self.assertIn("names-unit: read", output.getvalue())

    def test_names_read_refill_takes_only_files_without_pending_structure_cells(self) -> None:
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        read = self.rule("read")
        self.sync()
        self.record(structure, self.sources[1], self.tip)
        _ = self.plan.write_text(self.plan_header().replace("## Phases", "**Pilot scope:** alpha\n\n## Phases"))

        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))
        ticket = self.ledger().ticket_open("names-unit")[0]
        self.assertEqual((ticket["kind"], ticket["rules"], ticket["files"]),
                         ("read", [read], [self.sources[1]]))
        self.assertIn(f"- `{self.sources[1]}` — lines 1-1\n", self.plan.read_text())
        self.assertNotIn(f"- `{self.sources[0]}`", self.plan.read_text())

    def test_names_listed_refill_packs_only_sites_on_files_without_pending_structure_cells(self) -> None:
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        listed = self.rule("listed", "tier: B\nfamily: names\npre_filter: value\n")
        self.sync()
        self.record(structure, self.sources[1], self.tip)
        _ = self.plan.write_text(self.plan_header().replace("## Phases", "**Pilot scope:** alpha\n\n## Phases"))

        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))
        ticket = self.ledger().ticket_open("names-unit")[0]
        self.assertEqual((ticket["kind"], ticket["rules"], ticket["files"]),
                         ("listed", [listed], [self.sources[1]]))
        self.assertEqual([(site["path"], site["line"]) for site in ticket["sites"]], [(self.sources[1], 1)])

    def test_names_file_whose_structure_cells_are_judged_refills_while_its_crate_and_other_files_wait(self) -> None:
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        manifest = self.rule("manifest", "tier: D\nfamily: modules\ninput_scope: manifest\n")
        read = self.rule("read")
        self.sync()
        self.record(structure, self.sources[0], self.tip)
        _ = self.plan.write_text(self.plan_header().replace("## Phases", "**Pilot scope:** alpha\n\n## Phases"))
        pending = {(cell.rule, cell.path) for cell in self.ledger().cells(crate="crates/alpha") if cell.state == "open"}
        self.assertLessEqual({(manifest, "crates/alpha"), (structure, self.sources[1])}, pending)

        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))
        ticket = self.ledger().ticket_open("names-unit")[0]
        self.assertEqual((ticket["kind"], ticket["rules"], ticket["files"]), ("read", [read], [self.sources[0]]))

    def test_names_waits_for_sync_not_structure_when_its_only_unblocked_file_is_busy(self) -> None:
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        _ = self.rule("manifest", "tier: D\nfamily: modules\ninput_scope: manifest\n")
        _ = self.rule("read")
        self.sync()
        self.record(structure, self.sources[0], self.tip)
        _ = self.plan.write_text(self.plan_header().replace("## Phases", "**Pilot scope:** alpha\n\n## Phases"))

        with mock.patch.object(berth_board, "read_board", return_value=self.board(self.sources[0])):
            self.assertEqual(self.run_next(), (3, "waiting for sync\n", ""))
        self.assertEqual(self.count_tickets(), 0)

    def test_a_crate_cell_waits_for_every_structure_cell_in_its_crate_and_a_file_cell_for_its_own(self) -> None:
        def cell(path: str, tier: rules.Tier, crate: str = "crates/alpha") -> style_ledger.Cell:
            return style_ledger.Cell("rule", path, crate, tier, "names" if tier == "C" else "modules", "open")

        crate_cell, file_cell = cell("crates/alpha", "C"), cell(self.sources[0], "C")
        other_file, own_file, crate_structure = cell(self.sources[1], "D"), cell(self.sources[0], "D"), cell("crates/alpha", "D")
        other_crate = cell(self.sources[2], "D", "crates/beta")
        self.assertTrue(style_plan.blocked_by_structure(crate_cell, (other_file,)))
        self.assertFalse(style_plan.blocked_by_structure(crate_cell, (other_crate,)))
        self.assertTrue(style_plan.blocked_by_structure(file_cell, (other_file, own_file)))
        self.assertFalse(style_plan.blocked_by_structure(file_cell, (other_file, crate_structure, other_crate)))

    def test_names_waits_for_structure_when_every_pending_names_file_is_closed(self) -> None:
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        read = self.rule("read")
        self.sync()
        self.record(structure, self.sources[1], self.tip)
        self.record(read, self.sources[1], self.tip)
        _ = self.plan.write_text(self.plan_header().replace("## Phases", "**Pilot scope:** alpha\n\n## Phases"))
        before = self.plan.read_bytes()

        self.assertEqual(self.run_next(), (3, "waiting for structure: crates/alpha\n", ""))
        self.assertEqual(self.plan.read_bytes(), before)
        self.assertEqual(self.count_tickets(), 0)
        self.assertEqual([row[0] for row in self.sync_wait_rows()], ["names-unit"])

    def test_names_waits_for_sync_when_an_open_names_file_is_busy_despite_other_closed_files(self) -> None:
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        _ = self.rule("read")
        self.sync()
        self.record(structure, self.sources[1], self.tip)
        _ = self.plan.write_text(self.plan_header().replace("## Phases", "**Pilot scope:** alpha\n\n## Phases"))

        with mock.patch.object(berth_board, "read_board", return_value=self.board(self.sources[1])):
            self.assertEqual(self.run_next(), (3, "waiting for sync\n", ""))
        self.assertEqual(self.count_tickets(), 0)

    def test_modules_still_selects_only_pending_structure_files_when_names_can_refill(self) -> None:
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        _ = self.rule("read")
        self.sync()
        for path in self.sources[1:]:
            self.record(structure, path, self.tip)
        before = self.candidates("modules")
        self.assertEqual([(ticket.kind, ticket.crate, ticket.files, ticket.cells) for ticket in before], [
            ("structure", "crates/alpha", (self.sources[0],), ((structure, self.sources[0]),)),
        ])

        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))
        self.assertEqual(self.candidates("modules"), before)

    def test_structure_first_unblocks_more_other_family_cells_before_staler_crates(self) -> None:
        read = self.rule("read")
        own = self.rule("module-read", "tier: C\nfamily: modules\ninput_scope: file\n")
        structure = self.rule("structure", "tier: D\nfamily: modules\n")
        self.sync()
        self.record(read, self.sources[0], self.tip)
        self.write(self.sources[0], "pub fn changed() {}\n")
        self.tip = self.commit()
        self.sync()
        self.record(read, self.sources[1], self.tip)
        for path in self.sources[2:]:
            self.record(own, path, self.tip)

        tickets = self.candidates("modules")

        self.assertEqual([(ticket.crate, ticket.kind, ticket.rules) for ticket in tickets], [
            ("crates/beta", "structure", (structure,)), ("crates/alpha", "structure", (structure,)),
        ])
        ledger = self.ledger()
        scoped = style_plan.candidate_tickets(self.repo, ledger, style_plan.BusyFiles(self.board(), frozenset()),
                                             "modules", style_plan.PilotCrates(frozenset({"crates/alpha"})),
                                             base=self.base("modules"))
        self.assertEqual([(ticket.crate, ticket.kind) for ticket in scoped], [("crates/alpha", "structure")])

    def test_structure_priority_counts_names_cells_only_inside_the_names_pilot_scope(self) -> None:
        _ = self.rule("read")
        _ = self.rule("structure", "tier: D\nfamily: modules\n")
        for name in ("extra", "more"):
            self.write(f"crates/beta/src/{name}.rs", "pub fn value() {}\n")
        self.tip = self.commit()
        self.sync()
        ledger = self.ledger()
        busy = style_plan.BusyFiles(self.board(), frozenset())
        self.assertEqual(style_plan.candidate_tickets(self.repo, ledger, busy, "modules", base=self.base("modules"))[0].crate, "crates/beta")

        tickets = style_plan.candidate_tickets(
            self.repo, ledger, busy, "modules",
            family_scopes={"modules": style_plan.AllCrates(),
                           "names": style_plan.PilotCrates(frozenset({"crates/alpha"}))},
            base=self.base("modules"))

        self.assertEqual([ticket.crate for ticket in tickets], ["crates/alpha", "crates/beta"])
        self.assertEqual({path for ticket in tickets for path in ticket.files},
                         set(self.sources) | {"crates/beta/src/extra.rs", "crates/beta/src/more.rs"})

    def test_structure_priority_ignores_pending_cells_of_a_family_without_a_unit_row(self) -> None:
        names = self.rule("read")
        types = (self.rule("types-one", "tier: C\nfamily: types\ninput_scope: file\n"),
                 self.rule("types-two", "tier: C\nfamily: types\ninput_scope: file\n"))
        _ = self.rule("structure", "tier: D\nfamily: modules\n")
        self.sync()
        for rule in types:
            for path in self.sources[:2]:
                self.record(rule, path, self.tip)
        for path in self.sources[2:]:
            self.record(names, path, self.tip)
        ledger = self.ledger()
        busy = style_plan.BusyFiles(self.board(), frozenset())
        self.assertEqual(style_plan.candidate_tickets(self.repo, ledger, busy, "modules", base=self.base("modules"))[0].crate, "crates/beta")

        tickets = style_plan.candidate_tickets(
            self.repo, ledger, busy, "modules", family_scopes=style_plan.family_pilot_scopes(self.doc, self.repo),
            base=self.base("modules"))

        self.assertEqual([ticket.crate for ticket in tickets], ["crates/alpha", "crates/beta"])

    def test_family_pilot_scopes_reads_unit_copies_and_missing_plans_allow_all_crates(self) -> None:
        _ = self.plan.write_text(self.plan_header().replace("## Phases", "**Pilot scope:** alpha\n\n## Phases"))
        template = self.style / "docs/style-production/units/names-unit.md"
        _ = template.write_text(self.plan_header().replace("## Phases", "**Pilot scope:** beta\n\n## Phases"))
        _ = self.doc.write_text(self.doc.read_text() + f"| product-unit | docs/product.md | {self.root / 'product'} | product |\n")

        self.assertEqual(style_plan.family_pilot_scopes(self.doc, self.repo), {
            "names": style_plan.PilotCrates(frozenset({"crates/alpha"})), "modules": style_plan.AllCrates(),
        })
        self.plan.unlink()
        self.assertEqual(style_plan.family_pilot_scopes(self.doc, self.repo), {
            "names": style_plan.AllCrates(), "modules": style_plan.AllCrates(),
        })

    def test_modules_refill_and_queue_prioritize_the_crate_blocking_a_pilot_unit(self) -> None:
        _ = self.rule("read")
        _ = self.rule("structure", "tier: D\nfamily: modules\n")
        self.write("crates/beta/src/extra.rs", "pub fn value() {}\n")
        self.tip = self.commit()
        self.sync()
        _ = self.plan.write_text(self.plan_header().replace("## Phases", "**Pilot scope:** alpha\n\n## Phases"))
        worktree = self.root / "modules"
        _ = self.git("worktree", "add", "-q", "-b", "demo-style-modules", str(worktree))
        plan = worktree / "docs/style-production/units/modules-unit.md"
        plan.parent.mkdir(parents=True)
        _ = plan.write_text(self.plan_header())
        preview = io.StringIO()
        with redirect_stdout(preview):
            self.assertEqual(style_plan.main(["queue", "--repo", str(self.repo), "--unit", "modules-unit"]), 0)
        self.assertTrue(preview.getvalue().startswith("modules-unit: structure crates/alpha "), preview.getvalue())

        output = io.StringIO()
        with redirect_stdout(output):
            code = style_plan.main(["next-phase", "--unit", "modules-unit", "--plan", str(plan), "--repo", str(worktree)])

        self.assertEqual((code, output.getvalue()), (0, "phase written: modules-unit-1\n"))
        ticket = self.ledger().ticket_open("modules-unit")[0]
        self.assertEqual((ticket["kind"], ticket["crate"], ticket["files"]),
                         ("structure", "crates/alpha", list(self.sources[:2])))

    def test_equal_blocked_structure_counts_use_staleness_then_crate_name(self) -> None:
        read = self.rule("read")
        _ = self.rule("structure", "tier: D\nfamily: modules\n")
        self.sync()
        original_tip = self.tip
        self.record(read, self.sources[2], original_tip)
        self.write(self.sources[2], "pub fn changed_beta() {}\n")
        self.tip = self.commit()
        self.sync()
        self.assertEqual([ticket.crate for ticket in self.candidates("modules")], ["crates/beta", "crates/alpha"])

        self.record(read, self.sources[0], original_tip)
        self.write(self.sources[0], "pub fn changed_alpha() {}\n")
        self.tip = self.commit()
        self.sync()
        self.assertEqual([ticket.crate for ticket in self.candidates("modules")], ["crates/alpha", "crates/beta"])

    def test_current_other_family_cells_do_not_raise_structure_priority(self) -> None:
        read = self.rule("read")
        _ = self.rule("structure", "tier: D\nfamily: modules\n")
        self.sync()
        for path in self.sources[:2]:
            self.record(read, path, self.tip)

        self.assertEqual([ticket.crate for ticket in self.candidates("modules")], ["crates/beta", "crates/alpha"])

    def test_other_family_keeps_stale_order_and_skips_pending_structure(self) -> None:
        self.write("Cargo.toml", '[workspace]\nmembers = ["crates/alpha", "crates/beta", "crates/gamma"]\n')
        self.write("crates/gamma/Cargo.toml", '[package]\nname = "gamma"\nversion = "0.1.0"\n')
        gamma = "crates/gamma/src/lib.rs"
        self.write(gamma, "pub fn value() {}\n")
        self.write("docs/alpha/style/structure.md", "---\ntier: D\nfamily: modules\n---\n# Structure\n")
        self.tip = self.commit()
        read = self.rule("read")
        _ = self.rule("types", "tier: C\nfamily: types\ninput_scope: file\n")
        self.sync()
        self.record(read, gamma, self.tip)
        self.write(gamma, "pub fn changed() {}\n")
        self.tip = self.commit()
        self.sync()

        tickets = self.candidates("names")

        self.assertEqual([ticket.crate for ticket in tickets], ["crates/gamma", "crates/beta"])
        self.assertEqual({ticket.kind for ticket in tickets}, {"read"})
        self.assertEqual({rule for ticket in tickets for rule in ticket.rules}, {read})

    def test_structure_phase_contains_its_pinned_uncovered_instructions_and_sites(self) -> None:
        rule = self.rule("structure", "tier: D\nfamily: modules\ncandidates:\n  kind: regex\n  pattern: value\n  surface: partial\n  uncovered: Read the crate layout.\n")
        self.sync()
        worktree = self.root / "modules"
        _ = self.git("worktree", "add", "-q", "-b", "demo-style-modules", str(worktree))
        plan = worktree / "docs/style-production/units/modules-unit.md"
        plan.parent.mkdir(parents=True)
        _ = plan.write_text(self.plan_header())
        result = style_plan.next_phase("modules-unit", plan, self.repo)
        self.assertIsInstance(result, style_plan.PhaseWritten)
        ticket = self.ledger().ticket_open("modules-unit")[0]
        self.assertEqual(ticket["rules"], [rule])
        self.assertEqual({site["path"] for site in ticket["sites"]}, set(self.sources[:2]))
        text = plan.read_text()
        self.assertIn("uncovered: Read the crate layout.", text)
        self.assertIn('"line": 1', text)
        self.assertIn("Every structure finding is held", text)
        self.assertNotIn("#uncovered`", text)
        pin = self.ledger().pinned_rule_texts(ticket["id"])[0]
        self.assertIn(str(plan.parent.parent / "rules" / (pin.blob + ".md")), text)

    def test_only_structure_phases_require_each_file_and_every_distinct_defect(self) -> None:
        structure = self.rule("structure", "tier: D\nfamily: modules\n")
        read = self.rule("read")
        listed = self.rule("listed", "tier: B\nfamily: names\npre_filter: value\n")
        self.sync()
        worktree = self.root / "modules"
        _ = self.git("worktree", "add", "-q", "-b", "demo-style-modules", str(worktree))
        plan = worktree / "docs/style-production/units/modules-unit.md"
        plan.parent.mkdir(parents=True)
        _ = plan.write_text(self.plan_header())
        self.assertIsInstance(style_plan.next_phase("modules-unit", plan, self.repo), style_plan.PhaseWritten)
        text = plan.read_text()
        instructions = (
            "Judge file by file", "sibling files in the same module", "direct callers",
            "every distinct defect as its own finding", "never stop at a file's first finding",
            "spans files is held once", "several rules name is held once",
            "as soon as that file has been judged",
        )
        for phrase in instructions:
            self.assertIn(phrase, text)
        self.assertIn("Read only these pinned rule texts.", text)
        self.assertIn("#uncovered rule is the parent rule's uncovered read.", text)
        ledger = self.ledger()
        self.assertEqual(ledger.ticket_open("modules-unit")[0]["rules"], [structure])
        issue = self.root / "approved.md"
        blob = next(rule.blob for rule in rules.load_rules(self.repo) if rule.rule_id == read)
        finding = ledger.hold(read, self.sources[0], 1, "value", "type", "u32", "u64", blob, "outside")
        ledger.file_held(finding.finding_id, issue)
        kinds: tuple[style_ledger.TicketKind, ...] = ("read", "listed", "proposal")
        for number, kind in enumerate(kinds, 1):
            with self.subTest(kind=kind):
                rule = listed if kind == "listed" else read
                ticket_id = f"names-unit-{number}"
                ledger.ticket_add(ticket_id, "names-unit", kind, "names", "crates/alpha", [self.sources[0]],
                                  [rule], rule=rule if kind == "listed" else "",
                                  proposal=str(issue) if kind == "proposal" else "",
                                  finding_ids=(finding.finding_id,) if kind == "proposal" else ())
                ticket = ledger.queued_tickets("names-unit")[-1]
                phase = style_plan.phase_text(ticket, ledger.pinned_rule_texts(ticket_id), self.plan,
                                              style_plan.delegation_context(self.plan.read_text()),
                                              file_windows={self.sources[0]: (style_plan.ReadWindow(1, 1),)})
                self.assertIn("Read only these pinned rule texts.", phase)
                self.assertIn("#uncovered rule is the parent rule's uncovered read.", phase)
                self.assertNotIn("For a structure ticket, judge", phase)
                for phrase in instructions:
                    self.assertNotIn(phrase, phase)
        legacy = "\n".join(line for line in text.splitlines()
                           if not any(phrase in line for phrase in instructions)) + "\n"
        legacy = legacy.replace("Read only these pinned rule texts.",
                                "Read only these pinned rule texts. For a structure ticket, judge each rule's "
                                + "listed sites and its uncovered surface together; read the uncovered instructions in the pinned text.")
        _ = plan.write_text(legacy)
        self.assertEqual(style_plan.next_phase("modules-unit", plan, self.repo), style_plan.Resumed("modules-unit-1"))
        self.assertEqual(plan.read_text(), text)
        self.assertEqual(len(ledger.ticket_open("modules-unit")), 1)

    def test_listed_packing_keeps_whole_files_and_selects_remaining_file_after_judgment(self) -> None:
        for path, count in zip(self.sources, (100, 100, 151, 0), strict=True):
            self.write(path, "pub fn hit() {}\n" * count)
        self.tip = self.commit()
        rule = self.rule("listed", "tier: B\nfamily: names\npre_filter: hit\n")
        self.sync()
        tickets = self.candidates()
        self.assertEqual([len(ticket.sites) for ticket in tickets], [100, 100, 151])
        self.assertEqual([ticket.files for ticket in tickets], [(path,) for path in self.sources[:3]])
        self.record(rule, self.sources[0], self.tip)
        self.assertEqual([ticket.files for ticket in self.candidates()], [(path,) for path in self.sources[1:3]])
        self.assertFalse(next(
            progress for progress in self.ledger().rule_counts(style_ledger.WholeRepository())
            if progress.rule_id == rule
        ).complete)
        for path in self.sources[1:3]:
            self.record(rule, path, self.tip)
        self.assertEqual(self.candidates(), ())
        self.assertTrue(next(
            progress for progress in self.ledger().rule_counts(style_ledger.WholeRepository())
            if progress.rule_id == rule
        ).complete)

    def test_awaiting_merge_is_skipped_until_later_style_edit_makes_it_stale(self) -> None:
        rule = self.rule("read")
        self.sync()
        path = self.sources[0]
        _ = (self.style / path).write_text("pub fn judged() {}\n")
        _ = self.git("add", "--all", cwd=self.style)
        _ = self.git("commit", "-qm", "Judged", cwd=self.style)
        self.record(rule, path, self.git("rev-parse", "HEAD", cwd=self.style))
        self.assertNotIn(path, {file for ticket in self.candidates() for file in ticket.files})
        self.assertEqual(next(cell.state for cell in self.ledger().cells() if cell.path == path), "awaiting merge")
        _ = (self.style / path).write_text("pub fn later_edit() {}\n")
        _ = self.git("add", "--all", cwd=self.style)
        _ = self.git("commit", "-qm", "Later edit", cwd=self.style)
        self.assertIn(path, {file for ticket in self.candidates() for file in ticket.files})
        self.assertEqual(next(cell.state for cell in self.ledger().cells() if cell.path == path), "stale")

    def test_read_tickets_separate_name_bindings_from_other_names_rules_on_each_pending_file(self) -> None:
        bindings = self.rule("name-bindings-to-match-parameters")
        other = self.rule("other-names")
        self.sync()
        self.record(bindings, self.sources[2], self.tip)
        self.record(other, self.sources[3], self.tip)
        self.assertEqual(self.candidates(), (
            style_plan.TicketSelection("read", "names", "crates/alpha", self.sources[:2], (bindings,)),
            style_plan.TicketSelection("read", "names", "crates/alpha", self.sources[:2], (other,)),
            style_plan.TicketSelection("read", "names", "crates/beta", (self.sources[3],), (bindings,)),
            style_plan.TicketSelection("read", "names", "crates/beta", (self.sources[2],), (other,)),
        ))

    def test_read_tickets_separate_uncovered_name_bindings_from_other_names_rules(self) -> None:
        bindings = self.rule("name-bindings-to-match-parameters", "tier: B\nfamily: names\ncandidates:\n"
                             + "  kind: regex\n  pattern: absent\n  surface: partial\n  uncovered: Read every binding.\n")
        other = self.rule("other-names")
        self.sync()
        self.assertEqual(self.candidates(), (
            style_plan.TicketSelection("read", "names", "crates/alpha", self.sources[:2], (bindings + "#uncovered",)),
            style_plan.TicketSelection("read", "names", "crates/alpha", self.sources[:2], (other,)),
            style_plan.TicketSelection("read", "names", "crates/beta", self.sources[2:], (bindings + "#uncovered",)),
            style_plan.TicketSelection("read", "names", "crates/beta", self.sources[2:], (other,)),
        ))

    def test_read_tickets_without_name_bindings_keep_rules_grouped_on_their_pending_files(self) -> None:
        first = self.rule("first-types", "tier: C\nfamily: types\ninput_scope: file\n")
        second = self.rule("second-types", "tier: C\nfamily: types\ninput_scope: file\n")
        self.sync()
        self.record(second, self.sources[0], self.tip)
        self.assertEqual(self.candidates("types"), (
            style_plan.TicketSelection("read", "types", "crates/alpha", (self.sources[0],), (first,)),
            style_plan.TicketSelection("read", "types", "crates/alpha", (self.sources[1],), (first, second)),
            style_plan.TicketSelection("read", "types", "crates/beta", self.sources[2:], (first, second)),
        ))

    def test_partial_b_reads_uncovered_and_zero_sites_has_no_listed_ticket(self) -> None:
        rule = self.rule("partial", "tier: B\nfamily: names\ncandidates:\n  kind: regex\n  pattern: absent\n  surface: partial\n  uncovered: Read everything else.\n")
        self.sync()
        tickets = self.candidates()
        self.assertEqual({ticket.kind for ticket in tickets}, {"read"})
        self.assertEqual({rule_id for ticket in tickets for rule_id in ticket.rules}, {rule + "#uncovered"})
        self.assertEqual({path for ticket in tickets for path in ticket.files}, set(self.sources))

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

    def sync_wait_rows(self) -> list[tuple[str, str]]:
        with closing(sqlite3.connect(self.ledger().database)) as connection:
            return cast(list[tuple[str, str]], connection.execute(
                "SELECT unit,since FROM sync_waits ORDER BY since,unit",
            ).fetchall())

    def seed_sync_wait(self) -> None:
        with closing(sqlite3.connect(self.ledger().database)) as connection:
            _ = connection.execute("INSERT OR IGNORE INTO sync_waits (unit,since,token) VALUES ('names-unit','2026-01-01T00:00:00+00:00','seed-wait')")
            connection.commit()

    def test_refill_waits_without_selecting_work_when_the_branch_point_is_unsynced(self) -> None:
        _ = self.rule("read")
        self.sync()
        before = self.plan.read_bytes()
        self.write("README.md", "Branch point moved without a sync notice.\n")
        branch_tip = self.commit()
        with mock.patch.object(berth_board, "read_board", side_effect=AssertionError("board read before sync")):
            self.assertEqual(self.run_next(), (
                3, f"waiting for sync: main {branch_tip[:7]} is not in the synced tip {self.tip[:7]}\n", ""))
        self.assertEqual(self.plan.read_bytes(), before)
        self.assertEqual(self.count_tickets(), 0)
        self.assertEqual([row[0] for row in self.sync_wait_rows()], ["names-unit"])

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

        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))

        self.assertEqual(self.count_tickets(), 1)
        self.assertEqual(self.sync_wait_rows(), [])

    def test_refill_waits_for_sync_after_the_local_branch_point_merge_is_pushed(self) -> None:
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
        branch_tip = self.git("rev-parse", "HEAD")
        _ = self.git("push", "-q", "origin", "main")
        before = self.plan.read_bytes()

        self.assertEqual(self.run_next(), (
            3, f"waiting for sync: main {branch_tip[:7]} is not in the synced tip {self.tip[:7]}\n", ""))

        self.assertEqual(self.count_tickets(), 0)
        self.assertEqual(self.plan.read_bytes(), before)
        self.assertEqual([row[0] for row in self.sync_wait_rows()], ["names-unit"])

    def test_refill_selects_work_after_sync_merges_the_branch_point_into_a_distinct_style_tip(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.write("README.md", "Incoming branch-point work.\n")
        branch_tip = self.commit()
        self.assertEqual(self.run_next(), (
            3, f"waiting for sync: main {branch_tip[:7]} is not in the synced tip {self.tip[:7]}\n", ""))
        _ = self.git("merge", "--no-ff", "-qm", "Sync branch point", "main", cwd=self.style)
        self.tip = self.git("rev-parse", "HEAD", cwd=self.style)
        self.assertNotEqual(self.tip, branch_tip)
        self.sync()
        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))
        self.assertEqual(self.count_tickets(), 1)
        self.assertEqual(self.sync_wait_rows(), [])

    def test_assigned_ticket_resumes_unchanged_while_the_branch_point_is_unsynced(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))
        before = self.plan.read_bytes()
        ticket = self.ledger().ticket_open("names-unit")[0]
        self.write("README.md", "Branch point moved during an assigned ticket.\n")
        _ = self.commit()
        with mock.patch.object(berth_board, "read_board", side_effect=AssertionError("board read on resume")):
            self.assertEqual(self.run_next(), (0, "resume names-unit-1\n", ""))
        self.assertEqual(self.plan.read_bytes(), before)
        self.assertEqual(self.ledger().ticket_open("names-unit")[0], ticket)
        self.assertEqual(self.count_tickets(), 1)
        self.assertEqual(self.sync_wait_rows(), [])

    def test_sync_wait_is_recorded_until_structure_finishes_and_refill_writes_a_phase(self) -> None:
        structure = self.rule("structure", "tier: D\nfamily: modules\n")
        _ = self.rule("read")
        self.sync()
        with mock.patch.object(style_ledger, "_now", return_value="2026-01-02T03:04:05+00:00"):
            self.assertEqual(self.run_next(), (3, "waiting for structure: crates/alpha, crates/beta\n", ""))
        self.assertEqual(self.sync_wait_rows(), [("names-unit", "2026-01-02T03:04:05+00:00")])
        with mock.patch.object(style_ledger, "_now", return_value="2026-01-03T03:04:05+00:00"):
            self.assertEqual(self.run_next()[0], 3)
        self.assertEqual(self.sync_wait_rows(), [("names-unit", "2026-01-02T03:04:05+00:00")])
        for path in self.sources[:2]:
            self.record(structure, path, self.tip)
        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))
        self.assertEqual(self.ledger().ticket_open("names-unit")[0]["crate"], "crates/alpha")
        self.assertEqual(self.sync_wait_rows(), [])

    def test_names_refill_names_pending_structure_crates_and_preserves_unclassified_warning(self) -> None:
        _ = self.rule("structure", "tier: D\nfamily: modules\n")
        _ = self.rule("read")
        _ = self.rule("legacy", "")
        self.sync()
        before = self.plan.read_bytes()

        self.assertEqual(self.run_next(), (
            3, "waiting for structure: crates/alpha, crates/beta\n",
            "unclassified: 1 rules — pull nate_style\n"))
        self.assertEqual(self.plan.read_bytes(), before)
        self.assertEqual(self.count_tickets(), 0)
        self.assertEqual([row[0] for row in self.sync_wait_rows()], ["names-unit"])

    def test_names_refill_waits_for_sync_when_only_structure_cells_are_pending_in_scope(self) -> None:
        _ = self.rule("structure", "tier: D\nfamily: modules\n")
        read = self.rule("read")
        self.sync()
        _ = self.plan.write_text(self.plan_header().replace("## Phases", "**Pilot scope:** alpha\n\n## Phases"))
        for path in self.sources[:2]:
            self.record(read, path, self.tip)

        self.assertEqual(self.run_next(), (3, "waiting for sync\n", ""))
        self.assertEqual(self.count_tickets(), 0)
        self.assertEqual([row[0] for row in self.sync_wait_rows()], ["names-unit"])

    def test_names_structure_wait_excludes_crates_outside_its_pilot_scope(self) -> None:
        _ = self.rule("structure", "tier: D\nfamily: modules\n")
        _ = self.rule("read")
        self.sync()
        _ = self.plan.write_text(self.plan_header().replace("## Phases", "**Pilot scope:** beta\n\n## Phases"))

        self.assertEqual(self.run_next(), (3, "waiting for structure: crates/beta\n", ""))
        self.assertEqual(self.count_tickets(), 0)

    def test_modules_refill_with_completed_structure_waits_for_sync_despite_pending_names_cells(self) -> None:
        structure = self.rule("structure", "tier: D\nfamily: modules\n")
        _ = self.rule("read")
        self.sync()
        for path in self.sources:
            self.record(structure, path, self.tip)
        worktree = self.root / "modules"
        _ = self.git("worktree", "add", "-q", "-b", "demo-style-modules", str(worktree))
        plan = worktree / "docs/style-production/units/modules-unit.md"
        plan.parent.mkdir(parents=True)
        _ = plan.write_text(self.plan_header())
        output = io.StringIO()
        with redirect_stdout(output):
            code = style_plan.main(["next-phase", "--unit", "modules-unit", "--plan", str(plan), "--repo", str(worktree)])

        self.assertEqual((code, output.getvalue()), (3, "waiting for sync\n"))
        self.assertEqual(self.count_tickets(), 0)
        self.assertEqual([row[0] for row in self.sync_wait_rows()], ["modules-unit"])

    def test_structure_wait_uses_pending_crates_from_the_second_selection_pass(self) -> None:
        _ = self.rule("structure", "tier: D\nfamily: modules\n")
        read = self.rule("read")
        self.sync()
        reads = 0

        def board_after_judgment(repo: Path) -> berth_board.Board:
            nonlocal reads
            self.assertEqual(repo, self.repo)
            reads += 1
            if reads == 2:
                self.assertEqual([row[0] for row in self.sync_wait_rows()], ["names-unit"])
                for path in self.sources[:2]:
                    self.record(read, path, self.tip)
            return self.board()

        with mock.patch.object(berth_board, "read_board", side_effect=board_after_judgment):
            self.assertEqual(self.run_next(), (3, "waiting for structure: crates/beta\n", ""))
        self.assertEqual(reads, 2)

    def test_refill_rechecks_after_registering_a_wait_and_finds_the_just_merged_crate(self) -> None:
        structure = self.rule("structure", "tier: D\nfamily: modules\n")
        _ = self.rule("read")
        self.sync()
        reads = 0

        def board_after_merge(repo: Path) -> berth_board.Board:
            nonlocal reads
            self.assertEqual(repo, self.repo)
            reads += 1
            if reads == 2:
                self.assertEqual([row[0] for row in self.sync_wait_rows()], ["names-unit"])
                for path in self.sources[:2]:
                    self.record(structure, path, self.tip)
            return self.board()

        with mock.patch.object(berth_board, "read_board", side_effect=board_after_merge):
            self.assertEqual(style_plan.next_phase("names-unit", self.plan, self.repo),
                             style_plan.PhaseWritten("names-unit-1"))
        self.assertEqual(reads, 2)
        self.assertEqual(self.ledger().ticket_open("names-unit")[0]["crate"], "crates/alpha")
        self.assertEqual(self.sync_wait_rows(), [])

    def test_refill_checks_twice_before_sleeping_and_drops_a_busy_ticket_only_once(self) -> None:
        _ = self.rule("structure", "tier: D\nfamily: modules\n")
        read = self.rule("read")
        self.sync()
        ledger = self.ledger()
        ledger.ticket_add("names-unit-1", "names-unit", "read", "names", "crates/alpha",
                          list(self.sources[:2]), [read])
        output = io.StringIO()
        with mock.patch.object(berth_board, "read_board", return_value=self.board(self.sources[0])) as board, redirect_stdout(output):
            result = style_plan.next_phase("names-unit", self.plan, self.repo)
        self.assertEqual(result, style_plan.WaitingForStructure(("crates/alpha", "crates/beta"), 0))
        self.assertEqual(board.call_count, 2)
        self.assertEqual(output.getvalue(), "dropped names-unit-1: files busy\n")
        self.assertEqual([row[0] for row in self.sync_wait_rows()], ["names-unit"])
        self.assertEqual(ledger.queued_tickets("names-unit"), ())

    def test_board_failure_after_registering_a_wait_preserves_it_for_a_later_wake(self) -> None:
        _ = self.rule("structure", "tier: D\nfamily: modules\n")
        _ = self.rule("read")
        self.sync()
        unavailable = berth_board.BoardUnavailable("second read failed")
        with mock.patch.object(berth_board, "read_board", side_effect=[self.board(), unavailable]):
            self.assertEqual(style_plan.next_phase("names-unit", self.plan, self.repo), unavailable)
        self.assertEqual([row[0] for row in self.sync_wait_rows()], ["names-unit"])
        self.assertEqual(self.ledger().ticket_open("names-unit"), ())

    def test_resuming_an_assigned_ticket_clears_a_persisted_sync_wait(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.assertEqual(self.run_next()[0], 0)
        self.seed_sync_wait()
        self.assertEqual(self.run_next(), (0, "resume names-unit-1\n", ""))
        self.assertEqual(self.sync_wait_rows(), [])

    def test_repair_board_and_merge_waits_leave_the_sync_wait_unchanged(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.seed_sync_wait()
        expected = [("names-unit", "2026-01-01T00:00:00+00:00")]
        original = self.plan.read_text()
        _ = self.plan.write_text(original + "\n### Phase 3 — repair round 1 · status: todo\nRepair.\n")
        self.assertEqual(self.run_next(), (0, "repair first\n", ""))
        self.assertEqual(self.sync_wait_rows(), expected)
        _ = self.plan.write_text(original)
        with mock.patch.object(berth_board, "read_board", return_value=berth_board.BoardUnavailable("unreadable")):
            self.assertEqual(self.run_next()[0], 4)
        self.assertEqual(self.sync_wait_rows(), expected)
        self.assertEqual(self.run_next()[0], 0)
        ticket = self.ledger().ticket_open("names-unit")[0]
        for rule in ticket["rules"]:
            for path in ticket["files"]:
                self.record(rule, path, self.tip, ticket["id"])
        self.ledger().ticket_checkpoint(ticket["id"], self.tip)
        self.seed_sync_wait()
        self.assertEqual(self.run_next()[0], 5)
        self.assertEqual(self.sync_wait_rows(), expected)

    def test_legacy_only_waits_with_action_and_mixed_catalog_selects_only_classified_rules(self) -> None:
        _ = self.rule("legacy", "")
        self.sync()
        self.assertEqual(self.run_next(), (3, "waiting for sync\n", "unclassified: 1 rules — pull nate_style\n"))
        read = self.rule("read")
        self.sync()
        self.assertEqual({rule for ticket in self.candidates() for rule in ticket.rules}, {read})
        self.assertEqual(self.run_next()[0], 0)

    def test_stalest_crate_is_first_and_read_tickets_cover_only_pending_cells(self) -> None:
        first = self.rule("first")
        second = self.rule("second")
        self.sync()
        self.record(first, self.sources[2], self.tip)
        self.record(second, self.sources[0], self.tip)
        self.write(self.sources[2], "pub fn revised() {}\n")
        self.tip = self.commit()
        self.sync()
        self.unit_takes_tip()
        tickets = self.candidates()
        self.assertEqual(tickets[0].crate, "crates/beta")
        states = {(cell.rule, cell.path): cell.state for cell in self.ledger().cells()}
        for ticket in tickets:
            for rule in ticket.rules:
                for path in ticket.files:
                    self.assertIn(states[rule, path], ("open", "stale"))
        self.assertNotIn((second, self.sources[0]), {(rule, path) for ticket in tickets for rule in ticket.rules for path in ticket.files})

    def test_completed_classified_catalog_waits_for_sync_without_creating_work(self) -> None:
        rule = self.rule("read")
        self.sync()
        for path in self.sources:
            self.record(rule, path, self.tip)
        before = self.plan.read_bytes()
        self.assertEqual(self.run_next(), (3, "waiting for sync\n", ""))
        self.assertEqual(self.plan.read_bytes(), before)
        self.assertEqual(self.count_tickets(), 0)

    def test_member_rule_and_global_same_stem_have_separate_ids_and_reach(self) -> None:
        global_rule = self.rule("same")
        path = "docs/alpha/style/same.md"
        self.write(path, "---\ntier: C\nfamily: names\ninput_scope: file\n---\nLocal instructions.\n")
        self.tip = self.commit()
        self.sync()
        tickets = self.candidates()
        member = "local:" + path
        alpha = [ticket for ticket in tickets if ticket.crate == "crates/alpha"]
        beta = [ticket for ticket in tickets if ticket.crate == "crates/beta"]
        self.assertEqual({rule for ticket in alpha for rule in ticket.rules}, {global_rule, member})
        self.assertEqual({rule for ticket in beta for rule in ticket.rules}, {global_rule})

    def test_prefilter_word_and_regex_shell_command_create_non_rust_listed_tickets(self) -> None:
        self.write("scripts/test.sh", "#!/bin/sh\ncargo test -p sample # forbidden_marker\n")
        self.tip = self.commit()
        word = self.rule("words", "tier: B\nfamily: names\npre_filter: forbidden_marker\n")
        shell = self.rule("shell", "tier: B\nfamily: names\ncandidates:\n  kind: regex\n  pattern: cargo test\n  globs: [scripts/*.sh]\n  surface: whole\n")
        self.sync()
        tickets = self.candidates()
        self.assertEqual({(ticket.rule, ticket.files) for ticket in tickets}, {(word, ("scripts/test.sh",)), (shell, ("scripts/test.sh",))})

    def test_refill_uses_the_units_plan_and_records_branch_and_pinned_rule_paths(self) -> None:
        rule = self.rule("read")
        self.sync()
        _ = self.plan.write_text(self.plan.read_text() + "\n## Notes\nKeep this section.\n")
        showrunner = self.style / "docs/style-production/units/names-unit.md"
        before = showrunner.read_bytes()
        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))
        ticket = self.ledger().ticket_open("names-unit")[0]
        self.assertEqual(ticket["branch"], "refs/heads/demo-style-names")
        self.assertEqual(ticket["rules"], [rule])
        pinned = self.ledger().pinned_rule_texts(ticket["id"])[0]
        rule_file = self.plan.parent.parent / "rules" / (pinned.blob + ".md")
        self.assertEqual(rule_file.read_bytes(), (self.rule_dir / "read.md").read_bytes())
        self.assertIn(f"`{rule_file}`", self.plan.read_text())
        self.assertIn(f"blob `{ticket['rule_blobs'][rule]}`", self.plan.read_text())
        self.assertLess(self.plan.read_text().index("### Phase 1"), self.plan.read_text().index("## Notes"))
        self.assertTrue(self.plan.read_text().endswith("## Notes\nKeep this section.\n"))
        self.assertEqual(showrunner.read_bytes(), before)
        self.assertFalse((showrunner.parent.parent / "rules").exists())
        content = self.plan.read_bytes()
        self.assertEqual(self.run_next(), (0, "resume names-unit-1\n", ""))
        self.assertEqual(self.plan.read_bytes(), content)
        self.assertEqual(self.count_tickets(), 1)

    def test_each_interruption_rebuilds_the_uninterrupted_phase_byte_for_byte(self) -> None:
        _ = self.rule("read")
        self.sync()
        database = self.ledger().database
        snapshot = database.read_bytes()
        header = self.plan.read_bytes()
        self.assertEqual(self.run_next()[0], 0)
        uninterrupted = self.plan.read_bytes()
        pinned_dir = self.plan.parent.parent / "rules"
        for boundary in ("added", "assigned", "pinned"):
            with self.subTest(boundary=boundary):
                _ = database.write_bytes(snapshot)
                _ = self.plan.write_bytes(header)
                shutil.rmtree(pinned_dir)
                if boundary == "added":
                    with mock.patch.object(style_ledger.Ledger, "ticket_assign", side_effect=RuntimeError("interruption")):
                        with self.assertRaisesRegex(RuntimeError, "interruption"):
                            _ = style_plan.next_phase("names-unit", self.plan, self.repo)
                elif boundary == "assigned":
                    with mock.patch.object(style_plan, "write_phase", side_effect=RuntimeError("interruption")):
                        with self.assertRaisesRegex(RuntimeError, "interruption"):
                            _ = style_plan.next_phase("names-unit", self.plan, self.repo)
                else:
                    real_write = style_plan.atomic_write
                    def fail_plan(path: Path, data: bytes) -> None:
                        if path == self.plan:
                            raise RuntimeError("interruption")
                        return real_write(path, data)
                    with mock.patch.object(style_plan, "atomic_write", fail_plan):
                        with self.assertRaisesRegex(RuntimeError, "interruption"):
                            _ = style_plan.next_phase("names-unit", self.plan, self.repo)
                    self.assertTrue(pinned_dir.exists())
                self.assertEqual(self.run_next()[0], 0)
                self.assertEqual(self.plan.read_bytes(), uninterrupted)
                self.assertEqual(self.count_tickets(), 1)
                self.assertEqual(self.ledger().ticket_open("names-unit")[0]["branch"], "refs/heads/demo-style-names")

    def test_resume_restores_a_truncated_pin_without_changing_the_phase(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.assertEqual(self.run_next()[0], 0)
        expected = self.plan.read_bytes()
        pinned = self.ledger().pinned_rule_texts("names-unit-1")[0]
        path = self.plan.parent.parent / "rules" / (pinned.blob + ".md")
        _ = path.write_bytes(pinned.text[:len(pinned.text) // 2])
        self.assertEqual(self.run_next(), (0, "resume names-unit-1\n", ""))
        self.assertEqual(path.read_bytes(), pinned.text)
        self.assertEqual(self.git("hash-object", str(path)), pinned.blob)
        self.assertEqual(self.plan.read_bytes(), expected)
        self.assertEqual(self.count_tickets(), 1)

    def test_resume_restores_a_truncated_phase_byte_for_byte(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.assertEqual(self.run_next()[0], 0)
        expected = self.plan.read_bytes()
        for cut in (len(expected) // 2, expected.index(b"status: todo") + len(b"status: todo")):
            with self.subTest(cut=cut):
                _ = self.plan.write_bytes(expected[:cut])
                self.assertEqual(self.run_next(), (0, "resume names-unit-1\n", ""))
                self.assertEqual(self.plan.read_bytes(), expected)
        self.assertEqual(self.count_tickets(), 1)

    def test_interrupted_replacement_preserves_final_pin_and_plan_files(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.assertEqual(self.run_next()[0], 0)
        expected = self.plan.read_bytes()
        pinned = self.ledger().pinned_rule_texts("names-unit-1")[0]
        pin = self.plan.parent.parent / "rules" / (pinned.blob + ".md")
        for target in (pin, self.plan):
            with self.subTest(target=target):
                before = target.read_bytes()
                with mock.patch.object(os, "replace", side_effect=OSError("interrupted replacement")):
                    with self.assertRaisesRegex(OSError, "interrupted replacement"):
                        style_plan.atomic_write(target, b"incomplete")
                self.assertEqual(target.read_bytes(), before)
                self.assertEqual(tuple(target.parent.glob(f".{target.name}.*")), ())
        self.assertEqual(self.plan.read_bytes(), expected)

    def test_decision_ticket_text_separates_rule_findings_from_candidate_ideas(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.assertEqual(self.run_next()[0], 0)
        text = self.plan.read_text()
        self.assertIn("Hold a finding only when the cited rule's text requires the change.", text)
        self.assertIn('Record other ideas with `style_ledger.py candidate --ticket names-unit-1 --site <path>:<line> --idea "<one line>"`.', text)

    def test_decision_gate_refuses_an_answer_declined_after_ticket_creation(self) -> None:
        ticket = self.approved_reference_ticket()
        origin = self.ledger().ticket_origin(ticket)
        assert isinstance(origin, style_ledger.ApprovedProposal)
        _ = origin.issue.write_text(origin.issue.read_text().replace("status: open", "status: open\nstyle_answer: declined\nstyle_answer_words: No"))
        code, message = self.reference_gate(ticket)
        self.assertEqual(code, 1)
        self.assertIn("approval", message)

    def test_decision_gate_uses_ticket_findings_not_other_findings_of_the_issue(self) -> None:
        ticket = self.approved_reference_ticket()
        ledger = self.ledger()
        origin = ledger.ticket_origin(ticket)
        assert isinstance(origin, style_ledger.ApprovedProposal)
        first = ledger.proposal_findings(origin.issue)[0]
        other = ledger.hold(first.rule, self.sources[1], 1, "another change", "type", "u32", "u64", first.rule_blob, "outside")
        ledger.file_held(other.finding_id, origin.issue)
        self.write(self.sources[1], "pub fn changed_without_this_ticket() {}\n")
        self.assertEqual(self.reference_gate(ticket)[0], 1)
        self.write(self.sources[1], "pub fn value() {}\n")
        self.write(self.sources[0], "pub fn approved_change() {}\nenum Outcome { NewName }\n")
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_decision_gate_refuses_until_each_approved_rename_is_applied(self) -> None:
        ticket = self.approved_reference_ticket()
        origin = self.ledger().ticket_origin(ticket)
        assert isinstance(origin, style_ledger.ApprovedProposal)
        finding_id, = origin.finding_ids
        self.write(self.sources[0], "enum Outcome { OldName }\n")
        self.assertEqual(self.reference_gate(ticket), (1, f"review: {finding_id}: Outcome::OldName -> Outcome::NewName "
                                                          + f"not applied in {self.sources[0]}\n"))
        self.write(self.sources[0], "enum Outcome { NewName }\n")
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_decision_refill_slices_forty_restructure_files_and_releases_dropped_slice(self) -> None:
        rule = self.rule("decision", "tier: C\nfamily: names\ninput_scope: file\nmode: propose\n")
        files = tuple(f"crates/alpha/src/piece_{index:02}.rs" for index in range(40))
        for path in files:
            self.write(path, "fn work() {}\n")
        self.tip = self.commit()
        _ = self.git("merge", "--ff-only", "main", cwd=self.unit)
        _ = self.git("merge", "--ff-only", "main", cwd=self.style)
        self.sync()
        folder = self.root / "issues"
        folder.mkdir()
        issue = folder / "decision.md"
        ledger = self.ledger()
        blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule)
        for path in files:
            finding = ledger.hold(rule, path, 1, "split", "restructure", "before", "after", blob, "outside")
            ledger.file_held(finding.finding_id, issue)
        _ = issue.write_text("---\nstatus: open\ntags: [proposal, style]\nunit: names-unit\nplan: approved\n"
                             + f"style_rule: {rule}\nstyle_crate: crates/alpha\nstyle_rule_blob: {blob}\n---\n")
        _ = self.doc.write_text(self.doc.read_text().replace("## Units", f"- **Proposals:** {folder}\n\n## Units"))
        first_slice = style_plan.read_slices(self.repo, self.tip, files, style_plan.BatchLimits(6, 3000, 5))[0]
        with (mock.patch.object(berth_board, "read_board", return_value=self.board(*first_slice)),
              mock.patch.object(style_proposals, "proposal_command")):
            code, _, errors = self.run_next()
        self.assertEqual(code, 0, errors)
        first_ticket = ledger.ticket_open("names-unit")[0]
        self.assertLessEqual(len(first_ticket["files"]), 6)
        self.assertFalse(set(first_ticket["files"]) & set(first_slice))
        ledger.ticket_drop(first_ticket["id"])
        self.assertTrue(all(isinstance(finding.build_ticket, style_ledger.NoBuildTicket)
                            for finding in ledger.proposal_findings(issue)))
        built: set[str] = set()
        with mock.patch.object(style_proposals, "proposal_command"):
            for _ in range(40):
                code, _, errors = self.run_next()
                self.assertEqual(code, 0, errors)
                ticket = ledger.ticket_open("names-unit")[0]
                self.assertEqual(ticket["kind"], "proposal")
                self.assertLessEqual(len(ticket["files"]), 6)
                self.assertFalse(built & set(ticket["files"]))
                built.update(ticket["files"])
                ledger.ticket_checkpoint(ticket["id"], self.tip)
                not_built = ledger.ticket_merge(ticket["id"])
                self.assertEqual(not_built, tuple(style_ledger.FindingNotBuilt(identity, approved_renames.NoAppliedCheck("restructure"))
                                                  for identity in sorted(ticket["finding_ids"])))
                for identity in ticket["finding_ids"]:
                    ledger.mark_built(identity)
                if len(built) == 40:
                    break
        self.assertEqual(built, set(files))
        self.assertTrue(all(isinstance(finding.state, style_ledger.Built) for finding in ledger.proposal_findings(issue)))

    def approved_decision(self, rule: str, files: tuple[str, ...], kind: style_ledger.FindingKind) -> Path:
        folder = self.root / "issues"
        folder.mkdir(exist_ok=True)
        issue = folder / "decision.md"
        with style_ledger.open_ledger(self.repo) as ledger:
            blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule)
            for path in files:
                finding = ledger.hold(rule, path, 1, "change", kind, "Outcome::Old", "Outcome::New", blob, "outside")
                ledger.file_held(finding.finding_id, issue)
        _ = issue.write_text("---\nstatus: open\ntags: [proposal, style]\nunit: names-unit\nplan: approved\n"
                             + f"style_rule: {rule}\nstyle_crate: crates/alpha\nstyle_rule_blob: {blob}\n---\n")
        _ = self.doc.write_text(self.doc.read_text().replace("## Units", f"- **Proposals:** {folder}\n\n## Units"))
        return issue

    def commit_to_every_checkout(self) -> None:
        self.tip = self.commit()
        for checkout in (self.style, self.unit):
            _ = self.git("merge", "--ff-only", "main", cwd=checkout)
        self.sync()

    def test_decision_refill_makes_two_read_limit_tickets_for_twenty_six_type_files(self) -> None:
        rule = self.rule("types-decision")
        files = tuple(f"crates/alpha/src/piece_{index:02}.rs" for index in range(26))
        for path in files:
            self.write(path, "fn work() -> u32 { 1 }\n")
        self.commit_to_every_checkout()
        issue = self.approved_decision(rule, files, "type")
        expected = style_plan.read_slices(self.repo, self.tip, files, style_plan.BatchLimits(25, 5000, 10))
        self.assertEqual(len(expected), 2)
        ledger = self.ledger()
        tickets: list[list[str]] = []
        with mock.patch.object(style_proposals, "proposal_command"):
            for _ in expected:
                code, _, errors = self.run_next()
                self.assertEqual(code, 0, errors)
                ticket = ledger.ticket_open("names-unit")[0]
                self.assertEqual(ticket["kind"], "proposal")
                tickets.append(ticket["files"])
                ledger.ticket_checkpoint(ticket["id"], self.tip)
                _ = ledger.ticket_merge(ticket["id"])
                for identity in ticket["finding_ids"]:
                    ledger.mark_built(identity)
            self.assertEqual(self.run_next()[0], 0)
        self.assertEqual(sorted(tuple(files) for files in tickets), sorted(expected))
        self.assertEqual(len(ledger.proposal_tickets()), 2)
        self.assertTrue(all(isinstance(finding.state, style_ledger.Built) for finding in ledger.proposal_findings(issue)))

    def test_decision_refill_skips_a_rule_that_left_the_catalog_and_coverage_goes_on(self) -> None:
        gone = self.rule("gone-decision")
        _ = self.rule("read")
        self.sync()
        issue = self.approved_decision(gone, (self.sources[0],), "rename")
        (self.rule_dir / "gone-decision.md").unlink()
        with mock.patch.object(style_proposals, "proposal_command") as start:
            code, output, errors = self.run_next()
        self.assertEqual(code, 0, errors)
        self.assertEqual(output.count(f"waiting {issue}: rule {gone} not in the catalog\n"), 1)
        start.assert_not_called()
        self.assertEqual(self.ledger().ticket_open("names-unit")[0]["kind"], "read")

    def test_decision_refill_waives_a_finding_whose_file_is_gone_so_the_decision_can_close(self) -> None:
        rule = self.rule("read")
        self.sync()
        issue = self.approved_decision(rule, self.sources[:2], "rename")
        gone = next(finding for finding in self.ledger().proposal_findings(issue) if finding.path == self.sources[1])
        _ = self.git("rm", "-q", self.sources[1])
        self.write(self.sources[0], "pub enum Outcome { New }\n")
        self.commit_to_every_checkout()
        with mock.patch.object(style_proposals, "proposal_command"):
            code, output, errors = self.run_next()
        self.assertEqual(code, 0, errors)
        self.assertIn(f"waived 1 findings in {issue}: {self.sources[1]} is not in synced commit {self.tip}\n", output)
        ledger = self.ledger()
        self.assertIsInstance(next(finding.state for finding in ledger.proposal_findings(issue)
                                   if finding.finding_id == gone.finding_id), style_ledger.Waived)
        with closing(sqlite3.connect(ledger.database)) as connection:
            self.assertEqual(connection.execute("SELECT waive_provenance,waived_by FROM held_findings WHERE finding_id=?",
                                                (gone.finding_id,)).fetchone(), ("ledger", "ledger"))
        self.assertEqual(ledger.named_held((gone.finding_id,))[0].waive_actor, style_ledger.WaiveByLedger())
        ticket = ledger.ticket_open("names-unit")[0]
        self.assertEqual((ticket["kind"], ticket["files"]), ("proposal", [self.sources[0]]))
        ledger.ticket_checkpoint(ticket["id"], self.tip)
        _ = ledger.ticket_merge(ticket["id"])
        with (mock.patch.object(style_proposals, "proposal_command") as command,
              mock.patch.object(style_proposals, "renumber")):
            style_proposals.complete(ledger)
        command.assert_called_once_with("close", str(issue), "--reason", f"built in {ticket['id']}")

    def judged_findings(self, rule: str, sites: tuple[tuple[str, int, str], ...], *, kind: style_ledger.FindingKind = "type",
                        before: str = "before", after: str = "after") -> tuple[Path, tuple[style_ledger.HeldFinding, ...]]:
        """Files findings at sites in an approved decision, judged by a dropped ticket that started at the tip.

        They are type findings unless named.
        """
        issue = self.approved_decision(rule, (), "type")
        ledger = self.ledger()
        ledger.ticket_add("modules-unit-1", "modules-unit", "read", "names", "crates/alpha", sorted({site[0] for site in sites}), [rule])
        ledger.ticket_assign("modules-unit-1", "style/main")
        _ = ledger.ticket_base("modules-unit-1")
        ledger.ticket_drop("modules-unit-1")
        blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule)
        findings = tuple(ledger.hold(rule, path, line, text, kind, before, after, blob, "modules-unit-1")
                         for path, line, text in sites)
        for finding in findings:
            ledger.file_held(finding.finding_id, issue)
        return issue, findings

    def commit_files(self, files: dict[str, str | bytes]) -> None:
        for path, content in files.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            _ = target.write_bytes(content if isinstance(content, bytes) else content.encode())
        self.commit_to_every_checkout()

    def refill_after_site_merge(self, before: dict[str, str | bytes], after: dict[str, str | bytes]) -> tuple[str, style_ledger.HeldState]:
        """Judges a type finding on the first line of the first source at before, merges after and refills.

        Returns the refill output and the finding's state.
        """
        rule = self.rule("read")
        self.commit_files(before)
        site = self.sources[0]
        issue, (finding,) = self.judged_findings(rule, ((site, 1, (self.repo / site).read_text().splitlines()[0]),))
        self.commit_files(after)
        with mock.patch.object(style_proposals, "proposal_command"):
            code, output, errors = self.run_next()
        self.assertEqual(code, 0, errors)
        return output, next(held.state for held in self.ledger().proposal_findings(issue) if held.finding_id == finding.finding_id)

    def test_a_merge_that_moves_a_site_into_a_judged_file_stales_it_and_waives_the_moved_finding(self) -> None:
        rule = self.rule("read")
        target, source = self.sources[:2]
        # Macro calls name no item, so the declaration step cannot read them and the moved-site waive applies.
        moved_text, kept_text = "moved_away!(1);", "stays_here!(2);"
        self.write(source, f"{moved_text}\n{kept_text}\n")
        self.commit_to_every_checkout()
        self.record(rule, target, self.tip)
        issue, (moved, kept) = self.judged_findings(rule, ((source, 1, moved_text), (source, 2, kept_text)))
        ledger = self.ledger()
        self.assertEqual(next(cell.state for cell in ledger.cells(rule) if cell.path == target), "current")
        self.write(target, f"pub fn value() {{}}\n    {moved_text}\n")
        self.write(source, f"{kept_text}\n")
        self.commit_to_every_checkout()
        self.assertEqual(next(cell.state for cell in ledger.cells(rule) if cell.path == target), "stale")

        with mock.patch.object(style_proposals, "proposal_command"):
            code, output, errors = self.run_next()
        self.assertEqual(code, 0, errors)
        self.assertIn(f"waived 1 findings in {issue}: {source} site moved to {target} in synced commit {self.tip}\n", output)
        states = {finding.finding_id: finding.state for finding in ledger.proposal_findings(issue)}
        self.assertEqual(states, {moved.finding_id: style_ledger.Waived(), kept.finding_id: style_ledger.Filed(issue)})
        ticket = ledger.ticket_open("names-unit")[0]
        self.assertEqual((ticket["kind"], ticket["files"], ticket["finding_ids"]), ("proposal", [source], [kept.finding_id]))
        self.assertTrue(any(selection.kind == "read" and target in selection.files for selection in self.candidates()))

    def test_an_edited_site_whose_old_line_another_changed_file_always_held_stays_filed(self) -> None:
        site, other = self.sources[:2]
        output, state = self.refill_after_site_merge(
            {site: "pub count: usize,\n", other: "pub count: usize,\npub fn value() {}\n"},
            {site: "pub item_count: usize,\n", other: "pub count: usize,\npub fn other() {}\n"})
        self.assertNotIn("site moved", output)
        self.assertIsInstance(state, style_ledger.Filed)

    def test_a_site_text_that_went_to_no_other_file_stays_filed(self) -> None:
        site, other = self.sources[:2]
        output, state = self.refill_after_site_merge(
            {site: "pub fn gone() {}\npub fn value() {}\n"},
            {site: "pub fn value() {}\n", other: "pub fn changed() {}\n"})
        self.assertNotIn("site moved", output)
        self.assertIsInstance(state, style_ledger.Filed)

    def test_a_site_text_that_went_to_two_files_stays_filed(self) -> None:
        site, other = self.sources[:2]
        output, state = self.refill_after_site_merge(
            {site: "pub fn twice() {}\npub fn value() {}\n"},
            {site: "pub fn value() {}\n", other: "pub fn twice() {}\n", "crates/alpha/src/added.rs": "pub fn twice() {}\n"})
        self.assertNotIn("site moved", output)
        self.assertIsInstance(state, style_ledger.Filed)

    def test_a_latin_1_file_among_the_changed_files_still_takes_the_moved_site(self) -> None:
        site, other, notes = *self.sources[:2], "crates/alpha/notes.txt"
        output, state = self.refill_after_site_merge(
            {site: "moved!();\npub fn value() {}\n", notes: "caf\u00e9\nmoved!();\n".encode("latin-1")},
            {site: "pub fn value() {}\n", other: "moved!();\n", notes: "caf\u00e9s\nmoved!();\n".encode("latin-1")})
        self.assertIn(f"{site} site moved to {other} in synced commit {self.tip}\n", output)
        self.assertEqual(state, style_ledger.Waived())

    def write_generated_body(self, issue: Path) -> None:
        _ = issue.write_text(issue.read_text().replace("\n---\n", '\nproject: "[[sample]]"\n---\n', 1)
                             + f"{proposal_issue.GENERATED_START}\nParent: parent\n{proposal_issue.GENERATED_END}\n")

    def refill_after_declaration_move(self, after: dict[str, str | bytes], *,
                                      generated_body: bool = True) -> tuple[Path, style_ledger.HeldFinding, str]:
        """Files a type finding on a field of struct Parent in the first source, then removes that file, commits after
        and refills. With a generated body the declaration step reads the finding; without one it skips the decision.
        """
        rule = self.rule("read")
        site = self.sources[0]
        self.commit_files({site: "pub struct Parent {\n    old: u32,\n}\n"})
        issue, (finding,) = self.judged_findings(rule, ((site, 2, "old: u32,"),))
        if generated_body:
            self.write_generated_body(issue)
        _ = self.git("rm", "-q", site)
        self.commit_files(after)
        return issue, finding, self.refill_decisions()

    def refill_decisions(self) -> str:
        """Refills with the proposal commands stubbed and a GitHub root for the rule links a decision refresh writes."""
        with (mock.patch.object(style_proposals, "proposal_command"),
              mock.patch.object(style_proposals, "github_rule_root", return_value=style_proposals.GitHubRuleRoot("example/nate_style", "main"))):
            code, output, errors = self.run_next()
        self.assertEqual(code, 0, errors)
        return output

    def test_refill_uses_an_outside_follow_and_names_an_unproved_mapped_declaration(self) -> None:
        site, destination = self.sources[:2]
        source = "fn original() {\n    first();\n    second();\n    third();\n    fourth();\n}\n"
        self.commit_files({site: source})
        issue, (finding,) = self.judged_findings(self.rule("read"), ((site, 4, "third();"),))
        self.write_generated_body(issue)
        self.commit_files({site: "// removed\n", destination: source.replace("original", "renamed")})
        output = self.refill_decisions()
        self.assertNotIn("waived", output)
        row, = self.ledger().proposal_findings(issue)
        self.assertEqual((row.finding_id, row.path, row.line, row.state),
                         (finding.finding_id, destination, 4, style_ledger.Filed(issue)))
        ticket = self.ledger().ticket_open("names-unit")[0]
        self.assertEqual((ticket["files"], ticket["finding_ids"]), ([destination], [finding.finding_id]))
        self.ledger().ticket_drop(ticket["id"])
        failed = source.replace("original", "failed").replace("second();", "different();").replace("third();", "changed();").replace("fourth();", "other();")
        # The old body line is copied to another file: unresolved proof must suppress the text-move waiver.
        self.commit_files({destination: failed, site: "fn unrelated() { third(); }\n", "crates/alpha/src/failed.rs": failed})
        output = self.refill_decisions()
        self.assertIn(f"waiting {issue}:", output)
        self.assertIn("outside rename not proved", output)
        self.assertIn(f"mapped site {destination}:4", output)
        self.assertIn("crates/alpha/src/failed.rs", output)
        self.assertNotIn("waived", output)
        row, = self.ledger().proposal_findings(issue)
        self.assertEqual((row.finding_id, row.path, row.state), (finding.finding_id, destination, style_ledger.Filed(issue)))
        self.assertFalse(any(finding.finding_id in ticket["finding_ids"] for ticket in self.ledger().ticket_open("names-unit")))
        self.assertEqual(frontmatter(issue)["status"], ["open"])

    def test_decision_refill_tickets_a_finding_at_the_file_its_declaration_moved_to(self) -> None:
        issue, finding, output = self.refill_after_declaration_move({"crates/alpha/src/parent.rs": "pub struct Parent {\n    old: u32,\n}\n"})
        self.assertNotIn("waived", output)
        followed, = self.ledger().proposal_findings(issue)
        self.assertEqual((followed.finding_id, followed.path, followed.line, followed.state),
                         (finding.finding_id, "crates/alpha/src/parent.rs", 2, style_ledger.Filed(issue)))
        ticket = self.ledger().ticket_open("names-unit")[0]
        self.assertEqual((ticket["kind"], ticket["files"], ticket["finding_ids"]),
                         ("proposal", ["crates/alpha/src/parent.rs"], [finding.finding_id]))

    def test_decision_refill_waits_on_a_decision_the_declaration_step_skipped_and_does_not_waive_its_finding(self) -> None:
        issue, finding, output = self.refill_after_declaration_move(
            {"crates/alpha/src/parent.rs": "pub struct Parent {\n    old: u32,\n}\n"}, generated_body=False)
        self.assertIn(f"skipped {issue}: missing generated decision body\n", output)
        self.assertIn(f"waiting {issue}: skipped by the declaration step\n", output)
        self.assertNotIn("waived", output)
        self.assertEqual([(held.path, held.line, held.state, held.follow) for held in self.ledger().proposal_findings(issue)],
                         [(finding.path, 2, style_ledger.Filed(issue), style_ledger.NotFollowed())])
        self.assertEqual(frontmatter(issue)["status"], ["open"])

    def test_decision_refill_follows_the_findings_of_every_approved_decision_before_it_tickets_one(self) -> None:
        rule = self.rule("read")
        first, second = self.sources[:2]
        self.commit_files({first: "pub struct Parent {\n    old: u32,\n}\n", second: "pub struct Other {\n    old: u32,\n}\n"})
        issue, (stays,) = self.judged_findings(rule, ((first, 2, "old: u32,"),))
        self.write_generated_body(issue)
        # A second approved decision, after the first in the folder, holds a finding in the second source.
        later = issue.with_name("later.md")
        _ = later.write_text(issue.read_text())
        ledger = self.ledger()
        ledger.ticket_add("modules-unit-2", "modules-unit", "read", "names", "crates/alpha", [second], [rule])
        ledger.ticket_assign("modules-unit-2", "style/main")
        _ = ledger.ticket_base("modules-unit-2")
        ledger.ticket_drop("modules-unit-2")
        blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule)
        moves = ledger.hold(rule, second, 2, "old: u32,", "type", "before", "after", blob, "modules-unit-2")
        ledger.file_held(moves.finding_id, later)
        _ = self.git("rm", "-q", second)
        self.commit_files({"crates/alpha/src/moved.rs": "pub struct Other {\n    old: u32,\n}\n"})
        self.assertNotIn("waived", self.refill_decisions())
        self.assertEqual(self.ledger().ticket_open("names-unit")[0]["finding_ids"], [stays.finding_id])
        followed, = self.ledger().proposal_findings(later)
        self.assertEqual((followed.finding_id, followed.path, followed.line), (moves.finding_id, "crates/alpha/src/moved.rs", 2))

    def test_refill_names_every_ambiguous_declaration_destination(self) -> None:
        source = "pub struct Parent {\n    old: u32,\n}\n"
        first, second = "crates/alpha/src/one.rs", "crates/alpha/src/two.rs"
        issue, finding, output = self.refill_after_declaration_move({second: source, first: source})
        self.assertIn(f"waiting {issue}: {finding.finding_id} declared in 2 files: {first}, {second}", output)
        row, = self.ledger().proposal_findings(issue)
        self.assertEqual((row.path, row.state, row.follow), (finding.path, style_ledger.Filed(issue), style_ledger.NotFollowed()))

    def test_decision_refill_leaves_a_finding_declared_in_two_files_out_of_the_ticket(self) -> None:
        parent = "pub struct Parent {\n    old: u32,\n}\n"
        issue, finding, output = self.refill_after_declaration_move({"crates/alpha/src/one.rs": parent, "crates/alpha/src/two.rs": parent})
        self.assertIn(f"waiting {issue}: {finding.finding_id} declared in 2 files: crates/alpha/src/one.rs, crates/alpha/src/two.rs; outside rename not proved: old name still declared in crate; "
                      + "candidates: none; mapped site unresolved: source site deleted\n", output)
        self.assertEqual([(held.path, held.line, held.state, held.follow) for held in self.ledger().proposal_findings(issue)],
                         [(finding.path, 2, style_ledger.Filed(issue), style_ledger.NotFollowed())])
        self.assertNotEqual(self.ledger().ticket_open("names-unit")[0]["kind"], "proposal")

    def test_decision_refill_waits_on_a_finding_whose_declaration_is_gone_from_its_kept_file(self) -> None:
        site = self.sources[0]
        self.commit_files({site: "pub struct Parent {\n    old: u32,\n}\n"})
        issue, (finding,) = self.judged_findings(self.rule("read"), ((site, 2, "old: u32,"),))
        self.commit_files({site: "pub fn other() {}\n"})
        output = self.refill_decisions()
        self.assertIn(f"waiting {issue}: {finding.finding_id} outside rename not proved:", output)
        self.assertEqual([(held.path, held.line, held.state, held.follow) for held in self.ledger().proposal_findings(issue)],
                         [(site, 2, style_ledger.Filed(issue), style_ledger.NotFollowed())])
        self.assertNotEqual(self.ledger().ticket_open("names-unit")[0]["kind"], "proposal")
        self.assertEqual(frontmatter(issue)["status"], ["open"])

    def test_decision_refill_waits_when_a_findings_trait_identity_is_uncertain(self) -> None:
        site = self.sources[0]
        source = "impl Trait for Type {\n    fn method(&self) { 7 }\n}\n"
        self.commit_files({site: source})
        issue, (finding,) = self.judged_findings(self.rule("read"), ((site, 2, "7"),))
        self.write_generated_body(issue)
        self.commit_files({site: source.replace("impl Trait", "impl external::Trait")})
        output = self.refill_decisions()
        self.assertIn(f"waiting {issue}: {finding.finding_id} trait identity uncertain\n", output)
        self.assertEqual(self.ledger().proposal_findings(issue)[0].state, style_ledger.Filed(issue))
        self.assertFalse(any(finding.finding_id in ticket["finding_ids"] for ticket in self.ledger().ticket_open("names-unit")))

    def test_decision_refill_closes_a_decision_whose_only_finding_the_declaration_step_built(self) -> None:
        site = self.sources[0]
        parent = "pub struct Parent {\n    old: u32,\n}\n"
        self.commit_files({site: parent})
        issue, (finding,) = self.judged_findings(self.rule("read"), ((site, 1, "pub struct Parent {"),),
                                                 kind="rename", before="Parent", after="Renamed")
        self.commit_files({site: parent.replace("Parent", "Renamed")})
        code, output, errors, _ = self.refill_closing()
        self.assertEqual(code, 0, errors)
        reason = f"built in synced commit {self.tip}"
        self.assertIn(f"built {finding.finding_id} {finding.rule} {site}:1: struct Parent renamed to Renamed in {site}:1\n", output)
        self.assertIn(f"closed {issue}: {reason}\n", output)
        self.assertEqual((frontmatter(issue)["status"], frontmatter(issue)["reason"]), (["closed"], [reason]))
        self.assertEqual(self.ledger().proposal_findings(issue)[0].state, style_ledger.Built(issue))

    def test_decision_refill_waits_on_a_decision_whose_answer_changes_before_its_rename_is_built(self) -> None:
        site = self.sources[0]
        parent = "pub struct Parent {\n    old: u32,\n}\n"
        self.commit_files({site: parent})
        issue, (finding,) = self.judged_findings(self.rule("read"), ((site, 1, "pub struct Parent {"),),
                                                 kind="rename", before="Parent", after="Renamed")
        self.commit_files({site: parent.replace("Parent", "Renamed")})
        # The refill reads the decision as approved; the declaration step then reads another answer.
        with mock.patch.object(style_proposals, "approved_decision", return_value=False):
            output = self.refill_decisions()
        self.assertIn(f"waiting {issue}: no longer approved\n", output)
        self.assertEqual([(held.finding_id, held.state) for held in self.ledger().proposal_findings(issue)],
                         [(finding.finding_id, style_ledger.Filed(issue))])
        self.assertNotEqual(self.ledger().ticket_open("names-unit")[0]["kind"], "proposal")
        self.assertEqual(frontmatter(issue)["status"], ["open"])

    def test_decision_refill_after_a_merge_builds_only_the_unbuilt_findings(self) -> None:
        rule = self.rule("read")
        self.sync()
        issue = self.approved_decision(rule, self.sources[:2], "rename")
        self.write(self.sources[0], "pub enum Outcome { New }\n")
        self.write(self.sources[1], "pub enum Outcome { Old }\n")
        self.commit_to_every_checkout()
        ledger = self.ledger()
        applied, unapplied = (next(finding for finding in ledger.proposal_findings(issue) if finding.path == path)
                              for path in self.sources[:2])
        with mock.patch.object(style_proposals, "proposal_command"):
            self.assertEqual(self.run_next()[0], 0)
            first = ledger.ticket_open("names-unit")[0]
            self.assertEqual(sorted(first["finding_ids"]), sorted((applied.finding_id, unapplied.finding_id)))
            ledger.ticket_checkpoint(first["id"], self.tip)
            _ = ledger.ticket_merge(first["id"])
            code, _, errors = self.run_next()
        self.assertEqual(code, 0, errors)
        second = ledger.ticket_open("names-unit")[0]
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual((second["kind"], second["files"], second["finding_ids"]),
                         ("proposal", [unapplied.path], [unapplied.finding_id]))

    def test_decision_refill_skips_a_merged_type_finding_until_a_person_marks_it_built(self) -> None:
        rule = self.rule("read")
        self.sync()
        issue = self.approved_decision(rule, (self.sources[0],), "type")
        ledger = self.ledger()
        finding, = ledger.proposal_findings(issue)
        with mock.patch.object(style_proposals, "proposal_command"):
            self.assertEqual(self.run_next()[0], 0)
        ticket = ledger.ticket_open("names-unit")[0]
        self.assertEqual((ticket["kind"], ticket["finding_ids"]), ("proposal", [finding.finding_id]))
        ledger.ticket_checkpoint(ticket["id"], self.tip)
        self.assertEqual(ledger.ticket_merge(ticket["id"]),
                         (style_ledger.FindingNotBuilt(finding.finding_id, approved_renames.NoAppliedCheck("type")),))
        held, = ledger.proposal_findings(issue)
        self.assertEqual((held.state, held.build_ticket), (style_ledger.Filed(issue), style_ledger.BuildTicket(ticket["id"])))
        code, _, errors, _ = self.refill_closing()
        self.assertEqual(code, 0, errors)
        self.assertEqual([item["id"] for item in ledger.proposal_tickets()], [ticket["id"]])
        self.assertEqual(frontmatter(issue)["status"], ["open"])
        ledger.mark_built(finding.finding_id)
        with mock.patch.object(style_proposals, "renumber"), redirect_stdout(io.StringIO()):
            style_proposals.complete(ledger)
        self.assertEqual((frontmatter(issue)["status"], frontmatter(issue)["reason"]), (["closed"], [f"built in {ticket['id']}"]))

    def refill_closing(self) -> tuple[int, str, str, int]:
        """Run refill with only the real close command, so a decision it closes is closed on disk.

        The last value is the count of renumber calls.
        """
        original = style_proposals.proposal_command

        def command(*args: str) -> None:
            if args[0] == "close":
                original(*args)
        with (mock.patch.object(style_proposals, "proposal_command", side_effect=command),
              mock.patch.object(style_proposals, "renumber") as renumber):
            return *self.run_next(), renumber.call_count

    def test_decision_refill_closes_a_decision_whose_only_finding_is_in_a_removed_file(self) -> None:
        rule = self.rule("read")
        self.sync()
        issue = self.approved_decision(rule, (self.sources[1],), "rename")
        _ = self.git("rm", "-q", self.sources[1])
        self.commit_to_every_checkout()
        code, output, errors, renumbers = self.refill_closing()
        self.assertEqual(code, 0, errors)
        reason = f"nothing left to build: {self.sources[1]} not in synced commit {self.tip}"
        self.assertIn(f"closed {issue}: {reason}\n", output)
        self.assertEqual((frontmatter(issue)["status"], frontmatter(issue)["reason"]), (["closed"], [reason]))
        self.assertEqual(style_proposals.approved_decisions(issue.parent, "names-unit"), ())
        # No build merged, so complete does not renumber; the refill that closed the decision does, once.
        self.assertEqual(renumbers, 1)

    def test_decision_refill_closes_a_built_decision_whose_last_finding_is_in_a_removed_file(self) -> None:
        rule = self.rule("read")
        self.sync()
        issue = self.approved_decision(rule, self.sources[:2], "rename")
        ledger = self.ledger()
        built, gone = (next(finding for finding in ledger.proposal_findings(issue) if finding.path == path)
                       for path in self.sources[:2])
        self.write(built.path, "pub enum Outcome { New }\n")
        self.commit_to_every_checkout()
        ledger.ticket_add("names-unit-1", "names-unit", "proposal", "names", "crates/alpha", [built.path], [rule],
                          proposal=str(issue), finding_ids=(built.finding_id,))
        ledger.ticket_assign("names-unit-1", "main")
        ledger.ticket_checkpoint("names-unit-1", self.tip)
        _ = ledger.ticket_merge("names-unit-1")
        _ = self.git("rm", "-q", gone.path)
        self.commit_to_every_checkout()
        code, output, errors, renumbers = self.refill_closing()
        self.assertEqual(code, 0, errors)
        self.assertIn(f"closed {issue}: built in names-unit-1\n", output)
        self.assertEqual((frontmatter(issue)["status"], frontmatter(issue)["reason"]), (["closed"], ["built in names-unit-1"]))
        self.assertEqual(style_proposals.approved_decisions(issue.parent, "names-unit"), ())
        # Complete renumbers for the merged build, and the refill that closed the decision renumbers once.
        self.assertEqual(renumbers, 2)

    def test_decision_refill_skips_a_queued_ticket_made_before_decisions(self) -> None:
        rule = self.rule("read")
        self.sync()
        issue = self.approved_decision(rule, (self.sources[0],), "rename")
        ledger = self.ledger()
        finding = ledger.proposal_findings(issue)[0]
        ledger.ticket_add("names-unit-1", "names-unit", "proposal", "names", "crates/alpha", [finding.path], [rule],
                          proposal=str(issue), finding_ids=(finding.finding_id,))
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE tickets SET finding_ids='[]'")
            _ = connection.execute("UPDATE held_findings SET build_ticket=''")
        with mock.patch.object(style_proposals, "proposal_command"):
            code, output, errors = self.run_next()
        self.assertEqual(code, 0, errors)
        self.assertEqual(output.count("waiting names-unit-1: made before decisions; "
                                      + "drop it with style_ledger.py ticket drop, then run migrate\n"), 1)
        self.assertEqual([ticket["id"] for ticket in ledger.queued_tickets("names-unit")], ["names-unit-1"])
        opened = ledger.ticket_open("names-unit")[0]
        self.assertNotEqual(opened["id"], "names-unit-1")
        self.assertEqual(opened["finding_ids"], [finding.finding_id])

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
        self.assertEqual(output.count(f"waiting {issue}: files busy\n"), 1)
        self.assertEqual(self.ledger().ticket_open("names-unit")[0]["files"], [files[2]])

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

    def test_approved_proposal_precedes_coverage_and_restart_rebuild_keeps_authorization(self) -> None:
        rule = self.rule("read")
        self.sync()
        folder = self.root / "issues"
        folder.mkdir()
        issue = folder / "approved.md"
        with style_ledger.open_ledger(self.repo) as ledger:
            blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule)
            finding = ledger.hold(rule, self.sources[0], 1, "value", "type", "u32", "u64", blob, "outside")
            ledger.file_held(finding.finding_id, issue)
        _ = issue.write_text('---\nstatus: open\nstage: backlog\ntags: [issue, proposal, style]\n'
                             + f'unit: names-unit\nplan: "{self.plan} Phase 1"\nstyle_rule: {rule}\nstyle_crate: crates/alpha\nstyle_rule_blob: {blob}\n---\n')
        _ = self.doc.write_text(self.doc.read_text().replace("## Units", f"- **Proposals:** {folder}\n\n## Units"))
        def start(*args: str) -> None:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(proposal_issue.main(list(args)), 0)
        with mock.patch.object(style_proposals, "proposal_command", side_effect=start):
            self.assertEqual(self.run_next()[0], 0)
            ticket = self.ledger().ticket_open("names-unit")[0]
            self.assertEqual(ticket["kind"], "proposal")
            self.assertEqual(ticket["files"], [self.sources[0]])
            first = self.plan.read_text()
            self.assertIn(f"**Goal:**\nApply the approved findings of `{issue}` in 1 file of `crates/alpha`.\n", first)
            self.assertIn("This ticket builds the approved proposal", first)
            self.assertNotIn("Judge", first)
            self.assertNotIn("overrides the mechanical-only invariant", first)
            self.assertIn("Apply the approved findings.", first)
            self.assertNotIn("Apply only mechanical fixes", first)
            self.assertNotIn("Every structure finding is held", first)
            self.assertIn("style_plan.py gate --repo . --ticket names-unit-1", first)
            _ = self.plan.write_text(self.plan_header())
            result = self.run_next()
            self.assertEqual(result[0], 0, result)
            self.assertIn("resume names-unit-1", result[1])
            self.assertEqual(self.plan.read_text(), first)
        self.assertIn("stage: active", issue.read_text())

    def approved_reference_ticket(
        self, pairs: tuple[tuple[str, str], ...] = (("Outcome::OldName", "Outcome::NewName"),),
        kinds: tuple[style_ledger.FindingKind, ...] = ("rename",),
    ) -> str:
        rule = self.rule("approved-reference")
        issue = self.root / "reference-approval.md"
        with style_ledger.open_ledger(self.repo) as ledger:
            blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule)
            ids: list[str] = []
            for index, (before, after) in enumerate(pairs):
                finding = ledger.hold(rule, self.sources[0], index + 1, "rename", kinds[index % len(kinds)],
                                      before, after, blob, "outside")
                ledger.file_held(finding.finding_id, issue)
                ids.append(finding.finding_id)
            _ = issue.write_text("---\nstatus: open\ntags: [proposal, style]\nunit: names-unit\nplan: approved\n"
                                 + f"style_rule: {rule}\nstyle_crate: crates/alpha\nstyle_rule_blob: {blob}\n---\n")
            ledger.ticket_add("names-unit-1", "names-unit", "proposal", "names", "crates/alpha",
                              [self.sources[0]], [rule], proposal=str(issue), finding_ids=tuple(ids))
        # The gate passes only when the finding file declares each checkable rename's new name.
        declared: dict[str, list[str]] = {}
        for index, (before, after) in enumerate(pairs):
            rename = approved_renames.approved_rename(kinds[index % len(kinds)], before, after)
            if isinstance(rename, approved_renames.ApprovedRename):
                declared.setdefault(rename.parent, []).append(rename.new)
        if declared:
            self.write(self.sources[0], "".join(f"enum {parent} {{ {', '.join(names)} }}\n"
                                                for parent, names in declared.items()))
        return "names-unit-1"

    def write_approved_phase(self) -> None:
        """Write the phase of the queued approved ticket at a synced tip, where the ledger finds the cells that record takes."""
        self.sync()
        ledger = self.ledger()
        ticket = ledger.queued_tickets("names-unit")[0]
        ledger.ticket_assign(ticket["id"], "demo-style-names")
        style_plan.write_phase(ledger, ticket, self.plan, self.plan_header())

    def reference_gate(self, ticket: str) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            result = style_plan.gate(self.repo, ticket)
        return result, output.getvalue()

    LEDGER_MOD = ("mod handle;\nmod lock;\nmod map;\n\npub use lock::{LockError, MutationLock};\n\n"
                  + "#[cfg(test)]\nmod tests {\n    use super::*;\n\n    #[test]\n"
                  + "    fn reads() {\n        let _ = lock::MutationLock;\n    }\n}\n")
    LEDGER_HANDLE = ("// Takes the lock.\nuse super::lock::MutationLock;\nuse crate::ledger::lock;\n\n"
                     + "pub struct Handle {\n    lock: MutationLock,\n}\n\n"
                     + "pub fn take(handle: &Handle) -> lock::LockError {\n    let _ = &handle.lock;\n"
                     + "    self::lock();\n    lock::LockError\n}\n\nfn lock() {}\n")

    def file_rename_ticket(self, files: tuple[tuple[str, str], ...] = (),
                           pairs: tuple[tuple[str, str], ...] = ()) -> str:
        """An approved ticket that renames crates/alpha/src/ledger/lock.rs to mutation_lock.rs, at a HEAD that holds it.

        Files replace or add HEAD files, and each pair adds a finding that renames an item of the file.
        """
        old = "crates/alpha/src/ledger/lock.rs"
        self.write(self.sources[0], "pub mod ledger;\n")
        self.write("crates/alpha/src/ledger/mod.rs", self.LEDGER_MOD)
        self.write("crates/alpha/src/ledger/handle.rs", self.LEDGER_HANDLE)
        self.write("crates/alpha/src/ledger/map.rs", "pub fn lock() {}\n")
        self.write(old, "pub struct MutationLock;\npub struct LockError;\n")
        for path, text in files:
            self.write(path, text)
        _ = self.commit()
        rule = self.rule("file-rename")
        issue = self.root / "file-rename-approval.md"
        with style_ledger.open_ledger(self.repo) as ledger:
            blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule)
            findings = [ledger.hold(rule, old, 1, "pub struct MutationLock;", "rename", "The file contains MutationLock.",
                                    "Rename ledger/lock.rs to ledger/mutation_lock.rs; update ledger/mod.rs and all "
                                    + "super::lock paths", blob, "outside")]
            findings.extend(ledger.hold(rule, old, 2, "pub struct LockError;", "rename", before, after, blob, "outside")
                            for before, after in pairs)
            for finding in findings:
                ledger.file_held(finding.finding_id, issue)
            _ = issue.write_text("---\nstatus: open\ntags: [proposal, style]\nunit: names-unit\nplan: approved\n"
                                 + f"style_rule: {rule}\nstyle_crate: crates/alpha\nstyle_rule_blob: {blob}\n---\n")
            ledger.ticket_add("names-unit-1", "names-unit", "proposal", "names", "crates/alpha", [old], [rule],
                              proposal=str(issue), finding_ids=tuple(finding.finding_id for finding in findings))
        _ = self.git("mv", old, "crates/alpha/src/ledger/mutation_lock.rs")
        return "names-unit-1"

    def renamed_ledger(self, handle: str = LEDGER_HANDLE, error: str = "LockError") -> None:
        """The edits a file rename makes outside the file, ordered as rustfmt orders them, with LockError named error."""
        self.write("crates/alpha/src/ledger/mod.rs", self.LEDGER_MOD.replace(
            "mod lock;\nmod map;\n\npub use lock::{LockError, MutationLock};",
            f"mod map;\nmod mutation_lock;\n\npub use mutation_lock::{{{error}, MutationLock}};").replace(
            "lock::MutationLock;\n    }", "mutation_lock::MutationLock;\n    }"))
        self.write("crates/alpha/src/ledger/handle.rs", handle.replace(
            "use super::lock::MutationLock;\nuse crate::ledger::lock;",
            "use crate::ledger::mutation_lock;\nuse super::mutation_lock::MutationLock;").replace(
            "lock::LockError", f"mutation_lock::{error}"))

    def test_approved_file_rename_allows_its_mod_line_and_module_paths_in_other_files(self) -> None:
        ticket = self.file_rename_ticket()
        self.renamed_ledger()
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_approved_file_rename_refuses_another_edit_in_a_file_it_changes(self) -> None:
        ticket = self.file_rename_ticket()
        self.renamed_ledger()
        handle = self.repo / "crates/alpha/src/ledger/handle.rs"
        for other in (("pub fn take(", "pub fn take_lock("), ("// Takes the lock.", "// Takes the mutation_lock."),
                      ("    self::lock();", "    self::mutation_lock();"), ("handle.lock;", "handle.mutation_lock;")):
            with self.subTest(other=other):
                renamed = handle.read_text()
                _ = handle.write_text(renamed.replace(*other))
                result, output = self.reference_gate(ticket)
                self.assertEqual(result, 1, output)
                self.assertIn("crates/alpha/src/ledger/handle.rs (change is not an approved rename reference)", output)
                _ = handle.write_text(renamed)

    def test_canonical_order_drops_a_trailing_comma_only_where_it_does_not_change_the_code(self) -> None:
        def words(text: str) -> tuple[str, ...]:
            return style_plan.canonical_order(tuple(token.value for token in style_footprint.tokens(text)))
        # A one-entry tuple is not the grouped expression, even when its entry holds commas.
        self.assertNotEqual(words("let f = (|a: u8, b: u8| a + b,);"), words("let f = (|a: u8, b: u8| a + b);"))
        self.assertNotEqual(words("let m = (HashMap::<u8, u8>::new(),);"), words("let m = (HashMap::<u8, u8>::new());"))
        self.assertEqual(words("acquire(\n    mutation_lock::x,\n);"), words("acquire(mutation_lock::x);"))

    def test_approved_file_rename_refuses_a_comment_moved_between_items(self) -> None:
        handle = (self.LEDGER_HANDLE + "\n// a\nfn first() {}\n\n// b\nfn second() {}\n\n"
                  + "/// c\nfn third() {}\n\n// SAFETY: d\nfn fourth() {}\n")
        ticket = self.file_rename_ticket(files=(("crates/alpha/src/ledger/handle.rs", handle),))
        self.renamed_ledger(handle)
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))
        path = self.repo / "crates/alpha/src/ledger/handle.rs"
        renamed = path.read_text()
        for first, second in (("// a", "// b"), ("/// c", "// SAFETY: d")):
            with self.subTest(first=first, second=second):
                _ = path.write_text(renamed.replace(first, "\0").replace(second, first).replace("\0", second))
                result, output = self.reference_gate(ticket)
                self.assertEqual(result, 1, output)
                self.assertIn("crates/alpha/src/ledger/handle.rs (change is not an approved rename reference)", output)
                _ = path.write_text(renamed)

    UNIT_TESTS = ('#[path = "../src/ledger/handle.rs"]\nmod handle;\n#[path = "../src/ledger/lock.rs"]\nmod lock;\n'
                  + '#[path = "../src/ledger/map.rs"]\nmod map;\n')

    def test_approved_file_rename_allows_a_path_attribute_that_names_the_file(self) -> None:
        tests = "crates/alpha/tests/unit_tests.rs"
        ticket = self.file_rename_ticket(files=((tests, self.UNIT_TESTS),))
        self.renamed_ledger()
        # rustfmt moves the renamed item to its sorted place.
        renamed = ('#[path = "../src/ledger/handle.rs"]\nmod handle;\n#[path = "../src/ledger/map.rs"]\nmod map;\n'
                   + '#[path = "../src/ledger/mutation_lock.rs"]\nmod mutation_lock;\n')
        self.write(tests, renamed)
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))
        for literal in ('"../src/ledger/mutation_locks.rs"', '"../src/mutation_lock.rs"', '"../src/ledger/./mutation_lock.rs"'):
            with self.subTest(literal=literal):
                self.write(tests, renamed.replace('"../src/ledger/mutation_lock.rs"', literal))
                result, output = self.reference_gate(ticket)
                self.assertEqual(result, 1, output)
                self.assertIn(f"{tests} (change is not an approved rename reference)", output)

    def test_approved_file_rename_refuses_a_path_attribute_that_names_another_file(self) -> None:
        tests = "crates/alpha/tests/unit_tests.rs"
        ticket = self.file_rename_ticket(files=((tests, '#[path = "../src/ledger/map.rs"]\nmod lock;\n'),))
        self.renamed_ledger()
        self.write(tests, '#[path = "../src/ledger/map.rs"]\nmod mutation_lock;\n')
        result, output = self.reference_gate(ticket)
        self.assertEqual(result, 1, output)
        self.assertIn(f"{tests} (change is not an approved rename reference)", output)

    def test_approved_file_rename_refuses_a_mod_line_inside_a_macro(self) -> None:
        mod_file = "crates/alpha/src/ledger/mod.rs"
        lib = 'pub mod ledger;\nconst SOURCE: &str = stringify!(#[path = "ledger/lock.rs"] mod lock;);\n'
        macros = "macro_rules! declare {\n    () => {\n        mod lock;\n    };\n}\nconst SOURCE: &str = stringify! { mod lock; };\n"
        ticket = self.file_rename_ticket(files=((self.sources[0], lib), (mod_file, self.LEDGER_MOD + macros)))
        self.renamed_ledger()
        self.write(mod_file, (self.repo / mod_file).read_text() + macros)
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))
        for path, first, second in (
                (self.sources[0], '"ledger/lock.rs"] mod lock;', '"ledger/mutation_lock.rs"] mod mutation_lock;'),
                (mod_file, "        mod lock;", "        mod mutation_lock;"),
                (mod_file, "{ mod lock; }", "{ mod mutation_lock; }")):
            with self.subTest(path=path, second=second):
                text = (self.repo / path).read_text()
                self.write(path, text.replace(first, second))
                result, output = self.reference_gate(ticket)
                self.assertEqual(result, 1, output)
                self.assertIn(f"{path} (change is not an approved rename reference)", output)
                self.write(path, text)

    def test_approved_file_rename_allows_a_crate_path_to_the_module_in_a_comment(self) -> None:
        handle = "//! Holds [`crate::ledger::lock::MutationLock`], next to [`super::lock`].\n" + self.LEDGER_HANDLE
        map_file = "//! Next to [`crate::ledger::lock`] and [`crate::other::lock`].\npub fn lock() {}\n"
        ticket = self.file_rename_ticket(files=(("crates/alpha/src/ledger/handle.rs", handle),
                                                ("crates/alpha/src/ledger/map.rs", map_file)))
        self.renamed_ledger(handle.replace("crate::ledger::lock::", "crate::ledger::mutation_lock::"))
        self.write("crates/alpha/src/ledger/map.rs", map_file.replace("crate::ledger::lock`", "crate::ledger::mutation_lock`"))
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))
        for path, first, second in (
                ("crates/alpha/src/ledger/handle.rs", "//! Holds", "//! Keeps"),
                ("crates/alpha/src/ledger/map.rs", "crate::other::lock", "crate::other::mutation_lock"),
                ("crates/alpha/src/ledger/handle.rs", "[`super::lock`]", "[`super::mutation_lock`]")):
            with self.subTest(path=path, second=second):
                text = (self.repo / path).read_text()
                self.write(path, text.replace(first, second))
                result, output = self.reference_gate(ticket)
                self.assertEqual(result, 1, output)
                self.assertIn(f"{path} (change is not an approved rename reference)", output)
                self.write(path, text)

    def test_approved_file_rename_reads_a_plain_path_from_its_module_or_the_library_name(self) -> None:
        store, beta = "crates/alpha/src/store/mod.rs", self.sources[3]
        ticket = self.file_rename_ticket(files=(
            (self.sources[0], "pub mod ledger;\nuse ledger::lock::MutationLock;\n"),
            (self.sources[2], "use alpha::ledger::lock::MutationLock;\n"),
            (store, "use archive::ledger::lock::Entry;\n"),
            (beta, "use beta::ledger::lock::MutationLock;\n")))
        self.renamed_ledger()
        for path in (self.sources[0], self.sources[2]):
            self.write(path, (self.repo / path).read_text().replace("::lock::", "::mutation_lock::"))
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))
        for path in (store, beta):
            with self.subTest(path=path):
                text = (self.repo / path).read_text()
                self.write(path, text.replace("::lock::", "::mutation_lock::"))
                result, output = self.reference_gate(ticket)
                self.assertEqual(result, 1, output)
                self.assertIn(f"{path} (change is not an approved rename reference)", output)
                self.write(path, text)

    def test_approved_file_rename_and_item_rename_edit_one_file(self) -> None:
        handle = self.LEDGER_HANDLE.replace("-> lock::LockError", "-> super::lock::LockError")
        ticket = self.file_rename_ticket(files=(("crates/alpha/src/ledger/handle.rs", handle),),
                                         pairs=(("lock::LockError", "lock::LockFailure"),))
        self.renamed_ledger(handle, "LockFailure")
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))
        path = self.repo / "crates/alpha/src/ledger/handle.rs"
        _ = path.write_text(path.read_text().replace("super::mutation_lock::LockFailure", "super::mutation_lock::LockFault"))
        result, output = self.reference_gate(ticket)
        self.assertEqual(result, 1, output)
        self.assertIn("crates/alpha/src/ledger/handle.rs (change is not an approved rename reference)", output)

    def test_approved_file_rename_leaves_a_folder_of_child_modules_to_a_person(self) -> None:
        folder = "crates/alpha/src/ledger/lock"
        ticket = self.file_rename_ticket(files=(
            (f"{folder}.rs", "mod guard;\npub struct MutationLock;\npub struct LockError;\n"),
            (f"{folder}/guard.rs", "pub struct Guard;\n")))
        _ = self.git("mv", folder, "crates/alpha/src/ledger/mutation_lock")
        self.renamed_ledger()
        result, output = self.reference_gate(ticket)
        self.assertEqual(result, 1, output)
        for name in ("lock", "mutation_lock"):
            self.assertIn(f"crates/alpha/src/ledger/{name}/guard.rs (in crates/alpha/src/ledger/{name}/, the folder of "
                          + "a renamed module: a person moves it)", output)

    def test_approved_rename_allows_only_reference_edits_in_an_outside_file(self) -> None:
        before = ('// Outcome::OldName stays in this comment.\r\n'
                  + 'fn read() { /* nested /* note */ comment */ Outcome :: OldName; '
                  + 'let text = r#"Outcome::OldName é"#; }\r\n')
        self.write(self.sources[1], before)
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        self.write(self.sources[0], "// The finding file keeps unrestricted approval.\nenum Outcome { NewName }\n")
        self.write(self.sources[1], before.replace("Outcome :: OldName", "Outcome :: NewName"))
        _ = self.git("add", self.sources[1])
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_approved_rename_allows_two_pairs_and_qualified_use_leaves(self) -> None:
        before = ("use crate::m::Outcome::{OldName, Other};\n"
                  + "use crate::m::Outcome::SignalFailed;\n"
                  + "fn read() { Outcome::OldName; Outcome::SignalFailed; }\n")
        self.write(self.sources[1], before)
        _ = self.commit()
        ticket = self.approved_reference_ticket((("Outcome::OldName", "Outcome::NewName"),
                                                 ("Outcome::SignalFailed", "Outcome::Failed")))
        self.write(self.sources[1], before.replace("OldName", "NewName").replace("SignalFailed", "Failed"))
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_approved_rename_updates_comments_without_code_changes(self) -> None:
        before = ("// OldName and Outcome::OldName\r\n"
                  + "/// See OldName and Outcome::OldName.\r\n"
                  + "//! OldName remains; Outcome::OldName changes.\r\n"
                  + "/* OldName /* Outcome::OldName */ OldName */\r\n"
                  + "/** OldName */\r\n")
        self.write(self.sources[1], before)
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        after = before.replace("OldName", "NewName").replace("//! NewName", "//! OldName")
        self.write(self.sources[1], after)
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_approved_rename_updates_code_and_comments_together(self) -> None:
        before = "// OldName and Outcome::OldName\nfn read() { Outcome::OldName; }\n"
        self.write(self.sources[1], before)
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        self.write(self.sources[1], before.replace("OldName", "NewName"))
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_approved_rename_refuses_other_comment_changes_and_partial_words(self) -> None:
        before = "// OldName OldNameX XOldName _OldName OldName_ 1OldName OldName1\n"
        self.write(self.sources[1], before)
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        for after in (before.replace("OldName ", "NewName ", 1) + "// extra\n",
                      before.replace("OldNameX", "NewNameX"),
                      before.replace("XOldName", "XNewName"),
                      before.replace("_OldName", "_NewName"),
                      before.replace("OldName_", "NewName_"),
                      before.replace("1OldName", "1NewName"),
                      before.replace("OldName1", "NewName1")):
            with self.subTest(after=after):
                self.write(self.sources[1], after)
                result, output = self.reference_gate(ticket)
                self.assertEqual(result, 1, output)

    def test_approved_rename_keeps_string_and_char_literals_unchanged(self) -> None:
        before = 'fn read() { let text = "OldName"; let raw = r#"Outcome::OldName"#; let c = \'A\'; }\n'
        self.write(self.sources[1], before)
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        for after in (before.replace('"OldName"', '"NewName"'),
                      before.replace("Outcome::OldName", "Outcome::NewName"),
                      before.replace("'A'", "'B'")):
            with self.subTest(after=after):
                self.write(self.sources[1], after)
                result, output = self.reference_gate(ticket)
                self.assertEqual(result, 1, output)

    def test_approved_rename_updates_only_whole_names_in_markdown(self) -> None:
        path = "docs/renames.md"
        before = "# Outcome::OldName\r\nUse OldName; OldName may stay. OldNameX is unrelated. é\r\n"
        self.write(path, before)
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        after = before.replace("Outcome::OldName", "Outcome::NewName").replace("Use OldName", "Use NewName")
        self.write(path, after)
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))
        for refused in (after.replace("may stay", "can stay"),
                        after.replace("OldNameX", "NewNameX"), after.replace("\r\n", "\n")):
            with self.subTest(after=refused):
                self.write(path, refused)
                result, output = self.reference_gate(ticket)
                self.assertEqual(result, 1, output)
                self.assertIn(path, output)

    def test_approved_rename_refuses_reference_edits_in_other_file_types(self) -> None:
        paths = ("notes.txt", "settings.toml")
        for path in paths:
            self.write(path, "Outcome::OldName\n")
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        for path in paths:
            with self.subTest(path=path):
                self.write(path, "Outcome::NewName\n")
                result, output = self.reference_gate(ticket)
                self.assertEqual(result, 1, output)
                self.assertIn(path, output)
                self.write(path, "Outcome::OldName\n")

    def test_approved_rename_ticket_requires_comment_and_markdown_updates(self) -> None:
        _ = self.approved_reference_ticket()
        self.write_approved_phase()
        sentence = ("For each of these renames, find every use through the compiler, not a text search, and update each one "
                    + "in other files: `Outcome::OldName` -> `Outcome::NewName`. "
                    + "Also update every comment and Markdown doc in the repo that means the "
                    + "renamed item, and leave mentions of other items with the same name. "
                    + "Change nothing else in those files.")
        self.assertIn("Apply the approved findings. " + sentence, self.plan.read_text())

    def test_a_proposal_phase_names_each_qualified_pair_and_keeps_a_bare_rename_in_its_own_file(self) -> None:
        _ = self.approved_reference_ticket((("Outcome::OldName", "Outcome::NewName"), ("helper", "assistant")))
        self.write_approved_phase()
        text = self.plan.read_text()
        self.assertIn("update each one in other files: `Outcome::OldName` -> `Outcome::NewName`. ", text)
        self.assertNotIn("`helper`", text)
        self.assertIn("Apply any other rename only in its own file; when other files use its old name, hold it. ", text)

    def test_a_proposal_phase_with_only_qualified_pairs_has_no_own_file_rename_line(self) -> None:
        _ = self.approved_reference_ticket()
        self.write_approved_phase()
        self.assertNotIn("Apply any other rename only in its own file", self.plan.read_text())

    def test_nonrename_approval_ticket_omits_comment_and_markdown_instruction(self) -> None:
        _ = self.approved_reference_ticket(kinds=("type",))
        self.write_approved_phase()
        self.assertNotIn("find every use through the compiler", self.plan.read_text())

    def test_a_proposal_phase_lists_only_the_cells_record_takes_and_leaves_out_a_tier_b_cell_with_no_listed_site(self) -> None:
        rule = self.rule("partial", "tier: B\nfamily: names\ncandidates:\n  kind: regex\n  pattern: absent\n  surface: partial\n"
                         + "  uncovered: Read everything else.\n")
        uncovered = rule + "#uncovered"
        self.sync()
        issue = self.root / "partial-approval.md"
        ledger = self.ledger()
        blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule)
        ids = tuple(ledger.hold(held_rule, self.sources[0], line, "rename", "rename", "Outcome::Old", "Outcome::New", blob,
                                "outside").finding_id for line, held_rule in enumerate((rule, uncovered), start=1))
        for finding_id in ids:
            ledger.file_held(finding_id, issue)
        _ = issue.write_text("---\nstatus: open\ntags: [proposal, style]\nunit: names-unit\nplan: approved\n"
                             + f"style_rule: {rule}\nstyle_crate: crates/alpha\nstyle_rule_blob: {blob}\n---\n")
        ledger.ticket_add("names-unit-1", "names-unit", "proposal", "names", "crates/alpha", [self.sources[0]],
                          [rule, uncovered], proposal=str(issue), finding_ids=ids)
        ticket = ledger.queued_tickets("names-unit")[0]
        ledger.ticket_assign(ticket["id"], "demo-style-names")
        style_plan.write_phase(ledger, ticket, self.plan, self.plan_header())
        text = self.plan.read_text()
        self.assertIn("`style_ledger.py record --ticket names-unit-1 --rule <rule> --path <file>`", text)
        self.assertIn(f"- `{uncovered}` · `{self.sources[0]}`", text)
        self.assertNotIn(f"- `{rule}` · `{self.sources[0]}`", text)
        self.assertNotIn("Record every judged cell", text)

    def test_a_proposal_phase_gives_no_cell_at_a_ticket_file_that_record_refuses_for_its_rule(self) -> None:
        rule = self.rule("approved-cells")
        self.write("docs/notes.md", "Outcome::Old\n")
        self.tip = self.commit()
        self.sync()
        issue = self.root / "cells-approval.md"
        ledger = self.ledger()
        blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule)
        ids = tuple(ledger.hold(rule, path, 1, "rename", "rename", "Outcome::Old", "Outcome::New", blob, "outside").finding_id
                    for path in (self.sources[0], "docs/notes.md"))
        for finding_id in ids:
            ledger.file_held(finding_id, issue)
        _ = issue.write_text("---\nstatus: open\ntags: [proposal, style]\nunit: names-unit\nplan: approved\n"
                             + f"style_rule: {rule}\nstyle_crate: crates/alpha\nstyle_rule_blob: {blob}\n---\n")
        ledger.ticket_add("names-unit-1", "names-unit", "proposal", "names", "crates/alpha", [self.sources[0], "docs/notes.md"],
                          [rule], proposal=str(issue), finding_ids=ids)
        ticket = ledger.queued_tickets("names-unit")[0]
        ledger.ticket_assign(ticket["id"], "demo-style-names")
        style_plan.write_phase(ledger, ticket, self.plan, self.plan_header())
        text = self.plan.read_text()
        self.assertIn(f"- `{rule}` · `{self.sources[0]}`", text)
        self.assertNotIn(f"- `{rule}` · `docs/notes.md`", text)

    def test_prose_rename_approval_ticket_omits_reference_update_instruction(self) -> None:
        _ = self.approved_reference_ticket((("The file contains MutationLock.",
                                             "Rename ledger/lock.rs to ledger/mutation_lock.rs."),))
        self.write_approved_phase()
        self.assertIn("Apply the approved findings.", self.plan.read_text())
        self.assertNotIn("find every use through the compiler", self.plan.read_text())

    def test_bare_rename_mention_ignores_unknown_tokens_without_the_old_name(self) -> None:
        self.write(self.sources[0], "enum Outcome { OldName, Other }\n")
        self.write(self.sources[2], "fn unrelated() { § }\n")
        self.write("docs/renames.md", "Use OldName.\n")
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        self.write("docs/renames.md", "Use NewName.\n")
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_bare_rename_mention_allows_no_matching_rust_files(self) -> None:
        self.write(self.sources[2], "fn unrelated() { § }\n")
        self.write("docs/renames.md", "Use OldName.\n")
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        self.write("docs/renames.md", "Use NewName.\n")
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_bare_rename_mention_refuses_unknown_tokens_in_a_matching_file(self) -> None:
        self.write(self.sources[0], "enum Outcome { OldName, Other }\n")
        self.write(self.sources[2], "fn read() { Outcome::OldName; § }\n")
        self.write("docs/renames.md", "Use OldName.\n")
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        self.write("docs/renames.md", "Use NewName.\n")
        result, output = self.reference_gate(ticket)
        self.assertEqual(result, 1, output)
        self.assertIn("docs/renames.md", output)

    def test_read_ticket_omits_comment_and_markdown_instruction(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.assertEqual(self.run_next()[0], 0)
        self.assertNotIn("find every use through the compiler", self.plan.read_text())

    def test_approved_rename_allows_bare_comment_mentions_of_a_unique_item(self) -> None:
        self.write(self.sources[0], "enum Outcome { OldName, Other }\nimpl Outcome { fn read() { Self::OldName; } }\n")
        self.write(self.sources[1], "use crate::Outcome::{OldName};\n// OldName and Outcome::OldName\n")
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        self.write(self.sources[1], "use crate::Outcome::{OldName};\n// NewName and Outcome::OldName\n")
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_approved_rename_refuses_ambiguous_bare_comments_but_allows_qualified_mentions(self) -> None:
        self.write(self.sources[0], "enum Outcome { OldName, Other }\n")
        self.write(self.sources[2], "enum Different { OldName, Other }\n")
        self.write(self.sources[1], "// OldName and Outcome::OldName\n")
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        self.write(self.sources[1], "// NewName and Outcome::OldName\n")
        self.assertEqual(self.reference_gate(ticket)[0], 1)
        self.write(self.sources[1], "// OldName and Outcome::NewName\n")
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_approved_rename_allows_bare_markdown_mentions_of_a_unique_item(self) -> None:
        self.write(self.sources[0], "enum Outcome { OldName, Other }\n")
        self.write("docs/renames.md", "Use OldName and Outcome::OldName.\n")
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        self.write("docs/renames.md", "Use NewName and Outcome::OldName.\n")
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_approved_rename_refuses_ambiguous_bare_markdown_but_allows_qualified_mentions(self) -> None:
        self.write(self.sources[0], "enum Outcome { OldName, Other }\n")
        self.write(self.sources[2], "enum Different { OldName, Other }\n")
        self.write("docs/renames.md", "Use OldName and Outcome::OldName.\n")
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        self.write("docs/renames.md", "Use NewName and Outcome::OldName.\n")
        self.assertEqual(self.reference_gate(ticket)[0], 1)
        self.write("docs/renames.md", "Use OldName and Outcome::NewName.\n")
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_approved_rename_checks_ambiguity_at_head_even_in_the_finding_file(self) -> None:
        self.write(self.sources[0], "enum Outcome { OldName }\nenum Different { OldName }\n")
        self.write("docs/renames.md", "Use OldName.\n")
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        self.write(self.sources[0], "enum Outcome { NewName }\n")
        self.write("docs/renames.md", "Use NewName.\n")
        self.assertEqual(self.reference_gate(ticket)[0], 1)

    def test_approved_rename_uses_the_parent_of_an_absolute_approved_path(self) -> None:
        self.write(self.sources[1], "fn read() { library::Outcome::OldName; }\n")
        _ = self.commit()
        ticket = self.approved_reference_ticket((("::library::Outcome::OldName", "::library::Outcome::NewName"),))
        self.write(self.sources[1], "fn read() { library::Outcome::NewName; }\n")
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_approved_rename_refuses_other_text_changes_in_reference_files(self) -> None:
        before = ('// Outcome::OldName\nfn read() { Outcome::OldName; let n = 1; '
                  + 'let text = "Outcome::OldName"; }\n')
        self.write(self.sources[1], before)
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        reference = before.replace("{ Outcome::OldName", "{ Outcome::NewName")
        for after in (reference.replace("n = 1", "n = 2"),
                      reference.replace("// Outcome::OldName", "// Outcome::NewName extra"),
                      reference.replace('"Outcome::OldName"', '"Outcome::NewName"'),
                      reference.replace("let n", "let  n"),
                      before.replace("\n", "\r\n")):
            with self.subTest(after=after):
                self.write(self.sources[1], after)
                result, output = self.reference_gate(ticket)
                self.assertEqual(result, 1, output)
                self.assertIn("files outside approved findings:", output)
                self.assertIn(self.sources[1] + " (change is not an approved rename reference)", output)

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
        self.assertEqual(arguments[arguments.index("--ticket") + 1], "names-unit-1")
        base = self.tip
        self.assertEqual(self.ledger().ticket_open("names-unit")[0]["base"], base)
        self.assertEqual(self.git("for-each-ref", "refs/style-tickets"), "")
        expected = self.plan.read_bytes()
        _ = (self.unit / self.sources[0]).write_text("pub fn changed() {}\n")
        _ = self.git("add", self.sources[0], cwd=self.unit)
        _ = self.git("commit", "-qm", "Ticket change", cwd=self.unit)
        _ = self.plan.write_text(self.plan_header())
        self.assertEqual(self.run_next()[0], 0)
        self.assertEqual(self.ledger().ticket_open("names-unit")[0]["base"], base)
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
                             + f'unit: names-unit\nplan: "{self.plan} Phase 1"\nstyle_rule: {rule}\nstyle_crate: crates/alpha\nstyle_rule_blob: {blob}\n---\n')
        _ = self.doc.write_text(self.doc.read_text().replace("## Units", f"- **Proposals:** {folder}\n\n## Units"))
        with (mock.patch.object(berth_board, "read_board", return_value=self.board(self.sources[0])),
              mock.patch.object(style_proposals, "proposal_command") as start,
              mock.patch.object(style_ledger.Ledger, "cells", autospec=True, side_effect=style_ledger.Ledger.cells) as coverage):
            code, output, errors = self.run_next()
            self.assertEqual(code, 3, errors)
            self.assertIn(f"waiting {issue}: files busy", output)
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
        ticket = self.ledger().ticket_open("names-unit")[0]
        self.assertEqual((ticket["kind"], ticket["proposal"], ticket["files"]), ("proposal", str(issue), [self.sources[0]]))
        self.assertEqual(frontmatter(issue)["stage"], ["active"])

    def test_structure_gate_names_unjudged_cells_before_running_the_footprint_check(self) -> None:
        first = self.rule("structure-first", "tier: D\nfamily: modules\n")
        last = self.rule("structure-last", "tier: D\nfamily: modules\n")
        self.sync()
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.ticket_add("modules-unit-1", "modules-unit", "structure", "modules", "crates/alpha", [], [last, first])
            ledger.ticket_assign("modules-unit-1", "style/main")
            _ = ledger.ticket_base("modules-unit-1")
        self.record(first, "crates/alpha", self.tip, "modules-unit-1")
        output = io.StringIO()
        with (style_ledger.open_ledger(self.repo) as ledger,
              mock.patch.object(style_ledger, "open_ledger", return_value=ledger),
              mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as footprint,
              redirect_stdout(output)):
            self.assertEqual(style_plan.gate(self.repo, "modules-unit-1"), 1)
            footprint.assert_not_called()
        self.assertEqual(output.getvalue(), f"unjudged: {last} crates/alpha\n")

        self.record(last, "crates/alpha", self.tip, "modules-unit-1")
        with (style_ledger.open_ledger(self.repo) as ledger,
              mock.patch.object(style_ledger, "open_ledger", return_value=ledger),
              mock.patch.object(style_plan, "synced_style_branch", return_value=""),
              mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as footprint):
            self.assertEqual(style_plan.gate(self.repo, "modules-unit-1"), 0)
            footprint.assert_called_once()
        arguments = cast(list[str], footprint.call_args.args[0])
        self.assertIn("style_footprint.py", arguments[1])
        self.assertEqual(arguments[arguments.index("--base") + 1], self.tip)

    def test_read_gate_allows_a_yielded_file_while_checking_the_remaining_owned_cells(self) -> None:
        first, last = self.rule("read-first"), self.rule("read-last")
        self.sync()
        ledger = self.ledger()
        ledger.ticket_add("names-unit-1", "names-unit", "read", "names", "crates/alpha",
                          list(self.sources[:2]), [first, last])
        ledger.ticket_assign("names-unit-1", "style/main")
        _ = ledger.ticket_base("names-unit-1")
        before = {(cell.rule, cell.path): cell.state for cell in ledger.cells() if cell.path == self.sources[0]}
        for rule in (first, last):
            for path in self.sources[:2]:
                self.record(rule, path, self.tip, "names-unit-1")
        ledger.ticket_yield("names-unit-1", self.sources[0])
        self.assertEqual({(cell.rule, cell.path): cell.state for cell in ledger.cells() if cell.path == self.sources[0]}, before)
        with (mock.patch.object(style_ledger, "open_ledger", return_value=ledger),
              mock.patch.object(style_plan, "synced_style_branch", return_value=""),
              mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as footprint):
            self.assertEqual(style_plan.gate(self.repo, "names-unit-1"), 0)
            footprint.assert_called_once()
        with style_ledger.open_ledger(self.repo) as reopened:
            reopened.ticket_checkpoint("names-unit-1", self.tip)
            self.assertEqual(reopened.ticket_open("names-unit")[0]["state"], "checkpointed")

    def test_coverage_gate_uses_the_base_exposed_by_ticket_origin(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.assertEqual(self.run_next()[0], 0)
        ticket = self.ledger().ticket_open("names-unit")[0]
        for rule in ticket["rules"]:
            for path in ticket["files"]:
                self.record(rule, path, self.tip, ticket["id"])
        origin = style_ledger.SelectedByCoverage("base-from-origin")
        with (style_ledger.open_ledger(self.repo) as ledger,
              mock.patch.object(style_ledger, "open_ledger", return_value=ledger),
              mock.patch.object(style_ledger.Ledger, "ticket_origin", return_value=origin),
              mock.patch.object(style_plan, "synced_style_branch", return_value=""),
              mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run):
            self.assertEqual(style_plan.gate(self.repo, "names-unit-1"), 0)
        arguments = cast(list[str], run.call_args.args[0])
        self.assertEqual(arguments[arguments.index("--base") + 1], "base-from-origin")

    def take_style_work_into_judged_ticket(self) -> style_ledger.TicketRecord:
        _ = self.rule("read")
        self.sync()
        self.assertEqual(self.run_next()[0], 0)
        ticket = self.ledger().ticket_open("names-unit")[0]
        for rule in ticket["rules"]:
            for path in ticket["files"]:
                self.record(rule, path, self.tip, ticket["id"])
        self.write("incoming.md", "Documentation from the branch point.\n")
        incoming = self.commit()
        _ = self.git("merge", "--ff-only", "main", cwd=self.style)
        _ = self.git("merge", "--ff-only", "style/main", cwd=self.unit)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.unit), incoming)
        return ticket

    def test_coverage_gate_excludes_direct_branch_point_work_taken_in_from_the_style_branch(self) -> None:
        ticket = self.take_style_work_into_judged_ticket()
        result = subprocess.run(
            [sys.executable, str(Path(style_plan.__file__)), "gate", "--repo", str(self.unit), "--ticket", ticket["id"]],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "mechanical\n", ""))
        self.assertEqual(self.ledger().ticket_open("names-unit")[0]["base"], ticket["base"])
        self.assertEqual(ticket["base"], self.tip)

    def test_coverage_gate_still_reviews_ticket_edits_after_taking_in_style_branch_work(self) -> None:
        ticket = self.take_style_work_into_judged_ticket()
        _ = (self.unit / ticket["files"][0]).write_text("pub fn value() { let count = 7; }\n")
        result = subprocess.run(
            [sys.executable, str(Path(style_plan.__file__)), "gate", "--repo", str(self.unit), "--ticket", ticket["id"]],
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

    def test_finished_phases_are_discarded_before_resuming_or_waiting_for_merge(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.assertEqual(self.run_next()[0], 0)
        ledger = self.ledger()
        ledger.ticket_drop("names-unit-1")
        self.assertEqual(self.run_next()[0], 0)
        current = self.plan.read_text()
        finished = "\n### Phase 1 — names-unit-1  · status: done\nFinished instructions.\n\n"
        for state in ("assigned", "checkpointed"):
            with self.subTest(state=state):
                _ = self.plan.write_text(current.replace("### Phase 2", finished + "### Phase 2"))
                if state == "checkpointed":
                    ticket = ledger.ticket_open("names-unit")[0]
                    for rule in ticket["rules"]:
                        for path in ticket["files"]:
                            self.record(rule, path, self.tip, ticket["id"])
                    ledger.ticket_checkpoint("names-unit-2", self.tip)
                with mock.patch.object(berth_board, "read_board", side_effect=AssertionError("board read")):
                    code, output, errors = self.run_next()
                self.assertEqual((code, errors), (0 if state == "assigned" else 5, ""))
                self.assertIn("discarded names-unit-1: dropped\n", output)
                self.assertIn("resume names-unit-2" if state == "assigned" else "waiting for merge: names-unit-2", output)
                self.assertNotIn("### Phase 1", self.plan.read_text())
                self.assertIn("### Phase 2", self.plan.read_text())
                self.assertEqual(self.count_tickets(), 2)

    def test_rebuild_after_rule_edit_restores_old_pinned_text_and_instructions(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.assertEqual(self.run_next()[0], 0)
        expected = self.plan.read_bytes()
        pinned = self.ledger().pinned_rule_texts("names-unit-1")[0]
        path = self.plan.parent.parent / "rules" / (pinned.blob + ".md")
        path.unlink()
        _ = self.plan.write_text(self.plan_header())
        _ = (self.rule_dir / "read.md").write_text("---\ntier: C\nfamily: names\ninput_scope: file\n---\nChanged instructions.\n")
        self.assertEqual(self.run_next(), (0, "resume names-unit-1\n", ""))
        self.assertEqual(self.plan.read_bytes(), expected)
        self.assertEqual(path.read_bytes(), pinned.text)
        self.assertEqual(self.git("hash-object", str(path)), pinned.blob)

    def test_busy_queued_ticket_is_dropped_and_replaced_with_fresh_available_work(self) -> None:
        rule = self.rule("read")
        self.sync()
        ledger = self.ledger()
        ledger.ticket_add("names-unit-1", "names-unit", "read", "names", "crates/alpha", [self.sources[0]], [rule])
        with mock.patch.object(berth_board, "read_board", return_value=self.board(self.sources[0])):
            code, output, errors = self.run_next()
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("dropped names-unit-1: files busy", output)
        self.assertIn("phase written: names-unit-2", output)
        self.assertNotIn(self.sources[0], ledger.ticket_open("names-unit")[0]["files"])
        self.assertEqual(self.count_tickets(), 2)

    def test_checkpoint_waits_then_merged_phase_is_discarded_before_refill(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.assertEqual(self.run_next()[0], 0)
        ledger = self.ledger()
        ticket = ledger.ticket_open("names-unit")[0]
        for rule in ticket["rules"]:
            for path in ticket["files"]:
                self.record(rule, path, self.tip, ticket["id"])
        ledger.ticket_checkpoint(ticket["id"], self.tip)
        before = self.plan.read_bytes()
        with mock.patch.object(berth_board, "read_board", side_effect=AssertionError("board read")):
            self.assertEqual(self.run_next(), (5, f"waiting for merge: names-unit-1 checkpointed at {self.tip[:7]}\n", ""))
        self.assertEqual(self.plan.read_bytes(), before)
        _ = ledger.ticket_merge(ticket["id"])
        code, output, errors = self.run_next()
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("discarded names-unit-1: merged", output)
        self.assertIn("phase written: names-unit-2", output)
        self.assertNotIn("### Phase 1 — names-unit-1", self.plan.read_text())
        self.assertIn("### Phase 2 — names-unit-2", self.plan.read_text())

    def test_reconcile_dropped_ticket_phase_is_removed_after_unit_branch_disappears(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.assertEqual(self.run_next()[0], 0)
        _ = self.git("checkout", "--detach", cwd=self.unit)
        _ = self.git("branch", "-D", "demo-style-names")
        ledger = self.ledger()
        _ = ledger.reconcile("main", "style/main")
        with mock.patch.object(berth_board, "read_board", return_value=self.board(*self.sources)):
            code, output, errors = self.run_next()
        self.assertEqual((code, errors), (3, ""))
        self.assertIn("discarded names-unit-1: dropped", output)
        self.assertNotIn("### Phase 1 — names-unit-1", self.plan.read_text())


if __name__ == "__main__":
    _ = unittest.main()
