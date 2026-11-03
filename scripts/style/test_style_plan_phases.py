"""Pinned phase instructions and rule-specific review guidance."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from typing import final
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import style_plan
import unit_plans

import mechanical_classes
import rules
import style_ledger

from style_plan_test_support import StylePlanFixture


@final
class StylePlanTests(StylePlanFixture):
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
            + "Record a cell whose file this ticket deletes with `--verdict removed`; the ledger finds the commit that deleted it.\n"
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
                                     unit_plans.delegation_context(self.plan.read_text()))
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
        context = unit_plans.delegation_context(self.plan.read_text())
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
        self.assertIn("Every structure finding is held as a proposal, except a finding of a mechanical class below.",
                      text)
        self.assertIn("Apply a finding of a mechanical class without asking only when", text)
        self.assertIn("- `pure-move` (`module-structure`, `types-live-with-their-behavior`, "
                      + "`split-by-type-ownership`): A pure move takes one item", text)
        self.assertIn("- `stale-allow-lint` (`test-module-allow-boilerplate`): A stale lint removal", text)
        self.assertNotIn("#uncovered`", text)
        pin = self.ledger().pinned_rule_texts(ticket["id"])[0]
        self.assertIn(str(plan.parent.parent / "rules" / (pin.blob + ".md")), text)

    def test_a_third_listed_class_changes_the_unit_text_with_no_other_edit(self) -> None:
        listed = self.root / "mechanical_classes.toml"
        extra = ('\n[[class]]\nname = "third-class"\nproof = "lint_removal"\nrules = ["third-rule"]\n'
                 + 'summary = "A third change takes out one word."\n')
        _ = listed.write_text(mechanical_classes.CLASSES.read_text() + extra)
        catalog = frozenset(rule for entry in mechanical_classes.classes() for rule in entry.rules) | {"third-rule"}
        _ = self.rule("structure", "tier: D\nfamily: modules\n")
        self.sync()
        worktree = self.root / "modules"
        _ = self.git("worktree", "add", "-q", "-b", "demo-style-modules", str(worktree))
        plan = worktree / "docs/style-production/units/modules-unit.md"
        plan.parent.mkdir(parents=True)
        _ = plan.write_text(self.plan_header())
        with mock.patch.object(mechanical_classes, "classes", return_value=mechanical_classes.load(listed, catalog)):
            self.assertIsInstance(style_plan.next_phase("modules-unit", plan, self.repo), style_plan.PhaseWritten)
        text = plan.read_text()
        self.assertIn("- `pure-move` (", text)
        self.assertIn("- `third-class` (`third-rule`): A third change takes out one word.", text)

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
                                              unit_plans.delegation_context(self.plan.read_text()),
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


if __name__ == "__main__":
    _ = unittest.main()
