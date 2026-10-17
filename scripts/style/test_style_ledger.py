"""Coverage follows committed inputs, the judged rule revision, and rule reach."""

from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from contextlib import closing, redirect_stdout
from datetime import datetime, timezone
from typing import TypedDict, cast, final, override
from unittest import mock

import rules
import style_ledger
import style_rules


class StatusCell(TypedDict):
    rule: str
    path: str
    crate: str
    state: str


class LedgerStatus(TypedDict):
    cells: list[StatusCell]
    unclassified_rules: list[str]


class SummaryReport(TypedDict):
    rules_done: int
    rules_total: int
    unclassified_rules: int
    cells_current_pct: float
    stale: int
    proposals: int
    waiting_to_merge_back: int
    oldest_waiting: str | None
    synced_at: str
    synced_tip: str


@final
class StyleLedgerTests(unittest.TestCase):
    root: Path = Path()
    repo: Path = Path()
    rule_dir: Path = Path()
    initial_tip: str = ""
    sources: tuple[str, ...] = (
        "crates/alpha/src/lib.rs", "crates/alpha/src/other.rs",
        "crates/beta/src/lib.rs", "crates/beta/src/other.rs",
    )

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = self.root / "sample"
        self.rule_dir = self.root / "nate_style" / "rust"
        self.repo.mkdir()
        self.rule_dir.mkdir(parents=True)
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "STYLE_LEDGER_STATE_DIR": str(self.root / "state"),
            "STYLE_HISTORY_NATE_STYLE_DIR": str(self.rule_dir.parent),
        })))
        _ = self.enterContext(mock.patch.object(style_rules, "NATE_STYLE_DIR", self.rule_dir.parent))
        _ = self.git("init", "-q", "--initial-branch=main")
        _ = self.git("config", "user.name", "Ledger tests")
        _ = self.git("config", "user.email", "ledger-tests@example.invalid")
        self.write("Cargo.toml", '[workspace]\nmembers = ["crates/alpha", "crates/beta"]\nresolver = "2"\n')
        for member in ("alpha", "beta"):
            self.write(
                f"crates/{member}/Cargo.toml",
                f'[package]\nname = "{member}"\nversion = "0.1.0"\nedition = "2021"\n',
            )
        for source in self.sources:
            self.write(source, "pub fn value() -> u32 { 1 }\n")
        self.write("vendor/third-party/src/lib.rs", "pub fn external() {}\n")
        self.write("crates/alpha/vendor/embedded.rs", "pub fn embedded() {}\n")
        self.write("README.md", "A sample workspace\n")
        self.initial_tip = self.commit("Create workspace")

    def git(self, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), *arguments], check=True, capture_output=True, text=True,
        ).stdout.strip()

    def write(self, relative: str, content: str) -> None:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(content)

    def commit(self, message: str) -> str:
        _ = self.git("add", "--all")
        _ = self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def rule(self, name: str, metadata: str = "tier: C\nfamily: names\ninput_scope: file\n") -> str:
        _ = (self.rule_dir / f"{name}.md").write_text(f"---\n{metadata}---\n# {name}\n")
        return f"global:rust/{name}.md"

    def blob(self, rule_id: str) -> str:
        parent_id = rule_id.removesuffix("#uncovered")
        return next(rule.blob for rule in rules.load_rules(self.repo) if rule.rule_id == parent_id)

    def cli(self, *arguments: str, success: bool = True) -> subprocess.CompletedProcess[str]:
        prefix = 2 if arguments[0] in ("ticket", "handoff", "tip", "held") else 1
        result = subprocess.run(
            [sys.executable, str(Path(style_ledger.__file__)), *arguments[:prefix],
             "--repo", str(self.repo), *arguments[prefix:]],
            capture_output=True, text=True, check=False,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def set_tip(self, tip: str) -> None:
        _ = self.cli("set-tip", "--tip", tip)

    def record(
        self, rule_id: str, path: str, commit: str, verdict: str = "clean", *,
        rule_blob: str = "", detail: str = "", ticket: str = "unit-1",
    ) -> None:
        _ = self.cli(
            "record", "--rule", rule_id, "--path", path, "--commit", commit,
            "--rule-blob", rule_blob or self.blob(rule_id), "--verdict", verdict,
            "--detail", detail, "--ticket", ticket, "--worker", "test-worker",
        )

    def ledger(self) -> style_ledger.Ledger:
        ledger = style_ledger.open_ledger(self.repo)
        self.addCleanup(ledger.close)
        return ledger

    def cells(self, rule_id: str) -> list[style_ledger.Cell]:
        ledger = self.ledger()
        return list(ledger.cells(rule=rule_id))

    def states(self, rule_id: str) -> dict[str, str]:
        return {cell.path: cell.state for cell in self.cells(rule_id)}

    def lint(self, tip: str, tool: str, passed: bool, diagnostics: dict[str, int]) -> None:
        diagnostic_file = self.root / "diagnostics.json"
        _ = diagnostic_file.write_text(json.dumps(diagnostics))
        _ = self.cli(
            "lint-record", "--tip", tip, "--tool", tool,
            "--passed", "1" if passed else "0", "--diagnostics", str(diagnostic_file),
        )

    def summary_report(self) -> SummaryReport:
        return cast(SummaryReport, json.loads(self.cli("summary", "--json").stdout))

    def add_ticket(self, ticket: str, rule_ids: list[str], kind: str = "read", unit: str = "read") -> None:
        _ = self.cli(
            "ticket", "add", "--id", ticket, "--unit", unit, "--kind", kind,
            "--family", "names", "--crate", "crates/alpha",
            "--files", json.dumps(list(self.sources[:2])), "--rules", json.dumps(rule_ids),
        )

    def hold_finding(self, rule: str, line: int, *, ticket: str = "external-1", rule_blob: str = "") -> str:
        return self.cli(
            "held", "add", "--rule", rule, "--path", self.sources[0], "--line", str(line),
            "--text", f"pub fn old_name_{line}() {{}}", "--kind", "rename", "--before", "old_name",
            "--after", "new_name", "--rule-blob", rule_blob or self.blob(rule), "--ticket", ticket,
        ).stdout.strip()

    def test_initial_universe_has_file_and_crate_cells_but_no_vendor_or_process_cells(self) -> None:
        file_rule = self.rule("read")
        structure = self.rule("structure", "tier: D\nfamily: modules\n")
        listed = self.rule("listed", "tier: B\nfamily: names\npre_filter: word\n")
        process = self.rule("process", "tier: E\n")
        self.set_tip(self.initial_tip)

        self.assertEqual(self.states(file_rule), dict.fromkeys(self.sources, "open"))
        self.assertEqual(self.states(structure), {"crates/alpha": "open", "crates/beta": "open"})
        self.assertEqual(self.cells(listed), [])
        self.assertEqual(self.cells(process), [])
        ledger = self.ledger()
        progress = {rule.rule_id: rule for rule in ledger.rule_counts(style_ledger.WholeRepository())}
        self.assertTrue(progress[listed].complete)
        self.assertEqual(progress[listed].total, 0)
        self.assertEqual(progress[file_rule].total, 4)
        self.assertFalse(progress[file_rule].complete)
        self.assertEqual({cell.path for cell in ledger.cells(crate="crates/alpha")}, {
            "crates/alpha", "crates/alpha/src/lib.rs", "crates/alpha/src/other.rs",
        })
        status = self.cli("status").stdout
        self.assertIn(file_rule, status)
        self.assertIn(listed, status)
        self.assertNotIn("unclassified:", status)

    def test_rule_counts_fold_components_and_filter_every_state_and_current_held_verdicts(self) -> None:
        read = self.rule("read")
        partial = self.rule(
            "partial", "tier: B\nfamily: types\ninput_scope: file\ncandidates:\n"
            + "  kind: regex\n  pattern: value\n  surface: partial\n  uncovered: other sites\n",
        )
        process = self.rule("process", "tier: E\n")
        _ = self.rule("legacy", "mechanism: llm\n")
        self.set_tip(self.initial_tip)
        self.record(read, self.sources[0], self.initial_tip, "held")
        self.record(read, self.sources[1], self.initial_tip)
        for path in self.sources:
            self.record(partial, path, self.initial_tip)
        self.record(partial + "#uncovered", self.sources[0], self.initial_tip, "held")
        self.write(self.sources[1], "pub fn value() -> u32 { 2 }\n")
        main_tip = self.commit("Make judged inputs stale")
        _ = self.cli("set-tip", "--tip", main_tip, "--style-branch", "style/main")
        _ = self.git("checkout", "-qb", "style/main")
        self.write(self.sources[2], "pub fn corrected() {}\n")
        pending_tip = self.commit("Judge a pending style correction")
        self.record(read, self.sources[2], pending_tip, "held")
        ledger = self.ledger()

        with mock.patch.object(ledger, "_coverage", wraps=cast(object, getattr(ledger, "_coverage"))) as coverage:
            repository_counts = ledger.rule_counts(style_ledger.WholeRepository())
            coverage.assert_called_once_with(None, None)
        by_rule = {row.rule_id: row for row in repository_counts}
        self.assertEqual(by_rule, {
            read: style_ledger.RuleCounts(read, "C", 1, 1, 1, 1, 1),
            partial: style_ledger.RuleCounts(partial, "B", 4, 1, 3, 0, 1),
            process: style_ledger.RuleCounts(process, "E", 0, 0, 0, 0, 0),
        })
        self.assertEqual({row.rule_id: (row.total, row.complete) for row in repository_counts}, {
            read: (4, False), partial: (8, False), process: (0, True),
        })
        text_rows = [line.split("\t") for line in self.cli("status").stdout.splitlines()
                     if line.startswith("global:")]
        self.assertEqual({fields[0]: tuple(int(value) for value in fields[1:6]) for fields in text_rows}, {
            row.rule_id: (row.current, row.stale, row.open, row.awaiting_merge, row.held)
            for row in repository_counts
        })
        for crate, expected in (
            ("crates/alpha", {
                read: style_ledger.RuleCounts(read, "C", 1, 1, 0, 0, 1),
                partial: style_ledger.RuleCounts(partial, "B", 2, 1, 1, 0, 1),
                process: by_rule[process],
            }),
            ("crates/beta", {
                read: style_ledger.RuleCounts(read, "C", 0, 0, 1, 1, 0),
                partial: style_ledger.RuleCounts(partial, "B", 2, 0, 2, 0, 0),
                process: by_rule[process],
            }),
        ):
            with self.subTest(crate=crate):
                with mock.patch.object(ledger, "_coverage", wraps=cast(object, getattr(ledger, "_coverage"))) as coverage:
                    scoped = ledger.rule_counts(style_ledger.CrateCoverage(crate))
                    coverage.assert_called_once_with(None, crate)
                self.assertEqual({row.rule_id: row for row in scoped}, expected)

        finding = self.hold_finding(read, 1)
        ledger.file_held(finding, self.root / "proposal.md")
        ledger.handoff_add(pending_tip)
        with mock.patch.object(ledger, "_coverage", side_effect=AssertionError("summary_of rescanned cells")):
            summary = ledger.summary_of(repository_counts)
        self.assertEqual(summary, ledger.summary())
        self.assertEqual((summary.rules_done, summary.rules_total, summary.stale, summary.proposals), (0, 2, 2, 1))
        self.assertAlmostEqual(summary.cells_current_pct, 100 * 5 / 12)
        self.assertEqual(summary.unclassified_rules, 1)
        self.assertIsInstance(summary.waiting, style_ledger.PendingHandoffs)

    def test_status_crate_counts_answer_for_each_crate(self) -> None:
        read = self.rule("read")
        self.set_tip(self.initial_tip)
        for path in self.sources[:2]:
            self.record(read, path, self.initial_tip)

        output = self.cli("status", "--crate", "crates/alpha").stdout

        self.assertEqual(output,
                         "rule\tcurrent\tstale\topen\tawaiting merge\theld\tdone\n"
                         + "global:rust/read.md\t2\t0\t0\t0\t0\ttrue\n")
        beta = self.cli("status", "--crate", "crates/beta").stdout
        self.assertIn(read + "\t0\t0\t2\t0\t0\tfalse\n", beta)
        whole = self.cli("status", "--rule", read).stdout
        self.assertIn(read + "\t2\t0\t2\t0\t0\tfalse\n", whole)

    def test_status_reuses_repository_counts_and_scans_again_only_for_a_selector(self) -> None:
        read = self.rule("read")
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        for rule, scope, passes in ((None, style_ledger.WholeRepository(), 1),
                                    (read, style_ledger.WholeRepository(), 2),
                                    (None, style_ledger.CrateCoverage("crates/alpha"), 2)):
            with self.subTest(rule=rule, scope=scope):
                output = io.StringIO()
                with (
                    redirect_stdout(output),
                    mock.patch.object(ledger, "_coverage", wraps=cast(object, getattr(ledger, "_coverage"))) as coverage,
                ):
                    ledger.status(rule=rule, scope=scope)
                    self.assertEqual(coverage.call_count, passes)
                self.assertIn(read + "\t", output.getvalue())

    def test_status_cli_converts_omitted_empty_and_named_crates_to_coverage_scopes(self) -> None:
        read = self.rule("read")
        _ = self.git("mv", "crates/alpha", "alpha")
        self.write("Cargo.toml", '[workspace]\nmembers = ["alpha", "crates/beta"]\nresolver = "2"\n')
        tip = self.commit("Move alpha crate to the workspace root")
        self.set_tip(tip)
        self.record(read, "alpha/src/lib.rs", tip)
        header = "rule\tcurrent\tstale\topen\tawaiting merge\theld\tdone\n"
        whole = header + "global:rust/read.md\t1\t0\t3\t0\t0\tfalse\n"
        selected = header + "global:rust/read.md\t1\t0\t1\t0\t0\tfalse\n"
        for arguments, scope, expected in (
            ([], style_ledger.WholeRepository(), whole),
            (["--crate", ""], style_ledger.WholeRepository(), whole),
            (["--crate", "alpha"], style_ledger.CrateCoverage("alpha"), selected),
        ):
            with self.subTest(arguments=arguments):
                output = io.StringIO()
                with (
                    mock.patch.object(sys, "argv", ["style_ledger.py", "status", "--repo", str(self.repo), *arguments]),
                    mock.patch.object(style_ledger.Ledger, "status", autospec=True,
                                      side_effect=style_ledger.Ledger.status) as status,
                    redirect_stdout(output),
                ):
                    style_ledger.main()
                status.assert_called_once_with(mock.ANY, None, scope, False)
                self.assertEqual(output.getvalue(), expected)

    def test_status_uncovered_selector_prints_only_component_counts_and_parent_done(self) -> None:
        partial = self.rule(
            "partial", "tier: B\nfamily: names\ninput_scope: file\ncandidates:\n"
            + "  kind: regex\n  pattern: value\n  surface: partial\n  uncovered: other sites\n",
        )
        self.set_tip(self.initial_tip)
        for path in self.sources:
            self.record(partial + "#uncovered", path, self.initial_tip,
                        "held" if path == self.sources[0] else "clean")

        output = self.cli("status", "--rule", partial + "#uncovered").stdout

        self.assertEqual(output,
                         "rule\tcurrent\tstale\topen\tawaiting merge\theld\tdone\n"
                         + "global:rust/partial.md\t4\t0\t0\t0\t1\tfalse\n")
        self.assertEqual(self.cli("status", "--rule", partial).stdout,
                         "rule\tcurrent\tstale\topen\tawaiting merge\theld\tdone\n"
                         + "global:rust/partial.md\t4\t0\t4\t0\t1\tfalse\n")

    def test_zero_site_partial_rule_requires_uncovered_cells_and_counts_only_its_parent(self) -> None:
        rule_id = self.rule(
            "partial", "tier: B\nfamily: names\ninput_scope: file\ncandidates:\n"
            + "  kind: regex\n  pattern: absent_marker\n  surface: partial\n  uncovered: other sites\n",
        )
        uncovered = rule_id + "#uncovered"
        self.set_tip(self.initial_tip)
        ledger = self.ledger()

        self.assertEqual(ledger.listed_sites(rule_id), ())
        self.assertEqual(self.states(uncovered), dict.fromkeys(self.sources, "open"))
        self.assertEqual({cell.rule for cell in ledger.cells()}, {uncovered})
        self.assertEqual({cell.tier for cell in ledger.cells()}, {"C"})
        self.assertEqual({cell.family for cell in ledger.cells()}, {"names"})
        progress, = ledger.rule_counts(style_ledger.WholeRepository())
        self.assertEqual((progress.rule_id, progress.current, progress.total, progress.complete),
                         (rule_id, 0, 4, False))

        for path in self.sources:
            self.record(uncovered, path, self.initial_tip)

        progress, = ledger.rule_counts(style_ledger.WholeRepository())
        self.assertEqual((progress.rule_id, progress.current, progress.total, progress.complete),
                         (rule_id, 4, 4, True))
        row = next(line.split("\t") for line in self.cli("status").stdout.splitlines()
                   if line.startswith(rule_id + "\t"))
        self.assertEqual((row[1], row[-1]), ("4", "true"))
        self.assertNotIn(uncovered + "\t", self.cli("status").stdout)

    def test_partial_listed_rule_is_done_only_after_both_components_are_current(self) -> None:
        rule_id = self.rule(
            "partial", "tier: B\nfamily: types\ninput_scope: file\ncandidates:\n"
            + "  kind: regex\n  pattern: value\n  surface: partial\n  uncovered: other sites\n",
        )
        uncovered = rule_id + "#uncovered"
        self.set_tip(self.initial_tip)
        for path in self.sources:
            self.record(uncovered, path, self.initial_tip)
        ledger = self.ledger()
        progress, = ledger.rule_counts(style_ledger.WholeRepository())
        self.assertEqual((progress.current, progress.total, progress.complete), (4, 8, False))

        for path in self.sources:
            self.record(rule_id, path, self.initial_tip)

        progress, = ledger.rule_counts(style_ledger.WholeRepository())
        self.assertEqual((progress.rule_id, progress.current, progress.total, progress.complete),
                         (rule_id, 8, 8, True))
        rule_path = self.rule_dir / "partial.md"
        _ = rule_path.write_text(rule_path.read_text() + "Another requirement\n")
        self.assertEqual({cell.state for cell in ledger.cells()}, {"stale"})

    def test_partial_structure_rule_keeps_crate_cells_without_uncovered_pseudo_rule(self) -> None:
        rule_id = self.rule(
            "structure", "tier: D\nfamily: modules\ncandidates:\n  kind: regex\n"
            + "  pattern: value\n  surface: partial\n  uncovered: crate structure\n",
        )
        self.set_tip(self.initial_tip)
        ledger = self.ledger()

        self.assertEqual({site.path for site in ledger.listed_sites(rule_id)}, set(self.sources))
        self.assertEqual({(cell.rule, cell.path, cell.tier) for cell in ledger.cells()}, {
            (rule_id, "crates/alpha", "D"), (rule_id, "crates/beta", "D"),
        })
        self.record(rule_id, "crates/alpha", self.initial_tip)
        self.record(rule_id, "crates/beta", self.initial_tip)
        progress, = ledger.rule_counts(style_ledger.WholeRepository())
        self.assertEqual((progress.rule_id, progress.current, progress.total, progress.complete),
                         (rule_id, 2, 2, True))

    def test_pre_filter_lists_matching_rust_lines_and_accepts_a_file_judgment(self) -> None:
        rule_id = self.rule("words", "tier: B\nfamily: names\ninput_scope: file\npre_filter: '\\bbanned\\b'\n")
        self.write(self.sources[0], "// banned name\npub fn banned() {}\n// unbanned\n")
        tip = self.commit("Add forbidden word hits")
        self.set_tip(tip)
        ledger = self.ledger()

        self.assertEqual([(site.rule, site.path, site.crate, site.line, site.text)
                          for site in ledger.listed_sites(rule_id)], [
            (rule_id, self.sources[0], "crates/alpha", 1, "// banned name"),
            (rule_id, self.sources[0], "crates/alpha", 2, "pub fn banned() {}"),
        ])
        self.assertEqual(self.states(rule_id), {self.sources[0]: "open"})
        self.record(rule_id, self.sources[0], tip)
        self.assertEqual(self.states(rule_id), {self.sources[0]: "current"})
        progress, = ledger.rule_counts(style_ledger.WholeRepository())
        self.assertTrue(progress.complete)

    def test_shell_sites_are_rule_specific_file_cells_even_for_crate_scoped_rules(self) -> None:
        rule_id = self.rule(
            "nextest", "tier: B\nfamily: values\ninput_scope: crate\ncandidates:\n"
            + "  kind: regex\n  pattern: cargo test\n  globs: ['*.sh']\n  surface: whole\n",
        )
        read_rule = self.rule("read")
        script = "crates/alpha/check.sh"
        self.write(script, "#!/bin/sh\ncargo test\ncargo test --workspace\n")
        tip = self.commit("Add test script")
        self.set_tip(tip)
        ledger = self.ledger()

        self.assertEqual([(site.path, site.crate, site.line) for site in ledger.listed_sites(rule_id)], [
            (script, "crates/alpha", 2), (script, "crates/alpha", 3),
        ])
        self.assertEqual(self.states(rule_id), {script: "open"})
        self.assertEqual(set(self.states(read_rule)), set(self.sources))
        self.record(rule_id, script, tip)
        self.assertEqual(self.states(rule_id), {script: "current"})

        self.write(self.sources[0], "pub fn unrelated_change() {}\n")
        self.set_tip(self.commit("Change source in script crate"))
        self.assertEqual(self.states(rule_id), {script: "current"})
        self.write(script, "#!/bin/sh\ncargo test --all-features\n")
        self.set_tip(self.commit("Change listed script"))
        self.assertEqual(self.states(rule_id), {script: "stale"})

    def test_site_export_uses_synced_objects_and_excludes_untracked_vendor_and_ignored_paths(self) -> None:
        rule_id = self.rule(
            "scripts", "tier: B\nfamily: values\ncandidates:\n  kind: regex\n"
            + "  pattern: cargo test\n  globs: ['*.sh']\n  surface: whole\n",
        )
        self.write("check.sh", "cargo test\n")
        self.write("vendor/check.sh", "cargo test\n")
        self.write("crates/alpha/vendor/check.sh", "cargo test\n")
        self.write("ignored/check.sh", "cargo test\n")
        self.write("rustfmt.toml", 'ignore = ["ignored"]\n')
        tip = self.commit("Add tracked scripts and exclusions")
        self.write("check.sh", "cargo nextest run\n")
        self.write("untracked.sh", "cargo test\n")
        self.set_tip(tip)

        self.assertEqual([(site.path, site.line, site.text) for site in self.ledger().listed_sites(rule_id)],
                         [("check.sh", 1, "cargo test")])
        self.assertEqual(self.states(rule_id), {"check.sh": "open"})

    def test_listed_sites_ignore_archive_attributes_and_preserve_committed_text(self) -> None:
        listed = self.rule(
            "attributes", "tier: B\nfamily: names\ninput_scope: file\ncandidates:\n"
            + "  kind: regex\n  pattern: attribute_marker\n  surface: whole\n",
        )
        words = self.rule("words", "tier: B\nfamily: names\npre_filter: attribute_marker\n")
        ignored, substituted = self.sources[:2]
        ignored_text = "// attribute_marker in an export-ignored file"
        substituted_text = "// attribute_marker $Format:%H$"
        self.write(ignored, ignored_text + "\n")
        self.write(substituted, substituted_text + "\n")
        self.write(".gitattributes", f"{ignored} export-ignore\n{substituted} export-subst\n")
        self.set_tip(self.commit("Add archive attributes to tracked sources"))

        status = cast(LedgerStatus, json.loads(self.cli("status", "--json").stdout))

        self.assertEqual({(cell["rule"], cell["path"], cell["state"]) for cell in status["cells"]}, {
            (rule_id, path, "open") for rule_id in (listed, words) for path in (ignored, substituted)
        })
        ledger = self.ledger()
        for rule_id in (listed, words):
            with self.subTest(rule=rule_id):
                self.assertEqual([(site.path, site.line, site.text) for site in ledger.listed_sites(rule_id)], [
                    (ignored, 1, ignored_text), (substituted, 1, substituted_text),
                ])

    def test_member_listed_and_uncovered_components_stay_inside_member_reach(self) -> None:
        local_dir = self.repo / "docs/alpha/style"
        local_dir.mkdir(parents=True)
        _ = (local_dir / "local.md").write_text(
            "---\ntier: B\nfamily: names\ninput_scope: file\ncandidates:\n  kind: regex\n"
            + "  pattern: value\n  surface: partial\n  uncovered: other names\n---\n# Local\n"
        )
        rule_id = "local:docs/alpha/style/local.md"
        self.set_tip(self.initial_tip)
        ledger = self.ledger()

        self.assertEqual({site.path for site in ledger.listed_sites(rule_id)}, set(self.sources[:2]))
        self.assertEqual({(cell.rule, cell.path) for cell in ledger.cells()}, {
            (component, path) for component in (rule_id, rule_id + "#uncovered") for path in self.sources[:2]
        })
        self.assertEqual(ledger.listed_sites(rule_id, crate="crates/beta"), ())
        for component in (rule_id, rule_id + "#uncovered"):
            with self.subTest(component=component):
                refusal = self.cli(
                    "record", "--rule", component, "--path", self.sources[2], "--commit", self.initial_tip,
                    "--rule-blob", self.blob(rule_id), "--verdict", "clean", "--ticket", "unit-1",
                    "--worker", "test-worker", success=False,
                )
                self.assertEqual(refusal.returncode, 2)
                self.assertIn(component, refusal.stderr)

    def test_seen_tip_refreshes_listed_sites_after_rule_edit_without_another_sync(self) -> None:
        rule_id = self.rule(
            "listed", "tier: B\nfamily: names\ninput_scope: file\ncandidates:\n"
            + "  kind: regex\n  pattern: first_marker\n  surface: whole\n",
        )
        self.write(self.sources[0], "pub fn first_marker() {}\n")
        self.write(self.sources[2], "pub fn second_marker() {}\n")
        tip = self.commit("Candidate markers")
        self.set_tip(tip)
        _ = self.cli("tip", "done", "--tip", tip)
        self.assertEqual(self.cli("tip", "seen", "--tip", tip).stdout.strip(), "seen")
        self.assertEqual({site.path for site in self.ledger().listed_sites(rule_id)}, {self.sources[0]})
        path = self.rule_dir / "listed.md"
        _ = path.write_text(path.read_text().replace("first_marker", "second_marker"))
        self.assertEqual(self.cli("tip", "seen", "--tip", tip).stdout.strip(), "seen")
        self.assertEqual({site.path for site in self.ledger().listed_sites(rule_id)}, {self.sources[2]})
        self.assertEqual({cell.path for cell in self.ledger().cells(rule_id)}, {self.sources[2]})

    def test_sync_without_style_branch_keeps_unmerged_judgments_awaiting_merge(self) -> None:
        rule_id = self.rule("read")
        _ = self.cli("set-tip", "--tip", self.initial_tip, "--style-branch", "style/main")
        _ = self.git("checkout", "-qb", "style/main")
        self.write(self.sources[0], "pub fn corrected() {}\n")
        checked = self.commit("Correct style source")
        self.record(rule_id, self.sources[0], checked, "fixed")
        _ = self.git("checkout", "main")
        self.write("README.md", "Unrelated parent change\n")
        synced = self.commit("Advance parent")
        self.set_tip(synced)
        self.assertEqual(self.states(rule_id)[self.sources[0]], "awaiting merge")

    def test_status_refreshes_changed_generator_at_the_same_tip(self) -> None:
        rule_id = self.rule(
            "listed", "tier: B\nfamily: names\ninput_scope: file\ncandidates:\n"
            + "  kind: regex\n  pattern: first_marker\n  surface: whole\n",
        )
        self.write(self.sources[0], "pub fn first_marker() {}\n")
        self.write(self.sources[2], "pub fn second_marker() {}\n")
        tip = self.commit("Add distinct candidate markers")
        self.set_tip(tip)
        self.record(rule_id, self.sources[0], tip)
        ledger = self.ledger()
        self.assertEqual({site.path for site in ledger.listed_sites(rule_id)}, {self.sources[0]})
        rule_path = self.rule_dir / "listed.md"
        _ = rule_path.write_text(rule_path.read_text().replace("first_marker", "second_marker"))

        status = cast(LedgerStatus, json.loads(self.cli("status", "--json").stdout))

        self.assertEqual([(cell["rule"], cell["path"], cell["state"]) for cell in status["cells"]],
                         [(rule_id, self.sources[2], "open")])
        self.assertEqual({site.path for site in ledger.listed_sites(rule_id)}, {self.sources[2]})
        self.assertEqual({cell.path for cell in ledger.cells()}, {self.sources[2]})

    def test_same_tip_refresh_adds_newly_classified_rules_and_removes_legacy_sites(self) -> None:
        rule_id = self.rule("legacy", "mechanism: llm\ncandidates:\n  kind: regex\n  pattern: value\n")
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        self.assertEqual(ledger.listed_sites(rule_id), ())
        self.assertEqual(list(ledger.cells()), [])
        _ = self.rule(
            "legacy", "tier: B\nfamily: names\ninput_scope: file\ncandidates:\n"
            + "  kind: regex\n  pattern: value\n  surface: whole\n",
        )

        status = cast(LedgerStatus, json.loads(self.cli("status", "--json").stdout))

        self.assertEqual({cell["path"] for cell in status["cells"]}, set(self.sources))
        self.assertEqual(status["unclassified_rules"], [])
        self.assertEqual({site.path for site in ledger.listed_sites(rule_id)}, set(self.sources))
        _ = self.rule("legacy", "mechanism: llm\ncandidates:\n  kind: regex\n  pattern: value\n")
        self.assertEqual(ledger.listed_sites(rule_id), ())
        self.assertEqual(list(ledger.cells()), [])

    def test_zero_site_markers_survive_repeated_reads_and_refresh_to_new_sites(self) -> None:
        rule_id = self.rule(
            "empty", "tier: B\nfamily: names\ncandidates:\n  kind: regex\n"
            + "  pattern: absent_marker\n  surface: whole\n",
        )
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        for _ in range(2):
            self.assertEqual(ledger.listed_sites(rule_id), ())
            progress, = ledger.rule_counts(style_ledger.WholeRepository())
            self.assertTrue(progress.complete)
        with closing(sqlite3.connect(ledger.database)) as connection:
            markers = cast(list[tuple[str, str, str]], connection.execute(
                "SELECT rule,rule_blob,tip FROM listed_rules",
            ).fetchall())
        self.assertEqual(markers, [(rule_id, self.blob(rule_id), self.initial_tip)])

        rule_path = self.rule_dir / "empty.md"
        _ = rule_path.write_text(rule_path.read_text().replace("absent_marker", "value"))

        self.assertEqual({site.path for site in ledger.listed_sites(rule_id)}, set(self.sources))
        progress, = ledger.rule_counts(style_ledger.WholeRepository())
        self.assertFalse(progress.complete)

    def test_pre_filter_also_reads_non_rust_files_named_by_other_listed_rules(self) -> None:
        _ = self.rule(
            "scripts", "tier: B\nfamily: values\ncandidates:\n  kind: regex\n"
            + "  pattern: cargo test\n  globs: ['*.sh']\n  surface: whole\n",
        )
        words = self.rule("words", "tier: B\nfamily: names\npre_filter: banned\n")
        self.write("check.sh", "cargo test # banned\n")
        self.write("unlisted.md", "banned\n")
        self.set_tip(self.commit("Add non-Rust word hits"))

        self.assertEqual([(site.path, site.line) for site in self.ledger().listed_sites(words)], [("check.sh", 1)])
        self.assertEqual(self.states(words), {"check.sh": "open"})

    def test_version_two_migration_preserves_judgments_and_fills_listed_sites(self) -> None:
        read_rule = self.rule("read")
        listed = self.rule(
            "listed", "tier: B\nfamily: names\ninput_scope: file\ncandidates:\n"
            + "  kind: regex\n  pattern: value\n  surface: whole\n",
        )
        state_file = self.root / "state/sample/ledger.sqlite"
        state_file.parent.mkdir(parents=True)
        with closing(sqlite3.connect(state_file)) as connection:
            _ = connection.executescript(style_ledger.MIGRATIONS[1])
            _ = connection.executescript(style_ledger.MIGRATIONS[2])
            _ = connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
            _ = connection.execute("INSERT INTO schema_version VALUES (2)")
            _ = connection.execute("INSERT INTO sync VALUES (1, ?, ?)", (self.initial_tip, "refs/heads/style/main"))
            _ = connection.execute(
                'INSERT INTO judgments(rule,rule_blob,input_scope,input_fingerprint,path,"commit",verdict,verdict_detail,ticket,worker,recorded_at) '
                + 'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (read_rule, self.blob(read_rule), "file", self.git("rev-parse", f"{self.initial_tip}:{self.sources[0]}"),
                 self.sources[0], self.initial_tip, "clean", "", "old-ticket", "worker", "2026-10-16T00:00:00+00:00"),
            )
            connection.commit()

        ledger = self.ledger()

        self.assertEqual({cell.path: cell.state for cell in ledger.cells(read_rule)}, {
            self.sources[0]: "current", **dict.fromkeys(self.sources[1:], "open"),
        })
        self.assertEqual({site.path for site in ledger.listed_sites(listed)}, set(self.sources))
        with closing(sqlite3.connect(state_file)) as connection:
            versions = cast(list[tuple[int]], connection.execute("SELECT version FROM schema_version").fetchall())
            sync = cast(list[tuple[str, str]], connection.execute("SELECT tip,style_branch FROM sync").fetchall())
        self.assertEqual(versions, [(6,)])
        self.assertEqual(sync, [(self.initial_tip, "refs/heads/style/main")])

    def test_file_judgment_becomes_stale_only_for_changed_committed_file(self) -> None:
        rule_id = self.rule("read")
        self.set_tip(self.initial_tip)
        for path in self.sources:
            self.record(rule_id, path, self.initial_tip)
        self.assertEqual(self.states(rule_id), dict.fromkeys(self.sources, "current"))
        ledger = self.ledger()
        progress, = ledger.rule_counts(style_ledger.WholeRepository())
        self.assertTrue(progress.complete)
        self.assertEqual((progress.current, progress.total), (4, 4))

        self.write(self.sources[0], "pub fn value() -> u32 { 2 }\n")
        tip = self.commit("Change alpha source")
        self.set_tip(tip)

        self.assertEqual(self.states(rule_id), {**dict.fromkeys(self.sources, "current"), self.sources[0]: "stale"})
        self.record(rule_id, self.sources[0], tip)
        self.assertEqual(self.states(rule_id), dict.fromkeys(self.sources, "current"))

    def test_crate_scope_stales_sibling_files_but_not_the_other_crate(self) -> None:
        rule_id = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        self.set_tip(self.initial_tip)
        for path in self.sources:
            self.record(rule_id, path, self.initial_tip)
        self.write(self.sources[0], "pub fn value() -> u32 { 2 }\n")
        self.set_tip(self.commit("Change one alpha file"))

        self.assertEqual(self.states(rule_id), {
            self.sources[0]: "stale", self.sources[1]: "stale",
            self.sources[2]: "current", self.sources[3]: "current",
        })

    def test_manifest_scope_ignores_source_changes_and_tracks_every_workspace_manifest(self) -> None:
        rule_id = self.rule("manifests", "tier: D\nfamily: modules\ninput_scope: manifest\n")
        self.set_tip(self.initial_tip)
        for crate in ("crates/alpha", "crates/beta"):
            self.record(rule_id, crate, self.initial_tip)
        self.write(self.sources[0], "pub fn value() -> u32 { 3 }\n")
        self.set_tip(self.commit("Change source only"))
        self.assertEqual(self.states(rule_id), {"crates/alpha": "current", "crates/beta": "current"})

        manifest = self.repo / "crates" / "beta" / "Cargo.toml"
        _ = manifest.write_text(manifest.read_text() + '\n[features]\nextra = []\n')
        self.set_tip(self.commit("Change beta manifest"))
        self.assertEqual(self.states(rule_id), {"crates/alpha": "stale", "crates/beta": "stale"})

    def test_record_fingerprints_git_objects_even_with_dirty_working_files(self) -> None:
        rule_id = self.rule("read")
        self.set_tip(self.initial_tip)
        self.write(self.sources[0], "pub fn uncommitted() {}\n")

        self.record(rule_id, self.sources[0], self.initial_tip)

        self.assertEqual(self.states(rule_id)[self.sources[0]], "current")
        self.set_tip(self.commit("Commit previously dirty file"))
        self.assertEqual(self.states(rule_id)[self.sources[0]], "stale")

    def test_crate_and_manifest_fingerprints_ignore_dirty_working_inputs(self) -> None:
        crate_rule = self.rule("crate", "tier: C\nfamily: types\ninput_scope: crate\n")
        manifest_rule = self.rule("manifest", "tier: D\nfamily: modules\ninput_scope: manifest\n")
        self.set_tip(self.initial_tip)
        self.write(self.sources[0], "pub fn uncommitted() {}\n")
        manifest = self.repo / "crates" / "beta" / "Cargo.toml"
        _ = manifest.write_text(manifest.read_text() + '\n[features]\nextra = []\n')

        self.record(crate_rule, self.sources[0], self.initial_tip)
        self.record(manifest_rule, "crates/alpha", self.initial_tip)

        self.assertEqual(self.states(crate_rule)[self.sources[0]], "current")
        self.assertEqual(self.states(manifest_rule)["crates/alpha"], "current")
        self.set_tip(self.commit("Commit changed crate and manifest"))
        self.assertEqual(self.states(crate_rule)[self.sources[0]], "stale")
        self.assertEqual(self.states(manifest_rule)["crates/alpha"], "stale")

    def test_rustfmt_ignored_files_are_excluded_and_unowned_sources_use_root_crate(self) -> None:
        rule_id = self.rule("read")
        self.write("rustfmt.toml", 'ignore = ["crates/alpha/src/other.rs"]\n')
        self.write("tools/check.rs", "fn main() {}\n")
        self.set_tip(self.commit("Add ignored source and standalone tool"))

        cells = self.cells(rule_id)

        self.assertEqual({cell.path for cell in cells}, {
            self.sources[0], self.sources[2], self.sources[3], "tools/check.rs",
        })
        self.assertEqual({cell.crate for cell in cells if cell.path == "tools/check.rs"}, {"."})

    def test_member_structure_rule_has_only_its_member_crate(self) -> None:
        local_dir = self.repo / "docs" / "alpha" / "style"
        local_dir.mkdir(parents=True)
        _ = (local_dir / "structure.md").write_text("---\ntier: D\nfamily: modules\n---\n# Structure\n")
        local_id = "local:docs/alpha/style/structure.md"
        self.set_tip(self.initial_tip)

        self.assertEqual(self.states(local_id), {"crates/alpha": "open"})
        self.record(local_id, "crates/alpha", self.initial_tip)
        self.assertEqual(self.states(local_id), {"crates/alpha": "current"})

    def test_rule_edit_stales_all_its_cells_and_old_supplied_blob_cannot_refresh_them(self) -> None:
        rule_id = self.rule("read")
        old_blob = self.blob(rule_id)
        self.set_tip(self.initial_tip)
        for path in self.sources:
            self.record(rule_id, path, self.initial_tip, rule_blob=old_blob)
        path = self.rule_dir / "read.md"
        _ = path.write_text(path.read_text() + "Additional rule requirement\n")

        self.assertEqual(self.states(rule_id), dict.fromkeys(self.sources, "stale"))
        self.record(rule_id, self.sources[0], self.initial_tip, rule_blob=old_blob)
        self.assertEqual(self.states(rule_id)[self.sources[0]], "stale")
        self.record(rule_id, self.sources[0], self.initial_tip)
        self.assertEqual(self.states(rule_id)[self.sources[0]], "current")

    def test_exception_held_and_waived_expire_when_their_input_changes(self) -> None:
        rule_id = self.rule("read")
        self.set_tip(self.initial_tip)
        for path, verdict in zip(self.sources, ("exception", "held", "waived")):
            self.record(rule_id, path, self.initial_tip, verdict, detail="Reviewed decision")
        self.assertEqual(self.states(rule_id), {
            **dict.fromkeys(self.sources[:3], "current"), self.sources[3]: "open",
        })
        for path in self.sources[:3]:
            self.write(path, "pub fn changed() {}\n")
        self.set_tip(self.commit("Change reviewed inputs"))
        self.assertEqual(self.states(rule_id), {
            **dict.fromkeys(self.sources[:3], "stale"), self.sources[3]: "open",
        })

    def test_style_branch_judgment_awaits_merge_then_becomes_current(self) -> None:
        rule_id = self.rule("read")
        _ = self.cli("set-tip", "--tip", self.initial_tip, "--style-branch", "style/main")
        _ = self.git("checkout", "-q", "-b", "style/main")
        self.write(self.sources[0], "pub fn corrected() {}\n")
        style_tip = self.commit("Correct source on style branch")
        self.record(rule_id, self.sources[0], style_tip, "fixed", detail=style_tip)

        self.assertEqual(self.states(rule_id)[self.sources[0]], "awaiting merge")

        _ = self.git("checkout", "-q", "main")
        _ = self.git("merge", "--ff-only", "style/main")
        self.set_tip(style_tip)
        self.assertEqual(self.states(rule_id)[self.sources[0]], "current")

    def test_pending_judgment_expires_when_style_branch_inputs_change_again(self) -> None:
        rule_id = self.rule("read")
        _ = self.cli("set-tip", "--tip", self.initial_tip, "--style-branch", "style/main")
        _ = self.git("checkout", "-q", "-b", "style/main")
        self.write(self.sources[0], "pub fn corrected() {}\n")
        checked_tip = self.commit("Correct source on style branch")
        self.record(rule_id, self.sources[0], checked_tip, "fixed", detail=checked_tip)
        self.assertEqual(self.states(rule_id)[self.sources[0]], "awaiting merge")

        self.write(self.sources[0], "pub fn subsequently_changed() {}\n")
        _ = self.commit("Change checked source again")

        self.assertEqual(self.states(rule_id)[self.sources[0]], "stale")

    def test_pending_judgment_uses_explicit_style_branch_instead_of_worker_ref(self) -> None:
        rule_id = self.rule("read")
        _ = self.git("checkout", "-q", "-b", "style/main")
        _ = self.cli("set-tip", "--tip", self.initial_tip, "--style-branch", "style/main")
        self.write(self.sources[0], "pub fn corrected() {}\n")
        checked = self.commit("Correct source")
        _ = self.git("branch", "worker", checked)
        self.write(self.sources[0], "pub fn changed_again() {}\n")
        advanced = self.commit("Advance style branch")
        self.record(rule_id, self.sources[0], checked, "fixed")

        self.assertEqual(self.states(rule_id)[self.sources[0]], "stale")
        self.record(rule_id, self.sources[0], advanced, "fixed")
        self.assertEqual(self.states(rule_id)[self.sources[0]], "awaiting merge")

    def test_unmerged_judgment_without_configured_style_branch_is_stale(self) -> None:
        rule_id = self.rule("read")
        self.set_tip(self.initial_tip)
        _ = self.git("checkout", "-q", "-b", "style/main")
        self.write(self.sources[0], "pub fn corrected() {}\n")
        checked = self.commit("Correct source")
        self.record(rule_id, self.sources[0], checked)

        self.assertEqual(self.states(rule_id)[self.sources[0]], "stale")

    def test_implicit_dependency_member_has_its_own_crate_and_fingerprint(self) -> None:
        rule_id = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        manifest = self.repo / "crates/alpha/Cargo.toml"
        _ = manifest.write_text(manifest.read_text() + '\n[dependencies]\nhelper = { path = "../helper" }\n')
        self.write("crates/helper/Cargo.toml", '[package]\nname = "helper"\nversion = "0.1.0"\n')
        source = "crates/helper/src/lib.rs"
        self.write(source, "pub fn helper() {}\n")
        tip = self.commit("Add implicit member")
        self.set_tip(tip)
        self.record(rule_id, source, tip)

        self.assertEqual({cell.crate for cell in self.cells(rule_id) if cell.path == source}, {"crates/helper"})
        self.write(self.sources[0], "pub fn changed() {}\n")
        self.set_tip(self.commit("Change another member"))
        self.assertEqual(self.states(rule_id)[source], "current")

    def test_record_at_head_resolves_new_commit_on_an_open_ledger(self) -> None:
        rule_id = self.rule("read")
        ledger = self.ledger()
        blob = self.blob(rule_id)
        ledger.set_tip("HEAD")
        ledger.record(rule_id, self.sources[0], "HEAD", blob, "clean")
        self.write(self.sources[0], "pub fn changed() {}\n")
        _ = self.commit("Move HEAD")
        ledger.set_tip("HEAD")
        ledger.record(rule_id, self.sources[0], "HEAD", blob, "clean")

        self.assertEqual({cell.state for cell in ledger.cells(rule_id) if cell.path == self.sources[0]}, {"current"})

    def test_rerecording_older_ticket_selects_its_newest_verdict(self) -> None:
        rule_id = self.rule("read")
        self.set_tip(self.initial_tip)
        self.record(rule_id, self.sources[0], self.initial_tip, "held", ticket="A")
        self.record(rule_id, self.sources[0], self.initial_tip, "clean", ticket="B")
        self.record(rule_id, self.sources[0], self.initial_tip, "held", ticket="A")

        row = next(line.split("\t") for line in self.cli("status").stdout.splitlines() if line.startswith(rule_id + "\t"))
        self.assertEqual(row[5], "1")

    def test_manifest_cells_ignore_vendored_excluded_and_fixture_manifests(self) -> None:
        rule_id = self.rule("manifests", "tier: D\nfamily: modules\ninput_scope: manifest\n")
        self.write("Cargo.toml", '[workspace]\nmembers = ["crates/*"]\nexclude = ["crates/excluded"]\n')
        paths = ("vendor/library/Cargo.toml", "crates/excluded/Cargo.toml", "fixtures/example/Cargo.toml")
        for path in paths:
            self.write(path, '[package]\nname = "outside"\nversion = "0.1.0"\n')
        tip = self.commit("Add manifests outside workspace")
        self.set_tip(tip)
        self.record(rule_id, "crates/alpha", tip)
        for path in paths:
            with self.subTest(manifest=path):
                self.write(path, '[package]\nname = "outside"\nversion = "0.2.0"\n')
                self.set_tip(self.commit("Change unrelated manifest"))
                self.assertEqual(self.states(rule_id)["crates/alpha"], "current")

    def test_root_crate_fingerprint_excludes_member_files(self) -> None:
        rule_id = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        manifest = self.repo / "Cargo.toml"
        _ = manifest.write_text('[package]\nname = "root"\nversion = "0.1.0"\n' + manifest.read_text())
        self.write("src/lib.rs", "pub fn root() {}\n")
        tip = self.commit("Add root package")
        self.set_tip(tip)
        self.record(rule_id, "src/lib.rs", tip)
        self.write(self.sources[0], "pub fn changed_member() {}\n")
        self.set_tip(self.commit("Change member source"))

        self.assertEqual(self.states(rule_id)["src/lib.rs"], "current")
        self.write("src/lib.rs", "pub fn changed_root() {}\n")
        self.set_tip(self.commit("Change root source"))
        self.assertEqual(self.states(rule_id)["src/lib.rs"], "stale")

    def test_repeated_record_and_set_tip_preserve_coverage_after_reopening(self) -> None:
        rule_id = self.rule("read")
        for _ in range(2):
            self.set_tip(self.initial_tip)
            self.record(rule_id, self.sources[0], self.initial_tip, ticket="unit-repeat")
        status = cast(LedgerStatus, json.loads(self.cli("status", "--json").stdout))

        self.assertEqual(len(status["cells"]), len(self.sources))
        self.assertEqual(status["unclassified_rules"], [])
        self.assertEqual(self.states(rule_id), {
            self.sources[0]: "current", **dict.fromkeys(self.sources[1:], "open"),
        })
        state_file = self.root / "state" / "sample" / "ledger.sqlite"
        self.assertTrue(state_file.is_file())
        with closing(sqlite3.connect(state_file)) as connection:
            rows = cast(list[tuple[int]], connection.execute("SELECT COUNT(*) FROM judgments").fetchall())
        self.assertEqual(rows, [(1,)])

    def test_migration_preserves_judgments_and_requires_an_explicit_style_branch(self) -> None:
        rule_id = self.rule("read")
        blob = self.blob(rule_id)
        _ = self.git("checkout", "-q", "-b", "style/main")
        self.write(self.sources[0], "pub fn corrected() {}\n")
        checked = self.commit("Correct source")
        state_file = self.root / "state/sample/ledger.sqlite"
        state_file.parent.mkdir(parents=True)
        with closing(sqlite3.connect(state_file)) as connection:
            _ = connection.executescript(style_ledger.MIGRATIONS[1])
            _ = connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
            _ = connection.execute("INSERT INTO schema_version VALUES (1)")
            _ = connection.execute("INSERT INTO sync VALUES (1, ?)", (self.initial_tip,))
            _ = connection.execute(
                'INSERT INTO judgments(rule,rule_blob,input_scope,input_fingerprint,path,"commit",verdict,verdict_detail,ticket,worker,recorded_at,branch) '
                + 'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (rule_id, blob, "file", self.git("rev-parse", f"{checked}:{self.sources[0]}"), self.sources[0], checked,
                 "fixed", checked, "old-ticket", "worker", "2026-10-16T00:00:00+00:00", "refs/heads/style/main"),
            )
            connection.commit()

        ledger = self.ledger()

        self.assertEqual({cell.state for cell in ledger.cells(rule_id) if cell.path == self.sources[0]}, {"stale"})
        ledger.set_tip(self.initial_tip, style_branch="style/main")
        self.assertEqual({cell.state for cell in ledger.cells(rule_id) if cell.path == self.sources[0]}, {"awaiting merge"})
        ledger.set_tip(checked)
        self.assertEqual({cell.state for cell in ledger.cells(rule_id) if cell.path == self.sources[0]}, {"current"})

    def test_lint_records_hash_tool_configuration_and_replace_repeated_runs(self) -> None:
        rule_id = self.rule("lint", "tier: A\nmechanism: clippy\n")
        self.set_tip(self.initial_tip)
        self.lint(self.initial_tip, "clippy", True, {})
        state_file = self.root / "state" / "sample" / "ledger.sqlite"
        with closing(sqlite3.connect(state_file)) as connection:
            initial = cast(list[tuple[str]], connection.execute(
                "SELECT config_hash FROM lint_runs WHERE tool = 'clippy'",
            ).fetchall())
        for relative, content in (
            ("clippy.toml", 'disallowed-names = ["placeholder"]\n'),
            ("Cargo.toml", '[workspace]\nmembers = ["crates/alpha", "crates/beta"]\n'
             + '[workspace.lints.clippy]\nall = "warn"\n'),
        ):
            with self.subTest(config=relative):
                self.write(relative, content)
                tip = self.commit("Change lint configuration")
                self.set_tip(tip)
                self.assertEqual(self.states(rule_id), dict.fromkeys(self.sources, "stale"))
                self.lint(tip, "clippy", True, {})
                self.lint(tip, "clippy", True, {})
                with closing(sqlite3.connect(state_file)) as connection:
                    runs = cast(list[tuple[str, str]], connection.execute(
                        "SELECT tip, config_hash FROM lint_runs WHERE tool = 'clippy' ORDER BY id",
                    ).fetchall())
                matching = [config_hash for run_tip, config_hash in runs if run_tip == tip]
                self.assertEqual(len(matching), 1)
                self.assertNotEqual(matching[0], initial[-1][0])
                initial = [(matching[0],)]
                self.assertEqual(self.states(rule_id), dict.fromkeys(self.sources, "current"))

    def test_tier_a_requires_success_and_uses_only_its_own_tools_diagnostics(self) -> None:
        rule_id = self.rule("lint", "tier: A\nmechanism: clippy\n")
        self.set_tip(self.initial_tip)
        self.assertEqual(self.states(rule_id), dict.fromkeys(self.sources, "open"))
        self.lint(self.initial_tip, "mend", True, {})
        self.assertEqual(self.states(rule_id), dict.fromkeys(self.sources, "open"))
        self.lint(self.initial_tip, "clippy", False, {})
        self.assertEqual(self.states(rule_id), dict.fromkeys(self.sources, "stale"))
        self.lint(self.initial_tip, "clippy", True, {self.sources[0]: 1})
        self.assertEqual(self.states(rule_id), {
            self.sources[0]: "stale", **dict.fromkeys(self.sources[1:], "current"),
        })
        self.lint(self.initial_tip, "clippy", True, {})
        self.assertEqual(self.states(rule_id), dict.fromkeys(self.sources, "current"))
        self.lint(self.initial_tip, "clippy", False, {})
        self.assertEqual(self.states(rule_id), dict.fromkeys(self.sources, "stale"))

    def test_lint_run_matches_tree_across_empty_commits_and_expires_on_source_change(self) -> None:
        rule_id = self.rule("format", "tier: A\nmechanism: rustfmt\n")
        self.set_tip(self.initial_tip)
        self.lint(self.initial_tip, "rustfmt", True, {})
        _ = self.git("commit", "--allow-empty", "-q", "-m", "Same tree")
        self.set_tip(self.git("rev-parse", "HEAD"))
        self.assertEqual(self.states(rule_id), dict.fromkeys(self.sources, "current"))

        self.write(self.sources[0], "pub fn reformatted() {}\n")
        self.set_tip(self.commit("Change linted tree"))
        self.assertEqual(self.states(rule_id), dict.fromkeys(self.sources, "stale"))

    def test_all_legacy_catalog_has_no_cells_or_done_rules_and_names_the_action(self) -> None:
        legacy = self.rule("old", "mechanism: llm\ncandidates:\n  kind: regex\n  pattern: word\n")
        tool = self.rule("old-tool", "mechanism: clippy\n")
        self.set_tip(self.initial_tip)
        ledger = self.ledger()

        self.assertEqual(list(ledger.cells()), [])
        self.assertEqual(list(ledger.rule_counts(style_ledger.WholeRepository())), [])
        self.assertEqual(set(ledger.unclassified_rules()), {legacy, tool})
        self.assertIn("unclassified: 2 rules — pull nate_style", self.cli("status").stdout)
        status = cast(LedgerStatus, json.loads(self.cli("status", "--json").stdout))
        self.assertEqual(status["cells"], [])
        self.assertEqual(set(status["unclassified_rules"]), {legacy, tool})
        refusal = self.cli(
            "record", "--rule", legacy, "--path", self.sources[0], "--commit", self.initial_tip,
            "--rule-blob", self.blob(legacy), "--verdict", "clean", "--ticket", "unit-1",
            "--worker", "test-worker", success=False,
        )
        self.assertEqual(refusal.returncode, 2)
        self.assertIn(legacy, refusal.stderr)
        self.assertEqual(len(refusal.stderr.splitlines()), 1)

    def test_mixed_catalog_does_not_turn_legacy_rules_into_current_or_done(self) -> None:
        legacy = self.rule("old", "mechanism: llm\n")
        classified = self.rule("read")
        self.set_tip(self.initial_tip)
        for path in self.sources:
            self.record(classified, path, self.initial_tip)
        ledger = self.ledger()

        self.assertEqual({cell.rule for cell in ledger.cells()}, {classified})
        self.assertEqual(ledger.unclassified_rules(), (legacy,))
        progress, = ledger.rule_counts(style_ledger.WholeRepository())
        self.assertEqual(progress.rule_id, classified)
        self.assertTrue(progress.complete)
        self.assertIn("unclassified: 1 rules — pull nate_style", self.cli("status").stdout)

    def test_same_stem_global_and_member_rules_have_independent_cells_and_record_reach(self) -> None:
        global_id = self.rule("shared")
        local_dir = self.repo / "docs" / "alpha" / "style"
        local_dir.mkdir(parents=True)
        _ = (local_dir / "shared.md").write_text("---\ntier: C\nfamily: names\ninput_scope: file\n---\n# Local\n")
        local_id = "local:docs/alpha/style/shared.md"
        self.set_tip(self.initial_tip)

        self.assertEqual(self.states(global_id), dict.fromkeys(self.sources, "open"))
        self.assertEqual(self.states(local_id), dict.fromkeys(self.sources[:2], "open"))
        self.record(local_id, self.sources[0], self.initial_tip)
        self.assertEqual(self.states(global_id), dict.fromkeys(self.sources, "open"))
        self.assertEqual(self.states(local_id)[self.sources[0]], "current")
        for invalid_id, path in ((local_id, self.sources[2]), ("shared", self.sources[0]), ("global:rust/absent.md", self.sources[0])):
            with self.subTest(rule=invalid_id, path=path):
                refusal = self.cli(
                    "record", "--rule", invalid_id, "--path", path, "--commit", self.initial_tip,
                    "--rule-blob", self.blob(local_id), "--verdict", "clean", "--ticket", "unit-1",
                    "--worker", "test-worker", success=False,
                )
                self.assertEqual(refusal.returncode, 2)
                self.assertIn(invalid_id, refusal.stderr)
                self.assertEqual(len(refusal.stderr.splitlines()), 1)
        filtered = cast(LedgerStatus, json.loads(self.cli(
            "status", "--rule", local_id, "--crate", "crates/alpha", "--json",
        ).stdout))
        self.assertEqual({(cell["rule"], cell["path"]) for cell in filtered["cells"]}, {
            (local_id, path) for path in self.sources[:2]
        })


    def test_ticket_resume_transitions_repeats_and_rejected_skips(self) -> None:
        rule_id = self.rule("read")
        _ = self.cli("set-tip", "--tip", self.initial_tip, "--style-branch", "main")
        self.add_ticket("read-1", [rule_id], unit="read")
        self.add_ticket("read-1", [rule_id], unit="read")
        self.assertEqual(self.cli("ticket", "open", "--unit", "read").stdout.strip(), "")
        skipped = self.cli("ticket", "checkpoint", "--id", "read-1", "--commit", self.initial_tip, success=False)
        self.assertEqual(skipped.returncode, 2)
        self.assertIn("queued", skipped.stderr)
        for _ in range(2):
            _ = self.cli("ticket", "assign", "--id", "read-1", "--branch", "sample-style-read")
        self.assertIn("read-1", self.cli("ticket", "open", "--unit", "read").stdout)
        for _ in range(2):
            _ = self.cli("ticket", "checkpoint", "--id", "read-1", "--commit", self.initial_tip)
        self.assertIn("read-1", self.cli("ticket", "open", "--unit", "read").stdout)
        backward = self.cli("ticket", "assign", "--id", "read-1", "--branch", "sample-style-read", success=False)
        self.assertEqual(backward.returncode, 2)
        self.assertIn("checkpointed", backward.stderr)
        for _ in range(2):
            _ = self.cli("ticket", "merge", "--id", "read-1")
        self.assertEqual(self.cli("ticket", "open", "--unit", "read").stdout.strip(), "")
        dropped_final = self.cli("ticket", "drop", "--id", "read-1", success=False)
        self.assertEqual(dropped_final.returncode, 2)
        self.assertIn("merged", dropped_final.stderr)

    def test_ensuring_the_exact_tip_and_style_branch_never_calls_set_tip(self) -> None:
        _ = self.rule("read")
        ledger = self.ledger()
        ledger.set_tip(self.initial_tip, "style/main")
        before = ledger.synced_at()

        with mock.patch.object(ledger, "set_tip", side_effect=AssertionError("rewrote an exact sync match")):
            ledger.ensure_synced_tip(self.initial_tip, "style/main")
            ledger.ensure_synced_tip(self.initial_tip, "refs/heads/style/main")

        self.assertEqual(ledger.summary().synced_tip, self.initial_tip)
        self.assertEqual(ledger.synced_at(), before)

    def test_reconcile_new_style_branch_syncs_at_its_commit_before_the_branch_point(self) -> None:
        _ = self.rule("read")
        self.write("crates/beta/src/style.rs", "pub fn style_input() {}\n")
        style_tip = self.commit("Create the style branch input")
        _ = self.git("branch", "style/main")
        self.write("crates/beta/src/new.rs", "pub fn new_input() {}\n")
        branch_tip = self.commit("Advance branch point before style reconciliation")
        ledger = self.ledger()
        for previous_branch in ("", "style/previous"):
            with self.subTest(previous_branch=previous_branch):
                if previous_branch:
                    ledger.set_tip(self.initial_tip, previous_branch)
                else:
                    ledger.set_tip(self.initial_tip)

                changes = ledger.reconcile("main", "style/main")

                self.assertEqual(changes, ("style branch refs/heads/style/main",))
                self.assertEqual(ledger.summary().synced_tip, style_tip)
                paths = {cell.path for cell in ledger.cells()}
                self.assertIn("crates/beta/src/style.rs", paths)
                self.assertNotIn("crates/beta/src/new.rs", paths)
                self.assertEqual(self.git("rev-parse", "refs/heads/style/main"), style_tip)
                self.assertEqual(self.git("rev-parse", "main"), branch_tip)
                with closing(sqlite3.connect(ledger.database)) as connection:
                    stored = cast(tuple[str, str], connection.execute("SELECT tip,style_branch FROM sync").fetchone())
                self.assertEqual(stored, (style_tip, "refs/heads/style/main"))

    def test_reconcile_missing_style_branch_uses_the_branch_point_commit(self) -> None:
        _ = self.rule("read")
        self.write("crates/beta/src/new.rs", "pub fn new_input() {}\n")
        branch_tip = self.commit("Advance branch point without a style branch")
        ledger = self.ledger()
        ledger.set_tip(self.initial_tip, "style/previous")

        changes = ledger.reconcile("main", "style/main")

        self.assertEqual(changes, ("style branch refs/heads/style/main",))
        self.assertEqual(ledger.summary().synced_tip, branch_tip)
        self.assertIn("crates/beta/src/new.rs", {cell.path for cell in ledger.cells()})
        with closing(sqlite3.connect(ledger.database)) as connection:
            stored = cast(tuple[str, str], connection.execute("SELECT tip,style_branch FROM sync").fetchone())
        self.assertEqual(stored, (branch_tip, "refs/heads/style/main"))
        self.assertNotIn("refs/heads/style/main", self.git("for-each-ref", "--format=%(refname)"))

    def test_reconcile_matching_style_branch_preserves_an_older_synced_tip(self) -> None:
        _ = self.rule("read")
        _ = self.git("branch", "style/main")
        ledger = self.ledger()
        ledger.set_tip(self.initial_tip, "style/main")
        before = ledger.synced_at()
        self.write("crates/beta/src/new.rs", "pub fn new_input() {}\n")
        _ = self.commit("Advance branch point after the existing sync")

        self.assertEqual(ledger.reconcile("main", "refs/heads/style/main"), ())

        self.assertEqual(ledger.summary().synced_tip, self.initial_tip)
        self.assertEqual(ledger.synced_at(), before)
        self.assertNotIn("crates/beta/src/new.rs", {cell.path for cell in ledger.cells()})

    def test_assigned_ticket_resumes_until_its_recorded_branch_is_deleted(self) -> None:
        rule_id = self.rule("read")
        self.set_tip(self.initial_tip)
        _ = self.git("branch", "style/main")
        _ = self.git("branch", "sample-style-names")
        self.add_ticket("names-unit-1", [rule_id], unit="names-unit")
        missing = self.cli("ticket", "assign", "--id", "names-unit-1", success=False)
        self.assertEqual(missing.returncode, 2)
        self.assertIn("--branch", missing.stderr)
        self.assertEqual(self.cli("ticket", "open", "--unit", "names-unit").stdout.strip(), "")

        _ = self.cli("ticket", "assign", "--id", "names-unit-1", "--branch", "sample-style-names")
        opened = self.cli("ticket", "open", "--unit", "names-unit").stdout
        ticket = cast(style_ledger.TicketRecord, json.loads(opened))
        self.assertEqual(ticket["branch"], "refs/heads/sample-style-names")
        _ = self.cli("ticket", "assign", "--id", "names-unit-1", "--branch", "refs/heads/sample-style-names")
        self.assertEqual(self.cli("ticket", "open", "--unit", "names-unit").stdout, opened)
        refused = self.cli("ticket", "assign", "--id", "names-unit-1", "--branch", "other-branch", success=False)
        self.assertEqual(refused.returncode, 2)
        self.assertIn(ticket["branch"], refused.stderr)

        alive = self.cli("reconcile", "--branch-point", "main", "--style-branch", "style/main")
        self.assertNotIn("names-unit-1", alive.stdout)
        self.assertEqual(self.cli("ticket", "open", "--unit", "names-unit").stdout, opened)
        _ = self.git("branch", "-D", "sample-style-names")
        deleted = self.cli("reconcile", "--branch-point", "main", "--style-branch", "style/main")
        self.assertIn("ticket names-unit-1: assigned -> dropped", deleted.stdout)
        self.assertEqual(self.cli("ticket", "open", "--unit", "names-unit").stdout.strip(), "")
        with closing(sqlite3.connect(self.root / "state/sample/ledger.sqlite")) as connection:
            stored = cast(tuple[str, str], connection.execute(
                "SELECT state,branch FROM tickets WHERE id='names-unit-1'",
            ).fetchone())
        self.assertEqual(stored, ("dropped", ticket["branch"]))

    def test_ticket_drop_from_each_nonfinal_state_and_repeat(self) -> None:
        rule_id = self.rule("read")
        self.set_tip(self.initial_tip)
        for index, state in enumerate(("queued", "assigned", "checkpointed"), 1):
            with self.subTest(state=state):
                unit = f"reader{index}"
                ticket = f"{unit}-1"
                self.add_ticket(ticket, [rule_id], unit=unit)
                if state != "queued":
                    _ = self.cli("ticket", "assign", "--id", ticket, "--branch", f"sample-style-{unit}")
                if state == "checkpointed":
                    _ = self.cli("ticket", "checkpoint", "--id", ticket, "--commit", self.initial_tip)
                for _ in range(2):
                    _ = self.cli("ticket", "drop", "--id", ticket)
                refused = self.cli("ticket", "assign", "--id", ticket, "--branch", f"sample-style-{unit}", success=False)
                self.assertEqual(refused.returncode, 2)
                self.assertIn("dropped", refused.stderr)
                self.assertEqual(self.cli("ticket", "open", "--unit", unit).stdout.strip(), "")

    def test_ticket_pins_rule_text_and_refuses_a_mismatched_judgment(self) -> None:
        rule_id = self.rule("read")
        self.set_tip(self.initial_tip)
        old_blob = self.blob(rule_id)
        self.add_ticket("read-1", [rule_id])
        rule_path = self.rule_dir / "read.md"
        _ = rule_path.write_text(rule_path.read_text() + "New requirement\n")
        new_blob = self.blob(rule_id)
        self.add_ticket("read-1", [rule_id])

        self.record(rule_id, self.sources[0], self.initial_tip, rule_blob=old_blob, ticket="read-1")
        _ = self.cli(
            "record", "--rule", rule_id, "--path", self.sources[1], "--commit", self.initial_tip,
            "--verdict", "clean", "--ticket", "read-1", "--worker", "test",
        )

        self.assertEqual(self.states(rule_id)[self.sources[0]], "stale")
        self.assertEqual(self.states(rule_id)[self.sources[1]], "stale")
        refused = self.cli(
            "record", "--rule", rule_id, "--path", self.sources[0], "--commit", self.initial_tip,
            "--rule-blob", new_blob, "--verdict", "clean", "--ticket", "read-1", "--worker", "test",
            success=False,
        )
        self.assertEqual(refused.returncode, 2)
        self.assertIn(rule_id, refused.stderr)
        self.assertEqual(self.states(rule_id)[self.sources[0]], "stale")
        unpinned = self.cli(
            "record", "--rule", rule_id, "--path", self.sources[2], "--commit", self.initial_tip,
            "--verdict", "clean", "--ticket", "external", "--worker", "test", success=False,
        )
        self.assertEqual(unpinned.returncode, 2)
        self.assertEqual(self.states(rule_id)[self.sources[2]], "open")
        with closing(sqlite3.connect(self.root / "state/sample/ledger.sqlite")) as connection:
            pins = cast(list[tuple[str]], connection.execute("SELECT rule_blobs FROM tickets WHERE id='read-1'").fetchall())
        self.assertEqual(cast(dict[str, str], json.loads(pins[0][0])), {rule_id: old_blob})

    def test_listed_and_structure_tickets_keep_their_payloads_and_one_resume_point(self) -> None:
        listed = self.rule(
            "listed", "tier: B\nfamily: names\ninput_scope: file\ncandidates:\n"
            + "  kind: regex\n  pattern: value\n  surface: whole\n",
        )
        structure = self.rule("structure", "tier: D\nfamily: modules\n")
        self.set_tip(self.initial_tip)
        sites = [{"path": self.sources[0], "line": 1, "text": "pub fn value() -> u32 { 1 }"}]
        _ = self.cli(
            "ticket", "add", "--id", "listed-1", "--unit", "listed", "--kind", "listed",
            "--family", "names", "--crate", "crates/alpha", "--files", json.dumps([self.sources[0]]),
            "--rules", json.dumps([listed]), "--rule", listed, "--sites", json.dumps(sites),
        )
        _ = self.cli("ticket", "assign", "--id", "listed-1", "--branch", "sample-style-listed")
        self.record(listed, self.sources[0], self.initial_tip, ticket="listed-1")
        self.assertEqual(self.states(listed)[self.sources[0]], "current")
        self.add_ticket("listed-2", [structure], kind="structure", unit="listed")
        refused = self.cli("ticket", "assign", "--id", "listed-2", "--branch", "sample-style-structure", success=False)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("listed-1", refused.stderr)
        with closing(sqlite3.connect(self.root / "state/sample/ledger.sqlite")) as connection:
            stored = cast(list[tuple[str, str, str, str]], connection.execute(
                "SELECT kind,rule,files,sites FROM tickets WHERE id='listed-1'",
            ).fetchall())[0]
        self.assertEqual(stored[:2], ("listed", listed))
        self.assertEqual(cast(list[str], json.loads(stored[2])), [self.sources[0]])
        self.assertEqual(cast(list[dict[str, object]], json.loads(stored[3])), sites)
        _ = self.cli("ticket", "drop", "--id", "listed-1")
        _ = self.cli("ticket", "assign", "--id", "listed-2", "--branch", "sample-style-structure")
        self.assertIn("listed-2", self.cli("ticket", "open", "--unit", "listed").stdout)

    def test_ticket_pins_uncovered_parent_and_refuses_unknown_or_unclassified_ids(self) -> None:
        partial = self.rule(
            "partial", "tier: B\nfamily: names\ninput_scope: file\ncandidates:\n"
            + "  kind: regex\n  pattern: absent_marker\n  surface: partial\n  uncovered: other names\n",
        )
        legacy = self.rule("legacy", "mechanism: llm\n")
        self.set_tip(self.initial_tip)
        uncovered = partial + "#uncovered"
        self.add_ticket("read-1", [uncovered])
        self.record(uncovered, self.sources[0], self.initial_tip, ticket="read-1")
        self.assertEqual(self.states(uncovered)[self.sources[0]], "current")
        for invalid in (legacy, "global:rust/missing.md"):
            with self.subTest(rule=invalid):
                refused = self.cli(
                    "ticket", "add", "--id", "read-2", "--unit", "read", "--kind", "read",
                    "--family", "names", "--crate", "crates/alpha", "--files", json.dumps([self.sources[0]]),
                    "--rules", json.dumps([invalid]), success=False,
                )
                self.assertEqual(refused.returncode, 2)
                self.assertIn(invalid, refused.stderr)

    def test_processed_tips_survive_reopen_without_marking_seen_as_done(self) -> None:
        self.set_tip(self.initial_tip)
        for _ in range(2):
            self.assertEqual(self.cli("tip", "seen", "--tip", self.initial_tip).stdout.strip(), "new")
        for _ in range(2):
            _ = self.cli("tip", "done", "--tip", self.initial_tip)
        self.assertEqual(self.cli("tip", "seen", "--tip", self.initial_tip).stdout.strip(), "seen")
        _ = self.git("commit", "--allow-empty", "-q", "-m", "New tip with same tree")
        self.assertEqual(self.cli("tip", "seen", "--tip", "HEAD").stdout.strip(), "new")

    def test_handoffs_require_ancestry_and_keep_sent_items_pending(self) -> None:
        self.set_tip(self.initial_tip)
        _ = self.git("checkout", "-q", "-b", "style/main")
        self.write(self.sources[0], "pub fn corrected() {}\n")
        checkpoint = self.commit("Style checkpoint")
        _ = self.cli("reconcile", "--branch-point", "main", "--style-branch", "style/main")
        for _ in range(2):
            _ = self.cli("handoff", "add", "--checkpoint", checkpoint)
        refused = self.cli("handoff", "merged", "--checkpoint", checkpoint, success=False)
        self.assertEqual(refused.returncode, 2)
        for _ in range(2):
            _ = self.cli("handoff", "send", "--checkpoint", checkpoint, "--to", "parent")
        self.assertIn(checkpoint, self.cli("handoff", "pending").stdout)
        self.assertEqual(self.summary_report()["waiting_to_merge_back"], 1)
        _ = self.git("checkout", "-q", "main")
        _ = self.git("merge", "--no-ff", "-q", "style/main", "-m", "Integrate style")
        merge_commit = self.git("rev-parse", "HEAD")
        for _ in range(2):
            _ = self.cli("handoff", "merged", "--checkpoint", checkpoint, "--merge-commit", merge_commit)
        self.assertEqual(self.cli("handoff", "pending").stdout.strip(), "no pending hand-offs")
        backward = self.cli("handoff", "send", "--checkpoint", checkpoint, "--to", "parent", success=False)
        self.assertEqual(backward.returncode, 2)
        self.assertIn("merged", backward.stderr)

    def test_handoff_merged_preserves_supplied_later_merge_evidence(self) -> None:
        self.set_tip(self.initial_tip)
        _ = self.git("checkout", "-q", "-b", "style/main")
        self.write(self.sources[0], "pub fn first_fix() {}\n")
        checkpoint = self.commit("First style checkpoint")
        _ = self.cli("reconcile", "--branch-point", "main", "--style-branch", "style/main")
        _ = self.cli("handoff", "add", "--checkpoint", checkpoint)
        _ = self.cli("handoff", "send", "--checkpoint", checkpoint, "--to", "parent")
        _ = self.git("checkout", "-q", "main")
        _ = self.git("merge", "--no-ff", "-q", "style/main", "-m", "Integrate first checkpoint")
        first_merge = self.git("rev-parse", "HEAD")
        _ = self.git("checkout", "-q", "style/main")
        self.write(self.sources[2], "pub fn second_fix() {}\n")
        _ = self.commit("Second style checkpoint")
        _ = self.git("checkout", "-q", "main")
        _ = self.git("merge", "--no-ff", "-q", "style/main", "-m", "Integrate second checkpoint")
        second_merge = self.git("rev-parse", "HEAD")

        _ = self.cli("handoff", "merged", "--checkpoint", checkpoint, "--merge-commit", second_merge)

        with closing(sqlite3.connect(self.root / "state/sample/ledger.sqlite")) as connection:
            stored = cast(tuple[str, str], connection.execute(
                "SELECT state,merge_commit FROM handoffs WHERE checkpoint=?", (checkpoint,),
            ).fetchone())
        self.assertNotEqual(first_merge, second_merge)
        self.assertEqual(stored, ("merged", second_merge))

    def test_reconcile_recovers_merged_handoff_ticket_and_missing_unit_branch(self) -> None:
        rule_id = self.rule("read")
        self.set_tip(self.initial_tip)
        _ = self.git("checkout", "-q", "-b", "style/main")
        self.write(self.sources[0], "pub fn first_fix() {}\n")
        merged_checkpoint = self.commit("First style checkpoint")
        self.add_ticket("worker-1", [rule_id], unit="worker")
        _ = self.git("branch", "sample-style-worker", merged_checkpoint)
        _ = self.cli("ticket", "assign", "--id", "worker-1", "--branch", "sample-style-worker")
        _ = self.cli("ticket", "checkpoint", "--id", "worker-1", "--commit", merged_checkpoint)
        self.add_ticket("vanished-1", [rule_id], unit="vanished")
        _ = self.cli("ticket", "assign", "--id", "vanished-1", "--branch", "sample-style-vanished")
        _ = self.git("branch", "sample-style-live", merged_checkpoint)
        self.add_ticket("live-1", [rule_id], unit="live")
        _ = self.cli("ticket", "assign", "--id", "live-1", "--branch", "sample-style-live")
        _ = self.cli("handoff", "add", "--checkpoint", merged_checkpoint)
        _ = self.git("checkout", "-q", "main")
        _ = self.git("merge", "--no-ff", "-q", "style/main", "-m", "Integrate first checkpoint")
        first_merge = self.git("rev-parse", "HEAD")
        _ = self.git("checkout", "-q", "style/main")
        self.write(self.sources[2], "pub fn second_fix() {}\n")
        unmerged_checkpoint = self.commit("Second style checkpoint")
        _ = self.cli("handoff", "add", "--checkpoint", unmerged_checkpoint)

        result = self.cli("reconcile", "--branch-point", "main", "--style-branch", "style/main")

        self.assertIn(merged_checkpoint, result.stdout)
        self.assertIn("worker-1", result.stdout)
        self.assertIn("vanished-1", result.stdout)
        self.assertIn(unmerged_checkpoint, self.cli("handoff", "pending").stdout)
        self.assertNotIn(merged_checkpoint, self.cli("handoff", "pending").stdout)
        with closing(sqlite3.connect(self.root / "state/sample/ledger.sqlite")) as connection:
            tickets = cast(list[tuple[str, str]], connection.execute("SELECT id,state FROM tickets ORDER BY id").fetchall())
            handoffs = cast(list[tuple[str, str, str]], connection.execute(
                "SELECT checkpoint,state,merge_commit FROM handoffs WHERE checkpoint=?", (merged_checkpoint,),
            ).fetchall())
            sync = cast(list[tuple[str, str]], connection.execute("SELECT tip,style_branch FROM sync").fetchall())
        self.assertEqual(tickets, [("live-1", "assigned"), ("vanished-1", "dropped"), ("worker-1", "merged")])
        self.assertEqual(handoffs, [(merged_checkpoint, "merged", first_merge)])
        self.assertEqual(sync, [(unmerged_checkpoint, "refs/heads/style/main")])
        self.assertEqual(self.cli("reconcile", "--branch-point", "main", "--style-branch", "style/main").stdout.strip(), "")

    def test_reconcile_bootstraps_an_unsynced_ledger_and_preserves_stored_tip(self) -> None:
        _ = self.rule("read")
        _ = self.git("branch", "style/main")
        _ = self.cli("reconcile", "--branch-point", "main", "--style-branch", "style/main")
        report = self.summary_report()
        self.assertEqual(report["synced_tip"], self.initial_tip)
        self.write(self.sources[0], "pub fn product_change() {}\n")
        _ = self.commit("Advance branch point")
        _ = self.cli("reconcile", "--branch-point", "main", "--style-branch", "style/main")
        self.assertEqual(self.summary_report()["synced_tip"], self.initial_tip)

    def test_summary_excludes_legacy_process_and_pseudo_rules_and_counts_both_components(self) -> None:
        partial = self.rule(
            "partial", "tier: B\nfamily: names\ninput_scope: file\ncandidates:\n"
            + "  kind: regex\n  pattern: value\n  surface: partial\n  uncovered: other names\n",
        )
        _ = self.rule("legacy", "mechanism: llm\n")
        _ = self.rule("process", "tier: E\n")
        self.set_tip(self.initial_tip)
        for path in self.sources:
            self.record(partial + "#uncovered", path, self.initial_tip)
        report = self.summary_report()
        self.assertEqual((report["rules_done"], report["rules_total"], report["unclassified_rules"]), (0, 1, 1))
        self.assertEqual(report["cells_current_pct"], 50.0)
        for path in self.sources:
            self.record(partial, path, self.initial_tip)
        report = self.summary_report()
        self.assertEqual((report["rules_done"], report["rules_total"], report["cells_current_pct"]), (1, 1, 100.0))
        rule_path = self.rule_dir / "partial.md"
        _ = rule_path.write_text(rule_path.read_text() + "More requirements\n")
        report = self.summary_report()
        self.assertEqual((report["rules_done"], report["stale"], report["cells_current_pct"]), (0, 8, 0.0))

    def test_summary_matches_rule_progress_with_only_one_partial_component_done(self) -> None:
        partial = self.rule(
            "partial", "tier: B\nfamily: names\ninput_scope: file\ncandidates:\n"
            + "  kind: regex\n  pattern: value\n  surface: partial\n  uncovered: other names\n",
        )
        read_rule = self.rule("read")
        lint_rule = self.rule("lint", "tier: A\nmechanism: clippy\n")
        structure = self.rule("structure", "tier: D\nfamily: modules\n")
        legacy = self.rule("legacy", "mechanism: llm\n")
        process = self.rule("process", "tier: E\n")
        self.set_tip(self.initial_tip)
        self.lint(self.initial_tip, "clippy", True, {})
        for path in self.sources:
            self.record(read_rule, path, self.initial_tip)
            self.record(partial, path, self.initial_tip)
        ledger = self.ledger()
        progress = {row.rule_id: row for row in ledger.rule_counts(style_ledger.WholeRepository())}
        self.assertTrue(progress[lint_rule].complete)
        self.assertTrue(progress[read_rule].complete)
        self.assertFalse(progress[partial].complete)
        self.assertFalse(progress[structure].complete)
        self.assertIn(process, progress)
        self.assertNotIn(legacy, progress)
        counted = [row for row in progress.values() if row.tier in ("A", "B", "C", "D")]

        with mock.patch.object(ledger, "_coverage", wraps=cast(object, getattr(ledger, "_coverage"))) as coverage:
            summary = ledger.summary()
            coverage.assert_called_once_with(None, None)

        self.assertEqual(summary.rules_done, sum(row.complete for row in counted))
        self.assertEqual(summary.rules_total, len(counted))
        self.assertEqual(summary.unclassified_rules, 1)

    def test_summary_all_legacy_is_zero_of_zero_with_no_pending_handoffs(self) -> None:
        _ = self.rule("legacy", "mechanism: llm\n")
        _ = self.rule("old-tool", "mechanism: clippy\n")
        self.set_tip(self.initial_tip)
        report = self.summary_report()
        self.assertEqual(set(report), {
            "rules_done", "rules_total", "unclassified_rules", "cells_current_pct", "stale", "proposals",
            "waiting_to_merge_back", "oldest_waiting", "synced_at", "synced_tip",
        })
        self.assertEqual((report["rules_done"], report["rules_total"], report["unclassified_rules"]), (0, 0, 2))
        self.assertEqual((report["waiting_to_merge_back"], report["oldest_waiting"]), (0, None))
        self.assertIsInstance(self.ledger().summary().waiting, style_ledger.NoPendingHandoffs)
        self.assertEqual(self.cli("handoff", "pending").stdout.strip(), "no pending hand-offs")

    def test_summary_counts_unsent_handoffs_from_creation_and_keeps_oldest_after_send(self) -> None:
        self.set_tip(self.initial_tip)
        _ = self.git("branch", "style/main")
        _ = self.cli("reconcile", "--branch-point", "main", "--style-branch", "style/main")
        _ = self.cli("handoff", "add", "--checkpoint", self.initial_tip)
        skipped = self.cli("handoff", "merged", "--checkpoint", self.initial_tip,
                           "--merge-commit", self.initial_tip, success=False)
        self.assertEqual(skipped.returncode, 2)
        self.assertIn("pending", skipped.stderr)
        with closing(sqlite3.connect(self.root / "state/sample/ledger.sqlite")) as connection:
            created = cast(list[tuple[str]], connection.execute("SELECT created_at FROM handoffs").fetchall())[0][0]
        report = self.summary_report()
        self.assertEqual((report["waiting_to_merge_back"], report["oldest_waiting"]), (1, created))
        _ = self.git("commit", "--allow-empty", "-q", "-m", "Another checkpoint")
        second = self.git("rev-parse", "HEAD")
        _ = self.cli("handoff", "add", "--checkpoint", second)
        _ = self.cli("handoff", "send", "--checkpoint", self.initial_tip, "--to", "parent")
        report = self.summary_report()
        self.assertEqual((report["waiting_to_merge_back"], report["oldest_waiting"]), (2, created))
        waiting = self.ledger().summary().waiting
        self.assertIsInstance(waiting, style_ledger.PendingHandoffs)
        if isinstance(waiting, style_ledger.PendingHandoffs):
            self.assertEqual((waiting.count, waiting.oldest_pending_since), (2, datetime.fromisoformat(created)))

    def test_synced_time_survives_reopen_and_repeat_set_tip_advances_it(self) -> None:
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        first = ledger.synced_at()
        self.assertEqual(first.utcoffset(), timezone.utc.utcoffset(first))
        self.assertEqual(self.ledger().synced_at(), first)
        self.assertEqual(datetime.fromisoformat(self.summary_report()["synced_at"]), first)

        self.set_tip(self.initial_tip)

        self.assertGreater(ledger.synced_at(), first)
        self.assertEqual(self.summary_report()["synced_tip"], self.initial_tip)

    def test_version_three_migration_keeps_judgments_sites_and_style_branch_and_dates_sync(self) -> None:
        read_rule = self.rule("read")
        listed = self.rule(
            "listed", "tier: B\nfamily: names\ninput_scope: file\ncandidates:\n"
            + "  kind: regex\n  pattern: value\n  surface: whole\n",
        )
        state_file = self.root / "state/sample/ledger.sqlite"
        state_file.parent.mkdir(parents=True)
        with closing(sqlite3.connect(state_file)) as connection:
            for version in (1, 2, 3):
                _ = connection.executescript(style_ledger.MIGRATIONS[version])
            _ = connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
            _ = connection.execute("INSERT INTO schema_version VALUES (3)")
            _ = connection.execute("INSERT INTO sync VALUES (1,?,?)", (self.initial_tip, "refs/heads/style/main"))
            _ = connection.execute(
                'INSERT INTO judgments(rule,rule_blob,input_scope,input_fingerprint,path,"commit",verdict,verdict_detail,ticket,worker,recorded_at) '
                + 'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (read_rule, self.blob(read_rule), "file", self.git("rev-parse", f"{self.initial_tip}:{self.sources[0]}"),
                 self.sources[0], self.initial_tip, "held", "reviewed", "old-ticket", "worker", "2026-10-16T00:00:00+00:00"),
            )
            _ = connection.execute("INSERT INTO listed_rules VALUES (?,?,?)", (listed, self.blob(listed), self.initial_tip))
            _ = connection.execute("INSERT INTO listed_sites VALUES (?,?,?,?,?,?)", (
                listed, self.blob(listed), self.initial_tip, self.sources[0], 1, "pub fn value() -> u32 { 1 }",
            ))
            connection.commit()
        before = datetime.now(timezone.utc)

        ledger = self.ledger()

        self.assertEqual(self.states(read_rule)[self.sources[0]], "current")
        self.assertEqual([(site.path, site.line, site.text) for site in ledger.listed_sites(listed)], [
            (self.sources[0], 1, "pub fn value() -> u32 { 1 }"),
        ])
        self.assertLessEqual(before.replace(microsecond=0), ledger.synced_at())
        self.assertLessEqual(ledger.synced_at(), datetime.now(timezone.utc))
        self.assertEqual(self.ledger().synced_at(), ledger.synced_at())
        with closing(sqlite3.connect(state_file)) as connection:
            versions = cast(list[tuple[int]], connection.execute("SELECT version FROM schema_version").fetchall())
            sync = cast(list[tuple[str, str]], connection.execute("SELECT tip,style_branch FROM sync").fetchall())
        self.assertEqual(versions, [(6,)])
        self.assertEqual(sync, [(self.initial_tip, "refs/heads/style/main")])

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
        self.assertEqual(duplicate, original)
        proposal = self.root / "issues/rename.md"
        for finding in (first, second):
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
            _ = self.cli("held", "waive", "--id", first)
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
            _ = self.cli("held", "waive", "--id", finding)
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

    def test_rule_edit_during_pinning_rolls_back_ticket_and_all_its_texts(self) -> None:
        first = self.rule("first")
        second = self.rule("second")
        ledger = self.ledger()
        catalog = rules.load_rules(self.repo)

        def catalog_then_edit(repo: Path) -> tuple[rules.Rule, ...]:
            self.assertEqual(repo, self.repo)
            path = self.rule_dir / "second.md"
            _ = path.write_text(path.read_text() + "Concurrent edit\n")
            return catalog

        with mock.patch.object(rules, "load_rules", side_effect=catalog_then_edit):
            with self.assertRaisesRegex(style_ledger.LedgerError, second + ": rule changed while pinning; rerun"):
                ledger.ticket_add("read-1", "read", "read", "names", "crates/alpha", list(self.sources[:2]), [first, second])
        self.assertEqual(ledger.queued_tickets("read"), ())
        with closing(sqlite3.connect(ledger.database)) as connection:
            texts = cast(list[tuple[str]], connection.execute("SELECT blob FROM rule_texts").fetchall())
        self.assertEqual(texts, [])

    def test_ticket_start_is_recorded_once_and_survives_reopening_and_branch_deletion(self) -> None:
        rule = self.rule("read")
        _ = self.git("branch", "demo-style-names")
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.ticket_add("names-unit-1", "names-unit", "read", "names", "crates/alpha",
                              list(self.sources[:2]), [rule])
            with self.assertRaisesRegex(style_ledger.LedgerError, "cannot record start commit in state queued"):
                _ = ledger.ticket_base("names-unit-1")
            ledger.ticket_assign("names-unit-1", "demo-style-names")
            self.assertEqual(ledger.ticket_base("names-unit-1"), self.initial_tip)
            self.assertEqual(ledger.ticket_origin("names-unit-1"), style_ledger.SelectedByCoverage(self.initial_tip))

        self.write(self.sources[0], "pub fn changed() {}\n")
        advanced = self.commit("Advance unit branch")
        _ = self.git("branch", "-f", "demo-style-names", advanced)
        with style_ledger.open_ledger(self.repo) as ledger:
            self.assertEqual(ledger.ticket_base("names-unit-1"), self.initial_tip)
            self.assertEqual(ledger.ticket_origin("names-unit-1"), style_ledger.SelectedByCoverage(self.initial_tip))
            self.assertEqual(ledger.ticket_open("names-unit")[0]["base"], self.initial_tip)
            ledger.ticket_drop("names-unit-1")
        _ = self.git("branch", "-D", "demo-style-names")
        with style_ledger.open_ledger(self.repo) as ledger:
            self.assertEqual(ledger.ticket_base("names-unit-1"), self.initial_tip)
        self.assertEqual(self.git("for-each-ref", "refs/style-tickets"), "")

    def test_next_ticket_id_never_reuses_merged_or_dropped_numbers(self) -> None:
        rule = self.rule("read")
        ledger = self.ledger()
        ledger.set_tip(self.initial_tip, "main")
        self.assertEqual(ledger.next_ticket_id("names-unit"), "names-unit-1")
        for number in (9, 11):
            ledger.ticket_add(f"names-unit-{number}", "names-unit", "read", "names", "crates/alpha",
                              list(self.sources[:2]), [rule])
        ledger.ticket_drop("names-unit-9")
        ledger.ticket_assign("names-unit-11", "main")
        ledger.ticket_checkpoint("names-unit-11", self.initial_tip)
        ledger.ticket_merge("names-unit-11")
        self.assertEqual(ledger.next_ticket_id("names-unit"), "names-unit-12")
        ledger.ticket_add("names-unit-12", "names-unit", "read", "names", "crates/alpha", list(self.sources[:2]), [rule])
        self.assertEqual(self.ledger().next_ticket_id("names-unit"), "names-unit-13")

    def test_queued_tickets_are_only_the_units_unassigned_work_in_creation_order(self) -> None:
        rule = self.rule("read")
        ledger = self.ledger()
        with mock.patch.object(style_ledger, "_now", side_effect=[
            "2026-10-16T10:00:00+00:00", "2026-10-16T10:00:01+00:00", "2026-10-16T10:00:02+00:00",
        ]):
            for number in (2, 1, 3):
                ledger.ticket_add(f"read-{number}", "read", "read", "names", "crates/alpha", list(self.sources[:2]), [rule])
        ledger.ticket_assign("read-3", "main")
        ledger.ticket_add("other-1", "other", "read", "names", "crates/alpha", list(self.sources[:2]), [rule])
        self.assertEqual([ticket["id"] for ticket in ledger.queued_tickets("read")], ["read-2", "read-1"])
        ledger.ticket_drop("read-2")
        self.assertEqual([ticket["id"] for ticket in self.ledger().queued_tickets("read")], ["read-1"])

    def test_structure_ticket_keeps_sites_and_pins_their_rule_text(self) -> None:
        rule = self.rule(
            "structure", "tier: D\nfamily: modules\ncandidates:\n  kind: regex\n  pattern: value\n"
            + "  surface: partial\n  uncovered: crate structure\n",
        )
        sites: list[dict[str, object]] = [{"rule": rule, "path": self.sources[0], "line": 1, "text": "pub fn value() {}"}]
        ledger = self.ledger()
        ledger.ticket_add("modules-unit-1", "modules-unit", "structure", "modules", "crates/alpha",
                          list(self.sources[:2]), [rule], sites=sites)
        ticket, = ledger.queued_tickets("modules-unit")
        self.assertEqual((ticket["sites"], ticket["rule"]), (sites, ""))
        pinned, = ledger.pinned_rule_texts(ticket["id"])
        self.assertEqual((pinned.rule_id, pinned.text), (rule, (self.rule_dir / "structure.md").read_bytes()))

    def test_rust_files_exposes_the_synced_universe_even_with_only_crate_rules(self) -> None:
        _ = self.rule("structure", "tier: D\nfamily: modules\n")
        self.write("crates/alpha/src/ignored/hidden.rs", "pub fn ignored() {}\n")
        self.write("rustfmt.toml", 'ignore = ["crates/alpha/src/ignored"]\n')
        self.write("tools/root.rs", "pub fn root() {}\n")
        tip = self.commit("Add ignored and root Rust files")
        self.set_tip(tip)
        self.write("crates/alpha/src/untracked.rs", "pub fn untracked() {}\n")
        self.write("rustfmt.toml", 'ignore = ["crates/beta"]\n')
        ledger = self.ledger()

        self.assertEqual(ledger.rust_files("crates/alpha"), self.sources[:2])
        self.assertEqual(ledger.rust_files("crates/beta"), self.sources[2:])
        self.assertEqual(ledger.rust_files("."), ("tools/root.rs",))
        self.assertEqual(ledger.rust_files("missing"), ())

    def test_version_four_migration_preserves_work_and_reports_missing_historical_pin_text(self) -> None:
        rule = self.rule("read")
        blob = self.blob(rule)
        database = self.root / "state/sample/ledger.sqlite"
        database.parent.mkdir(parents=True)
        now = "2026-10-16T10:00:00+00:00"
        with closing(sqlite3.connect(database)) as connection:
            for version in (1, 2, 3, 4):
                _ = connection.executescript(style_ledger.MIGRATIONS[version])
            _ = connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
            _ = connection.execute("INSERT INTO schema_version VALUES (4)")
            _ = connection.execute("INSERT INTO sync VALUES (1,?,?,?)", (self.initial_tip, "refs/heads/main", now))
            _ = connection.execute(
                "INSERT INTO tickets VALUES (?,?,?,?,?,?,?,?,?,?,'queued','','',?,?)",
                ("read-1", "read", "read", "names", "", "crates/alpha", json.dumps(list(self.sources[:2])), "[]",
                 json.dumps([rule]), json.dumps({rule: blob}), now, now),
            )
            _ = connection.execute("INSERT INTO handoffs VALUES (?,'sent','','parent',?,?)", (self.initial_tip, now, now))
            _ = connection.execute(
                'INSERT INTO judgments(rule,rule_blob,input_scope,input_fingerprint,path,"commit",verdict,verdict_detail,ticket,worker,recorded_at) '
                + 'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (rule, blob, "file", self.git("rev-parse", f"{self.initial_tip}:{self.sources[0]}"), self.sources[0],
                 self.initial_tip, "clean", "", "read-1", "worker", now),
            )
            connection.commit()

        ledger = self.ledger()

        self.assertEqual([ticket["id"] for ticket in ledger.queued_tickets("read")], ["read-1"])
        self.assertEqual(ledger.queued_tickets("read")[0]["base"], "")
        self.assertEqual(ledger.next_ticket_id("read"), "read-2")
        self.assertEqual([(handoff["checkpoint"], handoff["state"]) for handoff in ledger.pending_handoffs()], [(self.initial_tip, "sent")])
        self.assertEqual(self.states(rule)[self.sources[0]], "current")
        with self.assertRaisesRegex(style_ledger.LedgerError, f"read-1: no pinned text for {blob}"):
            _ = ledger.pinned_rule_texts("read-1")
        ledger.ticket_add("read-2", "read", "read", "names", "crates/alpha", list(self.sources[:2]), [rule])
        self.assertEqual(ledger.pinned_rule_texts("read-1"), ledger.pinned_rule_texts("read-2"))
        with closing(sqlite3.connect(database)) as connection:
            versions = cast(list[tuple[int]], connection.execute("SELECT version FROM schema_version").fetchall())
        self.assertEqual(versions, [(6,)])


    def test_handoff_reroute_preserves_creation_and_rejects_invalid_states(self) -> None:
        ledger = self.ledger()
        ledger.handoff_add(self.initial_tip)
        pending = ledger.pending_handoffs()[0]
        refused = self.cli("handoff", "reroute", "--checkpoint", self.initial_tip, "--to", "new", success=False)
        self.assertEqual(refused.returncode, 2)
        self.assertTrue(refused.stderr.strip().endswith("; use handoff send"), refused.stderr)
        ledger.handoff_send(self.initial_tip, "old")
        sent = ledger.pending_handoffs()[0]
        with mock.patch.object(style_ledger, "_now", return_value="2099-01-01T00:00:00+00:00"):
            ledger.handoff_reroute(self.initial_tip, "new")
        rerouted = ledger.pending_handoffs()[0]
        self.assertEqual((rerouted["checkpoint"], rerouted["state"], rerouted["sent_to"], rerouted["created_at"]),
                         (self.initial_tip, "sent", "new", pending["created_at"]))
        self.assertNotEqual(rerouted["updated_at"], sent["updated_at"])
        _ = self.cli("handoff", "reroute", "--checkpoint", self.initial_tip, "--to", "new")
        self.assertEqual(ledger.pending_handoffs()[0], rerouted)
        resend = self.cli("handoff", "send", "--checkpoint", self.initial_tip, "--to", "third", success=False)
        self.assertEqual(resend.returncode, 2)
        _ = self.cli("reconcile", "--branch-point", "main", "--style-branch", "main")
        merged = self.cli("handoff", "reroute", "--checkpoint", self.initial_tip, "--to", "third", success=False)
        self.assertEqual(merged.returncode, 2)
        self.assertIn("merged", merged.stderr)
        _ = self.git("commit", "--allow-empty", "-qm", "Unknown handoff")
        unknown = self.cli("handoff", "reroute", "--checkpoint", "HEAD", "--to", "third", success=False)
        self.assertEqual(unknown.returncode, 2)
        self.assertIn("unknown", unknown.stderr.lower())

    def test_ticket_merge_requires_checkpoint_on_the_stored_style_branch(self) -> None:
        rule = self.rule("read")
        _ = self.git("branch", "style/main")
        _ = self.cli("set-tip", "--tip", self.initial_tip, "--style-branch", "style/main")
        self.add_ticket("read-1", [rule])
        _ = self.cli("ticket", "assign", "--id", "read-1", "--branch", "main")
        self.write(self.sources[0], "pub fn fixed() {}\n")
        checkpoint = self.commit("Unit checkpoint")
        _ = self.cli("ticket", "checkpoint", "--id", "read-1", "--commit", checkpoint)
        ledger = self.ledger()
        with self.assertRaisesRegex(style_ledger.LedgerError,
                                    f"read-1: checkpoint {checkpoint[:7]} is not on refs/heads/style/main"):
            ledger.ticket_merge("read-1")
        self.assertEqual(ledger.ticket_open("read")[0]["state"], "checkpointed")
        _ = self.git("branch", "-f", "style/main", checkpoint)
        ledger.ticket_merge("read-1")
        self.assertEqual(ledger.ticket_open("read"), ())
        with closing(sqlite3.connect(self.ledger().database)) as connection:
            self.assertEqual(connection.execute("SELECT state FROM tickets WHERE id='read-1'").fetchone(), ("merged",))

    def test_assigned_ticket_merge_reports_the_state_before_resolving_style_branch(self) -> None:
        rule = self.rule("read")
        self.add_ticket("read-1", [rule])
        _ = self.cli("ticket", "assign", "--id", "read-1", "--branch", "main")
        with closing(sqlite3.connect(self.ledger().database)) as connection:
            _ = connection.execute("UPDATE sync SET style_branch='refs/heads/missing'")
            connection.commit()
        refused = self.cli("ticket", "merge", "--id", "read-1", success=False)
        self.assertEqual(refused.returncode, 2)
        self.assertEqual(refused.stderr.strip(), "style_ledger: read-1: cannot become merged from assigned")
        self.assertEqual(self.ledger().ticket_open("read")[0]["state"], "assigned")

    def test_ticket_pin_git_failure_is_a_cli_error_and_rolls_back_the_ticket(self) -> None:
        rule = self.rule("read")
        binary = self.root / "bin"
        binary.mkdir()
        git = binary / "git"
        real_git = shutil.which("git")
        if real_git is None:
            self.fail("git is required for the ledger fixture")
        _ = git.write_text(
            "#!/bin/sh\nif [ \"$*\" = \"-C " + str(self.repo) + " hash-object --stdin\" ]; then\n"
            + "  echo 'pin hash unavailable' >&2; exit 17\nfi\nexec " + real_git + ' "$@"\n',
        )
        git.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": str(binary) + os.pathsep + os.environ.get("PATH", "")}):
            result = self.cli("ticket", "add", "--id", "read-1", "--unit", "read", "--kind", "read",
                              "--family", "names", "--crate", "crates/alpha", "--files", json.dumps([self.sources[0]]),
                              "--rules", json.dumps([rule]), success=False)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), "style_ledger: pin hash unavailable")
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.ledger().queued_tickets("read"), ())
        with closing(sqlite3.connect(self.ledger().database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM rule_texts").fetchone(), (0,))


if __name__ == "__main__":
    _ = unittest.main()
