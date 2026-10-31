"""Gate exit statuses, formatter protocols and coverage verdicts."""

from __future__ import annotations

import io
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from typing import cast, final
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import style_plan
import style_proposals
import proposal_issue
from issues import frontmatter

import berth_board
import fake_fmt
import rules
import style_ledger

from style_plan_test_support import StylePlanFixture


@final
class StylePlanTests(StylePlanFixture):
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

    def test_coverage_gate_refuses_an_unformatted_changed_file(self) -> None:
        rule = self.rule("structure", "tier: D\nfamily: modules\n")
        ledger = self.ledger()
        ledger.set_tip(self.tip)
        path = self.sources[0]
        ledger.ticket_add("modules-unit-1", "modules-unit", "structure", "modules", "crates/alpha",
                          [path], [rule], cells=((rule, path),))
        ledger.ticket_assign("modules-unit-1", "demo-style-names")
        _ = ledger.ticket_base("modules-unit-1")
        self.record(rule, path, self.tip, "modules-unit-1")
        _ = (self.unit / path).write_text("pub fn value()  {}\n")
        self.unformatted(path)
        result = subprocess.run([sys.executable, str(Path(style_plan.__file__)), "gate", "--repo", str(self.unit),
                                 "--ticket", "modules-unit-1"], capture_output=True, text=True, check=False)
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (1, f"mechanical\nreview: not formatted (rustfmt +nightly): {path}\n", ""))

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

    def test_approved_proposal_gate_refuses_an_unformatted_changed_file_and_only_checks(self) -> None:
        ticket = self.approved_reference_ticket()
        self.unformatted(self.sources[0])
        self.assertEqual(self.reference_gate(ticket),
                         (1, f"review: not formatted (rustfmt +nightly): {self.sources[0]}\n"))
        self.assertEqual(self.rustfmt_calls(),
                         [f"+nightly --check --files-with-diff --edition 2021 {self.repo.resolve() / self.sources[0]}"])

    def test_approved_proposal_gate_names_each_unformatted_changed_file_sorted(self) -> None:
        ticket = self.approved_reference_ticket()
        widened = self.sources[3]
        self.write(widened, "pub fn moved() -> usize { 1 }\n")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(style_plan.main(["widen", "--repo", str(self.repo), "--ticket", ticket, "--files", widened,
                                              "--reason", "the move needs a new file", "--by", "showrunner"]), 0)
        self.unformatted(widened, self.sources[0])
        self.assertEqual(self.reference_gate(ticket), (1, "".join(
            f"review: not formatted (rustfmt +nightly): {path}\n" for path in (self.sources[0], widened))))

    def test_approved_proposal_gate_passes_an_unformatted_unchanged_file(self) -> None:
        ticket = self.approved_reference_ticket()
        self.unformatted(self.sources[2])
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))
        self.assertEqual(len(self.rustfmt_calls()), 1)

    def test_approved_proposal_gate_runs_no_rustfmt_without_a_changed_rust_file(self) -> None:
        ticket = self.approved_reference_ticket()
        _ = self.commit()
        self.fake_fmt(stderr="error: rustfmt ran\n", code=1)
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))
        self.assertEqual(self.rustfmt_calls(), [])

    def test_format_check_names_every_changed_rust_file_with_the_edition_of_its_package(self) -> None:
        self.write("Cargo.toml", '[workspace]\nmembers = ["crates/alpha", "crates/beta"]\n'
                   + '[workspace.package]\nedition = "2024"\n')
        self.write("crates/beta/Cargo.toml", '[package]\nname = "beta"\nversion = "0.1.0"\nedition.workspace = true\n')
        self.write("crates/gamma/Cargo.toml", '[package]\nname = "gamma"\nversion = "0.1.0"\n')
        self.write("crates/gamma/src/lib.rs", "pub fn gamma() {}\n")
        _ = self.commit()
        # No module tree reaches the orphan, the example or the build script; a deleted file is not named.
        changed = ("crates/alpha/src/orphan.rs", "crates/beta/build.rs", "crates/beta/examples/demo.rs",
                   self.sources[2], "crates/gamma/src/lib.rs")
        for path in changed:
            self.write(path, "fn  changed() {}\n")
        self.write("docs/notes.md", "Not Rust.\n")
        (self.repo / self.sources[1]).unlink()
        self.assertEqual(style_plan.format_check(self.repo), 0)
        root = self.repo.resolve()
        files = {edition: " ".join(str(root / path) for path in changed if path.startswith(crate))
                 for edition, crate in (("2015", "crates/gamma"), ("2021", "crates/alpha"), ("2024", "crates/beta"))}
        self.assertEqual(self.rustfmt_calls(), [f"+nightly --check --files-with-diff --edition {edition} {names}"
                                                for edition, names in files.items()])

    def test_format_check_refuses_a_changed_file_with_no_edition_as_a_failure_to_run(self) -> None:
        for path, manifest, message in (
                ("tools/loose.rs", "", "tools/loose.rs is in no package, so rustfmt has no edition for it"),
                (self.sources[2], '[package]\nname = "beta"\nedition.workspace = true\n',
                 f"{self.sources[2]}: its package has no edition that rustfmt can take"),
                (self.sources[2], '[package\n', "Cargo.toml: ")):
            with self.subTest(message=message):
                _ = self.git("checkout", "--", ".")
                _ = self.git("clean", "-fdq", "--", "crates", "tools")
                if manifest:
                    self.write("crates/beta/Cargo.toml", manifest)
                self.write(path, "fn changed() {}\n")
                with self.assertRaisesRegex(style_ledger.CheckCouldNotRun, re.escape(message)):
                    _ = style_plan.format_check(self.repo)
        self.assertEqual(self.rustfmt_calls(), [])

    def test_approved_proposal_gate_reads_rustfmt_by_exit_code_and_stdout_and_not_by_notes_on_stderr(self) -> None:
        ticket = self.approved_reference_ticket()
        notes = ("info: syncing channel updates for 'nightly-x86_64-unknown-linux-gnu'\n"
                 + 'Warning: the `version` option is deprecated. Use `style_edition="2024"` instead.\n')
        self.fake_fmt(stderr=notes)
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))
        self.fake_fmt("{cwd}/" + self.sources[0] + "\n", notes, 1)
        self.assertEqual(self.reference_gate(ticket),
                         (1, f"review: not formatted (rustfmt +nightly): {self.sources[0]}\n"))

    def test_approved_proposal_gate_reports_a_failed_format_check_as_exit_two(self) -> None:
        ticket = self.approved_reference_ticket()
        failed = "error: rustfmt +nightly --check failed: "
        for stdout, stderr, code, message in (
                ("", "error: this file contains an unclosed delimiter\n", 1,
                 failed + "error: this file contains an unclosed delimiter"),
                # A parse error in one file still lists the other unformatted files.
                ("{cwd}/" + self.sources[0] + "\n", "Warning: deprecated\nerror: expected item\n", 1,
                 failed + "error: expected item"),
                ("", "Error: file `x.rs` does not exist\n", 1, failed + "Error: file `x.rs` does not exist"),
                ("", "thread 'main' panicked\n", 101, failed + "thread 'main' panicked"),
                ("", "", 1, failed + "exit 1"),
                ("Usage: rustfmt\n", "", 1, failed + "Usage: rustfmt")):
            with self.subTest(message=message):
                self.fake_fmt(stdout, stderr, code)
                self.assertEqual(self.reference_gate(ticket), (2, message + "\n"))
        git = self.root / "git-only/git"
        git.parent.mkdir()
        git.symlink_to(cast(str, shutil.which("git")))
        with mock.patch.dict(os.environ, {"PATH": str(git.parent)}):
            self.assertEqual(self.reference_gate(ticket), (2, "error: cargo +nightly fmt --check could not run: "
                                                              + "[Errno 2] No such file or directory: 'cargo'\n"))

    def test_format_check_takes_the_cargo_verdict_for_visited_files_and_rustfmt_for_every_other_file(self) -> None:
        lib, other, orphan = self.sources[0], self.sources[1], "crates/alpha/src/orphan.rs"
        skipped = ("crates/alpha/src/kept/mod.rs", "crates/alpha/src/skipped.rs", "crates/alpha/src/skipped/deep.rs")
        self.write(lib, "#[rustfmt::skip]\nmod skipped;\n#[cfg_attr(rustfmt, rustfmt_skip)]\nmod kept;\n"
                   + "#[rustfmt::skip]\nmod orphan {}\nmod other;\n")
        _ = self.commit()
        for path in (other, orphan, *skipped):
            self.write(path, "fn  changed() {}\n")
        # cargo visits lib and other and refuses other. It skips the skipped modules and never reaches the orphan, which
        # an inline module of the same name does not declare, so rustfmt checks all four by name.
        self.fake_cargo(self.CARGO_NOTES + f"Formatting {{cwd}}/{lib}\nFormatting {{cwd}}/{other}\n{{cwd}}/{other}\n"
                        + "Spent 0.001 secs\n", 1)
        self.unformatted(orphan, skipped[1])
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(style_plan.format_check(self.repo), 1)
        self.assertEqual(output.getvalue(), f"review: not formatted (rustfmt +nightly): {orphan}\n"
                                            + f"review: not formatted (cargo +nightly fmt): {other}\n"
                                            + f"review: not formatted (rustfmt +nightly): {skipped[1]}\n")
        root = self.repo.resolve()
        self.assertEqual(fake_fmt.calls(self.root / "fmt", "cargo"), [" ".join(style_plan.CARGO_FMT_CHECK[1:])])
        names = " ".join(str(root / path) for path in (skipped[0], orphan, *skipped[1:]))
        self.assertEqual(self.rustfmt_calls(), [f"+nightly --check --files-with-diff --edition 2021 {names}"])

    def test_format_check_reads_only_visited_and_listed_lines_and_judges_the_rest_by_exit_code(self) -> None:
        other = self.sources[1]
        self.write(other, "fn changed() {}\n")
        visit = f"Formatting {{cwd}}/{other}\nA line rustfmt may add one day for {{cwd}}/{other}\nSpent 0.001 secs\n"
        self.fake_cargo(self.CARGO_NOTES + visit)
        self.assertEqual(style_plan.format_check(self.repo), 0)
        self.assertEqual(self.rustfmt_calls(), [])
        self.fake_cargo(self.CARGO_NOTES + visit, 1)
        message = "cargo +nightly fmt --check failed: [lib (2021)]"
        with self.assertRaisesRegex(style_ledger.CheckCouldNotRun, re.escape(message)):
            _ = style_plan.format_check(self.repo)
        self.assertEqual(self.rustfmt_calls(), [])

    def test_format_check_refuses_a_changed_symlink_by_its_own_path(self) -> None:
        spare, alias = "crates/alpha/src/spare.rs", "crates/alpha/src/alias.rs"
        self.write(spare, "fn  spare() {}\n")
        _ = self.commit()
        (self.repo / alias).symlink_to("spare.rs")
        self.fake_cargo(f"Formatting {{cwd}}/{spare}\n{{cwd}}/{spare}\n", 1)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(style_plan.format_check(self.repo), 1)
        self.assertEqual(output.getvalue(), f"review: not formatted (cargo +nightly fmt): {alias}\n")
        outside = self.root / "outside.rs"
        _ = outside.write_text("fn outside() {}\n")
        (self.repo / alias).unlink()
        (self.repo / alias).symlink_to(outside)
        with self.assertRaisesRegex(style_ledger.CheckCouldNotRun, f"{alias} resolves to .*, outside the repo"):
            _ = style_plan.format_check(self.repo)

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
            self.assertIn(f"waiting {issue}: {self.sources[0]} busy\n", output)
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
              mock.patch.object(style_plan, "format_check", return_value=0),
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
        _ = ledger.ticket_yield("names-unit-1", self.sources[0])
        self.assertEqual({(cell.rule, cell.path): cell.state for cell in ledger.cells() if cell.path == self.sources[0]}, before)
        with (mock.patch.object(style_ledger, "open_ledger", return_value=ledger),
              mock.patch.object(style_plan, "synced_style_branch", return_value=""),
              mock.patch.object(style_plan, "format_check", return_value=0),
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
              mock.patch.object(style_plan, "format_check", return_value=0),
              mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run):
            self.assertEqual(style_plan.gate(self.repo, "names-unit-1"), 0)
        arguments = cast(list[str], run.call_args.args[0])
        self.assertEqual(arguments[arguments.index("--base") + 1], "base-from-origin")

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


if __name__ == "__main__":
    _ = unittest.main()
