"""Standing tickets checkpoint code while keeping their plans outside Git."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from typing import ClassVar, cast, final, override

from scripts.lib.py_floor_test_support import interpreter_cache_environment
from scripts.direct import phase_reservation
from scripts.style import fake_fmt
from scripts.style.style_child_test_support import style_cli_command, unqueued_style_children
from scripts.style import mechanical_classes
from scripts.style import style_plan
from scripts.style import style_ledger
from scripts.style import style_footprint
from scripts.style.ledger_test_support import write_style_units
from scripts.style.style_config import StyleUnitAssignment
from scripts.style.style_plan_test_support import private_class_catalog, run_style_command, unit_staff_id
from scripts.style import style_rules
from scripts.production import unit_config
from scripts.production.cli_fixture import process_boundary, process_boundary_for_case
from scripts.production.staff_test_support import fixture_unit_location, isolated_staff_file, recorded_showrunner


SCRIPTS = Path(__file__).resolve().parent


@final
class StandingCheckpointTests(unittest.TestCase):
    fixture_root: ClassVar[Path]
    fixture_snapshot: ClassVar[Path]
    fixture_tip: ClassVar[str]
    fixture_template_before: ClassVar[bytes]
    root: Path = Path()
    repo: Path = Path()
    style: Path = Path()
    unit: Path = Path()
    catalog: Path = Path()
    bin: Path = Path()
    env: dict[str, str] = {}
    tip: str = ""
    doc: Path = Path()
    plan: Path = Path()
    template: Path = Path()
    template_before: bytes = b""
    rule: str = ""

    @classmethod
    @override
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.fixture_root = Path(cls.enterClassContext(tempfile.TemporaryDirectory(prefix="standing-checkpoint-"))) / "case"
        cls.fixture_snapshot = cls.fixture_root.with_name("snapshot")
        builder = cls(methodName="runTest")
        try:
            builder.bind_fixture(cls.fixture_root)
            builder.build_fixture()
            cls.fixture_tip = builder.tip
            cls.fixture_template_before = builder.template_before
            _ = shutil.copytree(cls.fixture_root, cls.fixture_snapshot, symlinks=True)
        finally:
            builder.doCleanups()
        shutil.rmtree(cls.fixture_root)

    @override
    def setUp(self) -> None:
        self.addCleanup(shutil.rmtree, self.fixture_root)
        # Restore the original paths for Git's linked worktree metadata.
        _ = shutil.copytree(self.fixture_snapshot, self.fixture_root, symlinks=True)
        self.bind_fixture(self.fixture_root)
        self.tip = self.fixture_tip
        self.template_before = self.fixture_template_before
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", self.env))
        # Refresh copied index stat data for the new file metadata.
        for checkout in (self.repo, self.style, self.unit):
            _ = self.git(checkout, "update-index", "--refresh")

    def bind_fixture(self, root: Path) -> None:
        self.root = root
        self.repo = self.root / "sample"
        self.style = self.root / "sample-style"
        self.unit = self.root / "sample-style-names"
        self.catalog = self.root / "nate_style/rust"
        self.bin = self.root / "bin"
        self.env = {
            **os.environ, **interpreter_cache_environment(self.root / "cache"), "HOME": str(self.root / "home"),
            "STYLE_LEDGER_STATE_DIR": str(self.root / "state"),
            "STYLE_HISTORY_NATE_STYLE_DIR": str(self.catalog.parent),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            fake_fmt.STATE: str(self.root / "fmt"),
        }
        self.doc = self.style / "docs/sample-style-production.md"
        self.plan = self.unit / "docs/style-production/units/names-unit.md"
        self.template = self.style / "docs/style-production/units/names-unit.md"
        self.rule = "global:rust/names.md"

    def build_fixture(self) -> None:
        self.catalog.mkdir(parents=True)
        # Tier E catalog rules have no cells.
        with private_class_catalog(self.root):
            listed = mechanical_classes.classes()
        for rule in sorted({rule for entry in listed for rule in entry.rules}):
            self.write(self.catalog / f"{rule}.md", f"---\ntier: E\n---\n# {rule}\n")
        self.bin.mkdir()
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", self.env))
        fake_fmt.install(self.bin, self.root / "fmt")
        board = self.bin / "cargo-berth"
        _ = board.write_text(
            f"#!{sys.executable}\nimport json\n"
            + "print(json.dumps({'output_contract_version': 1, 'verb': 'board', "
            + "'status': 'unconfigured', 'payload': {'kind': 'no_facts'}}))\nraise SystemExit(4)\n"
        )
        board.chmod(0o755)
        self.repo.mkdir()
        _ = self.git(self.repo, "init", "-q", "--initial-branch=main")
        _ = self.git(self.repo, "config", "user.name", "Standing checkpoint tests")
        _ = self.git(self.repo, "config", "user.email", "standing@example.invalid")
        self.write(self.repo / "Cargo.toml", '[workspace]\nmembers = ["alpha", "beta"]\n')
        for member in ("alpha", "beta"):
            self.write(self.repo / member / "Cargo.toml", f'[package]\nname = "{member}"\nversion = "0.1.0"\n')
            self.write(self.repo / member / "src/lib.rs", "fn value() -> usize {\n    1\n}\n")
        _ = self.git(self.repo, "add", "--all")
        _ = self.git(self.repo, "commit", "-qm", "Initial sources")
        self.tip = self.git(self.repo, "rev-parse", "HEAD")
        _ = self.git(self.repo, "worktree", "add", "-q", "-b", "style/main", str(self.style))
        _ = self.git(self.repo, "worktree", "add", "-q", "-b", "sample-style-names", str(self.unit))
        self.write(self.repo / ".git/info/exclude", "/docs/style-production/\n/docs/sample-style-production.md\n/docs/sample-style-log.md\n")
        parent = self.repo / "docs/product-production.md"
        # The parent is outside style's excluded doc pattern; exclude it separately.
        with (self.repo / ".git/info/exclude").open("a") as exclude:
            _ = exclude.write("/docs/product-production.md\n")
        self.write(parent, self.production("main", self.repo))
        self.write(self.doc, self.production("style/main", self.style) + "\n- **Kind:** style\n- **Branch point:** main\n")
        write_style_units(self.doc, (StyleUnitAssignment("names", ("names", "values", "types", "modules")),))
        _ = recorded_showrunner("sample", parent)
        owner = recorded_showrunner("sample-style", self.doc, parent="sample")
        _ = fixture_unit_location(owner, "names", self.unit)
        _ = unit_config.write(self.unit, unit_config.UnitConfig(
            unit_config.PlanFile(self.plan.relative_to(self.unit)), unit_config.NoAppPort(), ()))
        header = "\n".join([
            "# Standing names", "> **Standing:** style_plan.py",
            "", "## Delegation Context", "Read the pinned names rule and judge every ticket file.",
            "", "## Phases", "",
        ])
        self.write(self.plan, header)
        self.write(self.template, header + "Showrunner-only template line.\n")
        self.template_before = self.template.read_bytes()
        self.write(self.catalog / "names.md", "---\ntier: C\nfamily: names\ninput_scope: file\n---\n# Names\nUse clear names.\n")
        result = self.cli("style_ledger.py", "set-tip", "--repo", str(self.repo), "--tip", self.tip, "--style-branch", "style/main")
        self.assertEqual(result.returncode, 0, result.stderr)

    def write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(text)

    def git(self, checkout: Path, *args: str) -> str:
        result = subprocess.run(["git", "-C", str(checkout), *args], env=self.env,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def cli(self, script: str, *args: str) -> subprocess.CompletedProcess[str]:
        if process_boundary_for_case(self):
            return subprocess.run(style_cli_command(SCRIPTS / script, *args), cwd=self.unit,
                                  env=self.env, capture_output=True, text=True, check=False)
        run_command = partial(run_style_command, self.root, self)
        with (unqueued_style_children(),
              mock.patch.object(style_rules, "NATE_STYLE_DIR", self.catalog.parent),
              mock.patch.object(subprocess, "run", side_effect=run_command)):
            return run_command([str(SCRIPTS.parent / "lib/py"), str(SCRIPTS / script), *args],
                               cwd=self.unit, env=self.env, capture_output=True, text=True, check=False)

    def rustfmt_calls(self) -> list[str]:
        return fake_fmt.calls(self.root / "fmt", "rustfmt")

    def rustfmt_call(self, path: str) -> str:
        """The fake rustfmt call of the format check for one changed file; the fixture packages have no edition."""
        return f"+nightly --check --files-with-diff --edition 2015 {self.unit.resolve() / path}"

    def production(self, branch: str, checkout: Path) -> str:
        return "\n".join([
            "# Production", f"- **Merge branch:** {branch}", f"- **Showrunner checkout:** {checkout}",
            "- **Log:** docs/sample-style-log.md", "",
        ])

    def refill(self) -> subprocess.CompletedProcess[str]:
        return self.cli("style_plan.py", "next-phase", "--unit", "names", "--plan", str(self.plan), "--repo", str(self.unit))

    def open_ticket(self) -> style_ledger.TicketRecord:
        result = self.cli("style_ledger.py", "ticket", "open", "--repo", str(self.unit), "--unit", "names")
        self.assertEqual(result.returncode, 0, result.stderr)
        return cast(style_ledger.TicketRecord, json.loads(result.stdout))

    def gate(self) -> subprocess.CompletedProcess[str]:
        match = re.search(r"`([^`]*style_plan\.py gate[^`]*)`", self.plan.read_text())
        self.assertIsNotNone(match)
        if match is None:
            self.fail("ticket has no origin gate")
        command = shlex.split(match[1])
        return self.cli("style_plan.py", *command[2:])

    def record_ticket_verdicts(self, ticket: style_ledger.TicketRecord) -> None:
        head = self.git(self.unit, "rev-parse", "HEAD")
        for rule in ticket["rules"]:
            for path in ticket["files"]:
                result = self.cli("style_ledger.py", "record", "--repo", str(self.unit), "--rule", rule,
                                  "--path", path, "--commit", head, "--verdict", "clean", "--ticket", ticket["id"], "--worker", "test")
                self.assertEqual(result.returncode, 0, result.stderr)

    def parallel_tickets(self) -> tuple[style_ledger.TicketRecord, style_ledger.TicketRecord]:
        write_style_units(self.doc, (StyleUnitAssignment("names", ("names", "values", "types", "modules"), 2),))
        _ = self.enterContext(unqueued_style_children())
        self.env["SCRATCH_COPIES_ROOT"] = str(self.root / "copies")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, self.env)))
        result = self.refill()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        with style_ledger.open_ledger(self.unit) as ledger:
            first, second = ledger.ticket_open(unit_staff_id("names"))
        for ticket in (first, second):
            self.record_ticket_verdicts(ticket)
        return first, second

    def publish(self, ticket: str) -> str:
        run_command = partial(run_style_command, self.root, self)
        with mock.patch.object(style_plan, "subprocess", SimpleNamespace(
                run=run_command, CalledProcessError=subprocess.CalledProcessError)):
            return style_plan.publish_checkpoint(self.unit, ticket, self.root / "run", ticket, "Checkpoint test\n")

    def test_single_ticket_publishes_a_mechanical_file_outside_its_footprint_and_runs_doc_in_the_tree(self) -> None:
        extra = "alpha/src/extra.rs"
        self.write(self.repo / extra, "fn extra() {}\n")
        _ = self.git(self.repo, "add", extra)
        _ = self.git(self.repo, "commit", "-qm", "Add another source")
        self.tip = self.git(self.repo, "rev-parse", "HEAD")
        for worktree in (self.unit, self.style):
            _ = self.git(worktree, "merge", "--no-edit", self.tip)
        _ = self.enterContext(unqueued_style_children())
        self.enterContext(mock.patch.dict(os.environ, self.env))
        _ = self.enterContext(mock.patch.object(style_rules, "NATE_STYLE_DIR", self.catalog.parent))
        with style_ledger.open_ledger(self.unit) as ledger:
            ledger.set_tip(self.tip, "style/main")
            ledger.ticket_add("1", unit_staff_id("names"), "read", "names", "alpha", ["alpha/src/lib.rs"], [self.rule])
        self.assertEqual(self.refill().returncode, 0)
        ticket = self.open_ticket()
        self.record_ticket_verdicts(ticket)
        self.assertNotIn(extra, ticket["files"])
        self.write(self.unit / ticket["files"][0], "fn value() -> usize {\n        1\n}\n")
        self.write(self.unit / extra, "fn extra() { }\n")
        def verify(tree: Path, stage: str, package: str) -> subprocess.CompletedProcess[bytes]:
            self.assertEqual(tree, self.unit)
            if stage == "doc":
                self.assertEqual(set(self.git(tree, "diff", "--cached", "--name-only").splitlines()),
                                 {ticket["files"][0], extra})
            return subprocess.CompletedProcess([stage, package], 0)
        with mock.patch.object(style_plan, "run_verification_step", side_effect=verify), \
                mock.patch.object(style_plan, "candidate_source", side_effect=AssertionError("N=1 uses the editing tree")):
            # Keep one publication path through the real footprint child.
            checkpoint = style_plan.publish_checkpoint(
                self.unit, ticket["id"], self.root / "run", ticket["id"], "Checkpoint test\n")
        self.assertEqual(set(self.git(self.unit, "diff-tree", "--no-commit-id", "--name-only", "-r", checkpoint).splitlines()),
                         {ticket["files"][0], extra})
        self.assertEqual(self.git(self.unit, "status", "--porcelain"), "")

    def test_publication_preserves_partial_sibling_bytes_and_index_entries(self) -> None:
        first, second = self.parallel_tickets()
        own, sibling = first["files"][0], second["files"][0]
        self.write(self.unit / own, "fn value() -> usize {\n        1\n}\n")
        self.write(self.unit / sibling, "fn unfinished(\n")
        _ = self.git(self.unit, "add", sibling)
        self.write(self.unit / sibling, "fn unfinished( with_more_bytes\n")
        extra = str(Path(sibling).parent / "partial.rs")
        self.write(self.unit / extra, "fn partial(\n")
        before_entry = self.git(self.unit, "ls-files", "--stage", "--", sibling)
        before_bytes = {path: (self.unit / path).read_bytes() for path in (sibling, extra)}
        def verify_source(tree: Path, stage: str, package: str) -> subprocess.CompletedProcess[bytes]:
            self.assertNotEqual(tree, self.unit)
            self.assertEqual((tree / sibling).read_text(), "fn value() -> usize {\n    1\n}\n")
            self.assertFalse((tree / extra).exists())
            return subprocess.CompletedProcess([stage, package], 0)
        with mock.patch.object(style_plan, "run_verification_step", side_effect=verify_source) as verify:
            checkpoint = self.publish(first["id"])
        self.assertEqual(self.git(self.unit, "rev-parse", "HEAD"), checkpoint)
        self.assertEqual(self.git(self.unit, "diff-tree", "--no-commit-id", "--name-only", "-r", checkpoint), own)
        self.assertEqual(self.git(self.unit, "ls-files", "--stage", "--", sibling), before_entry)
        self.assertEqual({path: (self.unit / path).read_bytes() for path in before_bytes}, before_bytes)
        self.assertEqual(self.git(self.unit, "diff", "--cached", "--name-only", "--", own), "")
        self.assertEqual([call.args[1] for call in verify.call_args_list], ["check", "lint", "test", "doc"])
        trees = {call.args[0] for call in verify.call_args_list}
        self.assertEqual(len(trees), 1)
        self.assertNotIn(self.unit, trees)

    def test_lower_capacity_keeps_existing_tickets_on_candidate_sources(self) -> None:
        first, second = self.parallel_tickets()
        write_style_units(self.doc, (StyleUnitAssignment("names", ("names", "values", "types", "modules")),))
        self.write(self.unit / first["files"][0], "fn value() -> usize {\n        1\n}\n")
        self.write(self.unit / second["files"][0], "fn unfinished(\n")
        with style_ledger.open_ledger(self.unit) as ledger:
            self.assertTrue(style_plan.isolated_ticket_source(ledger, first))
        run_command = partial(run_style_command, self.root, self)
        with mock.patch.object(style_plan, "subprocess", SimpleNamespace(
                run=run_command, CalledProcessError=subprocess.CalledProcessError)):
            self.assertEqual(style_plan.gate(self.unit, first["id"]), 0)

    def test_publication_recovers_a_crash_before_branch_movement(self) -> None:
        first, _ = self.parallel_tickets()
        path = first["files"][0]
        self.write(self.unit / path, "fn value() -> usize {\n        1\n}\n")
        before = self.git(self.unit, "rev-parse", "HEAD")
        with mock.patch.object(style_plan, "run_verification_step", return_value=subprocess.CompletedProcess([], 0)), \
                mock.patch.object(style_plan, "complete_publication", side_effect=RuntimeError("crash")):
            with self.assertRaisesRegex(RuntimeError, "crash"):
                _ = self.publish(first["id"])
        self.assertEqual(self.git(self.unit, "rev-parse", "HEAD"), before)
        journal = style_ledger.publication_path(self.unit, "publication.json")
        pending = cast(dict[str, str], json.loads(journal.read_text()))["commit"]
        self.assertEqual(self.publish(first["id"]), pending)
        self.assertFalse(journal.exists())
        self.assertEqual(self.git(self.unit, "rev-list", "--count", before + "..HEAD"), "1")

    def test_publication_recovers_after_branch_movement_and_releases_at_its_checkpoint(self) -> None:
        first, second = self.parallel_tickets()
        self.write(self.unit / first["files"][0], "fn value() -> usize {\n        1\n}\n")
        directory = phase_reservation.phase_directory(self.root / "run", first["id"])
        directory.mkdir(parents=True)
        state = {"kind": phase_reservation.ACTIVE, "active_phase_reservation": {"reservation_id": "a"}}
        self.write(directory / phase_reservation.STATE_FILE, json.dumps(state))
        with mock.patch.object(style_plan, "run_verification_step", return_value=subprocess.CompletedProcess([], 0)), \
                mock.patch.object(style_plan, "update_ticket_index", side_effect=RuntimeError("crash")):
            with self.assertRaisesRegex(RuntimeError, "crash"):
                _ = self.publish(first["id"])
        checkpoint = self.git(self.unit, "rev-parse", "HEAD")
        released: list[str] = []
        def release(phase_dir: Path, ticket: str, merged: str) -> str:
            self.assertEqual(ticket, first["id"])
            self.assertEqual(self.git(self.unit, "rev-parse", "HEAD"), checkpoint)
            self.assertEqual(merged, checkpoint)
            released.append(ticket)
            _ = phase_reservation.clear(phase_dir, checkpoint, self.unit)
            return "a"
        with mock.patch.object(phase_reservation, "release_held", side_effect=release):
            self.assertEqual(self.publish(first["id"]), checkpoint)
        self.assertEqual(released, [first["id"]])
        with style_ledger.open_ledger(self.unit) as ledger:
            self.assertEqual(ledger.ticket_get(second["id"])["state"], "assigned")

    def test_publication_follows_finish_order(self) -> None:
        first, second = self.parallel_tickets()
        for ticket in (first, second):
            self.write(self.unit / ticket["files"][0], "fn value() -> usize {\n        1\n}\n")
        with mock.patch.object(style_plan, "run_verification_step", return_value=subprocess.CompletedProcess([], 0)):
            second_commit = self.publish(second["id"])
            first_commit = self.publish(first["id"])
        self.assertEqual(self.git(self.unit, "rev-parse", first_commit + "^"), second_commit)
        self.assertEqual(self.git(self.unit, "diff-tree", "--no-commit-id", "--name-only", "-r", first_commit), first["files"][0])

    def test_ticket_candidate_excludes_staged_unstaged_and_untracked_sibling_work(self) -> None:
        own = "alpha/src/lib.rs"
        staged = "beta/src/lib.rs"
        unstaged = "beta/Cargo.toml"
        untracked = "beta/src/partial.rs"
        head = self.git(self.unit, "rev-parse", "HEAD")
        self.write(self.unit / own, "fn value() -> usize { 2 }\n")
        self.write(self.unit / staged, "fn sibling() {\n")
        _ = self.git(self.unit, "add", staged)
        self.write(self.unit / staged, "fn sibling() { unfinished\n")
        self.write(self.unit / unstaged, "[unfinished\n")
        self.write(self.unit / untracked, "fn partial(\n")
        index = Path(self.git(self.unit, "rev-parse", "--path-format=absolute", "--git-path", "index"))
        before_index = index.read_bytes()
        before_status = self.git(self.unit, "status", "--porcelain=v1", "-uall")
        with mock.patch.dict(os.environ, self.env):
            candidate = style_footprint.worktree_commit(self.unit, style_footprint.TicketSourcePaths((own,)))
        self.assertEqual(self.git(self.unit, "rev-parse", candidate + "^"), head)
        self.assertEqual(self.git(self.unit, "diff", "--name-only", head, candidate), own)
        self.assertEqual(self.git(self.unit, "show", candidate + ":" + own), "fn value() -> usize { 2 }")
        for path in (staged, unstaged):
            self.assertEqual(self.git(self.unit, "show", candidate + ":" + path), self.git(self.unit, "show", head + ":" + path))
        self.assertEqual(self.git(self.unit, "ls-tree", candidate, "--", untracked), "")
        self.assertEqual(index.read_bytes(), before_index)
        self.assertEqual(self.git(self.unit, "status", "--porcelain=v1", "-uall"), before_status)
        self.assertEqual((self.unit / staged).read_text(), "fn sibling() { unfinished\n")
        self.assertEqual((self.unit / unstaged).read_text(), "[unfinished\n")
        self.assertEqual((self.unit / untracked).read_text(), "fn partial(\n")

    def test_ticket_candidate_records_a_rename_and_ignores_absent_paths(self) -> None:
        old = "alpha/src/lib.rs"
        new = "alpha/src/moved.rs"
        head = self.git(self.unit, "rev-parse", "HEAD")
        _ = (self.unit / old).rename(self.unit / new)
        with mock.patch.dict(os.environ, self.env):
            candidate = style_footprint.worktree_commit(
                self.unit, style_footprint.TicketSourcePaths((old, new, "alpha/src/absent.rs")))
        self.assertEqual(self.git(self.unit, "diff", "--name-status", "--no-renames", head, candidate).splitlines(),
                         ["D\t" + old, "A\t" + new])
        self.assertEqual(self.git(self.unit, "show", candidate + ":" + new), self.git(self.unit, "show", head + ":" + old))

    def test_unchanged_ticket_candidate_has_an_exact_parent(self) -> None:
        head = self.git(self.unit, "rev-parse", "HEAD")
        with mock.patch.dict(os.environ, self.env):
            candidate = style_footprint.worktree_commit(
                self.unit, style_footprint.TicketSourcePaths(("alpha/src/lib.rs",)))
            self.assertEqual(style_footprint.worktree_commit(self.unit), head)
        self.assertNotEqual(candidate, head)
        self.assertEqual(self.git(self.unit, "rev-parse", candidate + "^"), head)
        self.assertEqual(self.git(self.unit, "diff", "--name-only", head, candidate), "")

    def test_approved_non_mechanical_work_passes_both_gates_and_reaches_checkpoint(self) -> None:
        # The ordinary case below exercises the same literal edit through the coverage gate.
        issue = self.root / "issues/approved.md"
        issue.parent.mkdir()
        with mock.patch.dict(os.environ, self.env), mock.patch.object(style_rules, "NATE_STYLE_DIR", self.catalog.parent):
            with style_ledger.open_ledger(self.repo) as ledger:
                rule_blob = self.git(self.repo, "hash-object", str(self.catalog / "names.md"))
                path = "alpha/src/lib.rs"
                finding = ledger.hold(self.rule, path, 2, "1", "type", "1", "2", rule_blob, "outside")
                ledger.file_held(finding.finding_id, issue)
        _ = issue.write_text('---\nstatus: open\nstage: backlog\ntags: [issue, proposal, style]\n'
                             + f'unit: {unit_staff_id("names")}\nplan: "{self.plan} Phase 1"\nstyle_rule: {self.rule}\nstyle_crate: alpha\n'
                             + f'style_rule_blob: {rule_blob}\nstyle_count: 1\n---\n')
        _ = self.doc.write_text(self.doc.read_text() + f"- **Proposals:** {issue.parent}\n")
        result = self.refill()
        self.assertEqual(result.returncode, 0, result.stderr)
        ticket = self.open_ticket()
        self.assertEqual(ticket["kind"], "proposal")
        source = self.unit / path
        _ = source.write_text(source.read_text().replace("    1\n", "    2\n"))
        work_order = self.gate()
        self.assertEqual(work_order.returncode, 0, work_order.stdout + work_order.stderr)
        self.assertEqual(self.rustfmt_calls(), [self.rustfmt_call(path)])
        checkpoint_gate = self.cli("style_plan.py", "gate", "--repo", ".", "--ticket", ticket["id"])
        self.assertEqual(checkpoint_gate.returncode, 0, checkpoint_gate.stdout + checkpoint_gate.stderr)
        outside = self.unit / "unexpected.rs"
        _ = outside.write_text("fn outside() {}\n")
        self.assertEqual(self.cli("style_plan.py", "gate", "--repo", ".", "--ticket", ticket["id"]).returncode, 1)
        outside.unlink()
        _ = self.git(self.unit, "add", "--", path)
        _ = self.git(self.unit, "commit", "-qm", "Approved code checkpoint")
        commit = self.git(self.unit, "rev-parse", "HEAD")
        recorded = self.cli("style_ledger.py", "ticket", "checkpoint", "--repo", ".", "--id", ticket["id"], "--commit", commit)
        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        self.assertEqual(self.open_ticket()["checkpoint"], commit)

    def test_code_checkpoint_stages_only_status_code_paths_and_keeps_the_template_unchanged(self) -> None:
        result = self.refill()
        self.assertEqual(result.returncode, 0, result.stderr)
        ticket = self.open_ticket()
        path = ticket["files"][0]
        source = self.unit / path
        _ = source.write_text(source.read_text().replace("value", "count"))
        self.record_ticket_verdicts(ticket)
        gate = self.gate()
        self.assertEqual((gate.returncode, gate.stdout), (0, "mechanical\n"), gate.stderr)
        self.assertEqual(self.rustfmt_calls(), [self.rustfmt_call(path)])
        # Retain the status columns while extracting the code paths to stage.
        raw = subprocess.run(["git", "-C", str(self.unit), "status", "--porcelain"], env=self.env,
                             capture_output=True, text=True, check=True).stdout
        paths = [line[3:] for line in raw.splitlines()]
        self.assertEqual(paths, [path])
        _ = self.git(self.unit, "add", "--", *paths)
        self.assertEqual(self.git(self.unit, "diff", "--cached", "--name-only"), path)
        refused = subprocess.run(["git", "-C", str(self.unit), "add", "--", str(self.plan)], env=self.env,
                                 capture_output=True, text=True, check=False)
        self.assertEqual(refused.returncode, 1)
        self.assertEqual(self.git(self.unit, "diff", "--cached", "--name-only"), path)
        _ = self.git(self.unit, "commit", "-qm", "Mechanical code checkpoint")
        commit = self.git(self.unit, "rev-parse", "HEAD")
        result = self.cli("style_ledger.py", "ticket", "checkpoint", "--repo", str(self.unit), "--id", ticket["id"], "--commit", commit)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.open_ticket()["checkpoint"], commit)
        self.assertEqual(self.git(self.unit, "diff-tree", "--no-commit-id", "--name-only", "-r", commit), path)
        self.assertTrue(tuple((self.plan.parent.parent / "rules").glob("*.md")))
        self.assertEqual(self.template.read_bytes(), self.template_before)

    def close_without_code(self, verdict: str) -> None:
        result = self.refill()
        self.assertEqual(result.returncode, 0, result.stderr)
        ticket = self.open_ticket()
        head = self.git(self.unit, "rev-parse", "HEAD")
        count = self.git(self.unit, "rev-list", "--count", "HEAD")
        if verdict == "held":
            held = self.cli("style_ledger.py", "held", "add", "--repo", str(self.unit), "--ticket", ticket["id"],
                            "--rule", self.rule, "--rule-blob", ticket["rule_blobs"][self.rule], "--path", ticket["files"][0],
                            "--line", "1", "--text", "fn value()", "--kind", "rename", "--before", "value", "--after", "count")
            self.assertEqual(held.returncode, 0, held.stderr)
        for path in ticket["files"]:
            result = self.cli("style_ledger.py", "record", "--repo", str(self.unit), "--rule", self.rule,
                              "--path", path, "--commit", head, "--verdict", verdict, "--ticket", ticket["id"], "--worker", "test")
            self.assertEqual(result.returncode, 0, result.stderr)
        _ = self.git(self.unit, "merge-base", "--is-ancestor", head, "refs/heads/style/main")
        for action, tail in (("checkpoint", ("--commit", head)), ("merge", ())):
            result = self.cli("style_ledger.py", "ticket", action, "--repo", str(self.unit), "--id", ticket["id"], *tail)
            self.assertEqual(result.returncode, 0, result.stderr)
        with closing(sqlite3.connect(self.root / "state/sample/ledger.sqlite")) as connection:
            rows = cast(list[tuple[str, str]], connection.execute("SELECT state,checkpoint FROM tickets WHERE id=?", (ticket["id"],)).fetchall())
            self.assertEqual(rows, [("merged", head)])
            held_count = cast(tuple[int], connection.execute("SELECT COUNT(*) FROM held_findings").fetchone())[0]
            self.assertEqual(held_count, int(verdict == "held"))
        self.assertEqual(self.git(self.unit, "rev-list", "--count", "HEAD"), count)
        self.assertEqual(self.git(self.unit, "status", "--porcelain"), "")
        result = self.refill()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"discarded {ticket['id']}: merged", result.stdout)
        self.assertIn("phase written: 2", result.stdout)
        self.assertNotIn(f"### Phase 1 — {ticket['id']}", self.plan.read_text())
        self.assertIn("### Phase 2 — 2", self.plan.read_text())
        self.assertEqual(self.template.read_bytes(), self.template_before)
        self.assertEqual(self.git(self.unit, "rev-parse", "HEAD"), head)

    def test_clean_only_ticket_merges_and_refills_without_a_commit(self) -> None:
        self.close_without_code("clean")

    def test_held_only_ticket_merges_and_refills_without_a_commit(self) -> None:
        self.close_without_code("held")

    def test_uncommitted_literal_change_fails_the_ticket_gate_at_its_unchanged_base(self) -> None:
        result = self.refill()
        self.assertEqual(result.returncode, 0, result.stderr)
        ticket = self.open_ticket()
        self.assertEqual(self.git(self.unit, "rev-parse", "HEAD"), ticket["base"])
        path = self.unit / ticket["files"][0]
        _ = path.write_text(path.read_text().replace("    1\n", "    2\n"))
        self.record_ticket_verdicts(ticket)
        result = self.gate()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("review: check 6:", result.stdout)
        checkpoint_gate = self.cli("style_plan.py", "gate", "--repo", ".", "--ticket", ticket["id"])
        self.assertEqual(checkpoint_gate.returncode, 1)
        self.assertIn("review: check 6:", checkpoint_gate.stdout)
        self.assertEqual(self.git(self.unit, "rev-parse", "HEAD"), ticket["base"])
        self.assertEqual(self.open_ticket()["state"], "assigned")
        self.assertEqual(self.git(self.unit, "diff", "--cached", "--name-only"), "")

    @process_boundary
    def test_notify_cli_is_silent_for_another_branch_point_and_an_absent_production(self) -> None:
        for present in (True, False):
            if not present:
                self.doc.unlink()
            result = self.cli("style_sync.py", "notify", "--repo-dir", str(self.unit), "--branch", "other", "--tip", self.tip)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))


if __name__ == "__main__":
    _ = unittest.main()
