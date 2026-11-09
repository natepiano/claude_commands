"""Standing tickets checkpoint code while keeping their plans outside Git."""

from __future__ import annotations

import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock
from typing import cast, final, override

from scripts.style import fake_fmt
from scripts.style.style_child_test_support import style_cli_command
from scripts.style import mechanical_classes
from scripts.style import style_ledger
from scripts.style import style_footprint
from scripts.style.ledger_test_support import write_style_units
from scripts.style.style_config import StyleUnitAssignment
from scripts.style.style_plan_test_support import private_class_catalog, unit_staff_id
from scripts.style import style_rules
from scripts.production import unit_config
from scripts.production.staff_test_support import fixture_unit_location, isolated_staff_file, recorded_showrunner


SCRIPTS = Path(__file__).resolve().parent


@final
class StandingCheckpointTests(unittest.TestCase):
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

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="standing-checkpoint-")))
        self.repo = self.root / "sample"
        self.style = self.root / "sample-style"
        self.unit = self.root / "sample-style-names"
        self.catalog = self.root / "nate_style/rust"
        self.catalog.mkdir(parents=True)
        # The CLI checks the class list against this catalog, so each listed rule is here as a tier E rule, which has
        # no cells.
        with private_class_catalog(self.root):
            listed = mechanical_classes.classes()
        for rule in sorted({rule for entry in listed for rule in entry.rules}):
            self.write(self.catalog / f"{rule}.md", f"---\ntier: E\n---\n# {rule}\n")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.env = {
            **os.environ, "HOME": str(self.root / "home"),
            "STYLE_LEDGER_STATE_DIR": str(self.root / "state"),
            "STYLE_HISTORY_NATE_STYLE_DIR": str(self.catalog.parent),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            fake_fmt.STATE: str(self.root / "fmt"),
        }
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
        self.doc = self.style / "docs/sample-style-production.md"
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
        self.plan = self.unit / "docs/style-production/units/names-unit.md"
        _ = unit_config.write(self.unit, unit_config.UnitConfig(
            unit_config.PlanFile(self.plan.relative_to(self.unit)), unit_config.NoAppPort(), ()))
        self.template = self.style / "docs/style-production/units/names-unit.md"
        header = "\n".join([
            "# Standing names", "> **Standing:** style_plan.py",
            "", "## Delegation Context", "Read the pinned names rule and judge every ticket file.",
            "", "## Phases", "",
        ])
        self.write(self.plan, header)
        self.write(self.template, header + "Showrunner-only template line.\n")
        self.template_before = self.template.read_bytes()
        self.rule = "global:rust/names.md"
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
        return subprocess.run(style_cli_command(SCRIPTS / script, *args), cwd=self.unit,
                              env=self.env, capture_output=True, text=True, check=False)

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

    def test_notify_cli_is_silent_for_another_branch_point_and_an_absent_production(self) -> None:
        for present in (True, False):
            if not present:
                self.doc.unlink()
            result = self.cli("style_sync.py", "notify", "--repo-dir", str(self.unit), "--branch", "other", "--tip", self.tip)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))


if __name__ == "__main__":
    _ = unittest.main()
