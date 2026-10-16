"""Coverage follows committed inputs, the judged rule revision, and rule reach."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
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
        result = subprocess.run(
            [sys.executable, str(Path(style_ledger.__file__)), arguments[0],
             "--repo", str(self.repo), *arguments[1:]],
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
        progress = {rule.rule_id: rule for rule in ledger.rules()}
        self.assertTrue(progress[listed].done)
        self.assertEqual(progress[listed].cells_total, 0)
        self.assertEqual(progress[file_rule].cells_total, 4)
        self.assertFalse(progress[file_rule].done)
        self.assertEqual({cell.path for cell in ledger.cells(crate="crates/alpha")}, {
            "crates/alpha", "crates/alpha/src/lib.rs", "crates/alpha/src/other.rs",
        })
        status = self.cli("status").stdout
        self.assertIn(file_rule, status)
        self.assertIn(listed, status)
        self.assertNotIn("unclassified:", status)

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
        progress, = ledger.rules()
        self.assertEqual((progress.rule_id, progress.cells_current, progress.cells_total, progress.done),
                         (rule_id, 0, 4, False))

        for path in self.sources:
            self.record(uncovered, path, self.initial_tip)

        progress, = ledger.rules()
        self.assertEqual((progress.rule_id, progress.cells_current, progress.cells_total, progress.done),
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
        progress, = ledger.rules()
        self.assertEqual((progress.cells_current, progress.cells_total, progress.done), (4, 8, False))

        for path in self.sources:
            self.record(rule_id, path, self.initial_tip)

        progress, = ledger.rules()
        self.assertEqual((progress.rule_id, progress.cells_current, progress.cells_total, progress.done),
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
        progress, = ledger.rules()
        self.assertEqual((progress.rule_id, progress.cells_current, progress.cells_total, progress.done),
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
        progress, = ledger.rules()
        self.assertTrue(progress.done)

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
            progress, = ledger.rules()
            self.assertTrue(progress.done)
        with closing(sqlite3.connect(ledger.database)) as connection:
            markers = cast(list[tuple[str, str, str]], connection.execute(
                "SELECT rule,rule_blob,tip FROM listed_rules",
            ).fetchall())
        self.assertEqual(markers, [(rule_id, self.blob(rule_id), self.initial_tip)])

        rule_path = self.rule_dir / "empty.md"
        _ = rule_path.write_text(rule_path.read_text().replace("absent_marker", "value"))

        self.assertEqual({site.path for site in ledger.listed_sites(rule_id)}, set(self.sources))
        progress, = ledger.rules()
        self.assertFalse(progress.done)

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
        self.assertEqual(versions, [(3,)])
        self.assertEqual(sync, [(self.initial_tip, "refs/heads/style/main")])

    def test_file_judgment_becomes_stale_only_for_changed_committed_file(self) -> None:
        rule_id = self.rule("read")
        self.set_tip(self.initial_tip)
        for path in self.sources:
            self.record(rule_id, path, self.initial_tip)
        self.assertEqual(self.states(rule_id), dict.fromkeys(self.sources, "current"))
        ledger = self.ledger()
        progress, = ledger.rules()
        self.assertTrue(progress.done)
        self.assertEqual((progress.cells_current, progress.cells_total), (4, 4))

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
        self.assertEqual(list(ledger.rules()), [])
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
        progress, = ledger.rules()
        self.assertEqual(progress.rule_id, classified)
        self.assertTrue(progress.done)
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


if __name__ == "__main__":
    _ = unittest.main()
