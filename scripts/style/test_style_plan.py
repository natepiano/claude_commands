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
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import cast, final, override
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

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
            "Hold that window's findings before opening the next window.",
            "Hold each distinct defect as its own finding; never stop at a window's first finding.",
            "Record a file's verdict for every rule only after its last window has been judged",
            "`record --rule <rule> --path <file>` for each rule/file cell.",
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
            "Hold that window's findings before opening the next window. "
            + "Hold each distinct defect as its own finding; never stop at a window's first finding.\n"
            + "A site that breaks more than one rule above is held under each rule it breaks, once per rule, "
            + "even when one change fixes both: each rule's cell records its own verdict.\n"
            + "Record a file's verdict for every rule only after its last window has been judged, ",
            self.plan.read_text())

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

    def test_written_structure_phase_keeps_its_file_list_and_procedure_without_read_windows(self) -> None:
        rule = self.rule("structure", "tier: D\nfamily: modules\n")
        self.sync()
        worktree = self.root / "modules"
        _ = self.git("worktree", "add", "-q", "-b", "demo-style-modules", str(worktree))
        plan = worktree / "docs/style-production/units/modules-unit.md"
        plan.parent.mkdir(parents=True)
        _ = plan.write_text(self.plan_header())
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

    def test_structure_work_order_shows_existing_holds_on_the_batch_files_only(self) -> None:
        rule = self.rule("structure", "tier: D\nfamily: modules\n")
        self.sync()
        ledger = self.ledger()
        blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule)
        held = ledger.hold(rule, self.sources[0], 1, "Split this module", "restructure", "before", "after", blob, "outside")
        waived = ledger.hold(rule, self.sources[0], 2, "Already waived", "restructure", "before", "after", blob, "outside")
        _ = ledger.decline_held(waived.finding_id, "declined")
        outside = ledger.hold(rule, self.sources[2], 1, "Different batch", "restructure", "before", "after", blob, "outside")
        worktree = self.root / "modules"
        _ = self.git("worktree", "add", "-q", "-b", "demo-style-modules", str(worktree))
        plan = worktree / "docs/style-production/units/modules-unit.md"
        plan.parent.mkdir(parents=True)
        _ = plan.write_text(self.plan_header())
        self.assertIsInstance(style_plan.next_phase("modules-unit", plan, self.repo), style_plan.PhaseWritten)
        text = plan.read_text()
        self.assertIn(f"{held.finding_id} · {rule} · {self.sources[0]}:1 · Split this module", text)
        self.assertIn("do not hold these again", text)
        self.assertNotIn(waived.finding_id, text)
        self.assertNotIn(outside.finding_id, text)
        self.assertIn("Judge these 2 files of `crates/alpha`", text)

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

    def test_pending_manifest_structure_cell_keeps_names_crate_closed_after_file_judgments(self) -> None:
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        manifest = self.rule("manifest", "tier: D\nfamily: modules\ninput_scope: manifest\n")
        _ = self.rule("read")
        self.sync()
        for path in self.sources:
            self.record(structure, path, self.tip)
        _ = self.plan.write_text(self.plan_header().replace("## Phases", "**Pilot scope:** alpha\n\n## Phases"))

        self.assertEqual(self.run_next(), (3, "waiting for structure: crates/alpha\n", ""))
        self.assertEqual(self.count_tickets(), 0)
        self.assertEqual([row[0] for row in self.sync_wait_rows()], ["names-unit"])
        self.record(manifest, "crates/alpha", self.tip)
        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))

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
                                             "modules", style_plan.PilotCrates(frozenset({"crates/alpha"})))
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
        self.assertEqual(style_plan.candidate_tickets(self.repo, ledger, busy, "modules")[0].crate, "crates/beta")

        tickets = style_plan.candidate_tickets(
            self.repo, ledger, busy, "modules",
            family_scopes={"modules": style_plan.AllCrates(),
                           "names": style_plan.PilotCrates(frozenset({"crates/alpha"}))})

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
        self.assertEqual(style_plan.candidate_tickets(self.repo, ledger, busy, "modules")[0].crate, "crates/beta")

        tickets = style_plan.candidate_tickets(
            self.repo, ledger, busy, "modules", family_scopes=style_plan.family_pilot_scopes(self.doc, self.repo))

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
                                  proposal=str(issue) if kind == "proposal" else "")
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
