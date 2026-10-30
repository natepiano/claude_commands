"""Independent copied repositories and CLI fixtures for the ledger subject suites."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from collections.abc import Callable
from pathlib import Path
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from typing import TypedDict, cast, override
from unittest import mock
import rules
import style_ledger
from approved_renames import AppliedCheck
import style_plan
import style_rules
import declarations
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


# A release checkpoints the current worktree; each fake berth keeps its own board.
PARENT_FILE = "crates/alpha/src/a.rs"

RELEASE_BERTH = r'''#!__PYTHON__
import json, os, subprocess, sys
with open(os.environ["LEDGER_BERTH_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\n")
path = os.environ["LEDGER_BOARD"]
board = json.load(open(path)) if os.path.exists(path) else {}
if sys.argv[1] == "release":
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    board[sys.argv[2]] = {"status": "outstanding", "protected_tip": head}
    json.dump(board, open(path, "w"))
print(json.dumps({"payload": {"data": {"lifecycle": board.get(sys.argv[-2], {"status": "active"})}}}))
'''

MOVED_FILE = "crates/alpha/src/b.rs"

TWIN_FILE = "crates/alpha/src/c.rs"

PARENT = "pub struct Parent {\n    old: u32,\n}\n"

# The same declaration as PARENT: an inline module is not part of it.
TWIN = "#[cfg(test)]\nmod tests {\n    struct Parent {\n        old: u32,\n    }\n}\n"

TRAIT_METHOD = "pub trait Trait {\n    fn old(&self) -> u32;\n}\npub struct Type;\nimpl Trait for Type {\n    fn old(&self) -> u32 {\n        7\n    }\n}\n"

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

class LedgerTestCase(unittest.TestCase):
    root: Path = Path()

    repo: Path = Path()

    rule_dir: Path = Path()

    initial_tip: str = ""

    sources: tuple[str, ...] = (
        "crates/alpha/src/lib.rs", "crates/alpha/src/other.rs",
        "crates/beta/src/lib.rs", "crates/beta/src/other.rs",
    )

    template_repo: Path = Path()
    template_tip: str = ""

    @classmethod
    @override
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.template_repo = Path(cls.enterClassContext(tempfile.TemporaryDirectory())) / "sample"
        cls.template_repo.mkdir()
        fixture = cls(methodName="runTest")
        fixture.repo = cls.template_repo
        _ = fixture.git("init", "-q", "--initial-branch=main")
        _ = fixture.git("config", "user.name", "Ledger tests")
        _ = fixture.git("config", "user.email", "ledger-tests@example.invalid")
        fixture.write("Cargo.toml", '[workspace]\nmembers = ["crates/alpha", "crates/beta"]\nresolver = "2"\n')
        for member in ("alpha", "beta"):
            fixture.write(
                f"crates/{member}/Cargo.toml",
                f'[package]\nname = "{member}"\nversion = "0.1.0"\nedition = "2021"\n',
            )
        for source in fixture.sources:
            fixture.write(source, "pub fn value() -> u32 { 1 }\n" +
                       ("mod a; mod b; mod c; mod other;\n" if source.endswith("src/lib.rs") else ""))
        fixture.write("vendor/third-party/src/lib.rs", "pub fn external() {}\n")
        fixture.write("crates/alpha/vendor/embedded.rs", "pub fn embedded() {}\n")
        fixture.write("README.md", "A sample workspace\n")
        cls.template_tip = fixture.commit("Create workspace")

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = self.root / "sample"
        self.rule_dir = self.root / "nate_style" / "rust"
        _ = shutil.copytree(self.template_repo, self.repo, symlinks=True)
        self.rule_dir.mkdir(parents=True)
        self.initial_tip = self.template_tip
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "STYLE_LEDGER_STATE_DIR": str(self.root / "state"),
            "STYLE_HISTORY_NATE_STYLE_DIR": str(self.rule_dir.parent),
        })))
        _ = self.enterContext(mock.patch.object(style_rules, "NATE_STYLE_DIR", self.rule_dir.parent))

    def git(self, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), *arguments], check=True, capture_output=True, text=True,
        ).stdout.strip()

    def write(self, relative: str, content: str) -> None:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(content)

    def in_worktree(self, relative: str) -> bool:
        return os.path.lexists(self.repo / relative)

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

    def cli_command(self, arguments: tuple[str, ...]) -> list[str]:
        prefix = 2 if arguments[0] in ("ticket", "handoff", "tip", "held", "unit") else 1
        return [sys.executable, str(Path(style_ledger.__file__)), *arguments[:prefix],
                "--repo", str(self.repo), *arguments[prefix:]]

    def cli(self, *arguments: str, success: bool = True) -> subprocess.CompletedProcess[str]:
        # main owns and closes its ledger each call; only argparse's argv is process-global.
        # Rule paths are patched in setUp. The footprint caches contain immutable text only.
        command = self.cli_command(arguments)
        output, errors = io.StringIO(), io.StringIO()
        code = 0
        with mock.patch.object(sys, "argv", command[1:]), redirect_stdout(output), redirect_stderr(errors):
            try:
                style_ledger.main()
            except SystemExit as error:
                if isinstance(error.code, int):
                    code = error.code
                elif error.code is not None:
                    print(error.code, file=sys.stderr)
                    code = 1
        result = subprocess.CompletedProcess(command, code, output.getvalue(), errors.getvalue())
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def subprocess_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        """Keep the script guard, imports, and persisted CLI writes covered end to end."""
        result = subprocess.run(self.cli_command(arguments), capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def set_tip(self, tip: str) -> None:
        _ = self.cli("set-tip", "--tip", tip)

    def record(
        self, rule_id: str, path: str, commit: str, verdict: str = "clean", *,
        rule_blob: str = "", detail: str = "", ticket: str = "unit-1",
        input_scope: style_ledger.DeclaredInputScope = style_ledger.DeclaredInputScope.RULE,
    ) -> None:
        _ = self.cli(
            "record", "--rule", rule_id, "--path", path, "--commit", commit,
            "--rule-blob", rule_blob or self.blob(rule_id), "--verdict", verdict,
            "--detail", detail, "--ticket", ticket, "--worker", "test-worker", "--input-scope", input_scope,
        )

    def ledger(self) -> style_ledger.Ledger:
        ledger = style_ledger.open_ledger(self.repo)
        self.addCleanup(ledger.close)
        return ledger

    def judgment_rows(self, database: Path) -> list[tuple[object, ...]]:
        with sqlite3.connect(database) as connection:
            return cast(list[tuple[object, ...]], connection.execute("SELECT * FROM judgments ORDER BY id").fetchall())

    def site_fixture(self, family: rules.Family = "names", *, rule_name: str = "site-rule",
                     scope: str = "file", source: str = "fn work() {\n    let old = 791;\n}\n",
                     line: int = 2, before: str = "old", kind: style_ledger.FindingKind = "rename",
                     ) -> tuple[style_ledger.Ledger, str, style_ledger.HeldFinding]:
        rule = self.rule(rule_name, f"tier: C\nfamily: {family}\ninput_scope: {scope}\n")
        self.write(PARENT_FILE, source)
        tip = self.commit("Source site")
        ledger = self.ledger()
        ledger.set_tip(tip)
        ledger.ticket_add("judge-1", "judge", "read", family, "crates/alpha", [PARENT_FILE], [rule])
        ledger.ticket_assign("judge-1", "main")
        _ = ledger.ticket_base("judge-1")
        finding = ledger.hold(rule, PARENT_FILE, line, source.splitlines()[line - 1], kind, before, "fresh",
                              self.blob(rule), "judge-1")
        ledger.record(rule, PARENT_FILE, tip, self.blob(rule), "held", ticket="judge-1", worker="test",
                      input_scope=style_ledger.DeclaredInputScope.RULE)
        ledger.ticket_drop("judge-1")
        ledger.file_held(finding.finding_id, self.root / "decision.md")
        return ledger, rule, ledger.named_held([finding.finding_id])[0]

    def strict_site(self, ledger: style_ledger.Ledger, finding: style_ledger.HeldFinding,
                    at: str) -> style_ledger.SiteEvidence:
        # Exercise the additive proof independently of earlier proved declaration transitions.
        proof = cast(Callable[[style_ledger.HeldFinding, str], style_ledger.SiteEvidence], getattr(ledger, "_site_proof"))
        return proof(finding, at)

    def assert_extern_owner_move_refused(self, work: str) -> None:
        source = ("fn container() {\n    extern fn first() {\n        " + work
                  + " work() {\n            let old = 791;\n        }\n    }\n    extern fn second() {}\n}\n")
        ledger, _, finding = self.site_fixture(source=source, line=4)
        # Establish that the qualified items are readable, rather than merely refusing the whole file.
        tip = self.commit_tip(ledger, {PARENT_FILE: "// header\n" + source})
        self.assertEqual(self.strict_site(ledger, finding, tip), style_ledger.UnchangedSite(PARENT_FILE, 5))
        moved = ("fn container() {\n    extern fn first() {}\n    extern fn second() {\n        " + work
                 + " work() {\n            let old = 791;\n        }\n    }\n}\n")
        tip = self.commit_tip(ledger, {PARENT_FILE: moved})
        self.assertIsInstance(self.strict_site(ledger, finding, tip), style_ledger.NeedsJudgment)
        self.assertIsInstance(ledger.finding_site(finding, tip), style_ledger.NeedsJudgment)
        _ = ledger.follow_declarations([finding.finding_id], tip, lambda _: True)
        self.assertEqual(ledger.named_held([finding.finding_id])[0].follow, style_ledger.NotFollowed())

    def record_reads(
        self, rule: str, path: str, commit: str, reads: tuple[str, ...], *,
        verdict: str = "clean", ticket: str = "outside",
    ) -> None:
        _ = self.cli(
            "record", "--rule", rule, "--path", path, "--commit", commit,
            "--rule-blob", self.blob(rule), "--verdict", verdict, "--ticket", ticket,
            "--worker", "test", "--input-scope", "rule", "--reads", *reads,
        )

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

    def decision_findings(self) -> tuple[str, Path, tuple[str, ...]]:
        rule = self.rule("decision")
        ledger = self.ledger()
        issue = self.root / "decision.md"
        identities: list[str] = []
        for index, path in enumerate(self.sources[:2]):
            finding = ledger.hold(rule, path, index + 1, "required change", "rename", "Outcome::Old", "Outcome::New",
                                  self.blob(rule), "outside")
            ledger.file_held(finding.finding_id, issue)
            identities.append(finding.finding_id)
        return rule, issue, tuple(identities)

    def built_time(self, ledger: style_ledger.Ledger, finding_id: str) -> str:
        with closing(sqlite3.connect(ledger.database)) as connection:
            row = cast(tuple[str], connection.execute(
                "SELECT built_at FROM held_findings WHERE finding_id=?", (finding_id,),
            ).fetchone())
        return row[0]

    def assert_build_time_survives_reading(self, ledger: style_ledger.Ledger, finding_id: str) -> None:
        self.assertEqual(self.built_time(ledger, finding_id), "2026-10-27T10:00:00+00:00")
        with mock.patch.object(style_ledger, "_now", return_value="2026-10-28T11:00:00+00:00"):
            ledger.record_person_applied({finding_id: style_ledger.ReadByPerson("reader")})
        self.assertEqual(self.built_time(ledger, finding_id), "2026-10-27T10:00:00+00:00")

    def refill_result(self, database: Path, unit: str = "names-unit") -> tuple[str, str, str, int, int, int, int]:
        with closing(sqlite3.connect(database)) as connection:
            return cast(tuple[str, str, str, int, int, int, int], connection.execute(
                "SELECT at,outcome,split_at,ready,changed,structure,other FROM refill_results WHERE unit=?", (unit,),
            ).fetchone())

    def file_rename_finding(self) -> tuple[style_ledger.HeldFinding, Path]:
        """A filed file rename of crates/alpha/src/ledger/lock.rs to mutation_lock.rs, and its decision."""
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.root / "runtime")})))
        rule = self.rule("file-rename")
        self.write("crates/alpha/src/ledger/lock.rs", "pub struct MutationLock;\n")
        _ = self.commit("Add the lock module")
        ledger = self.ledger()
        issue = self.root / "decision.md"
        finding = ledger.hold(rule, "crates/alpha/src/ledger/lock.rs", 1, "pub struct MutationLock;", "rename",
                              "The file contains MutationLock.",
                              "Rename ledger/lock.rs to ledger/mutation_lock.rs; update ledger/mod.rs",
                              self.blob(rule), "outside")
        ledger.file_held(finding.finding_id, issue)
        return finding, issue

    def binding_ticket(self, source: str, pairs: tuple[tuple[int, str, str], ...]) -> tuple[style_ledger.Ledger, Path, str]:
        rule = self.rule("prefer-type-named-fields-and-bindings")
        path = self.sources[0]
        self.write(path, source)
        base = self.commit("Binding declarations before the ticket")
        ledger = self.ledger()
        issue = self.root / "bindings.md"
        ids: list[str] = []
        for line, old, new in pairs:
            finding = ledger.hold(rule, path, line, source.splitlines()[line - 1], "rename", old, new, self.blob(rule), "outside")
            ledger.file_held(finding.finding_id, issue)
            ids.append(finding.finding_id)
        ledger.ticket_add("names-1", "names", "proposal", "names", "crates/alpha", [path], [rule],
                          proposal=str(issue), finding_ids=tuple(ids))
        ledger.ticket_assign("names-1", "main")
        self.assertEqual(ledger.ticket_base("names-1"), base)
        return ledger, issue, base

    def held_rows(self, database: Path) -> list[tuple[object, ...]]:
        with closing(sqlite3.connect(database)) as connection:
            return cast(list[tuple[object, ...]], connection.execute("SELECT * FROM held_findings ORDER BY finding_id").fetchall())

    def checkpointed_type_findings(self) -> tuple[style_ledger.Ledger, str, str, str]:
        """Two filed type findings; names-1 holds the first and merged at a checkpoint after the initial tip."""
        rule, issue, _ = self.decision_findings()
        self.write(self.sources[2], "pub fn checkpoint() {}\n")
        checkpoint = self.commit("Build the type change")
        ledger = self.ledger()
        ledger.set_tip(checkpoint, "main")
        built, other = (ledger.hold(rule, self.sources[2], line, "value", "type", "u32", "u64", self.blob(rule),
                                    "outside") for line in (1, 2))
        for finding in (built, other):
            ledger.file_held(finding.finding_id, issue)
        ledger.ticket_add("names-1", "names", "proposal", "names", "crates/beta", [self.sources[2]], [rule],
                          proposal=str(issue), finding_ids=(built.finding_id,))
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE tickets SET state='merged',checkpoint=? WHERE id='names-1'", (checkpoint,))
        return ledger, built.finding_id, other.finding_id, checkpoint

    def assert_refused_after_a_write_during_the_check(self, ledger: style_ledger.Ledger, finding_id: str,
                                                      update: str, parameters: tuple[str, ...], error: str) -> None:
        """Another writer runs update during the first applied check; the next attempt refuses and writes nothing."""
        before = self.held_rows(ledger.database)
        check = style_ledger.Ledger.applied_check
        calls = 0

        def check_while_another_writer_runs(self_: style_ledger.Ledger, finding: style_ledger.HeldFinding,
                                            commit: str) -> AppliedCheck:
            nonlocal calls
            calls += 1
            if calls == 1:
                with closing(sqlite3.connect(ledger.database)) as connection, connection:
                    _ = connection.execute(update, parameters)
            return check(self_, finding, commit)

        with (mock.patch.object(style_ledger.Ledger, "applied_check", check_while_another_writer_runs),
              self.assertRaisesRegex(style_ledger.LedgerError, re.escape(error))):
            _ = ledger.mark_built_at_merge((finding_id,), self.initial_tip, style_ledger.ReadByPerson("nate"))
        self.assertEqual(calls, 1)
        self.assertEqual(self.held_rows(ledger.database), before)

    def judged_type_finding(self) -> tuple[style_ledger.Ledger, str, str]:
        """A filed type finding judged by the read ticket names-1, which starts at the initial tip; and a later tip."""
        rule, issue, _ = self.decision_findings()
        self.write(self.sources[2], "pub fn judged() {}\n")
        judged = self.commit("Judge the finding")
        ledger = self.ledger()
        ledger.set_tip(judged, "main")
        finding = ledger.hold(rule, self.sources[2], 1, "value", "type", "u32", "u64", self.blob(rule), "outside")
        ledger.file_held(finding.finding_id, issue)
        ledger.ticket_add("names-1", "names", "read", "names", "crates/beta", [self.sources[2]], [rule])
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE tickets SET base=? WHERE id='names-1'", (self.initial_tip,))
            _ = connection.execute("UPDATE held_findings SET ticket='names-1' WHERE finding_id=?",
                                   (finding.finding_id,))
        return ledger, finding.finding_id, judged

    def widen_ticket(self) -> tuple[style_ledger.Ledger, Path]:
        """An approved proposal ticket names-1 that builds the finding in the first source, and its ledger."""
        rule, issue, ids = self.decision_findings()
        _ = issue.write_text("---\nstatus: open\ntags: [proposal, style]\nunit: names\nplan: approved\n"
                             + f"style_rule: {rule}\nstyle_crate: crates/alpha\n"
                             + f"style_rule_blob: {self.blob(rule)}\n---\n")
        ledger = self.ledger()
        ledger.ticket_add("names-1", "names", "proposal", "names", "crates/alpha", [self.sources[0]], [rule],
                          proposal=str(issue), finding_ids=ids[:1])
        return ledger, issue

    def widen(self, *paths: str, ticket: str = "names-1", reason: str = "the move needs a new file",
              by: str = "showrunner") -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            result = style_plan.main(["widen", "--repo", str(self.repo), "--ticket", ticket, "--files", *paths,
                                      "--reason", reason, "--by", by])
        return result, output.getvalue()

    def widened_rows(self, ledger: style_ledger.Ledger) -> list[tuple[str, ...]]:
        with closing(sqlite3.connect(ledger.database)) as connection:
            return cast(list[tuple[str, ...]], connection.execute(
                "SELECT * FROM ticket_widened_paths ORDER BY ticket,path").fetchall())

    def commit_tip(self, ledger: style_ledger.Ledger, files: dict[str, str | None]) -> str:
        """Write each file, or remove it for None, commit on main and sync the ledger there."""
        for path, text in files.items():
            if text is None:
                (self.repo / path).unlink()
            else:
                self.write(path, text)
        tip = self.commit("Move declarations")
        ledger.set_tip(tip)
        return tip

    def hold_parent(self, ledger: style_ledger.Ledger, lines: tuple[tuple[int, str], ...], *, before: str = "Parent::old",
                    after: str = "Parent::new", kind: style_ledger.FindingKind = "rename",
                    path: str = PARENT_FILE) -> tuple[str, tuple[str, ...]]:
        """Unfiled findings in path (a.rs unless named), renames of Parent::old unless named, that a dropped read ticket
        judged at the head of main."""
        rule = self.rule("names")
        ticket = f"judge-{len(ledger.held_findings(style_ledger.AllHeldFindings()))}"
        ledger.ticket_add(ticket, "judge", "read", "names", "crates/alpha", [path], [rule])
        ledger.ticket_assign(ticket, "main")
        _ = ledger.ticket_base(ticket)
        findings = tuple(ledger.hold(rule, path, line, text, kind, before, after, self.blob(rule), ticket)
                         for line, text in lines)
        ledger.ticket_drop(ticket)
        return rule, tuple(finding.finding_id for finding in findings)

    def judge_parent(self, ledger: style_ledger.Ledger, lines: tuple[tuple[int, str], ...], *, before: str = "Parent::old",
                     after: str = "Parent::new", kind: style_ledger.FindingKind = "rename",
                     path: str = PARENT_FILE) -> tuple[str, tuple[str, ...]]:
        """The findings of hold_parent, filed to decision.md."""
        rule, ids = self.hold_parent(ledger, lines, before=before, after=after, kind=kind, path=path)
        for finding_id in ids:
            ledger.file_held(finding_id, self.root / "decision.md")
        return rule, ids

    def follow_approved(self, ledger: style_ledger.Ledger, finding_ids: list[str]) -> tuple[style_ledger.DeclarationOutcome, ...]:
        """The declaration step at the synced tip, with every decision approved."""
        return ledger.follow_declarations(finding_ids, ledger.synced_commit(), lambda _: True).outcomes

    def follow_statement(self, moved: dict[str, str | None]) -> style_ledger.DeclarationOutcome:
        """A filed finding on the statement `let rgba = convert(2, 16);` of fn check in a.rs, read after moved."""
        ledger = self.ledger()
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub fn keep() {}\n\nfn check() {\n    let bytes = [0];\n    let rgba = convert(2, 16);\n}\n"})
        _, (finding,) = self.judge_parent(ledger, ((5, "let rgba = convert(2, 16);"),), kind="restructure")
        _ = self.commit_tip(ledger, moved)
        outcome, = self.follow_approved(ledger, [finding])
        return outcome

    def renamed_parent(self, ledger: style_ledger.Ledger) -> str:
        """A filed rename of struct Parent to Renamed in a.rs, judged at its keyword line."""
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT})
        _, (finding_id,) = self.judge_parent(ledger, ((1, "pub struct Parent {"),), before="Parent", after="Renamed")
        return finding_id

    def assert_rename_left(self, ledger: style_ledger.Ledger, finding_id: str, expected: style_ledger.DeclarationOutcome) -> None:
        """The declaration step leaves the filed rename with expected and writes nothing."""
        before = self.held_rows(ledger.database)
        left, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(left, expected)
        self.assertEqual(self.held_rows(ledger.database), before)

    def built_finding(self, ledger: style_ledger.Ledger, path: str, before: str, after: str, *,
                      kind: style_ledger.FindingKind = "rename", line: int = 1) -> str:
        """A finding at path that its own dropped read ticket judged, filed to built.md and built."""
        if kind == "rename" and (self.repo / path).is_file():
            source = (self.repo / path).read_text().splitlines(keepends=True)
            for spelling in (before, after):
                if spelling not in source[line - 1]:
                    source[line - 1] = source[line - 1].rstrip("\n") + f" const _: () = {{ {spelling}(); }};\n"
            updated = "".join(source)
            if updated != (self.repo / path).read_text():
                _ = self.commit_tip(ledger, {path: updated})
        rule = self.rule("names")
        ticket = f"built-{len(ledger.held_findings(style_ledger.AllHeldFindings()))}"
        ledger.ticket_add(ticket, "built", "read", "names", "/".join(path.split("/")[:2]), [path], [rule])
        ledger.ticket_assign(ticket, "main")
        _ = ledger.ticket_base(ticket)
        finding_id = ledger.hold(rule, path, line, f"built {before} to {after}", kind, before, after, self.blob(rule), ticket).finding_id
        ledger.file_held(finding_id, self.root / "built.md")
        ledger.ticket_drop(ticket)
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE held_findings SET state='built' WHERE finding_id=?", (finding_id,))
        return finding_id

    def trait_method_finding(self, ledger: style_ledger.Ledger, *, filed: bool = True) -> tuple[str, str]:
        """A finding on the body of Type's implementation of Trait::old."""
        source = TRAIT_METHOD
        _ = self.commit_tip(ledger, {PARENT_FILE: source})
        judge = self.judge_parent if filed else self.hold_parent
        _, (finding_id,) = judge(ledger, ((7, "7"),), kind="restructure")
        return finding_id, source

    def assert_trait_source_collision(self, owner: str) -> None:
        ledger = self.ledger()
        finding_id, source = self.trait_method_finding(ledger)
        twin = owner + " {\n    fn old(&self) -> u32 { 9 }\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: source + twin})
        _ = self.built_finding(ledger, PARENT_FILE, "Trait::old", "Trait::new", line=2)
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("fn old", "fn new") + twin})
        moved, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(moved, style_ledger.DeclarationMoved)
        row = next(row for row in ledger.held_findings(style_ledger.AllHeldFindings()) if row.finding_id == finding_id)
        self.assertEqual((row.path, row.line), (PARENT_FILE, 7))

    def assert_trait_destination_collision(self, owner: str) -> None:
        ledger = self.ledger()
        finding_id, source = self.trait_method_finding(ledger)
        _ = self.built_finding(ledger, PARENT_FILE, "Trait::old", "Trait::new", line=2)
        twin = owner + " {\n    fn new(&self) -> u32 { 9 }\n}\n"
        destination = "// gained trait implementation\n" + source.replace("fn old", "fn new")
        _ = self.commit_tip(ledger, {PARENT_FILE: twin, MOVED_FILE: destination})
        moved, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(moved, style_ledger.DeclarationMoved)
        row = next(row for row in ledger.held_findings(style_ledger.AllHeldFindings()) if row.finding_id == finding_id)
        self.assertEqual((row.path, row.line), (MOVED_FILE, 8))

    def assert_trait_destination_limit(self, case: str) -> None:
        ledger = self.ledger()
        source = TRAIT_METHOD
        renamed = source.replace("fn old", "fn new")
        _ = self.commit_tip(ledger, {PARENT_FILE: source, MOVED_FILE: renamed if case == "preexisting" else "pub fn helper() {}\n"})
        # Read after the twin was committed so the gain check has real prior evidence.
        _, (finding_id,) = self.judge_parent(ledger, ((7, "7"),), kind="restructure", before=case, after="change")
        _ = self.built_finding(ledger, PARENT_FILE, "Trait::old", "Trait::new", line=2)
        files: dict[str, str | None] = {PARENT_FILE: "pub fn helper() {}\n", MOVED_FILE: renamed}
        if case == "two files":
            files[TWIN_FILE] = renamed
        elif case == "two items":
            files[MOVED_FILE] = renamed + "\n" + renamed
        _ = self.commit_tip(ledger, files)
        before = self.held_rows(ledger.database)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertNotIsInstance(outcome, style_ledger.DeclarationMoved)
        self.assertEqual(self.held_rows(ledger.database), before)

    def assert_trait_follow_limit(self, case: str) -> None:
        ledger = self.ledger()
        finding_id, source = self.trait_method_finding(ledger)
        built_path = "crates/beta/src/other.rs" if case == "other crate" else PARENT_FILE
        _ = self.built_finding(ledger, built_path, "Trait::old", "Trait::new")
        if case == "fork":
            _ = self.built_finding(ledger, PARENT_FILE, "Trait::old", "Trait::other")
        if case == "open build":
            ledger.ticket_add("names-1", "names", "proposal", "names", "crates/alpha", [PARENT_FILE], [self.rule("names")],
                              proposal=str(self.root / "decision.md"), finding_ids=(finding_id,))
            ledger.ticket_assign("names-1", "main")
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("fn old", "fn new")})
        before = self.held_rows(ledger.database)
        answers = iter((True,))
        def approved(path: Path) -> bool:
            if case == "unapproved":
                return path != self.root / "built.md"
            return next(answers, False) if case == "lost approval" and path == self.root / "built.md" else True
        outcome, = ledger.follow_declarations([finding_id], ledger.synced_commit(), approved).outcomes
        self.assertNotIsInstance(outcome, style_ledger.DeclarationMoved)
        self.assertEqual(self.held_rows(ledger.database), before)
        if case == "open build":
            ledger.ticket_drop("names-1")

    def assert_changed_trait_membership_is_left(self, before_header: str, after_header: str) -> None:
        ledger = self.ledger()
        body = " {\n    fn old(&self) -> u32 {\n        7\n    }\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: before_header + body})
        _, (finding_id,) = self.judge_parent(ledger, ((3, "7"),), kind="restructure")
        _ = self.built_finding(ledger, PARENT_FILE, "Trait::old", "Trait::new")
        _ = self.commit_tip(ledger, {PARENT_FILE: after_header + body.replace("fn old", "fn new")})
        before = self.held_rows(ledger.database)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertNotIsInstance(outcome, style_ledger.DeclarationMoved)
        self.assertEqual(self.held_rows(ledger.database), before)

    def restructured_parent(self, ledger: style_ledger.Ledger) -> str:
        """A filed restructure finding on the field line of struct Parent in a.rs."""
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT})
        _, (finding_id,) = self.judge_parent(ledger, ((2, "old: u32,"),), kind="restructure")
        return finding_id

    def renamed_binding(self, ledger: style_ledger.Ledger, built_path: str, before: str, after: str) -> str:
        """A filed rename of the binding ws in fn old, a built rename of fn old at built_path, and a tip that renames
        only the fn. The step follows the finding to fn new."""
        function = "pub fn old() {\n    let ws = 1;\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: function, MOVED_FILE: "pub fn caller() {\n    crate::a::old();\n}\n"})
        _, (finding_id,) = self.judge_parent(ledger, ((2, "let ws = 1;"),), before="ws", after="workspace")
        _ = self.built_finding(ledger, built_path, before, after)
        _ = self.commit_tip(ledger, {PARENT_FILE: function.replace("fn old", "fn new"),
                                     MOVED_FILE: "pub fn caller() {\n    crate::a::new();\n}\n"})
        filed = ledger.held_findings(style_ledger.HeldFindingsInState("filed"))[0]
        moved, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(moved, style_ledger.DeclarationMoved(
            filed, declarations.Declaration("fn", "new", declarations.ModuleItem()), PARENT_FILE, 2))
        return finding_id

    def assert_gone_from_file(self, ledger: style_ledger.Ledger, finding_id: str, approved: Callable[[Path], bool]) -> None:
        """The declaration step leaves the filed finding on struct Parent gone from its file and writes nothing."""
        filed = ledger.held_findings(style_ledger.HeldFindingsInState("filed"))[0]
        before = self.held_rows(ledger.database)
        left, = ledger.follow_declarations([finding_id], ledger.synced_commit(), approved).outcomes
        self.assertEqual(left, style_ledger.DeclarationGoneFromFile(
            filed, declarations.Declaration("struct", "Parent", declarations.ModuleItem())))
        self.assertEqual(self.held_rows(ledger.database), before)

    def assert_left(self, ledger: style_ledger.Ledger, finding_id: str, declaration: declarations.Declaration) -> None:
        """The declaration step leaves the filed finding gone from its file at declaration and writes nothing."""
        filed = next(finding for finding in ledger.held_findings(style_ledger.AllHeldFindings()) if finding.finding_id == finding_id)
        self.assert_rename_left(ledger, finding_id, style_ledger.DeclarationGoneFromFile(filed, declaration))

    def restructured_method(self, ledger: style_ledger.Ledger) -> str:
        """A filed restructure finding in the body of fn new in impl Parent in a.rs, and fn new in impl Other in b.rs."""
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub struct Parent;\n\nimpl Parent {\n    pub fn new(seed: u32) -> u32 {\n        seed\n    }\n}\n",
                                     MOVED_FILE: "pub struct Other;\n\nimpl Other {\n    pub fn new() {}\n}\n"})
        _, (finding_id,) = self.judge_parent(ledger, ((5, "seed"),), kind="restructure")
        return finding_id

    def renamed_method(self, ledger: style_ledger.Ledger, method: str) -> tuple[str, style_ledger.HeldFinding]:
        """A filed rename of Parent::old to Parent::new at fn old in impl Parent, a built rename of Parent to Renamed,
        and a tip that renames Parent and gives its method the name method."""
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub struct Parent;\n\nimpl Parent {\n    pub fn old() {}\n}\n"})
        _, (finding_id,) = self.judge_parent(ledger, ((4, "pub fn old() {}"),))
        _ = self.built_finding(ledger, PARENT_FILE, "Parent", "Renamed")
        _ = self.commit_tip(ledger, {PARENT_FILE: f"pub struct Renamed;\n\nimpl Renamed {{\n    pub fn {method}() {{}}\n}}\n"})
        filed = next(finding for finding in ledger.held_findings(style_ledger.AllHeldFindings()) if finding.finding_id == finding_id)
        return finding_id, filed

    def assert_method_rename_built_in_place(self, ledger: style_ledger.Ledger, finding_id: str) -> None:
        """The step builds the filed rename of Parent::old to Parent::new at fn new in impl Parent in a.rs."""
        filed = next(finding for finding in ledger.held_findings(style_ledger.AllHeldFindings()) if finding.finding_id == finding_id)
        renamed, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(renamed, style_ledger.DeclarationRenamed(
            filed, declarations.Declaration("fn", "old", declarations.ImplOrTraitItem("Parent")),
            declarations.Declaration("fn", "new", declarations.ImplOrTraitItem("Parent")), PARENT_FILE, 4))

    def structure_module(self) -> tuple[str, str, str, str]:
        path = "crates/alpha/src/theme/colors.rs"
        sibling = "crates/alpha/src/theme/mod.rs"
        parent = "crates/alpha/src/theme.rs"
        for source in (path, sibling, parent, "crates/alpha/src/unrelated/item.rs"):
            self.write(source, "pub fn value() {}\n")
        return path, sibling, parent, self.commit("Add module inputs")

    def insert_legacy_structure_judgment(self, rule: str, path: str, commit: str, verdict: str = "clean") -> None:
        ledger = self.ledger()
        with closing(sqlite3.connect(ledger.database)) as connection:
            _ = connection.execute(
                'INSERT INTO judgments(rule,rule_blob,input_scope,input_fingerprint,path,"commit",verdict,verdict_detail,ticket,worker,recorded_at) '
                + 'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (rule, self.blob(rule), "crate", self.git("rev-parse", f"{commit}:crates/alpha"),
                 path, commit, verdict, "", "outside", "legacy-worker", "2026-10-16T00:00:00+00:00"),
            )
            connection.commit()

    def comment_scope_rules(self) -> tuple[str, str, str, str, str]:
        """A modules rule at file, module and crate scope, a names rule, and the module file they share a crate with."""
        file_rule = self.rule("file-layout", "tier: C\nfamily: modules\ninput_scope: file\n")
        module_rule = self.rule("structure", "tier: D\nfamily: modules\n")
        crate_rule = self.rule("crate-layout", "tier: C\nfamily: modules\ninput_scope: crate\n")
        names_rule = self.rule("naming", "tier: C\nfamily: names\ninput_scope: file\n")
        path, _, _, base = self.structure_module()
        self.set_tip(base)
        for rule, judged in ((file_rule, self.sources[0]), (module_rule, path), (crate_rule, self.sources[0]), (names_rule, self.sources[0])):
            self.record(rule, judged, base)
        return file_rule, module_rule, crate_rule, names_rule, path

    def comment_only_change(self, relative: str) -> None:
        content = (self.repo / relative).read_text()
        self.write(relative, "// A note.\n/// Returns the value.\n" + content.replace("{ 1 }", "{\n    // One.\n    1\n}"))

    def record_while_stalled(self, stalled: str, write: Callable[[style_ledger.Ledger], object]) -> None:
        """A second ledger records a verdict before write may leave style_ledger.<stalled>."""
        original = cast(Callable[..., object], getattr(style_ledger, stalled))
        started, release = threading.Event(), threading.Event()

        def stall(*arguments: object) -> object:
            if not started.is_set():
                started.set()
                self.assertTrue(release.wait(TEST_HANG_GUARD_SECS), "stalled writer was not released")
            return original(*arguments)

        _ = self.enterContext(mock.patch.object(style_ledger, "BUSY_TIMEOUT_SECONDS", 0.5))
        rule = self.rule("concurrent")
        recorder = self.ledger()
        failures: list[BaseException] = []

        def run() -> None:
            # A connection serves only the thread that opened it.
            try:
                with style_ledger.open_ledger(self.repo) as writer:
                    _ = write(writer)
            except BaseException as error:
                failures.append(error)

        with mock.patch.object(style_ledger, stalled, side_effect=stall):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(started.wait(TEST_HANG_GUARD_SECS), "writer did not reach its stalled operation")
                recorder.record(rule, self.sources[2], self.initial_tip, self.blob(rule), "clean",
                                ticket="unit-1", worker="test-worker")
            finally:
                release.set()
                thread.join(timeout=TEST_HANG_GUARD_SECS)
            self.assertFalse(thread.is_alive(), "released writer did not finish")
        self.assertEqual(failures, [])
        self.assertEqual(self.states(rule)[self.sources[2]], "current")

    def judge_while_loading_rules(self, changes: int) -> tuple[style_ledger.Ledger, str, mock.MagicMock]:
        """A names-1 ticket ready to checkpoint, and a catalog load whose first changes calls record a newer verdict for it."""
        rule = self.rule("read")
        path = self.sources[0]
        ledger = self.ledger()
        ledger.set_tip(self.initial_tip, "main")
        ledger.ticket_add("names-1", "names", "read", "names", "crates/alpha", [path], [rule])
        ledger.ticket_assign("names-1", "main")
        _ = ledger.ticket_base("names-1")
        self.record(rule, path, self.initial_tip, "fixed", ticket="names-1")
        self.write(path, "pub fn renamed() -> u32 { 1 }\n")
        checkpoint = self.commit("Ticket checkpoint")
        original = rules.load_rules
        loads: list[Path | None] = []

        def load(repo: Path | None) -> tuple[rules.Rule, ...]:
            # The checkpoint loads the catalog once per attempt, after it reads and before it locks.
            loads.append(repo)
            if len(loads) <= changes:
                with closing(sqlite3.connect(ledger.database)) as connection, connection:
                    _ = connection.execute(
                        'INSERT INTO judgments(rule,rule_blob,input_scope,input_fingerprint,path,"commit",verdict,verdict_detail,ticket,worker,recorded_at,declared_scope) '
                        + 'SELECT rule,rule_blob,input_scope,input_fingerprint,path,"commit",verdict,verdict_detail,ticket,?,?,declared_scope '
                        + "FROM judgments WHERE ticket='names-1' LIMIT 1",
                        (f"worker-{len(loads)}", datetime.now(timezone.utc).isoformat()))
            return original(repo)

        return ledger, checkpoint, self.enterContext(mock.patch.object(rules, "load_rules", side_effect=load))

    def removed_ticket(self) -> tuple[style_ledger.Ledger, str, str]:
        """An assigned names-1 read ticket on the first two sources under a crate-scope rule, started at the initial tip."""
        rule = self.rule("read", "tier: C\nfamily: names\ninput_scope: crate\n")
        ledger = self.ledger()
        ledger.set_tip(self.initial_tip, "main")
        ledger.ticket_add("names-1", "names", "read", "names", "crates/alpha", list(self.sources[:2]), [rule])
        ledger.ticket_assign("names-1", "main")
        return ledger, rule, ledger.ticket_base("names-1")

    def record_removed(self, ledger: style_ledger.Ledger, rule: str, path: str, commit: str, ticket: str = "names-1") -> None:
        ledger.record(rule, path, commit, self.blob(rule), "removed", ticket=ticket, worker="test-worker")

    def cell_rows(self, ledger: style_ledger.Ledger, path: str) -> list[tuple[str, str]]:
        with closing(sqlite3.connect(ledger.database)) as connection:
            return cast(list[tuple[str, str]], connection.execute(
                'SELECT "commit",verdict FROM judgments WHERE path=? ORDER BY id', (path,)).fetchall())

    def removed_by_another_ticket(self) -> tuple[style_ledger.Ledger, str, str, str]:
        """names-1 judges both sources at the initial tip; other-names-1 deletes the second source, and the file comes back."""
        ledger, rule, _ = self.removed_ticket()
        kept, path = self.sources[:2]
        for source in (kept, path):
            self.record(rule, source, self.initial_tip, ticket="names-1")
        ledger.ticket_add("other-names-1", "other-names", "read", "names", "crates/alpha", [path], [rule])
        ledger.ticket_assign("other-names-1", "main")
        _ = ledger.ticket_base("other-names-1")
        original = (self.repo / path).read_text()
        (self.repo / path).unlink()
        self.record_removed(ledger, rule, path, self.commit("Another ticket deletes the file"), ticket="other-names-1")
        self.write(path, original)
        return ledger, rule, path, self.commit("The file comes back")

    def held_reservation(self, ticket: str) -> Path:
        """A unit run folder whose phase 7 checkpoint kept reservation r1 for the ticket's merge, and a cargo-berth on PATH."""
        binary = self.root / "bin"
        binary.mkdir()
        berth = binary / "cargo-berth"
        _ = berth.write_text(RELEASE_BERTH.replace("__PYTHON__", sys.executable))
        berth.chmod(0o755)
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "PATH": str(binary) + os.pathsep + os.environ.get("PATH", ""),
            "LEDGER_BERTH_LOG": str(self.root / "berth.log"), "LEDGER_BOARD": str(self.root / "board.json")})))
        session = self.root / "unit run"
        (session / "phases/7").mkdir(parents=True)
        _ = (session / "phases/7/delegated_phase_reservation_state.json").write_text(json.dumps({
            "kind": "checkpoint_committed_awaiting_release_confirmation",
            "checkpoint_release_confirmation_pending": {
                "reservation_id": "r1", "coordination_run_id": "c1", "phase": "7", "phase_start_head": self.initial_tip,
                "checkpoint_commit": self.initial_tip, "held_for_ticket": ticket, "worktree": str(self.repo)}}))
        return session

    def berth_calls(self) -> list[list[str]]:
        log = self.root / "berth.log"
        return [cast(list[str], json.loads(line)) for line in log.read_text().splitlines()] if log.exists() else []

    def assigned_structure_ticket(self, ticket: str) -> None:
        rule = self.rule(f"structure-{ticket}", "tier: D\nfamily: modules\n")
        ledger = self.ledger()
        ledger.set_tip(self.initial_tip, "main")
        ledger.ticket_add(ticket, "modules-unit", "structure", "modules", "crates/alpha", [], [rule])
        ledger.ticket_assign(ticket, "main")
        self.record(rule, "crates/alpha", self.initial_tip, "waived", detail="accepted", ticket=ticket)

    def checkpointed_structure_ticket(self, ticket: str, *reservation: str) -> None:
        self.assigned_structure_ticket(ticket)
        _ = self.cli("ticket", "checkpoint", "--id", ticket, "--commit", "HEAD", *reservation)

    def ticket_row(self, ticket: str) -> tuple[str, str, str]:
        with closing(sqlite3.connect(style_ledger.ledger_location(self.repo).database)) as connection:
            return cast(tuple[str, str, str], connection.execute(
                "SELECT state,reservation_session_dir,reservation_phase FROM tickets WHERE id=?", (ticket,)).fetchone())

    def yield_proposal(self, kept: tuple[str, str, str, str], freed: tuple[str, str, str, str],
                       files: dict[str, str] | None = None) -> tuple[str, str, Path]:
        """Add an assigned proposal ticket proposal-1 with one finding at each (path, kind, before, after), at a tip
        that adds files; return the kept finding, the finding to yield and the decision."""
        rule = self.rule("decision")
        for path, text in (files or {}).items():
            self.write(path, text)
        self.set_tip(self.commit("Write the yield files") if files else self.initial_tip)
        ledger = self.ledger()
        issue = self.root / "issues/decision.md"
        identities: list[str] = []
        for line, (path, kind, before, after) in enumerate((kept, freed), 1):
            finding = ledger.hold(rule, path, line, "change", cast(style_ledger.FindingKind, kind), before, after,
                                  self.blob(rule), "outside")
            ledger.file_held(finding.finding_id, issue)
            identities.append(finding.finding_id)
        ledger.ticket_add("proposal-1", "proposal", "proposal", "names", "crates/alpha", sorted({kept[0], freed[0]}),
                          [rule], proposal=str(issue), finding_ids=tuple(identities))
        ledger.ticket_assign("proposal-1", "main")
        return identities[0], identities[1], issue

    yield_files: dict[str, str] = {
        "crates/alpha/src/a.rs": "pub fn alpha() {}\n",
        "crates/alpha/src/b.rs": "pub fn beta() {}\n",
        "crates/alpha/src/c.rs": "pub const BAR: char = '\\u{2580}';\npub const TAB: u8 = b'\\x7f';\n",
        "crates/alpha/src/other.rs": "#[cfg(test)]\nmod tests {\n    use super::*;\n    use std::collections::*;\n}\n",
    }

    kept_rename: tuple[str, str, str, str] = ("crates/alpha/src/a.rs", "rename", "Alpha", "Rename src/a.rs to src/first.rs")

    freed_rename: tuple[str, str, str, str] = ("crates/alpha/src/b.rs", "rename", "Beta", "Rename src/b.rs to src/second.rs")

