"""Selection respects product work and recovery preserves pinned instructions."""

from __future__ import annotations

import io
import os
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import cast, final, override
from unittest import mock

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
        self.repo = self.root / "sample"
        self.style = self.root / "sample-style"
        self.unit = self.root / "sample-style-names"
        self.rule_dir = self.root / "nate_style/rust"
        self.repo.mkdir()
        self.rule_dir.mkdir(parents=True)
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
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
        return style_plan.candidate_tickets(self.repo, ledger, busy, family)

    def run_next(self) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = style_plan.main(["next-phase", "--unit", "names-unit", "--plan", str(self.plan), "--repo", str(self.repo)])
        return code, output.getvalue(), errors.getvalue()

    def record(self, rule: str, path: str, commit: str, ticket: str = "outside") -> None:
        with style_ledger.open_ledger(self.repo) as ledger:
            blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule.removesuffix("#uncovered"))
            ledger.record(rule=rule, path=path, commit=commit, verdict="clean", detail="", ticket=ticket, worker="test", rule_blob=blob)

    def count_tickets(self) -> int:
        with closing(sqlite3.connect(self.ledger().database)) as connection:
            rows = cast(list[tuple[int]], connection.execute("SELECT COUNT(*) FROM tickets").fetchall())
        return rows[0][0]

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
        tickets = style_plan.candidate_tickets(self.repo, ledger, style_plan.BusyFiles(self.board(), frozenset()), "names", scope)
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
                             + f'unit: names-unit\nplan: "{self.plan} Phase 1"\nstyle_findings: [{finding.finding_id}]\n---\n')
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
        approved = style_proposals.approved_issue(folder, "names-unit", ledger)
        assert not isinstance(approved, style_proposals.NoApprovedProposal)
        self.assertEqual(approved.path, issue)
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
        for crate in ("crates/alpha", "crates/beta"):
            self.record(structure, crate, self.tip)
        reads = self.candidates()
        self.assertTrue(reads)
        self.assertEqual({rule for ticket in reads for rule in ticket.rules}, {read})
        self.assertFalse(any(cell.rule.endswith("#uncovered") for cell in ledger.cells(rule=structure)))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(style_plan.main(["queue", "--repo", str(self.repo)]), 0)
        self.assertIn("names-unit: read", output.getvalue())

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
                                             "modules", style_plan.PilotCrates(frozenset({"crates/alpha"})))
        self.assertEqual([(ticket.crate, ticket.kind) for ticket in scoped], [("crates/alpha", "structure")])

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
                             + f'unit: names-unit\nplan: "{self.plan} Phase 1"\nstyle_findings: [{finding.finding_id}]\n---\n')
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
            self.assertIn("overrides the mechanical-only invariant", first)
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
                             + f'unit: names-unit\nplan: "{self.plan} Phase 1"\nstyle_findings: [{finding.finding_id}]\n---\n')
        _ = self.doc.write_text(self.doc.read_text().replace("## Units", f"- **Proposals:** {folder}\n\n## Units"))
        with (mock.patch.object(berth_board, "read_board", return_value=self.board(self.sources[0])),
              mock.patch.object(style_proposals, "proposal_command") as start,
              mock.patch.object(style_plan, "candidate_tickets", wraps=style_plan.candidate_tickets) as coverage):
            code, output, errors = self.run_next()
            self.assertEqual(code, 3, errors)
            self.assertIn(f"waiting {issue}: files busy", output)
            self.assertEqual(self.count_tickets(), 0)
            self.assertEqual(frontmatter(issue)["stage"], ["backlog"])
            start.assert_not_called()
            coverage.assert_called_once()
        def start_issue(*args: str) -> None:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(proposal_issue.main(list(args)), 0)
        with mock.patch.object(style_proposals, "proposal_command", side_effect=start_issue):
            result = self.run_next()
        self.assertEqual(result[0], 0, result)
        ticket = self.ledger().ticket_open("names-unit")[0]
        self.assertEqual((ticket["kind"], ticket["proposal"], ticket["files"]), ("proposal", str(issue), [self.sources[0]]))
        self.assertEqual(frontmatter(issue)["stage"], ["active"])

    def test_coverage_gate_uses_the_base_exposed_by_ticket_origin(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.assertEqual(self.run_next()[0], 0)
        origin = style_ledger.SelectedByCoverage("base-from-origin")
        with (style_ledger.open_ledger(self.repo) as ledger,
              mock.patch.object(style_ledger, "open_ledger", return_value=ledger),
              mock.patch.object(style_ledger.Ledger, "ticket_origin", return_value=origin),
              mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run):
            self.assertEqual(style_plan.gate(self.repo, "names-unit-1"), 0)
        arguments = cast(list[str], run.call_args.args[0])
        self.assertEqual(arguments[arguments.index("--base") + 1], "base-from-origin")

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
        ledger.ticket_checkpoint(ticket["id"], self.tip)
        before = self.plan.read_bytes()
        with mock.patch.object(berth_board, "read_board", side_effect=AssertionError("board read")):
            self.assertEqual(self.run_next(), (5, f"waiting for merge: names-unit-1 checkpointed at {self.tip[:7]}\n", ""))
        self.assertEqual(self.plan.read_bytes(), before)
        for path in ticket["files"]:
            self.record(ticket["rules"][0], path, self.tip, ticket["id"])
        ledger.ticket_merge(ticket["id"])
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
