"""Coverage follows committed inputs, the judged rule revision, and rule reach."""

from __future__ import annotations

import hashlib
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
import time
import unittest
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from typing import TypedDict, cast, final, override
from unittest import mock

import rules
import style_ledger
from approved_renames import (AppliedCheck, AppliedInCode, BindingRename, Folder, NoAppliedCheck, RenameNotApplied,
                              binding_rename_check)
import style_plan
import style_rules
import berth_board
import coverage_report
import declarations
import style_footprint


PARENT_FILE = "crates/alpha/src/a.rs"
# A cargo-berth that logs each call and keeps lifecycles in a file; a release checkpoints the cwd's HEAD.
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
            self.write(source, "pub fn value() -> u32 { 1 }\n" +
                       ("mod a; mod b; mod c; mod other;\n" if source.endswith("src/lib.rs") else ""))
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

    def cli(self, *arguments: str, success: bool = True) -> subprocess.CompletedProcess[str]:
        prefix = 2 if arguments[0] in ("ticket", "handoff", "tip", "held", "unit") else 1
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

    def test_context_site_refuses_deleted_same_file_duplicate_in_original_crate_scope(self) -> None:
        source = "fn first() {\n    consume(791);\n}\nfn duplicate() {\n    consume(791);\n}\n"
        ledger, _, finding = self.site_fixture("types", rule_name="no-duplicate-implementations",
                                              scope="crate", source=source, before="791", kind="type")
        tip = self.commit_tip(ledger, {PARENT_FILE: source[:source.index("fn duplicate")]})
        self.assertIsInstance(self.strict_site(ledger, finding, tip), style_ledger.NeedsJudgment)
        self.assertIsInstance(ledger.finding_site(finding, tip), style_ledger.NeedsJudgment)
        _ = ledger.follow_declarations([finding.finding_id], tip, lambda _: True)
        self.assertEqual(ledger.named_held([finding.finding_id])[0].follow, style_ledger.NotFollowed())

    def test_site_owner_refuses_nested_function_moved_between_enclosing_functions(self) -> None:
        source = "fn first() {\n    fn work() {\n        let old = 791;\n    }\n}\nfn second() {\n}\n"
        ledger, _, finding = self.site_fixture(source=source, line=3)
        moved = "fn first() {\n}\nfn second() {\n    fn work() {\n        let old = 791;\n    }\n}\n"
        tip = self.commit_tip(ledger, {PARENT_FILE: moved})
        self.assertIsInstance(self.strict_site(ledger, finding, tip), style_ledger.NeedsJudgment)
        self.assertIsInstance(ledger.finding_site(finding, tip), style_ledger.NeedsJudgment)

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

    def test_site_owner_refuses_work_moved_between_nested_extern_functions(self) -> None:
        self.assert_extern_owner_move_refused("fn")

    def test_site_owner_refuses_qualified_work_moved_between_nested_extern_functions(self) -> None:
        self.assert_extern_owner_move_refused('pub(crate) const unsafe extern "C" fn')

    def test_site_owner_refuses_unknown_item_prefix_instead_of_skipping_it(self) -> None:
        source = "fn container() {\n    unrecognized fn first() {\n        fn work() {\n            let old = 791;\n        }\n    }\n}\n"
        ledger, _, finding = self.site_fixture(source=source, line=4)
        tip = self.commit_tip(ledger, {PARENT_FILE: "// header\n" + source})
        self.assertIsInstance(self.strict_site(ledger, finding, tip), style_ledger.NeedsJudgment)
        self.assertIsInstance(ledger.finding_site(finding, tip), style_ledger.NeedsJudgment)

    def test_site_proof_never_withdraws_removed_or_indirectly_included_literals(self) -> None:
        ledger, rule, finding = self.site_fixture("values", rule_name="no-magic-values", kind="restructure")
        judgments = self.judgment_rows(ledger.database)
        for expansion in ('use std::include as load; load!("body.inc");',
                          '#[cfg_attr(all(), path = "body.inc")] mod body;', ''):
            with self.subTest(expansion=expansion):
                tip = self.commit_tip(ledger, {PARENT_FILE: "fn work() {}\n",
                                               MOVED_FILE: expansion + "\n",
                                               "crates/alpha/src/body.inc": "fn included() { consume(791); }\n"})
                self.assertIsInstance(self.strict_site(ledger, finding, tip), style_ledger.NeedsJudgment)
                self.assertIsInstance(ledger.finding_site(finding, tip), style_ledger.NeedsJudgment)
                _ = ledger.follow_declarations([finding.finding_id], tip, lambda _: True)
                self.assertEqual(ledger.named_held([finding.finding_id])[0], finding)
                self.assertEqual(self.judgment_rows(ledger.database), judgments)
                self.assertEqual(self.states(rule)[PARENT_FILE], "stale")

    def test_site_owner_ignores_function_types_and_inline_const_expressions(self) -> None:
        source = "fn work(callback: fn()) {\n    let old = 791;\n    let value = const { 1 };\n}\n"
        ledger, rule, finding = self.site_fixture(source=source)
        tip = self.commit_tip(ledger, {PARENT_FILE: "// header comment\n" + source})
        self.assertEqual(self.strict_site(ledger, finding, tip), style_ledger.UnchangedSite(PARENT_FILE, 3))
        self.assertEqual(ledger.finding_site(finding, tip), style_ledger.UnchangedSite(PARENT_FILE, 3))
        judgments = self.judgment_rows(ledger.database)
        _ = ledger.follow_declarations([finding.finding_id], tip, lambda _: True)
        followed = ledger.named_held([finding.finding_id])[0]
        self.assertEqual(followed.follow, style_ledger.FollowedDeclaration(PARENT_FILE, 2, tip))
        self.assertEqual(followed.line, 3)
        self.assertEqual(self.judgment_rows(ledger.database), judgments)
        self.assertEqual(self.states(rule)[PARENT_FILE], "stale")

    def test_site_proof_refuses_unicode_token_failure_instead_of_empty_parse(self) -> None:
        source = "fn café() {\n    let old = 791;\n}\n"
        ledger, _, finding = self.site_fixture(source=source)
        tip = self.commit_tip(ledger, {PARENT_FILE: "// shift\n" + source})
        self.assertIsInstance(self.strict_site(ledger, finding, tip), style_ledger.NeedsJudgment)
        self.assertIsInstance(ledger.finding_site(finding, tip), style_ledger.NeedsJudgment)
        with self.assertRaises(style_footprint.UnreadableTokenSpans):
            _ = style_footprint.token_spans(source)
        self.assertEqual(style_footprint.token_spans("// no code\n"), ())

    def test_site_follow_preserves_line_evidence_and_pinned_fields_without_restamping_cells(self) -> None:
        ledger, rule, finding = self.site_fixture()
        # A substantive rule edit must remain separate coverage work after the follow.
        _ = (self.rule_dir / "site-rule.md").write_text((self.rule_dir / "site-rule.md").read_text() + "New requirement\n")
        tip = self.commit_tip(ledger, {PARENT_FILE: "// inserted\nfn work() {\n    let old = 791;\n}\nfn sibling() {}\n"})
        judgments = self.judgment_rows(ledger.database)
        _ = ledger.follow_declarations([finding.finding_id], tip, lambda _: True)
        followed = ledger.named_held([finding.finding_id])[0]
        self.assertEqual((followed.finding_id, followed.rule_blob, followed.before, followed.after, followed.state),
                         (finding.finding_id, finding.rule_blob, finding.before, finding.after, finding.state))
        self.assertEqual((followed.path, followed.line), (PARENT_FILE, 3))
        self.assertEqual(followed.follow, style_ledger.FollowedDeclaration(PARENT_FILE, 2, tip))
        self.assertEqual(self.judgment_rows(ledger.database), judgments)
        self.assertEqual(self.states(rule)[PARENT_FILE], "stale")
        rows = self.held_rows(ledger.database)
        _ = ledger.follow_declarations([finding.finding_id], tip, lambda _: True)
        self.assertEqual(self.held_rows(ledger.database), rows)

    def test_site_follow_requires_exact_line_and_unambiguous_owner(self) -> None:
        ledger, _, finding = self.site_fixture()
        for source in ("fn renamed() {\n    let old = 791;\n}\n",
                       "fn work() {\n    let old = 791;\n    let old = 791;\n}\n",
                       "fn work() {\n    let old =\n        791;\n}\n",
                       "fn work() {}\nfn moved() {\n    let old = 791;\n}\n"):
            with self.subTest(source=source):
                tip = self.commit_tip(ledger, {PARENT_FILE: source})
                self.assertIsInstance(self.strict_site(ledger, finding, tip), style_ledger.NeedsJudgment)

    def test_context_site_follows_whole_function_but_rejects_body_attributes_imports_and_owner_changes(self) -> None:
        original = "use crate::Other;\nstruct Parent;\nimpl Parent {\n    #[inline]\n    fn work() {\n        consume(791);\n    }\n}\n"
        ledger, _, finding = self.site_fixture("types", source=original, line=6, before="791", kind="type")
        tip = self.commit_tip(ledger, {PARENT_FILE: original + "fn sibling() {}\n"})
        self.assertEqual(self.strict_site(ledger, finding, tip), style_ledger.UnchangedSite(PARENT_FILE, 6))
        for source in (original.replace("consume(791);", "consume(791);\n        other();"),
                       original.replace("#[inline]", "#[cold]"), original.replace("crate::Other", "crate::Changed"),
                       original.replace("impl Parent", "impl Other")):
            with self.subTest(source=source):
                tip = self.commit_tip(ledger, {PARENT_FILE: source})
                self.assertIsInstance(self.strict_site(ledger, finding, tip), style_ledger.NeedsJudgment)

    def test_context_site_proves_complete_impl_including_its_attributes_and_body(self) -> None:
        source = "struct Parent;\n#[allow(dead_code)]\nimpl Parent {\n    fn work() { consume(791); }\n}\n"
        ledger, _, finding = self.site_fixture("types", source=source, line=3, kind="type", before="impl Parent")
        tip = self.commit_tip(ledger, {PARENT_FILE: source + "fn sibling() {}\n"})
        self.assertEqual(self.strict_site(ledger, finding, tip), style_ledger.UnchangedSite(PARENT_FILE, 3))
        tip = self.commit_tip(ledger, {PARENT_FILE: source.replace("consume(791)", "consume(792)")})
        self.assertIsInstance(self.strict_site(ledger, finding, tip), style_ledger.NeedsJudgment)
        tip = self.commit_tip(ledger, {PARENT_FILE: source.replace("allow(dead_code)", "deny(dead_code)")})
        self.assertIsInstance(self.strict_site(ledger, finding, tip), style_ledger.NeedsJudgment)

    def test_site_owner_includes_inline_module_identity(self) -> None:
        source = "mod first {\n    fn work() {\n        let old = 791;\n    }\n}\n"
        ledger, _, finding = self.site_fixture(source=source, line=3)
        tip = self.commit_tip(ledger, {PARENT_FILE: source.replace("mod first", "mod renamed")})
        self.assertIsInstance(self.strict_site(ledger, finding, tip), style_ledger.NeedsJudgment)

    def test_site_follow_requires_byte_identical_line_endings(self) -> None:
        source = "fn work() {\r\n    let old = 791;\r\n}\r\n"
        ledger, _, finding = self.site_fixture(source=source)
        tip = self.commit_tip(ledger, {PARENT_FILE: source.replace("\r\n", "\n")})
        self.assertIsInstance(self.strict_site(ledger, finding, tip), style_ledger.NeedsJudgment)

    def test_context_site_keeps_original_crate_scope_when_rule_narrows_and_after_prior_follow(self) -> None:
        ledger, _, finding = self.site_fixture("types", scope="crate", kind="type")
        source = (self.repo / PARENT_FILE).read_text()
        tip = self.commit_tip(ledger, {PARENT_FILE: "fn sibling() {}\n" + source})
        self.assertIsInstance(self.strict_site(ledger, finding, tip), style_ledger.NeedsJudgment)
        self.assertIsInstance(ledger.finding_site(finding, tip), style_ledger.NeedsJudgment)
        tip = self.commit_tip(ledger, {PARENT_FILE: "fn sibling() {}\n", MOVED_FILE: "// moved\n" + source})
        outcomes = ledger.follow_declarations([finding.finding_id], tip, lambda _: True)
        self.assertIsInstance(outcomes.outcomes[0], style_ledger.DeclarationMoved)
        followed = ledger.named_held([finding.finding_id])[0]
        self.assertEqual(followed.line, 3)
        self.assertEqual(followed.path, MOVED_FILE)
        self.assertEqual(followed.follow, style_ledger.FollowedDeclaration(PARENT_FILE, 2, tip))
        _ = (self.rule_dir / "site-rule.md").write_text("---\ntier: C\nfamily: types\ninput_scope: file\n---\n# Narrowed\n")
        tip = self.commit_tip(ledger, {self.sources[1]: "fn changed_dependency() {}\n"})
        self.assertIsInstance(self.strict_site(ledger, followed, tip), style_ledger.NeedsJudgment)

    def test_context_site_rejects_changed_explicit_reads(self) -> None:
        ledger, rule, finding = self.site_fixture("modules", kind="restructure", scope="crate")
        self.record_reads(rule, PARENT_FILE, ledger.synced_commit(), (self.sources[1],), verdict="held", ticket="judge-1")
        tip = self.commit_tip(ledger, {PARENT_FILE: "fn sibling() {}\n" + (self.repo / PARENT_FILE).read_text(),
                                       self.sources[1]: "fn changed_dependency() {}\n"})
        self.assertIsInstance(self.strict_site(ledger, finding, tip), style_ledger.NeedsJudgment)

    def test_site_follow_rejects_late_pre_deletion_judgment_after_identical_restore(self) -> None:
        ledger, rule, finding = self.site_fixture()
        source = (self.repo / PARENT_FILE).read_text()
        before = ledger.synced_commit()
        deletion = self.commit_tip(ledger, {PARENT_FILE: None})
        with sqlite3.connect(ledger.database) as connection:
            _ = connection.execute('INSERT INTO judgments(rule,rule_blob,input_fingerprint,path,"commit",verdict,verdict_detail,ticket,worker,recorded_at,input_scope,declared_scope) '
                                   + "VALUES (?,?, '',?,?,'removed','','removal','test','2000','file','rule')",
                                   (rule, self.blob(rule), PARENT_FILE, deletion))
        tip = self.commit_tip(ledger, {PARENT_FILE: source})
        # Recorded after restoration, but still judges the pre-deletion file.
        ledger.record(rule, PARENT_FILE, before, self.blob(rule), "held", ticket="judge-1", worker="late",
                      input_scope=style_ledger.DeclaredInputScope.RULE)
        self.assertIsInstance(self.strict_site(ledger, finding, tip), style_ledger.NeedsJudgment)
        _ = ledger.follow_declarations([finding.finding_id], tip, lambda _: True)
        self.assertEqual(ledger.named_held([finding.finding_id])[0].follow, style_ledger.NotFollowed())

    def test_site_follow_rechecks_rows_under_lock_and_never_overwrites_new_text(self) -> None:
        ledger, _, finding = self.site_fixture()
        tip = self.commit_tip(ledger, {PARENT_FILE: "// shift\n" + (self.repo / PARENT_FILE).read_text()})
        original = cast(Callable[[tuple[str, ...], style_ledger.RepositoryTip, Callable[[Path], bool]], style_ledger.DeclarationReading],
                        getattr(ledger, "_declaration_reading"))
        raced = False

        def racing_read(ids: tuple[str, ...], snapshot: style_ledger.RepositoryTip,
                        approved: Callable[[Path], bool]) -> style_ledger.DeclarationReading:
            nonlocal raced
            reading = original(ids, snapshot, approved)
            if not raced:
                raced = True
                with sqlite3.connect(ledger.database) as connection:
                    _ = connection.execute("UPDATE held_findings SET text='changed by another writer' WHERE finding_id=?", (finding.finding_id,))
            return reading

        with mock.patch.object(ledger, "_declaration_reading", side_effect=racing_read):
            _ = ledger.follow_declarations([finding.finding_id], tip, lambda _: True)
        current = ledger.named_held([finding.finding_id])[0]
        self.assertEqual((current.text, current.follow), ("changed by another writer", style_ledger.NotFollowed()))

    def test_site_follow_refuses_tip_race_before_writing(self) -> None:
        ledger, _, finding = self.site_fixture()
        tip = self.commit_tip(ledger, {PARENT_FILE: "// shift\n" + (self.repo / PARENT_FILE).read_text()})
        original = cast(Callable[[tuple[str, ...], style_ledger.RepositoryTip, Callable[[Path], bool]], style_ledger.DeclarationReading],
                        getattr(ledger, "_declaration_reading"))
        raced = False

        def racing_read(ids: tuple[str, ...], snapshot: style_ledger.RepositoryTip,
                        approved: Callable[[Path], bool]) -> style_ledger.DeclarationReading:
            nonlocal raced
            reading = original(ids, snapshot, approved)
            if not raced:
                raced = True
                _ = self.commit_tip(ledger, {MOVED_FILE: "fn new_tip() {}\n"})
            return reading

        with mock.patch.object(ledger, "_declaration_reading", side_effect=racing_read):
            with self.assertRaisesRegex(style_ledger.LedgerError, "synced tip moved"):
                _ = ledger.follow_declarations([finding.finding_id], tip, lambda _: True)
        self.assertEqual(ledger.named_held([finding.finding_id])[0].follow, style_ledger.NotFollowed())

    def test_site_proof_requires_a_type_proposals_before_subject_in_its_item(self) -> None:
        ledger, _, finding = self.site_fixture(kind="type", before="unlocated subject")
        tip = self.commit_tip(ledger, {PARENT_FILE: "// shift\n" + (self.repo / PARENT_FILE).read_text()})
        self.assertIsInstance(ledger.finding_site(finding, tip), style_ledger.NeedsJudgment)

    def test_site_proof_rejects_partly_unreadable_item_parse(self) -> None:
        ledger, _, finding = self.site_fixture()
        source = (self.repo / PARENT_FILE).read_text()
        tip = self.commit_tip(ledger, {PARENT_FILE: source + "fn sibling() {}\n"})
        self.assertIsInstance(ledger.finding_site(finding, tip), style_ledger.UnchangedSite)
        tip = self.commit_tip(ledger, {PARENT_FILE: source + "mod nested { unsupported; }\n"})
        self.assertIsInstance(ledger.finding_site(finding, tip), style_ledger.NeedsJudgment)
        _ = ledger.follow_declarations([finding.finding_id], tip, lambda _: True)
        self.assertEqual(ledger.named_held([finding.finding_id])[0].follow, style_ledger.NotFollowed())

    def test_site_proof_rejects_non_utf8_pinned_rule_text(self) -> None:
        ledger, _, finding = self.site_fixture()
        tip = self.commit_tip(ledger, {PARENT_FILE: "// shift\n" + (self.repo / PARENT_FILE).read_text()})
        self.assertIsInstance(ledger.finding_site(finding, tip), style_ledger.UnchangedSite)
        with sqlite3.connect(ledger.database) as connection:
            _ = connection.execute("UPDATE rule_texts SET text=? WHERE blob=?", (b"family: names\n\xff", finding.rule_blob))
        self.assertIsInstance(ledger.finding_site(finding, tip), style_ledger.NeedsJudgment)

    def test_site_proof_rejects_non_utf8_source(self) -> None:
        ledger, _, finding = self.site_fixture()
        source = (self.repo / PARENT_FILE).read_bytes()
        _ = (self.repo / PARENT_FILE).write_bytes(source + b"// unreadable: \xff\n")
        tip = self.commit("Non-UTF-8 source")
        ledger.set_tip(tip)
        self.assertIsInstance(ledger.finding_site(finding, tip), style_ledger.NeedsJudgment)
        _ = ledger.follow_declarations([finding.finding_id], tip, lambda _: True)
        self.assertEqual(ledger.named_held([finding.finding_id])[0].follow, style_ledger.NotFollowed())

    def test_refill_lock_blocks_another_worktree_until_the_body_exits(self) -> None:
        worktree = self.root / "other-checkout"
        _ = self.git("worktree", "add", "-q", "-b", "other", str(worktree))
        lock_path = self.root / "state" / self.repo.name / "refill.lock"
        self.assertEqual(lock_path.parent, style_ledger.ledger_location(self.repo).database.parent)
        self.assertFalse(lock_path.parent.exists())
        attempting, acquired = threading.Event(), threading.Event()

        def take_lock() -> None:
            attempting.set()
            with style_ledger.refill_lock(worktree):
                acquired.set()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with style_ledger.refill_lock(self.repo):
                self.assertTrue(lock_path.is_file())
                contender = pool.submit(take_lock)
                self.assertTrue(attempting.wait(5), "second worktree did not attempt refill")
                self.assertFalse(acquired.wait(0.2), "second worktree entered a held refill lock")
            contender.result(timeout=5)
        self.assertTrue(acquired.is_set(), "refill lock was not released on context exit")

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

    def test_migration_25_backfills_from_merged_build_ticket_else_the_finding(self) -> None:
        database = self.root / "version-24.sqlite"
        rule = self.rule("backfill")
        with closing(sqlite3.connect(database)) as connection, connection:
            for number, migration in sorted(style_ledger.MIGRATIONS.items()):
                if number <= 24:
                    _ = connection.executescript(migration)
            _ = connection.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
            _ = connection.execute("INSERT INTO schema_version VALUES (24)")
            for ticket, state in (("merged", "merged"), ("pending", "checkpointed")):
                _ = connection.execute(
                    "INSERT INTO tickets(id,unit,kind,family,rule,crate,files,sites,rules,rule_blobs,state,branch,"
                    + "checkpoint,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ticket, ticket, "proposal", "names", rule, "crates/alpha", "[]", "[]", "[]", "{}", state,
                     "main", "", "created", "2026-10-20T08:00:00+00:00"),
                )
            for finding_id, state, ticket in (("merged-build", "built", "merged"), ("pending-build", "built", "pending"),
                                               ("no-ticket", "built", ""), ("missing-ticket", "built", "missing"),
                                               ("not-built", "filed", "merged")):
                _ = connection.execute(
                    "INSERT INTO held_findings(finding_id,rule,path,line,text,kind,before,after,rule_blob,ticket,state,"
                    + "proposal,created_at,updated_at,build_ticket) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (finding_id, rule, self.sources[0], 1, "change", "type", "u32", "u64", self.blob(rule), "judge",
                     state, "", "created", "2026-10-21T09:00:00+00:00", ticket),
                )
        with style_ledger.Ledger(self.repo, database):
            pass
        with closing(sqlite3.connect(database)) as connection:
            rows = cast(list[tuple[str, str]], connection.execute(
                "SELECT finding_id,built_at FROM held_findings ORDER BY finding_id",
            ).fetchall())
            self.assertEqual(dict(rows), {
                "merged-build": "2026-10-20T08:00:00+00:00",
                "pending-build": "2026-10-21T09:00:00+00:00",
                "no-ticket": "2026-10-21T09:00:00+00:00",
                "missing-ticket": "2026-10-21T09:00:00+00:00", "not-built": "",
            })
            self.assertEqual(connection.execute("SELECT * FROM refill_results").fetchall(), [])

    def refill_result(self, database: Path, unit: str = "names-unit") -> tuple[str, str, str, int, int, int, int]:
        with closing(sqlite3.connect(database)) as connection:
            return cast(tuple[str, str, str, int, int, int, int], connection.execute(
                "SELECT at,outcome,split_at,ready,changed,structure,other FROM refill_results WHERE unit=?", (unit,),
            ).fetchone())

    def test_record_refill_upserts_only_the_outcome_and_leaves_reserved_columns_alone(self) -> None:
        ledger = self.ledger()
        first = "2026-10-27T08:15:00+00:00"
        later = "2026-10-28T09:30:00+00:00"
        with (mock.patch.object(rules, "load_rules", side_effect=AssertionError("record_refill loads no catalog")),
              mock.patch.object(style_ledger, "open_ledger", side_effect=AssertionError("record_refill runs no migration"))):
            with mock.patch.object(style_ledger, "_now", return_value=first):
                style_ledger.record_refill(self.repo, "names-unit", "phase written")
            self.assertEqual(self.refill_result(ledger.database), (first, "phase written", "", 0, 0, 0, 0))
            with closing(sqlite3.connect(ledger.database)) as connection, connection:
                _ = connection.execute("UPDATE refill_results SET split_at=?,ready=1,changed=2,structure=3,other=4", (first,))
            with mock.patch.object(style_ledger, "_now", return_value=later):
                style_ledger.record_refill(self.repo, "names-unit", "repair first")
                style_ledger.record_refill(self.repo, "types-unit", "board unavailable")
            self.assertEqual(self.refill_result(ledger.database), (later, "repair first", first, 1, 2, 3, 4))
            self.assertEqual(self.refill_result(ledger.database, "types-unit"), (later, "board unavailable", "", 0, 0, 0, 0))
        with closing(sqlite3.connect(ledger.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM refill_results").fetchone(), (2,))

    def test_record_refill_does_nothing_without_a_database_or_table(self) -> None:
        database = style_ledger.ledger_location(self.repo).database
        style_ledger.record_refill(self.repo, "names-unit", "repair first")
        self.assertFalse(database.exists())
        database.parent.mkdir(parents=True)
        with closing(sqlite3.connect(database)) as connection, connection:
            _ = connection.execute("CREATE TABLE existing(value TEXT)")
            _ = connection.execute("INSERT INTO existing VALUES ('kept')")
        before = database.read_bytes()
        style_ledger.record_refill(self.repo, "names-unit", "no work")
        self.assertEqual(database.read_bytes(), before)
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [("existing",)])

    def test_a_first_split_inserts_counts_without_inventing_a_refill(self) -> None:
        ledger = self.ledger()
        measured_at = "2026-10-28T10:01:02+00:00"
        split = style_ledger.RecordedSplit(7, 6, 5, 4, measured_at)
        with (mock.patch.object(rules, "load_rules", side_effect=AssertionError("no catalog write")),
              mock.patch.object(style_ledger, "open_ledger", side_effect=AssertionError("no migration"))):
            style_ledger.record_split(self.repo, "names-unit", split)
        self.assertEqual(self.refill_result(ledger.database), ("", "", measured_at, 7, 6, 5, 4))

    def test_the_split_blob_cache_reads_each_tree_once_and_keeps_versions_distinct(self) -> None:
        path = self.sources[0]
        old = (self.repo / path).read_text()
        self.write(path, "pub fn changed() -> u64 { 2 }\n")
        current = self.commit("Change one source")
        read = style_ledger._git  # pyright: ignore[reportPrivateUsage]
        trees: list[tuple[str, ...]] = []

        def git(repo: Path, *arguments: str) -> str:
            if arguments[0] == "ls-tree":
                trees.append(arguments)
            return read(repo, *arguments)

        with mock.patch.object(style_ledger, "_git", side_effect=git):
            cache = style_ledger.GitReadCache(self.repo)
            for _ in range(2):
                self.assertEqual(cache.text(self.initial_tip, path), old)
                self.assertEqual(cache.text(current, path), "pub fn changed() -> u64 { 2 }\n")
                self.assertEqual(cache.blob(current, self.sources[1]), cache.blob(self.initial_tip, self.sources[1]))
        self.assertEqual(len(trees), 2)
        self.assertEqual({arguments[-1] for arguments in trees}, {self.initial_tip, current})
        self.assertTrue(all(any(option.startswith("-") and "r" in option for option in arguments[1:-1])
                            for arguments in trees))

    def test_git_read_cache_and_ledgers_resolve_a_branch_again_after_it_moves(self) -> None:
        cache = style_ledger.GitReadCache(self.repo)
        ledger = self.ledger()
        with style_ledger.Ledger(self.repo, ledger.database, git_cache=cache) as cached_ledger:
            self.assertEqual(cache.tip("main").commit, self.initial_tip)
            self.assertEqual(cached_ledger._tip("main").commit, self.initial_tip)  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(ledger._tip("main").commit, self.initial_tip)  # pyright: ignore[reportPrivateUsage]
            self.write(self.sources[0], "pub fn advanced() {}\n")
            current = self.commit("Advance main after caching its tip")
            with self.subTest(reader="cache"):
                self.assertEqual(cache.tip("main").commit, current)
            with self.subTest(reader="cached ledger"):
                self.assertEqual(cached_ledger._tip("main").commit, current)  # pyright: ignore[reportPrivateUsage]
            with self.subTest(reader="ledger"):
                self.assertEqual(ledger._tip("main").commit, current)  # pyright: ignore[reportPrivateUsage]
            self.assertNotIn("main", cache.tips)
            self.assertEqual(cache.tip(self.initial_tip).commit, self.initial_tip)

    def test_site_proof_shares_blob_reads_with_the_split_cache(self) -> None:
        source = "fn work() {\n    let old = 791;\n}\n"
        ledger, _, finding = self.site_fixture(source=source)
        base = ledger.synced_commit()
        tip = self.commit_tip(ledger, {PARENT_FILE: "// header\n" + source})
        cache = style_ledger.GitReadCache(self.repo)
        with mock.patch.object(style_ledger, "blob_contents", wraps=style_ledger.blob_contents) as reads:
            self.assertEqual(cache.file(base, PARENT_FILE), source.encode())
            self.assertEqual(cache.file(tip, PARENT_FILE), ("// header\n" + source).encode())
            # The direct site read and the declaration step's batched preparation share those bytes.
            with style_ledger.Ledger(self.repo, ledger.database, git_cache=cache) as cached_ledger:
                self.assertIsInstance(self.strict_site(cached_ledger, finding, tip), style_ledger.UnchangedSite)
            with style_ledger.Ledger(self.repo, ledger.database, git_cache=cache) as cached_ledger:
                self.assertIsInstance(cached_ledger.finding_site(finding, tip), style_ledger.UnchangedSite)
        requested = [blob for call in reads.call_args_list for blob in cast(dict[str, str], call.args[1]).values()]
        for commit in (base, tip):
            with self.subTest(commit=commit):
                self.assertEqual(requested.count(cache.blob(commit, PARENT_FILE)), 1)

    def test_split_write_uses_a_small_busy_timeout_on_a_locked_ledger(self) -> None:
        ledger = self.ledger()
        previous = style_ledger.RecordedSplit(9, 8, 7, 6, "2026-10-27T12:00:00+00:00")
        style_ledger.record_split(self.repo, "names-unit", previous)
        before = self.refill_result(ledger.database)
        with closing(sqlite3.connect(ledger.database)) as writer:
            _ = writer.execute("BEGIN IMMEDIATE")
            began = time.monotonic()
            with self.assertRaisesRegex(sqlite3.OperationalError, "database is locked"):
                style_ledger.record_split(self.repo, "names-unit",
                                          style_ledger.RecordedSplit(1, 2, 3, 4, "2026-10-28T12:00:00+00:00"),
                                          busy_timeout_seconds=0.05)
            elapsed = time.monotonic() - began
            writer.rollback()
        self.assertLess(elapsed, 0.75)
        self.assertEqual(self.refill_result(ledger.database), before)

    def test_a_later_split_preserves_the_refill_and_a_refill_preserves_the_split(self) -> None:
        ledger = self.ledger()
        first_at = "2026-10-27T12:00:00+00:00"
        later_at = "2026-10-28T10:00:00+00:00"
        with mock.patch.object(style_ledger, "_now", return_value=first_at):
            style_ledger.record_refill(self.repo, "names-unit", "phase written")
        style_ledger.record_split(self.repo, "names-unit", style_ledger.RecordedSplit(1, 2, 3, 4, first_at))
        style_ledger.record_split(self.repo, "names-unit", style_ledger.RecordedSplit(0, 0, 0, 0, later_at))
        self.assertEqual(self.refill_result(ledger.database), (first_at, "phase written", later_at, 0, 0, 0, 0))
        with mock.patch.object(style_ledger, "_now", return_value=later_at):
            style_ledger.record_refill(self.repo, "names-unit", "waiting for sync")
        self.assertEqual(self.refill_result(ledger.database), (later_at, "waiting for sync", later_at, 0, 0, 0, 0))

    def test_proposal_merge_sets_built_at_once(self) -> None:
        rule, issue, ids = self.decision_findings()
        ledger = self.ledger()
        ledger.ticket_add("names-1", "names", "proposal", "names", "crates/alpha", [self.sources[0]], [rule],
                          proposal=str(issue), finding_ids=ids[:1])
        ledger.ticket_assign("names-1", "main")
        self.write(self.sources[0], "pub enum Outcome { New }\n")
        tip = self.commit("Apply approved rename")
        ledger.set_tip(tip, "main")
        ledger.ticket_checkpoint("names-1", tip)
        with mock.patch.object(style_ledger, "_now", return_value="2026-10-27T10:00:00+00:00"):
            self.assertEqual(ledger.ticket_merge("names-1"), ())
        self.assert_build_time_survives_reading(ledger, ids[0])
        with mock.patch.object(style_ledger, "_now", return_value="2026-10-29T12:00:00+00:00"):
            _ = ledger.ticket_merge("names-1")
        self.assertEqual(self.built_time(ledger, ids[0]), "2026-10-27T10:00:00+00:00")

    def test_declaration_follow_sets_built_at_once(self) -> None:
        ledger = self.ledger()
        finding_id = self.renamed_parent(ledger)
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT.replace("Parent", "Renamed")})
        with mock.patch.object(style_ledger, "_now", return_value="2026-10-27T10:00:00+00:00"):
            outcome, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(outcome, style_ledger.DeclarationRenamed)
        self.assert_build_time_survives_reading(ledger, finding_id)
        with mock.patch.object(style_ledger, "_now", return_value="2026-10-29T12:00:00+00:00"):
            _ = self.follow_approved(ledger, [finding_id])
        self.assertEqual(self.built_time(ledger, finding_id), "2026-10-27T10:00:00+00:00")

    def test_held_built_sets_built_at_once_and_reopening_clears_it(self) -> None:
        ledger, finding_id, _, _ = self.checkpointed_type_findings()
        with mock.patch.object(style_ledger, "_now", return_value="2026-10-27T10:00:00+00:00"):
            ledger.mark_built(finding_id)
        self.assert_build_time_survives_reading(ledger, finding_id)
        with mock.patch.object(style_ledger, "_now", return_value="2026-10-29T12:00:00+00:00"):
            ledger.mark_built(finding_id)
        self.assertEqual(self.built_time(ledger, finding_id), "2026-10-27T10:00:00+00:00")
        ledger.return_to_filed({finding_id: style_ledger.ReadByPerson("reader")})
        self.assertEqual(self.built_time(ledger, finding_id), "")

    def test_mark_built_at_merge_sets_built_at_once(self) -> None:
        _, _, ids = self.decision_findings()
        self.write(self.sources[0], "pub enum Outcome { New }\n")
        tip = self.commit("Apply rename at merge")
        ledger = self.ledger()
        ledger.set_tip(tip, "main")
        with mock.patch.object(style_ledger, "_now", return_value="2026-10-27T10:00:00+00:00"):
            _ = ledger.mark_built_at_merge(ids[:1], tip, style_ledger.NotReadByPerson())
        self.assert_build_time_survives_reading(ledger, ids[0])
        with mock.patch.object(style_ledger, "_now", return_value="2026-10-29T12:00:00+00:00"):
            with self.assertRaisesRegex(style_ledger.LedgerError, "cannot mark a built finding built"):
                _ = ledger.mark_built_at_merge(ids[:1], tip, style_ledger.ReadByPerson("other"))
        self.assertEqual(self.built_time(ledger, ids[0]), "2026-10-27T10:00:00+00:00")

    def test_withdrawing_a_built_duplicate_clears_its_built_at(self) -> None:
        _, issue, ids = self.decision_findings()
        self.write(self.sources[0], "pub enum Outcome { New }\n")
        tip = self.commit("Apply rename at merge")
        ledger = self.ledger()
        ledger.set_tip(tip, "main")
        with mock.patch.object(style_ledger, "_now", return_value="2026-10-27T10:00:00+00:00"):
            _ = ledger.mark_built_at_merge(ids[:1], tip, style_ledger.NotReadByPerson())
        self.assertEqual(self.built_time(ledger, ids[0]), "2026-10-27T10:00:00+00:00")
        ledger.withdraw_duplicates((style_ledger.BareDuplicate(ids[0], ids[1], issue),))
        self.assertEqual(self.built_time(ledger, ids[0]), "")
        states = {finding.finding_id: finding.state for finding in ledger.proposal_findings(issue)}
        self.assertEqual(states[ids[0]], style_ledger.Withdrawn(issue, style_ledger.DuplicateOf(ids[1])))

    def test_record_held_refuses_a_cell_with_no_findings(self) -> None:
        rule = self.rule("read")
        self.set_tip(self.initial_tip)
        result = self.cli("record", "--rule", rule, "--path", self.sources[0], "--commit", self.initial_tip,
                          "--rule-blob", self.blob(rule), "--verdict", "held", "--ticket", "outside", "--worker", "test", success=False)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(rule, result.stderr)
        self.assertIn(self.sources[0], result.stderr)
        self.assertIn("held add", result.stderr)
        self.assertEqual(self.states(rule)[self.sources[0]], "open")

    def test_record_held_names_inactive_findings_and_keeps_the_users_waiver(self) -> None:
        rule = self.rule("read")
        self.set_tip(self.initial_tip)
        finding = self.hold_finding(rule, 1)
        _ = self.cli("held", "waive", "--id", finding, "--person", "nate")
        result = self.cli("record", "--rule", rule, "--path", self.sources[0], "--commit", self.initial_tip,
                          "--rule-blob", self.blob(rule), "--verdict", "held", "--ticket", "outside", "--worker", "test", success=False)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        for words in (rule, self.sources[0], finding, "waived", "user's decision", "held add", "record waived"):
            self.assertIn(words, result.stderr)
        self.assertIsInstance(self.ledger().named_held((finding,))[0].state, style_ledger.Waived)
        self.assertEqual(self.states(rule)[self.sources[0]], "open")

    def test_record_held_accepts_unfiled_and_filed_findings_only_on_the_matching_cell(self) -> None:
        rule = self.rule("read")
        other = self.rule("other")
        self.set_tip(self.initial_tip)
        finding = self.hold_finding(rule, 1)
        self.record(rule, self.sources[0], self.initial_tip, "held")
        ledger = self.ledger()
        ledger.file_held(finding, self.root / "decision.md")
        self.record(rule, self.sources[0], self.initial_tip, "held", ticket="filed-judge")
        self.assertEqual(self.states(rule)[self.sources[0]], "current")
        for wrong_rule, wrong_path in ((rule, self.sources[1]), (other, self.sources[0])):
            with self.subTest(rule=wrong_rule, path=wrong_path):
                with self.assertRaises(style_ledger.LedgerError):
                    ledger.record(wrong_rule, wrong_path, self.initial_tip, self.blob(wrong_rule), "held")

    def test_record_held_reports_built_and_withdrawn_findings_without_counting_them_as_live(self) -> None:
        rule = self.rule("read")
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        built, withdrawn = self.hold_finding(rule, 1), self.hold_finding(rule, 2)
        ledger.file_held(built, self.root / "decision.md")
        ledger.file_held(withdrawn, self.root / "decision.md")
        self.record(rule, self.sources[0], self.initial_tip, "exception")
        with closing(sqlite3.connect(ledger.database)) as connection:
            _ = connection.execute("UPDATE held_findings SET state='built' WHERE finding_id=?", (built,))
            _ = connection.execute("UPDATE held_findings SET state='withdrawn',withdrawn_by=(SELECT MAX(id) FROM judgments) "
                                   + "WHERE finding_id=?", (withdrawn,))
            connection.commit()
        with self.assertRaises(style_ledger.LedgerError) as refused:
            ledger.record(rule, self.sources[0], self.initial_tip, self.blob(rule), "held")
        for words in (built, "built", withdrawn, "withdrawn"):
            self.assertIn(words, str(refused.exception))
        self.assertEqual(ledger.latest_held_judgments(), {})

    def test_record_held_maps_manifest_findings_to_their_crate_cell(self) -> None:
        rule = self.rule("manifest", "tier: D\nfamily: modules\ninput_scope: manifest\n")
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        _ = ledger.hold(rule, self.sources[0], 1, "Change manifest", "restructure", "before", "after", self.blob(rule), "outside")
        ledger.record(rule, "crates/alpha", self.initial_tip, self.blob(rule), "held")
        self.assertEqual(self.states(rule)["crates/alpha"], "current")
        with self.assertRaises(style_ledger.LedgerError):
            ledger.record(rule, "crates/beta", self.initial_tip, self.blob(rule), "held")

    def test_unwaiving_a_finding_restores_a_worker_recorded_waived_verdict_to_held(self) -> None:
        rule = self.rule("read")
        self.set_tip(self.initial_tip)
        finding = self.hold_finding(rule, 1)
        _ = self.cli("held", "waive", "--id", finding, "--person", "nate")
        self.record(rule, self.sources[0], self.initial_tip, "waived")
        ledger = self.ledger()
        self.assertIsInstance(ledger.newest_held_judgment(rule, self.sources[0]), style_ledger.NoHeldJudgment)
        self.assertEqual(ledger.unwaive_held((finding,), style_ledger.WaiveUndoneBy("ana")), (finding,))
        self.assertIsInstance(ledger.named_held((finding,))[0].state, style_ledger.Unfiled)
        self.assertEqual(ledger.latest_held_judgments(), {(rule, self.sources[0]): self.initial_tip})

    def test_preview_ledger_migrates_and_changes_only_a_temporary_copy(self) -> None:
        rule = self.rule("preview")
        database = self.root / "state/sample/ledger.sqlite"
        database.parent.mkdir(parents=True)
        issue = self.root / "decision.md"
        with closing(sqlite3.connect(database)) as connection:
            for number, migration in sorted(style_ledger.MIGRATIONS.items()):
                if number <= 10:
                    _ = connection.executescript(migration)
            _ = connection.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
            _ = connection.execute("INSERT INTO schema_version VALUES (10)")
            _ = connection.execute("INSERT INTO held_findings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                   ("legacy", rule, self.sources[0], 2, "change", "rename", "old", "new", self.blob(rule),
                                    "read-1", "filed", str(issue), "created", "updated"))
            connection.commit()
        original = database.read_bytes()
        with style_ledger.preview_ledger(self.repo) as ledger:
            preview_database = ledger.database
            finding, = ledger.proposal_findings(issue)
            self.assertEqual(finding.before, "old")
            self.assertIsInstance(finding.build_ticket, style_ledger.NoBuildTicket)
            ledger.waive_held(finding.finding_id, style_ledger.WaiveByPerson("nate"))
            self.assertIsInstance(ledger.proposal_findings(issue)[0].state, style_ledger.Waived)
        self.assertEqual(database.read_bytes(), original)
        self.assertFalse(preview_database.exists())
        with closing(sqlite3.connect(database)) as connection:
            version = cast(tuple[int], connection.execute("SELECT version FROM schema_version").fetchone())
            state = cast(tuple[str], connection.execute("SELECT state FROM held_findings").fetchone())
        self.assertEqual(version, (10,))
        self.assertEqual(state, ("filed",))

    def test_preview_ledger_does_not_create_missing_state(self) -> None:
        state = self.root / "state"
        with style_ledger.preview_ledger(self.repo) as ledger:
            self.assertEqual(ledger.proposal_tickets(), ())
            preview_database = ledger.database
        self.assertFalse(state.exists())
        self.assertFalse(preview_database.exists())

    def test_decision_merge_builds_only_its_ticket_findings(self) -> None:
        rule, issue, ids = self.decision_findings()
        ledger = self.ledger()
        ledger.ticket_add("names-1", "names", "proposal", "names", "crates/alpha", [self.sources[0]], [rule],
                          proposal=str(issue), finding_ids=ids[:1])
        ledger.ticket_assign("names-1", "main")
        origin = ledger.ticket_origin("names-1")
        self.assertEqual(origin, style_ledger.ApprovedProposal(issue, "names", ids[:1]))
        self.write(self.sources[0], "pub enum Outcome { New }\n")
        applied = self.commit("Rename Old to New")
        ledger.set_tip(applied, "main")
        ledger.ticket_checkpoint("names-1", applied)
        self.assertEqual(ledger.ticket_merge("names-1"), ())
        findings = ledger.proposal_findings(issue)
        self.assertIsInstance(findings[0].state, style_ledger.Built)
        self.assertEqual(findings[1].state, style_ledger.Filed(issue))
        self.assertIsInstance(findings[1].build_ticket, style_ledger.NoBuildTicket)

    def test_decision_merge_builds_an_applied_rename_and_files_an_unapplied_one_again(self) -> None:
        rule, issue, ids = self.decision_findings()
        ledger = self.ledger()
        ledger.ticket_add("names-1", "names", "proposal", "names", "crates/alpha", list(self.sources[:2]), [rule],
                          proposal=str(issue), finding_ids=ids)
        ledger.ticket_assign("names-1", "main")
        self.write(self.sources[0], "pub enum Outcome { New }\n")
        self.write(self.sources[1], "pub enum Outcome { Old }\n")
        checkpoint = self.commit("Rename one of two")
        ledger.set_tip(checkpoint, "main")
        ledger.ticket_checkpoint("names-1", checkpoint)
        output = self.cli("ticket", "merge", "--id", "names-1").stdout
        self.assertEqual(output, f"not built {ids[1]}: rename not applied in {self.sources[1]}\n")
        built, unbuilt = self.ledger().proposal_findings(issue)
        self.assertIsInstance(built.state, style_ledger.Built)
        self.assertEqual(unbuilt.state, style_ledger.Filed(issue))
        self.assertIsInstance(unbuilt.build_ticket, style_ledger.NoBuildTicket)

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

    def test_binding_carry_restamps_types_and_follows_open_sites_through_reflow(self) -> None:
        source = "fn f() {\n    let old = input;\n    consume(old);\n}\n"
        ledger, _, base = self.binding_ticket(source, ((2, "old", "fresh"),))
        path = self.sources[0]
        types = self.rule("types-read", "tier: C\nfamily: types\ninput_scope: file\n")
        # The types read predates the rename ticket but reads exactly the base blob.
        ledger.ticket_add("types-1", "types", "read", "types", "crates/alpha", [path], [types])
        ledger.ticket_assign("types-1", "main")
        _ = ledger.ticket_base("types-1")
        ledger.ticket_drop("types-1")
        findings = tuple(ledger.hold(types, path, line, source.splitlines()[line - 1], "type", "u32", "Count",
                                     self.blob(types), "types-1") for line in (2, 3))
        ledger.file_held(findings[1].finding_id, self.root / "types.md")
        ledger.record(types, path, base, self.blob(types), "held", ticket="types-1", worker="judge",
                      input_scope=style_ledger.DeclaredInputScope.RULE)
        crate_types = self.rule("crate-types", "tier: C\nfamily: types\ninput_scope: crate\n")
        reads_types = self.rule("reads-types", "tier: C\nfamily: types\ninput_scope: crate\n")
        for rule in (crate_types, reads_types):
            ledger.record(rule, path, base, self.blob(rule), "clean", ticket="outside", worker="judge",
                          input_scope=style_ledger.DeclaredInputScope.RULE,
                          reads=(self.sources[1],) if rule == reads_types else style_ledger.NO_READ_FILES)
        self.write(path, source.replace("old", "fresh").replace("    let", "\n    let"))
        checkpoint = self.commit("Rename and reflow")
        ledger.set_tip(checkpoint, "main")
        ledger.ticket_checkpoint("names-1", checkpoint)
        self.assertTrue(all(isinstance(f.follow, style_ledger.NotFollowed) for f in ledger.named_held([f.finding_id for f in findings])))
        _ = ledger.ticket_merge("names-1")
        moved = tuple(sorted(ledger.named_held([f.finding_id for f in findings]), key=lambda f: f.line))
        self.assertEqual([f.line for f in moved], [3, 4])
        self.assertEqual([f.text for f in moved], ["    let fresh = input;", "    consume(fresh);"])
        self.assertEqual([f.follow for f in moved], [style_ledger.FollowedDeclaration(path, line, checkpoint) for line in (2, 3)])
        self.assertEqual([style_ledger.held_state_name(f.state) for f in moved], ["unfiled", "filed"])
        self.assertEqual([f.ticket for f in moved], ["types-1", "types-1"])
        with closing(sqlite3.connect(ledger.database)) as connection:
            rows = connection.execute('SELECT "commit",ticket,worker,verdict FROM judgments WHERE rule=? ORDER BY id', (types,)).fetchall()
        self.assertEqual(rows, [(base, "types-1", "judge", "held"), (checkpoint, "types-1", "judge", "held")])
        with closing(sqlite3.connect(ledger.database)) as connection:
            carried_rules = connection.execute('SELECT rule FROM judgments WHERE "commit"=? ORDER BY rule', (checkpoint,)).fetchall()
        self.assertEqual(carried_rules, [(rule,) for rule in sorted((types, crate_types, reads_types))])
        self.assertEqual(next(ledger.cells(rule=types)).state, "current")
        # A repeated merge cannot restamp or follow twice.
        _ = ledger.ticket_merge("names-1")
        self.assertEqual(tuple(sorted(ledger.named_held([f.finding_id for f in findings]), key=lambda f: f.line)), moved)

    def test_binding_carry_rejects_a_late_judgment_before_deletion_and_identical_restore(self) -> None:
        source = "fn f() {\n    let old = input;\n    consume(old);\n}\n"
        path = self.sources[0]
        types = self.rule("types-read", "tier: C\nfamily: types\ninput_scope: file\n")
        self.write(path, source)
        original = self.commit("Original binding before deletion")
        ledger = self.ledger()
        ledger.ticket_add("types-1", "types", "read", "types", "crates/alpha", [path], [types])
        ledger.ticket_assign("types-1", "main")
        self.assertEqual(ledger.ticket_base("types-1"), original)
        (self.repo / path).unlink()
        deleting = self.commit("Delete the types cell")
        self.record_removed(ledger, types, path, deleting, ticket="types-1")
        # The rename ticket starts at a byte-identical restoration of the deleted file.
        ledger, _, base = self.binding_ticket(source, ((2, "old", "fresh"),))
        self.assertEqual(self.git("rev-parse", f"{original}:{path}"), self.git("rev-parse", f"{base}:{path}"))
        ledger.record(types, path, original, self.blob(types), "clean", ticket="types-1", worker="late-judge",
                      input_scope=style_ledger.DeclaredInputScope.RULE)
        ledger.set_tip(base, "main")
        self.assertEqual(next(ledger.cells(rule=types)).state, "open")
        self.write(path, source.replace("old", "fresh"))
        checkpoint = self.commit("Proven binding rename after restoration")
        ledger.set_tip(checkpoint, "main")
        ledger.ticket_checkpoint("names-1", checkpoint)
        ticket, = ledger.proposal_tickets()
        self.assertEqual(ledger.binding_carry_forward(ticket, checkpoint), style_ledger.BindingCarryForward())
        self.assertEqual(ledger.ticket_merge("names-1"), ())
        self.assertEqual(self.cell_rows(ledger, path), [(deleting, "removed"), (original, "clean")])
        self.assertEqual(next(ledger.cells(rule=types)).state, "open")

    def test_binding_carry_requires_whole_file_proof_current_rule_and_unchanged_other_inputs(self) -> None:
        source = "fn f() {\n    let old = input;\n    consume(old);\n}\n"
        ledger, _, base = self.binding_ticket(source, ((2, "old", "fresh"),))
        path, other = self.sources[:2]
        carried = self.rule("carried-types", "tier: C\nfamily: types\ninput_scope: file\n")
        reads = self.rule("reads-types", "tier: C\nfamily: types\ninput_scope: crate\n")
        crate = self.rule("crate-types", "tier: C\nfamily: types\ninput_scope: crate\n")
        changed = self.rule("changed-types", "tier: C\nfamily: types\ninput_scope: file\n")
        old_blob = self.rule("older-file-types", "tier: C\nfamily: types\ninput_scope: file\n")
        ledger.record(old_blob, path, self.initial_tip, self.blob(old_blob), "clean", ticket="types", worker="judge",
                      input_scope=style_ledger.DeclaredInputScope.RULE)
        for rule in (carried, crate, changed):
            ledger.record(rule, path, base, self.blob(rule), "clean", ticket="types", worker="judge",
                          input_scope=style_ledger.DeclaredInputScope.RULE)
        ledger.record(reads, path, base, self.blob(reads), "clean", ticket="types", worker="judge",
                      input_scope=style_ledger.DeclaredInputScope.RULE, reads=(other,))
        _ = self.rule("changed-types", "tier: C\nfamily: types\ninput_scope: file\nmode: propose\n")
        self.write(path, source.replace("old", "fresh"))
        self.write(other, "fn another_change() {}\n")
        checkpoint = self.commit("Rename plus another input changed")
        ledger.set_tip(checkpoint, "main")
        ledger.ticket_checkpoint("names-1", checkpoint)
        _ = ledger.ticket_merge("names-1")
        with closing(sqlite3.connect(ledger.database)) as connection:
            restamped = connection.execute('SELECT rule FROM judgments WHERE "commit"=?', (checkpoint,)).fetchall()
        self.assertEqual(restamped, [(carried,)])
        # A proof that applies one rename but cannot explain another token never carries types work.
        # Use the same public planning operation on a fresh checkpoint; it must refuse the whole path.
        self.write(path, source.replace("old", "fresh").replace("input", "different"))
        unproved = self.commit("Unexplained token change")
        ticket = ledger.proposal_tickets()[0]
        self.assertEqual(ledger.binding_carry_forward(ticket, unproved), style_ledger.BindingCarryForward())

    def test_binding_applied_check_groups_the_ticket_path_and_caches_each_commit(self) -> None:
        source = "fn f() {\n    let old = input;\n    let second = old;\n    consume(second);\n}\n"
        ledger, issue, base = self.binding_ticket(source, ((2, "old", "fresh"), (3, "second", "next")))
        findings = ledger.proposal_findings(issue)
        self.write(self.sources[0], source.replace("old", "fresh").replace("second", "next"))
        checkpoint = self.commit("Rename both bindings")
        with mock.patch.object(style_ledger, "binding_rename_check", wraps=binding_rename_check) as proof:
            self.assertEqual([ledger.applied_check(finding, checkpoint) for finding in findings], [AppliedInCode()] * 2)
            self.assertEqual(proof.call_count, 1)
            self.assertEqual(len(cast(tuple[BindingRename, ...], proof.call_args.args[3])), 2)
            self.assertEqual([ledger.applied_check(finding, base) for finding in findings],
                             [RenameNotApplied(self.sources[0])] * 2)
            self.assertEqual(proof.call_count, 2)
        ledger.set_tip(checkpoint, "main")
        ledger.ticket_checkpoint("names-1", checkpoint)
        self.assertEqual(ledger.ticket_merge("names-1"), ())
        self.assertTrue(all(isinstance(f.state, style_ledger.Built) for f in ledger.proposal_findings(issue)))
        self.assertTrue(all(isinstance(f.reading, style_ledger.NotReadByPerson) for f in ledger.proposal_findings(issue)))
        # A fresh ledger must group built findings too.
        fresh = self.ledger()
        self.assertEqual([fresh.applied_check(f, checkpoint) for f in fresh.proposal_findings(issue)], [AppliedInCode()] * 2)

    def test_binding_merge_keeps_unapplied_and_unproved_findings_filed(self) -> None:
        source = "fn f() {\n    let old = input;\n    consume(old);\n    let second = input;\n    consume(second);\n}\n"
        ledger, issue, _ = self.binding_ticket(source, ((2, "old", "fresh"), (4, "second", "next")))
        # One declaration changed without its use; the other rename was never attempted.
        self.write(self.sources[0], source.replace("let old", "let fresh"))
        checkpoint = self.commit("Incomplete rename")
        findings = ledger.proposal_findings(issue)
        by_old = {f.before: ledger.applied_check(f, checkpoint) for f in findings}
        self.assertEqual(by_old, {"old": NoAppliedCheck("rename"), "second": RenameNotApplied(self.sources[0])})
        ledger.set_tip(checkpoint, "main")
        ledger.ticket_checkpoint("names-1", checkpoint)
        _ = ledger.ticket_merge("names-1")
        self.assertTrue(all(isinstance(f.state, style_ledger.Filed) for f in ledger.proposal_findings(issue)))

    def test_binding_check_needs_a_build_ticket_with_a_saved_base(self) -> None:
        source = "fn f() {\n    let old = input;\n    consume(old);\n}\n"
        ledger, issue, _ = self.binding_ticket(source, ((2, "old", "fresh"),))
        self.write(self.sources[0], source.replace("old", "fresh"))
        checkpoint = self.commit("Rename without a saved base")
        finding, = ledger.proposal_findings(issue)
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE tickets SET base='' WHERE id='names-1'")
        self.assertEqual(ledger.applied_check(finding, checkpoint), NoAppliedCheck("rename"))
        ledger.ticket_drop("names-1")
        finding, = ledger.proposal_findings(issue)
        self.assertIsInstance(finding.build_ticket, style_ledger.NoBuildTicket)
        self.assertEqual(ledger.applied_check(finding, checkpoint), NoAppliedCheck("rename"))

    def test_a_file_rename_check_reads_which_file_is_at_the_commit(self) -> None:
        finding, _ = self.file_rename_finding()
        old, new = "crates/alpha/src/ledger/lock.rs", "crates/alpha/src/ledger/mutation_lock.rs"
        unapplied = self.git("rev-parse", "HEAD")
        _ = self.git("mv", old, new)
        applied = self.commit("Rename the lock module")
        self.write(old, "pub struct MutationLock;\n")
        both = self.commit("Copy the lock module back")
        _ = self.git("rm", "-q", old, new)
        neither = self.commit("Remove both files")
        ledger = self.ledger()
        self.assertEqual(ledger.applied_check(finding, unapplied), RenameNotApplied(old))
        self.assertEqual(ledger.applied_check(finding, applied), AppliedInCode())
        self.assertEqual(ledger.applied_check(finding, both), NoAppliedCheck("rename"))
        self.assertEqual(ledger.applied_check(finding, neither), NoAppliedCheck("rename"))
        # A commit git cannot read is an error, never a missing file.
        with self.assertRaises(style_ledger.CheckCouldNotRun):
            _ = ledger.applied_check(finding, "0" * 40)

    def test_a_folder_at_the_commit_reads_as_a_folder(self) -> None:
        self.write("ledger/lock/guard.rs", "pub struct Guard;\n")
        commit = self.commit("Add the guard module")
        self.assertEqual(self.ledger()._file_at(commit, "ledger/lock"), Folder())  # pyright: ignore[reportPrivateUsage]

    def test_decision_merge_builds_a_file_rename_only_when_it_is_applied(self) -> None:
        finding, issue = self.file_rename_finding()
        ledger = self.ledger()
        for ticket in ("names-1", "names-2"):
            ledger.ticket_add(ticket, "names", "proposal", "names", "crates/alpha", [finding.path], [finding.rule],
                              proposal=str(issue), finding_ids=(finding.finding_id,))
            ledger.ticket_assign(ticket, "main")
            if ticket == "names-2":
                _ = self.git("mv", finding.path, "crates/alpha/src/ledger/mutation_lock.rs")
                _ = self.commit("Rename the lock module")
            checkpoint = self.git("rev-parse", "HEAD")
            ledger.set_tip(checkpoint, "main")
            ledger.ticket_checkpoint(ticket, checkpoint)
            output = self.cli("ticket", "merge", "--id", ticket).stdout
            held, = self.ledger().proposal_findings(issue)
            if ticket == "names-1":
                self.assertEqual(output, f"not built {finding.finding_id}: rename not applied in {finding.path}\n")
                self.assertEqual(held.state, style_ledger.Filed(issue))
                self.assertIsInstance(held.build_ticket, style_ledger.NoBuildTicket)
            else:
                self.assertEqual(output, "")
                self.assertIsInstance(held.state, style_ledger.Built)

    def test_decision_merge_leaves_a_type_finding_filed_until_a_person_marks_it_built(self) -> None:
        rule, issue, _ = self.decision_findings()
        ledger = self.ledger()
        finding = ledger.hold(rule, self.sources[0], 3, "value", "type", "u32", "u64", self.blob(rule), "outside")
        ledger.file_held(finding.finding_id, issue)
        ledger.ticket_add("names-1", "names", "proposal", "names", "crates/alpha", [self.sources[0]], [rule],
                          proposal=str(issue), finding_ids=(finding.finding_id,))
        ledger.ticket_assign("names-1", "main")
        ledger.set_tip(self.initial_tip, "main")
        ledger.ticket_checkpoint("names-1", self.initial_tip)
        output = self.cli("ticket", "merge", "--id", "names-1").stdout
        self.assertEqual(output, f"not built {finding.finding_id}: type has no applied check; mark it built with "
                         + f"style_ledger.py held built --repo {ledger.repo} --id {finding.finding_id} after checking\n")
        held = next(item for item in self.ledger().proposal_findings(issue) if item.finding_id == finding.finding_id)
        self.assertEqual(held.state, style_ledger.Filed(issue))
        # The finding keeps its merged ticket, so refill does not take it again.
        self.assertEqual(held.build_ticket, style_ledger.BuildTicket("names-1"))
        _ = self.cli("held", "built", "--id", finding.finding_id)
        held = next(item for item in self.ledger().proposal_findings(issue) if item.finding_id == finding.finding_id)
        self.assertIsInstance(held.state, style_ledger.Built)

    def test_a_rerun_of_ticket_merge_prints_the_findings_that_wait_for_a_person_again(self) -> None:
        rule, issue, _ = self.decision_findings()
        ledger = self.ledger()
        finding = ledger.hold(rule, self.sources[0], 3, "value", "type", "u32", "u64", self.blob(rule), "outside")
        ledger.file_held(finding.finding_id, issue)
        ledger.ticket_add("names-1", "names", "proposal", "names", "crates/alpha", [self.sources[0]], [rule],
                          proposal=str(issue), finding_ids=(finding.finding_id,))
        ledger.ticket_assign("names-1", "main")
        ledger.set_tip(self.initial_tip, "main")
        ledger.ticket_checkpoint("names-1", self.initial_tip)
        line = (f"not built {finding.finding_id}: type has no applied check; mark it built with "
                + f"style_ledger.py held built --repo {ledger.repo} --id {finding.finding_id} after checking\n")
        self.assertEqual(self.cli("ticket", "merge", "--id", "names-1").stdout, line)
        self.assertEqual(self.cli("ticket", "merge", "--id", "names-1").stdout, line)
        _ = self.cli("held", "built", "--id", finding.finding_id)
        self.assertEqual(self.cli("ticket", "merge", "--id", "names-1").stdout, "")

    def held_rows(self, database: Path) -> list[tuple[object, ...]]:
        with closing(sqlite3.connect(database)) as connection:
            return cast(list[tuple[object, ...]], connection.execute("SELECT * FROM held_findings ORDER BY finding_id").fetchall())

    def test_held_replace_reopens_the_same_finding_and_restores_its_waived_cell(self) -> None:
        rule = self.rule("replace")
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        ledger.ticket_add("read-1", "read", "read", "names", "crates/alpha", [self.sources[0]], [rule])
        ledger.ticket_assign("read-1", "main")
        finding_id = self.hold_finding(rule, 1, ticket="read-1")
        ledger.waive_held(finding_id, style_ledger.WaiveByUnit(ledger.named_held((finding_id,))[0].ticket))
        self.record(rule, self.sources[0], self.initial_tip, "waived", ticket="read-1")
        before = ledger.named_held((finding_id,))[0]
        result = self.cli("held", "replace", "--id", finding_id, "--after", "Outcome::Correct")
        self.assertEqual(result.stdout, finding_id + "\n")
        finding, = ledger.named_held((finding_id,))
        self.assertEqual((finding.finding_id, finding.after, finding.state, finding.build_ticket),
                         (finding_id, "Outcome::Correct", style_ledger.Unfiled(), style_ledger.NoBuildTicket()))
        self.assertEqual((finding.rule, finding.path, finding.line, finding.text, finding.kind, finding.before,
                          finding.rule_blob, finding.ticket, finding.waive_undo),
                         (before.rule, before.path, before.line, before.text, before.kind, before.before,
                          before.rule_blob, before.ticket, before.waive_undo))
        self.assertEqual(finding.waive_provenance, style_ledger.WaiveProvenance.UNIT)
        self.assertEqual(ledger.latest_held_judgments(), {(rule, self.sources[0]): self.initial_tip})
        self.record(rule, self.sources[0], self.initial_tip, "held", ticket="read-1")

    def test_held_add_keeps_a_waive_until_explicit_replace(self) -> None:
        rule = self.rule("replace")
        self.set_tip(self.initial_tip)
        finding_id = self.hold_finding(rule, 1)
        ledger = self.ledger()
        ledger.waive_held(finding_id, style_ledger.WaiveByUnit(ledger.named_held((finding_id,))[0].ticket))
        before = self.held_rows(ledger.database)
        self.assertEqual(self.hold_finding(rule, 1), finding_id)
        self.assertEqual(self.held_rows(ledger.database), before)
        _ = self.cli("held", "replace", "--id", finding_id, "--after", "corrected")
        self.assertEqual(ledger.named_held((finding_id,))[0].after, "corrected")

    def test_hold_of_a_different_change_at_a_held_site_gets_its_own_row(self) -> None:
        rule = self.rule("change-ids")
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        blob = self.blob(rule)
        change: tuple[style_ledger.FindingKind, str, str] = ("rename", "old", "new")
        others: tuple[tuple[str, tuple[style_ledger.FindingKind, str, str]], ...] = (
            ("after", ("rename", "old", "other")), ("kind", ("type", "old", "new")),
            ("before", ("rename", "older", "new")))

        def hold(line: int, held: tuple[style_ledger.FindingKind, str, str]) -> style_ledger.HeldFinding:
            return ledger.hold(rule, self.sources[0], line, "value", *held, blob, "outside")

        def finding_id(fields: tuple[str | int, ...]) -> str:
            return hashlib.sha256(json.dumps(fields, separators=(",", ":")).encode()).hexdigest()[:16]

        for line, (name, other) in enumerate(others, 1):
            with self.subTest(differs=name):
                site = (rule, self.sources[0], line, "value", blob)
                # The same change twice is one row at today's id, so a row from before this change keeps its id.
                today = finding_id(site)
                self.assertEqual(hold(line, change).finding_id, today)
                rows = self.held_rows(ledger.database)
                self.assertEqual(hold(line, change).finding_id, today)
                self.assertEqual(self.held_rows(ledger.database), rows)
                second = hold(line, other)
                self.assertEqual((second.finding_id, second.kind, second.before, second.after),
                                 (finding_id((*site, *other)), *other))
                count = len(self.held_rows(ledger.database))
                self.assertEqual((hold(line, change).finding_id, hold(line, other).finding_id),
                                 (today, second.finding_id))
                self.assertEqual(len(self.held_rows(ledger.database)), count)
                kept, = ledger.named_held((today,))
                self.assertEqual((kept.kind, kept.before, kept.after), change)

    def test_hold_returns_its_change_when_another_writer_changes_the_row_after_the_commit(self) -> None:
        rule = self.rule("change-race")
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        held = cast(Callable[[style_ledger.Ledger, str], style_ledger.HeldFinding],
                    getattr(style_ledger.Ledger, "_held"))
        waiting: list[str] = []

        def held_with_another_writer(self_: style_ledger.Ledger, finding_id: str) -> style_ledger.HeldFinding:
            # Another writer changes the row now if no lock stops it, else right after the commit.
            try:
                with closing(sqlite3.connect(ledger.database, timeout=0)) as connection, connection:
                    _ = connection.execute("UPDATE held_findings SET after='other' WHERE finding_id=?",
                                           (finding_id,))
            except sqlite3.OperationalError:
                waiting.append(finding_id)
            return held(self_, finding_id)

        def hold() -> style_ledger.HeldFinding:
            return ledger.hold(rule, self.sources[0], 1, "value", "rename", "old", "new", self.blob(rule), "outside")

        first = hold()
        # The second call returns the existing row; the third inserts a row at the second id.
        for path in ("existing row", "insert"):
            with self.subTest(path=path):
                with mock.patch.object(style_ledger.Ledger, "_held", held_with_another_writer):
                    finding = hold()
                self.assertEqual(finding.after, "new")
                self.assertEqual(len(waiting), 1)
                with closing(sqlite3.connect(ledger.database)) as connection, connection:
                    _ = connection.execute("UPDATE held_findings SET after='other' WHERE finding_id=?",
                                           (waiting.pop(),))
                self.assertEqual(ledger.named_held((finding.finding_id,))[0].after, "other")
        self.assertEqual(len(self.held_rows(ledger.database)), 2)
        self.assertEqual(ledger.named_held((first.finding_id,))[0].after, "other")

    def test_held_replace_refuses_an_unknown_id(self) -> None:
        result = self.cli("held", "replace", "--id", "missing", "--after", "new", success=False)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), "style_ledger: unknown held finding missing")

    def test_decline_keeps_a_nonempty_actor_when_it_overrides_a_unit_waive(self) -> None:
        rule = self.rule("decline-actor")
        self.set_tip(self.initial_tip)
        finding_id = self.hold_finding(rule, 1)
        ledger = self.ledger()
        finding, = ledger.named_held((finding_id,))
        ledger.waive_held(finding_id, style_ledger.WaiveByUnit(finding.ticket))
        _ = ledger.decline_findings((finding_id,), "Keep this name", style_ledger.WaiveByPerson("user"))
        with closing(sqlite3.connect(ledger.database)) as connection:
            self.assertEqual(connection.execute("SELECT waive_provenance,waived_by FROM held_findings WHERE finding_id=?",
                                                (finding_id,)).fetchone(), ("person", "user"))
        self.assertEqual(ledger.named_held((finding_id,))[0].waive_actor, style_ledger.WaiveByPerson("user"))

    def test_decline_rejects_a_unit_and_keeps_person_and_ledger_waives_protected(self) -> None:
        rule = self.rule("decline-protection")
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        for line, actor in enumerate((style_ledger.WaiveByPerson("ana"), style_ledger.WaiveByLedger()), 1):
            with self.subTest(actor=actor):
                finding_id = self.hold_finding(rule, line)
                finding, = ledger.named_held((finding_id,))
                ledger.waive_held(finding_id, actor)
                before = ledger.database.read_bytes()
                invalid_actor = cast(style_ledger.WaiveByPerson | style_ledger.WaiveByLedger,
                                     cast(object, style_ledger.WaiveByUnit(finding.ticket)))
                with self.assertRaisesRegex(style_ledger.LedgerError, "decline requires a person or the ledger"):
                    _ = ledger.decline_findings((finding_id,), "Keep this name", invalid_actor)
                self.assertEqual(ledger.database.read_bytes(), before)
                _ = ledger.decline_findings((finding_id,), "Keep this name", actor)
                self.assertEqual(ledger.named_held((finding_id,))[0].waive_actor, actor)
                before = ledger.database.read_bytes()
                with self.assertRaisesRegex(style_ledger.LedgerError, finding_id):
                    _ = ledger.replace_held(finding_id, "corrected")
                self.assertEqual(ledger.database.read_bytes(), before)

    def test_held_replace_refuses_a_ledger_waive_without_writing(self) -> None:
        rule = self.rule("ledger-waive")
        self.set_tip(self.initial_tip)
        finding_id = self.hold_finding(rule, 1)
        ledger = self.ledger()
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE held_findings SET state='waived',waive_provenance='ledger',waived_by='ledger' WHERE finding_id=?",
                                   (finding_id,))
        for person in ((), ("--person", "ana")):
            with self.subTest(person=person):
                before = ledger.database.read_bytes()
                result = self.cli("held", "replace", "--id", finding_id, "--after", "corrected", *person, success=False)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(finding_id, result.stderr)
                self.assertIn("ledger", result.stderr)
                self.assertEqual(ledger.database.read_bytes(), before)

    def test_held_replace_preserves_person_waives_and_replaces_only_a_unit_waive(self) -> None:
        rule = self.rule("waive-origin")
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        person_id = self.hold_finding(rule, 1)
        issue = self.root / "declined.md"
        ledger.file_held(person_id, issue)
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE held_findings SET person_read_by='ana' WHERE finding_id=?", (person_id,))
        self.record(rule, self.sources[0], self.initial_tip, "held")
        _ = ledger.decline_findings((person_id,), "Keep the old name", style_ledger.WaiveByPerson("user"))
        # A declined decision can also waive a new, unfiled finding before any judgment exists.
        unfiled_id = self.hold_finding(rule, 2)
        _ = ledger.decline_findings((unfiled_id,), "Keep this name too", style_ledger.WaiveByPerson("user"))
        for finding_id in (person_id, unfiled_id):
            with self.subTest(finding_id=finding_id):
                before = ledger.database.read_bytes()
                result = self.cli("held", "replace", "--id", finding_id, "--after", "corrected", success=False)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(finding_id, result.stderr)
                self.assertIn("person", result.stderr)
                self.assertEqual(ledger.database.read_bytes(), before)
        unit_id = self.hold_finding(rule, 3)
        ledger.waive_held(unit_id, style_ledger.WaiveByUnit("external-1"))
        result = self.cli("held", "replace", "--id", unit_id, "--after", "corrected")
        self.assertEqual(result.stdout, unit_id + "\n")
        unit, = ledger.named_held((unit_id,))
        self.assertEqual((unit.after, unit.state), ("corrected", style_ledger.Unfiled()))

    def test_held_waive_records_the_caller_and_replace_protects_a_person(self) -> None:
        rule = self.rule("explicit-waive")
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        person_id = self.hold_finding(rule, 1)
        _ = self.cli("held", "waive", "--id", person_id, "--person", "ana")
        person, = ledger.named_held((person_id,))
        self.assertEqual(person.waive_actor, style_ledger.WaiveByPerson("ana"))
        for flags in ((), ("--person", "ana")):
            with self.subTest(flags=flags):
                before = ledger.database.read_bytes()
                result = self.cli("held", "replace", "--id", person_id, "--after", "corrected", *flags, success=False)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(person_id, result.stderr)
                self.assertIn("person", result.stderr)
                self.assertEqual(ledger.database.read_bytes(), before)
        unit_id = self.hold_finding(rule, 2)
        # The caller supplies the ticket even when the finding has already been filed.
        ledger.file_held(unit_id, self.root / "unit-waive.md")
        _ = self.cli("held", "waive", "--id", unit_id, "--ticket", "external-1")
        unit, = ledger.named_held((unit_id,))
        self.assertEqual(unit.waive_actor, style_ledger.WaiveByUnit("external-1"))
        _ = self.cli("held", "replace", "--id", unit_id, "--after", "corrected")
        replaced, = ledger.named_held((unit_id,))
        self.assertEqual((replaced.finding_id, replaced.after, replaced.state),
                         (unit_id, "corrected", style_ledger.Unfiled()))

    def test_held_waive_requires_exactly_one_nonblank_actor(self) -> None:
        rule = self.rule("waive-actor")
        finding_id = self.hold_finding(rule, 1)
        ledger = self.ledger()
        for flags in ((), ("--ticket", "outside", "--person", "ana"),
                      ("--ticket", " "), ("--person", " "), ("--ticket", "another-ticket")):
            with self.subTest(flags=flags):
                before = ledger.database.read_bytes()
                result = self.cli("held", "waive", "--id", finding_id, *flags, success=False)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                if not flags:
                    self.assertIn("one of the arguments --ticket --person is required", result.stderr)
                elif "--ticket" in flags and "--person" in flags:
                    self.assertIn("not allowed with argument --ticket", result.stderr)
                elif flags[-1] == "another-ticket":
                    self.assertIn("did not judge this finding", result.stderr)
                else:
                    self.assertIn("requires a", result.stderr)
                self.assertEqual(ledger.database.read_bytes(), before)

    def test_a_persons_explicit_waive_protects_an_existing_unit_waive(self) -> None:
        rule = self.rule("repeat-waive")
        finding_id = self.hold_finding(rule, 1)
        ledger = self.ledger()
        ledger.waive_held(finding_id, style_ledger.WaiveByUnit("external-1"))
        ledger.waive_held(finding_id, style_ledger.WaiveByPerson("ana"))
        before = ledger.database.read_bytes()
        ledger.waive_held(finding_id, style_ledger.WaiveByUnit("external-1"))
        self.assertEqual(ledger.database.read_bytes(), before)
        finding, = ledger.named_held((finding_id,))
        self.assertEqual(finding.waive_actor, style_ledger.WaiveByPerson("ana"))
        with self.assertRaisesRegex(style_ledger.LedgerError, finding_id + ".*person"):
            _ = ledger.replace_held(finding_id, "corrected")
        self.assertEqual(ledger.database.read_bytes(), before)

    def test_held_replace_requires_and_records_a_person_for_an_unrecorded_waive(self) -> None:
        rule = self.rule("legacy-replace")
        self.set_tip(self.initial_tip)
        finding_id = self.hold_finding(rule, 1)
        ledger = self.ledger()
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE held_findings SET state='waived',waive_provenance='unrecorded' WHERE finding_id=?",
                                   (finding_id,))
        self.record(rule, self.sources[0], self.initial_tip, "waived")
        for flags in ((), ("--person", " ")):
            with self.subTest(flags=flags):
                before = ledger.database.read_bytes()
                result = self.cli("held", "replace", "--id", finding_id, "--after", "corrected", *flags, success=False)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(ledger.database.read_bytes(), before)
        result = self.cli("held", "replace", "--id", finding_id, "--after", "corrected", "--person", "ana")
        self.assertEqual(result.stdout, finding_id + "\n")
        finding, = ledger.named_held((finding_id,))
        self.assertEqual((finding.finding_id, finding.after, finding.state, finding.waive_undo),
                         (finding_id, "corrected", style_ledger.Unfiled(), style_ledger.WaiveUndoneBy("ana")))
        self.assertEqual(ledger.latest_held_judgments(), {(rule, self.sources[0]): self.initial_tip})

    def test_held_replace_refuses_each_non_waived_state_without_writes(self) -> None:
        _, _, ids = self.decision_findings()
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        for state in ("unfiled", "filed", "built", "withdrawn"):
            with self.subTest(state=state):
                with closing(sqlite3.connect(ledger.database)) as connection, connection:
                    _ = connection.execute("UPDATE held_findings SET state=? WHERE finding_id=?", (state, ids[0]))
                before = self.held_rows(ledger.database)
                result = self.cli("held", "replace", "--id", ids[0], "--after", "new", success=False)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stderr.strip(), f"style_ledger: {ids[0]}: cannot replace a {state} finding")
                self.assertEqual(self.held_rows(ledger.database), before)

    def test_held_replace_refuses_legacy_waives_without_unit_provenance(self) -> None:
        rule = self.rule("legacy-waive")
        self.set_tip(self.initial_tip)
        finding_id = self.hold_finding(rule, 1)
        ledger = self.ledger()
        ledger.waive_held(finding_id, style_ledger.WaiveByUnit(ledger.named_held((finding_id,))[0].ticket))
        database = ledger.database
        ledger.close()
        with closing(sqlite3.connect(database)) as connection, connection:
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN waive_provenance")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN waived_by")
            _ = connection.execute("DROP TABLE ticket_widened_paths")
            _ = connection.execute("ALTER TABLE tickets DROP COLUMN reservation_session_dir")
            _ = connection.execute("ALTER TABLE tickets DROP COLUMN reservation_phase")
            _ = connection.execute("ALTER TABLE processed_tips DROP COLUMN reason")
            _ = connection.execute("DROP TABLE refill_results")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN built_at")
            _ = connection.execute("UPDATE schema_version SET version=18")
        migrated = self.ledger()
        finding, = migrated.named_held((finding_id,))
        self.assertEqual(finding.waive_provenance, style_ledger.WaiveProvenance.UNRECORDED)
        before = database.read_bytes()
        with self.assertRaisesRegex(style_ledger.LedgerError, finding_id + ".*unrecorded"):
            _ = migrated.replace_held(finding_id, "corrected")
        self.assertEqual(database.read_bytes(), before)
        migrated.waive_held(finding_id, style_ledger.WaiveByUnit("external-1"))
        self.assertEqual(migrated.named_held((finding_id,))[0].waive_provenance, style_ledger.WaiveProvenance.UNRECORDED)

    def test_held_replace_does_not_create_or_overwrite_other_cell_verdicts(self) -> None:
        rule = self.rule("replace")
        self.set_tip(self.initial_tip)
        finding_id = self.hold_finding(rule, 1)
        ledger = self.ledger()
        for verdict in ("missing", "clean", "exception"):
            with self.subTest(verdict=verdict):
                if verdict != "missing":
                    self.record(rule, self.sources[0], self.initial_tip, verdict)
                ledger.waive_held(finding_id, style_ledger.WaiveByUnit(ledger.named_held((finding_id,))[0].ticket))
                _ = self.cli("held", "replace", "--id", finding_id, "--after", "Outcome::Correct")
                self.assertEqual(ledger.latest_held_judgments(), {})
                self.assertEqual(self.states(rule)[self.sources[0]], "open" if verdict == "missing" else "current")

    def test_held_add_of_a_built_row_names_reopen_only_when_its_rename_is_not_in_the_code(self) -> None:
        rule, issue, ids = self.decision_findings()
        self.write(self.sources[0], "pub enum Outcome { New }\n")
        self.write(self.sources[1], "pub enum Outcome { Old }\n")
        tip = self.commit("Rename one of two")
        ledger = self.ledger()
        ledger.set_tip(tip, "main")
        # Merges before the applied check marked each finding built without reading the code.
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE held_findings SET state='built'")
        before = self.held_rows(ledger.database)
        hint = f"built, but not in the code at {tip[:7]}: run style_proposals.py reopen --all (issue {issue})\n"
        for index, (finding_id, notice) in enumerate(((ids[0], ""), (ids[1], hint))):
            result = self.cli(
                "held", "add", "--rule", rule, "--path", self.sources[index], "--line", str(index + 1),
                "--text", "required change", "--kind", "rename", "--before", "Outcome::Old",
                "--after", "Outcome::New", "--rule-blob", self.blob(rule), "--ticket", "outside",
            )
            self.assertEqual((result.stdout, result.stderr), (finding_id + "\n", notice))
        self.assertEqual(self.held_rows(ledger.database), before)
        self.assertEqual([finding.state for finding in self.ledger().proposal_findings(issue)],
                         [style_ledger.Built(issue), style_ledger.Built(issue)])

    def test_held_add_of_a_built_row_that_a_person_read_names_no_reopen(self) -> None:
        rule, issue, ids = self.decision_findings()
        self.write(self.sources[1], "pub enum Outcome { Old }\n")
        tip = self.commit("Keep Old")
        ledger = self.ledger()
        ledger.set_tip(tip, "main")
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE held_findings SET state='built'")
        ledger.record_person_applied({ids[1]: style_ledger.ReadByPerson("nate")})
        result = self.cli(
            "held", "add", "--rule", rule, "--path", self.sources[1], "--line", "2", "--text", "required change",
            "--kind", "rename", "--before", "Outcome::Old", "--after", "Outcome::New", "--rule-blob", self.blob(rule),
            "--ticket", "outside",
        )
        self.assertEqual((result.stdout, result.stderr), (ids[1] + "\n", ""))
        self.assertEqual(self.ledger().proposal_findings(issue)[1].reading, style_ledger.ReadByPerson("nate"))

    def test_a_ticket_merge_that_finds_the_rename_applied_clears_a_person_reading(self) -> None:
        rule, issue, ids = self.decision_findings()
        ledger = self.ledger()
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE held_findings SET state='built' WHERE finding_id=?", (ids[0],))
        ledger.return_to_filed({ids[0]: style_ledger.ReadByPerson("nate")})
        self.assertEqual(ledger.proposal_findings(issue)[0].reading, style_ledger.ReadByPerson("nate"))
        ledger.ticket_add("names-1", "names", "proposal", "names", "crates/alpha", [self.sources[0]], [rule],
                          proposal=str(issue), finding_ids=ids[:1])
        ledger.ticket_assign("names-1", "main")
        self.write(self.sources[0], "pub enum Outcome { New }\n")
        checkpoint = self.commit("Rename Old to New")
        ledger.set_tip(checkpoint, "main")
        ledger.ticket_checkpoint("names-1", checkpoint)
        self.assertEqual(ledger.ticket_merge("names-1"), ())
        held = ledger.proposal_findings(issue)[0]
        self.assertEqual((held.finding_id, held.state, held.reading),
                         (ids[0], style_ledger.Built(issue), style_ledger.NotReadByPerson()))

    def test_held_built_refuses_a_finding_with_no_merged_build_ticket(self) -> None:
        _, issue, ids = self.decision_findings()
        result = self.cli("held", "built", "--id", ids[0], success=False)
        self.assertEqual((result.returncode, result.stderr.strip()), (2, f"style_ledger: {ids[0]}: no merged build ticket"))
        self.assertEqual(self.ledger().proposal_findings(issue)[0].state, style_ledger.Filed(issue))

    def test_held_built_refuses_a_rename_not_applied_at_its_build_ticket_checkpoint(self) -> None:
        rule, issue, ids = self.decision_findings()
        ledger = self.ledger()
        ledger.ticket_add("names-1", "names", "proposal", "names", "crates/alpha", [self.sources[1]], [rule],
                          proposal=str(issue), finding_ids=ids[1:])
        ledger.ticket_assign("names-1", "main")
        self.write(self.sources[1], "pub enum Outcome { Old }\n")
        checkpoint = self.commit("Keep Old")
        ledger.set_tip(checkpoint, "main")
        ledger.ticket_checkpoint("names-1", checkpoint)
        _ = ledger.ticket_merge("names-1")
        with self.assertRaisesRegex(style_ledger.LedgerError, re.escape(f"{ids[1]}: no merged build ticket")):
            ledger.mark_built(ids[1])
        # A row that names a merged ticket at whose checkpoint the rename is not applied.
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE held_findings SET build_ticket='names-1' WHERE finding_id=?", (ids[1],))
        with self.assertRaisesRegex(style_ledger.LedgerError,
                                    re.escape(f"{ids[1]}: rename not applied in {self.sources[1]} at names-1 checkpoint")):
            ledger.mark_built(ids[1])
        self.assertEqual(ledger.proposal_findings(issue)[1].state, style_ledger.Filed(issue))

    def test_held_built_at_a_merge_refuses_a_commit_off_the_synced_branch(self) -> None:
        _, issue, ids = self.decision_findings()
        self.write(self.sources[0], "pub enum Outcome { New }\n")
        tip = self.commit("Rename Old to New")
        self.ledger().set_tip(self.initial_tip, "main")
        for merge, error in ((tip, f"{tip} is not on the synced branch at {self.initial_tip[:7]}"), ("missing", "missing: no such commit")):
            with self.subTest(merge=merge):
                result = self.cli("held", "built", "--id", ids[0], "--commit", merge, success=False)
                self.assertEqual((result.returncode, result.stderr.strip()), (2, f"style_ledger: {error}"))
        self.assertEqual(self.ledger().proposal_findings(issue)[0].state, style_ledger.Filed(issue))

    def test_held_built_at_a_merge_refuses_a_merge_older_than_the_commit_the_finding_was_judged_at(self) -> None:
        rule, _, ids = self.decision_findings()
        self.write(self.sources[2], "pub fn judged() {}\n")
        judged = self.commit("Judge the findings")
        ledger = self.ledger()
        ledger.set_tip(judged, "main")
        ledger.ticket_add("names-1", "names", "read", "names", "crates/alpha", [self.sources[0]], [rule])
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE tickets SET checkpoint=? WHERE id='names-1'", (judged,))
            _ = connection.execute("UPDATE held_findings SET ticket='names-1' WHERE finding_id=?", (ids[0],))
        before = self.held_rows(ledger.database)
        result = self.cli("held", "built", "--id", ids[0], "--commit", self.initial_tip, "--by", "nate", success=False)
        self.assertEqual((result.returncode, result.stderr.strip()),
                         (2, f"style_ledger: {ids[0]}: {self.initial_tip} is older than {judged[:7]}, where the finding was judged"))
        self.assertEqual(self.held_rows(ledger.database), before)

    def test_held_built_at_a_merge_refuses_every_id_when_one_is_refused(self) -> None:
        rule, issue, ids = self.decision_findings()
        self.write(self.sources[0], "pub enum Outcome { New }\n")
        self.write(self.sources[1], "pub enum Outcome { Old }\n")
        tip = self.commit("Rename one of two")
        ledger = self.ledger()
        ledger.set_tip(tip, "main")
        third = ledger.hold(rule, self.sources[2], 1, "required change", "type", "u32", "u64", self.blob(rule), "outside")
        ledger.file_held(third.finding_id, issue)
        ledger.ticket_add("names-1", "names", "proposal", "names", "crates/beta", [self.sources[2]], [rule],
                          proposal=str(issue), finding_ids=(third.finding_id,))
        before = self.held_rows(ledger.database)
        for names, error in (((ids[0], ids[1]), f"{ids[1]}: rename not applied in {self.sources[1]} at {tip}"),
                             ((ids[0], "zzzz"), "no finding zzzz"),
                             ((ids[0], third.finding_id), f"{third.finding_id}: build ticket names-1 still holds the finding")):
            with self.subTest(names=names):
                result = self.cli("held", "built", *(part for name in names for part in ("--id", name)), "--commit", tip,
                                  "--by", "nate", success=False)
                self.assertEqual((result.returncode, result.stderr.strip()), (2, f"style_ledger: {error}"))
                self.assertEqual(self.held_rows(ledger.database), before)

    def test_held_built_at_a_merge_marks_an_applied_rename_built_and_clears_a_reading(self) -> None:
        _, issue, ids = self.decision_findings()
        self.write(self.sources[0], "pub enum Outcome { New }\n")
        tip = self.commit("Rename Old to New")
        ledger = self.ledger()
        ledger.set_tip(tip, "main")
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE held_findings SET state='built' WHERE finding_id=?", (ids[0],))
        ledger.return_to_filed({ids[0]: style_ledger.ReadByPerson("nate")})
        result = self.cli("held", "built", "--id", ids[0][:8], "--commit", tip, "--by", "ana")
        self.assertEqual(result.stdout, f"built {ids[0]} at {tip[:7]}\n")
        held = {finding.finding_id: (finding.state, finding.reading) for finding in self.ledger().proposal_findings(issue)}
        self.assertEqual(held, {ids[0]: (style_ledger.Built(issue), style_ledger.NotReadByPerson()),
                                ids[1]: (style_ledger.Filed(issue), style_ledger.NotReadByPerson())})

    def test_held_built_at_a_merge_needs_a_person_for_a_type_finding_and_records_who_read_it(self) -> None:
        rule, issue, _ = self.decision_findings()
        ledger = self.ledger()
        ledger.set_tip(self.initial_tip, "main")
        finding = ledger.hold(rule, self.sources[2], 1, "value", "type", "u32", "u64", self.blob(rule), "outside")
        ledger.file_held(finding.finding_id, issue)
        refusal = self.cli("held", "built", "--id", finding.finding_id, "--commit", "main", success=False)
        self.assertEqual(refusal.stderr.strip(), f"style_ledger: {finding.finding_id}: type has no applied check at main; "
                         + "pass --by after reading the code")
        for arguments, error in ((("--commit", "main", "--by", "  "), "held built --by requires a name"),
                                 (("--by", "nate"), "held built --by requires --commit"),
                                 (("--id", finding.finding_id), "held built takes more than one --id only with --commit"),
                                 (("--id", " ", "--commit", "main"), "held built --id requires a finding id")):
            with self.subTest(arguments=arguments):
                result = self.cli("held", "built", "--id", finding.finding_id, *arguments, success=False)
                self.assertEqual(result.returncode, 2)
                self.assertIn(error, result.stderr)
        self.assertEqual(next(item for item in self.ledger().proposal_findings(issue) if item.finding_id == finding.finding_id).state, style_ledger.Filed(issue))
        result = self.cli("held", "built", "--id", finding.finding_id, "--commit", "main", "--by", " nate ")
        self.assertEqual(result.stdout, f"built {finding.finding_id} at {self.initial_tip[:7]}, read by nate\n")
        held = next(item for item in self.ledger().proposal_findings(issue) if item.finding_id == finding.finding_id)
        self.assertEqual((held.state, held.reading), (style_ledger.Built(issue), style_ledger.ReadByPerson("nate")))

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

    def test_held_built_at_a_merge_refuses_a_merge_without_the_build_ticket_checkpoint(self) -> None:
        ledger, finding_id, _, checkpoint = self.checkpointed_type_findings()
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE tickets SET finding_ids='[]' WHERE id='names-1'")
        before = self.held_rows(ledger.database)
        result = self.cli("held", "built", "--id", finding_id, "--commit", self.initial_tip, "--by", "nate",
                          success=False)
        self.assertEqual((result.returncode, result.stderr.strip()),
                         (2, f"style_ledger: {finding_id}: {self.initial_tip} does not contain names-1's checkpoint "
                          + checkpoint[:7]))
        self.assertEqual(self.held_rows(ledger.database), before)
        result = self.cli("held", "built", "--id", finding_id, "--commit", checkpoint, "--by", "nate")
        self.assertEqual(result.stdout, f"built {finding_id} at {checkpoint[:7]}, read by nate\n")

    def test_held_built_at_a_merge_refuses_a_merge_without_a_listing_ticket_checkpoint(self) -> None:
        ledger, finding_id, _, checkpoint = self.checkpointed_type_findings()
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE held_findings SET build_ticket='' WHERE finding_id=?", (finding_id,))
        before = self.held_rows(ledger.database)
        result = self.cli("held", "built", "--id", finding_id, "--commit", self.initial_tip, "--by", "nate",
                          success=False)
        self.assertEqual((result.returncode, result.stderr.strip()),
                         (2, f"style_ledger: {finding_id}: {self.initial_tip} does not contain names-1's checkpoint "
                          + checkpoint[:7]))
        self.assertEqual(self.held_rows(ledger.database), before)

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

    def test_held_built_at_a_merge_rereads_the_linked_tickets_under_the_write_lock(self) -> None:
        ledger, finding_id, _, checkpoint = self.checkpointed_type_findings()
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE tickets SET state='assigned',checkpoint='' WHERE id='names-1'")
            _ = connection.execute("UPDATE held_findings SET build_ticket='' WHERE finding_id=?", (finding_id,))
        # Another writer checkpoints and merges the linked ticket after the checkpoint check read it.
        self.assert_refused_after_a_write_during_the_check(
            ledger, finding_id, "UPDATE tickets SET state='merged',checkpoint=? WHERE id='names-1'", (checkpoint,),
            f"{finding_id}: {self.initial_tip} does not contain names-1's checkpoint {checkpoint[:7]}",
        )

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

    def test_held_built_at_a_merge_rereads_the_judging_ticket_under_the_write_lock(self) -> None:
        ledger, finding_id, judged = self.judged_type_finding()
        # The judging ticket checkpoints at a later commit after the judged-commit check read its start.
        self.assert_refused_after_a_write_during_the_check(
            ledger, finding_id, "UPDATE tickets SET checkpoint=? WHERE id='names-1'", (judged,),
            f"{finding_id}: {self.initial_tip} is older than {judged[:7]}, where the finding was judged",
        )

    def test_held_built_at_a_merge_decides_from_the_rows_it_read(self) -> None:
        ledger, finding_id, judged = self.judged_type_finding()
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE tickets SET checkpoint=? WHERE id='names-1'", (judged,))
        before = self.held_rows(ledger.database)
        judged_commit = style_ledger.Ledger.judged_commit

        def judged_while_another_writer_flips(self_: style_ledger.Ledger, finding: style_ledger.HeldFinding,
                                              ) -> style_ledger.JudgedCommit | style_ledger.NoJudgedCommit:
            # Another writer clears the checkpoint for this one read and puts it back before the write lock.
            with closing(sqlite3.connect(ledger.database)) as connection, connection:
                _ = connection.execute("UPDATE tickets SET checkpoint='' WHERE id='names-1'")
            try:
                return judged_commit(self_, finding)
            finally:
                with closing(sqlite3.connect(ledger.database)) as connection, connection:
                    _ = connection.execute("UPDATE tickets SET checkpoint=? WHERE id='names-1'", (judged,))

        with (mock.patch.object(style_ledger.Ledger, "judged_commit", judged_while_another_writer_flips),
              self.assertRaisesRegex(style_ledger.LedgerError, re.escape(
                  f"{finding_id}: {self.initial_tip} is older than {judged[:7]}, where the finding was judged"))):
            _ = ledger.mark_built_at_merge((finding_id,), self.initial_tip, style_ledger.ReadByPerson("nate"))
        self.assertEqual(self.held_rows(ledger.database), before)

    def test_held_built_at_a_merge_ignores_the_checkpoint_of_a_dropped_ticket(self) -> None:
        ledger, finding_id, _, _ = self.checkpointed_type_findings()
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE tickets SET state='dropped' WHERE id='names-1'")
        result = self.cli("held", "built", "--id", finding_id, "--commit", self.initial_tip, "--by", "nate")
        self.assertEqual(result.stdout, f"built {finding_id} at {self.initial_tip[:7]}, read by nate\n")

    def test_held_built_at_a_merge_writes_no_id_when_one_lacks_a_checkpoint(self) -> None:
        ledger, finding_id, other_id, checkpoint = self.checkpointed_type_findings()
        before = self.held_rows(ledger.database)
        result = self.cli("held", "built", "--id", finding_id, "--id", other_id, "--commit", self.initial_tip,
                          "--by", "nate", success=False)
        self.assertEqual((result.returncode, result.stderr.strip()),
                         (2, f"style_ledger: {finding_id}: {self.initial_tip} does not contain names-1's checkpoint "
                          + checkpoint[:7]))
        self.assertEqual(self.held_rows(ledger.database), before)

    def test_unwaive_moves_waived_findings_to_unfiled_with_no_move_and_the_held_list_shows_who(self) -> None:
        _, issue, ids = self.decision_findings()
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        ledger.move_issue_findings(issue, {ids[0]: self.root / "moved.md"})
        for finding_id in ids:
            ledger.waive_held(finding_id, style_ledger.WaiveByPerson("nate"))
        person = style_ledger.WaiveUndoneBy("nate")
        self.assertEqual(ledger.unwaive_held((ids[0][:8], ids[1], ids[1][:6]), person), tuple(sorted(ids)))
        unfiled = ledger.held_findings(style_ledger.HeldFindingsInState("unfiled"))
        self.assertEqual({(finding.finding_id, finding.state, finding.waive_undo) for finding in unfiled},
                         {(finding_id, style_ledger.Unfiled(), person) for finding_id in ids})
        self.assertEqual(ledger.proposal_findings(issue), ())
        # A resumed migrate does not take the finding again with an empty decision.
        self.assertEqual(ledger.moved_findings(issue), {})
        listed = cast(list[dict[str, object]], json.loads(self.cli("held", "list", "--json").stdout))
        self.assertEqual({(finding["finding_id"], finding["state"], finding["unwaived_by"]) for finding in listed},
                         {(finding_id, "unfiled", "nate") for finding_id in ids})
        lines = {line.split("\t")[0]: line.split("\t")[3:] for line in self.cli("held", "list").stdout.splitlines()}
        self.assertEqual(lines, {finding_id: ["unfiled", "unwaived by nate"] for finding_id in ids})
        # A new waive keeps the record of who undid the old one.
        ledger.waive_held(ids[0], style_ledger.WaiveByPerson("nate"))
        self.assertEqual(ledger.held_findings(style_ledger.HeldFindingsInState("waived"))[0].waive_undo, person)

    def test_unwaive_holds_the_cell_that_a_decline_waived_so_a_second_decline_waives_it_again(self) -> None:
        rule, _, ids = self.decision_findings()
        self.set_tip(self.initial_tip)
        for path in self.sources[:2]:
            self.record(rule, path, self.initial_tip, "held")
        ledger = self.ledger()
        self.assertIsInstance(ledger.decline_findings(ids[:1], "Keep the name", style_ledger.WaiveByPerson("user"))[ids[0]], style_ledger.CellWaived)
        self.assertEqual(next(row for row in ledger.rule_counts(style_ledger.WholeRepository()) if row.rule_id == rule).held, 1)
        _ = ledger.unwaive_held(ids[:1], style_ledger.WaiveUndoneBy("nate"))
        self.assertEqual(next(row for row in ledger.rule_counts(style_ledger.WholeRepository()) if row.rule_id == rule).held, 2)
        self.assertIn((rule, self.sources[0]), ledger.latest_held_judgments())
        self.assertIsInstance(ledger.decline_findings(ids[:1], "Keep the name", style_ledger.WaiveByPerson("user"))[ids[0]], style_ledger.CellWaived)
        # The new waive keeps the record of who undid the old one.
        waived, = ledger.held_findings(style_ledger.HeldFindingsInState("waived"))
        self.assertEqual((waived.finding_id, waived.waive_undo), (ids[0], style_ledger.WaiveUndoneBy("nate")))

    def test_unwaive_refuses_a_finding_judged_under_an_older_rule_text(self) -> None:
        rule, _, ids = self.decision_findings()
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        ledger.waive_held(ids[0], style_ledger.WaiveByPerson("nate"))
        definition = self.rule_dir / "decision.md"
        _ = definition.write_text(definition.read_text() + "New rule text.\n")
        self.assertNotEqual(self.blob(rule), ledger.held_findings(style_ledger.HeldFindingsInState("waived"))[0].rule_blob)
        before = self.held_rows(ledger.database)
        with self.assertRaisesRegex(style_ledger.LedgerError, re.escape(f"{ids[0]}: rule changed since judged")):
            _ = ledger.unwaive_held(ids[:1], style_ledger.WaiveUndoneBy("nate"))
        self.assertEqual(self.held_rows(ledger.database), before)

    def test_unwaive_refuses_every_id_when_one_is_refused(self) -> None:
        rule, issue, ids = self.decision_findings()
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        ledger.waive_held(ids[0], style_ledger.WaiveByPerson("nate"))
        built = ledger.hold(rule, self.sources[2], 1, "required change", "type", "u32", "u64", self.blob(rule), "outside")
        ledger.file_held(built.finding_id, issue)
        ledger.ticket_add("names-1", "names", "proposal", "names", "crates/beta", [self.sources[2]], [rule],
                          proposal=str(issue), finding_ids=(built.finding_id,))
        ledger.waive_held(built.finding_id, style_ledger.WaiveByPerson("nate"))
        ledger.ticket_add("read-1", "read", "read", "names", "crates/beta", [self.sources[3]], [rule])
        ledger.ticket_assign("read-1", "main")
        judged = ledger.hold(rule, self.sources[3], 1, "required change", "type", "u32", "u64", self.blob(rule), "read-1")
        ledger.waive_held(judged.finding_id, style_ledger.WaiveByPerson("nate"))
        twin = ids[1][:8] + "0" * 8
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("INSERT INTO held_findings SELECT ?,rule,path,line,text,kind,before,after,rule_blob,ticket,"
                                   + "state,proposal,created_at,updated_at,build_ticket,moved_from,withdrawn_by,person_read_by,"
                                   + "duplicate_of,unwaived_by,followed_from,followed_from_line,followed_at,declaration_gone_at,waive_provenance,waived_by,built_at FROM held_findings "
                                   + "WHERE finding_id=?", (twin, ids[1]))
        before = self.held_rows(ledger.database)
        for name, error in (
            (ids[1], f"{ids[1]}: cannot unwaive a filed finding"),
            ("zzzz", "no finding zzzz"),
            (ids[1][:8], f"{ids[1][:8]} matches 2 findings"),
            (built.finding_id, f"{built.finding_id}: build ticket names-1 still holds the finding"),
            (judged.finding_id, f"{judged.finding_id}: assigned ticket read-1 still holds the finding"),
        ):
            with self.subTest(name=name), self.assertRaisesRegex(style_ledger.LedgerError, re.escape(error)):
                _ = ledger.unwaive_held((ids[0], name), style_ledger.WaiveUndoneBy("nate"))
            self.assertEqual(self.held_rows(ledger.database), before)

    def test_an_unwaived_finding_with_a_dropped_build_ticket_files_into_a_proposal_again(self) -> None:
        rule, issue, ids = self.decision_findings()
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        ledger.ticket_add("names-1", "names", "proposal", "names", "crates/alpha", [self.sources[0]], [rule],
                          proposal=str(issue), finding_ids=ids[:1])
        ledger.waive_held(ids[0], style_ledger.WaiveByPerson("nate"))
        ledger.ticket_drop("names-1")
        with self.assertRaisesRegex(style_ledger.LedgerError, re.escape(f"{ids[0]}: cannot file finding in state waived")):
            ledger.file_held(ids[0], issue)
        self.assertEqual(ledger.unwaive_held(ids[:1], style_ledger.WaiveUndoneBy("nate")), ids[:1])
        unfiled, = ledger.held_findings(style_ledger.HeldFindingsInState("unfiled"))
        self.assertEqual((unfiled.finding_id, unfiled.build_ticket), (ids[0], style_ledger.NoBuildTicket()))
        ledger.file_held(ids[0], issue)
        ledger.ticket_add("names-2", "names", "proposal", "names", "crates/alpha", [self.sources[0]], [rule],
                          proposal=str(issue), finding_ids=ids[:1])
        filed = next(finding for finding in ledger.proposal_findings(issue) if finding.finding_id == ids[0])
        self.assertEqual((filed.state, filed.build_ticket, filed.waive_undo),
                         (style_ledger.Filed(issue), style_ledger.BuildTicket("names-2"), style_ledger.WaiveUndoneBy("nate")))

    def test_decision_drop_releases_findings_for_another_ticket(self) -> None:
        rule, issue, ids = self.decision_findings()
        ledger = self.ledger()
        ledger.ticket_add("names-1", "names", "proposal", "names", "crates/alpha", [self.sources[0]], [rule],
                          proposal=str(issue), finding_ids=ids[:1])
        ledger.ticket_assign("names-1", "main")
        ledger.ticket_drop("names-1")
        finding = ledger.proposal_findings(issue)[0]
        self.assertEqual(finding.state, style_ledger.Filed(issue))
        self.assertIsInstance(finding.build_ticket, style_ledger.NoBuildTicket)
        ledger.ticket_add("names-2", "names", "proposal", "names", "crates/alpha", [self.sources[0]], [rule],
                          proposal=str(issue), finding_ids=ids[:1])
        self.assertEqual(ledger.proposal_findings(issue)[0].build_ticket, style_ledger.BuildTicket("names-2"))
        self.assertEqual(ledger.ticket_origin("names-1"), style_ledger.ApprovedProposal(issue, "names", ids[:1]))

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

    def test_widen_records_each_path_with_its_reason_name_and_time(self) -> None:
        ledger, issue = self.widen_ticket()
        extra, more = "crates/alpha/src/extra.rs", "crates/alpha/src/more.rs"
        result, output = self.widen(extra, more)
        self.assertEqual(result, 0, output)
        ledger.ticket_assign("names-1", "main")
        opened, = (cast(style_ledger.TicketRecord, json.loads(line))
                   for line in self.cli("ticket", "open", "--unit", "names").stdout.splitlines())
        widened = opened["widened"]
        self.assertEqual([(item["path"], item["reason"], item["widened_by"]) for item in widened],
                         [(path, "the move needs a new file", "showrunner") for path in (extra, more)])
        at = widened[0]["widened_at"]
        self.assertEqual(datetime.fromisoformat(at).utcoffset(), timezone.utc.utcoffset(None))
        self.assertEqual(output, "".join(f"widened: {path} — the move needs a new file (showrunner, {at})\n"
                                         for path in (extra, more)))
        self.assertEqual(ledger.ticket_origin("names-1"), style_ledger.ApprovedProposal(
            issue, "names", tuple(opened["finding_ids"]), (extra, more)))
        rows = self.widened_rows(ledger)
        self.assertEqual(self.widen(extra), (0, output))
        self.assertEqual(self.widened_rows(ledger), rows)

    def test_widen_refuses_tickets_paths_and_reasons_it_cannot_record(self) -> None:
        ledger, issue = self.widen_ticket()
        self.add_ticket("read-1", [self.rule("read")])
        extra, more = "crates/alpha/src/extra.rs", "crates/alpha/src/more.rs"
        approved = issue.read_text()
        refusals: tuple[tuple[str, tuple[str, ...], dict[str, str], str], ...] = (
            ("unknown ticket", (extra,), {"ticket": "names-9"}, "unknown ticket names-9"),
            ("coverage ticket", (extra,), {"ticket": "read-1"},
             "read-1: only a proposal ticket widens; this is a read ticket"),
            ("empty reason", (extra,), {"reason": "  "}, "names-1: widen requires a reason"),
            ("empty name", (extra,), {"by": " "}, "names-1: widen requires --by"),
            ("absolute path", ("/etc/hosts",), {}, "path must be relative to the repository: /etc/hosts"),
            ("parent path", ("crates/../extra.rs",), {}, "path must be relative to the repository: crates/../extra.rs"),
            ("allowed path", (self.sources[0],), {},
             f"names-1: {self.sources[0]} is already allowed by the approved findings"),
        )
        for name, paths, options, message in refusals:
            with self.subTest(name):
                result, output = self.widen(*paths, **options)
                self.assertEqual((result, output), (2, f"style_plan: {message}\n"))
                self.assertEqual(self.widened_rows(ledger), [])
        with self.subTest("decision not approved"):
            _ = issue.write_text(approved.replace("status: open", "status: closed"))
            self.assertEqual(self.widen(extra), (2, "style_plan: proposal approval or filed findings changed\n"))
            self.assertEqual(self.widened_rows(ledger), [])
            _ = issue.write_text(approved)
        with self.subTest("another reason"):
            self.assertEqual(self.widen(extra)[0], 0)
            rows = self.widened_rows(ledger)
            self.assertEqual(self.widen(extra, reason="another reason"), (
                2, f"style_plan: names-1: {extra} is already widened for another reason: the move needs a new file\n"))
            self.assertEqual(self.widened_rows(ledger), rows)
        with self.subTest("merged ticket"):
            with closing(sqlite3.connect(ledger.database)) as connection:
                _ = connection.execute("UPDATE tickets SET state='merged' WHERE id='names-1'")
                connection.commit()
            self.assertEqual(self.widen(more), (2, "style_plan: names-1: cannot widen a merged ticket\n"))
            with closing(sqlite3.connect(ledger.database)) as connection:
                _ = connection.execute("UPDATE tickets SET state='queued' WHERE id='names-1'")
                connection.commit()
        with self.subTest("dropped ticket"):
            ledger.ticket_drop("names-1")
            self.assertEqual(self.widen(more), (2, "style_plan: names-1: cannot widen a dropped ticket\n"))
        self.assertEqual([row[1] for row in self.widened_rows(ledger)], [extra])

    def test_decision_ticket_claim_is_atomic_and_refuses_other_issues_and_live_claims(self) -> None:
        rule, issue, ids = self.decision_findings()
        ledger = self.ledger()
        for proposal in (str(self.root / "wrong.md"), str(issue)):
            if proposal == str(issue):
                ledger.ticket_add("names-1", "names", "proposal", "names", "crates/alpha", [self.sources[0]], [rule],
                                  proposal=str(issue), finding_ids=ids[:1])
                ledger.ticket_assign("names-1", "main")
            with self.subTest(proposal=proposal), self.assertRaises(style_ledger.LedgerError):
                ledger.ticket_add("other-1", "other", "proposal", "names", "crates/alpha", list(self.sources[:2]), [rule],
                                  proposal=proposal, finding_ids=ids)
            self.assertEqual(ledger.queued_tickets("other"), ())
            self.assertIsInstance(ledger.proposal_findings(issue)[1].build_ticket, style_ledger.NoBuildTicket)

    def test_decision_move_issue_findings_is_one_transaction_and_idempotent(self) -> None:
        rule, issue, ids = self.decision_findings()
        ledger = self.ledger()
        target = self.root / "combined.md"
        moves = {identity: target for identity in ids}
        with self.assertRaisesRegex(style_ledger.LedgerError, "not filed"):
            ledger.move_issue_findings(self.root / "wrong.md", moves)
        for state in ("queued", "assigned"):
            ticket = f"names-{state}"
            ledger.ticket_add(f"{ticket}-1", ticket, "proposal", "names", "crates/alpha", [self.sources[1]], [rule],
                              proposal=str(issue), finding_ids=ids[1:])
            if state == "assigned":
                ledger.ticket_assign(f"{ticket}-1", "main")
            with self.subTest(state=state), self.assertRaisesRegex(style_ledger.LedgerError, "live build ticket"):
                ledger.move_issue_findings(issue, moves)
            # The first finding has no ticket, but the refused second one stops the whole move.
            self.assertEqual(tuple(finding.finding_id for finding in ledger.proposal_findings(issue)), ids)
            ledger.ticket_drop(f"{ticket}-1")
        ledger.move_issue_findings(issue, moves)
        ledger.move_issue_findings(issue, moves)
        self.assertEqual(tuple(finding.finding_id for finding in ledger.proposal_findings(target)), ids)
        self.assertEqual(ledger.proposal_findings(issue), ())
        ledger.waive_held(ids[1], style_ledger.WaiveByPerson("nate"))
        with self.assertRaisesRegex(style_ledger.LedgerError, "not filed"):
            ledger.move_issue_findings(target, {ids[1]: issue})

    def test_drop_candidates_removes_every_candidate_with_the_exact_idea(self) -> None:
        rule = self.rule("read")
        self.add_ticket("names-1", [rule], unit="names")
        ledger = self.ledger()
        for path, idea in ((self.sources[0], "Consider a new rule"), (self.sources[1], "Consider a new rule"),
                           (self.sources[2], "Consider a new rule.")):
            ledger.record_candidate("names-1", path, 1, idea)
        self.assertEqual(ledger.drop_candidates("Consider a new rule"), 2)
        self.assertEqual(tuple(candidate.idea for candidate in ledger.rule_candidates()), ("Consider a new rule.",))
        with self.assertRaisesRegex(style_ledger.LedgerError, "no rule candidate"):
            _ = ledger.drop_candidates("Consider a new rule")

    def test_decision_candidate_cli_stores_idea_without_a_held_finding(self) -> None:
        rule = self.rule("read")
        self.add_ticket("names-1", [rule], unit="names")
        result = self.cli("candidate", "--ticket", "names-1", "--site", self.sources[0] + ":3", "--idea", "Consider a new rule")
        self.assertEqual(result.stdout, "")
        ledger = self.ledger()
        candidate, = ledger.rule_candidates()
        self.assertEqual((candidate.ticket, candidate.unit, candidate.path, candidate.line, candidate.idea),
                         ("names-1", "names", self.sources[0], 3, "Consider a new rule"))
        self.assertTrue(candidate.created_at)
        self.assertEqual(ledger.held_findings(style_ledger.AllHeldFindings()), ())
        refused = self.cli("candidate", "--ticket", "missing-1", "--site", self.sources[0] + ":3", "--idea", "Another idea", success=False)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("unknown ticket", refused.stderr)
        self.assertEqual(ledger.rule_candidates(), (candidate,))

    def test_withdrawal_migration_thirteen_keeps_version_twelve_findings_filed(self) -> None:
        rule = self.rule("read")
        database = self.root / "version-twelve.sqlite"
        issue = self.root / "decision.md"
        with closing(sqlite3.connect(database)) as connection:
            for number, migration in sorted(style_ledger.MIGRATIONS.items()):
                if number <= 12:
                    _ = connection.executescript(migration)
            _ = connection.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
            _ = connection.execute("INSERT INTO schema_version VALUES (12)")
            _ = connection.execute("INSERT INTO held_findings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                   ("legacy", rule, self.sources[0], 2, "change", "rename", "old", "new", self.blob(rule),
                                    "read-1", "filed", str(issue), "created", "updated", "", ""))
            connection.commit()
        with style_ledger.Ledger(self.repo, database) as ledger:
            finding, = ledger.proposal_findings(issue)
            self.assertEqual(finding.state, style_ledger.Filed(issue))
            self.assertEqual(ledger.held_findings(style_ledger.HeldFindingsInState("withdrawn")), ())
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute("SELECT version FROM schema_version").fetchone(), (max(style_ledger.MIGRATIONS),))
            self.assertEqual(connection.execute("SELECT withdrawn_by FROM held_findings").fetchone(), (0,))

    def test_person_read_migration_fourteen_keeps_version_thirteen_findings_not_read_by_a_person(self) -> None:
        rule = self.rule("read")
        database = self.root / "version-thirteen.sqlite"
        issue = self.root / "decision.md"
        with closing(sqlite3.connect(database)) as connection:
            for number, migration in sorted(style_ledger.MIGRATIONS.items()):
                if number <= 13:
                    _ = connection.executescript(migration)
            _ = connection.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
            _ = connection.execute("INSERT INTO schema_version VALUES (13)")
            _ = connection.execute("INSERT INTO held_findings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                   ("legacy", rule, self.sources[0], 2, "change", "rename", "old", "new", self.blob(rule),
                                    "read-1", "built", str(issue), "created", "updated", "", "", 0))
            connection.commit()
        with style_ledger.Ledger(self.repo, database) as ledger:
            finding, = ledger.proposal_findings(issue)
            self.assertEqual((finding.state, finding.reading), (style_ledger.Built(issue), style_ledger.NotReadByPerson()))
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute("SELECT version FROM schema_version").fetchone(), (max(style_ledger.MIGRATIONS),))
            self.assertEqual(connection.execute("SELECT person_read_by FROM held_findings").fetchone(), ("",))

    def test_an_unfiled_finding_withdrawn_by_a_newer_judgment_reads_and_lists_as_judged_again(self) -> None:
        finding_id = self.hold_finding(self.rule("read"), 2)
        ledger = self.ledger()
        with closing(sqlite3.connect(ledger.database)) as connection:
            _ = connection.execute("UPDATE held_findings SET state='withdrawn',withdrawn_by=7 WHERE finding_id=?",
                                   (finding_id,))
            connection.commit()
        lines = {line.split("\t")[0]: line.split("\t")[3:] for line in self.cli("held", "list").stdout.splitlines()}
        self.assertEqual(lines[finding_id], ["withdrawn", "judged again in judgment 7"])
        finding, = ledger.held_findings(style_ledger.HeldFindingsInState("withdrawn"))
        self.assertEqual(finding.state, style_ledger.Withdrawn(style_ledger.NoDecision(), style_ledger.JudgedAgain(7)))
        listed = cast(list[dict[str, object]], json.loads(self.cli("held", "list", "--json").stdout))
        self.assertEqual(listed[0]["withdrawn_by"], 7)

    def test_duplicate_migration_fifteen_keeps_version_fourteen_withdrawals_by_their_exception(self) -> None:
        rule = self.rule("read")
        database = self.root / "version-fourteen.sqlite"
        issue = self.root / "decision.md"
        with closing(sqlite3.connect(database)) as connection:
            for number, migration in sorted(style_ledger.MIGRATIONS.items()):
                if number <= 14:
                    _ = connection.executescript(migration)
            _ = connection.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
            _ = connection.execute("INSERT INTO schema_version VALUES (14)")
            _ = connection.execute("INSERT INTO held_findings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                   ("legacy", rule, self.sources[0], 2, "change", "rename", "old", "new", self.blob(rule),
                                    "read-1", "withdrawn", str(issue), "created", "updated", "", "", 7, ""))
            connection.commit()
        with style_ledger.Ledger(self.repo, database) as ledger:
            finding, = ledger.proposal_findings(issue)
            self.assertEqual(finding.state, style_ledger.Withdrawn(issue, style_ledger.ExceptionVerdict(7)))
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute("SELECT version FROM schema_version").fetchone(), (max(style_ledger.MIGRATIONS),))
            self.assertEqual(connection.execute("SELECT withdrawn_by,duplicate_of FROM held_findings").fetchone(), (7, ""))

    def test_unwaive_migration_sixteen_keeps_version_fifteen_waived_findings_not_unwaived(self) -> None:
        rule = self.rule("read")
        database = self.root / "version-fifteen.sqlite"
        issue = self.root / "decision.md"
        with closing(sqlite3.connect(database)) as connection:
            for number, migration in sorted(style_ledger.MIGRATIONS.items()):
                if number <= 15:
                    _ = connection.executescript(migration)
            _ = connection.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
            _ = connection.execute("INSERT INTO schema_version VALUES (15)")
            _ = connection.execute("INSERT INTO held_findings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                   ("legacy", rule, self.sources[0], 2, "change", "rename", "old", "new", self.blob(rule),
                                    "read-1", "waived", str(issue), "created", "updated", "", "", 0, "", ""))
            connection.commit()
        with style_ledger.Ledger(self.repo, database) as ledger:
            finding, = ledger.proposal_findings(issue)
            self.assertEqual((finding.state, finding.waive_undo), (style_ledger.Waived(), style_ledger.NoWaiveUndo()))
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute("SELECT version FROM schema_version").fetchone(), (max(style_ledger.MIGRATIONS),))
            self.assertEqual(connection.execute("SELECT unwaived_by FROM held_findings").fetchone(), ("",))

    def test_follow_migration_seventeen_keeps_version_sixteen_findings_not_followed(self) -> None:
        rule = self.rule("read")
        database = self.root / "version-sixteen.sqlite"
        issue = self.root / "decision.md"
        with closing(sqlite3.connect(database)) as connection:
            for number, migration in sorted(style_ledger.MIGRATIONS.items()):
                if number <= 16:
                    _ = connection.executescript(migration)
            _ = connection.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
            _ = connection.execute("INSERT INTO schema_version VALUES (16)")
            _ = connection.execute("INSERT INTO held_findings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                   ("legacy", rule, self.sources[0], 2, "change", "rename", "old", "new", self.blob(rule),
                                    "read-1", "withdrawn", str(issue), "created", "updated", "", "", 7, "", "", ""))
            connection.commit()
        with style_ledger.Ledger(self.repo, database) as ledger:
            finding, = ledger.proposal_findings(issue)
            self.assertEqual((finding.state, finding.follow),
                             (style_ledger.Withdrawn(issue, style_ledger.ExceptionVerdict(7)), style_ledger.NotFollowed()))
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute("SELECT version FROM schema_version").fetchone(), (max(style_ledger.MIGRATIONS),))
            self.assertEqual(connection.execute("SELECT followed_from,followed_from_line,followed_at,declaration_gone_at "
                                                + "FROM held_findings").fetchone(), ("", 0, "", ""))

    def test_hold_of_a_followed_finding_site_returns_the_followed_finding(self) -> None:
        rule, issue, ids = self.decision_findings()
        ledger = self.ledger()
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE held_findings SET path=?,line=7,followed_from=?,followed_from_line=1,followed_at=? "
                                   + "WHERE finding_id=?", (self.sources[2], self.sources[0], self.initial_tip, ids[0]))
        count = len(ledger.held_findings(style_ledger.AllHeldFindings()))
        held = ledger.hold(rule, self.sources[2], 8, "required change", "rename", "Outcome::Old", "Outcome::New",
                           self.blob(rule), "read-2")
        self.assertEqual((held.finding_id, held.state, held.line), (ids[0], style_ledger.Filed(issue), 7))
        self.assertEqual(len(ledger.held_findings(style_ledger.AllHeldFindings())), count)

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

    def test_hold_after_a_follow_returns_the_followed_finding_only_for_the_same_change(self) -> None:
        ledger = self.ledger()
        other = "pub struct Other {\n    old: u32,\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT, MOVED_FILE: other})
        rule, (parent,) = self.judge_parent(ledger, ((2, "old: u32,"),))
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub fn other() {}\n", MOVED_FILE: other + "\n" + PARENT})
        followed, = self.follow_approved(ledger, [parent])
        self.assertEqual((type(followed), ledger.held_findings(style_ledger.AllHeldFindings())[0].line), (style_ledger.DeclarationMoved, 6))
        ledger.ticket_add("read-9", "read", "read", "names", "crates/alpha", [MOVED_FILE], [rule])
        ledger.ticket_assign("read-9", "main")
        # A read of b.rs holds a different change on the same text, then the followed change again.
        held_other = ledger.hold(rule, MOVED_FILE, 2, "old: u32,", "rename", "Other::old", "Other::new", self.blob(rule), "read-9")
        held_parent = ledger.hold(rule, MOVED_FILE, 6, "old: u32,", "rename", "Parent::old", "Parent::new", self.blob(rule), "read-9")
        self.assertNotEqual(held_other.finding_id, parent)
        self.assertEqual((held_other.before, held_other.line, held_other.state), ("Other::old", 2, style_ledger.Unfiled()))
        self.assertEqual((held_parent.finding_id, held_parent.line), (parent, 6))
        self.assertEqual(len(ledger.held_findings(style_ledger.AllHeldFindings())), 2)

    def test_a_removed_declaration_whose_twin_was_already_in_another_file_is_withdrawn(self) -> None:
        ledger = self.ledger()
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT, TWIN_FILE: TWIN})
        _, (parent,) = self.judge_parent(ledger, ((2, "old: u32,"),))
        tip = self.commit_tip(ledger, {PARENT_FILE: None})
        removed, = self.follow_approved(ledger, [parent])
        self.assertIsInstance(removed, style_ledger.DeclarationRemoved)
        finding, = ledger.held_findings(style_ledger.AllHeldFindings())
        self.assertEqual((finding.path, finding.state),
                         (PARENT_FILE, style_ledger.Withdrawn(self.root / "decision.md", style_ledger.DeclarationGone(tip))))

    def test_a_move_to_one_file_follows_when_a_changed_third_file_already_declared_a_twin(self) -> None:
        ledger = self.ledger()
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT, TWIN_FILE: TWIN})
        _, (parent,) = self.judge_parent(ledger, ((2, "old: u32,"),))
        tip = self.commit_tip(ledger, {PARENT_FILE: "pub fn other() {}\n", MOVED_FILE: PARENT, TWIN_FILE: TWIN + "fn helper() {}\n"})
        moved, = self.follow_approved(ledger, [parent])
        self.assertIsInstance(moved, style_ledger.DeclarationMoved)
        finding, = ledger.held_findings(style_ledger.AllHeldFindings())
        self.assertEqual((finding.path, finding.line, finding.follow),
                         (MOVED_FILE, 2, style_ledger.FollowedDeclaration(PARENT_FILE, 2, tip)))

    def follow_statement(self, moved: dict[str, str | None]) -> style_ledger.DeclarationOutcome:
        """A filed finding on the statement `let rgba = convert(2, 16);` of fn check in a.rs, read after moved."""
        ledger = self.ledger()
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub fn keep() {}\n\nfn check() {\n    let bytes = [0];\n    let rgba = convert(2, 16);\n}\n"})
        _, (finding,) = self.judge_parent(ledger, ((5, "let rgba = convert(2, 16);"),), kind="restructure")
        _ = self.commit_tip(ledger, moved)
        outcome, = self.follow_approved(ledger, [finding])
        return outcome

    def test_a_statement_follows_its_whole_moved_function_to_the_one_line_that_holds_its_text(self) -> None:
        outcome = self.follow_statement({PARENT_FILE: "pub fn keep() {}\n",
                                         MOVED_FILE: "fn other() {}\n\nfn check() {\n    let bytes = [0];\n\n    let rgba = convert(2, 16);\n}\n"})
        self.assertIsInstance(outcome, style_ledger.DeclarationMoved)
        self.assertEqual((cast(style_ledger.DeclarationMoved, outcome).path, cast(style_ledger.DeclarationMoved, outcome).line),
                         (MOVED_FILE, 6))

    def test_a_statement_whose_text_is_twice_in_the_moved_function_is_left(self) -> None:
        outcome = self.follow_statement({PARENT_FILE: "pub fn keep() {}\n",
                                         MOVED_FILE: "fn check() {\n    let rgba = convert(2, 16);\n    let rgba = convert(2, 16);\n}\n"})
        self.assertEqual(outcome, style_ledger.StatementNotFollowed(
            outcome.finding, declarations.Declaration("fn", "check", declarations.ModuleItem()), MOVED_FILE, declarations.StatementRefusal.TEXT_REPEATED))

    def test_a_statement_whose_text_changed_in_the_moved_function_is_left(self) -> None:
        outcome = self.follow_statement({PARENT_FILE: "pub fn keep() {}\n",
                                         MOVED_FILE: "fn check() {\n    let rgba = convert(WIDTH, STRIDE);\n}\n"})
        self.assertEqual(outcome, style_ledger.StatementNotFollowed(
            outcome.finding, declarations.Declaration("fn", "check", declarations.ModuleItem()), MOVED_FILE, declarations.StatementRefusal.TEXT_NOT_FOUND))

    def test_a_statement_whose_text_is_only_in_a_function_nested_in_the_moved_function_is_left(self) -> None:
        outcome = self.follow_statement({PARENT_FILE: "pub fn keep() {}\n",
                                         MOVED_FILE: "fn check() {\n    fn inner() {\n        let rgba = convert(2, 16);\n    }\n}\n"})
        self.assertEqual(outcome, style_ledger.StatementNotFollowed(
            outcome.finding, declarations.Declaration("fn", "check", declarations.ModuleItem()), MOVED_FILE, declarations.StatementRefusal.TEXT_NOT_FOUND))

    def test_a_statement_whose_text_is_only_in_a_comment_or_string_of_the_moved_function_is_left(self) -> None:
        body = ("    // let rgba = convert(2, 16);\n    /* let rgba = convert(2, 16); */\n"
                + "    let note = \"let rgba = convert(2, 16);\";\n")
        outcome = self.follow_statement({PARENT_FILE: "pub fn keep() {}\n", MOVED_FILE: "fn check() {\n" + body + "}\n"})
        self.assertEqual(outcome, style_ledger.StatementNotFollowed(
            outcome.finding, declarations.Declaration("fn", "check", declarations.ModuleItem()), MOVED_FILE,
            declarations.StatementRefusal.TEXT_NOT_FOUND))

    def test_a_statement_follows_to_its_code_line_past_a_commented_copy(self) -> None:
        outcome = self.follow_statement({PARENT_FILE: "pub fn keep() {}\n",
                                         MOVED_FILE: "fn check() {\n    // let rgba = convert(2, 16);\n    let rgba = convert(2, 16);\n}\n"})
        self.assertEqual((type(outcome), cast(style_ledger.DeclarationMoved, outcome).line), (style_ledger.DeclarationMoved, 3))

    def test_a_statement_with_a_trailing_comment_follows_its_unchanged_moved_function(self) -> None:
        ledger = self.ledger()
        source = "fn check() {\n    let bytes = [0];\n    let rgba = convert(2, 16); // RGBA stride\n    let alpha = convert(3, 8); /* alpha */\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub fn keep() {}\n\n" + source})
        _, ids = self.judge_parent(ledger, ((5, "let rgba = convert(2, 16); // RGBA stride"),
                                            (6, "let alpha = convert(3, 8); /* alpha */")), kind="restructure")
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub fn keep() {}\n", MOVED_FILE: "fn other() {}\n\n" + source})
        outcomes = self.follow_approved(ledger, list(ids))
        self.assertEqual([(type(outcome), cast(style_ledger.DeclarationMoved, outcome).line) for outcome in outcomes],
                         [(style_ledger.DeclarationMoved, 5), (style_ledger.DeclarationMoved, 6)])

    def test_a_statement_whose_function_went_to_two_files_is_left(self) -> None:
        body = "fn check() {\n    let rgba = convert(2, 16);\n}\n"
        outcome = self.follow_statement({PARENT_FILE: "pub fn keep() {}\n", MOVED_FILE: body, TWIN_FILE: body})
        self.assertIsInstance(outcome, style_ledger.DeclaredInSeveralFiles)

    def test_the_declaration_step_reads_each_commit_and_file_once_and_its_recheck_sees_another_writer(self) -> None:
        ledger = self.ledger()
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT})
        _, (field, header) = self.judge_parent(ledger, ((2, "old: u32,"), (1, "pub struct Parent {")))
        tip = self.commit_tip(ledger, {PARENT_FILE: "pub fn other() {}\n", MOVED_FILE: PARENT})
        judged = style_ledger.Ledger.judged_commit
        written = False

        def judged_with_another_writer(self_: style_ledger.Ledger, finding: style_ledger.HeldFinding) -> style_ledger.JudgedCommit | style_ledger.NoJudgedCommit:
            nonlocal written
            if not written:
                written = True
                # Another writer moves the field finding's line after the plan read its row, before the write lock.
                with closing(sqlite3.connect(ledger.database)) as connection, connection:
                    _ = connection.execute("UPDATE held_findings SET line=3,text='}' WHERE finding_id=?", (field,))
            return judged(self_, finding)

        with (mock.patch.object(style_ledger, "read_declarations", wraps=declarations.read_declarations) as reads,
              mock.patch.object(subprocess, "run", wraps=subprocess.run) as runs,
              mock.patch.object(style_ledger.Ledger, "judged_commit", judged_with_another_writer)):
            outcomes = self.follow_approved(ledger, [field, header])
        resolves = [call for call in runs.call_args_list if "--quiet" in cast(tuple[list[str]], call.args)[0]]
        self.assertEqual([type(outcome) for outcome in outcomes], [style_ledger.DeclarationMoved] * 2)
        # One judged commit, and each of a.rs and b.rs at the judged commit and at the tip, across two findings and
        # four readings: a plan, its refused recheck, a plan and its recheck.
        self.assertEqual((len(resolves), reads.call_count), (1, 4))
        rows = {finding.finding_id: (finding.path, finding.line, finding.follow) for finding in ledger.held_findings(style_ledger.AllHeldFindings())}
        self.assertEqual(rows[field], (MOVED_FILE, 3, style_ledger.FollowedDeclaration(PARENT_FILE, 3, tip)))
        self.assertEqual(rows[header][:2], (MOVED_FILE, 1))

    def renamed_parent(self, ledger: style_ledger.Ledger) -> str:
        """A filed rename of struct Parent to Renamed in a.rs, judged at its keyword line."""
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT})
        _, (finding_id,) = self.judge_parent(ledger, ((1, "pub struct Parent {"),), before="Parent", after="Renamed")
        return finding_id

    def test_a_filed_rename_whose_after_name_its_file_now_declares_is_built_not_withdrawn(self) -> None:
        ledger = self.ledger()
        finding_id = self.renamed_parent(ledger)
        # A person read an older build; the new build clears that reading.
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE held_findings SET person_read_by='nate' WHERE finding_id=?", (finding_id,))
        # The renamed declaration is two lines lower; the built finding follows it there, as `built` prints it.
        tip = self.commit_tip(ledger, {PARENT_FILE: "use std::fmt;\n\n" + PARENT.replace("Parent", "Renamed")})
        filed = ledger.held_findings(style_ledger.AllHeldFindings())[0]
        renamed, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(renamed, style_ledger.DeclarationRenamed(
            filed, declarations.Declaration("struct", "Parent", declarations.ModuleItem()),
            declarations.Declaration("struct", "Renamed", declarations.ModuleItem()), PARENT_FILE, 3))
        built, = ledger.held_findings(style_ledger.AllHeldFindings())
        self.assertEqual((built.path, built.line, built.state, built.reading, built.follow),
                         (PARENT_FILE, 3, style_ledger.Built(self.root / "decision.md"), style_ledger.NotReadByPerson(),
                          style_ledger.FollowedDeclaration(PARENT_FILE, 1, tip)))

    def test_a_filed_rename_whose_after_name_one_gaining_file_declares_is_built_and_followed_there(self) -> None:
        ledger = self.ledger()
        finding_id = self.renamed_parent(ledger)
        tip = self.commit_tip(ledger, {PARENT_FILE: "pub fn other() {}\n",
                                       MOVED_FILE: "use std::fmt;\n\n" + PARENT.replace("Parent", "Renamed")})
        renamed, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(renamed, style_ledger.DeclarationRenamed)
        built, = ledger.held_findings(style_ledger.AllHeldFindings())
        self.assertEqual((built.path, built.line, built.state, built.follow),
                         (MOVED_FILE, 3, style_ledger.Built(self.root / "decision.md"),
                          style_ledger.FollowedDeclaration(PARENT_FILE, 1, tip)))

    def test_a_filed_rename_of_an_impl_method_is_built_and_its_applied_check_passes(self) -> None:
        ledger = self.ledger()
        method = "pub struct Parent;\n\nimpl Parent {\n    pub fn old(&self) {}\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: method})
        _, (finding_id,) = self.judge_parent(ledger, ((4, "pub fn old(&self) {}"),))
        tip = self.commit_tip(ledger, {PARENT_FILE: method.replace("fn old", "fn new")})
        filed = ledger.held_findings(style_ledger.AllHeldFindings())[0]
        renamed, = self.follow_approved(ledger, [finding_id])
        owner = declarations.ImplOrTraitItem("Parent")
        self.assertEqual(renamed, style_ledger.DeclarationRenamed(
            filed, declarations.Declaration("fn", "old", owner), declarations.Declaration("fn", "new", owner), PARENT_FILE, 4))
        built, = ledger.held_findings(style_ledger.AllHeldFindings())
        self.assertEqual(built.state, style_ledger.Built(self.root / "decision.md"))
        self.assertEqual(ledger.applied_check(built, tip), AppliedInCode())

    def test_a_filed_finding_follows_a_proved_outside_name_without_changing_its_request(self) -> None:
        ledger = self.ledger()
        finding_id = self.renamed_parent(ledger)
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT.replace("Parent", "Other")})
        followed, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(followed, style_ledger.DeclarationMoved)
        finding, = ledger.held_findings(style_ledger.AllHeldFindings())
        self.assertEqual((finding.finding_id, finding.before, finding.after, finding.state),
                         (finding_id, "Parent", "Renamed", style_ledger.Filed(self.root / "decision.md")))
        before = self.held_rows(ledger.database)
        again, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(again, style_ledger.DeclarationInPlace)
        self.assertEqual(self.held_rows(ledger.database), before)

    def test_a_filed_finding_with_no_proved_destination_stays_filed_after_its_file_is_deleted(self) -> None:
        ledger = self.ledger()
        finding_id = self.renamed_parent(ledger)
        for contents in ("pub fn other() {}\n", None):
            with self.subTest(contents=contents):
                _ = self.commit_tip(ledger, {PARENT_FILE: contents})
                before = self.held_rows(ledger.database)
                left, = self.follow_approved(ledger, [finding_id])
                self.assertIsInstance(left, style_ledger.OutsideRenameUnresolved)
                self.assertEqual(self.held_rows(ledger.database), before)
                finding, = ledger.held_findings(style_ledger.AllHeldFindings())
                self.assertEqual(finding.state, style_ledger.Filed(self.root / "decision.md"))

    def test_an_unfiled_rename_follows_outside_code_while_its_approved_twin_is_built(self) -> None:
        ledger = self.ledger()
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT})
        _, (unfiled, filed) = self.hold_parent(ledger, ((1, "pub struct Parent {"), (2, "old: u32,")), before="Parent", after="Renamed")
        ledger.file_held(filed, self.root / "decision.md")
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT.replace("Parent", "Renamed")})
        outcomes = self.follow_approved(ledger, [unfiled, filed])
        self.assertEqual([type(outcome) for outcome in outcomes], [style_ledger.DeclarationMoved, style_ledger.DeclarationRenamed])
        states = {finding.finding_id: finding.state for finding in ledger.held_findings(style_ledger.AllHeldFindings())}
        self.assertEqual(states, {unfiled: style_ledger.Unfiled(), filed: style_ledger.Built(self.root / "decision.md")})

    def test_a_renamed_finding_that_an_open_build_ticket_holds_is_kept(self) -> None:
        ledger = self.ledger()
        finding_id = self.renamed_parent(ledger)
        ledger.ticket_add("names-1", "names", "proposal", "names", "crates/alpha", [PARENT_FILE], [self.rule("names")],
                          proposal=str(self.root / "decision.md"), finding_ids=(finding_id,))
        ledger.ticket_assign("names-1", "main")
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT.replace("Parent", "Renamed")})
        before = self.held_rows(ledger.database)
        kept, = self.follow_approved(ledger, [finding_id])
        self.assertEqual((type(kept), kept.finding.finding_id), (style_ledger.KeptForOpenBuild, finding_id))
        self.assertEqual(self.held_rows(ledger.database), before)
        # Once the ticket is final, the step builds the finding.
        ledger.ticket_drop("names-1")
        built, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(type(built), style_ledger.DeclarationRenamed)

    def assert_rename_left(self, ledger: style_ledger.Ledger, finding_id: str, expected: style_ledger.DeclarationOutcome) -> None:
        """The declaration step leaves the filed rename with expected and writes nothing."""
        before = self.held_rows(ledger.database)
        left, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(left, expected)
        self.assertEqual(self.held_rows(ledger.database), before)

    def test_a_filed_rename_whose_after_name_its_file_already_declared_is_left(self) -> None:
        ledger = self.ledger()
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT + "\npub struct Renamed;\n"})
        _, (finding_id,) = self.judge_parent(ledger, ((1, "pub struct Parent {"),), before="Parent", after="Renamed")
        # Parent is deleted and Renamed stays where it was, so the rename was never applied.
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub struct Renamed;\n"})
        before = self.held_rows(ledger.database)
        left, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(left, style_ledger.OutsideRenameUnresolved)
        self.assertEqual(self.held_rows(ledger.database), before)
        self.assertIn("declaration not gained", repr(left))

    def test_a_filed_rename_whose_after_name_is_declared_with_another_keyword_is_left(self) -> None:
        ledger = self.ledger()
        finding_id = self.renamed_parent(ledger)
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub enum Renamed {\n    Old,\n}\n"})
        before = self.held_rows(ledger.database)
        left, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(left, style_ledger.OutsideRenameUnresolved)
        self.assertEqual(self.held_rows(ledger.database), before)
        self.assertIn("keyword differs", repr(left))

    def test_a_filed_rename_whose_after_name_one_file_in_another_crate_gained_is_left(self) -> None:
        ledger = self.ledger()
        finding_id = self.renamed_parent(ledger)
        other = "crates/beta/src/other.rs"
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub fn other() {}\n", other: PARENT.replace("Parent", "Renamed")})
        filed = ledger.held_findings(style_ledger.AllHeldFindings())[0]
        self.assert_rename_left(ledger, finding_id, style_ledger.DeclaredInAnotherCrate(
            filed, declarations.Declaration("struct", "Renamed", declarations.ModuleItem()), other))

    def test_a_filed_rename_whose_after_name_two_gaining_files_declare_is_left(self) -> None:
        ledger = self.ledger()
        finding_id = self.renamed_parent(ledger)
        renamed = PARENT.replace("Parent", "Renamed")
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub fn other() {}\n", MOVED_FILE: renamed, TWIN_FILE: renamed})
        filed = ledger.held_findings(style_ledger.AllHeldFindings())[0]
        self.assert_rename_left(ledger, finding_id, style_ledger.DeclaredInSeveralFiles(
            filed, declarations.Declaration("struct", "Renamed", declarations.ModuleItem()), (MOVED_FILE, TWIN_FILE)))

    def test_a_filed_rename_whose_site_still_declares_the_old_name_under_its_parent_is_left(self) -> None:
        ledger = self.ledger()
        method = "pub struct Parent {\n    old: u32,\n}\n\nimpl Parent {\n    pub fn old(&self) {}\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: method})
        # The applied check reads `r#new` as a name the file does not hold, and the field still declares Parent::old.
        _, (finding_id,) = self.judge_parent(ledger, ((6, "pub fn old(&self) {}"),), after="Parent::r#new")
        tip = self.commit_tip(ledger, {PARENT_FILE: method.replace("fn old", "fn r#new")})
        filed = ledger.held_findings(style_ledger.AllHeldFindings())[0]
        self.assertEqual(ledger.applied_check(filed, tip), RenameNotApplied(PARENT_FILE))
        self.assert_rename_left(ledger, finding_id, style_ledger.DeclarationGoneFromFile(
            filed, declarations.Declaration("fn", "old", declarations.ImplOrTraitItem("Parent"))))

    def test_a_filed_finding_whose_file_a_format_ignore_drops_from_the_rust_files_stays_filed(self) -> None:
        ledger = self.ledger()
        finding_id = self.renamed_parent(ledger)
        # The file leaves the synced Rust files but is still in the tree, so the finding waits for a person.
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub fn other() {}\n", "rustfmt.toml": f'ignore = ["{PARENT_FILE}"]\n'})
        filed = ledger.held_findings(style_ledger.AllHeldFindings())[0]
        self.assert_rename_left(ledger, finding_id, style_ledger.DeclarationGoneFromFile(
            filed, declarations.Declaration("struct", "Parent", declarations.ModuleItem())))

    def test_a_renamed_finding_whose_decision_loses_its_approval_before_the_lock_stays_filed(self) -> None:
        ledger = self.ledger()
        finding_id = self.renamed_parent(ledger)
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT.replace("Parent", "Renamed")})
        # The answer is approved at the plan, then not at the recheck under the write lock, nor after.
        answers = iter((True,))
        before = self.held_rows(ledger.database)
        waiting, = ledger.follow_declarations([finding_id], ledger.synced_commit(), lambda _: next(answers, False)).outcomes
        filed = ledger.held_findings(style_ledger.AllHeldFindings())[0]
        self.assertEqual(waiting, style_ledger.RenameAwaitingApproval(filed))
        self.assertEqual(self.held_rows(ledger.database), before)

    def test_the_declaration_step_refuses_when_the_synced_tip_moved_since_its_reading(self) -> None:
        ledger = self.ledger()
        finding_id = self.renamed_parent(ledger)
        read = ledger.synced_commit()
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT.replace("Parent", "Renamed")})
        before = self.held_rows(ledger.database)
        with self.assertRaisesRegex(style_ledger.LedgerError, f"synced tip moved from {read}"):
            _ = ledger.follow_declarations([finding_id], read, lambda _: True)
        self.assertEqual(self.held_rows(ledger.database), before)

    def test_the_declaration_step_refuses_when_the_synced_tip_moves_between_its_plan_and_the_write_lock(self) -> None:
        ledger = self.ledger()
        finding_id = self.renamed_parent(ledger)
        planned = self.commit_tip(ledger, {PARENT_FILE: PARENT.replace("Parent", "Renamed")})
        moved = False

        def read_during_a_sync(text: str) -> declarations.DeclaredFile:
            # A sync lands while the plan reads its first file.
            nonlocal moved
            if not moved:
                moved = True
                _ = self.commit_tip(ledger, {MOVED_FILE: "pub fn helper() {}\n"})
            return declarations.read_declarations(text)

        before = self.held_rows(ledger.database)
        with (mock.patch.object(style_ledger, "read_declarations", read_during_a_sync),
              self.assertRaisesRegex(style_ledger.LedgerError, f"synced tip moved from {planned}")):
            _ = ledger.follow_declarations([finding_id], planned, lambda _: True)
        self.assertEqual(self.held_rows(ledger.database), before)

    def test_a_built_rename_reads_its_file_at_each_commit_once_across_the_plan_and_its_recheck(self) -> None:
        ledger = self.ledger()
        finding_id = self.renamed_parent(ledger)
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT.replace("Parent", "Renamed")})
        with mock.patch.object(subprocess, "run", wraps=subprocess.run) as runs:
            renamed, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(renamed, style_ledger.DeclarationRenamed)
        reads = [call for call in runs.call_args_list
                 if PARENT_FILE in cast(tuple[list[str]], call.args)[0] and "ls-tree" in cast(tuple[list[str]], call.args)[0]]
        # a.rs at the read commit and at the tip; the applied check and the recheck under the lock read no git.
        self.assertEqual(len(reads), 2)

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

    def test_a_trait_qualified_built_rename_follows_a_filed_impl_method_and_keeps_its_type_owner(self) -> None:
        ledger = self.ledger()
        finding_id, source = self.trait_method_finding(ledger)
        _ = self.built_finding(ledger, PARENT_FILE, "Trait::old", "Trait::new", line=2)
        filed = next(row for row in ledger.held_findings(style_ledger.AllHeldFindings()) if row.finding_id == finding_id)
        tip = self.commit_tip(ledger, {PARENT_FILE: "// moved down\n" + source.replace("fn old", "fn new")})
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(outcome, style_ledger.DeclarationMoved(
            filed, declarations.Declaration("fn", "new", declarations.ImplOrTraitItem("Type")), PARENT_FILE, 8))
        with style_ledger.Ledger(self.repo, ledger.database) as reopened:
            followed = next(row for row in reopened.held_findings(style_ledger.AllHeldFindings()) if row.finding_id == finding_id)
            self.assertEqual((followed.finding_id, followed.path, followed.line, followed.state, followed.follow),
                             (finding_id, PARENT_FILE, 8, style_ledger.Filed(self.root / "decision.md"),
                              style_ledger.FollowedDeclaration(PARENT_FILE, 7, tip)))
            self.assertEqual((followed.before, followed.after), (filed.before, filed.after))

    def test_trait_qualified_renames_follow_two_impl_types_in_separate_files_and_leave_unfiled_findings_unfiled(self) -> None:
        ledger = self.ledger()
        source = "pub struct Type;\nimpl Trait for Type {\n    fn old(&self) -> u32 {\n        7\n    }\n}\n"
        other = source.replace("Type", "Other")
        _ = self.commit_tip(ledger, {PARENT_FILE: source, MOVED_FILE: other, TWIN_FILE: "pub trait Trait { fn old(&self) -> u32; }\n"})
        _, (filed,) = self.judge_parent(ledger, ((4, "7"),), kind="restructure")
        _, (unfiled,) = self.hold_parent(ledger, ((4, "7"),), kind="restructure", path=MOVED_FILE)
        _ = self.built_finding(ledger, TWIN_FILE, "Trait::old", "Trait::new")
        tip = self.commit_tip(ledger, {PARENT_FILE: source.replace("fn old", "fn new"),
                                       MOVED_FILE: other.replace("fn old", "fn new"),
                                       TWIN_FILE: "pub trait Trait { fn new(&self) -> u32; }\n"})
        outcomes = self.follow_approved(ledger, [filed, unfiled])
        self.assertEqual([type(outcome) for outcome in outcomes], [style_ledger.DeclarationMoved] * 2)
        rows = {row.finding_id: row for row in ledger.held_findings(style_ledger.AllHeldFindings())}
        for finding_id, path, state in ((filed, PARENT_FILE, style_ledger.Filed(self.root / "decision.md")),
                                        (unfiled, MOVED_FILE, style_ledger.Unfiled())):
            row = rows[finding_id]
            self.assertEqual((row.path, row.line, row.state, row.follow),
                             (path, 4, state, style_ledger.FollowedDeclaration(path, 4, tip)))
        before = self.held_rows(ledger.database)
        self.assertEqual([type(outcome) for outcome in self.follow_approved(ledger, [filed, unfiled])],
                         [style_ledger.DeclarationInPlace] * 2)
        self.assertEqual(self.held_rows(ledger.database), before)

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

    def test_inherent_source_keeps_trait_follow_identity(self) -> None:
        self.assert_trait_source_collision('impl Type')

    def test_other_trait_source_keeps_trait_follow_identity(self) -> None:
        self.assert_trait_source_collision('impl OtherTrait for Type')

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

    def test_inherent_destination_keeps_trait_follow_identity(self) -> None:
        self.assert_trait_destination_collision('impl Type')

    def test_other_trait_destination_keeps_trait_follow_identity(self) -> None:
        self.assert_trait_destination_collision('impl OtherTrait for Type')

    def test_trait_method_follow_composes_with_built_type_and_trait_renames(self) -> None:
        ledger = self.ledger()
        finding_id, source = self.trait_method_finding(ledger)
        _ = self.built_finding(ledger, PARENT_FILE, "Type", "RenamedType", line=4)
        _ = self.built_finding(ledger, PARENT_FILE, "Trait", "RenamedTrait", line=1)
        _ = self.built_finding(ledger, PARENT_FILE, "RenamedTrait::old", "RenamedTrait::new", line=2)
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("Type", "RenamedType").replace("Trait", "RenamedTrait").replace("fn old", "fn new")})
        filed = next(row for row in ledger.held_findings(style_ledger.AllHeldFindings()) if row.finding_id == finding_id)
        moved, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(moved, style_ledger.DeclarationMoved(
            filed, declarations.Declaration("fn", "new", declarations.ImplOrTraitItem("RenamedType")), PARENT_FILE, 7))

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

    def test_preexisting_destination_keeps_trait_follow_identity(self) -> None:
        self.assert_trait_destination_limit('preexisting')

    def test_two_destination_files_keeps_trait_follow_identity(self) -> None:
        self.assert_trait_destination_limit('two files')

    def test_two_destination_items_keeps_trait_follow_identity(self) -> None:
        self.assert_trait_destination_limit('two items')

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

    def test_unapproved_trait_rename_keeps_trait_follow_identity(self) -> None:
        self.assert_trait_follow_limit('unapproved')

    def test_trait_rename_from_another_crate_keeps_trait_follow_identity(self) -> None:
        self.assert_trait_follow_limit('other crate')

    def test_forked_trait_rename_keeps_trait_follow_identity(self) -> None:
        self.assert_trait_follow_limit('fork')

    def test_open_trait_build_ticket_keeps_trait_follow_identity(self) -> None:
        self.assert_trait_follow_limit('open build')

    def test_trait_rename_losing_approval_before_lock_keeps_trait_follow_identity(self) -> None:
        self.assert_trait_follow_limit('lost approval')

    def test_trait_method_rename_chains_stop_before_returning_to_the_original_name(self) -> None:
        ledger = self.ledger()
        finding_id, source = self.trait_method_finding(ledger)
        _ = self.built_finding(ledger, PARENT_FILE, "Trait::old", "Trait::middle", line=2)
        _ = self.built_finding(ledger, PARENT_FILE, "Trait::middle", "Trait::old", line=2)
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("fn old", "fn middle")})
        filed = next(row for row in ledger.held_findings(style_ledger.AllHeldFindings()) if row.finding_id == finding_id)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(outcome, style_ledger.DeclarationMoved(
            filed, declarations.Declaration("fn", "middle", declarations.ImplOrTraitItem("Type")), PARENT_FILE, 7))

    def test_an_open_judging_ticket_keeps_its_trait_method_finding_at_the_read_site(self) -> None:
        ledger = self.ledger()
        source = TRAIT_METHOD
        _ = self.commit_tip(ledger, {PARENT_FILE: source})
        rule = self.rule("names")
        ledger.ticket_add("judge-1", "judge", "read", "names", "crates/alpha", [PARENT_FILE], [rule])
        ledger.ticket_assign("judge-1", "main")
        _ = ledger.ticket_base("judge-1")
        finding = ledger.hold(rule, PARENT_FILE, 7, "7", "restructure", "body", "new body", self.blob(rule), "judge-1")
        _ = self.built_finding(ledger, PARENT_FILE, "Trait::old", "Trait::new", line=2)
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("fn old", "fn new")})
        before = self.held_rows(ledger.database)
        outcome, = self.follow_approved(ledger, [finding.finding_id])
        self.assertIsInstance(outcome, style_ledger.DeclarationNotRead)
        self.assertEqual(self.held_rows(ledger.database), before)

    def test_a_readable_qualified_trait_path_follows_its_method_rename(self) -> None:
        ledger = self.ledger()
        source = "pub struct Type;\nimpl crate::traits::Trait for Type {\n    fn old(&self) -> u32 {\n        7\n    }\n}\n"
        source += "use crate::traits::Trait;\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: source})
        _, (finding_id,) = self.judge_parent(ledger, ((4, "7"),), kind="restructure")
        _ = self.built_finding(ledger, PARENT_FILE, "Trait::old", "Trait::new")
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("fn old", "fn new")})
        filed = next(row for row in ledger.held_findings(style_ledger.AllHeldFindings()) if row.finding_id == finding_id)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(outcome, style_ledger.DeclarationMoved(
            filed, declarations.Declaration("fn", "new", declarations.ImplOrTraitItem("Type")), PARENT_FILE, 4))

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

    def test_an_unreadable_implemented_trait_grants_no_method_follow(self) -> None:
        self.assert_changed_trait_membership_is_left("impl <Type as Family>::Trait for Type",
                                                     "impl <Type as Family>::Trait for Type")

    def test_matching_trait_leaf_names_do_not_hide_different_qualified_trait_paths(self) -> None:
        self.assert_changed_trait_membership_is_left("impl crate::first::Trait for Type",
                                                     "impl crate::second::Trait for Type")

    def test_an_aliased_trait_name_does_not_establish_a_trait_qualified_follow(self) -> None:
        ledger = self.ledger()
        source = "use crate::Trait as Alias;\npub struct Type;\nimpl Alias for Type {\n    fn old(&self) -> u32 {\n        7\n    }\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: source})
        _, (finding_id,) = self.judge_parent(ledger, ((5, "7"),), kind="restructure")
        _ = self.built_finding(ledger, PARENT_FILE, "Trait::old", "Trait::new")
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("fn old", "fn new")})
        before = self.held_rows(ledger.database)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertNotIsInstance(outcome, style_ledger.DeclarationMoved)
        self.assertEqual(self.held_rows(ledger.database), before)

    def test_an_inherent_self_rename_does_not_rename_a_traits_other_implementations(self) -> None:
        ledger = self.ledger()
        finding_id, source = self.trait_method_finding(ledger)
        inherent = "pub struct Other;\nimpl Other {\n    fn old() {}\n    fn caller() { Self::old(); }\n}\n"
        _ = self.commit_tip(ledger, {MOVED_FILE: inherent})
        _ = self.built_finding(ledger, MOVED_FILE, "Self::old", "Self::new", line=4)
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("fn old", "fn new"), MOVED_FILE: inherent.replace("old", "new")})
        before = self.held_rows(ledger.database)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertNotIsInstance(outcome, style_ledger.DeclarationMoved)
        self.assertEqual(self.held_rows(ledger.database), before)

    def test_qualifying_the_same_trait_keeps_an_unfiled_method_in_place(self) -> None:
        ledger = self.ledger()
        source = TRAIT_METHOD
        _ = self.commit_tip(ledger, {PARENT_FILE: source})
        _, (finding_id,) = self.hold_parent(ledger, ((7, "7"),), kind="restructure")
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("impl Trait", "impl crate::a::Trait")})
        before = self.held_rows(ledger.database)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(outcome, style_ledger.DeclarationInPlace)
        self.assertEqual(self.held_rows(ledger.database), before)

    def test_an_import_proves_trait_identity_when_other_modules_declare_the_same_name(self) -> None:
        ledger = self.ledger()
        source = "use crate::b::Trait;\npub struct Type;\nimpl Trait for Type {\n    fn old(&self) { 7 }\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: source, MOVED_FILE: "pub trait Trait {}\n", TWIN_FILE: "pub trait Trait {}\n"})
        _, (finding_id,) = self.hold_parent(ledger, ((4, "7"),), kind="restructure")
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("impl Trait", "impl crate::b::Trait")})
        before = self.held_rows(ledger.database)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(outcome, style_ledger.DeclarationInPlace)
        self.assertEqual(self.held_rows(ledger.database), before)

    def test_an_uncertain_trait_qualification_keeps_the_unfiled_row_unchanged(self) -> None:
        ledger = self.ledger()
        source = "impl Trait for Type {\n    fn old(&self) { 7 }\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: source})
        _, (finding_id,) = self.hold_parent(ledger, ((2, "7"),), kind="restructure")
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("impl Trait", "impl external::Trait")})
        before = self.held_rows(ledger.database)
        planned, = ledger.declaration_reading([finding_id], lambda _: True).outcomes
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(type(outcome).__name__, "TraitIdentityUncertain")
        self.assertEqual(outcome, planned)
        self.assertEqual(self.held_rows(ledger.database), before)

    def test_an_unresolved_built_trait_qualifier_cannot_rename_another_modules_impl(self) -> None:
        ledger = self.ledger()
        source = "impl crate::b::Trait for Type {\n    fn old(&self) { 7 }\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: source, MOVED_FILE: "pub trait Trait { fn old() {} fn new() {} }\n",
                                       TWIN_FILE: "pub trait Trait { fn old() {} fn new() {} }\n"})
        _, (finding_id,) = self.hold_parent(ledger, ((2, "7"),), kind="restructure")
        _ = self.built_finding(ledger, PARENT_FILE, "Trait::old", "Trait::new")
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("fn old", "fn new")})
        before = self.held_rows(ledger.database)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(type(outcome).__name__, "TraitIdentityUncertain")
        self.assertEqual(self.held_rows(ledger.database), before)

    def test_a_module_trait_rename_cannot_change_a_different_modules_trait(self) -> None:
        ledger = self.ledger()
        source = "impl crate::b::Trait for Type {\n    fn old(&self) { 7 }\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: source, MOVED_FILE: "pub trait Trait {}\n", TWIN_FILE: "pub trait Trait {}\n"})
        _, (finding_id,) = self.judge_parent(ledger, ((2, "7"),), kind="restructure")
        _ = self.built_finding(ledger, TWIN_FILE, "c::Trait", "c::NewTrait")
        _ = self.built_finding(ledger, TWIN_FILE, "NewTrait::old", "NewTrait::new")
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("b::Trait", "b::NewTrait").replace("fn old", "fn new"),
                                       TWIN_FILE: "pub trait NewTrait {}\n"})
        before = self.held_rows(ledger.database)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertNotIsInstance(outcome, style_ledger.DeclarationMoved)
        self.assertEqual(self.held_rows(ledger.database), before)

    def test_a_method_rename_does_not_follow_between_different_trait_arguments(self) -> None:
        ledger = self.ledger()
        source = "pub trait Trait<T> { fn old() {} fn new() {} }\nimpl Trait<u8> for Type {\n    fn old(&self) { 7 }\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: source})
        _, (finding_id,) = self.judge_parent(ledger, ((3, "7"),), kind="restructure")
        _ = self.built_finding(ledger, PARENT_FILE, "Trait::old", "Trait::new")
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("Trait<u8>", "Trait<u16>").replace("fn old(&self)", "fn new(&self)")})
        before = self.held_rows(ledger.database)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertNotIsInstance(outcome, style_ledger.DeclarationMoved)
        self.assertEqual(self.held_rows(ledger.database), before)

    def test_a_trait_method_follows_to_a_proved_equal_qualified_destination(self) -> None:
        ledger = self.ledger()
        source = "impl Trait for Type {\n    fn old(&self) { 7 }\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: source, TWIN_FILE: "pub trait Trait { fn old() {} }\n"})
        _, (finding_id,) = self.hold_parent(ledger, ((2, "7"),), kind="restructure")
        _ = self.built_finding(ledger, TWIN_FILE, "Trait::old", "Trait::new")
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub fn helper() {}\n",
                                       MOVED_FILE: source.replace("impl Trait", "impl crate::c::Trait").replace("fn old", "fn new"),
                                       TWIN_FILE: "pub trait Trait { fn new() {} }\n"})
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(outcome, style_ledger.DeclarationMoved)
        row = next(row for row in ledger.held_findings(style_ledger.AllHeldFindings()) if row.finding_id == finding_id)
        self.assertEqual((row.path, row.line, row.state), (MOVED_FILE, 2, style_ledger.Unfiled()))

    def test_unreadable_trait_arguments_keep_the_finding_instead_of_withdrawing_it(self) -> None:
        ledger = self.ledger()
        source = "impl Trait<u8>::Assoc for Type {\n    fn old(&self) { 7 }\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: source})
        _, (finding_id,) = self.hold_parent(ledger, ((2, "7"),), kind="restructure")
        _ = self.built_finding(ledger, PARENT_FILE, "Trait::old", "Trait::new")
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("fn old", "fn new")})
        before = self.held_rows(ledger.database)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(type(outcome).__name__, "TraitIdentityUncertain")
        self.assertEqual(self.held_rows(ledger.database), before)

    def test_a_built_method_rename_in_another_traits_module_grants_no_follow(self) -> None:
        ledger = self.ledger()
        source = "impl crate::b::Trait for Type {\n    fn old(&self) { 7 }\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: source,
                                       MOVED_FILE: "pub trait Trait { fn old() {} fn new() {} }\n",
                                       TWIN_FILE: "pub trait Trait { fn old() {} fn new() {} }\n"})
        _, (finding_id,) = self.judge_parent(ledger, ((2, "7"),), kind="restructure")
        _ = self.built_finding(ledger, TWIN_FILE, "Trait::old", "Trait::new")
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("fn old", "fn new")})
        before = self.held_rows(ledger.database)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertNotIsInstance(outcome, style_ledger.DeclarationMoved)
        self.assertEqual(self.held_rows(ledger.database), before)

    def test_a_plain_trait_alias_keeps_the_same_qualified_impl_in_place(self) -> None:
        ledger = self.ledger()
        source = "use crate::b::Trait as Alias;\nimpl Alias for Type {\n    fn old(&self) { 7 }\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: source, MOVED_FILE: "pub trait Trait {}\n"})
        _, (finding_id,) = self.hold_parent(ledger, ((3, "7"),), kind="restructure")
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("impl Alias", "impl crate::b::Trait")})
        before = self.held_rows(ledger.database)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(outcome, style_ledger.DeclarationInPlace)
        self.assertEqual(self.held_rows(ledger.database), before)

    def test_an_expanded_trait_alias_does_not_match_another_nested_modules_reexport(self) -> None:
        ledger = self.ledger()
        source = "use crate::one::nested::Trait as Alias;\nimpl Alias for Type {\n    fn old(&self) { 7 }\n}\n"
        _ = self.commit_tip(ledger, {
            PARENT_FILE: source,
            "crates/alpha/src/lib.rs": "mod a; mod b; mod other; mod one; mod two;\n",
            "crates/alpha/src/one.rs": "mod nested;\n",
            "crates/alpha/src/two.rs": "mod nested;\n",
            "crates/alpha/src/one/nested.rs": "pub trait Trait { fn old() {} fn new() {} }\n",
            "crates/alpha/src/two/nested.rs": "pub use crate::other::Other as Trait;\n",
            "crates/alpha/src/other.rs": "pub trait Other { fn old() {} fn new() {} }\n",
        })
        _, (finding_id,) = self.judge_parent(ledger, ((3, "7"),), kind="restructure")
        _ = self.built_finding(ledger, PARENT_FILE, "Alias::old", "Alias::new", line=3)
        _ = self.commit_tip(ledger, {
            PARENT_FILE: "use crate::one::nested::Trait as Alias;\n",
            MOVED_FILE: "impl crate::two::nested::Trait for Type {\n    fn new(&self) { 7 }\n}\n",
        })
        before = self.held_rows(ledger.database)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(outcome, style_ledger.TraitIdentityUncertain)
        self.assertEqual(self.held_rows(ledger.database), before)
        _ = self.commit_tip(ledger, {
            MOVED_FILE: None,
            "crates/alpha/src/one/nested.rs": (
                "pub trait Trait { fn old() {} fn new() {} }\n"
                "impl Trait for Type {\n    fn new(&self) { 7 }\n}\n"
            ),
        })
        proved, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(proved, style_ledger.DeclarationMoved)
        self.assertEqual(cast(style_ledger.DeclarationMoved, proved).path, "crates/alpha/src/one/nested.rs")

    def test_growing_trait_alias_cycles_stop_without_following_or_withdrawing(self) -> None:
        ledger = self.ledger()
        source = "use b::a;\nuse a::b;\nimpl a::Trait for Type {\n    fn old(&self) { 7 }\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: source})
        _, (finding_id,) = self.hold_parent(ledger, ((4, "7"),), kind="restructure")
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("impl a::Trait", "impl crate::b::Trait")})
        before = self.held_rows(ledger.database)
        # The child must finish within five seconds, even if expansion keeps growing its path.
        program = (
            "from pathlib import Path; import style_ledger; import sys; "
            "ledger = style_ledger.Ledger(Path(sys.argv[1]), Path(sys.argv[2])); "
            "outcome, = ledger.follow_declarations([sys.argv[3]], ledger.synced_commit(), approved=lambda path: True).outcomes; "
            "print(type(outcome).__name__); ledger.close()"
        )
        result = subprocess.run([sys.executable, "-c", program, str(self.repo), str(ledger.database), finding_id],
                                capture_output=True, text=True, check=True, timeout=5,
                                env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
        self.assertEqual(result.stdout.strip(), "TraitIdentityUncertain")
        self.assertEqual(self.held_rows(ledger.database), before)

    def test_unsupported_alias_sources_keep_trait_identity_uncertain(self) -> None:
        for index, (prefix, trait) in enumerate((
            ("#[cfg(test)] use crate::b::Trait as Alias;\n", "Alias"),
            ("mod imports { use crate::b::Trait as Alias;\n", "Alias"),
            ("pub use crate::b::Trait as Alias;\n", "Alias"),
            ("use external::*;\n", "Alias"),
            ("mod nested {\n", "self::Alias"),
            ("mod nested {\n", "super::Alias"),
        )):
            with self.subTest(prefix=prefix, trait=trait):
                ledger = style_ledger.Ledger(self.repo, self.root / f"aliases-{index}.db")
                self.addCleanup(ledger.close)
                source = prefix + f"impl {trait} for Type {{\n    fn old(&self) {{ 7 }}\n}}\n"
                if prefix.startswith(("mod imports", "mod nested")):
                    source += "}\n"
                _ = self.commit_tip(ledger, {PARENT_FILE: source, MOVED_FILE: "pub trait Trait {}\n"})
                _, (finding_id,) = self.hold_parent(ledger, ((len(prefix.splitlines()) + 2, "7"),),
                                                     kind="restructure", before=prefix + trait)
                _ = self.commit_tip(ledger, {PARENT_FILE: source.replace(f"impl {trait}", "impl crate::b::Trait")})
                before = self.held_rows(ledger.database)
                outcome, = self.follow_approved(ledger, [finding_id])
                self.assertIsInstance(outcome, style_ledger.TraitIdentityUncertain)
                self.assertEqual(self.held_rows(ledger.database), before)

    def test_a_built_trait_alias_cannot_authorize_a_different_traits_method(self) -> None:
        ledger = self.ledger()
        source = "impl crate::b::Trait for Type {\n    fn old(&self) { 7 }\n}\n"
        built_site = "use crate::other::Other as Trait;\npub fn caller() { Trait::old(); }\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: source, MOVED_FILE: "pub trait Trait {}\n", TWIN_FILE: built_site,
                                   "crates/alpha/src/other.rs": "pub trait Other {}\n"})
        _, (finding_id,) = self.judge_parent(ledger, ((2, "7"),), kind="restructure")
        _ = self.built_finding(ledger, TWIN_FILE, "Trait::old", "Trait::new", line=2)
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("fn old", "fn new")})
        before = self.held_rows(ledger.database)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(outcome, style_ledger.DeclarationGoneFromFile)
        self.assertEqual(self.held_rows(ledger.database), before)

    def test_unsupported_imports_block_built_trait_declaration_fallback(self) -> None:
        for index, imported in enumerate(("#[cfg(test)] use crate::other::Other as Trait;",
                         "mod imports { use crate::other::Other as Trait;",
                         "use external::*;", "pub use crate::other::Other as Trait;")):
            with self.subTest(imported=imported):
                ledger = style_ledger.Ledger(self.repo, self.root / f"blocked-{index}.db")
                self.addCleanup(ledger.close)
                source = "impl crate::b::Trait for Type {\n    fn old(&self) { 7 }\n}\n"
                built_site = imported + "\npub fn caller() { Trait::old(); }\n"
                if imported.startswith("mod imports"):
                    built_site += "}\n"
                _ = self.commit_tip(ledger, {PARENT_FILE: source, MOVED_FILE: "pub trait Trait {}\n",
                                           TWIN_FILE: built_site})
                _, (finding_id,) = self.judge_parent(ledger, ((2, "7"),), kind="restructure", before=imported)
                _ = self.built_finding(ledger, TWIN_FILE, "Trait::old", "Trait::new", line=2)
                _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("fn old", "fn new")})
                before = self.held_rows(ledger.database)
                outcome, = self.follow_approved(ledger, [finding_id])
                self.assertIsInstance(outcome, style_ledger.TraitIdentityUncertain)
                self.assertEqual(self.held_rows(ledger.database), before)

    def test_a_real_built_trait_rename_follows_its_dependent_impl_method(self) -> None:
        ledger = self.ledger()
        source = "pub trait Trait {\n    fn old() {}\n}\nimpl Trait for Type {\n    fn old(&self) { 7 }\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: source})
        _, (method,) = self.judge_parent(ledger, ((5, "7"),), kind="restructure")
        _, (rename,) = self.judge_parent(ledger, ((1, "pub trait Trait"),), before="Trait", after="RenamedTrait")
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("Trait", "RenamedTrait")})
        renamed, = self.follow_approved(ledger, [rename])
        self.assertIsInstance(renamed, style_ledger.DeclarationRenamed)
        followed, = self.follow_approved(ledger, [method])
        self.assertIsInstance(followed, style_ledger.DeclarationMoved)
        self.assertEqual(cast(style_ledger.DeclarationMoved, followed).declaration,
                         declarations.Declaration("fn", "old", declarations.ImplOrTraitItem("Type")))
        again, = self.follow_approved(ledger, [method])
        self.assertIsInstance(again, style_ledger.DeclarationInPlace)

    def test_an_uncertain_built_rename_without_a_gained_destination_does_not_keep_a_deleted_method(self) -> None:
        ledger = self.ledger()
        source = "impl crate::b::Trait for Type {\n    fn old(&self) { 7 }\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: source, MOVED_FILE: "pub trait Trait {}\n", TWIN_FILE: "pub trait Trait {}\n"})
        _, (finding_id,) = self.hold_parent(ledger, ((2, "7"),), kind="restructure")
        _ = self.built_finding(ledger, PARENT_FILE, "Trait::old", "Trait::new")
        _ = self.commit_tip(ledger, {PARENT_FILE: "impl crate::b::Trait for Type {}\n"})
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(outcome, style_ledger.DeclarationRemoved)
        row = next(row for row in ledger.held_findings(style_ledger.AllHeldFindings()) if row.finding_id == finding_id)
        self.assertIsInstance(row.state, style_ledger.Withdrawn)

    def test_an_unrelated_built_name_in_another_crate_does_not_withdraw_an_outside_rename(self) -> None:
        ledger = self.ledger()
        source = "fn original() {\n    first();\n    second();\n}\n"
        other = "crates/beta/src/other.rs"
        _ = self.commit_tip(ledger, {PARENT_FILE: source, other: source.replace("original", "unrelated")})
        _, (finding_id,) = self.hold_parent(ledger, ((1, "fn original() {"),), kind="restructure")
        _ = self.built_finding(ledger, other, "original", "elsewhere")
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("original", "renamed")})
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(outcome, style_ledger.DeclarationMoved)
        row = next(row for row in ledger.held_findings(style_ledger.AllHeldFindings()) if row.finding_id == finding_id)
        self.assertEqual((row.path, row.state), (PARENT_FILE, style_ledger.Unfiled()))
        self.assertIsInstance(row.follow, style_ledger.FollowedDeclaration)

    def test_a_built_rename_in_another_module_does_not_withdraw_an_outside_rename(self) -> None:
        ledger = self.ledger()
        source = "fn original() {\n    first();\n    second();\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: source, MOVED_FILE: "fn elsewhere() {}\n"})
        _, (finding_id,) = self.hold_parent(ledger, ((1, "fn original() {"),), kind="restructure")
        _ = self.built_finding(ledger, MOVED_FILE, "b::original", "b::elsewhere")
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("original", "renamed")})
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(outcome, style_ledger.DeclarationMoved)
        row = next(row for row in ledger.held_findings(style_ledger.AllHeldFindings()) if row.finding_id == finding_id)
        self.assertEqual((row.path, row.state), (PARENT_FILE, style_ledger.Unfiled()))
        self.assertIsInstance(row.follow, style_ledger.FollowedDeclaration)

    def test_outside_method_renames_require_the_same_resolved_generic_argument(self) -> None:
        for changed in (False, True):
            with self.subTest(argument_changed=changed):
                with style_ledger.Ledger(self.repo, self.root / f"generic-owner-{changed}.db") as ledger:
                    source = ("use crate::a::one::Arg as A;\nmod one { pub struct Arg; }\n"
                              + "mod two { pub struct Arg; }\nstruct Holder<T>(T);\n"
                              + "impl crate::a::Holder<A> {\n    fn original(&self) -> u32 {\n        1\n    }\n}\n")
                    _ = self.commit_tip(ledger, {PARENT_FILE: source})
                    _, (finding_id,) = self.judge_parent(ledger, ((6, "fn original(&self) -> u32 {"),), kind="restructure")
                    target = source.replace("fn original", "fn renamed")
                    if changed:
                        target = target.replace("use crate::a::one::Arg", "use crate::a::two::Arg")
                    _ = self.commit_tip(ledger, {PARENT_FILE: target})
                    before = self.held_rows(ledger.database)
                    outcome, = self.follow_approved(ledger, [finding_id])
                    if changed:
                        self.assertIsInstance(outcome, style_ledger.OutsideRenameUnresolved)
                        self.assertIn(style_ledger.OutsideFailure.WRONG_OWNER, repr(outcome))
                        self.assertEqual(self.held_rows(ledger.database), before)
                    else:
                        self.assertIsInstance(outcome, style_ledger.DeclarationMoved)
                        row = next(row for row in ledger.held_findings(style_ledger.AllHeldFindings()) if row.finding_id == finding_id)
                        self.assertEqual((row.path, row.state), (PARENT_FILE, style_ledger.Filed(self.root / "decision.md")))

    def test_outside_method_renames_distinguish_external_arguments_from_local_paths(self) -> None:
        for argument, local in (("crate::a::std::string::String", True),
                                ("std::string::String", True), ("std::string::String", False)):
            with self.subTest(argument=argument, local_root_added=local):
                with style_ledger.Ledger(self.repo, self.root / f"external-owner-{local}-{argument.count('::')}.db") as ledger:
                    source = ("struct Holder<T>(T);\nimpl Holder<std::string::String> {\n"
                              + "    fn original(&self) -> u32 {\n        1\n    }\n}\n")
                    _ = self.commit_tip(ledger, {PARENT_FILE: source})
                    _, (finding_id,) = self.judge_parent(ledger, ((3, "fn original(&self) -> u32 {"),), kind="restructure")
                    target = source.replace("fn original", "fn renamed").replace("std::string::String", argument)
                    if local:
                        target += "mod std { pub mod string { pub struct String; } }\n"
                    _ = self.commit_tip(ledger, {PARENT_FILE: target})
                    before = self.held_rows(ledger.database)
                    outcome, = self.follow_approved(ledger, [finding_id])
                    if local:
                        self.assertIsInstance(outcome, style_ledger.OutsideRenameUnresolved)
                        self.assertIn(style_ledger.OutsideFailure.WRONG_OWNER, repr(outcome))
                        self.assertEqual(self.held_rows(ledger.database), before)
                    else:
                        self.assertIsInstance(outcome, style_ledger.DeclarationMoved)
                        row = next(row for row in ledger.held_findings(style_ledger.AllHeldFindings()) if row.finding_id == finding_id)
                        self.assertEqual((row.path, row.state), (PARENT_FILE, style_ledger.Filed(self.root / "decision.md")))
                        self.assertIsInstance(row.follow, style_ledger.FollowedDeclaration)

    def test_outside_method_renames_require_the_same_external_crate_binding(self) -> None:
        for binding in ("std", "beta"):
            with self.subTest(destination_crate=binding):
                with style_ledger.Ledger(self.repo, self.root / f"external-binding-{binding}.db") as ledger:
                    source = ("extern crate std as external;\npub struct Holder<T>(T);\n"
                              + "impl crate::a::Holder<external::string::String> {\n"
                              + "    fn original(&self) -> u32 {\n        7\n    }\n}\n")
                    target = (f"extern crate {binding} as external;\n"
                              + "impl crate::a::Holder<external::string::String> {\n"
                              + "    fn renamed(&self) -> u32 {\n        7\n    }\n}\n")
                    _ = self.commit_tip(ledger, {PARENT_FILE: source, MOVED_FILE: "// empty\n",
                                                 "crates/beta/src/lib.rs": "pub mod string { pub struct String; }\n"})
                    _, (finding_id,) = self.judge_parent(ledger, ((4, "fn original(&self) -> u32 {"),), kind="restructure")
                    _ = self.commit_tip(ledger, {PARENT_FILE: "pub struct Holder<T>(T);\n", MOVED_FILE: target})
                    before = self.held_rows(ledger.database)
                    outcome, = self.follow_approved(ledger, [finding_id])
                    self.assertIsInstance(outcome, style_ledger.OutsideRenameUnresolved)
                    self.assertIn(style_ledger.OutsideFailure.WRONG_OWNER, repr(outcome))
                    self.assertEqual(self.held_rows(ledger.database), before)

    def test_crate_root_extern_aliases_do_not_prove_an_outside_method_rename(self) -> None:
        ledger = self.ledger()
        source = ("extern crate beta as external;\npub struct Holder<T>(T);\n"
                  + "impl crate::a::Holder<external::string::String> {\n"
                  + "    fn original(&self) -> u32 {\n        7\n    }\n}\n")
        target = ("impl crate::a::Holder<beta::string::String> {\n"
                  + "    fn renamed(&self) -> u32 {\n        7\n    }\n}\n")
        _ = self.commit_tip(ledger, {
            PARENT_FILE: source, MOVED_FILE: "// empty\n",
            "crates/alpha/src/lib.rs": "extern crate std as beta;\nmod a; mod b; mod c; mod other;\n",
            "crates/alpha/Cargo.toml": '[package]\nname="alpha"\nversion="0.1.0"\nedition="2021"\n'
                + '[dependencies]\nbeta={path="../beta"}\n',
            "crates/beta/src/lib.rs": "pub mod string { pub struct String; }\n",
        })
        _, (finding_id,) = self.judge_parent(ledger, ((4, "fn original(&self) -> u32 {"),), kind="restructure")
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub struct Holder<T>(T);\n", MOVED_FILE: target})
        before = self.held_rows(ledger.database)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(outcome, style_ledger.OutsideRenameUnresolved)
        self.assertIn(style_ledger.OutsideFailure.WRONG_OWNER, repr(outcome))
        self.assertEqual(self.held_rows(ledger.database), before)

    def test_extern_bindings_in_either_revision_keep_outside_method_findings_unchanged(self) -> None:
        root = "crates/alpha/src/lib.rs"
        for file in (PARENT_FILE, MOVED_FILE, root):
            for revision in ("before", "after"):
                with self.subTest(file=file, revision=revision):
                    with style_ledger.Ledger(self.repo, self.root / f"extern-{Path(file).stem}-{revision}.db") as ledger:
                        source = ("pub struct Holder<T>(T);\nimpl crate::a::Holder<std::string::String> {\n"
                                  + "    fn original(&self) -> u32 {\n        7\n    }\n}\n")
                        target = ("impl crate::a::Holder<std::string::String> {\n"
                                  + "    fn renamed(&self) -> u32 {\n        7\n    }\n}\n")
                        before_files: dict[str, str | None] = {
                            PARENT_FILE: source, MOVED_FILE: "// empty\n", root: "mod a; mod b; mod c; mod other;\n",
                        }
                        after_files: dict[str, str | None] = {
                            PARENT_FILE: "pub struct Holder<T>(T);\n", MOVED_FILE: target,
                            root: "mod a; mod b; mod c; mod other;\n",
                        }
                        files = before_files if revision == "before" else after_files
                        files[file] = "extern crate std as external;\n" + str(files[file])
                        _ = self.commit_tip(ledger, before_files)
                        line = 4 if file == PARENT_FILE and revision == "before" else 3
                        _, (finding_id,) = self.judge_parent(ledger, ((line, "fn original(&self) -> u32 {"),), kind="restructure")
                        _ = self.commit_tip(ledger, after_files)
                        before = self.held_rows(ledger.database)
                        outcome, = self.follow_approved(ledger, [finding_id])
                        self.assertIsInstance(outcome, style_ledger.OutsideRenameUnresolved)
                        self.assertIn(style_ledger.OutsideFailure.WRONG_OWNER, repr(outcome))
                        self.assertEqual(self.held_rows(ledger.database), before)

    def test_local_argument_attributes_only_block_conditional_identity(self) -> None:
        for attribute in ("#[derive(Clone)]", "#[derive(Clone)]\n", "#[cfg(test)]", "#[cfg_attr(test, derive(Clone))]"):
            with self.subTest(attribute=attribute):
                with style_ledger.Ledger(self.repo, self.root / f"argument-attribute-{len(attribute)}.db") as ledger:
                    source = (f"{attribute} struct Arg;\nstruct Holder<T>(T);\n"
                              + "impl crate::a::Holder<Arg> {\n"
                              + "    fn original(&self) -> u32 {\n        7\n    }\n}\n")
                    line = source.splitlines().index("    fn original(&self) -> u32 {") + 1
                    _ = self.commit_tip(ledger, {PARENT_FILE: source})
                    _, (finding_id,) = self.judge_parent(ledger, ((line, "fn original(&self) -> u32 {"),), kind="restructure")
                    _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("fn original", "fn renamed")})
                    before = self.held_rows(ledger.database)
                    outcome, = self.follow_approved(ledger, [finding_id])
                    if "cfg" in attribute:
                        self.assertIsInstance(outcome, style_ledger.OutsideRenameUnresolved)
                        self.assertIn(style_ledger.OutsideFailure.WRONG_OWNER, repr(outcome))
                        self.assertEqual(self.held_rows(ledger.database), before)
                    else:
                        self.assertIsInstance(outcome, style_ledger.DeclarationMoved)
                        row = next(row for row in ledger.held_findings(style_ledger.AllHeldFindings()) if row.finding_id == finding_id)
                        self.assertEqual((row.path, row.line, row.state),
                                         (PARENT_FILE, line, style_ledger.Filed(self.root / "decision.md")))

    def test_a_typed_closures_retained_parameter_follows_its_renamed_function_under_lock(self) -> None:
        ledger = self.ledger()
        source = ("fn original() {\n    common_one();\n    common_two();\n"
                  + "    let handler: fn(u32) =\n        move |answers| {\n"
                  + "            consume(answers);\n        };\n    common_three();\n    common_four();\n}\n")
        _ = self.commit_tip(ledger, {PARENT_FILE: source})
        _, (finding_id,) = self.judge_parent(ledger, ((5, "move |answers| {"),), kind="restructure")
        original, = ledger.held_findings(style_ledger.AllHeldFindings())
        tip = self.commit_tip(ledger, {PARENT_FILE: source.replace("fn original", "fn renamed")})
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(outcome, style_ledger.DeclarationMoved)
        row, = ledger.held_findings(style_ledger.AllHeldFindings())
        self.assertEqual((row.finding_id, row.path, row.line, row.state, row.text, row.before, row.after),
                         (original.finding_id, PARENT_FILE, 5, original.state, original.text, original.before, original.after))
        self.assertEqual(row.follow, style_ledger.FollowedDeclaration(PARENT_FILE, 5, tip))
        before = self.held_rows(ledger.database)
        _ = self.follow_approved(ledger, [finding_id])
        self.assertEqual(self.held_rows(ledger.database), before)

    def test_retained_children_follow_a_renamed_function_with_changed_closure_call_arguments(self) -> None:
        source = """    fn uncached_completed_rows_arrive_before_hierarchy_discovery_is_released() {
        struct GatedHierarchy {
            entered: Sender<()>,
            release: Mutex<Receiver<()>>,
        }
        impl HierarchyRunner for GatedHierarchy {
            fn run(&self, _: &str, _: Duration) -> Result<HierarchyEdges, HierarchyError> {
                self.entered.send(()).expect("signal discovery");
                self.release
                    .lock()
                    .expect("release lock")
                    .recv()
                    .expect("release discovery");
                Err(HierarchyError::TimedOut)
            }
        }
        let (entered, waiting) = mpsc::channel();
        let (release, released) = mpsc::channel();
        let expected = vec![session_row("worker", 2)];
        let rows = expected.clone();
        let updates = spawn_with_test_source(
            HierarchyCommand::new(HierarchySource::Command("fixture".to_string())),
            Arc::new(GatedHierarchy {
                entered,
                release: Mutex::new(released),
            }),
            move |answers| {
                answers
                    .publish(ScanStage::Completed, rows.clone())
                    .expect("publish completion");
            },
        );
        waiting
            .recv_timeout(Duration::from_millis(200))
            .expect("discovery starts");
        let early = updates.try_recv();
        release
            .send(())
            .expect("release discovery before assertions");
        assert_eq!(
            early.expect("uncached completed rows precede discovery"),
            CensusUpdate::Local {
                rows:      expected.clone(),
                hierarchy: HierarchyEdges::default().into(),
                stage:     ScanStage::Classified,
            }
        );
        assert_eq!(
            updates
                .recv_timeout(Duration::from_millis(200))
                .expect("failed discovery answers"),
            noted(expected, HierarchyNotice::Failed(HierarchyError::TimedOut))
        );
    }
"""
        target = """    fn uncached_first_publication_respects_its_budget_and_holds_claude_rows() {
        struct GatedHierarchy {
            entered: Sender<()>,
            release: Mutex<Receiver<()>>,
        }
        impl HierarchyRunner for GatedHierarchy {
            fn run(&self, _: &str, _: Duration) -> Result<HierarchyEdges, HierarchyError> {
                self.entered.send(()).expect("signal discovery");
                self.release
                    .lock()
                    .expect("release lock")
                    .recv()
                    .expect("release discovery");
                Err(HierarchyError::TimedOut)
            }
        }
        let (entered, waiting) = mpsc::channel();
        let (release, released) = mpsc::channel();
        let expected = vec![session_row("worker", 2)];
        let rows = expected.clone();
        let budget = HIERARCHY_FIRST_PUBLISH_BUDGET / 100;
        let deadline = budget + HIERARCHY_FIRST_PUBLISH_BUDGET / 5;
        let (finish, details) = mpsc::channel();
        let started = Instant::now();
        let updates = spawn_with_test_source_and_budget(
            HierarchyCommand::new(HierarchySource::Command("fixture".to_string())),
            Arc::new(GatedHierarchy {
                entered,
                release: Mutex::new(released),
            }),
            budget,
            move |answers| {
                answers
                    .publish(ScanStage::Completed, rows.clone())
                    .expect("publish completion");
                details
                    .recv()
                    .expect("keep the scan open for the late answer");
            },
        );
        waiting
            .recv_timeout(Duration::from_millis(200))
            .expect("discovery starts");
        let early = updates.recv_timeout(deadline);
        let elapsed = started.elapsed();
        eprintln!("bounded first publication: {} ms", elapsed.as_millis());
        release
            .send(())
            .expect("release discovery before assertions");
        assert!(elapsed < deadline, "first publication took {elapsed:?}");
        assert_eq!(
            early.expect("safe rows publish inside the startup budget"),
            CensusUpdate::Local {
                rows:      Vec::new(),
                hierarchy: HierarchySnapshot::default(),
                stage:     ScanStage::Completed,
            }
        );
        let late = updates.recv_timeout(deadline);
        finish.send(()).expect("release details before assertions");
        assert_eq!(
            late.expect("failed discovery answers during detail reads"),
            noted(expected, HierarchyNotice::Failed(HierarchyError::TimedOut))
        );
    }
"""
        ledger = self.ledger()
        _ = self.commit_tip(ledger, {PARENT_FILE: source})
        _, findings = self.judge_parent(ledger, ((19, "let rows = expected.clone();"), (27, "move |answers| {")), kind="restructure")
        before = {row.finding_id: row for row in ledger.held_findings(style_ledger.AllHeldFindings())}
        _ = self.commit_tip(ledger, {PARENT_FILE: target})
        outcomes = self.follow_approved(ledger, list(findings))
        self.assertEqual(len(outcomes), 2)
        for outcome, line in zip(outcomes, (19, 32), strict=True):
            self.assertIsInstance(outcome, style_ledger.DeclarationMoved)
            moved = cast(style_ledger.DeclarationMoved, outcome)
            self.assertEqual((moved.path, moved.line), (PARENT_FILE, line))
            row = next(row for row in ledger.held_findings(style_ledger.AllHeldFindings()) if row.finding_id == moved.finding.finding_id)
            original = before[row.finding_id]
            self.assertEqual((row.state, row.before, row.after, row.text, row.ticket),
                             (original.state, original.before, original.after, original.text, original.ticket))
            self.assertIsInstance(row.follow, style_ledger.FollowedDeclaration)
        again = self.follow_approved(ledger, list(findings))
        self.assertTrue(all(isinstance(outcome, style_ledger.DeclarationInPlace) for outcome in again))

    def test_an_impl_member_inherits_only_a_proven_outside_trait_rename(self) -> None:
        for fails in (False, True):
            with self.subTest(body_fails=fails):
                with style_ledger.Ledger(self.repo, self.root / f"outside-trait-{fails}.db") as ledger:
                    source = ("pub struct Type;\ntrait OldTrait {\n    fn child(&self);\n    const ONE: u32 = 1;\n"
                              + "    const TWO: u32 = 2;\n}\nimpl OldTrait for Type {\n    fn child(&self) { 7 }\n}\n")
                    _ = self.commit_tip(ledger, {PARENT_FILE: source})
                    _, (finding_id,) = self.judge_parent(ledger, ((8, "7"),), kind="restructure")
                    target = source.replace("OldTrait", "NewTrait")
                    if fails:
                        target = target.replace("const ONE: u32 = 1", "const THREE: u32 = 3").replace("const TWO: u32 = 2", "const FOUR: u32 = 4")
                    _ = self.commit_tip(ledger, {PARENT_FILE: target})
                    before = self.held_rows(ledger.database)
                    outcome, = self.follow_approved(ledger, [finding_id])
                    if fails:
                        self.assertIsInstance(outcome, style_ledger.OutsideRenameUnresolved)
                        self.assertEqual(self.held_rows(ledger.database), before)
                        self.assertIn(style_ledger.OutsideFailure.BODY_FAILED, repr(outcome))
                    else:
                        self.assertIsInstance(outcome, style_ledger.DeclarationMoved)
                        row, = ledger.held_findings(style_ledger.AllHeldFindings())
                        self.assertEqual((row.line, row.state), (8, style_ledger.Filed(self.root / "decision.md")))
                        self.assertIsInstance(row.follow, style_ledger.FollowedDeclaration)

    def test_different_qualified_impl_types_do_not_share_an_outside_method_rename(self) -> None:
        ledger = self.ledger()
        source = ("mod one { pub struct Type; }\nmod two { pub struct Type; }\nimpl one::Type {\n"
                  + "    fn original(&self) -> u32 {\n        1\n    }\n}\n")
        _ = self.commit_tip(ledger, {PARENT_FILE: source})
        _, (finding_id,) = self.judge_parent(ledger, ((4, "fn original(&self) -> u32 {"),), kind="restructure")
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("impl one::Type", "impl two::Type").replace("fn original", "fn renamed")})
        before = self.held_rows(ledger.database)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(outcome, style_ledger.OutsideRenameUnresolved)
        self.assertEqual(self.held_rows(ledger.database), before)
        self.assertIn(style_ledger.OutsideFailure.WRONG_OWNER, repr(outcome))

    def test_removed_parameters_and_fields_do_not_inherit_an_outside_owner_rename(self) -> None:
        cases = (
            ("fn original(\n    input: u32,\n) -> u32 {\n    let retained = 1;\n    input + retained\n}\n",
             "fn renamed(\n    replacement: u32,\n) -> u32 {\n    let retained = 1;\n    replacement + retained\n}\n"),
            ("struct Original {\n    selected: u32,\n    common_one: u32,\n    common_two: u32,\n}\n",
             "struct Renamed {\n    replacement: u64,\n    common_one: u32,\n    common_two: u32,\n}\n"),
        )
        for index, (source, target) in enumerate(cases):
            with self.subTest(source=source):
                with style_ledger.Ledger(self.repo, self.root / f"outside-removed-child-{index}.db") as ledger:
                    _ = self.commit_tip(ledger, {PARENT_FILE: source})
                    _, (finding_id,) = self.judge_parent(ledger, ((2, source.splitlines()[1]),), kind="restructure")
                    _ = self.commit_tip(ledger, {PARENT_FILE: target})
                    before = self.held_rows(ledger.database)
                    outcome, = self.follow_approved(ledger, [finding_id])
                    self.assertIsInstance(outcome, style_ledger.OutsideRenameUnresolved)
                    self.assertEqual(self.held_rows(ledger.database), before)
                    self.assertIn(style_ledger.OutsideFailure.CHILD_UNPROVED, repr(outcome))

    def test_a_closure_parameter_does_not_follow_into_a_different_closure_under_lock(self) -> None:
        ledger = self.ledger()
        source = ("fn original() {\n    common_one();\n    common_two();\n    consume(move |answers| {\n"
                  + "        first(answers);\n    });\n    consume(move |other| {\n        second(other);\n"
                  + "    });\n    common_three();\n    common_four();\n}\n")
        _ = self.commit_tip(ledger, {PARENT_FILE: source})
        _, (finding_id,) = self.judge_parent(ledger, ((4, "consume(move |answers| {"),), kind="restructure")
        target = (source.replace("fn original", "fn renamed").replace("|answers|", "|removed|")
                  .replace("first(answers)", "first(removed)").replace("|other|", "|answers|")
                  .replace("second(other)", "second(answers)"))
        _ = self.commit_tip(ledger, {PARENT_FILE: target})
        before = self.held_rows(ledger.database)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(outcome, style_ledger.OutsideRenameUnresolved)
        self.assertEqual(self.held_rows(ledger.database), before)
        self.assertIn(style_ledger.OutsideFailure.CHILD_UNPROVED, repr(outcome))

    def test_a_deleted_closures_parameter_does_not_follow_into_the_surviving_call_under_lock(self) -> None:
        ledger = self.ledger()
        source = ("fn original() {\n    common_one();\n    common_two();\n    first(move |answers| {\n"
                  + "        consume(answers);\n    });\n    second(move |other| {\n        consume(other);\n"
                  + "    });\n    common_three();\n    common_four();\n}\n")
        target = ("fn renamed() {\n    common_one();\n    common_two();\n    second(move |answers| {\n"
                  + "        consume(answers);\n    });\n    common_three();\n    common_four();\n}\n")
        _ = self.commit_tip(ledger, {PARENT_FILE: source})
        _, (finding_id,) = self.judge_parent(ledger, ((4, "first(move |answers| {"),), kind="restructure")
        _ = self.commit_tip(ledger, {PARENT_FILE: target})
        before = self.held_rows(ledger.database)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(outcome, style_ledger.OutsideRenameUnresolved)
        self.assertIn(style_ledger.OutsideFailure.CHILD_UNPROVED, repr(outcome))
        self.assertEqual(self.held_rows(ledger.database), before)

    def test_a_filed_and_an_unfiled_finding_follow_a_proven_outside_rename(self) -> None:
        for index, (filed, destination) in enumerate((
            (False, PARENT_FILE), (True, PARENT_FILE), (False, MOVED_FILE), (True, MOVED_FILE),
        )):
            with self.subTest(filed=filed, destination=destination):
                ledger = style_ledger.Ledger(self.repo, self.root / f"outside-{index}.db")
                source = "fn original() {\n    first();\n    second();\n    third();\n    fourth();\n}\n"
                target = source.replace("original", "renamed").replace("third();", "changed_third();").replace("fourth();", "changed_fourth();")
                _ = self.commit_tip(ledger, {PARENT_FILE: source, MOVED_FILE: "// empty\n", TWIN_FILE: target})
                hold = self.judge_parent if filed else self.hold_parent
                _, (finding_id,) = hold(ledger, ((1, "fn original() {"),), kind="restructure", before="keep this request", after="use a type")
                original, = ledger.held_findings(style_ledger.AllHeldFindings())
                changes: dict[str, str | None] = {PARENT_FILE: "// owner moved\n", destination: target,
                                                   TWIN_FILE: target + "// changed pre-existing twin\n"}
                tip = self.commit_tip(ledger, changes)
                outcome, = self.follow_approved(ledger, [finding_id])
                self.assertIsInstance(outcome, style_ledger.DeclarationMoved)
                database = ledger.database
                ledger.close()
                with style_ledger.Ledger(self.repo, database) as reopened:
                    row, = reopened.held_findings(style_ledger.AllHeldFindings())
                    self.assertEqual((row.finding_id, row.path, row.line, row.state, row.before, row.after,
                                      row.rule_blob, row.kind, row.text, row.ticket, row.reading, row.follow),
                                     (finding_id, destination, 1, original.state, original.before, original.after,
                                      original.rule_blob, original.kind, original.text, original.ticket, original.reading,
                                      style_ledger.FollowedDeclaration(PARENT_FILE, 1, tip)))
                    before = self.held_rows(database)
                    again, = self.follow_approved(reopened, [finding_id])
                    self.assertIsInstance(again, style_ledger.DeclarationInPlace)
                    self.assertEqual(self.held_rows(database), before)

    def test_an_outside_rename_needs_every_check_and_reports_its_mapped_declaration(self) -> None:
        source = "fn original() {\n    first();\n    second();\n    third();\n}\n"
        good = source.replace("original", "renamed")
        cases: tuple[tuple[str, str, dict[str, str | None], bool], ...] = (
            ("only call uses the old name", source, {PARENT_FILE: good + "fn caller() { original(); }\n"}, True),
            ("old declaration only in another crate", source, {PARENT_FILE: good, self.sources[2]: source}, True),
            ("one of three", source, {PARENT_FILE: good.replace("second();", "different();").replace("third();", "other();")}, False),
            ("changed keyword", "struct Original {\n    first: u32,\n    second: u32,\n}\n", {PARENT_FILE: "enum Renamed {\n    First,\n    Second,\n}\n"}, False),
            ("wrong impl owner", "impl Type {\n    fn original() {\n        first();\n        second();\n    }\n}\n", {PARENT_FILE: "impl Other {\n    fn renamed() {\n        first();\n        second();\n    }\n}\n"}, False),
            ("wrong implemented trait", "impl crate::First for Type {\n    fn original() {\n        first();\n        second();\n    }\n}\n", {PARENT_FILE: "impl crate::Second for Type {\n    fn renamed() {\n        first();\n        second();\n    }\n}\n"}, False),
            ("ambiguous mapped line", source, {PARENT_FILE: "fn renamed() { first(); second(); third(); } fn another() { first(); second(); third(); }\n"}, False),
            ("empty", "fn original() {}\n", {PARENT_FILE: "fn renamed() {}\n"}, False),
            ("semicolon has no body", "type Original = u32;\nfn nearby() { first(); }\n", {PARENT_FILE: "type Renamed = u32;\nfn nearby() { first(); }\n"}, False),
            ("one-line body changed", "fn original() { first(); }\n", {PARENT_FILE: "fn renamed() { other(); }\n"}, False),
            ("repeated lines count once", "fn original() {\n    same();\n    same();\n    same();\n}\n", {PARENT_FILE: "fn renamed() {\n    same();\n    other();\n    last();\n}\n"}, False),
            ("deleted site and zero targets", source, {PARENT_FILE: None}, False),
            ("two passing files", source, {PARENT_FILE: "// removed\n", MOVED_FILE: good, TWIN_FILE: good}, False),
            ("two items in one gained file", source, {PARENT_FILE: "// removed\n", MOVED_FILE: good + good.replace("renamed", "another")}, False),
            ("different crate only", source, {PARENT_FILE: "// removed\n", self.sources[2]: good}, False),
            ("original file counts beside gained file", source, {PARENT_FILE: "// mapped away\nfn unrelated() {}\n" + good, MOVED_FILE: good}, False),
        )
        for index, (name, old, changes, follows) in enumerate(cases):
            with self.subTest(case=name):
                with style_ledger.Ledger(self.repo, self.root / f"outside-limit-{index}.db") as ledger:
                    _ = self.commit_tip(ledger, {PARENT_FILE: old, MOVED_FILE: "// empty\n", TWIN_FILE: "// empty\n",
                                               self.sources[2]: source if name == "old declaration only in another crate" else "// empty\n"})
                    site_line = 2 if name.startswith("wrong ") else 1
                    _, (finding_id,) = self.hold_parent(ledger, ((site_line, old.splitlines()[site_line - 1]),), kind="restructure")
                    _ = self.commit_tip(ledger, changes)
                    before = self.held_rows(ledger.database)
                    outcome, = self.follow_approved(ledger, [finding_id])
                    if follows:
                        self.assertIsInstance(outcome, style_ledger.DeclarationMoved)
                        if isinstance(outcome, style_ledger.DeclarationMoved):
                            self.assertEqual(outcome.path, PARENT_FILE)
                    else:
                        self.assertEqual(self.held_rows(ledger.database), before)
                        self.assertIsInstance(outcome, style_ledger.OutsideRenameUnresolved)
                        self.assertEqual(ledger.held_findings(style_ledger.AllHeldFindings())[0].state, style_ledger.Unfiled())
                        if isinstance(outcome, style_ledger.OutsideRenameUnresolved):
                            evidence = outcome.evidence
                            if name == "deleted site and zero targets":
                                self.assertIsInstance(evidence.mapped, declarations.UnmappedSite)
                                self.assertIsInstance(evidence.candidates, style_ledger.NoOutsideCandidates)
                            elif name == "ambiguous mapped line":
                                self.assertEqual(evidence.mapped, declarations.UnmappedSite(declarations.MappingFailure.AMBIGUOUS))
                            elif PARENT_FILE in changes and changes[PARENT_FILE] != "// removed\n":
                                self.assertIsInstance(evidence.mapped, declarations.MappedDeclaration)
                            if isinstance(evidence.candidates, style_ledger.OutsideCandidates):
                                paths = [item.path for item in evidence.candidates.items]
                                self.assertEqual(paths, sorted(paths))
                                if MOVED_FILE in changes:
                                    self.assertIn(MOVED_FILE, paths)
                                if name == "one of three":
                                    candidate, = evidence.candidates.items
                                    self.assertEqual((candidate.unchanged, candidate.old_nonblank_count), (1, 3))
                                    self.assertIn(style_ledger.OutsideFailure.BODY_FAILED, candidate.checks)
                                if name == "two passing files":
                                    self.assertEqual({item.path for item in evidence.candidates.items if not item.checks},
                                                     {MOVED_FILE, TWIN_FILE})
        for index, old_elsewhere in enumerate((source, "#[cfg(test)] mod tests {\n" + source + "}\n")):
            with self.subTest(old_elsewhere=old_elsewhere):
                with style_ledger.Ledger(self.repo, self.root / f"outside-old-{index}.db") as ledger:
                    _ = self.commit_tip(ledger, {PARENT_FILE: source, MOVED_FILE: old_elsewhere, TWIN_FILE: "// empty\n"})
                    _, (finding_id,) = self.judge_parent(ledger, ((1, "fn original() {"),), kind="restructure")
                    _ = self.commit_tip(ledger, {PARENT_FILE: good})
                    before = self.held_rows(ledger.database)
                    outcome, = self.follow_approved(ledger, [finding_id])
                    self.assertNotIsInstance(outcome, style_ledger.DeclarationMoved)
                    self.assertEqual(self.held_rows(ledger.database), before)

        for filed in (False, True):
            with self.subTest(preexisting_destination=True, filed=filed):
                with style_ledger.Ledger(self.repo, self.root / f"outside-preexisting-{filed}.db") as ledger:
                    _ = self.commit_tip(ledger, {PARENT_FILE: source, MOVED_FILE: good, TWIN_FILE: "// empty\n"})
                    hold = self.judge_parent if filed else self.hold_parent
                    _, (finding_id,) = hold(ledger, ((1, "fn original() {"),), kind="restructure")
                    _ = self.commit_tip(ledger, {PARENT_FILE: None, MOVED_FILE: good + "// edited twin\n"})
                    before = self.held_rows(ledger.database)
                    outcome, = self.follow_approved(ledger, [finding_id])
                    self.assertIsInstance(outcome, style_ledger.OutsideRenameUnresolved)
                    self.assertEqual(self.held_rows(ledger.database), before)
                    self.assertIn(MOVED_FILE, repr(outcome))
                    self.assertIn("declaration not gained", repr(outcome))

    def test_children_follow_only_when_their_renamed_owner_is_proven(self) -> None:
        cases = (
            ("let", "fn original() {\n    let rows = collect();\n    publish(rows);\n    finish();\n}\n", 2, "rows", True),
            ("closure", "fn original() {\n    serve(move |answers| {\n        publish(answers);\n    });\n    finish();\n}\n", 2, "answers", True),
            ("method", "struct Original {\n    value: u32,\n}\nimpl Original {\n    fn child(&self) {\n        first();\n        second();\n    }\n}\n", 5, "child", True),
            ("trait associated item", "trait Original {\n    const CHILD: u32 = 7;\n    fn child() { first(); }\n}\n", 2, "CHILD", True),
            ("child renamed", "fn original() {\n    let rows = collect();\n    publish(rows);\n    finish();\n}\n", 2, "rows", False),
            ("owner still declared", "struct Original {\n    value: u32,\n}\nimpl Original {\n    fn child(&self) { first(); }\n}\n", 5, "child", False),
            ("call belongs to function", "fn original() {\n    rows();\n    finish();\n}\n", 2, "rows", True),
            ("shadowed binding", "fn original() {\n    let rows = collect();\n    { let rows = other(); publish(rows); }\n    finish();\n}\n", 2, "rows", False),
        )
        for index, (name, source, line, child, follows) in enumerate(cases):
            for moved in (False, True):
                with self.subTest(case=name, moved=moved):
                    with style_ledger.Ledger(self.repo, self.root / f"outside-child-{index}-{moved}.db") as ledger:
                        _ = self.commit_tip(ledger, {PARENT_FILE: source, MOVED_FILE: "// empty\n", TWIN_FILE: "// empty\n"})
                        _, (finding_id,) = self.judge_parent(ledger, ((line, source.splitlines()[line - 1].strip()),),
                                                           before=child, after="a better name", kind="restructure")
                        target = source.replace("original", "renamed").replace("Original", "Renamed")
                        if name == "child renamed":
                            target = target.replace("rows", "entries")
                        if name == "owner still declared":
                            target += "struct Original;\n"
                        destination = MOVED_FILE if moved else PARENT_FILE
                        changes: dict[str, str | None] = {PARENT_FILE: "// owner moved\n", destination: target}
                        tip = self.commit_tip(ledger, changes)
                        before = self.held_rows(ledger.database)
                        outcome, = self.follow_approved(ledger, [finding_id])
                        if follows:
                            self.assertIsInstance(outcome, style_ledger.DeclarationMoved)
                            row, = ledger.held_findings(style_ledger.AllHeldFindings())
                            self.assertEqual((row.path, row.line, row.state, row.follow),
                                             (destination, line, style_ledger.Filed(self.root / "decision.md"),
                                              style_ledger.FollowedDeclaration(PARENT_FILE, line, tip)))
                        else:
                            self.assertNotIsInstance(outcome, style_ledger.DeclarationMoved)
                            self.assertEqual(self.held_rows(ledger.database), before)

    def test_outside_follow_preserves_existing_follow_authority_and_write_rechecks(self) -> None:
        ledger = self.ledger()
        source = "fn original() {\n    first();\n    second();\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: source})
        _, (finding_id,) = self.hold_parent(ledger, ((1, "fn original() {"),), kind="restructure")
        read = ledger.synced_commit()
        planned = self.commit_tip(ledger, {PARENT_FILE: source.replace("original", "renamed")})
        before = self.held_rows(ledger.database)
        with self.assertRaisesRegex(style_ledger.LedgerError, "synced tip moved"):
            _ = ledger.follow_declarations([finding_id], read, lambda _: True)
        self.assertEqual(self.held_rows(ledger.database), before)
        original_read = declarations.read_declarations
        changed = False

        def read_during_sync(text: str) -> declarations.DeclaredFile:
            nonlocal changed
            if not changed:
                changed = True
                _ = self.commit_tip(ledger, {MOVED_FILE: "fn helper() {}\n"})
            return original_read(text)

        with (mock.patch.object(style_ledger, "read_declarations", read_during_sync),
              self.assertRaisesRegex(style_ledger.LedgerError, "synced tip moved")):
            _ = ledger.follow_declarations([finding_id], planned, lambda _: True)
        self.assertEqual(self.held_rows(ledger.database), before)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(outcome, style_ledger.DeclarationMoved)

        with style_ledger.Ledger(self.repo, self.root / "outside-row-recheck.db") as other:
            _ = self.commit_tip(other, {PARENT_FILE: source, MOVED_FILE: "// empty\n", TWIN_FILE: "// empty\n"})
            _, (finding_id,) = self.hold_parent(other, ((1, "fn original() {"),), kind="restructure")
            _ = self.commit_tip(other, {PARENT_FILE: source.replace("original", "renamed")})
            judged = style_ledger.Ledger.judged_commit
            wrote = False

            def read_during_row_change(reader: style_ledger.Ledger, finding: style_ledger.HeldFinding) -> style_ledger.JudgedCommit | style_ledger.NoJudgedCommit:
                nonlocal wrote
                if not wrote:
                    wrote = True
                    with closing(sqlite3.connect(other.database)) as connection, connection:
                        _ = connection.execute("UPDATE held_findings SET text='fresh request' WHERE finding_id=?", (finding_id,))
                return judged(reader, finding)

            with mock.patch.object(style_ledger.Ledger, "judged_commit", read_during_row_change):
                outcome, = self.follow_approved(other, [finding_id])
            self.assertIsInstance(outcome, style_ledger.DeclarationMoved)
            row, = other.held_findings(style_ledger.AllHeldFindings())
            self.assertEqual(row.text, "fresh request")

    def test_live_outside_rename_examples_follow_only_the_two_retained_children(self) -> None:
        ledger = self.ledger()
        old_body = ["    let rows = collect();", "    serve(move |answers| publish(answers));"]
        old_body.extend(f"    step_{index}();" for index in range(50))
        new_body = old_body[:43] + [f"    replacement_{index}();" for index in range(9)]
        old_name = "uncached_completed_rows_arrive_before_hierarchy_discovery_is_released"
        new_name = "uncached_first_publication_respects_its_budget_and_holds_claude_rows"
        source = f"fn {old_name}() {{\n" + "\n".join(old_body) + "\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: source})
        _, ids = self.judge_parent(ledger, ((2, old_body[0].strip()), (3, old_body[1].strip())), kind="restructure")
        target = f"fn {new_name}() {{\n" + "\n".join(new_body) + "\n}\n"
        tip = self.commit_tip(ledger, {PARENT_FILE: "// relocated\n" + target})
        outcomes = self.follow_approved(ledger, list(ids))
        self.assertEqual([type(outcome) for outcome in outcomes], [style_ledger.DeclarationMoved] * 2)
        rows = ledger.held_findings(style_ledger.AllHeldFindings())
        self.assertEqual([(row.finding_id, row.line, row.state, row.follow) for row in rows],
                         [(finding_id, line + 1, style_ledger.Filed(self.root / "decision.md"),
                           style_ledger.FollowedDeclaration(PARENT_FILE, line, tip)) for finding_id, line in zip(ids, (2, 3))])
        cases = (
            ("device", "fn device_line() { Device::new() }\n", "fn total_line() { Total::new() }\n"),
            ("CPU test", "fn original() {\n" + "\n".join(f"    check_{i}();" for i in range(8)) + "\n}\n",
             "fn renamed() {\n" + "\n".join([f"    check_{i}();" for i in range(3)] + [f"    changed_{i}();" for i in range(5)]) + "\n}\n"),
            ("internal docs count", "struct CratesIoInfo {\n    /// old one\n    a: u32,\n    /// old two\n    b: u32,\n    /// old three\n    /// old four\n    c: u32,\n}\n",
             "struct CratesIoRelease {\n    /// new one\n    a: u32,\n    /// new two\n    b: u32,\n    /// new three\n    /// new four\n    c: u32,\n}\n"),
            ("changed keyword", "struct CratesIoFields {\n" + "\n".join(f"    field_{i}: u32," for i in range(9)) + "\n}\n",
             "enum CratesIoSection {\n    Value,\n}\n"),
        )
        for index, (name, old, after) in enumerate(cases):
            with self.subTest(case=name):
                with style_ledger.Ledger(self.repo, self.root / f"live-limit-{index}.db") as other:
                    _ = self.commit_tip(other, {PARENT_FILE: old, MOVED_FILE: "// empty\n", TWIN_FILE: "// empty\n"})
                    _, (finding_id,) = self.judge_parent(other, ((1, old.splitlines()[0]),), kind="restructure")
                    destination = PARENT_FILE if name == "changed keyword" else MOVED_FILE
                    changes: dict[str, str | None] = {PARENT_FILE: after} if destination == PARENT_FILE else {PARENT_FILE: "// removed\n", MOVED_FILE: after}
                    _ = self.commit_tip(other, changes)
                    before = self.held_rows(other.database)
                    outcome, = self.follow_approved(other, [finding_id])
                    self.assertIsInstance(outcome, style_ledger.OutsideRenameUnresolved)
                    self.assertEqual(self.held_rows(other.database), before)
                    self.assertIn(destination, repr(outcome))
                    if isinstance(outcome, style_ledger.OutsideRenameUnresolved):
                        if name == "changed keyword":
                            self.assertEqual(outcome.evidence.mapped, declarations.MappedDeclaration(
                                PARENT_FILE, 1, declarations.Declaration("enum", "CratesIoSection", declarations.ModuleItem()),
                            ))
                        candidates = outcome.evidence.candidates
                        if isinstance(candidates, style_ledger.OutsideCandidates):
                            counts = {(item.unchanged, item.old_nonblank_count) for item in candidates.items if item.path == destination}
                            if name == "changed keyword":
                                self.assertIn((0, 9), counts)
                            elif name == "internal docs count":
                                self.assertIn((3, 7), counts)
                            elif name == "CPU test":
                                self.assertIn((3, 8), counts)
                            elif name == "device":
                                self.assertIn((0, 1), counts)

        for index, name in enumerate(("account name", "LocalAnswers", "renders_primary_marker")):
            with self.subTest(case=name):
                with style_ledger.Ledger(self.repo, self.root / f"live-existing-{index}.db") as other:
                    if name == "renders_primary_marker":
                        old = "fn renders_primary_marker() {\n    primary();\n    label();\n}\n"
                        existing = old.replace("renders_primary_marker", "worktree_entry_label")
                        target = existing + "// absorbed into the old label function\n"
                    else:
                        old = f"struct {name.replace(' ', '_')} {{\n    value: u32,\n}}\n"
                        existing = old
                        target = old.replace(name.replace(" ", "_"), "Different")
                    _ = self.commit_tip(other, {PARENT_FILE: old, MOVED_FILE: existing, TWIN_FILE: "// empty\n"})
                    _, (finding_id,) = self.judge_parent(other, ((1, old.splitlines()[0]),), kind="restructure")
                    _ = self.commit_tip(other, {PARENT_FILE: target if name != "renders_primary_marker" else "// removed\n",
                                              MOVED_FILE: target if name == "renders_primary_marker" else existing})
                    before = self.held_rows(other.database)
                    outcome, = self.follow_approved(other, [finding_id])
                    self.assertNotIsInstance(outcome, style_ledger.DeclarationMoved)
                    self.assertEqual(self.held_rows(other.database), before)

    def test_a_test_module_glob_does_not_block_a_file_level_trait_impl(self) -> None:
        for index, destination in enumerate((PARENT_FILE, MOVED_FILE)):
            with self.subTest(destination=destination):
                ledger = style_ledger.Ledger(self.repo, self.root / f"module-glob-{index}.db")
                self.addCleanup(ledger.close)
                source = (
                    "use crate::c::Trait;\nimpl Trait for Type {\n    fn method(&self) { 7 }\n}\n"
                    "#[cfg(test)]\nmod tests { use super::*; }\n"
                )
                _ = self.commit_tip(ledger, {PARENT_FILE: source, MOVED_FILE: "pub fn helper() {}\n",
                                           TWIN_FILE: "pub trait Trait { fn method(&self); }\n"})
                _, (finding_id,) = self.judge_parent(ledger, ((3, "7"),), kind="restructure")
                changes: dict[str, str | None]
                if destination == PARENT_FILE:
                    changes = {PARENT_FILE: source + "pub fn unrelated() {}\n"}
                else:
                    changes = {PARENT_FILE: "pub fn unrelated() {}\n", MOVED_FILE: source}
                tip = self.commit_tip(ledger, changes)
                before = self.held_rows(ledger.database)
                outcome, = self.follow_approved(ledger, [finding_id])
                if destination == PARENT_FILE:
                    self.assertIsInstance(outcome, style_ledger.DeclarationInPlace)
                    self.assertEqual(self.held_rows(ledger.database), before)
                else:
                    self.assertIsInstance(outcome, style_ledger.DeclarationMoved)
                    row, = ledger.held_findings(style_ledger.HeldFindingsInState("filed"))
                    self.assertEqual((row.finding_id, row.path, row.line, row.state, row.follow),
                                     (finding_id, destination, 3, style_ledger.Filed(self.root / "decision.md"),
                                      style_ledger.FollowedDeclaration(PARENT_FILE, 3, tip)))

    def test_a_block_import_of_the_trait_name_does_not_block_the_file_level_impl(self) -> None:
        ledger = self.ledger()
        source = (
            "use other::Hittable;\nimpl Hittable<Target> for Pane {\n    fn method(&self) { 7 }\n}\n"
            "#[cfg(test)]\nmod tests { use other::Hittable; }\n"
        )
        _ = self.commit_tip(ledger, {PARENT_FILE: source})
        _, (finding_id,) = self.hold_parent(ledger, ((3, "7"),), kind="restructure")
        _ = self.commit_tip(ledger, {PARENT_FILE: source + "pub fn unrelated() {}\n"})
        before = self.held_rows(ledger.database)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(outcome, style_ledger.DeclarationInPlace)
        self.assertEqual(self.held_rows(ledger.database), before)

    def test_a_relative_trait_import_resolves_from_the_file_module(self) -> None:
        cases = (
            ("crates/alpha/src/nested/a.rs", "use super::Trait;", "crate::nested::Trait", "same file"),
            ("crates/alpha/src/nested/a.rs", "use self::inner::Trait;", "crate::nested::a::inner::Trait", "same file"),
            ("crates/alpha/src/nested/deeper/a.rs", "use super::super::Trait;", "crate::nested::Trait", "same file"),
            ("crates/alpha/src/nested/mod.rs", "use self::Trait;", "crate::nested::Trait", "same file"),
            ("crates/alpha/src/nested/a.rs", "use super::Trait;", "crate::nested::Trait", "move"),
            ("crates/alpha/src/nested/a.rs", "use super::Trait;", "crate::different::Trait", "different trait"),
            ("crates/alpha/src/nested/a.rs", "", "self::inner::Trait", "direct header"),
            ("crates/alpha/src/nested/a.rs", "", "super::Trait", "direct header"),
        )
        for index, (path, imported, rooted, action) in enumerate(cases):
            with self.subTest(path=path, imported=imported, action=action, rooted=rooted):
                ledger = style_ledger.Ledger(self.repo, self.root / f"relative-trait-{index}.db")
                self.addCleanup(ledger.close)
                trait = rooted if action == "direct header" else "Trait"
                source = f"{imported}\nimpl {trait} for Type {{\n    fn method(&self) {{ 7 }}\n}}\n"
                parents: dict[str, str | None] = {
                    "crates/alpha/src/lib.rs": "mod nested;\n",
                    "crates/alpha/src/nested/mod.rs": "mod a; mod b; mod deeper;\n",
                    "crates/alpha/src/nested/deeper/mod.rs": "mod a;\n",
                }
                parents[path] = source
                _ = self.commit_tip(ledger, parents)
                _, (finding_id,) = self.judge_parent(ledger, ((3, "7"),), path=path, kind="restructure")
                if action == "direct header":
                    absolute = "crate::nested::a::inner::Trait" if rooted.startswith("self") else "crate::nested::Trait"
                    after = source.replace(rooted, absolute)
                else:
                    after = source.replace(imported, f"use {rooted};")
                destination = "crates/alpha/src/nested/b.rs" if action == "move" else path
                changes: dict[str, str | None] = {destination: after}
                if destination != path:
                    changes[path] = "pub fn unrelated() {}\n"
                tip = self.commit_tip(ledger, changes)
                before = self.held_rows(ledger.database)
                outcome, = self.follow_approved(ledger, [finding_id])
                if action == "different trait":
                    self.assertIsInstance(outcome, (style_ledger.TraitIdentityUncertain, style_ledger.DeclarationGoneFromFile))
                    self.assertEqual(self.held_rows(ledger.database), before)
                elif action == "move":
                    self.assertIsInstance(outcome, style_ledger.DeclarationMoved)
                    row = next(row for row in ledger.held_findings(style_ledger.AllHeldFindings())
                               if row.finding_id == finding_id)
                    self.assertEqual((row.path, row.line, row.state, row.follow),
                                     (destination, 3, style_ledger.Filed(self.root / "decision.md"),
                                      style_ledger.FollowedDeclaration(path, 3, tip)))
                else:
                    self.assertIsInstance(outcome, style_ledger.DeclarationInPlace)
                    self.assertEqual(self.held_rows(ledger.database), before)

    def test_block_and_relative_import_limits_keep_the_finding(self) -> None:
        cases = (
            (PARENT_FILE, "#[cfg(test)]\nmod tests {\nuse super::*;\nimpl Trait for Type {\n    fn method(&self) { 7 }\n}\n}\n", 5),
            (PARENT_FILE, "use crate::c::*;\nimpl Trait for Type {\n    fn method(&self) { 7 }\n}\n", 3),
            (PARENT_FILE, "#[cfg(test)] use crate::c::Trait;\nimpl Trait for Type {\n    fn method(&self) { 7 }\n}\n", 3),
            (PARENT_FILE, "use super::super::Trait;\nimpl Trait for Type {\n    fn method(&self) { 7 }\n}\n", 3),
            ("crates/alpha/examples/a.rs", "use self::Trait;\nimpl Trait for Type {\n    fn method(&self) { 7 }\n}\n", 3),
            (PARENT_FILE, "mod tests {\nuse crate::c::Trait;\nimpl Trait for Type {\n    fn method(&self) { 7 }\n}\n}\n", 4),
            (PARENT_FILE, "mod tests {\nimpl self::Trait for Type {\n    fn method(&self) { 7 }\n}\n}\n", 3),
            (PARENT_FILE, "fn container() {\nuse crate::c::*;\nimpl Trait for Type {\n    fn method(&self) { 7 }\n}\n}\n", 4),
        )
        for index, (path, source, line) in enumerate(cases):
            with self.subTest(path=path, source=source):
                ledger = style_ledger.Ledger(self.repo, self.root / f"trait-import-limit-{index}.db")
                self.addCleanup(ledger.close)
                _ = self.commit_tip(ledger, {path: source, TWIN_FILE: "pub trait Trait {}\n"})
                _, (finding_id,) = self.hold_parent(ledger, ((line, "7"),), path=path, kind="restructure")
                _ = self.commit_tip(ledger, {path: source + "pub fn unrelated() {}\n"})
                before = self.held_rows(ledger.database)
                outcome, = self.follow_approved(ledger, [finding_id])
                self.assertIsInstance(outcome, style_ledger.TraitIdentityUncertain)
                self.assertEqual(self.held_rows(ledger.database), before)

    def test_same_line_built_qualifiers_keep_enclosing_aliases_uncertain(self) -> None:
        sites = (
            "mod imports { use crate::other::Other as Trait; pub fn caller() { Trait::old(); } }",
            "pub fn caller() { use crate::other::Other as Trait; Trait::old(); }",
            "pub fn caller() { Trait::old(); } mod imports { use crate::other::Other as Trait; pub fn caller() { Trait::old(); } }",
        )
        for index, site in enumerate(sites):
            with self.subTest(site=site):
                ledger = style_ledger.Ledger(self.repo, self.root / f"same-line-{index}.db")
                self.addCleanup(ledger.close)
                source = "impl crate::b::Trait for Type {\n    fn old(&self) { 7 }\n}\n"
                _ = self.commit_tip(ledger, {PARENT_FILE: source, MOVED_FILE: "pub trait Trait {}\n",
                                           TWIN_FILE: site + "\n"})
                _, (finding_id,) = self.judge_parent(ledger, ((2, "7"),), kind="restructure")
                _ = self.built_finding(ledger, TWIN_FILE, "Trait::old", "Trait::new")
                _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("fn old", "fn new")})
                before = self.held_rows(ledger.database)
                outcome, = self.follow_approved(ledger, [finding_id])
                self.assertIsInstance(outcome, style_ledger.TraitIdentityUncertain)
                self.assertEqual(self.held_rows(ledger.database), before)

    def test_unproved_module_layouts_keep_relative_trait_findings_unchanged(self) -> None:
        root = "crates/alpha/src/"
        cases: tuple[tuple[str, str, dict[str, str]], ...] = (
            ("nested/main.rs", "crate::nested::Trait", {"lib.rs": "mod nested;", "nested/mod.rs": "pub mod main;"}),
            ("nested/lib.rs", "crate::nested::Trait", {"lib.rs": "mod nested;", "nested/mod.rs": "pub mod lib;"}),
            ("bin/tool.rs", "crate::bin::tool::Trait", {}),
            ("actual.rs", "crate::actual::Trait", {"lib.rs": '#[path="actual.rs"] mod renamed;'}),
            ("missing/a.rs", "crate::missing::a::Trait", {}),
            ("guarded.rs", "crate::guarded::Trait", {"lib.rs": "#[cfg(test)] mod guarded;"}),
            ("custom.rs", "crate::custom::Trait", {"lib.rs": "mod custom;"}),
        )
        for index, (relative, rooted, parents) in enumerate(cases):
            with self.subTest(relative=relative):
                ledger = style_ledger.Ledger(self.repo, self.root / f"layout-{index}.db")
                self.addCleanup(ledger.close)
                source = "impl self::Trait for Type {\n    fn method(&self) { 7 }\n}\n"
                path = root + relative
                files: dict[str, str | None] = {root + name: text + "\n" for name, text in parents.items()}
                files[path] = source
                if relative == "custom.rs":
                    files["crates/alpha/Cargo.toml"] = ('[package]\nname="alpha"\nversion="0.1.0"\n'
                                                       '[lib]\npath="src/custom.rs"\n')
                _ = self.commit_tip(ledger, files)
                _, (finding_id,) = self.hold_parent(ledger, ((2, "7"),), path=path, kind="restructure")
                _ = self.commit_tip(ledger, {path: source.replace("self::Trait", rooted)})
                before = self.held_rows(ledger.database)
                outcome, = self.follow_approved(ledger, [finding_id])
                self.assertIsInstance(outcome, style_ledger.TraitIdentityUncertain)
                self.assertEqual(self.held_rows(ledger.database), before)

    def test_a_nested_main_module_does_not_move_a_finding_to_its_parent_trait(self) -> None:
        ledger = self.ledger()
        root = "crates/alpha/src/"
        path = root + "nested/main.rs"
        destination = root + "destination.rs"
        source = "impl self::Trait for Type {\n    fn method(&self) { 7 }\n}\n"
        _ = self.commit_tip(ledger, {root + "lib.rs": "mod nested; mod destination;\n",
                                   root + "nested/mod.rs": "pub mod main;\n", path: source})
        _, (finding_id,) = self.judge_parent(ledger, ((2, "7"),), path=path, kind="restructure")
        _ = self.commit_tip(ledger, {path: "pub fn unrelated() {}\n",
                                   destination: source.replace("self::Trait", "crate::nested::Trait")})
        before = self.held_rows(ledger.database)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(outcome, style_ledger.TraitIdentityUncertain)
        self.assertEqual(self.held_rows(ledger.database), before)

    def test_a_plain_main_module_chain_keeps_relative_shortcuts_identity(self) -> None:
        ledger = self.ledger()
        root = "crates/alpha/src/"
        path = root + "tui/keys/output.rs"
        destination = root + "tui/keys/other.rs"
        source = "use super::Shortcuts;\nimpl Shortcuts for Pane {\n    fn method(&self) { 7 }\n}\n"
        _ = self.commit_tip(ledger, {
            root + "lib.rs": None,
            root + "main.rs": "mod tui;\n",
            root + "tui/mod.rs": "mod keys;\n",
            root + "tui/keys.rs": "mod output; mod other; pub trait Shortcuts {}\n",
            path: source,
            destination: "pub fn unrelated() {}\n",
        })
        _, (finding_id,) = self.judge_parent(ledger, ((3, "7"),), path=path, kind="restructure")
        _ = self.commit_tip(ledger, {path: "pub fn unrelated() {}\n", destination: source.replace(
            "super::Shortcuts", "crate::tui::keys::Shortcuts")})
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(outcome, style_ledger.DeclarationMoved)
        row = next(row for row in ledger.held_findings(style_ledger.AllHeldFindings()) if row.finding_id == finding_id)
        self.assertEqual((row.path, row.line, row.state), (destination, 3, style_ledger.Filed(self.root / "decision.md")))

    def restructured_parent(self, ledger: style_ledger.Ledger) -> str:
        """A filed restructure finding on the field line of struct Parent in a.rs."""
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT})
        _, (finding_id,) = self.judge_parent(ledger, ((2, "old: u32,"),), kind="restructure")
        return finding_id

    def test_a_finding_whose_item_a_built_rename_renamed_follows_the_new_name_in_its_file(self) -> None:
        ledger = self.ledger()
        finding_id = self.restructured_parent(ledger)
        _ = self.built_finding(ledger, PARENT_FILE, "Parent", "Renamed")
        tip = self.commit_tip(ledger, {PARENT_FILE: PARENT.replace("Parent", "Renamed")})
        filed = ledger.held_findings(style_ledger.HeldFindingsInState("filed"))[0]
        moved, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(moved, style_ledger.DeclarationMoved(
            filed, declarations.Declaration("struct", "Renamed", declarations.ModuleItem()), PARENT_FILE, 2))
        followed, = ledger.held_findings(style_ledger.HeldFindingsInState("filed"))
        self.assertEqual((followed.finding_id, followed.path, followed.line, followed.state, followed.follow),
                         (finding_id, PARENT_FILE, 2, style_ledger.Filed(self.root / "decision.md"),
                          style_ledger.FollowedDeclaration(PARENT_FILE, 2, tip)))
        # A second run reads the finding at its follow commit.
        before = self.held_rows(ledger.database)
        in_place, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(in_place, style_ledger.DeclarationInPlace(followed))
        self.assertEqual(self.held_rows(ledger.database), before)

    def test_a_method_whose_impl_type_a_built_type_finding_renamed_follows_the_renamed_owner(self) -> None:
        ledger = self.ledger()
        method = "pub enum Parent {\n    A,\n}\n\nimpl Parent {\n    pub fn consume(&self) {}\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: method})
        _, (finding_id,) = self.judge_parent(ledger, ((6, "pub fn consume(&self) {}"),), kind="type")
        _ = self.built_finding(ledger, PARENT_FILE, "enum Parent { A }", "enum Renamed { A }", kind="type")
        _ = self.commit_tip(ledger, {PARENT_FILE: method.replace("Parent", "Renamed")})
        filed = ledger.held_findings(style_ledger.HeldFindingsInState("filed"))[0]
        moved, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(moved, style_ledger.DeclarationMoved(
            filed, declarations.Declaration("fn", "consume", declarations.ImplOrTraitItem("Renamed")), PARENT_FILE, 6))

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

    def test_a_binding_rename_inside_a_renamed_fn_follows_the_fn_and_stays_filed(self) -> None:
        ledger = self.ledger()
        finding_id = self.renamed_binding(ledger, PARENT_FILE, "old", "new")
        followed, = ledger.held_findings(style_ledger.HeldFindingsInState("filed"))
        self.assertEqual((followed.finding_id, followed.state), (finding_id, style_ledger.Filed(self.root / "decision.md")))

    def test_a_built_rename_at_a_call_site_in_the_same_crate_moves_the_finding(self) -> None:
        ledger = self.ledger()
        _ = self.renamed_binding(ledger, MOVED_FILE, "a::old", "a::new")

    def test_a_built_rename_whose_after_adds_a_note_moves_the_finding_to_the_head_name(self) -> None:
        ledger = self.ledger()
        _ = self.renamed_binding(ledger, PARENT_FILE, "old", "new; rename the connected satellites")

    def test_an_unfiled_finding_whose_item_a_built_rename_renamed_follows_and_is_not_withdrawn(self) -> None:
        ledger = self.ledger()
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT})
        _, (finding_id,) = self.hold_parent(ledger, ((2, "old: u32,"),), kind="restructure")
        _ = self.built_finding(ledger, PARENT_FILE, "Parent", "Renamed")
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT.replace("Parent", "Renamed")})
        unfiled = ledger.held_findings(style_ledger.HeldFindingsInState("unfiled"))[0]
        moved, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(moved, style_ledger.DeclarationMoved(
            unfiled, declarations.Declaration("struct", "Renamed", declarations.ModuleItem()), PARENT_FILE, 2))
        followed, = ledger.held_findings(style_ledger.HeldFindingsInState("unfiled"))
        self.assertEqual((followed.finding_id, followed.state), (finding_id, style_ledger.Unfiled()))

    def test_a_finding_whose_renamed_item_two_files_gained_is_left(self) -> None:
        ledger = self.ledger()
        finding_id = self.restructured_parent(ledger)
        _ = self.built_finding(ledger, PARENT_FILE, "Parent", "Renamed")
        renamed = PARENT.replace("Parent", "Renamed")
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub fn other() {}\n", MOVED_FILE: renamed, TWIN_FILE: renamed})
        filed = ledger.held_findings(style_ledger.HeldFindingsInState("filed"))[0]
        self.assert_rename_left(ledger, finding_id, style_ledger.DeclaredInSeveralFiles(
            filed, declarations.Declaration("struct", "Renamed", declarations.ModuleItem()), (MOVED_FILE, TWIN_FILE)))

    def assert_gone_from_file(self, ledger: style_ledger.Ledger, finding_id: str, approved: Callable[[Path], bool]) -> None:
        """The declaration step leaves the filed finding on struct Parent gone from its file and writes nothing."""
        filed = ledger.held_findings(style_ledger.HeldFindingsInState("filed"))[0]
        before = self.held_rows(ledger.database)
        left, = ledger.follow_declarations([finding_id], ledger.synced_commit(), approved).outcomes
        self.assertEqual(left, style_ledger.DeclarationGoneFromFile(
            filed, declarations.Declaration("struct", "Parent", declarations.ModuleItem())))
        self.assertEqual(self.held_rows(ledger.database), before)

    def test_a_finding_whose_item_a_rename_of_an_unapproved_decision_renamed_is_left(self) -> None:
        ledger = self.ledger()
        finding_id = self.restructured_parent(ledger)
        _ = self.built_finding(ledger, PARENT_FILE, "Parent", "Renamed")
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT.replace("Parent", "Renamed")})
        self.assert_gone_from_file(ledger, finding_id, lambda path: path != self.root / "built.md")

    def test_a_finding_whose_item_a_built_rename_in_another_crate_renamed_is_left(self) -> None:
        ledger = self.ledger()
        finding_id = self.restructured_parent(ledger)
        _ = self.built_finding(ledger, "crates/beta/src/other.rs", "Parent", "Renamed")
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT.replace("Parent", "Renamed")})
        self.assert_gone_from_file(ledger, finding_id, lambda _: True)

    def test_a_finding_whose_item_two_built_renames_name_differently_is_left(self) -> None:
        ledger = self.ledger()
        finding_id = self.restructured_parent(ledger)
        _ = self.built_finding(ledger, PARENT_FILE, "Parent", "Renamed")
        _ = self.built_finding(ledger, PARENT_FILE, "Parent", "Other")
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT.replace("Parent", "Renamed")})
        self.assert_gone_from_file(ledger, finding_id, lambda _: True)

    def test_an_unfiled_rename_whose_own_rename_a_built_twin_did_is_still_withdrawn(self) -> None:
        ledger = self.ledger()
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT})
        _, (finding_id,) = self.hold_parent(ledger, ((1, "pub struct Parent {"),), before="Parent", after="Renamed")
        _ = self.built_finding(ledger, PARENT_FILE, "Parent", "Renamed")
        tip = self.commit_tip(ledger, {PARENT_FILE: PARENT.replace("Parent", "Renamed")})
        removed, = self.follow_approved(ledger, [finding_id])
        self.assertEqual((type(removed), removed.finding.finding_id), (style_ledger.DeclarationRemoved, finding_id))
        withdrawn = next(finding for finding in ledger.held_findings(style_ledger.AllHeldFindings()) if finding.finding_id == finding_id)
        self.assertEqual(withdrawn.state, style_ledger.Withdrawn(style_ledger.NoDecision(), style_ledger.DeclarationGone(tip)))

    def test_a_filed_rename_whose_built_twin_renamed_its_item_is_built_not_followed(self) -> None:
        ledger = self.ledger()
        finding_id = self.renamed_parent(ledger)
        _ = self.built_finding(ledger, PARENT_FILE, "Parent", "Renamed")
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT.replace("Parent", "Renamed")})
        renamed, = self.follow_approved(ledger, [finding_id])
        self.assertEqual((type(renamed), renamed.finding.finding_id), (style_ledger.DeclarationRenamed, finding_id))
        built = next(finding for finding in ledger.held_findings(style_ledger.AllHeldFindings()) if finding.finding_id == finding_id)
        self.assertEqual(built.state, style_ledger.Built(self.root / "decision.md"))

    def test_a_rename_follow_whose_built_rename_loses_its_approval_before_the_lock_stays_filed(self) -> None:
        ledger = self.ledger()
        finding_id = self.restructured_parent(ledger)
        _ = self.built_finding(ledger, PARENT_FILE, "Parent", "Renamed")
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT.replace("Parent", "Renamed")})
        # The answer is approved at the plan, then not at the recheck under the write lock, nor after.
        answers = iter((True,))
        self.assert_gone_from_file(ledger, finding_id, lambda _: next(answers, False))

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

    def test_a_qualified_built_rename_of_another_types_method_does_not_follow_onto_a_same_named_method(self) -> None:
        ledger = self.ledger()
        finding_id = self.restructured_method(ledger)
        _ = self.built_finding(ledger, MOVED_FILE, "Other::new", "Other::create")
        # Parent::new is gone, and impl Parent gains an unrelated create.
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub struct Parent;\n\nimpl Parent {\n    pub fn create(seed: u32) -> u32 {\n        seed\n    }\n}\n",
                                     MOVED_FILE: "pub struct Other;\n\nimpl Other {\n    pub fn create() {}\n}\n"})
        self.assert_left(ledger, finding_id, declarations.Declaration("fn", "new", declarations.ImplOrTraitItem("Parent")))

    def test_a_qualified_built_rename_of_another_types_method_does_not_stop_the_owner_rename_follow(self) -> None:
        ledger = self.ledger()
        finding_id = self.restructured_method(ledger)
        _ = self.built_finding(ledger, PARENT_FILE, "Parent", "Renamed")
        _ = self.built_finding(ledger, MOVED_FILE, "Other::new", "Other::create")
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub struct Renamed;\n\nimpl Renamed {\n    pub fn new(seed: u32) -> u32 {\n        seed\n    }\n}\n",
                                     MOVED_FILE: "pub struct Other;\n\nimpl Other {\n    pub fn create() {}\n}\n"})
        filed = ledger.held_findings(style_ledger.HeldFindingsInState("filed"))[0]
        moved, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(moved, style_ledger.DeclarationMoved(
            filed, declarations.Declaration("fn", "new", declarations.ImplOrTraitItem("Renamed")), PARENT_FILE, 5))

    def test_a_built_variant_rename_of_the_same_name_does_not_stop_the_type_rename_follow(self) -> None:
        ledger = self.ledger()
        finding_id = self.restructured_parent(ledger)
        _ = self.built_finding(ledger, PARENT_FILE, "Parent", "Renamed")
        _ = self.built_finding(ledger, MOVED_FILE, "Kind::Parent", "Kind::Child")
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT.replace("Parent", "Renamed"), MOVED_FILE: "pub enum Kind {\n    Child,\n}\n"})
        filed = ledger.held_findings(style_ledger.HeldFindingsInState("filed"))[0]
        moved, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(moved, style_ledger.DeclarationMoved(
            filed, declarations.Declaration("struct", "Renamed", declarations.ModuleItem()), PARENT_FILE, 2))

    def renamed_method(self, ledger: style_ledger.Ledger, method: str) -> tuple[str, style_ledger.HeldFinding]:
        """A filed rename of Parent::old to Parent::new at fn old in impl Parent, a built rename of Parent to Renamed,
        and a tip that renames Parent and gives its method the name method."""
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub struct Parent;\n\nimpl Parent {\n    pub fn old() {}\n}\n"})
        _, (finding_id,) = self.judge_parent(ledger, ((4, "pub fn old() {}"),))
        _ = self.built_finding(ledger, PARENT_FILE, "Parent", "Renamed")
        _ = self.commit_tip(ledger, {PARENT_FILE: f"pub struct Renamed;\n\nimpl Renamed {{\n    pub fn {method}() {{}}\n}}\n"})
        filed = next(finding for finding in ledger.held_findings(style_ledger.AllHeldFindings()) if finding.finding_id == finding_id)
        return finding_id, filed

    def test_a_filed_method_rename_whose_impl_type_a_built_rename_renamed_is_built_at_the_renamed_owner(self) -> None:
        ledger = self.ledger()
        finding_id, filed = self.renamed_method(ledger, "new")
        renamed, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(renamed, style_ledger.DeclarationRenamed(
            filed, declarations.Declaration("fn", "old", declarations.ImplOrTraitItem("Parent")),
            declarations.Declaration("fn", "new", declarations.ImplOrTraitItem("Renamed")), PARENT_FILE, 4))
        built = next(finding for finding in ledger.held_findings(style_ledger.AllHeldFindings()) if finding.finding_id == finding_id)
        self.assertEqual(built.state, style_ledger.Built(self.root / "decision.md"))

    def test_a_filed_method_rename_not_yet_applied_follows_its_impl_type_rename_and_stays_filed(self) -> None:
        ledger = self.ledger()
        finding_id, filed = self.renamed_method(ledger, "old")
        moved, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(moved, style_ledger.DeclarationMoved(
            filed, declarations.Declaration("fn", "old", declarations.ImplOrTraitItem("Renamed")), PARENT_FILE, 4))
        followed = next(finding for finding in ledger.held_findings(style_ledger.AllHeldFindings()) if finding.finding_id == finding_id)
        self.assertEqual(followed.state, style_ledger.Filed(self.root / "decision.md"))

    def test_a_filed_finding_whose_renamed_item_moved_into_another_crate_is_left(self) -> None:
        ledger = self.ledger()
        finding_id = self.restructured_parent(ledger)
        _ = self.built_finding(ledger, PARENT_FILE, "Parent", "Renamed")
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub fn other() {}\n", "crates/beta/src/other.rs": PARENT.replace("Parent", "Renamed")})
        filed = ledger.held_findings(style_ledger.HeldFindingsInState("filed"))[0]
        self.assert_rename_left(ledger, finding_id, style_ledger.DeclaredInAnotherCrate(
            filed, declarations.Declaration("struct", "Renamed", declarations.ModuleItem()), "crates/beta/src/other.rs"))

    def test_a_method_whose_impl_type_two_built_renames_name_differently_is_left(self) -> None:
        ledger = self.ledger()
        method = "pub enum Parent {\n    A,\n}\n\nimpl Parent {\n    pub fn consume(&self) {}\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: method})
        _, (finding_id,) = self.judge_parent(ledger, ((6, "pub fn consume(&self) {}"),), kind="type")
        _ = self.built_finding(ledger, PARENT_FILE, "enum Parent { A }", "enum First { A }", kind="type")
        _ = self.built_finding(ledger, PARENT_FILE, "enum Parent { A }", "enum Second { A }", kind="type")
        # Both new names are declared, so a step that picked one would follow it.
        _ = self.commit_tip(ledger, {PARENT_FILE: method.replace("Parent", "First") + "\n" + method.replace("Parent", "Second")})
        self.assert_left(ledger, finding_id, declarations.Declaration("fn", "consume", declarations.ImplOrTraitItem("Parent")))

    def test_a_built_type_finding_that_changes_more_than_the_name_gives_no_rename(self) -> None:
        ledger = self.ledger()
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub fn f(x: u8) -> u8 {\n    x\n}\n"})
        _, (finding_id,) = self.judge_parent(ledger, ((2, "x"),), kind="restructure")
        _ = self.built_finding(ledger, PARENT_FILE, "fn f(x: u8)", "fn g(x: u16)", kind="type")
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub fn g(x: u16) -> u16 {\n    x\n}\n"})
        self.assert_left(ledger, finding_id, declarations.Declaration("fn", "f", declarations.ModuleItem()))

    def test_a_finding_whose_item_two_built_renames_renamed_in_turn_follows_the_last_name(self) -> None:
        ledger = self.ledger()
        finding_id = self.restructured_parent(ledger)
        _ = self.built_finding(ledger, PARENT_FILE, "Parent", "Middle")
        _ = self.built_finding(ledger, PARENT_FILE, "Middle", "Renamed")
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT.replace("Parent", "Renamed")})
        filed = ledger.held_findings(style_ledger.HeldFindingsInState("filed"))[0]
        moved, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(moved, style_ledger.DeclarationMoved(
            filed, declarations.Declaration("struct", "Renamed", declarations.ModuleItem()), PARENT_FILE, 2))

    def test_a_finding_whose_built_renames_come_back_to_its_name_follows_the_name_before_the_return(self) -> None:
        ledger = self.ledger()
        finding_id = self.restructured_parent(ledger)
        _ = self.built_finding(ledger, PARENT_FILE, "Parent", "Middle")
        _ = self.built_finding(ledger, PARENT_FILE, "Middle", "Parent")
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT.replace("Parent", "Middle")})
        filed = ledger.held_findings(style_ledger.HeldFindingsInState("filed"))[0]
        moved, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(moved, style_ledger.DeclarationMoved(
            filed, declarations.Declaration("struct", "Middle", declarations.ModuleItem()), PARENT_FILE, 2))

    def assert_method_rename_built_in_place(self, ledger: style_ledger.Ledger, finding_id: str) -> None:
        """The step builds the filed rename of Parent::old to Parent::new at fn new in impl Parent in a.rs."""
        filed = next(finding for finding in ledger.held_findings(style_ledger.AllHeldFindings()) if finding.finding_id == finding_id)
        renamed, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(renamed, style_ledger.DeclarationRenamed(
            filed, declarations.Declaration("fn", "old", declarations.ImplOrTraitItem("Parent")),
            declarations.Declaration("fn", "new", declarations.ImplOrTraitItem("Parent")), PARENT_FILE, 4))

    def test_a_filed_method_rename_is_built_when_a_built_rename_renamed_another_type_of_its_owners_name(self) -> None:
        ledger = self.ledger()
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub struct Parent;\n\nimpl Parent {\n    pub fn old() {}\n}\n",
                                     MOVED_FILE: "pub struct Parent;\n"})
        _, (finding_id,) = self.judge_parent(ledger, ((4, "pub fn old() {}"),))
        # The built rename renames the Parent of b.rs only.
        _ = self.built_finding(ledger, MOVED_FILE, "Parent", "Renamed")
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub struct Parent;\n\nimpl Parent {\n    pub fn new() {}\n}\n",
                                     MOVED_FILE: "pub struct Renamed;\n"})
        self.assert_method_rename_built_in_place(ledger, finding_id)

    def test_a_filed_method_rename_is_built_when_a_later_commit_put_back_a_built_rename_of_its_owner(self) -> None:
        ledger = self.ledger()
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub struct Parent;\n\nimpl Parent {\n    pub fn old() {}\n}\n"})
        _, (finding_id,) = self.judge_parent(ledger, ((4, "pub fn old() {}"),))
        _ = self.built_finding(ledger, PARENT_FILE, "Parent", "Renamed")
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub struct Parent;\n\nimpl Parent {\n    pub fn new() {}\n}\n"})
        self.assert_method_rename_built_in_place(ledger, finding_id)

    def test_a_method_whose_self_path_a_built_rename_renamed_follows_the_new_name(self) -> None:
        ledger = self.ledger()
        method = "pub struct Parent;\n\nimpl Parent {\n    pub fn old() {\n        let x = 1;\n    }\n    pub fn caller() {\n        Self::old();\n    }\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: method})
        _, (finding_id,) = self.judge_parent(ledger, ((5, "let x = 1;"),), kind="restructure")
        _ = self.built_finding(ledger, PARENT_FILE, "Self::old", "Self::new", line=8)
        _ = self.commit_tip(ledger, {PARENT_FILE: method.replace("old", "new")})
        filed = ledger.held_findings(style_ledger.HeldFindingsInState("filed"))[0]
        moved, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(moved, style_ledger.DeclarationMoved(
            filed, declarations.Declaration("fn", "new", declarations.ImplOrTraitItem("Parent")), PARENT_FILE, 5))

    def test_a_module_path_rename_of_a_fn_in_another_module_does_not_follow_onto_it(self) -> None:
        ledger = self.ledger()
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub fn helper() {\n    let x = 1;\n}\n", MOVED_FILE: "pub fn helper() {}\n"})
        _, (finding_id,) = self.judge_parent(ledger, ((2, "let x = 1;"),), kind="restructure")
        _ = self.built_finding(ledger, MOVED_FILE, "b::helper", "b::assist")
        # a::helper is gone, and only b::helper has the new name.
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub fn other() {}\n", MOVED_FILE: "pub fn assist() {}\n"})
        self.assert_left(ledger, finding_id, declarations.Declaration("fn", "helper", declarations.ModuleItem()))

    def test_a_module_path_rename_follows_a_fn_in_the_mod_rs_of_that_module(self) -> None:
        ledger = self.ledger()
        module_file = "crates/alpha/src/b/mod.rs"
        function = "pub fn old() {\n    let x = 1;\n}\n"
        _ = self.commit_tip(ledger, {module_file: function})
        _, (finding_id,) = self.judge_parent(ledger, ((2, "let x = 1;"),), kind="restructure", path=module_file)
        _ = self.built_finding(ledger, PARENT_FILE, "b::old", "b::new")
        _ = self.commit_tip(ledger, {module_file: function.replace("fn old", "fn new")})
        filed = ledger.held_findings(style_ledger.HeldFindingsInState("filed"))[0]
        moved, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(moved, style_ledger.DeclarationMoved(
            filed, declarations.Declaration("fn", "new", declarations.ModuleItem()), module_file, 2))

    def test_a_module_qualified_type_rename_follows_an_impl_in_another_file(self) -> None:
        ledger = self.ledger()
        method = "use crate::b::App;\n\nimpl App {\n    pub fn run(&self) {\n        let x = 1;\n    }\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: method, MOVED_FILE: "pub struct App;\n"})
        _, (finding_id,) = self.judge_parent(ledger, ((5, "let x = 1;"),), kind="restructure")
        _ = self.built_finding(ledger, PARENT_FILE, "b::App", "b::Application")
        _ = self.commit_tip(ledger, {PARENT_FILE: method.replace("App", "Application"),
                                     MOVED_FILE: "pub struct Application;\n"})
        filed = ledger.held_findings(style_ledger.HeldFindingsInState("filed"))[0]
        moved, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(moved, style_ledger.DeclarationMoved(
            filed, declarations.Declaration("fn", "run", declarations.ImplOrTraitItem("Application")), PARENT_FILE, 5))

    def test_a_self_qualified_rename_does_not_follow_a_method_of_another_type(self) -> None:
        ledger = self.ledger()
        parent = "pub struct Parent;\n\nimpl Parent {\n    pub fn old() {\n        let x = 1;\n    }\n}\n"
        other = "pub struct Other;\n\nimpl Other {\n    pub fn old() {}\n    pub fn caller() {\n        Self::old();\n    }\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: parent, MOVED_FILE: other})
        _, (finding_id,) = self.judge_parent(ledger, ((5, "let x = 1;"),), kind="restructure")
        _ = self.built_finding(ledger, MOVED_FILE, "Self::old", "Self::new", line=6)
        # Parent loses old and gains an unrelated constructor. The Self path names Other alone.
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub struct Parent;\n\nimpl Parent {\n    pub fn new() -> Self {\n        Self\n    }\n}\n",
                                     MOVED_FILE: other.replace("old", "new")})
        self.assert_left(ledger, finding_id, declarations.Declaration("fn", "old", declarations.ImplOrTraitItem("Parent")))

    def test_a_crate_qualified_rename_follows_a_function_in_the_library_root(self) -> None:
        ledger = self.ledger()
        root_file = "crates/alpha/src/lib.rs"
        function = "pub fn helper() {\n    let x = 1;\n}\n"
        _ = self.commit_tip(ledger, {root_file: function})
        _, (finding_id,) = self.judge_parent(ledger, ((2, "let x = 1;"),), kind="restructure", path=root_file)
        _ = self.built_finding(ledger, PARENT_FILE, "crate::helper", "crate::assist")
        _ = self.commit_tip(ledger, {root_file: function.replace("helper", "assist")})
        filed = ledger.held_findings(style_ledger.HeldFindingsInState("filed"))[0]
        moved, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(moved, style_ledger.DeclarationMoved(
            filed, declarations.Declaration("fn", "assist", declarations.ModuleItem()), root_file, 2))

    def test_a_missing_item_with_no_built_rename_skips_the_rename_follow(self) -> None:
        ledger = self.ledger()
        finding_id = self.restructured_parent(ledger)
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub fn other() {}\n"})
        before = self.held_rows(ledger.database)
        with mock.patch.object(ledger, "_followed_rename", side_effect=AssertionError("no built rename to follow")):
            left, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(left, style_ledger.OutsideRenameUnresolved)
        self.assertEqual(self.held_rows(ledger.database), before)

    def test_withdraw_duplicates_is_idempotent_refuses_an_open_ticket_and_the_held_list_names_the_duplicate(self) -> None:
        rule, issue, ids = self.decision_findings()
        ledger = self.ledger()
        bare = [ledger.hold(rule, path, 5, "required change", "rename", "Old", "New", self.blob(rule), "outside").finding_id
                for path in self.sources[:2]]
        for finding_id in bare:
            ledger.file_held(finding_id, issue)
        duplicates = [style_ledger.BareDuplicate(finding_id, qualified, issue) for finding_id, qualified in zip(bare, ids)]
        ledger.ticket_add("names-1", "names", "proposal", "names", "crates/alpha", [self.sources[1]], [rule],
                          proposal=str(issue), finding_ids=(bare[1],))
        before = self.held_rows(ledger.database)
        with self.assertRaisesRegex(style_ledger.LedgerError, re.escape(f"{bare[1]}: build ticket names-1 still holds the finding")):
            ledger.withdraw_duplicates(duplicates)
        self.assertEqual(self.held_rows(ledger.database), before)
        ledger.withdraw_duplicates(duplicates[:1])
        withdrawn = self.held_rows(ledger.database)
        ledger.withdraw_duplicates(duplicates[:1])
        self.assertEqual(self.held_rows(ledger.database), withdrawn)
        held = {finding.finding_id: finding.state for finding in ledger.proposal_findings(issue)}
        self.assertEqual((held[bare[0]], held[bare[1]]),
                         (style_ledger.Withdrawn(issue, style_ledger.DuplicateOf(ids[0])), style_ledger.Filed(issue)))
        listed = {cast(str, finding["finding_id"]): finding
                  for finding in cast(list[dict[str, object]], json.loads(self.cli("held", "list", "--json").stdout))}
        self.assertEqual((listed[bare[0]]["withdrawn_by"], listed[bare[0]]["duplicate_of"]), (0, ids[0]))
        self.assertNotIn("duplicate_of", listed[bare[1]])
        lines = {line.split("\t")[0]: line.split("\t")[3:] for line in self.cli("held", "list").stdout.splitlines()}
        self.assertEqual((lines[bare[0]], lines[bare[1]]), (["withdrawn", f"duplicate of {ids[0]}"], ["filed"]))

    def test_a_person_reading_stays_on_the_row_and_the_held_list_shows_it_by_state(self) -> None:
        rule_id = self.rule("read")
        self.set_tip(self.initial_tip)
        not_applied, checked, applied = (self.hold_finding(rule_id, line) for line in (1, 2, 3))
        proposal = self.root / "issues/rename.md"
        ledger = self.ledger()
        for finding in (not_applied, checked, applied):
            ledger.file_held(finding, proposal)
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE held_findings SET state='built'")
        ledger.return_to_filed({not_applied: style_ledger.ReadByPerson("nate"), checked: style_ledger.NotReadByPerson()})
        ledger.record_person_applied({applied: style_ledger.ReadByPerson("ana")})
        with self.assertRaisesRegex(style_ledger.LedgerError, f"{checked}: finding is not built"):
            ledger.record_person_applied({checked: style_ledger.ReadByPerson("ana")})
        held = {finding.finding_id: finding for finding in ledger.proposal_findings(proposal)}
        self.assertEqual([(held[finding].state, held[finding].reading) for finding in (not_applied, checked, applied)],
                         [(style_ledger.Filed(proposal), style_ledger.ReadByPerson("nate")),
                          (style_ledger.Filed(proposal), style_ledger.NotReadByPerson()),
                          (style_ledger.Built(proposal), style_ledger.ReadByPerson("ana"))])
        shown = {not_applied: "filed again by nate: a person read the code", checked: "",
                 applied: "applied, read by ana: a person read the code"}
        listed = cast(list[dict[str, object]], json.loads(self.cli("held", "list", "--json").stdout))
        self.assertEqual({finding["finding_id"]: finding["reading"] for finding in listed}, shown)
        lines = {line.split("\t")[0]: line.split("\t")[4:] for line in self.cli("held", "list").stdout.splitlines()}
        self.assertEqual(lines, {finding: [text] if text else [] for finding, text in shown.items()})
        # A later state change keeps the reading.
        ledger.waive_held(not_applied, style_ledger.WaiveByPerson("nate"))
        self.assertEqual(ledger.proposal_findings(proposal)[0].reading, style_ledger.ReadByPerson("nate"))

    def test_decision_migration_eleven_preserves_version_ten_findings_and_tickets(self) -> None:
        rule = self.rule("read")
        database = self.root / "version-ten.sqlite"
        with closing(sqlite3.connect(database)) as connection:
            for number, migration in sorted(style_ledger.MIGRATIONS.items()):
                if number <= 10:
                    _ = connection.executescript(migration)
            _ = connection.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
            _ = connection.execute("INSERT INTO schema_version VALUES (10)")
            _ = connection.execute("INSERT INTO held_findings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                   ("legacy", rule, self.sources[0], 2, "change", "rename", "old", "new", self.blob(rule),
                                    "read-1", "filed", str(self.root / "issue.md"), "created", "updated"))
            _ = connection.execute("INSERT INTO tickets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                   ("read-1", "read", "read", "names", "", "crates/alpha", json.dumps(list(self.sources[:2])), "[]",
                                    json.dumps([rule]), json.dumps({rule: self.blob(rule)}), "assigned", "refs/heads/main", "", "created", "updated", "", "", "[]"))
            connection.commit()
        with style_ledger.Ledger(self.repo, database) as ledger:
            finding, = ledger.proposal_findings(self.root / "issue.md")
            self.assertEqual((finding.finding_id, finding.before, finding.after, finding.ticket), ("legacy", "old", "new", "read-1"))
            self.assertIsInstance(finding.build_ticket, style_ledger.NoBuildTicket)
            self.assertEqual(ledger.ticket_open("read")[0]["files"], list(self.sources[:2]))
            ledger.record_candidate("read-1", self.sources[0], 2, "Check this idea")
            self.assertEqual(ledger.rule_candidates()[0].idea, "Check this idea")

    def test_file_scope_verdict_crate_clean_tracks_only_its_file_and_rule(self) -> None:
        rule = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        ledger.record(rule, self.sources[0], self.initial_tip, self.blob(rule), "clean",
                      input_scope=style_ledger.DeclaredInputScope.FILE)
        self.record(rule, self.sources[1], self.initial_tip)
        self.write(self.sources[1], "pub fn sibling_changed() {}\n")
        self.set_tip(self.commit("Change sibling"))
        self.assertEqual(self.states(rule)[self.sources[0]], "current")
        self.assertEqual(self.states(rule)[self.sources[1]], "stale")
        self.assertEqual(next(row for row in ledger.rule_counts(style_ledger.WholeRepository())
                              if row.rule_id == rule).current, 1)
        self.write(self.sources[0], "pub fn own_file_changed() {}\n")
        tip = self.commit("Change judged file")
        self.set_tip(tip)
        self.assertEqual(self.states(rule)[self.sources[0]], "stale")
        ledger.record(rule, self.sources[0], tip, self.blob(rule), "clean",
                      input_scope=style_ledger.DeclaredInputScope.FILE)
        _ = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n# Revised rule\n")
        self.assertEqual(self.states(rule)[self.sources[0]], "stale")

    def test_file_scope_verdict_module_exception_tracks_only_its_file_and_rule(self) -> None:
        rule = self.rule("structure", "tier: D\nfamily: modules\n")
        path, sibling, _, base = self.structure_module()
        self.set_tip(base)
        ledger = self.ledger()
        ledger.record(rule, path, base, self.blob(rule), "exception",
                      input_scope=style_ledger.DeclaredInputScope.FILE)
        self.record(rule, sibling, base, "exception")
        self.write(sibling, "pub fn sibling_changed() {}\n")
        self.set_tip(self.commit("Change module sibling"))
        self.assertEqual(self.states(rule)[path], "current")
        self.assertEqual(self.states(rule)[sibling], "stale")
        self.write(path, "pub fn own_file_changed() {}\n")
        tip = self.commit("Change module file")
        self.set_tip(tip)
        self.assertEqual(self.states(rule)[path], "stale")
        ledger.record(rule, path, tip, self.blob(rule), "exception",
                      input_scope=style_ledger.DeclaredInputScope.FILE)
        _ = self.rule("structure", "tier: D\nfamily: modules\n# Revised rule\n")
        self.assertEqual(self.states(rule)[path], "stale")

    def test_file_scope_verdict_rejects_other_verdicts(self) -> None:
        rule = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        ledger = self.ledger()
        for verdict in ("fixed", "waived"):
            with self.subTest(verdict=verdict), self.assertRaisesRegex(style_ledger.LedgerError, verdict):
                ledger.record(rule, self.sources[0], self.initial_tip, self.blob(rule), verdict,
                              input_scope=style_ledger.DeclaredInputScope.FILE)
        with closing(sqlite3.connect(ledger.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM judgments").fetchone(), (0,))

    def test_scoped_verdict_without_input_scope_is_refused_on_crate_and_module_cells(self) -> None:
        read = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        structure = self.rule("structure", "tier: D\nfamily: modules\n")
        module_path, _, _, base = self.structure_module()
        ledger = self.ledger()
        refusal = "say --input-scope file \\(the verdict rests on this file alone; .*--input-scope rule \\("
        for rule, path in ((read, self.sources[0]), (structure, module_path)):
            for verdict in style_ledger.SCOPED_VERDICTS:
                with self.subTest(rule=rule, verdict=verdict), self.assertRaisesRegex(style_ledger.LedgerError, refusal):
                    ledger.record(rule, path, base, self.blob(rule), verdict)
        result = self.cli("record", "--rule", read, "--path", self.sources[0], "--commit", base,
                          "--rule-blob", self.blob(read), "--verdict", "clean", "--ticket", "outside", "--worker", "test", success=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"{read}: {self.sources[0]}: say --input-scope file", result.stderr)
        self.assertIn("or --input-scope rule", result.stderr)
        with closing(sqlite3.connect(ledger.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM judgments").fetchone(), (0,))

    def test_scoped_verdicts_record_with_either_declared_scope(self) -> None:
        read = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        structure = self.rule("structure", "tier: D\nfamily: modules\n")
        module_path, _, _, base = self.structure_module()
        ledger = self.ledger()
        recorded: list[tuple[str, str, str, str]] = []
        for rule, path in ((read, self.sources[0]), (structure, module_path)):
            _ = ledger.hold(rule, path, 1, "Required change", "restructure", "before", "after", self.blob(rule), "outside")
            for verdict in style_ledger.SCOPED_VERDICTS:
                for scope in style_ledger.DeclaredInputScope:
                    ledger.record(rule, path, base, self.blob(rule), verdict, ticket=f"{verdict}-{scope}",
                                  input_scope=scope)
                    recorded.append((rule, verdict, scope.value, self.git("rev-parse", f"{base}:{path}")
                                     if scope == style_ledger.DeclaredInputScope.FILE else ""))
        with closing(sqlite3.connect(ledger.database)) as connection:
            rows = cast(list[tuple[str, str, str, str, str]], connection.execute(
                "SELECT rule,verdict,declared_scope,input_scope,input_fingerprint FROM judgments ORDER BY id").fetchall())
        self.assertEqual([(rule, verdict, declared, fingerprint if declared == "file" else "")
                          for rule, verdict, declared, _, fingerprint in rows], recorded)
        self.assertEqual({(declared, scope) for _, _, declared, scope, _ in rows},
                         {("file", "file"), ("rule", "crate"), ("rule", "module")})

    def test_file_scoped_held_verdict_stays_current_after_a_crate_change(self) -> None:
        rule = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        findings: dict[str, style_ledger.HeldFinding] = {}
        for path, scope in ((self.sources[0], style_ledger.DeclaredInputScope.FILE),
                            (self.sources[1], style_ledger.DeclaredInputScope.RULE)):
            findings[path] = ledger.hold(rule, path, 1, "Rename value", "rename", "value", "amount", self.blob(rule), "outside")
            ledger.record(rule, path, self.initial_tip, self.blob(rule), "held", ticket="outside", input_scope=scope)
        self.write("crates/alpha/src/added.rs", "pub fn added() {}\n")
        self.set_tip(self.commit("Add a file to the crate"))
        self.assertEqual({path: state for path, state in self.states(rule).items() if path in findings},
                         {self.sources[0]: "current", self.sources[1]: "stale"})
        self.assertEqual(next(row for row in ledger.rule_counts(style_ledger.WholeRepository())
                              if row.rule_id == rule).held, 1)
        outcomes = ledger.decline_findings((finding.finding_id for finding in findings.values()), "declined", style_ledger.WaiveByPerson("user"))
        self.assertIsInstance(outcomes[findings[self.sources[0]].finding_id], style_ledger.CellWaived)
        self.assertIsInstance(outcomes[findings[self.sources[1]].finding_id], style_ledger.InputChangedSinceJudged)

    def test_local_rule_cell_requires_an_input_scope_like_a_catalog_rule(self) -> None:
        local_dir = self.repo / "docs/style"
        local_dir.mkdir(parents=True)
        _ = (local_dir / "local.md").write_text("---\ntier: C\nfamily: names\ninput_scope: crate\n---\n# Local\n")
        rule = "local:docs/style/local.md"
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        with self.assertRaisesRegex(style_ledger.LedgerError, f"{rule}: {self.sources[0]}: say --input-scope file"):
            ledger.record(rule, self.sources[0], self.initial_tip, self.blob(rule), "clean")
        ledger.record(rule, self.sources[0], self.initial_tip, self.blob(rule), "clean",
                      input_scope=style_ledger.DeclaredInputScope.FILE)
        ledger.record(rule, self.sources[1], self.initial_tip, self.blob(rule), "clean",
                      input_scope=style_ledger.DeclaredInputScope.RULE)
        self.write("crates/alpha/src/added.rs", "pub fn added() {}\n")
        self.set_tip(self.commit("Add a file to the crate"))
        self.assertEqual({path: state for path, state in self.states(rule).items() if path in self.sources[:2]},
                         {self.sources[0]: "current", self.sources[1]: "stale"})

    def test_cells_without_file_scope_record_without_an_input_scope(self) -> None:
        read = self.rule("read")
        crate_read = self.rule("crate-read", "tier: C\nfamily: types\ninput_scope: crate\n")
        manifest = self.rule("manifest", "tier: D\nfamily: modules\ninput_scope: manifest\n")
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        ledger = self.ledger()
        cells: tuple[tuple[str, str, style_ledger.Verdict], ...] = (
            (read, self.sources[0], "clean"), (manifest, "crates/alpha", "held"),
            (structure, "crates/alpha", "exception"),
            (crate_read, self.sources[0], "fixed"), (crate_read, self.sources[1], "waived"),
        )
        _ = ledger.hold(manifest, "crates/alpha", 1, "Change manifest", "restructure", "before", "after", self.blob(manifest), "outside")
        for rule, path, verdict in cells:
            ledger.record(rule, path, self.initial_tip, self.blob(rule), verdict)
        with closing(sqlite3.connect(ledger.database)) as connection:
            rows = cast(list[tuple[str, str, str]], connection.execute(
                "SELECT rule,path,declared_scope FROM judgments ORDER BY id").fetchall())
        self.assertEqual(rows, [(rule, path, "rule") for rule, path, _ in cells])

    def test_file_scope_verdict_rejects_manifest_crate_and_excluded_paths(self) -> None:
        manifest = self.rule("manifest", "tier: D\nfamily: modules\ninput_scope: manifest\n")
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        read = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        ledger = self.ledger()
        for rule, path in ((manifest, "crates/alpha"), (manifest, self.sources[0]),
                           (structure, "crates/alpha"), (read, "crates/alpha/vendor/embedded.rs")):
            with self.subTest(rule=rule, path=path), self.assertRaises(style_ledger.LedgerError):
                ledger.record(rule, path, self.initial_tip, self.blob(rule), "clean",
                              input_scope=style_ledger.DeclaredInputScope.FILE)

    def test_file_scope_verdict_already_file_scope_preserves_behavior(self) -> None:
        rule = self.rule("read")
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        ledger.record(rule, self.sources[0], self.initial_tip, self.blob(rule), "clean",
                      input_scope=style_ledger.DeclaredInputScope.FILE)
        self.write(self.sources[1], "pub fn sibling_changed() {}\n")
        self.set_tip(self.commit("Change sibling"))
        self.assertEqual(self.states(rule)[self.sources[0]], "current")
        self.write(self.sources[0], "pub fn own_file_changed() {}\n")
        self.set_tip(self.commit("Change judged file"))
        self.assertEqual(self.states(rule)[self.sources[0]], "stale")

        listed = self.rule("shell", "tier: B\nfamily: values\ninput_scope: crate\ncandidates:\n"
                           + "  kind: regex\n  pattern: cargo test\n  globs: ['*.sh']\n  surface: whole\n")
        script = "crates/alpha/check.sh"
        self.write(script, "cargo test\n")
        tip = self.commit("Add already file-scoped shell site")
        self.set_tip(tip)
        ledger.record(listed, script, tip, self.blob(listed), "exception",
                      input_scope=style_ledger.DeclaredInputScope.FILE)
        self.write(self.sources[0], "pub fn unrelated_change() {}\n")
        self.set_tip(self.commit("Change source outside shell cell"))
        self.assertEqual(self.states(listed), {script: "current"})
        self.write(script, "cargo test --workspace\n")
        self.set_tip(self.commit("Change shell cell"))
        self.assertEqual(self.states(listed), {script: "stale"})

    def test_file_scope_verdict_no_op_keeps_older_ancestor_current(self) -> None:
        rule = self.rule("read")
        ledger = self.ledger()
        ledger.set_tip(self.initial_tip, "main")
        ledger.record(rule, self.sources[0], self.initial_tip, self.blob(rule), "clean")
        _ = self.git("checkout", "-q", "-b", "outside")
        self.write("README.md", "Unmerged work\n")
        unmerged = self.commit("Work outside the synced branch")
        ledger.record(rule, self.sources[0], unmerged, self.blob(rule), "exception",
                      input_scope=style_ledger.DeclaredInputScope.FILE)
        cell = next(cell for cell in ledger.cells(rule=rule) if cell.path == self.sources[0])
        self.assertEqual(cell.state, "current")

    def test_file_scope_verdict_cli_passes_declaration_and_persists_file_fingerprint(self) -> None:
        rule = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        self.set_tip(self.initial_tip)
        _ = self.cli("record", "--rule", rule, "--path", self.sources[0], "--commit", self.initial_tip,
                     "--rule-blob", self.blob(rule), "--verdict", "exception", "--ticket", "outside",
                     "--worker", "test", "--input-scope", "file")
        with closing(sqlite3.connect(self.ledger().database)) as connection:
            row = cast(tuple[str, str], connection.execute("SELECT declared_scope,input_fingerprint FROM judgments").fetchone())
        self.assertEqual(row, ("file", self.git("rev-parse", f"{self.initial_tip}:{self.sources[0]}")))
        self.write(self.sources[1], "pub fn sibling_changed() {}\n")
        self.set_tip(self.commit("Change sibling"))
        status = cast(LedgerStatus, json.loads(self.cli("status", "--json").stdout))
        cell = next(cell for cell in status["cells"] if cell["rule"] == rule and cell["path"] == self.sources[0])
        self.assertEqual(cell["state"], "current")

    def test_rule_reads_stays_current_after_unrelated_crate_and_module_changes(self) -> None:
        crate_rule = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        module_rule = self.rule("structure", "tier: D\nfamily: modules\n")
        module_path, _, _, base = self.structure_module()
        self.set_tip(base)
        for rule, path in ((crate_rule, self.sources[0]), (module_rule, module_path)):
            _ = self.ledger().hold(rule, path, 1, "Required change", "restructure", "before", "after",
                                   self.blob(rule), "outside")
        self.write("crates/alpha/src/added.rs", "pub fn added() {}\n")
        self.write("crates/alpha/src/theme/added.rs", "pub fn added() {}\n")
        self.set_tip(self.commit("Change unrelated crate and module files"))
        for rule, path in ((crate_rule, self.sources[0]), (module_rule, module_path)):
            for verdict in style_ledger.SCOPED_VERDICTS:
                with self.subTest(rule=rule, verdict=verdict):
                    self.record_reads(rule, path, base, (self.sources[1],), verdict=verdict, ticket=verdict)
                    self.assertEqual(self.states(rule)[path], "current")

    def test_rule_reads_becomes_stale_when_own_file_changes(self) -> None:
        rule = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        self.set_tip(self.initial_tip)
        self.record_reads(rule, self.sources[0], self.initial_tip, (self.sources[1],))
        self.assertEqual(self.states(rule)[self.sources[0]], "current")
        self.write(self.sources[0], "pub fn changed() {}\n")
        self.set_tip(self.commit("Change judged file"))
        self.assertEqual(self.states(rule)[self.sources[0]], "stale")

    def test_rule_reads_becomes_stale_when_read_file_changes(self) -> None:
        rule = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        self.set_tip(self.initial_tip)
        self.record_reads(rule, self.sources[0], self.initial_tip, (self.sources[1],))
        self.assertEqual(self.states(rule)[self.sources[0]], "current")
        self.write(self.sources[1], "pub fn changed() {}\n")
        self.set_tip(self.commit("Change read file"))
        self.assertEqual(self.states(rule)[self.sources[0]], "stale")

    def test_rule_reads_becomes_stale_when_read_file_is_deleted(self) -> None:
        rule = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        self.set_tip(self.initial_tip)
        self.record_reads(rule, self.sources[0], self.initial_tip, (self.sources[1],))
        self.assertEqual(self.states(rule)[self.sources[0]], "current")
        (self.repo / self.sources[1]).unlink()
        self.set_tip(self.commit("Delete read file"))
        self.assertEqual(self.states(rule)[self.sources[0]], "stale")

    def test_rule_reads_awaiting_merge_uses_only_named_files(self) -> None:
        rule = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        _ = self.cli("set-tip", "--tip", self.initial_tip, "--style-branch", "style/main")
        _ = self.git("checkout", "-q", "-b", "style/main")
        self.write(self.sources[0], "pub fn checked() {}\n")
        checked = self.commit("Change judged file on style branch")
        self.record_reads(rule, self.sources[0], checked, (self.sources[1],))
        self.write("crates/alpha/src/added.rs", "pub fn unrelated() {}\n")
        style_tip = self.commit("Advance style branch unrelated file")
        self.assertEqual(self.states(rule)[self.sources[0]], "awaiting merge")
        self.set_tip(style_tip)
        self.assertEqual(self.states(rule)[self.sources[0]], "current")
        self.write(self.sources[1], "pub fn changed() {}\n")
        _ = self.commit("Advance style branch read file")
        self.set_tip(self.initial_tip)
        self.assertEqual(self.states(rule)[self.sources[0]], "stale")

    def test_rule_reads_judged_at_base_uses_only_named_files(self) -> None:
        rule = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        self.set_tip(self.initial_tip)
        self.record_reads(rule, self.sources[0], self.initial_tip, (self.sources[1],))
        self.write("crates/alpha/src/added.rs", "pub fn unrelated() {}\n")
        base = self.commit("Advance unit base unrelated file")
        self.write(self.sources[1], "pub fn changed() {}\n")
        changed = self.commit("Change read file at synced tip")
        self.set_tip(changed)
        cell = next(cell for cell in self.cells(rule) if cell.path == self.sources[0])
        self.assertEqual(cell.state, "stale")
        ledger = self.ledger()
        self.assertEqual(ledger.judged_at_base((cell,), base), (cell,))
        self.assertEqual(ledger.judged_at_base((cell,), changed), ())

    def test_rule_reads_checkpoint_keeps_named_files_for_later_comparisons(self) -> None:
        rule = self.rule("read", "tier: C\nfamily: names\ninput_scope: crate\n")
        path = self.sources[0]
        ledger = self.ledger()
        ledger.set_tip(self.initial_tip, "main")
        ledger.ticket_add("names-1", "names", "read", "names", "crates/alpha", [path], [rule])
        ledger.ticket_assign("names-1", "main")
        self.assertEqual(ledger.ticket_base("names-1"), self.initial_tip)
        self.record_reads(rule, path, self.initial_tip, (self.sources[1],), ticket="names-1")
        self.write(path, "pub fn ticket_changed() {}\n")
        checkpoint = self.commit("Ticket edit")
        ledger.ticket_checkpoint("names-1", checkpoint)
        _ = ledger.ticket_merge("names-1")
        self.write("crates/alpha/src/added.rs", "pub fn unrelated() {}\n")
        ledger.set_tip(self.commit("Unrelated change after checkpoint"), "main")
        self.assertEqual({cell.path: cell.state for cell in ledger.cells(rule=rule)}[path], "current")
        self.write(self.sources[1], "pub fn changed() {}\n")
        ledger.set_tip(self.commit("Read file change after checkpoint"), "main")
        self.assertEqual({cell.path: cell.state for cell in ledger.cells(rule=rule)}[path], "stale")

    def test_rule_reads_latest_declared_scope_supersedes_old_file_verdict(self) -> None:
        rule = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        path = self.sources[0]
        self.set_tip(self.initial_tip)
        self.record(rule, path, self.initial_tip, input_scope=style_ledger.DeclaredInputScope.FILE)
        self.record_reads(rule, path, self.initial_tip, (self.sources[1],))
        self.write(self.sources[1], "pub fn changed() {}\n")
        self.set_tip(self.commit("Change named read after scope declaration"))
        self.assertEqual(self.states(rule)[path], "stale")
        self.record(rule, path, self.initial_tip, input_scope=style_ledger.DeclaredInputScope.FILE)
        self.assertEqual(self.states(rule)[path], "current")

    def test_rule_reads_requires_rule_scope_and_scoped_verdict(self) -> None:
        rule = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        for scope, verdict, message in (
            ((), "clean", "--reads requires --input-scope rule"),
            (("--input-scope", "file"), "clean", "--reads requires --input-scope rule"),
            (("--input-scope", "rule"), "fixed", "fixed: --reads requires clean, exception or held"),
            (("--input-scope", "rule"), "waived", "waived: --reads requires clean, exception or held"),
        ):
            with self.subTest(scope=scope, verdict=verdict):
                result = self.cli(
                    "record", "--rule", rule, "--path", self.sources[0], "--commit", self.initial_tip,
                    "--rule-blob", self.blob(rule), "--verdict", verdict, "--ticket", "outside", "--worker", "test",
                    *scope, "--reads", self.sources[1], success=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stderr.strip(), f"style_ledger: {message}")
        with closing(sqlite3.connect(self.ledger().database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM judgments").fetchone(), (0,))

    def test_rule_reads_rejects_file_manifest_and_crate_cells(self) -> None:
        file_rule = self.rule("file")
        manifest_rule = self.rule("manifest", "tier: D\nfamily: modules\ninput_scope: manifest\n")
        crate_rule = self.rule("crate", "tier: D\nfamily: modules\ninput_scope: file\n")
        for rule, path in ((file_rule, self.sources[0]), (manifest_rule, "crates/alpha"), (crate_rule, "crates/alpha")):
            with self.subTest(rule=rule, path=path):
                result = self.cli(
                    "record", "--rule", rule, "--path", path, "--commit", self.initial_tip,
                    "--rule-blob", self.blob(rule), "--verdict", "clean", "--ticket", "outside", "--worker", "test",
                    "--input-scope", "rule", "--reads", self.sources[1], success=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(
                    result.stderr.strip(),
                    f"style_ledger: {rule}: {path}: --reads requires a crate or module cell of an included Rust file",
                )

    def test_rule_reads_rejects_path_missing_at_recorded_commit(self) -> None:
        rule = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        missing = "crates/alpha/src/not_committed.rs"
        self.write(missing, "pub fn uncommitted() {}\n")
        result = self.cli(
            "record", "--rule", rule, "--path", self.sources[0], "--commit", self.initial_tip,
            "--rule-blob", self.blob(rule), "--verdict", "clean", "--ticket", "outside", "--worker", "test",
            "--input-scope", "rule", "--reads", missing, success=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr.strip(), f"style_ledger: {missing}: no file at {self.initial_tip}")

    def test_rule_reads_normalizes_paths_and_ignores_order_duplicates_and_own_path(self) -> None:
        rule = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        self.set_tip(self.initial_tip)
        self.record_reads(rule, self.sources[0], self.initial_tip, (self.sources[1], self.sources[2]), ticket="first")
        self.record_reads(rule, self.sources[0], self.initial_tip,
                          (self.sources[2], "./" + self.sources[1], self.sources[1], self.sources[0]),
                          ticket="second")
        with closing(sqlite3.connect(self.ledger().database)) as connection:
            rows = cast(list[tuple[str, str]], connection.execute(
                "SELECT declared_scope,input_fingerprint FROM judgments ORDER BY id").fetchall())
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], rows[1])
        self.assertEqual(rows[0][0], "rule")
        self.assertEqual(self.states(rule)[self.sources[0]], "current")

    def test_rule_reads_migration_eighteen_preserves_schema_seventeen_crate_judgments(self) -> None:
        rule = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        database = self.root / "version-seventeen.sqlite"
        with closing(sqlite3.connect(database)) as connection:
            for number, migration in sorted(style_ledger.MIGRATIONS.items()):
                if number <= 17:
                    _ = connection.executescript(migration)
            _ = connection.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
            _ = connection.execute("INSERT INTO schema_version VALUES (17)")
            _ = connection.execute(
                'INSERT INTO judgments(rule,rule_blob,input_scope,input_fingerprint,path,"commit",verdict,'
                + 'verdict_detail,ticket,worker,recorded_at,declared_scope) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (rule, self.blob(rule), "crate", self.git("rev-parse", f"{self.initial_tip}:crates/alpha"),
                 self.sources[0], self.initial_tip, "clean", "old detail", "old-ticket", "test", "2026-01-01", "rule"))
            connection.commit()
        with style_ledger.Ledger(self.repo, database) as ledger:
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute("SELECT version FROM schema_version").fetchone(), (max(style_ledger.MIGRATIONS),))
            ledger.set_tip(self.initial_tip)
            self.assertEqual({cell.path: cell.state for cell in ledger.cells(rule=rule)}[self.sources[0]], "current")
            self.write(self.sources[1], "pub fn changed() {}\n")
            ledger.set_tip(self.commit("Change old judgment crate inputs"))
            self.assertEqual({cell.path: cell.state for cell in ledger.cells(rule=rule)}[self.sources[0]], "stale")

    def test_file_scope_verdict_migration_ten_keeps_version_nine_rows_unmarked(self) -> None:
        rule = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        database = self.root / "version-nine.sqlite3"
        with closing(sqlite3.connect(database)) as connection:
            for number, migration in sorted(style_ledger.MIGRATIONS.items()):
                if number <= 9:
                    _ = connection.executescript(migration)
            _ = connection.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
            _ = connection.execute("INSERT INTO schema_version VALUES (9)")
            for index, path in enumerate(self.sources[:2]):
                _ = connection.execute(
                    'INSERT INTO judgments(rule,rule_blob,input_scope,input_fingerprint,path,"commit",verdict,'
                    + 'verdict_detail,ticket,worker,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    (rule, self.blob(rule), "crate", self.git("rev-parse", f"{self.initial_tip}:crates/alpha"),
                     path, self.initial_tip, "clean", "original detail", f"ticket-{index}", "test", "2026-01-01"))
            connection.commit()
        with style_ledger.Ledger(self.repo, database) as ledger:
            with closing(sqlite3.connect(database)) as connection:
                rows = cast(list[tuple[str, str, str]], connection.execute(
                    "SELECT path,declared_scope,verdict_detail FROM judgments ORDER BY id").fetchall())
                version = cast(tuple[int], connection.execute("SELECT version FROM schema_version").fetchone())
            self.assertEqual(version, (max(style_ledger.MIGRATIONS),))
            self.assertEqual(rows, [(path, "rule", "original detail") for path in self.sources[:2]])
            self.write(self.sources[1], "pub fn sibling_changed() {}\n")
            ledger.set_tip(self.commit("Change sibling after migration"))
            self.assertEqual({cell.path: cell.state for cell in ledger.cells(rule=rule)}[self.sources[0]], "stale")

    def test_file_scope_verdict_awaiting_merge_tracks_file_on_style_branch(self) -> None:
        rule = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        _ = self.cli("set-tip", "--tip", self.initial_tip, "--style-branch", "style/main")
        _ = self.git("checkout", "-q", "-b", "style/main")
        self.write(self.sources[0], "pub fn checked() {}\n")
        checked = self.commit("Change judged file on style branch")
        self.ledger().record(rule, self.sources[0], checked, self.blob(rule), "clean",
                             input_scope=style_ledger.DeclaredInputScope.FILE)
        self.write(self.sources[1], "pub fn sibling_changed() {}\n")
        style_tip = self.commit("Advance style branch sibling")
        self.assertEqual(self.states(rule)[self.sources[0]], "awaiting merge")
        self.set_tip(style_tip)
        self.assertEqual(self.states(rule)[self.sources[0]], "current")
        self.write(self.sources[0], "pub fn changed_again() {}\n")
        _ = self.commit("Advance style branch judged file")
        self.set_tip(self.initial_tip)
        self.assertEqual(self.states(rule)[self.sources[0]], "stale")

    def test_file_scope_verdict_latest_declaration_supersedes_both_directions(self) -> None:
        rule = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        ledger = self.ledger()
        ledger.record(rule, self.sources[0], self.initial_tip, self.blob(rule), "clean",
                      input_scope=style_ledger.DeclaredInputScope.FILE)
        ledger.record(rule, self.sources[0], self.initial_tip, self.blob(rule), "exception",
                      input_scope=style_ledger.DeclaredInputScope.RULE)
        self.write(self.sources[1], "pub fn sibling_changed() {}\n")
        tip = self.commit("Change sibling")
        self.set_tip(tip)
        self.assertEqual(self.states(rule)[self.sources[0]], "stale")
        ledger.record(rule, self.sources[0], self.initial_tip, self.blob(rule), "clean",
                      input_scope=style_ledger.DeclaredInputScope.FILE)
        self.assertEqual(self.states(rule)[self.sources[0]], "current")

    def test_file_scope_verdict_checkpoint_preserves_declaration(self) -> None:
        rule = self.rule("read", "tier: C\nfamily: names\ninput_scope: crate\n")
        path = self.sources[0]
        ledger = self.ledger()
        ledger.set_tip(self.initial_tip, "main")
        ledger.ticket_add("names-1", "names", "read", "names", "crates/alpha", [path], [rule])
        ledger.ticket_assign("names-1", "main")
        self.assertEqual(ledger.ticket_base("names-1"), self.initial_tip)
        ledger.record(rule, path, self.initial_tip, self.blob(rule), "clean", ticket="names-1",
                      input_scope=style_ledger.DeclaredInputScope.FILE)
        self.write(path, "pub fn ticket_changed() {}\n")
        checkpoint = self.commit("Ticket edit")
        ledger.ticket_checkpoint("names-1", checkpoint)
        _ = ledger.ticket_merge("names-1")
        self.write(self.sources[1], "pub fn later_sibling_changed() {}\n")
        ledger.set_tip(self.commit("Change sibling after checkpoint"), "main")
        self.assertEqual(self.states(rule)[path], "current")
        with closing(sqlite3.connect(ledger.database)) as connection:
            rows = cast(list[tuple[str, str, str]], connection.execute(
                'SELECT "commit",declared_scope,input_fingerprint FROM judgments ORDER BY id').fetchall())
        self.assertEqual(rows, [
            (self.initial_tip, "file", self.git("rev-parse", f"{self.initial_tip}:{path}")),
            (checkpoint, "file", self.git("rev-parse", f"{checkpoint}:{path}")),
        ])

    def test_latest_held_judgments_selects_each_cells_latest_commit_and_excludes_later_non_held_verdicts(self) -> None:
        rule = self.rule("read")
        other_rule = self.rule("other-read")
        ledger = self.ledger()
        for held_rule, path in ((rule, self.sources[0]), (rule, self.sources[1]), (other_rule, self.sources[0])):
            _ = ledger.hold(held_rule, path, 1, "Required rename", "rename", "value", "amount", self.blob(held_rule), "outside")
        self.record(rule, self.sources[0], self.initial_tip, "held")
        self.record(rule, self.sources[1], self.initial_tip, "held")
        self.record(other_rule, self.sources[0], self.initial_tip, "held")
        self.write(self.sources[0], "pub fn changed() {}\n")
        tip = self.commit("Change the held file")
        self.record(rule, self.sources[0], tip, "held")
        ledger = self.ledger()
        self.assertEqual(ledger.latest_held_judgments(), {
            (rule, self.sources[0]): tip,
            (rule, self.sources[1]): self.initial_tip,
            (other_rule, self.sources[0]): self.initial_tip,
        })

        self.record(rule, self.sources[0], tip, "clean", ticket="later-clean")
        self.assertEqual(ledger.latest_held_judgments(), {
            (rule, self.sources[1]): self.initial_tip,
            (other_rule, self.sources[0]): self.initial_tip,
        })

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

    def test_structure_module_cell_ignores_other_directories_but_tracks_siblings_and_parent(self) -> None:
        rule = self.rule("structure", "tier: D\nfamily: modules\n")
        path, sibling, parent, base = self.structure_module()
        self.set_tip(base)
        self.record(rule, path, base)
        self.write("crates/alpha/src/unrelated/item.rs", "pub fn unrelated() {}\n")
        self.set_tip(self.commit("Change another module"))
        self.assertEqual(self.states(rule)[path], "current")
        for changed in (sibling, parent, path):
            with self.subTest(changed=changed):
                self.write(changed, "pub fn changed() {}\n")
                tip = self.commit("Change a module input")
                self.set_tip(tip)
                self.assertEqual(self.states(rule)[path], "stale")
                self.record(rule, path, tip)
                self.assertEqual(self.states(rule)[path], "current")
        with closing(sqlite3.connect(self.ledger().database)) as connection:
            scopes = cast(list[tuple[str]], connection.execute("SELECT DISTINCT input_scope FROM judgments").fetchall())
        self.assertEqual(scopes, [("module",)])

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

    def test_a_modules_cell_stays_current_after_a_comment_only_change_at_each_scope(self) -> None:
        file_rule, module_rule, crate_rule, _, path = self.comment_scope_rules()
        for relative in (self.sources[0], self.sources[1], path, "crates/alpha/src/theme/mod.rs", "crates/alpha/src/theme.rs"):
            self.comment_only_change(relative)
        self.set_tip(self.commit("Change comments only"))
        for rule, judged in ((file_rule, self.sources[0]), (module_rule, path), (crate_rule, self.sources[0])):
            with self.subTest(rule=rule):
                self.assertEqual(self.states(rule)[judged], "current")

    def test_a_names_cell_goes_stale_after_a_comment_only_change(self) -> None:
        _, _, _, names_rule, _ = self.comment_scope_rules()
        self.comment_only_change(self.sources[0])
        self.set_tip(self.commit("Change comments only"))
        self.assertEqual(self.states(names_rule)[self.sources[0]], "stale")

    def test_a_modules_cell_goes_stale_after_a_code_inner_doc_file_set_or_manifest_change(self) -> None:
        file_rule, module_rule, crate_rule, _, path = self.comment_scope_rules()
        sibling = "crates/alpha/src/theme/mod.rs"
        library = (self.repo / self.sources[0]).read_text()
        changes: tuple[tuple[str, str, str, str, str | None], ...] = (
            ("code", module_rule, path, sibling, "pub fn changed() {}\n"),
            ("inner line doc", module_rule, path, sibling, "//! Theme values.\npub fn changed() {}\n"),
            ("inner block doc", file_rule, self.sources[0], self.sources[0], "/*! The alpha crate. */\n" + library),
            ("added file", module_rule, path, "crates/alpha/src/theme/added.rs", "pub fn added() {}\n"),
            ("manifest", crate_rule, self.sources[0], "crates/alpha/Cargo.toml",
             (self.repo / "crates/alpha/Cargo.toml").read_text() + "# A note.\n"),
            ("removed file", crate_rule, self.sources[0], self.sources[1], None),
            ("removed module file", module_rule, path, "crates/alpha/src/theme.rs", None),
        )
        tip = self.git("rev-parse", "HEAD")
        for label, rule, judged, relative, content in changes:
            with self.subTest(change=label):
                self.record(rule, judged, tip)
                if content is None:
                    (self.repo / relative).unlink()
                else:
                    self.write(relative, content)
                tip = self.commit(f"Change {label}")
                self.set_tip(tip)
                self.assertEqual(self.states(rule)[judged], "stale")

    def test_a_modules_cell_goes_stale_when_an_inner_doc_moves_to_another_module_block(self) -> None:
        file_rule, _, _, _, _ = self.comment_scope_rules()
        blocks = "mod alpha {{\n{}    pub fn a() {{}}\n}}\nmod beta {{\n{}    pub fn b() {{}}\n}}\n"
        doc = "    //! Alpha facade.\n"
        self.write(self.sources[0], blocks.format(doc, ""))
        tip = self.commit("Document the alpha block")
        self.set_tip(tip)
        self.record(file_rule, self.sources[0], tip)
        self.write(self.sources[0], blocks.format("", doc))
        self.set_tip(self.commit("Move the inner doc to the beta block"))
        self.assertEqual(self.states(file_rule)[self.sources[0]], "stale")

    def test_a_modules_cell_goes_stale_after_a_mode_or_submodule_change_beside_a_comment_edit(self) -> None:
        _, _, crate_rule, _, _ = self.comment_scope_rules()
        submodule = self.repo / "crates/alpha/vendored"
        submodule.mkdir()

        def submodule_commit(message: str) -> None:
            _ = subprocess.run(["git", "-C", str(submodule), "-c", "user.name=Ledger tests", "-c",
                                "user.email=ledger-tests@example.invalid", "commit", "-q", "--allow-empty", "-m", message],
                               check=True, capture_output=True)

        _ = subprocess.run(["git", "-C", str(submodule), "init", "-q"], check=True, capture_output=True)
        submodule_commit("First")
        tip = self.commit("Add a submodule")
        self.assertIn("160000 commit", self.git("ls-tree", tip, "crates/alpha/vendored"))
        changes: tuple[tuple[str, Callable[[], None]], ...] = (
            ("submodule pointer", lambda: submodule_commit("Second")),
            ("manifest mode", lambda: (self.repo / "crates/alpha/Cargo.toml").chmod(0o755)),
            ("Rust file mode", lambda: (self.repo / self.sources[1]).chmod(0o755)),
        )
        for label, change in changes:
            with self.subTest(change=label):
                self.set_tip(tip)
                self.record(crate_rule, self.sources[0], tip)
                change()
                self.comment_only_change(self.sources[0])
                tip = self.commit(f"Change {label} and a comment")
                self.set_tip(tip)
                self.assertEqual(self.states(crate_rule)[self.sources[0]], "stale")

    def test_a_modules_judgment_with_reads_stays_current_after_a_comment_only_change_in_a_read(self) -> None:
        rule = self.rule("structure", "tier: D\nfamily: modules\n")
        path, _, _, base = self.structure_module()
        self.set_tip(base)
        self.record_reads(rule, path, base, (self.sources[1],))
        self.comment_only_change(self.sources[1])
        self.set_tip(self.commit("Change comments in a read file"))
        self.assertEqual(self.states(rule)[path], "current")

    def test_legacy_structure_crate_key_carries_over_without_rewriting_the_row(self) -> None:
        rule = self.rule("structure", "tier: D\nfamily: modules\n")
        path, sibling, _, base = self.structure_module()
        self.set_tip(base)
        self.insert_legacy_structure_judgment(rule, path, base)
        self.write("crates/alpha/src/unrelated/item.rs", "pub fn unrelated() {}\n")
        self.set_tip(self.commit("Change outside the judged module"))
        self.assertEqual(self.states(rule)[path], "current")
        with closing(sqlite3.connect(self.ledger().database)) as connection:
            stored = cast(list[tuple[str, str, str]], connection.execute(
                'SELECT input_scope,input_fingerprint,"commit" FROM judgments').fetchall())
        self.assertEqual(stored, [("crate", self.git("rev-parse", f"{base}:crates/alpha"), base)])
        self.write(sibling, "pub fn sibling_changed() {}\n")
        self.set_tip(self.commit("Change a sibling after legacy judgment"))
        self.assertEqual(self.states(rule)[path], "stale")

    def test_legacy_structure_module_key_carries_over_on_the_style_branch(self) -> None:
        rule = self.rule("structure", "tier: D\nfamily: modules\n")
        path, sibling, _, base = self.structure_module()
        _ = self.cli("set-tip", "--tip", base, "--style-branch", "style/main")
        _ = self.git("checkout", "-q", "-b", "style/main")
        self.write(path, "pub fn fixed() {}\n")
        checked = self.commit("Fix module on style branch")
        self.insert_legacy_structure_judgment(rule, path, checked, "fixed")
        self.write("crates/alpha/src/unrelated/item.rs", "pub fn unrelated() {}\n")
        _ = self.commit("Change another module on style branch")
        self.assertEqual(self.states(rule)[path], "awaiting merge")
        self.write(sibling, "pub fn changed() {}\n")
        _ = self.commit("Change judged module on style branch")
        self.assertEqual(self.states(rule)[path], "stale")
        self.record(rule, path, self.git("rev-parse", "HEAD"), "fixed")
        self.write("crates/alpha/src/unrelated/item.rs", "pub fn unrelated_again() {}\n")
        _ = self.commit("Change another module after new judgment")
        self.assertEqual(self.states(rule)[path], "awaiting merge")

    def test_legacy_structure_held_judgment_carries_over_for_decline(self) -> None:
        rule = self.rule("structure", "tier: D\nfamily: modules\n")
        path, _, _, base = self.structure_module()
        self.set_tip(base)
        ledger = self.ledger()
        finding = ledger.hold(rule, path, 1, "Split module", "restructure", "before", "after", self.blob(rule), "outside")
        self.insert_legacy_structure_judgment(rule, path, base, "held")
        self.write("crates/alpha/src/unrelated/item.rs", "pub fn unrelated() {}\n")
        self.set_tip(self.commit("Change outside held module"))
        self.assertIsInstance(ledger.decline_findings((finding.finding_id,), "declined", style_ledger.WaiveByPerson("user"))[finding.finding_id], style_ledger.CellWaived)
        self.assertEqual(self.states(rule)[path], "current")

    def test_structure_row_with_an_unresolvable_commit_counts_as_changed(self) -> None:
        rule = self.rule("structure", "tier: D\nfamily: modules\n")
        path, _, _, base = self.structure_module()
        self.set_tip(base)
        ledger = self.ledger()
        finding = ledger.hold(rule, path, 1, "Split module", "restructure", "before", "after", self.blob(rule), "outside")
        self.insert_legacy_structure_judgment(rule, path, base, "held")
        with closing(sqlite3.connect(ledger.database)) as connection:
            _ = connection.execute('UPDATE judgments SET "commit"=?', ("0" * 40,))
            connection.commit()
        self.assertEqual(self.states(rule)[path], "stale")
        self.assertIsInstance(ledger.decline_findings((finding.finding_id,), "declined", style_ledger.WaiveByPerson("user"))[finding.finding_id], style_ledger.InputChangedSinceJudged)

    def test_structure_crate_and_manifest_cells_retain_their_original_keys(self) -> None:
        crate_rule = self.rule("structure", "tier: D\nfamily: modules\n")
        manifest_rule = self.rule("manifests", "tier: D\nfamily: modules\ninput_scope: manifest\n")
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        finding = ledger.hold(crate_rule, "crates/alpha", 1, "Split crate", "restructure",
                              "before", "after", self.blob(crate_rule), "outside")
        self.record(crate_rule, "crates/alpha", self.initial_tip, "held")
        self.record(manifest_rule, "crates/alpha", self.initial_tip)
        self.write(self.sources[1], "pub fn changed() {}\n")
        self.set_tip(self.commit("Change crate source"))
        with closing(sqlite3.connect(ledger.database)) as connection:
            stored = cast(list[tuple[str, str, str]], connection.execute(
                "SELECT rule,input_scope,input_fingerprint FROM judgments ORDER BY id").fetchall())
        self.assertEqual(stored[0], (crate_rule, "crate", self.git("rev-parse", f"{self.initial_tip}:crates/alpha")))
        self.assertEqual(stored[1][1], "manifest")
        self.assertEqual(self.states(manifest_rule)["crates/alpha"], "current")
        self.assertIsInstance(ledger.decline_findings((finding.finding_id,), "declined", style_ledger.WaiveByPerson("user"))[finding.finding_id], style_ledger.InputChangedSinceJudged)
        self.write("crates/alpha/Cargo.toml", (self.repo / "crates/alpha/Cargo.toml").read_text() + "\n[features]\nextra = []\n")
        self.set_tip(self.commit("Change workspace manifest"))
        self.assertEqual(self.states(manifest_rule)["crates/alpha"], "stale")

    def test_celled_structure_record_refuses_rule_path_pairs_outside_ticket_ownership(self) -> None:
        rule = self.rule("structure", "tier: D\nfamily: modules\n")
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        _ = self.cli("ticket", "add", "--id", "modules-1", "--unit", "modules", "--kind", "structure",
                     "--family", "modules", "--crate", "crates/alpha", "--files", json.dumps(list(self.sources[:2])),
                     "--rules", json.dumps([rule]), "--cells", json.dumps([[rule, self.sources[0]]]))
        ledger.ticket_assign("modules-1", "main")
        with self.assertRaisesRegex(style_ledger.LedgerError, f"{rule} {self.sources[1]} is outside ticket cells"):
            ledger.record(rule, self.sources[1], self.initial_tip, "", "clean", ticket="modules-1")
        ledger.record(rule, self.sources[0], self.initial_tip, "", "clean", ticket="modules-1",
                      input_scope=style_ledger.DeclaredInputScope.RULE)
        self.assertEqual(ledger.unjudged_ticket_cells("modules-1", "HEAD", self.in_worktree), ())
        with self.assertRaisesRegex(style_ledger.LedgerError, "different fields"):
            ledger.ticket_add("modules-1", "modules", "structure", "modules", "crates/alpha",
                              list(self.sources[:2]), [rule], cells=((rule, self.sources[1]),))

    def test_migration_nine_preserves_legacy_crate_ticket_and_does_not_cover_new_file_cells(self) -> None:
        rule = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        ledger = self.ledger()
        ledger.set_tip(self.initial_tip, "main")
        ledger.ticket_add("modules-1", "modules", "structure", "modules", "crates/alpha", list(self.sources[:2]), [rule])
        ledger.ticket_assign("modules-1", "main")
        database = ledger.database
        ledger.close()
        with closing(sqlite3.connect(database)) as connection:
            _ = connection.execute("ALTER TABLE tickets DROP COLUMN cells")
            _ = connection.execute("ALTER TABLE judgments DROP COLUMN declared_scope")
            _ = connection.execute("ALTER TABLE judgments DROP COLUMN read_files")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN waive_provenance")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN waived_by")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN build_ticket")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN moved_from")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN withdrawn_by")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN person_read_by")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN duplicate_of")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN unwaived_by")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN followed_from")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN followed_from_line")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN followed_at")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN declaration_gone_at")
            _ = connection.execute("ALTER TABLE tickets DROP COLUMN finding_ids")
            _ = connection.execute("DROP TABLE rule_candidates")
            _ = connection.execute("DROP TABLE ticket_widened_paths")
            _ = connection.execute("ALTER TABLE tickets DROP COLUMN reservation_session_dir")
            _ = connection.execute("ALTER TABLE tickets DROP COLUMN reservation_phase")
            _ = connection.execute("ALTER TABLE processed_tips DROP COLUMN reason")
            _ = connection.execute("DROP TABLE refill_results")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN built_at")
            _ = connection.execute("UPDATE schema_version SET version=8")
            connection.commit()
        migrated = self.ledger()
        self.assertEqual(migrated.ticket_open("modules")[0]["cells"], [])
        self.assertEqual(migrated.unjudged_ticket_cells("modules-1", "HEAD", self.in_worktree), (style_ledger.UnjudgedTicketCell(rule, "crates/alpha"),))
        migrated.record(rule, "crates/alpha", self.initial_tip, "", "clean", ticket="modules-1")
        migrated.ticket_checkpoint("modules-1", self.initial_tip)
        self.assertEqual(migrated.ticket_open("modules")[0]["state"], "checkpointed")
        self.assertEqual(self.states(rule), dict.fromkeys(self.sources, "open"))

    def test_structure_file_cells_track_module_inputs_and_decline_only_the_matching_file(self) -> None:
        rule = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: crate\n")
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        holds = [ledger.hold(rule, path, 1, "Split module", "restructure", "before", "after", self.blob(rule), "outside")
                 for path in self.sources[:2]]
        for path in self.sources[:2]:
            self.record(rule, path, self.initial_tip, "held")
        self.assertIsInstance(ledger.decline_findings((holds[0].finding_id,), "declined", style_ledger.WaiveByPerson("user"))[holds[0].finding_id], style_ledger.CellWaived)
        progress = next(row for row in ledger.rule_counts(style_ledger.WholeRepository()) if row.rule_id == rule)
        self.assertEqual((progress.current, progress.held), (2, 1))
        self.write(self.sources[0], "pub fn changed() {}\n")
        self.set_tip(self.commit("Change a crate input"))
        self.assertEqual(self.states(rule), {**dict.fromkeys(self.sources[:2], "stale"), **dict.fromkeys(self.sources[2:], "open")})

    def test_structure_file_scope_keys_rust_modules_and_preserves_non_rust_site_fingerprints(self) -> None:
        rule = self.rule("structure-sites", "tier: D\nfamily: modules\ninput_scope: file\ncandidates:\n"
                         + "  kind: regex\n  pattern: PLACEHOLDER\n  globs: [README.md]\n  surface: partial\n"
                         + "  uncovered: Read module ownership.\n")
        self.write("README.md", "PLACEHOLDER module documentation\n")
        tip = self.commit("Add a non-Rust structure site")
        self.set_tip(tip)
        ledger = self.ledger()
        self.assertEqual({(cell.path, cell.crate) for cell in ledger.cells(rule)},
                         {(path, "crates/alpha" if "/alpha/" in path else "crates/beta") for path in self.sources}
                         | {("README.md", ".")})
        self.record(rule, "README.md", tip)
        self.record(rule, self.sources[0], tip)
        self.write(self.sources[1], "pub fn changed() {}\n")
        self.set_tip(self.commit("Change another file"))
        self.assertEqual(self.states(rule), {**dict.fromkeys(self.sources, "open"),
                                           self.sources[0]: "stale", "README.md": "current"})

    def test_initial_universe_has_file_cells_but_no_vendor_or_process_cells(self) -> None:
        file_rule = self.rule("read")
        structure = self.rule("structure", "tier: D\nfamily: modules\n")
        listed = self.rule("listed", "tier: B\nfamily: names\npre_filter: word\n")
        process = self.rule("process", "tier: E\n")
        self.set_tip(self.initial_tip)

        self.assertEqual(self.states(file_rule), dict.fromkeys(self.sources, "open"))
        self.assertEqual(self.states(structure), dict.fromkeys(self.sources, "open"))
        self.assertEqual(self.cells(listed), [])
        self.assertEqual(self.cells(process), [])
        ledger = self.ledger()
        progress = {rule.rule_id: rule for rule in ledger.rule_counts(style_ledger.WholeRepository())}
        self.assertTrue(progress[listed].complete)
        self.assertEqual(progress[listed].total, 0)
        self.assertEqual(progress[file_rule].total, 4)
        self.assertFalse(progress[file_rule].complete)
        self.assertEqual({cell.path for cell in ledger.cells(crate="crates/alpha")}, {
            "crates/alpha/src/lib.rs", "crates/alpha/src/other.rs",
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
        _ = self.hold_finding(read, 1)
        self.record(read, self.sources[0], self.initial_tip, "held")
        self.record(read, self.sources[1], self.initial_tip)
        for path in self.sources:
            self.record(partial, path, self.initial_tip)
        _ = self.hold_finding(partial + "#uncovered", 1)
        self.record(partial + "#uncovered", self.sources[0], self.initial_tip, "held")
        self.write(self.sources[1], "pub fn value() -> u32 { 2 }\n")
        main_tip = self.commit("Make judged inputs stale")
        _ = self.cli("set-tip", "--tip", main_tip, "--style-branch", "style/main")
        _ = self.git("checkout", "-qb", "style/main")
        self.write(self.sources[2], "pub fn corrected() {}\n")
        pending_tip = self.commit("Judge a pending style correction")
        _ = self.ledger().hold(read, self.sources[2], 1, "Required rename", "rename", "corrected", "value", self.blob(read), "outside")
        self.record(read, self.sources[2], pending_tip, "held")
        ledger = self.ledger()

        with mock.patch.object(ledger, "_coverage", wraps=cast(object, getattr(ledger, "_coverage"))) as coverage:
            repository_counts = ledger.rule_counts(style_ledger.WholeRepository())
            coverage.assert_called_once_with(None, None)
        by_rule = {row.rule_id: row for row in repository_counts}
        self.assertEqual(by_rule, {
            read: style_ledger.RuleCounts(read, "C", 1, 1, 1, 1, 1, "file"),
            partial: style_ledger.RuleCounts(partial, "B", 4, 1, 3, 0, 1, "file"),
            process: style_ledger.RuleCounts(process, "E", 0, 0, 0, 0, 0, "crate"),
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
                read: style_ledger.RuleCounts(read, "C", 1, 1, 0, 0, 1, "file"),
                partial: style_ledger.RuleCounts(partial, "B", 2, 1, 1, 0, 1, "file"),
                process: by_rule[process],
            }),
            ("crates/beta", {
                read: style_ledger.RuleCounts(read, "C", 0, 0, 1, 1, 0, "file"),
                partial: style_ledger.RuleCounts(partial, "B", 2, 0, 2, 0, 0, "file"),
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
        _ = self.hold_finding(partial + "#uncovered", 1)
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

    def test_partial_structure_rule_keeps_file_cells_without_uncovered_pseudo_rule(self) -> None:
        rule_id = self.rule(
            "structure", "tier: D\nfamily: modules\ncandidates:\n  kind: regex\n"
            + "  pattern: value\n  surface: partial\n  uncovered: crate structure\n",
        )
        self.set_tip(self.initial_tip)
        ledger = self.ledger()

        self.assertEqual({site.path for site in ledger.listed_sites(rule_id)}, set(self.sources))
        self.assertEqual({(cell.rule, cell.path, cell.tier) for cell in ledger.cells()}, {
            (rule_id, path, "D") for path in self.sources
        })
        for path in self.sources:
            self.record(rule_id, path, self.initial_tip)
        progress, = ledger.rule_counts(style_ledger.WholeRepository())
        self.assertEqual((progress.rule_id, progress.current, progress.total, progress.complete),
                         (rule_id, 4, 4, True))

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
        self.assertEqual(versions, [(max(style_ledger.MIGRATIONS),)])
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

    def test_member_structure_rule_has_only_its_member_files(self) -> None:
        local_dir = self.repo / "docs" / "alpha" / "style"
        local_dir.mkdir(parents=True)
        _ = (local_dir / "structure.md").write_text("---\ntier: D\nfamily: modules\n---\n# Structure\n")
        local_id = "local:docs/alpha/style/structure.md"
        self.set_tip(self.initial_tip)

        self.assertEqual(self.states(local_id), dict.fromkeys(self.sources[:2], "open"))
        for path in self.sources[:2]:
            self.record(local_id, path, self.initial_tip)
        self.assertEqual(self.states(local_id), dict.fromkeys(self.sources[:2], "current"))

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
        _ = self.ledger().hold(rule_id, self.sources[1], 1, "Required rename", "rename", "value", "amount", self.blob(rule_id), "outside")
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
        _ = self.hold_finding(rule_id, 1, ticket="A")
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

    def test_checkpoint_restamps_own_family_verdicts_after_only_ticket_edits_and_refill_does_not_reissue(self) -> None:
        rule = self.rule("read")
        path = self.sources[0]
        ledger = self.ledger()
        ledger.set_tip(self.initial_tip, "main")
        ledger.ticket_add("names-1", "names", "read", "names", "crates/alpha", [path], [rule])
        ledger.ticket_assign("names-1", "main")
        self.assertEqual(ledger.ticket_base("names-1"), self.initial_tip)
        self.record(rule, path, self.initial_tip, "fixed", ticket="names-1")
        self.record(rule, path, self.initial_tip, ticket="another-ticket")
        self.write(path, "pub fn renamed() -> u32 { 1 }\n")
        _ = self.commit("Ticket renames the function")
        self.write(path, "pub fn renamed() -> u32 { 2 }\n")
        checkpoint = self.commit("Ticket checkpoint")

        ledger.ticket_checkpoint("names-1", checkpoint)
        _ = ledger.ticket_merge("names-1")
        ledger.set_tip(checkpoint, "main")
        self.assertEqual(self.states(rule)[path], "current")
        tickets = style_plan.candidate_tickets(
            self.repo, ledger, style_plan.BusyFiles(berth_board.Board(()), frozenset()), "names",
            base=style_plan.NoUnitBranch())
        self.assertFalse(any(path in ticket.files for ticket in tickets))
        with closing(sqlite3.connect(ledger.database)) as connection:
            rows = cast(list[tuple[str, str]], connection.execute(
                'SELECT "commit",verdict FROM judgments WHERE ticket=? ORDER BY id', ("names-1",)).fetchall())
            other_commits = cast(list[tuple[str]], connection.execute(
                'SELECT "commit" FROM judgments WHERE ticket=?', ("another-ticket",)).fetchall())
        self.assertEqual(rows, [(self.initial_tip, "fixed"), (checkpoint, "fixed")])
        self.assertEqual(other_commits, [(self.initial_tip,)])

    def record_while_stalled(self, stalled: str, write: Callable[[style_ledger.Ledger], object]) -> None:
        """A second ledger records a verdict while write waits 2 s in style_ledger.<stalled>."""
        original = cast(Callable[..., object], getattr(style_ledger, stalled))
        started = threading.Event()

        def stall(*arguments: object) -> object:
            if not started.is_set():
                started.set()
                time.sleep(2)
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
            self.assertTrue(started.wait(10))
            recorder.record(rule, self.sources[2], self.initial_tip, self.blob(rule), "clean",
                            ticket="unit-1", worker="test-worker")
            thread.join()
        self.assertEqual(failures, [])
        self.assertEqual(self.states(rule)[self.sources[2]], "current")

    def test_set_tip_holds_no_write_lock_while_it_lists_sites(self) -> None:
        listed = self.rule(
            "listed", "tier: B\nfamily: names\ninput_scope: file\ncandidates:\n"
            + "  kind: regex\n  pattern: marker\n  surface: whole\n",
        )
        self.set_tip(self.initial_tip)
        self.write(self.sources[0], "pub fn marker() {}\n")
        tip = self.commit("Add a marker")
        self.record_while_stalled("enumerate_candidates", lambda ledger: ledger.set_tip(tip))
        ledger = self.ledger()
        self.assertEqual(ledger.synced_commit(), tip)
        self.assertEqual({site.path for site in ledger.listed_sites(listed)}, {self.sources[0]})

    def test_checkpoint_holds_no_write_lock_while_it_restamps(self) -> None:
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
        self.record_while_stalled("_fingerprint", lambda writer: writer.ticket_checkpoint("names-1", checkpoint))
        self.assertEqual(ledger.ticket_open("names")[0]["checkpoint"], checkpoint)
        self.assertIn(checkpoint, {judgment.commit for judgment in ledger.latest_ticket_judgments("names-1")})

    def test_mark_built_holds_no_write_lock_while_it_checks_the_code(self) -> None:
        rule, issue, _ = self.decision_findings()
        ledger = self.ledger()
        finding = ledger.hold(rule, self.sources[0], 3, "value", "type", "u32", "u64", self.blob(rule), "outside")
        ledger.file_held(finding.finding_id, issue)
        ledger.ticket_add("names-1", "names", "proposal", "names", "crates/alpha", [self.sources[0]], [rule],
                          proposal=str(issue), finding_ids=(finding.finding_id,))
        ledger.ticket_assign("names-1", "main")
        ledger.set_tip(self.initial_tip, "main")
        ledger.ticket_checkpoint("names-1", self.initial_tip)
        _ = ledger.ticket_merge("names-1")
        self.record_while_stalled("applied_check", lambda writer: writer.mark_built(finding.finding_id))
        built = next(item for item in ledger.proposal_findings(issue) if item.finding_id == finding.finding_id)
        self.assertIsInstance(built.state, style_ledger.Built)

    def test_decline_holds_no_write_lock_while_it_checks_judged_inputs(self) -> None:
        rule = self.rule("read")
        path = self.sources[0]
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        finding = ledger.hold(rule, path, 1, "Rename value", "rename", "value", "amount", self.blob(rule), "outside")
        ledger.record(rule, path, self.initial_tip, self.blob(rule), "held", ticket="outside")
        self.record_while_stalled("_fingerprint", lambda writer: writer.decline_findings((finding.finding_id,), "declined", style_ledger.WaiveByPerson("user")))
        self.assertIsInstance(ledger.held_findings(style_ledger.AllHeldFindings())[0].state, style_ledger.Waived)
        self.assertIsInstance(ledger.newest_held_judgment(rule, path), style_ledger.NoHeldJudgment)

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

    def test_checkpoint_starts_again_when_its_judgments_change_before_the_lock(self) -> None:
        ledger, checkpoint, load = self.judge_while_loading_rules(1)
        ledger.ticket_checkpoint("names-1", checkpoint)
        self.assertEqual(load.call_count, 2)
        self.assertEqual(ledger.ticket_open("names")[0]["checkpoint"], checkpoint)
        self.assertEqual({judgment.commit for judgment in ledger.latest_ticket_judgments("names-1")}, {checkpoint})

    def test_checkpoint_refuses_after_its_judgments_change_on_every_attempt(self) -> None:
        ledger, checkpoint, load = self.judge_while_loading_rules(style_ledger.LOCK_ATTEMPTS)
        with self.assertRaisesRegex(style_ledger.ChangedBeforeLock, "names-1: the ticket changed while it became checkpointed; rerun"):
            ledger.ticket_checkpoint("names-1", checkpoint)
        self.assertEqual(load.call_count, style_ledger.LOCK_ATTEMPTS)
        self.assertEqual(ledger.ticket_open("names")[0]["state"], "assigned")

    def test_checkpoint_keeps_verdicts_stale_and_reissues_when_an_outside_commit_changed_the_path(self) -> None:
        rule = self.rule("read")
        path = self.sources[0]
        self.record(rule, path, self.initial_tip, ticket="names-1")
        self.write(path, "pub fn outside_change() {}\n")
        base = self.commit("Outside edit before ticket starts")
        ledger = self.ledger()
        ledger.set_tip(base, "main")
        ledger.ticket_add("names-1", "names", "read", "names", "crates/alpha", [path], [rule])
        ledger.ticket_assign("names-1", "main")
        self.assertEqual(ledger.ticket_base("names-1"), base)
        self.write(path, "pub fn ticket_change() {}\n")
        checkpoint = self.commit("Ticket checkpoint")

        ledger.ticket_checkpoint("names-1", checkpoint)
        _ = ledger.ticket_merge("names-1")
        ledger.set_tip(checkpoint, "main")
        self.assertEqual(self.states(rule)[path], "stale")
        tickets = style_plan.candidate_tickets(
            self.repo, ledger, style_plan.BusyFiles(berth_board.Board(()), frozenset()), "names",
            base=style_plan.NoUnitBranch())
        self.assertTrue(any(path in ticket.files for ticket in tickets))

    def test_checkpoint_does_not_restamp_a_path_changed_on_a_merged_outside_branch(self) -> None:
        rule = self.rule("read")
        path = self.sources[0]
        ledger = self.ledger()
        ledger.set_tip(self.initial_tip, "main")
        ledger.ticket_add("names-1", "names", "read", "names", "crates/alpha", [path], [rule])
        ledger.ticket_assign("names-1", "main")
        self.assertEqual(ledger.ticket_base("names-1"), self.initial_tip)
        self.record(rule, path, self.initial_tip, ticket="names-1")
        _ = self.git("checkout", "-q", "-b", "outside")
        self.write(path, "pub fn outside_change() {}\n")
        _ = self.commit("Outside branch edit")
        _ = self.git("checkout", "-q", "main")
        self.write("README.md", "Ticket work\n")
        _ = self.commit("Ticket work")
        _ = self.git("merge", "--no-ff", "-q", "outside", "-m", "Bring in outside work")
        checkpoint = self.git("rev-parse", "HEAD")
        ledger.ticket_checkpoint("names-1", checkpoint)
        ledger.set_tip(checkpoint, "main")
        self.assertEqual(self.states(rule)[path], "stale")

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

    def test_removed_cell_keeps_the_deleting_commit_and_closes_the_cell_without_a_verdict_on_code(self) -> None:
        ledger, rule, _ = self.removed_ticket()
        kept, removed = self.sources[:2]
        self.record(rule, kept, self.initial_tip, ticket="names-1")
        self.record(rule, removed, self.initial_tip, ticket="earlier-1")
        (self.repo / removed).unlink()
        deleting = self.commit("Ticket deletes the file")
        self.write("README.md", "Ticket work after the delete\n")
        checkpoint = self.commit("Ticket checkpoint")
        self.assertEqual(ledger.unjudged_ticket_cells("names-1", "HEAD", self.in_worktree), (style_ledger.UnjudgedTicketCell(rule, removed),))

        self.record_removed(ledger, rule, removed, checkpoint)

        state = f"removed by ticket names-1 at {deleting[:7]}"
        expected = style_ledger.RemovedCell(rule=rule, path=removed, commit=deleting, state=state)
        opened = cast(dict[str, object], json.loads(self.cli("ticket", "open", "--unit", "names").stdout))
        self.assertEqual(opened["removed"], [dict(expected)])
        self.assertEqual(ledger.ticket_open("names")[0]["removed"], (expected,))
        self.assertIn(f"{rule} {removed}: {state}", coverage_report.ticket_line(ledger.ticket_open("names")[0]).splitlines())
        self.assertEqual(ledger.unjudged_ticket_cells("names-1", "HEAD", self.in_worktree), ())

        ledger.ticket_checkpoint("names-1", checkpoint)
        _ = ledger.ticket_merge("names-1")
        ledger.set_tip(checkpoint, "main")
        # The restamp carries the kept verdict to the checkpoint and leaves the removed row at its deleting commit.
        self.assertEqual(self.cell_rows(ledger, removed), [(self.initial_tip, "clean"), (deleting, "removed")])
        self.assertEqual(self.cell_rows(ledger, kept), [(self.initial_tip, "clean"), (checkpoint, "clean")])
        self.assertEqual(self.states(rule), {kept: "current", self.sources[2]: "open", self.sources[3]: "open"})

        # A file at the same path later is a new cell: the verdict of the file before the delete does not judge it.
        self.write(removed, "pub fn value() -> u32 { 1 }\n")
        ledger.set_tip(self.commit("Bring the file back"), "main")
        self.assertEqual(self.states(rule)[removed], "open")

    def test_checkpoint_names_a_cell_neither_recorded_nor_removed(self) -> None:
        ledger, rule, _ = self.removed_ticket()
        for path in self.sources[:2]:
            (self.repo / path).unlink()
        checkpoint = self.commit("Ticket deletes both files")
        self.record_removed(ledger, rule, self.sources[0], checkpoint)
        # The style branch holds the delete and the synced tip does not, so the cell awaits merge; it is not current.
        self.assertEqual({path: self.states(rule)[path] for path in self.sources[:2]},
                         {self.sources[0]: "awaiting merge", self.sources[1]: "open"})
        counts = next(row for row in ledger.rule_counts(style_ledger.WholeRepository()) if row.rule_id == rule)
        self.assertEqual((counts.current, counts.awaiting_merge, counts.held), (0, 1, 0))
        refused = self.cli("ticket", "checkpoint", "--id", "names-1", "--commit", checkpoint, success=False)
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertEqual(refused.stderr.strip(), f"style_ledger: names-1: unjudged cells: {rule} {self.sources[1]}")
        self.record_removed(ledger, rule, self.sources[1], checkpoint)
        ledger.ticket_checkpoint("names-1", checkpoint)
        self.assertEqual(ledger.ticket_open("names")[0]["state"], "checkpointed")

    def test_a_verdict_recorded_after_a_removal_on_an_older_commit_does_not_judge_the_file_brought_back(self) -> None:
        ledger, rule, _ = self.removed_ticket()
        path = self.sources[1]
        original = (self.repo / path).read_text()
        (self.repo / path).unlink()
        deleting = self.commit("Ticket deletes the file")
        self.record_removed(ledger, rule, path, deleting)
        # The late verdict judges the file before the delete; the order of recording does not move the removal.
        self.record(rule, path, self.initial_tip, ticket="earlier-1")
        self.write(path, original)
        ledger.set_tip(self.commit("Bring the same file back"), "main")
        self.assertEqual(self.states(rule)[path], "open")

    def test_removed_keeps_the_deleting_commit_and_not_a_later_merge_of_other_work(self) -> None:
        ledger, rule, base = self.removed_ticket()
        path = self.sources[1]
        (self.repo / path).unlink()
        deleting = self.commit("Ticket deletes the file")
        _ = self.git("checkout", "-q", "-b", "side", base)
        self.write("README.md", "Side work\n")
        _ = self.commit("Side work keeps the file")
        _ = self.git("checkout", "-q", "main")
        # The merge differs from its second parent at the path, but its diff against its first parent deletes nothing.
        _ = self.git("merge", "--no-ff", "-q", "side", "-m", "Bring in side work")
        self.record_removed(ledger, rule, path, "HEAD")
        self.assertEqual(self.cell_rows(ledger, path), [(deleting, "removed")])

    def test_removed_records_an_own_delete_of_a_file_that_merged_in_work_edited_and_prints_its_line(self) -> None:
        ledger, rule, base = self.removed_ticket()
        path = self.sources[1]
        _ = self.git("checkout", "-q", "-b", "outside", base)
        self.write(path, "pub fn edited_outside() {}\n")
        _ = self.commit("Outside work edits the file")
        _ = self.git("checkout", "-q", "main")
        # The normal sync: a first-parent merge brings the outside edit onto the ticket branch.
        _ = self.git("merge", "--no-ff", "-q", "outside", "-m", "Bring in outside work")
        (self.repo / path).unlink()
        deleting = self.commit("Ticket deletes the file")
        recorded = self.cli("record", "--rule", rule, "--path", path, "--commit", "HEAD", "--rule-blob", self.blob(rule),
                            "--verdict", "removed", "--ticket", "names-1", "--worker", "test-worker")
        self.assertEqual(recorded.stdout, f"{rule} {path}: removed by ticket names-1 at {deleting[:7]}\n")
        self.assertEqual(self.cell_rows(ledger, path), [(deleting, "removed")])

    def test_checkpoint_names_a_removed_cell_whose_file_is_back(self) -> None:
        ledger, rule, _ = self.removed_ticket()
        kept, path = self.sources[:2]
        self.record(rule, kept, self.initial_tip, ticket="names-1")
        original = (self.repo / path).read_text()
        (self.repo / path).unlink()
        deleting = self.commit("Ticket deletes the file")
        self.record_removed(ledger, rule, path, deleting)
        self.write(path, original)
        restored = self.commit("Ticket brings the file back")
        self.assertEqual(ledger.unjudged_ticket_cells("names-1", "HEAD", self.in_worktree), (style_ledger.UnjudgedTicketCell(rule, path),))
        with self.assertRaisesRegex(style_ledger.LedgerError, f"^names-1: unjudged cells: {re.escape(rule)} {re.escape(path)}$"):
            ledger.ticket_checkpoint("names-1", restored)
        self.record(rule, path, restored, ticket="names-1")
        ledger.ticket_checkpoint("names-1", restored)
        self.assertEqual(ledger.ticket_open("names")[0]["state"], "checkpointed")

    def test_checkpoint_does_not_count_a_verdict_of_the_ticket_on_a_commit_before_its_removal(self) -> None:
        ledger, rule, _ = self.removed_ticket()
        kept, path = self.sources[:2]
        self.record(rule, kept, self.initial_tip, ticket="names-1")
        original = (self.repo / path).read_text()
        (self.repo / path).unlink()
        deleting = self.commit("Ticket deletes the file")
        self.record_removed(ledger, rule, path, deleting)
        # The ticket's newest row judges the file before the delete, so coverage reads the file brought back as open.
        self.record(rule, path, self.initial_tip, ticket="names-1")
        self.write(path, original)
        restored = self.commit("Ticket brings the file back")
        ledger.set_tip(restored, "main")
        self.assertEqual(self.states(rule)[path], "open")
        with self.assertRaisesRegex(style_ledger.LedgerError, f"^names-1: unjudged cells: {re.escape(rule)} {re.escape(path)}$"):
            ledger.ticket_checkpoint("names-1", restored)

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

    def test_checkpoint_counts_a_removal_by_another_ticket(self) -> None:
        ledger, rule, path, restored = self.removed_by_another_ticket()
        ledger.set_tip(restored, "main")
        self.assertEqual(self.states(rule)[path], "open")
        with self.assertRaisesRegex(style_ledger.LedgerError, f"^names-1: unjudged cells: {re.escape(rule)} {re.escape(path)}$"):
            ledger.ticket_checkpoint("names-1", restored)

    def test_restamp_does_not_carry_a_verdict_across_a_removal_by_another_ticket(self) -> None:
        ledger, rule, path, restored = self.removed_by_another_ticket()
        self.record(rule, path, restored, ticket="names-1")
        # The ticket's newest row judges the file before the other ticket's delete.
        self.record(rule, path, self.initial_tip, ticket="names-1")
        self.write(path, "fn edited() {}\n")
        checkpoint = self.commit("Ticket edits the file brought back")
        ledger.ticket_checkpoint("names-1", checkpoint)
        ledger.set_tip(checkpoint, "main")
        self.assertEqual(self.states(rule)[path], "stale")

    def test_restamp_rechecks_a_removal_recorded_before_the_write_lock(self) -> None:
        ledger, rule, _ = self.removed_ticket()
        kept, path = self.sources[:2]
        self.record(rule, kept, self.initial_tip, ticket="names-1")
        ledger.ticket_add("other-names-1", "other-names", "read", "names", "crates/alpha", [path], [rule])
        ledger.ticket_assign("other-names-1", "main")
        _ = ledger.ticket_base("other-names-1")
        original = (self.repo / path).read_text()
        (self.repo / path).unlink()
        deleting = self.commit("Another ticket deletes the file")
        self.write(path, original)
        restored = self.commit("The file comes back")
        self.record(rule, path, restored, ticket="names-1")
        # The ticket's newest row judges the file before the delete, which no ticket has recorded yet.
        self.record(rule, path, self.initial_tip, ticket="names-1")
        self.write(path, "fn edited() {}\n")
        checkpoint = self.commit("Ticket edits the file brought back")
        prepared = style_ledger.Ledger.checkpoint_restamps
        other = self.ledger()

        def remove_after_prepare(owner: style_ledger.Ledger, ticket: style_ledger.TicketRecord,
                                 commit: str) -> style_ledger.CheckpointRestamps | style_ledger.NoRestamps:
            restamps = prepared(owner, ticket, commit)
            if (deleting, "removed") not in self.cell_rows(other, path):
                self.record_removed(other, rule, path, deleting, ticket="other-names-1")
            return restamps

        with mock.patch.object(style_ledger.Ledger, "checkpoint_restamps", autospec=True, side_effect=remove_after_prepare):
            ledger.ticket_checkpoint("names-1", checkpoint)
        # The checkpoint passes by the verdict on the restored file, and the verdict before the delete does not carry.
        ledger.set_tip(checkpoint, "main")
        self.assertEqual(self.states(rule)[path], "stale")

    def test_restamp_rechecks_a_removal_that_takes_the_id_of_a_yielded_one(self) -> None:
        ledger, rule, _ = self.removed_ticket()
        kept, path = self.sources[:2]
        self.record(rule, kept, self.initial_tip, ticket="names-1")
        for ticket in ("other-names-1", "third-names-1"):
            ledger.ticket_add(ticket, ticket.removesuffix("-1"), "read", "names", "crates/alpha", [path], [rule])
            ledger.ticket_assign(ticket, "main")
            _ = ledger.ticket_base(ticket)
        original = (self.repo / path).read_text()
        (self.repo / path).unlink()
        first = self.commit("Another ticket deletes the file")
        self.write(path, original)
        restored = self.commit("The file comes back")
        (self.repo / path).unlink()
        second = self.commit("A third ticket deletes the file")
        self.write(path, original)
        restored_again = self.commit("The file comes back again")
        self.record(rule, path, restored_again, ticket="names-1")
        # The ticket's newest row judges the file between the two deletes.
        self.record(rule, path, restored, ticket="names-1")
        # The first removal is the newest row, so the next row can take its id once the yield deletes it.
        self.record_removed(ledger, rule, path, first, ticket="other-names-1")
        self.write(path, "fn edited() {}\n")
        checkpoint = self.commit("Ticket edits the file brought back")
        prepared = style_ledger.Ledger.checkpoint_restamps
        other = self.ledger()

        def swap_after_prepare(owner: style_ledger.Ledger, ticket: style_ledger.TicketRecord,
                               commit: str) -> style_ledger.CheckpointRestamps | style_ledger.NoRestamps:
            restamps = prepared(owner, ticket, commit)
            if (second, "removed") not in self.cell_rows(other, path):
                _ = other.ticket_yield("other-names-1", path)
                self.record_removed(other, rule, path, second, ticket="third-names-1")
            return restamps

        with mock.patch.object(style_ledger.Ledger, "checkpoint_restamps", autospec=True, side_effect=swap_after_prepare):
            ledger.ticket_checkpoint("names-1", checkpoint)
        # The checkpoint passes by the verdict after the second delete, and the verdict before it does not carry.
        ledger.set_tip(checkpoint, "main")
        self.assertEqual(self.states(rule)[path], "stale")

    def test_removed_refuses_a_file_still_present_outside_the_ticket_or_its_start(self) -> None:
        self.write("README.md", "Work before the ticket\n")
        _ = self.commit("Work before the ticket")
        ledger, rule, base = self.removed_ticket()
        path = self.sources[1]
        with self.assertRaisesRegex(style_ledger.LedgerError, f"^{re.escape(path)}: still in the tree at {base[:7]}$"):
            self.record_removed(ledger, rule, path, base)
        self.write(path, "pub fn changed() {}\n")
        edited = self.commit("Ticket edits the file")
        with self.assertRaisesRegex(style_ledger.LedgerError, f"^{re.escape(path)}: still in the tree at {edited[:7]}$"):
            self.record_removed(ledger, rule, path, edited)
        (self.repo / self.sources[2]).unlink()
        outside = self.commit("Delete a file outside the ticket")
        with self.assertRaisesRegex(style_ledger.LedgerError, f"^names-1: {re.escape(rule)} {re.escape(self.sources[2])} is outside ticket cells$"):
            self.record_removed(ledger, rule, self.sources[2], outside)
        _ = self.git("checkout", "-q", "-b", "side", self.initial_tip)
        (self.repo / path).unlink()
        side = self.commit("Delete on a branch not under the start")
        _ = self.git("checkout", "-q", "main")
        with self.assertRaisesRegex(style_ledger.LedgerError, f"^names-1: {side[:7]} is not under the ticket's start {base[:7]}$"):
            self.record_removed(ledger, rule, path, side)
        with self.assertRaisesRegex(style_ledger.LedgerError, "^removed: --reads requires clean, exception or held$"):
            ledger.record(rule, path, side, "", "removed", ticket="names-1", worker="test-worker", reads=(self.sources[0],))
        ledger.ticket_add("names-2", "names", "read", "names", "crates/alpha", [path], [rule])
        with self.assertRaisesRegex(style_ledger.LedgerError, "^names-2: no start commit, so its own commits are not known$"):
            self.record_removed(ledger, rule, path, outside, ticket="names-2")
        with self.assertRaisesRegex(style_ledger.LedgerError, "^unknown ticket other-1$"):
            self.record_removed(ledger, rule, path, outside, ticket="other-1")
        self.assertEqual(self.cell_rows(ledger, path), [])

    def test_removed_refuses_a_file_that_a_merged_in_commit_deleted(self) -> None:
        ledger, rule, _ = self.removed_ticket()
        path = self.sources[1]
        _ = self.git("checkout", "-q", "-b", "outside")
        (self.repo / path).unlink()
        _ = self.commit("Outside branch deletes the file")
        _ = self.git("checkout", "-q", "main")
        self.write("README.md", "Ticket work\n")
        _ = self.commit("Ticket work")
        _ = self.git("merge", "--no-ff", "-q", "outside", "-m", "Bring in outside work")
        merge = self.git("rev-parse", "HEAD")
        # The merge's diff against its first parent deletes the path, so the delete is not the ticket's.
        with self.assertRaisesRegex(style_ledger.LedgerError, f"^{re.escape(path)}: merge {merge[:7]} deletes it, not a commit of names-1's own$"):
            self.record_removed(ledger, rule, path, "HEAD")
        self.assertEqual(self.cell_rows(ledger, path), [])

    def test_removed_refuses_a_file_deleted_before_the_ticket_start(self) -> None:
        rule = self.rule("read")
        path = self.sources[1]
        (self.repo / path).unlink()
        base = self.commit("Delete before the ticket starts")
        ledger = self.ledger()
        ledger.set_tip(self.initial_tip, "main")
        ledger.ticket_add("names-1", "names", "read", "names", "crates/alpha", [path], [rule])
        ledger.ticket_assign("names-1", "main")
        self.assertEqual(ledger.ticket_base("names-1"), base)
        with self.assertRaisesRegex(style_ledger.LedgerError, f"^{re.escape(path)}: not a file at the ticket's start {base[:7]}$"):
            self.record_removed(ledger, rule, path, base)
        # A structure ticket's crate cell is a folder, so its removal is out of scope.
        ledger.ticket_add("modules-1", "modules", "structure", "names", "crates/alpha", [], [rule])
        ledger.ticket_assign("modules-1", "main")
        self.assertEqual(ledger.ticket_base("modules-1"), base)
        with self.assertRaisesRegex(style_ledger.LedgerError, f"^crates/alpha: not a file at the ticket's start {base[:7]}$"):
            self.record_removed(ledger, rule, "crates/alpha", base, ticket="modules-1")

    def test_removed_refuses_a_path_yielded_before_the_write_lock(self) -> None:
        ledger, rule, _ = self.removed_ticket()
        path = self.sources[1]
        (self.repo / path).unlink()
        deleting = self.commit("Ticket deletes the file")
        original = style_ledger._cell_scope  # pyright: ignore[reportPrivateUsage]

        def yield_then_scope(definition: rules.Rule, cell: str, tip: style_ledger.RepositoryTip) -> style_ledger.CellInputScope:
            with closing(sqlite3.connect(ledger.database)) as connection, connection:
                _ = connection.execute("INSERT INTO ticket_yields VALUES (?,?,?)", ("names-1", path, "now"))
            return original(definition, cell, tip)

        with (mock.patch.object(style_ledger, "_cell_scope", side_effect=yield_then_scope),
              self.assertRaisesRegex(style_ledger.LedgerError, f"^names-1: {re.escape(path)} was yielded$")):
            self.record_removed(ledger, rule, path, deleting)
        self.assertEqual(self.cell_rows(ledger, path), [])

    def test_names_checkpoint_leaves_forbidden_words_judgments_stale_on_the_edited_path(self) -> None:
        names = self.rule("read")
        forbidden = self.rule("forbidden-words", "tier: C\nfamily: values\ninput_scope: file\n")
        path = self.sources[0]
        ledger = self.ledger()
        ledger.set_tip(self.initial_tip, "main")
        ledger.ticket_add("names-1", "names", "read", "names", "crates/alpha", [path], [names, forbidden])
        ledger.ticket_assign("names-1", "main")
        self.assertEqual(ledger.ticket_base("names-1"), self.initial_tip)
        # Even a verdict recorded under this ticket is not its family's judgment.
        for rule in (names, forbidden):
            self.record(rule, path, self.initial_tip, ticket="names-1")
        self.write(path, "// A new comment needs its own word check.\npub fn renamed() {}\n")
        checkpoint = self.commit("Names checkpoint adds a comment")
        ledger.ticket_checkpoint("names-1", checkpoint)
        _ = ledger.ticket_merge("names-1")
        ledger.set_tip(checkpoint, "main")
        self.assertEqual(self.states(names)[path], "current")
        self.assertEqual(self.states(forbidden)[path], "stale")
        tickets = style_plan.candidate_tickets(
            self.repo, ledger, style_plan.BusyFiles(berth_board.Board(()), frozenset()), "values",
            base=style_plan.NoUnitBranch())
        self.assertTrue(any(path in ticket.files and forbidden in ticket.rules for ticket in tickets))

    def test_structure_checkpoint_requires_every_rule_and_accepts_any_verdict_at_head(self) -> None:
        first = self.rule("structure-first", "tier: D\nfamily: modules\n")
        missing = self.rule("structure-last", "tier: D\nfamily: modules\n")
        ledger = self.ledger()
        ledger.set_tip(self.initial_tip, "main")
        ledger.ticket_add("modules-unit-1", "modules-unit", "structure", "modules", "crates/alpha", [], [missing, first])
        ledger.ticket_assign("modules-unit-1", "main")
        _ = ledger.hold(first, "crates/alpha", 1, "Split crate", "restructure", "before", "after", self.blob(first), "modules-unit-1")
        self.record(first, "crates/alpha", self.initial_tip, "held", ticket="modules-unit-1")

        refused = self.cli("ticket", "checkpoint", "--id", "modules-unit-1", "--commit", "HEAD", success=False)
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertEqual(refused.stderr.strip(), f"style_ledger: modules-unit-1: unjudged cells: {missing} crates/alpha")
        self.assertNotIn(f"{first} crates/alpha", refused.stderr)
        self.assertEqual(ledger.ticket_open("modules-unit")[0]["state"], "assigned")

        self.record(missing, "crates/alpha", self.initial_tip, "waived", detail="accepted", ticket="modules-unit-1")
        for _ in range(2):
            _ = self.cli("ticket", "checkpoint", "--id", "modules-unit-1", "--commit", "HEAD")
        checkpointed, = ledger.ticket_open("modules-unit")
        self.assertEqual((checkpointed["state"], checkpointed["checkpoint"]), ("checkpointed", self.initial_tip))
        with mock.patch.object(ledger, "unjudged_ticket_cells", side_effect=AssertionError("rechecked an idempotent checkpoint")):
            ledger.ticket_checkpoint("modules-unit-1", self.initial_tip)
        _ = self.cli("ticket", "merge", "--id", "modules-unit-1")
        self.assertEqual(ledger.ticket_open("modules-unit"), ())

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

    def test_a_style_checkpoint_keeps_its_reservation_until_ticket_merge_releases_it(self) -> None:
        session = self.held_reservation("modules-unit-1")
        half = self.cli("ticket", "checkpoint", "--id", "modules-unit-1", "--commit", "HEAD", "--session-dir", str(session),
                        success=False)
        self.assertEqual(half.returncode, 2)
        self.assertIn("ticket checkpoint takes --session-dir and --phase together", half.stderr)
        self.checkpointed_structure_ticket("modules-unit-1", "--session-dir", str(session), "--phase", "7")
        self.assertEqual(self.berth_calls(), [])
        self.assertEqual(self.cli("ticket", "merge", "--id", "modules-unit-1").stdout,
                         "ticket modules-unit-1: phase 7: released reservation r1\n")
        self.assertIn(["release", "r1", "--json"], self.berth_calls())
        self.assertFalse((session / "phases/7/delegated_phase_reservation_state.json").exists())
        self.assertEqual(self.ticket_row("modules-unit-1"), ("merged", "", ""))
        calls = self.berth_calls()
        self.assertEqual(self.cli("ticket", "merge", "--id", "modules-unit-1").stdout, "")
        self.assertEqual(self.berth_calls(), calls)

    def test_a_checkpoint_stores_its_reservation_in_the_same_write_as_its_state(self) -> None:
        session = self.held_reservation("modules-unit-1")
        self.assigned_structure_ticket("modules-unit-1")
        with closing(sqlite3.connect(style_ledger.ledger_location(self.repo).database)) as connection:
            _ = connection.execute("CREATE TRIGGER lost_reservation BEFORE UPDATE OF reservation_session_dir ON tickets "
                                   + "BEGIN SELECT RAISE(ABORT, 'disk full'); END")
            connection.commit()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "disk full"):
            self.ledger().ticket_checkpoint("modules-unit-1", "HEAD", str(session), "7")
        self.assertEqual(self.ticket_row("modules-unit-1"), ("assigned", "", ""))
        with closing(sqlite3.connect(style_ledger.ledger_location(self.repo).database)) as connection:
            _ = connection.execute("DROP TRIGGER lost_reservation")
            connection.commit()
        self.ledger().ticket_checkpoint("modules-unit-1", "HEAD", str(session), "7")
        self.assertEqual(self.ticket_row("modules-unit-1"), ("checkpointed", str(session), "7"))

    def test_a_repeat_checkpoint_stores_a_missing_reservation_and_refuses_another(self) -> None:
        session = self.held_reservation("modules-unit-1")
        self.checkpointed_structure_ticket("modules-unit-1")
        self.ledger().ticket_checkpoint("modules-unit-1", "HEAD", str(session), "7")
        self.assertEqual(self.ticket_row("modules-unit-1"), ("checkpointed", str(session), "7"))
        with self.assertRaisesRegex(style_ledger.LedgerError, f"checkpointed holding the reservation of phase 7 in {re.escape(str(session))}"):
            self.ledger().ticket_checkpoint("modules-unit-1", "HEAD", str(session), "8")
        self.ledger().ticket_checkpoint("modules-unit-1", "HEAD")
        self.assertEqual(self.ticket_row("modules-unit-1"), ("checkpointed", str(session), "7"))

    def test_a_checkpoint_that_names_no_reservation_record_merges_as_before(self) -> None:
        session = self.held_reservation("modules-unit-1")
        state = (session / "phases/7/delegated_phase_reservation_state.json").read_bytes()
        self.checkpointed_structure_ticket("modules-unit-1")
        self.assertEqual(self.cli("ticket", "merge", "--id", "modules-unit-1").stdout, "")
        self.assertEqual(self.berth_calls(), [])
        self.assertEqual((session / "phases/7/delegated_phase_reservation_state.json").read_bytes(), state)

    def test_a_reservation_ticket_merge_cannot_release_stays_held_until_a_rerun(self) -> None:
        session = self.held_reservation("modules-unit-1")
        self.checkpointed_structure_ticket("modules-unit-1", "--session-dir", str(session), "--phase", "7")
        self.write("README.md", "moved on\n")
        moved = self.commit("Move the unit worktree off its checkpoint")
        refused = self.cli("ticket", "merge", "--id", "modules-unit-1", success=False)
        self.assertEqual(refused.returncode, 2)
        self.assertIn(f"style_ledger: ticket modules-unit-1: phase_reservation: {self.repo} is at {moved[:7]}, not checkpoint "
                      + f"{self.initial_tip[:7]}; reservation r1 stays held; rerun ticket merge", refused.stderr)
        self.assertEqual(self.ledger().ticket_open("modules-unit"), ())
        self.assertNotIn("release", [call[0] for call in self.berth_calls()])
        # Only a merged ticket releases its reservation.
        self.checkpointed_structure_ticket("modules-unit-2", "--session-dir", str(session), "--phase", "7")
        with self.assertRaisesRegex(style_ledger.LedgerError, "released at ticket merge, not in state checkpointed"):
            _ = self.ledger().release_held_reservation("modules-unit-2")
        _ = self.git("reset", "-q", "--hard", self.initial_tip)
        self.assertEqual(self.cli("ticket", "merge", "--id", "modules-unit-1").stdout,
                         "ticket modules-unit-1: phase 7: released reservation r1\n")

    def test_reconcile_retries_a_merged_ticket_reservation_until_its_release_is_confirmed(self) -> None:
        session = self.held_reservation("modules-unit-1")
        self.checkpointed_structure_ticket("modules-unit-1", "--session-dir", str(session), "--phase", "7")
        self.write("README.md", "moved on\n")
        moved = self.commit("Move the unit worktree off its checkpoint")
        changes = self.ledger().reconcile("main", "main")
        self.assertIn("ticket modules-unit-1: checkpointed -> merged", changes)
        self.assertIn(f"ticket modules-unit-1: phase_reservation: {self.repo} is at {moved[:7]}, not checkpoint "
                      + f"{self.initial_tip[:7]}; reservation r1 stays held", changes)
        self.assertEqual(self.ticket_row("modules-unit-1"), ("merged", str(session), "7"))
        _ = self.git("reset", "-q", "--hard", self.initial_tip)
        self.assertIn("ticket modules-unit-1: phase 7: released reservation r1", self.ledger().reconcile("main", "main"))
        self.assertEqual(self.ticket_row("modules-unit-1"), ("merged", "", ""))
        calls = self.berth_calls()
        self.assertEqual(self.ledger().reconcile("main", "main"), ())
        self.assertEqual(self.berth_calls(), calls)

    def test_ticket_merge_takes_a_release_at_a_later_merge_once_the_style_branch_contains_it(self) -> None:
        session = self.held_reservation("modules-unit-1")
        self.checkpointed_structure_ticket("modules-unit-1", "--session-dir", str(session), "--phase", "7")
        # The unit merges style/main after its checkpoint and berth releases there (cargo-liner ticket 90).
        _ = self.git("checkout", "-q", "-b", "unit")
        self.write("README.md", "merged style/main\n")
        later = self.commit("Merge style/main into the unit")
        _ = (self.root / "board.json").write_text(json.dumps({"r1": {"status": "released_after_checkpoint", "protected_tip": later}}))
        refused = self.cli("ticket", "merge", "--id", "modules-unit-1", success=False)
        self.assertEqual(refused.returncode, 2)
        self.assertIn(f"or at a later commit that {self.initial_tip[:7]} contains; it stays held; rerun ticket merge", refused.stderr)
        self.assertEqual(self.ticket_row("modules-unit-1"), ("merged", str(session), "7"))
        _ = self.git("checkout", "-q", "main")
        _ = self.git("merge", "-q", "--ff-only", "unit")
        self.assertEqual(self.cli("ticket", "merge", "--id", "modules-unit-1").stdout,
                         "ticket modules-unit-1: phase 7: released reservation r1\n")
        self.assertEqual(self.ticket_row("modules-unit-1"), ("merged", "", ""))
        self.assertEqual({call[0] for call in self.berth_calls()}, {"board"})

    def test_read_checkpoint_names_only_the_missing_rule_and_file_pair(self) -> None:
        first, last = self.rule("read-first"), self.rule("read-last")
        self.set_tip(self.initial_tip)
        self.add_ticket("read-1", [last, first])
        _ = self.cli("ticket", "assign", "--id", "read-1", "--branch", "main")
        complete = ((first, self.sources[0]), (first, self.sources[1]), (last, self.sources[0]))
        for rule, path in complete:
            self.record(rule, path, self.initial_tip, ticket="read-1")

        refused = self.cli("ticket", "checkpoint", "--id", "read-1", "--commit", "HEAD", success=False)
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn(f"{last} {self.sources[1]}", refused.stderr)
        for rule, path in complete:
            self.assertNotIn(f"{rule} {path}", refused.stderr)
        self.assertEqual(self.ledger().ticket_open("read")[0]["state"], "assigned")
        self.assertEqual(self.ledger().unjudged_ticket_cells("read-1", "HEAD", self.in_worktree),
                         (style_ledger.UnjudgedTicketCell(last, self.sources[1]),))

    def test_judgments_without_the_owned_ticket_do_not_complete_its_cells(self) -> None:
        rule = self.rule("read")
        self.set_tip(self.initial_tip)
        self.add_ticket("read-1", [rule])
        self.add_ticket("other-1", [rule], unit="other")
        _ = self.cli("ticket", "assign", "--id", "read-1", "--branch", "main")
        for ticket in ("", "other-1"):
            with self.subTest(judged_under=ticket):
                for path in self.sources[:2]:
                    self.record(rule, path, self.initial_tip, ticket=ticket)
                refused = self.cli("ticket", "checkpoint", "--id", "read-1", "--commit", "HEAD", success=False)
                self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
                self.assertEqual(refused.stderr.strip(),
                                 f"style_ledger: read-1: unjudged cells: {rule} {self.sources[0]}, {rule} {self.sources[1]}")
                self.assertEqual(self.ledger().ticket_open("read")[0]["state"], "assigned")

    def test_listed_checkpoint_requires_a_verdict_for_each_file_of_its_rule(self) -> None:
        rule = self.rule("listed", "tier: B\nfamily: names\ninput_scope: file\npre_filter: value\n")
        ledger = self.ledger()
        ledger.set_tip(self.initial_tip, "main")
        ledger.ticket_add("listed-1", "listed", "listed", "names", "crates/alpha", list(self.sources[:2]), [rule], rule)
        ledger.ticket_assign("listed-1", "main")
        self.record(rule, self.sources[0], self.initial_tip, ticket="listed-1")
        refused = self.cli("ticket", "checkpoint", "--id", "listed-1", "--commit", "HEAD", success=False)
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn(f"{rule} {self.sources[1]}", refused.stderr)
        self.assertNotIn(f"{rule} {self.sources[0]}", refused.stderr)
        self.record(rule, self.sources[1], self.initial_tip, ticket="listed-1")
        _ = self.cli("ticket", "checkpoint", "--id", "listed-1", "--commit", "HEAD")

    def test_yielded_read_file_returns_to_open_and_does_not_block_checkpoint(self) -> None:
        first, last = self.rule("read-first"), self.rule("read-last")
        self.set_tip(self.initial_tip)
        self.add_ticket("read-1", [last, first])
        _ = self.cli("ticket", "assign", "--id", "read-1", "--branch", "main")
        before = {rule: self.states(rule)[self.sources[0]] for rule in (first, last)}
        for rule in (first, last):
            for path in self.sources[:2]:
                self.record(rule, path, self.initial_tip, ticket="read-1")
        self.assertTrue(all(self.states(rule)[self.sources[0]] == "current" for rule in (first, last)))

        _ = self.cli("ticket", "yield", "--id", "read-1", "--path", self.sources[0])

        self.assertEqual({rule: self.states(rule)[self.sources[0]] for rule in (first, last)}, before)
        self.assertEqual(self.ledger().unjudged_ticket_cells("read-1", "HEAD", self.in_worktree), ())
        _ = self.cli("ticket", "checkpoint", "--id", "read-1", "--commit", "HEAD")
        self.assertEqual(self.ledger().ticket_open("read")[0]["state"], "checkpointed")

    def test_yield_removes_only_its_own_verdicts_and_unfiled_findings_on_that_file(self) -> None:
        rule = self.rule("read")
        self.set_tip(self.initial_tip)
        self.add_ticket("read-1", [rule])
        self.add_ticket("other-1", [rule], unit="other")
        _ = self.cli("ticket", "assign", "--id", "read-1", "--branch", "main")
        self.record(rule, self.sources[0], self.initial_tip, "waived", ticket="other-1")
        removed = self.hold_finding(rule, 1, ticket="read-1")
        self.record(rule, self.sources[0], self.initial_tip, "held", ticket="read-1")
        self.record(rule, self.sources[1], self.initial_tip, ticket="read-1")
        filed = self.hold_finding(rule, 2, ticket="read-1")
        waived = self.hold_finding(rule, 3, ticket="read-1")
        other = self.hold_finding(rule, 4, ticket="other-1")
        ledger = self.ledger()
        proposal = self.root / "issues/filed.md"
        ledger.file_held(filed, proposal)
        ledger.waive_held(waived, style_ledger.WaiveByPerson("nate"))
        untouched = ledger.hold(rule, self.sources[1], 5, "other file", "rename", "old", "new",
                                self.blob(rule), "read-1")

        _ = self.cli("ticket", "yield", "--id", "read-1", "--path", self.sources[0])

        with closing(sqlite3.connect(ledger.database)) as connection:
            judgments = cast(list[tuple[str, str, str]], connection.execute(
                "SELECT ticket,path,verdict FROM judgments ORDER BY ticket,path",
            ).fetchall())
        self.assertEqual(judgments, [("other-1", self.sources[0], "waived"), ("read-1", self.sources[1], "clean")])
        findings = {finding.finding_id: finding for finding in ledger.held_findings(style_ledger.AllHeldFindings())}
        self.assertNotIn(removed, findings)
        self.assertEqual(set(findings), {filed, waived, other, untouched.finding_id})
        self.assertEqual(findings[filed].state, style_ledger.Filed(proposal))
        self.assertIsInstance(findings[waived].state, style_ledger.Waived)
        self.assertIsInstance(findings[other].state, style_ledger.Unfiled)
        self.assertIsInstance(findings[untouched.finding_id].state, style_ledger.Unfiled)

    def test_yield_refuses_nonassigned_and_nonfile_tickets_and_outside_paths(self) -> None:
        rule = self.rule("read")
        structure = self.rule("structure", "tier: D\nfamily: modules\n")
        self.set_tip(self.initial_tip)
        self.add_ticket("read-1", [rule])
        ledger = self.ledger()
        ledger.ticket_add("modules-1", "modules", "structure", "modules", "crates/alpha", [], [structure])
        ledger.ticket_assign("modules-1", "main")
        finding = self.hold_finding(rule, 1)
        issue = self.root / "issues/approved.md"
        ledger.file_held(finding, issue)
        ledger.ticket_add("proposal-1", "proposal", "proposal", "names", "crates/alpha",
                          [self.sources[0]], [rule], proposal=str(issue), finding_ids=(finding,))
        ledger.ticket_assign("proposal-1", "main")
        cases = (("read-1", self.sources[0], "cannot yield from queued"),
                 ("modules-1", self.sources[0], "cannot yield a structure ticket"),
                 ("proposal-1", self.sources[0], f"{self.sources[0]} holds every finding the ticket has left; "
                  + "drop the ticket with style_ledger.py ticket drop"))
        for ticket, path, reason in cases:
            with self.subTest(ticket=ticket):
                refused = self.cli("ticket", "yield", "--id", ticket, "--path", path, success=False)
                self.assertEqual(refused.returncode, 2)
                self.assertEqual(refused.stderr.strip(), f"style_ledger: {ticket}: {reason}")
        ledger.ticket_assign("read-1", "main")
        refused = self.cli("ticket", "yield", "--id", "read-1", "--path", self.sources[2], success=False)
        self.assertEqual(refused.returncode, 2)
        self.assertEqual(refused.stderr.strip(), f"style_ledger: read-1: {self.sources[2]} is not in ticket files")
        for path in self.sources[:2]:
            self.record(rule, path, self.initial_tip, ticket="read-1")
        ledger.ticket_checkpoint("read-1", self.initial_tip)
        refused = self.cli("ticket", "yield", "--id", "read-1", "--path", self.sources[0], success=False)
        self.assertEqual(refused.returncode, 2)
        self.assertEqual(refused.stderr.strip(), "style_ledger: read-1: cannot yield from checkpointed")
        self.assertEqual(self.states(rule)[self.sources[0]], "current")
        self.assertEqual(ledger.held_findings(style_ledger.AllHeldFindings())[0].state, style_ledger.Filed(issue))

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

    def test_a_proposal_yield_keeps_the_yielded_finding_filed_and_free_for_a_later_ticket(self) -> None:
        kept, freed, issue = self.yield_proposal((self.sources[3], "rename", "Outcome::Old", "Outcome::New"),
                                                 (self.sources[1], "rename", "Shape::Round", "Shape::Square"))
        ledger = self.ledger()
        refused = self.cli("ticket", "yield", "--id", "proposal-1", "--path", self.sources[0], success=False)
        self.assertEqual(refused.stderr.strip(), f"style_ledger: proposal-1: no finding of the ticket is at {self.sources[0]}")
        yielded = self.cli("ticket", "yield", "--id", "proposal-1", "--path", self.sources[1])
        self.assertEqual(yielded.stdout, f"yielded {freed}: it stays filed for a later ticket\n")
        findings = {finding.finding_id: finding for finding in ledger.held_findings(style_ledger.AllHeldFindings())}
        self.assertEqual(findings[freed].state, style_ledger.Filed(issue))
        self.assertEqual(findings[freed].build_ticket, style_ledger.NoBuildTicket())
        self.assertEqual(findings[kept].build_ticket, style_ledger.BuildTicket("proposal-1"))
        ticket = ledger.ticket_open("proposal")[0]
        self.assertEqual(ticket["finding_ids"], [kept])
        self.assertEqual([path["path"] for path in ticket["yielded"]], [self.sources[1]])
        self.assertEqual(self.cli("ticket", "yield", "--id", "proposal-1", "--path", self.sources[1]).stdout, "")
        self.assertEqual(ledger.ticket_open("proposal")[0], ticket)
        refused = self.cli("ticket", "yield", "--id", "proposal-1", "--path", self.sources[3], success=False)
        self.assertIn("holds every finding the ticket has left", refused.stderr)
        # The merge builds the kept finding and leaves the yielded one filed for a later ticket.
        self.write(self.sources[3], "enum Outcome { New }\n")
        checkpoint = self.commit("Build the kept finding")
        ledger.set_tip(checkpoint, "main")
        ledger.ticket_checkpoint("proposal-1", checkpoint)
        self.assertEqual(ledger.ticket_merge("proposal-1"), ())
        findings = {finding.finding_id: finding for finding in ledger.held_findings(style_ledger.AllHeldFindings())}
        self.assertEqual(findings[kept].state, style_ledger.Built(issue))
        self.assertEqual((findings[freed].state, findings[freed].build_ticket), (style_ledger.Filed(issue), style_ledger.NoBuildTicket()))
        ledger.ticket_add("proposal-2", "proposal", "proposal", "names", "crates/alpha", [self.sources[1]],
                          [findings[freed].rule], proposal=str(issue), finding_ids=(freed,))
        self.assertEqual(ledger.held_findings(style_ledger.AllHeldFindings())[1].build_ticket, style_ledger.BuildTicket("proposal-2"))

    # Two module files of crates/alpha that two file renames move; lib.rs declares them.
    yield_files: dict[str, str] = {
        "crates/alpha/src/a.rs": "pub fn alpha() {}\n",
        "crates/alpha/src/b.rs": "pub fn beta() {}\n",
        "crates/alpha/src/c.rs": "pub const BAR: char = '\\u{2580}';\npub const TAB: u8 = b'\\x7f';\n",
        "crates/alpha/src/other.rs": "#[cfg(test)]\nmod tests {\n    use super::*;\n    use std::collections::*;\n}\n",
    }
    kept_rename: tuple[str, str, str, str] = ("crates/alpha/src/a.rs", "rename", "Alpha", "Rename src/a.rs to src/first.rs")
    freed_rename: tuple[str, str, str, str] = ("crates/alpha/src/b.rs", "rename", "Beta", "Rename src/b.rs to src/second.rs")

    def test_a_proposal_yield_frees_a_file_rename_beside_an_independent_one_in_its_crate(self) -> None:
        # Mounts inside alpha, also through an inline module's folder, and one from beta to a file of no finding's
        # crate, join no finding crate to another.
        mounts = {"crates/alpha/src/other.rs": self.yield_files["crates/alpha/src/other.rs"] + "#[path = \"c.rs\"]\nmod again;\n"
                  + "#[path = \".\"]\nmod folded {\n    #[path = \"c.rs\"]\n    mod again;\n}\n",
                  "crates/beta/src/other.rs": "#[path = \"../../../vendor/third-party/src/lib.rs\"]\nmod third;\n"}
        kept, freed, _ = self.yield_proposal(self.kept_rename, self.freed_rename, {**self.yield_files, **mounts})
        self.assertEqual(self.ledger().ticket_yield("proposal-1", self.freed_rename[0]), (freed,))
        self.assertEqual(self.ledger().ticket_open("proposal")[0]["finding_ids"], [kept])

    def test_a_proposal_yield_refuses_a_kept_finding_that_can_need_the_yielded_one(self) -> None:
        library = "crates/alpha/src/lib.rs"
        reexport = {library: "mod producer; mod consumer;\npub use producer::make;\n",
                    "crates/alpha/src/producer.rs": "pub fn make() -> u32 { 1 }\n",
                    "crates/alpha/src/consumer.rs": "pub fn take() -> u32 { crate::make() }\n"}
        qualified = ("crates/alpha/src/b.rs", "rename", "Shape::Round", "Shape::Square")
        cases: tuple[tuple[str, tuple[str, str, str, str], tuple[str, str, str, str], dict[str, str], str], ...] = (
            ("a re-export carries the yielded type", ("crates/alpha/src/consumer.rs", "type", "pub fn take() -> u32", "u64"),
             ("crates/alpha/src/producer.rs", "type", "pub fn make() -> u32", "u64"), reexport,
             "{kept} is not a file rename, and crates/alpha holds a freed and a kept finding"),
            ("a qualified rename beside a file rename", self.kept_rename, qualified, {},
             "{freed} is not a file rename, and crates/alpha holds a freed and a kept finding"),
            ("the yielded file names the kept old module", self.kept_rename, self.freed_rename,
             {"crates/alpha/src/b.rs": "pub fn beta() { crate::a::alpha() }\n"},
             "crates/alpha/src/b.rs names a, a module that {kept} renames"),
            ("the yielded file names the kept new module", self.kept_rename, self.freed_rename,
             {"crates/alpha/src/b.rs": "pub fn first() {}\n"}, "crates/alpha/src/b.rs names first, a module that {kept} renames"),
            ("one new name", self.kept_rename, (*self.freed_rename[:3], "Rename src/b.rs to src/first.rs"), {},
             "{freed} and {kept} both rename to or from first"),
            ("the yielded new name is the kept old name", self.kept_rename,
             (*self.freed_rename[:3], "Rename src/b.rs to src/a.rs"), {}, "{freed} and {kept} both rename to or from a"),
            ("the kept new name is the yielded old name", (*self.kept_rename[:3], "Rename src/a.rs to src/b.rs"),
             self.freed_rename, {}, "{freed} and {kept} both rename to or from b"),
            ("a public use of a renamed module", self.kept_rename, self.freed_rename,
             {library: "mod a; mod b; mod c; mod other;\npub use a::alpha;\n"}, f"{library} re-exports a"),
            ("a glob use of the renamed modules' parent", self.kept_rename, self.freed_rename,
             {"crates/alpha/src/other.rs": "use super::*;\n"}, "crates/alpha/src/other.rs glob-imports crate"),
            ("a macro_rules item", self.kept_rename, self.freed_rename,
             {"crates/alpha/src/c.rs": "macro_rules! made { () => {} }\n"}, "crates/alpha/src/c.rs holds macro_rules"),
            ("an include call", self.kept_rename, self.freed_rename,
             {"crates/alpha/src/c.rs": "include!(\"made.rs\");\n"}, "crates/alpha/src/c.rs holds include"),
            ("a path attribute mounts the yielded file into the kept crate",
             ("crates/alpha/src/consumer.rs", "type", "pub fn take() -> u32", "u64"),
             ("crates/beta/src/producer.rs", "type", "pub fn make() -> u32", "u64"),
             {library: "#[path = \"../../beta/src/producer.rs\"]\nmod producer;\nmod consumer;\n"},
             f"{library} mounts crates/beta/src/producer.rs, a file outside crates/alpha"),
            ("a cfg_attr path attribute mounts the yielded file", ("crates/alpha/src/consumer.rs", "type", "u32", "u64"),
             ("crates/beta/src/producer.rs", "type", "u32", "u64"),
             {library: "#[cfg_attr(all(), cfg_attr(test, path = \"../../beta/src/producer.rs\"))]\nmod producer;\n"},
             f"{library} mounts crates/beta/src/producer.rs, a file outside crates/alpha"),
            ("another crate mounts the yielded file", self.kept_rename, self.freed_rename,
             {"crates/beta/src/lib.rs": "#[path = \"../../alpha/src/b.rs\"]\nmod b;\n"},
             "crates/beta/src/lib.rs mounts crates/alpha/src/b.rs, a file outside crates/beta"),
            ("a path attribute mounts a file that is not .rs", ("crates/alpha/src/consumer.rs", "type", "u32", "u64"),
             ("crates/beta/src/producer.rs", "type", "u32", "u64"),
             {library: "#[path = \"mount.inc\"]\nmod mount;\n",
              "crates/alpha/src/mount.inc": "#[path = \"../../beta/src/producer.rs\"]\nmod producer;\n"},
             f"{library} mounts crates/alpha/src/mount.inc, which is not a .rs file"),
            ("an inline module's folder is in another crate", ("crates/alpha/src/consumer.rs", "type", "u32", "u64"),
             ("crates/beta/src/producer.rs", "type", "u32", "u64"),
             {library: "#[path = \"../../beta/src\"]\nmod mounted {\n    mod producer;\n}\n"},
             f"{library} mounts crates/beta/src, a folder outside crates/alpha"),
            ("a path attribute in an inline module reads from the module's folder",
             ("crates/alpha/src/consumer.rs", "type", "u32", "u64"), ("crates/beta/src/producer.rs", "type", "u32", "u64"),
             {library: "#[path = \"a/b\"]\nmod up {\n    #[path = \"../../../../beta/src/producer.rs\"]\n    mod producer;\n}\n"},
             f"{library} mounts crates/beta/src/producer.rs, a file outside crates/alpha"),
            ("an inactive path attribute leaves the module's own folder a reading",
             ("crates/alpha/src/consumer.rs", "type", "u32", "u64"), ("crates/beta/src/producer.rs", "type", "u32", "u64"),
             {"Cargo.toml": '[workspace]\nmembers = ["crates/alpha", "crates/beta", "crates/gamma"]\nresolver = "2"\n',
              "crates/gamma/Cargo.toml": '[package]\nname = "gamma"\nversion = "0.1.0"\nedition = "2021"\n',
              "crates/gamma/src/lib.rs": "#[cfg_attr(any(), path = \".\")]\npub mod deep {\n"
              + "    #[path = \"../../../beta/src/producer.rs\"]\n    pub mod producer;\n}\n"},
             "crates/gamma/src/lib.rs mounts crates/beta/src/producer.rs, a file outside crates/gamma"),
            ("a path attribute leaves the repository", self.kept_rename, self.freed_rename,
             {library: "mod a; mod b; mod c; mod other;\n#[path = \"../../../../outside.rs\"]\nmod outside;\n"},
             f"{library} mounts ../outside.rs, a file outside crates/alpha"),
            ("a path attribute in a macro of another crate", self.kept_rename, self.freed_rename,
             {"crates/beta/src/other.rs": "macro_rules! mount { () => { #[path = \"x.rs\"] mod x; } }\n"},
             "crates/beta/src/other.rs holds a path attribute in a macro"),
            ("a path attribute that is not a string", self.kept_rename, self.freed_rename,
             {"crates/beta/src/other.rs": "#[path = concat!(\"x\", \".rs\")]\nmod x;\n"},
             "crates/beta/src/other.rs holds a path attribute that is not a relative string"),
            ("a file that does not tokenize", self.kept_rename, self.freed_rename,
             {"crates/alpha/src/c.rs": "pub fn odd() { \\ }\n"}, "crates/alpha/src/c.rs does not tokenize"),
        )
        for name, kept, freed, files, reason in cases:
            with self.subTest(name):
                self.setUp()
                kept_id, freed_id, _ = self.yield_proposal(kept, freed, {**self.yield_files, **files})
                ledger = self.ledger()
                with self.assertRaises(style_ledger.LedgerError) as refused:
                    _ = ledger.ticket_yield("proposal-1", freed[0])
                self.assertIn(f"proposal-1: a kept finding can need a finding at {freed[0]}", str(refused.exception))
                self.assertIn(reason.format(kept=kept_id, freed=freed_id), str(refused.exception))
                ticket = ledger.ticket_open("proposal")[0]
                self.assertEqual((ticket["finding_ids"], ticket["yielded"]), (sorted([kept_id, freed_id]), ()))

    def test_a_proposal_yield_reads_again_when_the_synced_tip_moves_before_its_lock(self) -> None:
        _ = self.yield_proposal(self.kept_rename, self.freed_rename, self.yield_files)
        self.write("crates/alpha/src/c.rs", "macro_rules! made { () => {} }\n")
        moved = self.commit("Add a macro")
        ledger = self.ledger()
        read = style_ledger.Ledger._yielded_findings  # pyright: ignore[reportPrivateUsage]
        calls: list[str] = []

        def advancing(self: style_ledger.Ledger, ticket: style_ledger.TicketRecord, findings: tuple[style_ledger.HeldFinding, ...],
                      path: str) -> style_ledger.FileYield | style_ledger.FindingYield:
            calls.append(self.synced_commit())
            result = read(self, ticket, findings, path)
            if len(calls) == 1:
                self.set_tip(moved)
            return result

        with mock.patch.object(style_ledger.Ledger, "_yielded_findings", advancing), \
                self.assertRaises(style_ledger.LedgerError) as refused:
            _ = ledger.ticket_yield("proposal-1", self.freed_rename[0])
        self.assertIn("crates/alpha/src/c.rs holds macro_rules", str(refused.exception))
        self.assertEqual(calls, [self.git("rev-parse", "HEAD~1"), moved])
        self.assertEqual(ledger.ticket_open("proposal")[0]["yielded"], ())

    def test_repeated_listed_yield_preserves_the_recorded_yield_and_remaining_ownership(self) -> None:
        rule = self.rule("listed", "tier: B\nfamily: names\ninput_scope: file\npre_filter: value\n")
        ledger = self.ledger()
        ledger.set_tip(self.initial_tip, "main")
        ledger.ticket_add("listed-1", "listed", "listed", "names", "crates/alpha", list(self.sources[:2]), [rule], rule)
        ledger.ticket_assign("listed-1", "main")
        self.record(rule, self.sources[0], self.initial_tip, ticket="listed-1")
        _ = self.cli("ticket", "yield", "--id", "listed-1", "--path", self.sources[0])
        with closing(sqlite3.connect(ledger.database)) as connection:
            before = cast(list[tuple[str, str, str]], connection.execute("SELECT * FROM ticket_yields").fetchall())
        self.assertEqual([(ticket, path) for ticket, path, _ in before], [("listed-1", self.sources[0])])
        _ = self.cli("ticket", "yield", "--id", "listed-1", "--path", self.sources[0])
        with closing(sqlite3.connect(ledger.database)) as connection:
            after = cast(list[tuple[str, str, str]], connection.execute("SELECT * FROM ticket_yields").fetchall())
        self.assertEqual(after, before)
        self.assertEqual(ledger.unjudged_ticket_cells("listed-1", "HEAD", self.in_worktree), (style_ledger.UnjudgedTicketCell(rule, self.sources[1]),))

    def test_record_under_a_yielded_ticket_is_refused_without_recreating_its_verdict(self) -> None:
        rule = self.rule("read")
        self.set_tip(self.initial_tip)
        self.add_ticket("read-1", [rule])
        _ = self.cli("ticket", "assign", "--id", "read-1", "--branch", "main")
        _ = self.cli("ticket", "yield", "--id", "read-1", "--path", self.sources[0])
        refused = self.cli("record", "--rule", rule, "--path", self.sources[0], "--commit", self.initial_tip,
                           "--rule-blob", self.blob(rule), "--verdict", "clean", "--ticket", "read-1",
                           "--worker", "test-worker", success=False)
        self.assertEqual(refused.returncode, 2)
        self.assertEqual(refused.stderr.strip(), f"style_ledger: read-1: {self.sources[0]} was yielded")
        self.assertEqual(self.states(rule)[self.sources[0]], "open")
        self.record(rule, self.sources[0], self.initial_tip, ticket="another-ticket")
        self.assertEqual(self.states(rule)[self.sources[0]], "current")

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
        for path in self.sources[:2]:
            self.record(rule_id, path, self.initial_tip, ticket="read-1")
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

    def test_reconcile_uses_the_pushed_tip_when_the_style_branch_is_missing(self) -> None:
        _ = self.rule("read")
        origin = self.root / "origin.git"
        _ = self.git("init", "-q", "--bare", str(origin))
        _ = self.git("remote", "add", "origin", str(origin))
        _ = self.git("push", "-q", "origin", "main")
        self.write(self.sources[0], "pub fn unpushed() {}\n")
        _ = self.commit("Unpushed branch-point work")
        ledger = self.ledger()

        _ = ledger.reconcile("main", "style/main")

        self.assertEqual(ledger.summary().synced_tip, self.initial_tip)

    def test_origin_without_a_tracking_ref_refuses_to_use_the_local_branch_point(self) -> None:
        origin = self.root / "origin.git"
        _ = self.git("init", "-q", "--bare", str(origin))
        _ = self.git("remote", "add", "origin", str(origin))
        with self.assertRaisesRegex(style_ledger.LedgerError, "origin.*refs/remotes/origin/main.*fetch"):
            _ = self.ledger().reconcile("main", "style/main")

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
                    for path in self.sources[:2]:
                        self.record(rule_id, path, self.initial_tip, ticket=ticket)
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

    def test_migration_23_preserves_processed_tips_as_linted(self) -> None:
        database = self.root / "legacy.sqlite"
        with closing(sqlite3.connect(database)) as connection, connection:
            for number, migration in sorted(style_ledger.MIGRATIONS.items()):
                if number < 23:
                    _ = connection.executescript(migration)
            _ = connection.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
            _ = connection.execute("INSERT INTO schema_version VALUES (22)")
            _ = connection.execute("INSERT INTO processed_tips VALUES (?,?)", (self.initial_tip, "2026-10-27"))
        with style_ledger.Ledger(self.repo, database) as ledger:
            self.assertEqual(ledger.tip_seen(self.initial_tip), "linted")
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute("SELECT * FROM processed_tips").fetchone(),
                             (self.initial_tip, "2026-10-27", "linted"))

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

    def test_handoff_merged_takes_a_pending_row_once_the_branch_point_contains_it(self) -> None:
        """A queued notice left the row pending and its merge was refused (cargo-liner, 2026-10-27)."""
        self.set_tip(self.initial_tip)
        _ = self.git("checkout", "-q", "-b", "style/main")
        self.write(self.sources[0], "pub fn corrected() {}\n")
        checkpoint = self.commit("Style checkpoint")
        _ = self.cli("reconcile", "--branch-point", "main", "--style-branch", "style/main")
        _ = self.cli("handoff", "add", "--checkpoint", checkpoint)
        refused = self.cli("handoff", "merged", "--checkpoint", checkpoint, success=False)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("hand-off is not merged into main", refused.stderr)
        _ = self.git("checkout", "-q", "main")
        _ = self.git("merge", "--no-ff", "-q", "style/main", "-m", "Integrate style")
        merge_commit = self.git("rev-parse", "HEAD")
        _ = self.cli("handoff", "merged", "--checkpoint", checkpoint)
        with closing(sqlite3.connect(self.root / "state/sample/ledger.sqlite")) as connection:
            stored = cast(tuple[str, str, str], connection.execute(
                "SELECT state,merge_commit,sent_to FROM handoffs WHERE checkpoint=?", (checkpoint,),
            ).fetchone())
        self.assertEqual(stored, ("merged", merge_commit, ""))

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

    def test_reconcile_and_handoff_merged_accept_only_a_pushed_branch_point_merge(self) -> None:
        origin = self.root / "origin.git"
        _ = self.git("init", "-q", "--bare", str(origin))
        _ = self.git("remote", "add", "origin", str(origin))
        _ = self.git("push", "-q", "origin", "main")
        _ = self.git("checkout", "-q", "-b", "style/main")
        self.write(self.sources[0], "pub fn corrected() {}\n")
        checkpoint = self.commit("Style checkpoint")
        ledger = self.ledger()
        _ = ledger.reconcile("refs/heads/main", "style/main")
        ledger.handoff_add(checkpoint)
        ledger.handoff_send(checkpoint, "parent")
        _ = self.git("checkout", "-q", "main")
        _ = self.git("merge", "--no-ff", "-qm", "Unpushed integration", "style/main")
        merge_commit = self.git("rev-parse", "HEAD")

        self.assertEqual(ledger.reconcile("refs/heads/main", "style/main"), ())
        self.assertEqual(ledger.pending_handoffs()[0]["state"], "sent")
        for evidence in ("", merge_commit):
            with self.subTest(evidence=evidence):
                with self.assertRaisesRegex(style_ledger.LedgerError, "hand-off is not merged"):
                    ledger.handoff_merged(checkpoint, evidence)
        _ = self.git("push", "-q", "origin", "main")
        ledger.handoff_merged(checkpoint)

        self.assertEqual(ledger.pending_handoffs(), ())
        with closing(sqlite3.connect(ledger.database)) as connection:
            stored = cast(tuple[str], connection.execute(
                "SELECT merge_commit FROM handoffs WHERE checkpoint=?", (checkpoint,),
            ).fetchone())
        self.assertEqual(stored, (merge_commit,))

    def test_reconcile_fast_forward_evidence_stops_at_the_pushed_tip(self) -> None:
        origin = self.root / "origin.git"
        _ = self.git("init", "-q", "--bare", str(origin))
        _ = self.git("remote", "add", "origin", str(origin))
        _ = self.git("push", "-q", "origin", "main")
        _ = self.git("checkout", "-q", "-b", "style/main")
        self.write(self.sources[0], "pub fn corrected() {}\n")
        checkpoint = self.commit("Style checkpoint")
        ledger = self.ledger()
        _ = ledger.reconcile("main", "style/main")
        ledger.handoff_add(checkpoint)
        ledger.handoff_send(checkpoint, "parent")
        _ = self.git("checkout", "-q", "main")
        _ = self.git("merge", "--ff-only", "-q", "style/main")
        _ = self.git("push", "-q", "origin", "main")
        self.write("README.md", "Unpushed product work.\n")
        _ = self.commit("Advance only local branch point")

        self.assertEqual(ledger.reconcile("main", "style/main"), (f"hand-off {checkpoint}: sent -> merged",))

        with closing(sqlite3.connect(ledger.database)) as connection:
            stored = cast(tuple[str], connection.execute(
                "SELECT merge_commit FROM handoffs WHERE checkpoint=?", (checkpoint,),
            ).fetchone())
        self.assertEqual(stored, (checkpoint,))

    def test_reconcile_recovers_merged_handoff_ticket_and_missing_unit_branch(self) -> None:
        rule_id = self.rule("read")
        self.set_tip(self.initial_tip)
        _ = self.git("checkout", "-q", "-b", "style/main")
        self.write(self.sources[0], "pub fn first_fix() {}\n")
        merged_checkpoint = self.commit("First style checkpoint")
        self.add_ticket("worker-1", [rule_id], unit="worker")
        _ = self.git("branch", "sample-style-worker", merged_checkpoint)
        _ = self.cli("ticket", "assign", "--id", "worker-1", "--branch", "sample-style-worker")
        for path in self.sources[:2]:
            self.record(rule_id, path, merged_checkpoint, ticket="worker-1")
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
        self.assertEqual(versions, [(max(style_ledger.MIGRATIONS),)])
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
        # A different change at the same site is its own row, and the first row keeps its change.
        self.assertNotEqual(duplicate.finding_id, first)
        self.assertEqual((duplicate.kind, duplicate.before, duplicate.after),
                         ("type", "different before", "different after"))
        self.assertEqual(ledger.named_held((first,))[0], original)
        proposal = self.root / "issues/rename.md"
        for finding in (first, second, duplicate.finding_id):
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
            _ = self.cli("held", "waive", "--id", first, "--person", "nate")
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
            _ = self.cli("held", "waive", "--id", finding, "--person", "nate")
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

        with mock.patch.object(rules, "load_rules", side_effect=catalog_then_edit) as load:
            with self.assertRaisesRegex(style_ledger.ChangedBeforeLock, second + ": rule changed while pinning; rerun"):
                ledger.ticket_add("read-1", "read", "read", "names", "crates/alpha", list(self.sources[:2]), [first, second])
        self.assertEqual(load.call_count, style_ledger.LOCK_ATTEMPTS)
        self.assertEqual(ledger.queued_tickets("read"), ())
        with closing(sqlite3.connect(ledger.database)) as connection:
            texts = cast(list[tuple[str]], connection.execute("SELECT blob FROM rule_texts").fetchall())
        self.assertEqual(texts, [])

    def test_rule_edit_during_one_pinning_attempt_pins_the_edited_text(self) -> None:
        rule = self.rule("first")
        ledger = self.ledger()
        path = self.rule_dir / "first.md"
        catalog = rules.load_rules(self.repo)
        original = rules.load_rules

        def edit_once(repo: Path | None) -> tuple[rules.Rule, ...]:
            if repo == self.repo and not path.read_text().endswith("Concurrent edit\n"):
                _ = path.write_text(path.read_text() + "Concurrent edit\n")
                return catalog
            return original(repo)

        with mock.patch.object(rules, "load_rules", side_effect=edit_once) as load:
            ledger.ticket_add("read-1", "read", "read", "names", "crates/alpha", list(self.sources[:2]), [rule])
        self.assertEqual(load.call_count, 2)
        pinned, = ledger.pinned_rule_texts("read-1")
        self.assertEqual(pinned.text, path.read_bytes())

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

    def test_unit_rename_moves_current_rows_and_keeps_ticket_ids_and_history(self) -> None:
        rule = self.rule("read")
        ledger = self.ledger()
        ledger.set_tip(self.initial_tip, "main")
        files = list(self.sources[:2])
        for number in (8, 9, 1, 2):
            ledger.ticket_add(f"names-unit-{number}", "names-unit", "read", "names", "crates/alpha", files, [rule])
        ledger.ticket_assign("names-unit-8", "main")
        for path in files:
            self.record(rule, path, self.initial_tip, ticket="names-unit-8")
        ledger.ticket_checkpoint("names-unit-8", self.initial_tip)
        _ = ledger.ticket_merge("names-unit-8")
        ledger.ticket_drop("names-unit-9")
        ledger.ticket_assign("names-unit-2", "main")
        _ = ledger.hold(rule, files[0], 1, "pub fn old() {}", "rename", "old", "new",
                        self.blob(rule), "names-unit-2")
        ledger.record_candidate("names-unit-2", files[0], 1, "Check names in public APIs.")
        _ = ledger.ticket_yield("names-unit-2", files[1])
        ledger.wait_for_sync("names-unit")
        ledger.wait_for_sync("types-unit")
        tables = ("tickets", "sync_waits", "rule_candidates", "held_findings", "ticket_yields")
        with closing(sqlite3.connect(ledger.database)) as connection:
            _ = connection.execute("UPDATE held_findings SET build_ticket='names-unit-2'")
            connection.commit()
            before = {table: cast(list[tuple[object, ...]], connection.execute(
                f"SELECT * FROM {table} ORDER BY rowid").fetchall()) for table in tables}
        new = "sample-style-names-unit"

        self.assertEqual(ledger.rename_unit("names-unit", new), style_ledger.UnitRenamed(2, 1))

        with closing(sqlite3.connect(ledger.database)) as connection:
            after = {table: cast(list[tuple[object, ...]], connection.execute(
                f"SELECT * FROM {table} ORDER BY rowid").fetchall()) for table in tables}
        expected = dict(before)
        expected["tickets"] = [
            (row[0], new, *row[2:]) if row[1] == "names-unit" and row[10] in ("queued", "assigned", "checkpointed") else row
            for row in before["tickets"]
        ]
        expected["sync_waits"] = [(new, *row[1:]) if row[0] == "names-unit" else row
                                  for row in before["sync_waits"]]
        self.assertEqual(after, expected)
        self.assertEqual([ticket["id"] for ticket in self.ledger().queued_tickets(new)], ["names-unit-1"])
        self.assertEqual([ticket["id"] for ticket in self.ledger().ticket_open(new)], ["names-unit-2"])
        self.assertEqual(ledger.ticket_open("names-unit"), ())

    def test_unit_rename_moves_a_checkpointed_ticket_without_changing_its_merge_evidence(self) -> None:
        rule = self.rule("read")
        ledger = self.ledger()
        ledger.set_tip(self.initial_tip, "main")
        ledger.ticket_add("names-unit-1", "names-unit", "read", "names", "crates/alpha",
                          list(self.sources[:2]), [rule])
        ledger.ticket_assign("names-unit-1", "main")
        for path in self.sources[:2]:
            self.record(rule, path, self.initial_tip, ticket="names-unit-1")
        ledger.ticket_checkpoint("names-unit-1", self.initial_tip)
        before = ledger.ticket_open("names-unit")[0]

        _ = ledger.rename_unit("names-unit", "sample-style-names-unit")

        self.assertEqual(ledger.ticket_open("sample-style-names-unit")[0],
                         {**before, "unit": "sample-style-names-unit"})
        self.assertEqual(ledger.next_ticket_id("sample-style-names-unit"), "sample-style-names-unit-1")

    def test_unit_rename_refuses_a_target_with_current_tickets_or_a_sync_wait(self) -> None:
        rule = self.rule("read")
        ledger = self.ledger()
        ledger.ticket_add("names-unit-1", "names-unit", "read", "names", "crates/alpha",
                          list(self.sources[:2]), [rule])
        ledger.wait_for_sync("names-unit")
        old_tickets, old_waits = ledger.queued_tickets("names-unit"), ledger.sync_waits()
        for state in ("queued", "assigned", "checkpointed", "wait"):
            with self.subTest(state=state):
                target = f"{state}-style-names-unit"
                if state == "wait":
                    ledger.wait_for_sync(target)
                else:
                    ledger.ticket_add(f"{target}-1", target, "read", "names", "crates/alpha",
                                      list(self.sources[:2]), [rule])
                    with closing(sqlite3.connect(ledger.database)) as connection:
                        _ = connection.execute("UPDATE tickets SET state=? WHERE unit=?", (state, target))
                        connection.commit()
                target_waits = ledger.sync_waits()
                with self.assertRaises(style_ledger.LedgerError):
                    _ = ledger.rename_unit("names-unit", target)
                self.assertEqual(ledger.queued_tickets("names-unit"), old_tickets)
                self.assertEqual(ledger.sync_waits(), target_waits)
        self.assertTrue(all(wait in ledger.sync_waits() for wait in old_waits))

    def test_unit_rename_refuses_other_families_and_nonstyle_ids_without_moving_rows(self) -> None:
        ledger = self.ledger()
        ledger.wait_for_sync("names-unit")
        before = ledger.sync_waits()
        for old, new in (("names-unit", "sample-style-types-unit"),
                         ("names-unit", "feature-unit"), ("feature-unit", "names-unit")):
            with self.subTest(old=old, new=new), self.assertRaises(style_ledger.LedgerError):
                _ = ledger.rename_unit(old, new)
            self.assertEqual(ledger.sync_waits(), before)

    def test_unit_rename_rolls_back_ticket_updates_when_the_wait_move_fails(self) -> None:
        rule = self.rule("read")
        ledger = self.ledger()
        ledger.ticket_add("names-unit-1", "names-unit", "read", "names", "crates/alpha",
                          list(self.sources[:2]), [rule])
        ledger.wait_for_sync("names-unit")
        tickets, waits = ledger.queued_tickets("names-unit"), ledger.sync_waits()
        with closing(sqlite3.connect(ledger.database)) as connection:
            _ = connection.execute("CREATE TRIGGER reject_wait_move BEFORE UPDATE OF unit ON sync_waits "
                                   + "BEGIN SELECT RAISE(ABORT, 'wait move rejected'); END")
            connection.commit()
        with self.assertRaises((sqlite3.Error, style_ledger.LedgerError)):
            _ = ledger.rename_unit("names-unit", "sample-style-names-unit")
        self.assertEqual(self.ledger().queued_tickets("names-unit"), tickets)
        self.assertEqual(self.ledger().sync_waits(), waits)
        self.assertEqual(ledger.queued_tickets("sample-style-names-unit"), ())

    def test_moved_ticket_can_be_readded_but_new_ids_must_use_the_new_prefix(self) -> None:
        rule = self.rule("read")
        ledger = self.ledger()
        files = list(self.sources[:2])
        ledger.ticket_add("names-unit-7", "names-unit", "read", "names", "crates/alpha", files, [rule])
        _ = ledger.rename_unit("names-unit", "sample-style-names-unit")
        before = ledger.queued_tickets("sample-style-names-unit")
        ledger.ticket_add("names-unit-7", "sample-style-names-unit", "read", "names", "crates/alpha", files, [rule])
        self.assertEqual(ledger.queued_tickets("sample-style-names-unit"), before)
        with self.assertRaises(style_ledger.LedgerError):
            ledger.ticket_add("names-unit-7", "sample-style-names-unit", "read", "names", "crates/beta", files, [rule])
        with self.assertRaises(style_ledger.LedgerError):
            ledger.ticket_add("names-unit-8", "sample-style-names-unit", "read", "names", "crates/alpha", files, [rule])
        self.assertEqual(ledger.next_ticket_id("sample-style-names-unit"), "sample-style-names-unit-1")
        ledger.ticket_add("sample-style-names-unit-1", "sample-style-names-unit", "read", "names",
                          "crates/alpha", files, [rule])
        self.assertEqual(self.ledger().next_ticket_id("sample-style-names-unit"), "sample-style-names-unit-2")
        self.assertEqual(ledger.next_ticket_id("names-unit"), "names-unit-8")

    def test_unit_rename_allows_target_history_and_moves_a_wait_without_tickets(self) -> None:
        rule = self.rule("read")
        ledger = self.ledger()
        target = "sample-style-names-unit"
        ledger.ticket_add(f"{target}-4", target, "read", "names", "crates/alpha",
                          list(self.sources[:2]), [rule])
        ledger.ticket_drop(f"{target}-4")
        ledger.wait_for_sync("names-unit")
        before = ledger.sync_waits()[0]

        self.assertEqual(ledger.rename_unit("names-unit", target), style_ledger.UnitRenamed(0, 1))

        after = self.ledger().sync_waits()[0]
        self.assertEqual((after.unit, after.since, after.token), (target, before.since, before.token))
        self.assertEqual(ledger.next_ticket_id(target), f"{target}-5")
        self.assertEqual(ledger.rename_unit("names-unit", target), style_ledger.UnitRenamed(0, 0))

    def test_unit_rename_cli_reports_counts_and_succeeds_again_without_moves(self) -> None:
        rule = self.rule("read")
        ledger = self.ledger()
        ledger.ticket_add("names-unit-1", "names-unit", "read", "names", "crates/alpha",
                          list(self.sources[:2]), [rule])
        ledger.wait_for_sync("names-unit")
        moved = self.cli("unit", "rename", "--from", "names-unit", "--to", "sample-style-names-unit")
        self.assertRegex(moved.stdout, r"1 tickets?")
        self.assertRegex(moved.stdout, r"1 (?:sync )?waits?")
        repeated = self.cli("unit", "rename", "--from", "names-unit", "--to", "sample-style-names-unit")
        self.assertRegex(repeated.stdout, r"0 tickets?")
        self.assertRegex(repeated.stdout, r"0 (?:sync )?waits?")
        self.assertEqual([ticket["id"] for ticket in self.ledger().queued_tickets("sample-style-names-unit")],
                         ["names-unit-1"])
        self.assertEqual([wait.unit for wait in self.ledger().sync_waits()], ["sample-style-names-unit"])

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
        for path in self.sources[:2]:
            self.record(rule, path, self.initial_tip, ticket="names-unit-11")
        ledger.ticket_checkpoint("names-unit-11", self.initial_tip)
        _ = ledger.ticket_merge("names-unit-11")
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
        self.assertEqual(versions, [(max(style_ledger.MIGRATIONS),)])

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

    def test_sync_waits_survive_reopen_keep_first_time_and_clear_only_the_named_unit(self) -> None:
        ledger = self.ledger()
        with mock.patch.object(style_ledger, "_now", return_value="2026-01-02T00:00:00+00:00"):
            ledger.wait_for_sync("types-unit")
            ledger.wait_for_sync("names-unit")
        first_wait = next(wait for wait in ledger.sync_waits() if wait.unit == "names-unit")
        with mock.patch.object(style_ledger, "_now", return_value="2026-01-03T00:00:00+00:00"):
            ledger.wait_for_sync("names-unit")
            ledger.wait_for_sync("modules-unit")
        with style_ledger.open_ledger(self.repo) as reopened:
            repeated_wait = next(wait for wait in reopened.sync_waits() if wait.unit == "names-unit")
            self.assertEqual(repeated_wait.token, first_wait.token)
            self.assertEqual([(wait.unit, wait.since) for wait in reopened.sync_waits()], [
                ("names-unit", "2026-01-02T00:00:00+00:00"),
                ("types-unit", "2026-01-02T00:00:00+00:00"),
                ("modules-unit", "2026-01-03T00:00:00+00:00"),
            ])
            reopened.clear_sync_wait("types-unit")
            reopened.clear_sync_wait("types-unit")
        self.assertEqual([wait.unit for wait in ledger.sync_waits()], ["names-unit", "modules-unit"])

    def test_existing_ledger_migrates_sync_waits_without_losing_tickets(self) -> None:
        rule = self.rule("read")
        self.set_tip(self.initial_tip)
        self.add_ticket("read-1", [rule])
        database = self.ledger().database
        with closing(sqlite3.connect(database)) as connection:
            _ = connection.execute("DROP TABLE IF EXISTS sync_waits")
            _ = connection.execute("DROP TABLE IF EXISTS ticket_yields")
            _ = connection.execute("DROP TABLE IF EXISTS ticket_widened_paths")
            _ = connection.execute("ALTER TABLE tickets DROP COLUMN cells")
            _ = connection.execute("ALTER TABLE judgments DROP COLUMN declared_scope")
            _ = connection.execute("ALTER TABLE judgments DROP COLUMN read_files")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN waive_provenance")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN waived_by")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN build_ticket")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN moved_from")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN withdrawn_by")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN person_read_by")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN duplicate_of")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN unwaived_by")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN followed_from")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN followed_from_line")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN followed_at")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN declaration_gone_at")
            _ = connection.execute("ALTER TABLE tickets DROP COLUMN finding_ids")
            _ = connection.execute("DROP TABLE rule_candidates")
            _ = connection.execute("ALTER TABLE tickets DROP COLUMN reservation_session_dir")
            _ = connection.execute("ALTER TABLE tickets DROP COLUMN reservation_phase")
            _ = connection.execute("ALTER TABLE processed_tips DROP COLUMN reason")
            _ = connection.execute("DROP TABLE refill_results")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN built_at")
            _ = connection.execute("UPDATE schema_version SET version=6")
            connection.commit()
        with style_ledger.open_ledger(self.repo) as migrated:
            migrated.wait_for_sync("names-unit")
            self.assertEqual([wait.unit for wait in migrated.sync_waits()], ["names-unit"])
            self.assertEqual(migrated.queued_tickets("read")[0]["id"], "read-1")
            self.assertEqual(migrated.summary().synced_tip, self.initial_tip)

    def test_ticket_merge_requires_checkpoint_on_the_stored_style_branch(self) -> None:
        rule = self.rule("read")
        _ = self.git("branch", "style/main")
        _ = self.cli("set-tip", "--tip", self.initial_tip, "--style-branch", "style/main")
        self.add_ticket("read-1", [rule])
        _ = self.cli("ticket", "assign", "--id", "read-1", "--branch", "main")
        self.write(self.sources[0], "pub fn fixed() {}\n")
        checkpoint = self.commit("Unit checkpoint")
        for path in self.sources[:2]:
            self.record(rule, path, checkpoint, ticket="read-1")
        _ = self.cli("ticket", "checkpoint", "--id", "read-1", "--commit", checkpoint)
        ledger = self.ledger()
        with self.assertRaisesRegex(style_ledger.LedgerError,
                                    f"read-1: checkpoint {checkpoint[:7]} is not on refs/heads/style/main"):
            _ = ledger.ticket_merge("read-1")
        self.assertEqual(ledger.ticket_open("read")[0]["state"], "checkpointed")
        _ = self.git("branch", "-f", "style/main", checkpoint)
        _ = ledger.ticket_merge("read-1")
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
