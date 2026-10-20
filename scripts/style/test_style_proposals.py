"""Proposal batches preserve findings and approved changes follow their ticket to completion."""

from __future__ import annotations

import io
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from typing import cast, final, override
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rules
import coverage_report
import style_ledger
import style_proposals
import add_style
import approved_renames
import style_rules
import proposal_issue
import proposals_table
from issues import frontmatter, open_issues

# Each crate declares the new name of the fixture rename, so a merge finds it applied, and still declares
# Size and Rate, so the renames of those variants are not applied.
APPLIED = "pub enum Kind { Count, Size, Rate }\n"

TEMPLATE = '''---
area: "[[{{AREA}}]]"
project: "[[{{PROJECT}}]]"
return:
  - "[[issues - {{SHORT}}.base]]"
  - "[[issues.base]]"
proposed_by: "{{PROPOSED_BY}}"
backlog_goal: "{{BACKLOG_GOAL}}"
backlog_alignment: "{{BACKLOG_ALIGNMENT}}"
backlog_impact: "{{BACKLOG_IMPACT}}"
backlog_urgency: "{{BACKLOG_URGENCY}}"
backlog_effort: "{{BACKLOG_EFFORT}}"
date_created: "[[{{DATE}}]]"
plan: "{{PLAN}}"
unit: "{{UNIT}}"
stage: backlog
status: open
tags: [issue]
---
'''


@final
class StyleProposalsTests(unittest.TestCase):
    root: Path = Path()
    repo: Path = Path()
    catalog: Path = Path()
    proposals: Path = Path()
    template: Path = Path()
    tip: str = ""
    rename: str = "global:rust/names.md"
    structure: str = "global:rust/modules.md"
    propose: str = "global:rust/propose.md"
    calls: list[tuple[str, ...]] = []

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = self.root / "sample"
        self.catalog = self.root / "nate_style/rust"
        self.proposals = self.root / "vault/issues"
        self.template = self.root / "issue.yaml"
        self.repo.mkdir()
        self.catalog.mkdir(parents=True)
        _ = self.template.write_text(TEMPLATE)
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "STYLE_LEDGER_STATE_DIR": str(self.root / "state"),
            "STYLE_HISTORY_NATE_STYLE_DIR": str(self.catalog.parent),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        })))
        _ = self.enterContext(mock.patch.object(style_rules, "NATE_STYLE_DIR", self.catalog.parent))
        _ = self.git("init", "-q", "--initial-branch=main")
        _ = self.git("config", "user.name", "Proposal tests")
        _ = self.git("config", "user.email", "proposals@example.invalid")
        self.write("Cargo.toml", '[workspace]\nmembers = ["alpha", "beta"]\n')
        for crate in ("alpha", "beta"):
            self.write(f"{crate}/Cargo.toml", f'[package]\nname = "{crate}"\nversion = "0.1.0"\n')
            self.write(f"{crate}/src/lib.rs", APPLIED + "fn value() -> u32 { 1 }\n")
        _ = self.git("add", "--all")
        _ = self.git("commit", "-qm", "Workspace")
        self.tip = self.git("rev-parse", "HEAD")
        _ = self.git("branch", "style/main")
        for name, metadata in (("names", "tier: C\nfamily: names\ninput_scope: file\n"),
                               ("modules", "tier: D\nfamily: modules\ninput_scope: crate\n"),
                               ("propose", "tier: C\nfamily: names\ninput_scope: file\nmode: propose\n")):
            _ = (self.catalog / (name + ".md")).write_text("---\n" + metadata + "---\n# Rule\n")
        with self.ledger() as ledger:
            ledger.set_tip(self.tip, "style/main")
        for root, origin in ((self.repo, "git@github.com:example/sample.git"),
                             (self.catalog.parent, "https://github.com/example/nate_style.git")):
            if root != self.repo:
                _ = self.git_at(root, "init", "-q", "--initial-branch=main")
            _ = self.git_at(root, "remote", "add", "origin", origin)
            if root != self.repo:
                _ = self.git_at(root, "config", "user.name", "Rule tests")
                _ = self.git_at(root, "config", "user.email", "rules@example.invalid")
                _ = self.git_at(root, "add", "--all")
                _ = self.git_at(root, "commit", "-qm", "Rules")
            _ = self.git_at(root, "update-ref", "refs/remotes/origin/main", self.git_at(root, "rev-parse", "HEAD"))
            # All refs are local; no test contacts the fake origin.
            _ = self.git_at(root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        self.calls = []
        _ = self.enterContext(mock.patch.object(style_proposals, "proposal_command", side_effect=self.issue_command))
        _ = self.enterContext(mock.patch.object(style_proposals, "renumber"))

    def ledger(self) -> style_ledger.Ledger:
        return style_ledger.open_ledger(self.repo)

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True, check=True).stdout.strip()

    def git_at(self, root: Path, *args: str) -> str:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout.strip()

    def write(self, path: str, text: str) -> None:
        destination = self.repo / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        _ = destination.write_text(text)

    def issue_command(self, *args: str) -> None:
        self.calls.append(args)
        command = [*args, "--template", str(self.template)] if args[0] == "create" else list(args)
        with redirect_stdout(io.StringIO()):
            result = proposal_issue.main(command)
        if result:
            raise style_ledger.LedgerError("stubbed issue command failed")

    def hold(self, ledger: style_ledger.Ledger, *, rule: str = "global:rust/names.md", crate: str = "alpha",
             line: int = 1, kind: style_ledger.FindingKind = "rename", record: bool = True,
             ticket: str = "outside", before: str = "Kind::Value", after: str = "Kind::Count") -> style_ledger.HeldFinding:
        blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule)
        finding = ledger.hold(rule, f"{crate}/src/lib.rs", line, f"site {line}", kind, before, after, blob, ticket)
        if record:
            ledger.record(rule, finding.path, self.tip, blob, "held", ticket=ticket, worker="test",
                          input_scope=style_ledger.DeclaredInputScope.RULE)
        return finding

    def file(self, ledger: style_ledger.Ledger) -> tuple[Path, ...]:
        with redirect_stdout(io.StringIO()):
            return style_proposals.file_proposals(ledger, self.proposals, "parent").filed

    def assign_ticket(self, ledger: style_ledger.Ledger, unit: str) -> str:
        ticket = f"{unit}-1"
        ledger.ticket_add(ticket, unit, "read", "names", "alpha", ["alpha/src/lib.rs"], [self.rename])
        ledger.ticket_assign(ticket, "main")
        return ticket

    def hold_root_structure(self, ledger: style_ledger.Ledger, count: int) -> None:
        self.write("Cargo.toml", (self.repo / "Cargo.toml").read_text()
                   + '[package]\nname = "root-package"\nversion = "0.1.0"\n')
        self.write("src/lib.rs", "fn root() {}\n")
        _ = self.git("add", "--all")
        _ = self.git("commit", "-qm", "Root package")
        self.tip = self.git("rev-parse", "HEAD")
        ledger.set_tip(self.tip, "style/main")
        blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == self.structure)
        for line in range(1, count + 1):
            _ = ledger.hold(self.structure, "src/lib.rs", line, f"root site {line}", "rename",
                            "before", "after", blob, "outside")

    def ranked_issue(self, name: str, goal: str, *, project: str = "sample",
                     status: str = "open", tags: str = "issue") -> None:
        self.proposals.mkdir(parents=True, exist_ok=True)
        _ = (self.proposals / (name + ".md")).write_text(
            f'---\nproject: "[[{project}]]"\nbacklog_goal: "{goal}"\nstatus: {status}\ntags: [{tags}]\n---\n')

    def test_decision_reuses_its_key_and_keeps_an_approval_for_later_findings(self) -> None:
        with self.ledger() as ledger:
            first = self.hold(ledger)
            issue = self.file(ledger)[0]
            self.assertNotIn("style_findings", frontmatter(issue))
            self.assertEqual(frontmatter(issue)["style_rule"], [self.rename])
            self.assertEqual(self.file(ledger), ())
            self.issue_command("approve", str(issue), "--showrunner", "parent", "--unit", "names-unit",
                               "--plan", "docs/units/names-unit.md")
            later = self.hold(ledger, line=2)
            self.assertEqual(self.file(ledger), (issue,))
            self.assertEqual(len(tuple(self.proposals.glob("*.md"))), 1)
            self.assertEqual(frontmatter(issue)["style_count"], ["2"])
            self.assertEqual(frontmatter(issue)["plan"], ["docs/units/names-unit.md"])
            self.assertEqual({finding.finding_id for finding in ledger.proposal_findings(issue)},
                             {first.finding_id, later.finding_id})
            self.assertEqual(tuple(item.path for item in style_proposals.approved_decisions(self.proposals, "names-unit")), (issue,))

    def test_refiling_writes_no_unchanged_decision_and_repairs_a_stale_count(self) -> None:
        with self.ledger() as ledger:
            _, _ = self.hold(ledger), self.hold(ledger, crate="beta")
            alpha, beta = self.file(ledger)
            before = {path: (path.stat().st_mtime_ns, path.read_bytes()) for path in (alpha, beta)}
            calls = len(self.calls)
            self.assertEqual(self.file(ledger), ())
            self.assertEqual(len(self.calls), calls)
            self.assertEqual({path: (path.stat().st_mtime_ns, path.read_bytes()) for path in (alpha, beta)}, before)
            # A crash after the ledger move and before the refresh leaves the count stale.
            late = self.hold(ledger, line=2)
            ledger.file_held(late.finding_id, alpha)
            self.assertEqual(frontmatter(alpha)["style_count"], ["1"])
            self.assertEqual(self.file(ledger), ())
            self.assertEqual(len(self.calls), calls)
            self.assertEqual(frontmatter(alpha)["style_count"], ["2"])
            self.assertEqual((beta.stat().st_mtime_ns, beta.read_bytes()), before[beta])
            # A no-change run reads the rule list once, and its git work does not grow with the decisions.
            runs: list[int] = []
            for added in (False, True):
                if added:
                    _ = self.hold(ledger, rule=self.structure, kind="type", line=3)
                    _ = self.file(ledger)
                with (mock.patch.object(rules, "load_rules", wraps=rules.load_rules) as load,
                      mock.patch.object(subprocess, "run", wraps=subprocess.run) as run):
                    self.assertEqual(self.file(ledger), ())
                self.assertLessEqual(load.call_count, 1)
                runs.append(run.call_count)
            self.assertEqual(len(open_issues(self.proposals, "style")), 3)
            self.assertEqual(runs[1], runs[0])

    def test_declined_decision_waives_later_findings_with_the_stored_answer(self) -> None:
        with self.ledger() as ledger:
            _ = self.hold(ledger)
            issue = self.file(ledger)[0]
            style_proposals.decline(ledger, issue, "Keep these names")
            _ = self.hold(ledger, line=2)
            self.assertEqual(self.file(ledger), ())
            self.assertEqual(frontmatter(issue)["style_answer_words"], ["Keep these names"])
            self.assertEqual(len(ledger.held_findings(style_ledger.HeldFindingsInState("waived"))), 2)
            self.assertEqual(len(tuple(self.proposals.glob("*.md"))), 1)
            with closing(sqlite3.connect(ledger.database)) as connection:
                self.assertEqual(connection.execute("SELECT verdict_detail FROM judgments").fetchall(), [("Keep these names",)])

    def test_a_new_rule_text_gets_a_separate_decision(self) -> None:
        with self.ledger() as ledger:
            _ = self.hold(ledger)
            old = self.file(ledger)[0]
            rule = self.catalog / "names.md"
            _ = rule.write_text(rule.read_text() + "New rule text.\n")
            _ = self.hold(ledger, line=2)
            new = self.file(ledger)[0]
            self.assertNotEqual(new, old)
            self.assertNotEqual(frontmatter(new)["style_rule_blob"], frontmatter(old)["style_rule_blob"])

    def test_decision_body_limits_examples_and_chooses_short_pairs_from_distinct_files(self) -> None:
        with self.ledger() as ledger:
            first = self.hold(ledger)
            for index in range(1, 5):
                path = f"alpha/src/example{index}.rs"
                self.write(path, "fn f() {}\n")
                _ = ledger.hold(self.rename, path, index, "example", "rename", "x" * index,
                                "y" * index, first.rule_blob, "outside")
            _ = self.hold(ledger, line=2)
            issue = self.file(ledger)[0]
            body = issue.read_text()
            self.assertIn("Findings: 6; files: 5", body)
            sites = re.findall(r"^Site: (.*)$", body, re.MULTILINE)
            self.assertEqual(sites, [f"alpha/src/example{i}.rs:{i}" for i in (1, 2, 3)])
            self.assertNotIn("example4.rs", body)

    def legacy_issue(self, ledger: style_ledger.Ledger, name: str, *findings: style_ledger.HeldFinding) -> Path:
        self.proposals.mkdir(parents=True, exist_ok=True)
        issue = self.proposals / (name + ".md")
        identities = ", ".join(finding.finding_id for finding in findings)
        _ = issue.write_text(f'---\nstatus: open\nstage: backlog\ntags: [issue, proposal, style]\n'
                             + f'style_findings: [{identities}]\nunit: names-unit\nplan: unit.md\n---\n# Old\n')
        for finding in findings:
            ledger.file_held(finding.finding_id, issue)
        return issue

    def test_migration_dry_run_moves_nothing_and_merges_three_issues_idempotently(self) -> None:
        with self.ledger() as ledger:
            findings = tuple(self.hold(ledger, line=i) for i in (1, 2, 3))
            old = tuple(self.legacy_issue(ledger, f"old{i}", finding) for i, finding in enumerate(findings))
            before = {path: path.read_bytes() for path in old}
            with redirect_stdout(io.StringIO()) as output:
                style_proposals.migrate(ledger, self.proposals, "parent", dry_run=True)
            self.assertIn("create", output.getvalue())
            self.assertIn("move 3", output.getvalue())
            self.assertEqual({path: path.read_bytes() for path in self.proposals.glob("*.md")}, before)
            self.assertEqual(tuple(item.state.proposal for item in ledger.held_findings(style_ledger.AllHeldFindings())
                                   if isinstance(item.state, style_ledger.Filed)), old)
            with redirect_stdout(io.StringIO()):
                style_proposals.migrate(ledger, self.proposals, "parent")
            decisions = tuple(path for path in self.proposals.glob("*.md") if "style_rule" in frontmatter(path))
            self.assertEqual(len(decisions), 1)
            decision = decisions[0]
            self.assertEqual(len(ledger.proposal_findings(decision)), 3)
            self.assertEqual(frontmatter(decision)["unit"], ["names-unit"])
            self.assertTrue(all(frontmatter(path)["status"] == ["closed"] for path in old))
            snapshot = {path: path.read_bytes() for path in self.proposals.glob("*.md")}
            with redirect_stdout(io.StringIO()):
                style_proposals.migrate(ledger, self.proposals, "parent")
            self.assertEqual({path: path.read_bytes() for path in snapshot}, snapshot)

    def test_migration_cli_dry_run_leaves_the_ledger_and_issue_bytes_unchanged(self) -> None:
        with self.ledger() as ledger:
            finding = self.hold(ledger)
            old = self.legacy_issue(ledger, "old", finding)
            database = ledger.database
        state = database.parent.parent
        before = {path: path.read_bytes() for path in state.rglob("*") if path.is_file()}
        issue_bytes = old.read_bytes()
        with redirect_stdout(io.StringIO()) as output:
            result = style_proposals.main(["migrate", "--repo", str(self.repo), "--proposals", str(self.proposals), "--parent", "parent", "--dry-run"])
        self.assertEqual(result, 0)
        self.assertIn("move 1", output.getvalue())
        self.assertEqual({path: path.read_bytes() for path in state.rglob("*") if path.is_file()}, before)
        self.assertEqual({path: path.read_bytes() for path in self.proposals.glob("*.md")}, {old: issue_bytes})

    def test_migration_resumes_after_moves_before_closes(self) -> None:
        with self.ledger() as ledger:
            finding = self.hold(ledger)
            old = self.legacy_issue(ledger, "old", finding)
            original = style_proposals.proposal_command
            def interrupt(*args: str) -> None:
                if args[0] == "close":
                    raise OSError("before close")
                original(*args)
            with mock.patch.object(style_proposals, "proposal_command", side_effect=interrupt):
                with self.assertRaisesRegex(OSError, "before close"):
                    style_proposals.migrate(ledger, self.proposals, "parent")
            self.assertEqual(frontmatter(old)["status"], ["open"])
            with redirect_stdout(io.StringIO()):
                style_proposals.migrate(ledger, self.proposals, "parent")
            self.assertEqual(frontmatter(old)["status"], ["closed"])
            decision = next(path for path in self.proposals.glob("*.md") if "style_rule" in frontmatter(path))
            self.assertEqual(len(ledger.proposal_findings(decision)), 1)

    def migration_lines(self, ledger: style_ledger.Ledger, *, dry_run: bool) -> list[str]:
        with redirect_stdout(io.StringIO()) as output:
            style_proposals.migrate(ledger, self.proposals, "parent", dry_run=dry_run)
        return [line for line in output.getvalue().splitlines()
                if line.split(" ", 1)[0] in {"kept", "refused", "create", "move", "waive", "reopen", "close", "totals:"}]

    def off_rule_folder(self, ledger: style_ledger.Ledger) -> tuple[Path, Path]:
        """A decision, an old style issue, an off-rule issue and an issue from another proposer."""
        decision, = self.file(ledger)
        legacy = self.legacy_issue(ledger, "old", self.hold(ledger, line=2))
        off_rule = self.proposals / "tidy names.md"
        _ = off_rule.write_text('---\nproposed_by: "types-unit (sample-style)"\nstatus: open\ntags: [issue]\n---\n# Tidy names\n')
        _ = (self.proposals / "other.md").write_text(off_rule.read_text().replace("sample-style", "other-style"))
        self.assertEqual(frontmatter(decision)["proposed_by"], ["sample-style"])
        return legacy, off_rule

    def skipped_lines(self, ledger: style_ledger.Ledger, *, dry_run: bool) -> list[str]:
        with redirect_stdout(io.StringIO()) as output:
            style_proposals.migrate(ledger, self.proposals, "parent", dry_run=dry_run)
        lines = output.getvalue().splitlines()
        return [line for line in lines if line.startswith("skipped:")] + [lines[-1].rsplit(", ", 1)[1]]

    def test_migration_moves_an_old_issue_and_lists_an_off_rule_issue_without_writing_it(self) -> None:
        with self.ledger() as ledger:
            _ = self.hold(ledger)
            legacy, off_rule = self.off_rule_folder(ledger)
            before = off_rule.read_bytes()
            self.assertEqual(self.skipped_lines(ledger, dry_run=False), [f"skipped: off-rule {off_rule}", "off-rule 1"])
            self.assertEqual(frontmatter(legacy)["status"], ["closed"])
            self.assertEqual(off_rule.read_bytes(), before)
            self.assertEqual([call for call in self.calls if str(off_rule) in call], [])

    def test_migration_dry_run_lists_the_same_off_rule_issues(self) -> None:
        with self.ledger() as ledger:
            _ = self.hold(ledger)
            _, off_rule = self.off_rule_folder(ledger)
            before = {path: path.read_bytes() for path in self.proposals.glob("*.md")}
            self.assertEqual(self.skipped_lines(ledger, dry_run=True), [f"skipped: off-rule {off_rule}", "off-rule 1"])
            self.assertEqual({path: path.read_bytes() for path in self.proposals.glob("*.md")}, before)

    def test_migration_takes_only_findings_filed_to_the_issue(self) -> None:
        with self.ledger() as ledger:
            owned, elsewhere, waived = self.hold(ledger), self.hold(ledger, line=2), self.hold(ledger, line=3)
            old = self.legacy_issue(ledger, "old", owned)
            _ = old.write_text(old.read_text().replace(f"style_findings: [{owned.finding_id}]",
                                                       f"style_findings: [{owned.finding_id}, {elsewhere.finding_id}, {waived.finding_id}]"))
            other = self.proposals / "other.md"
            ledger.file_held(elsewhere.finding_id, other)
            ledger.waive_held(waived.finding_id)
            empty = self.legacy_issue(ledger, "empty", self.hold(ledger, crate="beta"))
            beta = ledger.proposal_findings(empty)[0]
            ledger.waive_held(beta.finding_id)
            decision = self.proposals / style_proposals.batch_name(style_proposals.decision_key(owned, ("alpha", "beta")))
            expected = [
                f"kept {beta.finding_id}: waived, not filed to {empty}",
                f"kept {elsewhere.finding_id}: filed, not filed to {old}",
                f"kept {waived.finding_id}: waived, not filed to {old}",
                f"create {decision}", f"move 1 findings to {decision}",
                f"close {empty}: no filed findings", f"close {old}: merged into {decision.name}",
                "totals: create 1, move 1, close 2, off-rule 0",
            ]
            before = {path: path.read_bytes() for path in self.proposals.glob("*.md")}
            self.assertEqual(self.migration_lines(ledger, dry_run=True), expected)
            self.assertEqual({path: path.read_bytes() for path in self.proposals.glob("*.md")}, before)
            self.assertEqual(self.migration_lines(ledger, dry_run=False), expected)
            self.assertEqual([item.finding_id for item in ledger.proposal_findings(decision)], [owned.finding_id])
            self.assertEqual([item.finding_id for item in ledger.proposal_findings(other)], [elsewhere.finding_id])
            self.assertIsInstance(ledger.held_findings(style_ledger.HeldFindingsInState("waived"))[0].state, style_ledger.Waived)
            self.assertEqual(frontmatter(old)["reason"], [f"merged into {decision.name}"])
            self.assertEqual(frontmatter(empty)["reason"], ["no filed findings"])
            self.assertEqual(len(tuple(path for path in self.proposals.glob("*.md") if "style_rule" in frontmatter(path))), 1)

    def test_migration_refuses_an_issue_with_a_queued_ticket_made_before_decisions(self) -> None:
        with self.ledger() as ledger:
            finding = self.hold(ledger)
            old = self.legacy_issue(ledger, "old", finding)
            ledger.ticket_add("names-unit-1", "names-unit", "proposal", "names", "alpha", [finding.path], [self.rename],
                              proposal=str(old), finding_ids=(finding.finding_id,))
            with closing(sqlite3.connect(ledger.database)) as connection, connection:
                _ = connection.execute("UPDATE tickets SET finding_ids='[]'")
                _ = connection.execute("UPDATE held_findings SET build_ticket=''")
            before = old.read_bytes()
            self.assertEqual(self.migration_lines(ledger, dry_run=False),
                             [f"refused {old}: live build ticket names-unit-1 (queued)", "totals: create 0, move 0, close 0, off-rule 0"])
            self.assertEqual(old.read_bytes(), before)
            self.assertEqual(ledger.proposal_findings(old)[0].finding_id, finding.finding_id)

    def test_migration_stopped_before_its_moves_ends_like_one_clean_run(self) -> None:
        results: list[tuple[dict[str, bytes], dict[str, tuple[str, str]]]] = []
        for name, stopped in (("clean", False), ("stopped", True)):
            with mock.patch.dict(os.environ, {"STYLE_LEDGER_STATE_DIR": str(self.root / name / "state")}):
                self.proposals = self.root / name / "vault/issues"
                with self.ledger() as ledger:
                    ledger.set_tip(self.tip, "style/main")
                    findings = (self.hold(ledger), self.hold(ledger, line=2), self.hold(ledger, crate="beta"))
                    for index, finding in enumerate(findings):
                        _ = self.legacy_issue(ledger, f"old{index}", finding)
                    if stopped:
                        with (mock.patch.object(ledger, "move_issue_findings", side_effect=style_ledger.LedgerError("stopped")),
                              redirect_stdout(io.StringIO()), self.assertRaisesRegex(style_ledger.LedgerError, "stopped")):
                            style_proposals.migrate(ledger, self.proposals, "parent")
                        self.assertEqual(len(tuple(self.proposals.glob("style *.md"))), 2)
                    with redirect_stdout(io.StringIO()):
                        style_proposals.migrate(ledger, self.proposals, "parent")
                    states = {item.finding_id: (style_ledger.held_state_name(item.state),
                                                item.state.proposal.name if isinstance(item.state, style_ledger.Filed) else "")
                              for item in ledger.held_findings(style_ledger.AllHeldFindings())}
                results.append(({path.name: path.read_bytes() for path in self.proposals.glob("*.md")}, states))
        self.assertEqual(len(tuple(name for name in results[0][0] if name.startswith("style "))), 2)
        self.assertEqual(results[1], results[0])

    def test_migration_waives_moved_findings_with_the_words_of_a_declined_decision(self) -> None:
        with self.ledger() as ledger:
            _ = self.hold(ledger)
            decision = self.file(ledger)[0]
            style_proposals.decline(ledger, decision, "No")
            later = self.hold(ledger, line=2)
            old = self.legacy_issue(ledger, "old", later)
            expected = [f"move 1 findings to {decision}", f"waive 1 findings in {decision}: declined: No",
                        f"close {old}: merged into {decision.name}", "totals: create 0, move 1, close 1, off-rule 0"]
            self.assertEqual(self.migration_lines(ledger, dry_run=True), expected)
            self.assertEqual(self.migration_lines(ledger, dry_run=False), expected)
            moved = next(item for item in ledger.proposal_findings(decision) if item.finding_id == later.finding_id)
            self.assertIsInstance(moved.state, style_ledger.Waived)
            self.assertEqual(frontmatter(decision)["status"], ["closed"])
            with closing(sqlite3.connect(ledger.database)) as connection:
                self.assertEqual(connection.execute("SELECT verdict,verdict_detail FROM judgments").fetchall(), [("waived", "No")])

    def test_migration_reopens_a_built_decision_with_its_approval_for_moved_findings(self) -> None:
        with self.ledger() as ledger:
            finding = self.hold(ledger)
            decision = self.file(ledger)[0]
            self.issue_command("approve", str(decision), "--showrunner", "parent", "--unit", "names-unit", "--plan", "unit.md")
            ledger.ticket_add("names-unit-1", "names-unit", "proposal", "names", "alpha", [finding.path], [self.rename],
                              proposal=str(decision), finding_ids=(finding.finding_id,))
            ledger.ticket_assign("names-unit-1", "main")
            ledger.ticket_checkpoint("names-unit-1", self.tip)
            _ = ledger.ticket_merge("names-unit-1")
            style_proposals.complete(ledger)
            self.assertEqual(style_proposals.approved_decisions(self.proposals, "names-unit"), ())
            later = self.hold(ledger, line=2)
            old = self.legacy_issue(ledger, "old", later)
            self.assertIn(f"reopen {decision}", self.migration_lines(ledger, dry_run=False))
            self.assertEqual(frontmatter(decision)["status"], ["open"])
            self.assertEqual(frontmatter(old)["status"], ["closed"])
            self.assertEqual(tuple(item.path for item in style_proposals.approved_decisions(self.proposals, "names-unit")),
                             (decision,))
            moved = next(item for item in ledger.proposal_findings(decision) if item.finding_id == later.finding_id)
            self.assertEqual(moved.state, style_ledger.Filed(decision))

    def test_migration_rerun_leaves_a_decision_built_after_the_stop_closed(self) -> None:
        with self.ledger() as ledger:
            finding = self.hold(ledger)
            old = self.legacy_issue(ledger, "old", finding)
            original = style_proposals.proposal_command

            def interrupt(*args: str) -> None:
                if args[0] == "close":
                    raise OSError("before close")
                original(*args)
            with (mock.patch.object(style_proposals, "proposal_command", side_effect=interrupt),
                  redirect_stdout(io.StringIO()), self.assertRaisesRegex(OSError, "before close")):
                style_proposals.migrate(ledger, self.proposals, "parent")
            decision = next(self.proposals.glob("style *.md"))
            self.assertEqual(ledger.proposal_findings(decision)[0].state, style_ledger.Filed(decision))
            self.assertEqual(frontmatter(old)["status"], ["open"])
            ledger.ticket_add("names-unit-1", "names-unit", "proposal", "names", "alpha", [finding.path], [self.rename],
                              proposal=str(decision), finding_ids=(finding.finding_id,))
            ledger.ticket_assign("names-unit-1", "main")
            ledger.ticket_checkpoint("names-unit-1", self.tip)
            _ = ledger.ticket_merge("names-unit-1")
            with redirect_stdout(io.StringIO()):
                style_proposals.complete(ledger)
            self.assertEqual(frontmatter(decision)["status"], ["closed"])
            lines = self.migration_lines(ledger, dry_run=False)
            self.assertNotIn(f"reopen {decision}", lines)
            self.assertEqual(frontmatter(decision)["status"], ["closed"])
            self.assertEqual(frontmatter(decision)["reason"], ["built in names-unit-1"])
            self.assertEqual(frontmatter(old)["status"], ["closed"])
            self.assertNotIn("reopen", [call[0] for call in self.calls])

    def test_migration_rerun_after_a_stop_credits_each_moved_finding_to_its_old_issue(self) -> None:
        results: list[tuple[dict[str, list[str]], list[str]]] = []
        for name, stopped in (("clean", False), ("stopped", True)):
            with mock.patch.dict(os.environ, {"STYLE_LEDGER_STATE_DIR": str(self.root / name / "state")}):
                self.proposals = self.root / name / "vault/issues"
                with self.ledger() as ledger:
                    ledger.set_tip(self.tip, "style/main")
                    first, second = self.hold(ledger), self.hold(ledger, line=2)
                    # The first old issue also lists a finding that the second old issue owns.
                    early = self.legacy_issue(ledger, "a-old", first)
                    _ = early.write_text(early.read_text().replace(
                        f"style_findings: [{first.finding_id}]", f"style_findings: [{first.finding_id}, {second.finding_id}]"))
                    late = self.legacy_issue(ledger, "b-old", second)
                    if stopped:
                        original = style_proposals.proposal_command
                        def interrupt(*args: str) -> None:
                            if args[0] == "close":
                                raise OSError("before close")
                            original(*args)
                        with (mock.patch.object(style_proposals, "proposal_command", side_effect=interrupt),
                              redirect_stdout(io.StringIO()), self.assertRaisesRegex(OSError, "before close")):
                            style_proposals.migrate(ledger, self.proposals, "parent")
                    lines = self.migration_lines(ledger, dry_run=False)
                results.append(({path.name: frontmatter(path)["reason"] for path in (early, late)},
                                [line.replace(str(self.proposals), "") for line in lines if line.startswith(("kept", "close"))]))
        self.assertEqual(results[1], results[0])
        reasons = results[0][0]
        self.assertEqual(reasons["a-old.md"], reasons["b-old.md"])
        self.assertTrue(reasons["b-old.md"][0].startswith("merged into style names alpha "))

    def test_migration_leaves_an_issue_with_an_assigned_build_ticket_unchanged(self) -> None:
        with self.ledger() as ledger:
            finding = self.hold(ledger)
            old = self.legacy_issue(ledger, "old", finding)
            ledger.ticket_add("names-unit-1", "names-unit", "proposal", "names", "alpha", [finding.path], [self.rename], proposal=str(old), finding_ids=(finding.finding_id,))
            ledger.ticket_assign("names-unit-1", "main")
            before = old.read_bytes()
            with redirect_stdout(io.StringIO()) as output:
                style_proposals.migrate(ledger, self.proposals, "parent")
            self.assertIn("live", output.getvalue())
            self.assertEqual(old.read_bytes(), before)
            self.assertEqual(ledger.proposal_findings(old)[0].finding_id, finding.finding_id)

    def test_complete_keeps_a_decision_open_until_every_finding_is_resolved(self) -> None:
        with self.ledger() as ledger:
            first, second = self.hold(ledger), self.hold(ledger, line=2)
            issue = self.file(ledger)[0]
            self.issue_command("approve", str(issue), "--showrunner", "parent", "--unit", "names-unit", "--plan", "unit.md")
            ledger.ticket_add("names-unit-1", "names-unit", "proposal", "names", "alpha", [first.path], [self.rename],
                              proposal=str(issue), finding_ids=(first.finding_id,))
            ledger.ticket_assign("names-unit-1", "main")
            ledger.ticket_checkpoint("names-unit-1", self.tip)
            _ = ledger.ticket_merge("names-unit-1")
            style_proposals.complete(ledger)
            self.assertEqual(frontmatter(issue)["status"], ["open"])
            _ = ledger.decline_findings((second.finding_id,), "Keep the second")
            style_proposals.complete(ledger)
            self.assertEqual(frontmatter(issue)["status"], ["closed"])
            self.assertIn("names-unit-1", frontmatter(issue)["reason"][0])

    def test_declining_a_partly_built_decision_keeps_built_findings_and_waives_filed_ones(self) -> None:
        with self.ledger() as ledger:
            first, second = self.hold(ledger), self.hold(ledger, line=2)
            issue = self.file(ledger)[0]
            ledger.ticket_add("names-unit-1", "names-unit", "proposal", "names", "alpha", [first.path], [self.rename],
                              proposal=str(issue), finding_ids=(first.finding_id,))
            ledger.ticket_assign("names-unit-1", "main")
            ledger.ticket_checkpoint("names-unit-1", self.tip)
            _ = ledger.ticket_merge("names-unit-1")
            style_proposals.decline(ledger, issue, "Keep the current code")
            states = {finding.finding_id: type(finding.state) for finding in ledger.proposal_findings(issue)}
            self.assertEqual(states, {first.finding_id: style_ledger.Built, second.finding_id: style_ledger.Waived})
            with closing(sqlite3.connect(ledger.database)) as connection:
                verdict = cast(tuple[str, str], connection.execute(
                    "SELECT verdict,verdict_detail FROM judgments WHERE rule=?", (self.rename,),
                ).fetchone())
            self.assertEqual(verdict, ("waived", "Keep the current code"))
            self.assertEqual(frontmatter(issue)["style_answer_words"], ["Keep the current code"])
            self.assertEqual(frontmatter(issue)["status"], ["closed"])

    def test_built_decision_reopens_with_its_approval_for_a_later_finding(self) -> None:
        with self.ledger() as ledger:
            finding = self.hold(ledger)
            issue = self.file(ledger)[0]
            self.issue_command("approve", str(issue), "--showrunner", "parent", "--unit", "names-unit", "--plan", "unit.md")
            ledger.ticket_add("names-unit-1", "names-unit", "proposal", "names", "alpha", [finding.path], [self.rename],
                              proposal=str(issue), finding_ids=(finding.finding_id,))
            ledger.ticket_assign("names-unit-1", "main")
            ledger.ticket_checkpoint("names-unit-1", self.tip)
            _ = ledger.ticket_merge("names-unit-1")
            style_proposals.complete(ledger)
            self.assertEqual(frontmatter(issue)["status"], ["closed"])
            _ = self.hold(ledger, line=2)
            self.assertEqual(self.file(ledger), (issue,))
            self.assertEqual(frontmatter(issue)["status"], ["open"])
            self.assertEqual(frontmatter(issue)["unit"], ["names-unit"])
            self.assertEqual(frontmatter(issue)["style_count"], ["2"])
            style_proposals.complete(ledger)
            self.assertEqual(frontmatter(issue)["status"], ["open"])

    def test_a_decision_closed_with_nothing_left_to_build_reopens_with_its_approval_for_a_later_finding(self) -> None:
        self.write("alpha/src/gone.rs", "fn gone() {}\n")
        _ = self.git("add", "--all")
        _ = self.git("commit", "-qm", "Add a file")
        self.tip = self.git("rev-parse", "HEAD")
        with self.ledger() as ledger:
            ledger.set_tip(self.tip, "style/main")
            blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == self.rename)
            finding = ledger.hold(self.rename, "alpha/src/gone.rs", 1, "site 1", "rename", "value", "count", blob, "outside")
            ledger.record(self.rename, finding.path, self.tip, blob, "held", ticket="outside", worker="test",
                          input_scope=style_ledger.DeclaredInputScope.RULE)
            issue = self.file(ledger)[0]
            self.issue_command("approve", str(issue), "--showrunner", "parent", "--unit", "names-unit", "--plan", "unit.md")
            _ = ledger.decline_findings((finding.finding_id,), f"{finding.path} is not in synced commit")
            _ = self.git("rm", "-q", finding.path)
            _ = self.git("commit", "-qm", "Remove the file")
            self.tip = self.git("rev-parse", "HEAD")
            ledger.set_tip(self.tip, "style/main")
            with redirect_stdout(io.StringIO()):
                style_proposals.complete(ledger)
            self.assertEqual(frontmatter(issue)["reason"], [f"nothing left to build: {finding.path} not in synced commit {self.tip}"])
            self.assertEqual(style_proposals.approved_decisions(self.proposals, "names-unit"), ())
            later = self.hold(ledger, line=2)
            self.assertEqual(self.file(ledger), (issue,))
            self.assertEqual((frontmatter(issue)["status"], frontmatter(issue)["unit"]), (["open"], ["names-unit"]))
            self.assertEqual(ledger.proposal_findings(issue)[-1].state, style_ledger.Filed(issue))
            self.assertEqual(ledger.proposal_findings(issue)[-1].finding_id, later.finding_id)

    def approved_decision(self, ledger: style_ledger.Ledger, *findings: style_ledger.HeldFinding) -> Path:
        issue, = self.file(ledger)
        self.assertEqual({item.finding_id for item in ledger.proposal_findings(issue)},
                         {finding.finding_id for finding in findings})
        self.issue_command("approve", str(issue), "--showrunner", "parent", "--unit", "names-unit", "--plan", "unit.md")
        return issue

    def merge_ticket(self, ledger: style_ledger.Ledger, ticket: str, issue: Path,
                     *findings: style_ledger.HeldFinding) -> tuple[style_ledger.FindingNotBuilt, ...]:
        ledger.ticket_add(ticket, "names-unit", "proposal", "names", "alpha", ["alpha/src/lib.rs"], [self.rename],
                          proposal=str(issue), finding_ids=tuple(finding.finding_id for finding in findings))
        ledger.ticket_assign(ticket, "main")
        ledger.ticket_checkpoint(ticket, self.tip)
        return ledger.ticket_merge(ticket)

    def decision_closed_as_built(self, ledger: style_ledger.Ledger) -> tuple[Path, tuple[style_ledger.HeldFinding, ...]]:
        """An approved decision closed as built with only the first of its 3 renames in the code."""
        findings = (self.hold(ledger), self.hold(ledger, line=2, before="Kind::Size", after="Kind::Length"),
                    self.hold(ledger, line=3, before="Kind::Rate", after="Kind::Speed"))
        issue = self.approved_decision(ledger, *findings)
        _ = self.merge_ticket(ledger, "names-unit-1", issue, *findings)
        self.built_before_the_check(ledger, *findings[1:])
        with redirect_stdout(io.StringIO()):
            style_proposals.complete(ledger)
        self.assertEqual(frontmatter(issue)["status"], ["closed"])
        return issue, findings

    def closed_old_issue(self, ledger: style_ledger.Ledger,
                         crate: str = "alpha") -> tuple[Path, style_ledger.HeldFinding, style_ledger.HeldFinding]:
        """An old issue closed as built with only the first of its 2 renames in the code."""
        applied = self.hold(ledger, crate=crate)
        missing = self.hold(ledger, crate=crate, line=2, before="Kind::Size", after="Kind::Length")
        old = self.legacy_issue(ledger, "old", applied, missing)
        self.built_before_the_check(ledger, applied, missing)
        self.issue_command("close", str(old), "--reason", "built in names-unit-1")
        return old, applied, missing

    def built_before_the_check(self, ledger: style_ledger.Ledger, *findings: style_ledger.HeldFinding) -> None:
        """Mark the findings built as a merge did before the applied check, whether or not their change is in the code."""
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.executemany("UPDATE held_findings SET state='built' WHERE finding_id=?",
                                       [(finding.finding_id,) for finding in findings])

    def written(self, ledger: style_ledger.Ledger) -> tuple[tuple[style_ledger.HeldFinding, ...], dict[Path, bytes]]:
        """The ledger's findings and the bytes of each issue, to show that a run wrote nothing."""
        return (tuple(ledger.held_findings(style_ledger.AllHeldFindings())),
                {path: path.read_bytes() for path in self.proposals.glob("*.md")})

    def reopen_output(self, ledger: style_ledger.Ledger, issue: Path) -> str:
        with redirect_stdout(io.StringIO()) as output:
            style_proposals.reopen(ledger, issue, "parent")
        return output.getvalue()

    def test_complete_keeps_a_decision_open_until_its_unapplied_rename_is_built(self) -> None:
        with self.ledger() as ledger:
            applied = self.hold(ledger)
            missing = self.hold(ledger, line=2, before="Kind::Size", after="Kind::Length")
            issue = self.approved_decision(ledger, applied, missing)
            self.assertEqual(self.merge_ticket(ledger, "names-unit-1", issue, applied, missing),
                             (style_ledger.FindingNotBuilt(missing.finding_id, approved_renames.RenameNotApplied(missing.path)),))
            with redirect_stdout(io.StringIO()):
                style_proposals.complete(ledger)
            self.assertEqual(frontmatter(issue)["status"], ["open"])
            self.write("alpha/src/lib.rs", "pub enum Kind { Count, Length }\n")
            _ = self.git("commit", "-qam", "Rename Size to Length")
            self.tip = self.git("rev-parse", "HEAD")
            _ = self.git("update-ref", "refs/heads/style/main", self.tip)
            ledger.set_tip(self.tip, "style/main")
            self.assertEqual(self.merge_ticket(ledger, "names-unit-2", issue, missing), ())
            with redirect_stdout(io.StringIO()):
                style_proposals.complete(ledger)
            self.assertEqual(frontmatter(issue)["status"], ["closed"])
            self.assertIn("names-unit-2", frontmatter(issue)["reason"][0])

    def test_reopen_files_again_the_renames_a_built_decision_lacks_and_opens_it_approved(self) -> None:
        with self.ledger() as ledger:
            issue, findings = self.decision_closed_as_built(ledger)
            self.assertEqual(self.reopen_output(ledger, issue), "".join(
                f"filed again {finding.finding_id} in {issue}: {finding.before} -> {finding.after} "
                + f"not applied in {finding.path}\n" for finding in findings[1:]))
            held = {finding.finding_id: finding for finding in ledger.proposal_findings(issue)}
            self.assertIsInstance(held[findings[0].finding_id].state, style_ledger.Built)
            for finding in findings[1:]:
                self.assertEqual(held[finding.finding_id].state, style_ledger.Filed(issue))
                self.assertIsInstance(held[finding.finding_id].build_ticket, style_ledger.NoBuildTicket)
            self.assertEqual(frontmatter(issue)["status"], ["open"])
            self.assertEqual([item.path for item in style_proposals.approved_decisions(self.proposals, "names-unit")], [issue])

    def test_reopen_moves_the_unapplied_renames_of_a_closed_old_issue_to_an_approved_decision(self) -> None:
        with self.ledger() as ledger:
            old, applied, missing = self.closed_old_issue(ledger)
            output = self.reopen_output(ledger, old)
            decision, = (path for path in self.proposals.glob("*.md") if "style_rule" in frontmatter(path))
            # Reopen prints only its own lines: not the lines of migrate's move, and not the notices of a new decision.
            self.assertEqual(output, f"filed again {missing.finding_id} in {decision}: Kind::Size -> Kind::Length "
                             + f"not applied in {missing.path}\n")
            self.assertEqual([(item.finding_id, item.state) for item in ledger.proposal_findings(decision)],
                             [(missing.finding_id, style_ledger.Filed(decision))])
            self.assertEqual([(item.finding_id, isinstance(item.state, style_ledger.Built))
                              for item in ledger.proposal_findings(old)], [(applied.finding_id, True)])
            self.assertEqual([item.path for item in style_proposals.approved_decisions(self.proposals, "names-unit")], [decision])
            self.assertEqual(frontmatter(old)["status"], ["closed"])

    def test_reopen_dry_run_prints_the_lines_and_writes_nothing(self) -> None:
        with self.ledger() as ledger:
            issue, _ = self.decision_closed_as_built(ledger)
            database = ledger.database
        state = database.parent.parent
        before = {path: path.read_bytes() for path in state.rglob("*") if path.is_file()}
        issues = {path: path.read_bytes() for path in self.proposals.glob("*.md")}
        with redirect_stdout(io.StringIO()) as output:
            result = style_proposals.main(["reopen", "--repo", str(self.repo), "--issue", str(issue),
                                           "--parent", "parent", "--dry-run"])
        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue().count("filed again "), 2)
        self.assertEqual({path: path.read_bytes() for path in state.rglob("*") if path.is_file()}, before)
        self.assertEqual({path: path.read_bytes() for path in self.proposals.glob("*.md")}, issues)

    def test_reopen_called_with_dry_run_writes_no_ledger_row_and_no_issue_byte(self) -> None:
        with self.ledger() as ledger:
            issue, _ = self.decision_closed_as_built(ledger)
            old, _, _ = self.closed_old_issue(ledger, "beta")
            before = self.written(ledger)
            with redirect_stdout(io.StringIO()) as output:
                for target in (issue, old):
                    style_proposals.reopen(ledger, target, "parent", dry_run=True)
            self.assertEqual(output.getvalue().count("filed again "), 3)
            self.assertEqual(self.written(ledger), before)

    def test_reopen_refuses_an_old_issue_it_cannot_move_before_any_write(self) -> None:
        with self.ledger() as ledger:
            old, _, missing = self.closed_old_issue(ledger)
            _ = old.write_text(re.sub(r"style_findings: \[[^\]]*\]", f"style_findings: [{missing.finding_id}, unknown]",
                                      old.read_text()))
            before = self.written(ledger)
            with self.assertRaisesRegex(style_ledger.LedgerError, re.escape(f"{old}: unknown findings")):
                style_proposals.reopen(ledger, old, "parent")
            self.assertEqual(self.written(ledger), before)

    def test_reopen_moves_only_what_it_files_again_past_the_decision_of_a_finding_migrate_moved(self) -> None:
        with self.ledger() as ledger:
            applied = self.hold(ledger)
            missing = self.hold(ledger, line=2, before="Kind::Size", after="Kind::Length")
            other = self.hold(ledger, crate="beta", line=3, before="Kind::Rate", after="Kind::Speed")
            old = self.legacy_issue(ledger, "old", applied, missing, other)
            self.built_before_the_check(ledger, applied, missing)
            with redirect_stdout(io.StringIO()):
                style_proposals.migrate(ledger, self.proposals, "parent")
            moved_to, = (path for path in self.proposals.glob("style *.md"))
            self.assertEqual([item.finding_id for item in ledger.proposal_findings(moved_to)], [other.finding_id])
            ledger.ticket_add("names-unit-1", "names-unit", "proposal", "names", "beta", [other.path], [self.rename],
                              proposal=str(moved_to), finding_ids=(other.finding_id,))
            with redirect_stdout(io.StringIO()) as output:
                style_proposals.reopen(ledger, old, "parent", dry_run=True)
            self.assertEqual(output.getvalue().count(f"filed again {missing.finding_id} in "), 1)
            ledger.ticket_drop("names-unit-1")
            style_proposals.decline(ledger, moved_to, "not now")
            output = self.reopen_output(ledger, old)
            decision, = (path for path in self.proposals.glob("style *.md") if path != moved_to)
            self.assertEqual(output, f"filed again {missing.finding_id} in {decision}: Kind::Size -> Kind::Length "
                             + f"not applied in {missing.path}\n")
            self.assertEqual([(item.finding_id, item.state) for item in ledger.proposal_findings(decision)],
                             [(missing.finding_id, style_ledger.Filed(decision))])
            self.assertIsInstance(ledger.proposal_findings(moved_to)[0].state, style_ledger.Waived)

    def test_reopen_opens_a_built_decision_before_it_moves_findings_into_it(self) -> None:
        with self.ledger() as ledger:
            decision, _ = self.decision_closed_as_built(ledger)
            applied = self.hold(ledger, line=4)
            missing = self.hold(ledger, line=5, before="Kind::Rate", after="Kind::Speed")
            old = self.legacy_issue(ledger, "old", applied, missing)
            self.built_before_the_check(ledger, applied, missing)
            self.issue_command("close", str(old), "--reason", "built in names-unit-1")

            def stop_at_reopen(*arguments: str) -> None:
                if arguments[0] == "reopen":
                    raise style_ledger.LedgerError("stopped")
                self.issue_command(*arguments)

            with mock.patch.object(style_proposals, "proposal_command", side_effect=stop_at_reopen), \
                    self.assertRaisesRegex(style_ledger.LedgerError, "stopped"):
                style_proposals.reopen(ledger, old, "parent")
            self.assertEqual([(item.finding_id, type(item.state)) for item in ledger.proposal_findings(old)],
                             [(applied.finding_id, style_ledger.Built), (missing.finding_id, style_ledger.Built)])
            self.assertEqual(frontmatter(decision)["status"], ["closed"])
            _ = self.reopen_output(ledger, old)
            self.assertEqual(ledger.proposal_findings(decision)[-1].finding_id, missing.finding_id)
            self.assertEqual(ledger.proposal_findings(decision)[-1].state, style_ledger.Filed(decision))
            self.assertEqual(frontmatter(decision)["status"], ["open"])

    def test_reopen_refuses_until_a_sync_takes_in_a_merged_ticket(self) -> None:
        with self.ledger() as ledger:
            missing = self.hold(ledger, before="Kind::Size", after="Kind::Length")
            issue = self.approved_decision(ledger, missing)
            synced = self.tip
            self.write("alpha/src/lib.rs", "pub enum Kind { Count, Length }\n")
            _ = self.git("commit", "-qam", "Rename Size to Length")
            self.tip = self.git("rev-parse", "HEAD")
            _ = self.git("update-ref", "refs/heads/style/main", self.tip)
            self.assertEqual(self.merge_ticket(ledger, "names-unit-1", issue, missing), ())
            with redirect_stdout(io.StringIO()):
                style_proposals.complete(ledger)
            before = self.written(ledger)
            with self.assertRaisesRegex(style_ledger.LedgerError, re.escape(
                    f"{issue}: merged ticket names-unit-1 is not in synced commit {synced[:7]}; sync first")):
                style_proposals.reopen(ledger, issue, "parent")
            self.assertEqual(self.written(ledger), before)
            ledger.set_tip(self.tip, "style/main")
            self.assertEqual(self.reopen_output(ledger, issue), "nothing to reopen: every built finding is applied\n")

    def test_migrate_prints_the_notices_of_a_new_decision_between_its_create_and_move_lines(self) -> None:
        with self.ledger() as ledger:
            old = self.legacy_issue(ledger, "old", self.hold(ledger))
            with redirect_stdout(io.StringIO()) as output:
                style_proposals.migrate(ledger, self.proposals, "parent")
            decision, = (path for path in self.proposals.glob("style *.md"))
            self.assertEqual(output.getvalue().splitlines(), [
                f"create {decision}", "no issue base for sample: run /new_issue_base sample in hanadocs",
                "goal: no ranked sample issue; default 1 - Ship Hana", f"move 1 findings to {decision}",
                f"close {old}: merged into {decision.name}", "totals: create 1, move 1, close 1, off-rule 0"])

    def test_reopen_refuses_an_old_issue_whose_decision_has_no_answer_or_is_declined(self) -> None:
        with self.ledger() as ledger:
            _ = self.hold(ledger, line=3, before="Kind::Rate", after="Kind::Speed")
            decision, = self.file(ledger)
            old, _, _ = self.closed_old_issue(ledger)
            for answer in ("has no answer", "is declined"):
                if answer == "is declined":
                    style_proposals.decline(ledger, decision, "not now")
                before = self.written(ledger)
                with self.subTest(answer=answer), self.assertRaisesRegex(
                        style_ledger.LedgerError, re.escape(f"{old}: decision {decision} {answer}")):
                    style_proposals.reopen(ledger, old, "parent")
                self.assertEqual(self.written(ledger), before)

    def test_reopen_refuses_while_a_ticket_builds_the_issue(self) -> None:
        with self.ledger() as ledger:
            finding = self.hold(ledger, before="Kind::Size", after="Kind::Length")
            issue = self.approved_decision(ledger, finding)
            ledger.ticket_add("names-unit-1", "names-unit", "proposal", "names", "alpha", [finding.path], [self.rename],
                              proposal=str(issue), finding_ids=(finding.finding_id,))
            with self.assertRaisesRegex(style_ledger.LedgerError, "open ticket names-unit-1 builds it"):
                style_proposals.reopen(ledger, issue, "parent")
            self.assertEqual(ledger.proposal_findings(issue)[0].build_ticket, style_ledger.BuildTicket("names-unit-1"))

    def test_reopen_prints_the_nothing_line_when_every_built_rename_is_applied(self) -> None:
        with self.ledger() as ledger:
            finding = self.hold(ledger)
            issue = self.approved_decision(ledger, finding)
            self.assertEqual(self.merge_ticket(ledger, "names-unit-1", issue, finding), ())
            with redirect_stdout(io.StringIO()):
                style_proposals.complete(ledger)
            self.assertEqual(self.reopen_output(ledger, issue), "nothing to reopen: every built finding is applied\n")
            self.assertEqual(frontmatter(issue)["status"], ["closed"])

    def built_decision(self, ledger: style_ledger.Ledger,
                       crate: str) -> tuple[Path, style_ledger.HeldFinding, style_ledger.HeldFinding]:
        """An approved decision in crate whose 2 renames a merge marked built before the check; only the first is in the code."""
        applied = self.hold(ledger, crate=crate)
        missing = self.hold(ledger, crate=crate, line=2, before="Kind::Size", after="Kind::Length")
        issue = self.approved_decision(ledger, applied, missing)
        self.built_before_the_check(ledger, applied, missing)
        return issue, applied, missing

    def reopen_all(self, *arguments: str) -> tuple[int, list[str], str]:
        """The exit code, stdout lines and stderr of reopen --all."""
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            result = style_proposals.main(["reopen", "--repo", str(self.repo), "--all", *arguments])
        return result, output.getvalue().splitlines(), errors.getvalue()

    def filed_again_line(self, issue: Path, finding: style_ledger.HeldFinding) -> str:
        return f"filed again {finding.finding_id} in {issue}: Kind::Size -> Kind::Length not applied in {finding.path}"

    def test_reopen_all_files_again_the_unapplied_renames_of_every_approved_decision(self) -> None:
        with self.ledger() as ledger:
            # Decisions run in path order, and the alpha decision's name sorts first.
            decisions = (self.built_decision(ledger, "alpha"), self.built_decision(ledger, "beta"))
        self.assertEqual(self.reopen_all(), (0, [*(self.filed_again_line(issue, missing) for issue, _, missing in decisions),
                                                 "totals: reopen 2, filed again 2, skipped 0"], ""))
        with self.ledger() as ledger:
            for issue, applied, missing in decisions:
                self.assertEqual({finding.finding_id: finding.state for finding in ledger.proposal_findings(issue)},
                                 {applied.finding_id: style_ledger.Built(issue), missing.finding_id: style_ledger.Filed(issue)})

    def test_reopen_all_skips_a_refused_decision_and_reopens_the_others(self) -> None:
        with self.ledger() as ledger:
            reopened, _, missing = self.built_decision(ledger, "alpha")
            refused, _, kept = self.built_decision(ledger, "beta")
            later = self.hold(ledger, crate="beta", line=3, before="Kind::Rate", after="Kind::Speed")
            self.assertEqual(self.file(ledger), (refused,))
            ledger.ticket_add("names-unit-1", "names-unit", "proposal", "names", "beta", [later.path], [self.rename],
                              proposal=str(refused), finding_ids=(later.finding_id,))
        # Decisions run in path order, and the alpha decision's name sorts first.
        self.assertEqual(self.reopen_all(), (1, [self.filed_again_line(reopened, missing),
                                                 f"skipped {refused}: open ticket names-unit-1 builds it",
                                                 "totals: reopen 1, filed again 1, skipped 1"],
                                             "style_proposals: skipped 1 decisions\n"))
        with self.ledger() as ledger:
            states = {finding.finding_id: finding.state for finding in ledger.held_findings(style_ledger.AllHeldFindings())}
        self.assertEqual((states[missing.finding_id], states[kept.finding_id]),
                         (style_ledger.Filed(reopened), style_ledger.Built(refused)))

    def test_reopen_all_dry_run_prints_the_lines_of_the_run_and_writes_nothing(self) -> None:
        with self.ledger() as ledger:
            for crate in ("alpha", "beta"):
                _ = self.built_decision(ledger, crate)
            before = self.written(ledger)
        preview = self.reopen_all("--dry-run")
        with self.ledger() as ledger:
            self.assertEqual(self.written(ledger), before)
        self.assertEqual(self.reopen_all(), preview)

    def test_sites_and_candidates_list_the_ledger_records_without_filing_candidates(self) -> None:
        with self.ledger() as ledger:
            finding = self.hold(ledger)
            issue = self.file(ledger)[0]
            ticket = self.assign_ticket(ledger, "candidate")
            ledger.record_candidate(ticket, "alpha/src/lib.rs", 2, "Consider a new rule")
            ledger.record_candidate(ticket, "beta/src/lib.rs", 3, "Consider a new rule")
            with redirect_stdout(io.StringIO()) as output:
                style_proposals.sites(ledger, issue)
                style_proposals.candidates(ledger)
            self.assertIn(f"{finding.finding_id} filed alpha/src/lib.rs:1", output.getvalue())
            self.assertIn("Before: Kind::Value\nAfter: Kind::Count", output.getvalue())
            self.assertEqual(output.getvalue().count("Consider a new rule"), 1)
            self.assertIn("beta/src/lib.rs:3 — candidate-1 (candidate)", output.getvalue())
            self.assertEqual(len(tuple(self.proposals.glob("*.md"))), 1)

    def test_drop_candidate_removes_each_candidate_with_the_idea_and_refuses_an_unknown_idea(self) -> None:
        with self.ledger() as ledger:
            ticket = self.assign_ticket(ledger, "candidate")
            for path, line, idea in (("alpha/src/lib.rs", 2, "Consider a new rule"), ("beta/src/lib.rs", 3, "Consider a new rule"),
                                     ("beta/src/lib.rs", 4, "Keep this idea")):
                ledger.record_candidate(ticket, path, line, idea)
        arguments = ["drop-candidate", "--repo", str(self.repo), "--idea", "Consider a new rule"]
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(style_proposals.main(arguments), 0)
            self.assertEqual(style_proposals.main(arguments), 1)
        self.assertEqual(output.getvalue(), "dropped 2 candidates\n")
        self.assertIn("no rule candidate has the idea 'Consider a new rule'", errors.getvalue())
        with self.ledger() as ledger:
            self.assertEqual([item.idea for item in ledger.rule_candidates()], ["Keep this idea"])

    def test_one_bad_decision_is_skipped_while_other_decisions_file(self) -> None:
        with self.ledger() as ledger:
            _ = self.hold(ledger)
            alpha = self.file(ledger)[0]
            _ = alpha.write_text(alpha.read_text().replace(proposal_issue.GENERATED_START, ""))
            late, beta = self.hold(ledger, line=2), self.hold(ledger, crate="beta")
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = style_proposals.main(["file", "--repo", str(self.repo), "--proposals", str(self.proposals), "--parent", "parent"])
        self.assertEqual(code, 1)
        self.assertEqual(errors.getvalue(), "style_proposals: skipped 1 decisions\n")
        self.assertEqual([line for line in output.getvalue().splitlines() if line.startswith("skipped")],
                         [f"skipped {alpha}: missing generated decision body"])
        with self.ledger() as ledger:
            self.assertEqual([item.finding_id for item in ledger.held_findings(style_ledger.HeldFindingsInState("unfiled"))],
                             [late.finding_id])
            filed = ledger.held_findings(style_ledger.HeldFindingsInState("filed"))
            self.assertIn(beta.finding_id, {item.finding_id for item in filed})
            self.assertNotEqual(next(item for item in filed if item.finding_id == beta.finding_id).state, style_ledger.Filed(alpha))

    def test_crates_whose_safe_names_match_and_a_crate_named_root_get_separate_decisions(self) -> None:
        crates = ("a/util", "a-util", "a+util", "b/util", "crates/foo", "root")
        self.write("Cargo.toml", '[workspace]\nmembers = ["alpha", "beta", ' + ", ".join(f'"{crate}"' for crate in crates) + ']\n'
                   + '[package]\nname = "root-package"\nversion = "0.1.0"\n')
        for crate, package in (*((crate, re.sub(r"[/+]", "-", crate) + "-package") for crate in crates), (".", "root-package")):
            if crate != ".":
                self.write(f"{crate}/Cargo.toml", f'[package]\nname = "{package}"\nversion = "0.1.0"\n')
            self.write(f"{crate}/src/lib.rs", "fn value() {}\n")
        _ = self.git("add", "--all")
        _ = self.git("commit", "-qm", "More crates")
        self.tip = self.git("rev-parse", "HEAD")
        with self.ledger() as ledger:
            ledger.set_tip(self.tip, "style/main")
            findings = {crate: self.hold(ledger, crate=crate) for crate in (*crates, ".")}
            paths = self.file(ledger)
            self.assertEqual(len(set(paths)), 7)
            blob = findings["."].rule_blob[:8]
            names = {frontmatter(path)["style_crate"][0]: path.name for path in paths}
            # A plain crate path shows "/" as "+" with no hash; a raw "+" adds a hash of its path.
            self.assertEqual({crate: names[crate] for crate in (*crates, ".") if crate != "a+util"},
                             {"a/util": f"style names a+util {blob}.md", "a-util": f"style names a-util {blob}.md",
                              "b/util": f"style names b+util {blob}.md", "crates/foo": f"style names crates+foo {blob}.md",
                              "root": f"style names root {blob}.md", ".": f"style names {blob}.md"})
            self.assertRegex(names["a+util"], rf"^style names a\+util \([0-9a-f]{{8}}\) {blob}\.md$")
            for crate, finding in findings.items():
                decision = next(path for path in paths if frontmatter(path)["style_crate"] == [crate])
                self.assertEqual([item.finding_id for item in ledger.proposal_findings(decision)], [finding.finding_id])
                if crate != ".":
                    self.assertIn(f"# Style decision: names in {crate}\n", decision.read_text())

    def test_an_unanswered_ranking_follows_the_count_and_an_approved_one_stays(self) -> None:
        def ranking(issue: Path) -> str:
            return next(line for line in issue.read_text().splitlines() if line.startswith("Proposed ranking:"))
        with self.ledger() as ledger:
            _ = self.hold(ledger)
            issue = self.file(ledger)[0]
            self.assertTrue(ranking(issue).endswith("effort ⭐"))
            for line in range(2, 5):
                _ = self.hold(ledger, line=line)
            _ = self.file(ledger)
            self.assertTrue(ranking(issue).endswith("effort ⭐⭐"))
            self.issue_command("approve", str(issue), "--showrunner", "parent", "--unit", "names-unit", "--plan", "unit.md")
            approved = ranking(issue)
            for line in range(5, 17):
                _ = self.hold(ledger, line=line)
            _ = self.file(ledger)
            self.assertEqual(frontmatter(issue)["style_count"], ["16"])
            self.assertEqual(ranking(issue), approved)

    def test_a_created_decision_is_written_once_with_live_holders(self) -> None:
        standing = add_style.StandingStyleProduction(self.repo, "sample-style", self.root / "sample-style",
                                                      self.root / "sample-style/docs/production.md", "main")
        with self.ledger() as ledger:
            first = self.hold(ledger)
            legacy = self.legacy_issue(ledger, "old", self.hold(ledger, crate="beta"))
            holders = {"product-unit": frozenset({first.path, "beta/src/lib.rs"})}
            written: list[Path] = []
            original = proposal_issue._write  # pyright: ignore[reportPrivateUsage]
            def record(path: Path, text: str) -> None:
                written.append(path)
                original(path, text)
            with (mock.patch.object(style_proposals, "standing_style_production", return_value=standing),
                  mock.patch.object(style_proposals, "unit_file_holders", return_value=holders),
                  mock.patch.object(proposal_issue, "_write", side_effect=record)):
                with redirect_stdout(io.StringIO()):
                    style_proposals.migrate(ledger, self.proposals, "parent")
                created = self.file(ledger)
                decisions = tuple(sorted(self.proposals.glob("style *.md")))
                self.assertEqual(len(decisions), 2)
                self.assertEqual([path for path in written if path in decisions], [])
                self.assertTrue(all("Live units holding file: product-unit" in path.read_text() for path in decisions))
                stamps = {path: path.stat().st_mtime_ns for path in decisions}
                self.assertEqual(self.file(ledger), ())
                self.assertEqual([path for path in written if path in decisions], [])
                self.assertEqual({path: path.stat().st_mtime_ns for path in decisions}, stamps)
            self.assertEqual(len(created), 1)
            self.assertEqual(frontmatter(legacy)["status"], ["closed"])

    def test_migration_copies_only_agreed_fields_and_preserves_other_proposals(self) -> None:
        with self.ledger() as ledger:
            first, second = self.hold(ledger), self.hold(ledger, line=2)
            left = self.legacy_issue(ledger, "left", first)
            right = self.legacy_issue(ledger, "right", second)
            _ = proposal_issue.edit(left, {"showrunner": "parent", "backlog_impact": "⭐⭐"}, date(2026, 10, 19))
            _ = proposal_issue.edit(right, {"showrunner": "parent", "backlog_impact": "⭐"}, date(2026, 10, 19))
            other = self.proposals / "not-style.md"
            _ = other.write_text(left.read_text().replace("proposal, style", "proposal"))
            original = other.read_bytes()
            with redirect_stdout(io.StringIO()):
                style_proposals.migrate(ledger, self.proposals, "parent")
            decision = next(path for path in self.proposals.glob("*.md") if "style_rule" in frontmatter(path))
            self.assertEqual(frontmatter(decision)["showrunner"], ["parent"])
            self.assertEqual(frontmatter(decision)["backlog_impact"], [])
            self.assertEqual(other.read_bytes(), original)

    def test_goal_counts_only_open_non_style_project_issues_and_breaks_ties(self) -> None:
        self.ranked_issue("one", "2 - Find Collaborators")
        for name, options in (("style", {"tags": "issue, style"}), ("closed", {"status": "closed"}),
                              ("other", {"project": "other"})):
            self.ranked_issue(name, "1 - Ship Hana", **options)
        self.ranked_issue("invalid", "not a goal")
        self.assertEqual(style_proposals.project_goal(self.proposals, "sample"), "2 - Find Collaborators")
        self.ranked_issue("tie", "1 - Ship Hana")
        self.assertEqual(style_proposals.project_goal(self.proposals, "sample"), "1 - Ship Hana")

    def test_goal_default_names_project_once_for_multiple_decisions(self) -> None:
        output = io.StringIO()
        with self.ledger() as ledger:
            _ = self.hold(ledger)
            _ = self.hold(ledger, rule=self.structure, kind="type", line=2)
            with redirect_stdout(output):
                paths = style_proposals.file_proposals(ledger, self.proposals, "parent").filed
        self.assertEqual(len(paths), 2)
        self.assertEqual(output.getvalue().count("goal: no ranked sample issue; default 1 - Ship Hana\n"), 1)

    def test_file_and_rank_use_project_goal_two(self) -> None:
        self.ranked_issue("ranked", "2 - Find Collaborators")
        with self.ledger() as ledger:
            _ = self.hold(ledger)
            issue = self.file(ledger)[0]
            expected = issue.read_bytes()
            self.assertIn(b"Proposed ranking: goal 2 - Find Collaborators", expected)
            _ = issue.write_bytes(re.sub(rb"Proposed ranking: [^\n]*", b"Proposed ranking: ", expected))
            with redirect_stdout(io.StringIO()):
                style_proposals.rank_proposals(ledger, self.proposals)
            self.assertEqual(issue.read_bytes(), expected)

    def test_rule_urls_use_both_github_remotes_and_cache_each_root(self) -> None:
        _ = self.git("update-ref", "refs/remotes/origin/release", self.tip)
        _ = self.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/release")
        linker = style_proposals.RuleLinker(self.repo)
        with mock.patch.object(style_proposals, "github_rule_root", wraps=style_proposals.github_rule_root) as roots:
            self.assertEqual(linker.rule_url("local:docs/style/names.md#uncovered"),
                             "https://github.com/example/sample/blob/release/docs/style/names.md")
            self.assertEqual(linker.rule_url("global:rust/names.md#uncovered"),
                             "https://github.com/example/nate_style/blob/main/rust/names.md")
            self.assertEqual(linker.rule_url("global:rust/modules.md"),
                             "https://github.com/example/nate_style/blob/main/rust/modules.md")
            self.assertEqual(roots.call_count, 2)

    def test_rule_urls_report_main_fallback_and_refuse_non_github_remote(self) -> None:
        _ = self.git("symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
        output = io.StringIO()
        linker = style_proposals.RuleLinker(self.repo)
        with redirect_stdout(output):
            url = linker.rule_url("local:docs/style/names.md")
            _ = linker.rule_url("local:docs/style/other.md")
        self.assertEqual(url, "https://github.com/example/sample/blob/main/docs/style/names.md")
        self.assertEqual(output.getvalue(), f"rules: {self.repo}: no origin/HEAD; default main\n")
        _ = self.git("remote", "set-url", "origin", "https://gitlab.com/example/sample.git")
        with self.assertRaisesRegex(style_ledger.LedgerError, "non-GitHub origin https://gitlab.com/example/sample.git"):
            _ = style_proposals.RuleLinker(self.repo).rule_url("local:docs/style/names.md")

    def test_file_writes_sorted_distinct_rule_links_and_keeps_body_rule_lines(self) -> None:
        local_rule = "local:docs/style/modules.md"
        self.write("docs/style/modules.md", "---\ntier: D\nfamily: modules\ninput_scope: crate\n---\n# Modules\n")
        _ = (self.catalog / "propose.md").write_text(
            "---\ntier: B\nfamily: names\ninput_scope: file\nmode: propose\ncandidates:\n"
            + "  kind: regex\n  pattern: value\n  surface: partial\n  uncovered: other names\n---\n# Rule\n")
        with self.ledger() as ledger:
            first = self.hold(ledger, rule=self.propose)
            _ = self.hold(ledger, rule=local_rule, line=2)
            _ = ledger.hold(self.propose + "#uncovered", first.path, 3, "uncovered site", "rename",
                            "before", "after", first.rule_blob, "outside")
            _ = self.file(ledger)
        decisions = tuple(self.proposals.glob("*.md"))
        self.assertEqual(len(decisions), 2)
        links = {url for path in decisions for url in frontmatter(path)["style_rules"]}
        self.assertEqual(links, {"https://github.com/example/nate_style/blob/main/rust/propose.md",
                                 "https://github.com/example/sample/blob/main/docs/style/modules.md"})
        with self.ledger() as ledger:
            self.assertEqual(sum(len(ledger.proposal_findings(path)) for path in decisions), 3)
        self.assertTrue(any(f"Rule: {local_rule}\n" in path.read_text() for path in decisions))
        self.assertTrue(any(f"Rule: {self.propose}\n" in path.read_text() for path in decisions))

    def test_link_rules_backfills_open_and_closed_issues_and_is_idempotent(self) -> None:
        with self.ledger() as ledger:
            _ = self.hold(ledger)
            opened = self.file(ledger)[0]
            _ = self.hold(ledger, rule=self.structure, kind="type", line=2)
            closed = self.file(ledger)[0]
            _ = self.hold(ledger, crate="beta")
            linked = self.file(ledger)[0]
        for issue in (opened, closed):
            text = re.sub(r"^style_rules:.*\n", "", issue.read_text(), flags=re.MULTILINE)
            if issue == closed:
                text = text.replace("status: open", "status: closed")
            _ = issue.write_text(text)
        unknown = self.proposals / "unknown.md"
        _ = unknown.write_text("---\nstatus: open\ntags: [style]\nstyle_findings: [missing]\n---\n# Keep\n")
        untagged = self.proposals / "untagged.md"
        _ = untagged.write_text(unknown.read_text().replace("tags: [style]", "tags: [issue]"))
        untouched = {path: path.read_bytes() for path in (linked, unknown, untagged)}
        args = ["link-rules", "--repo", str(self.repo), "--proposals", str(self.proposals)]
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(style_proposals.main(args), 0)
        self.assertNotIn(str(unknown), output.getvalue())
        self.assertTrue(output.getvalue().endswith("linked 2, already linked 1, skipped 0\n"))
        for issue in (opened, closed):
            expected_rule = "modules" if issue == closed else "names"
            self.assertEqual(frontmatter(issue)["style_rules"],
                             [f"https://github.com/example/nate_style/blob/main/rust/{expected_rule}.md"])
        self.assertEqual(frontmatter(closed)["status"], ["closed"])
        self.assertEqual({path: path.read_bytes() for path in untouched}, untouched)
        stamps = {path: path.stat().st_mtime_ns for path in self.proposals.glob("*.md")}
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(style_proposals.main(args), 0)
        self.assertTrue(output.getvalue().endswith("linked 0, already linked 3, skipped 0\n"))
        self.assertEqual({path: path.stat().st_mtime_ns for path in stamps}, stamps)

    def test_proposed_ranking_sets_all_values_for_each_kind_and_effort_boundary(self) -> None:
        for kind, impact in (("rename", "⭐"), ("type", "⭐⭐"), ("restructure", "⭐⭐")):
            for count, effort in ((1, "⭐"), (3, "⭐"), (4, "⭐⭐"), (15, "⭐⭐"), (16, "⭐⭐⭐")):
                with self.subTest(kind=kind, findings=count):
                    self.assertEqual(style_proposals.proposed_ranking(cast(style_ledger.FindingKind, kind), count, "2 - Find Collaborators"),
                                     f"goal 2 - Find Collaborators · alignment ⭐⭐ · impact {impact} · urgency ⭐ · effort {effort}")

    def test_root_crate_restructure_uses_repository_name_in_title_and_filename_with_ranking(self) -> None:
        with self.ledger() as ledger:
            self.hold_root_structure(ledger, 4)
            issue = self.file(ledger)[0]
        self.assertRegex(issue.name, r"^style modules [0-9a-f]{8}\.md$")
        self.assertIn("# Style decision: modules in sample\n", issue.read_text())
        self.assertIn("Proposed ranking: goal 1 - Ship Hana · alignment ⭐⭐ · impact ⭐⭐ · urgency ⭐ · effort ⭐⭐\n",
                      issue.read_text())
        self.assertIn("--proposed-ranking", self.calls[0])

    def test_root_crate_rename_keeps_rule_stem_in_title_and_filename(self) -> None:
        self.write("Cargo.toml", (self.repo / "Cargo.toml").read_text()
                   + '[package]\nname = "root-package"\nversion = "0.1.0"\n')
        self.write("src/lib.rs", "fn value() {}\n")
        _ = self.git("add", "--all")
        _ = self.git("commit", "-qm", "Root package")
        with self.ledger() as ledger:
            ledger.set_tip(self.git("rev-parse", "HEAD"), "style/main")
            blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == self.rename)
            _ = ledger.hold(self.rename, "src/lib.rs", 1, "root name", "rename",
                            "value", "count", blob, "outside")
            issue = self.file(ledger)[0]
        self.assertRegex(issue.name, r"^style names [0-9a-f]{8}\.md$")
        self.assertIn("# Style decision: names in sample\n", issue.read_text())

    def test_rank_fills_only_empty_ranking_preserving_other_bytes_and_is_idempotent(self) -> None:
        with self.ledger() as ledger:
            self.hold_root_structure(ledger, 4)
            root_issue = self.file(ledger)[0]
            _ = self.hold(ledger)
            filled = self.file(ledger)[0]
            _ = self.hold(ledger, crate="beta")
            closed = self.file(ledger)[0]
            _ = self.hold(ledger, rule=self.propose, line=5, kind="type")
            propose_issue = self.file(ledger)[0]
        # CRLF, frontmatter dates, trailing whitespace and file names must all survive.
        expected = root_issue.read_text().replace("\n", "\r\n").encode()
        ranking = b"Proposed ranking: " + "goal 1 - Ship Hana · alignment ⭐⭐ · impact ⭐⭐ · urgency ⭐ · effort ⭐⭐".encode()
        _ = root_issue.write_bytes(expected.replace(ranking, b"Proposed ranking: "))
        filled_text = filled.read_text()
        _ = filled.write_text(re.sub(r"Proposed ranking: .*", "Proposed ranking: confirmed by user", filled_text))
        _ = closed.write_text(re.sub(r"Proposed ranking: .*", "Proposed ranking: ", closed.read_text())
                              .replace("status: open\n", "status: closed\n"))
        propose_expected = propose_issue.read_bytes()
        _ = propose_issue.write_bytes(re.sub(rb"Proposed ranking: [^\n]*", b"Proposed ranking: ", propose_expected))
        untagged = self.proposals / "not-style.md"
        _ = untagged.write_bytes(root_issue.read_bytes().replace(b'"style"', b'"other"'))
        unknown = self.proposals / "unknown.md"
        _ = unknown.write_text('---\nstatus: open\ntags: [style]\nstyle_findings: [missing]\n---\nProposed ranking: \n')
        untouched = {path: path.read_bytes() for path in (filled, closed, untagged, unknown)}
        names = sorted(path.name for path in self.proposals.iterdir())
        output, errors = io.StringIO(), io.StringIO()
        arguments = ["rank", "--repo", str(self.repo), "--proposals", str(self.proposals)]
        with redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(style_proposals.main(arguments), 0)
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(root_issue.read_bytes(), expected)
        self.assertEqual(propose_issue.read_bytes(), propose_expected)
        self.assertEqual({path: path.read_bytes() for path in untouched}, untouched)
        self.assertEqual(sorted(path.name for path in self.proposals.iterdir()), names)
        self.assertIn(f"ranked {root_issue}\n", output.getvalue())
        self.assertIn(f"ranked {propose_issue}\n", output.getvalue())
        self.assertNotIn(str(unknown), output.getvalue())
        self.assertTrue(output.getvalue().endswith("ranked 2 issues\n"))
        stamps = {path: path.stat().st_mtime_ns for path in self.proposals.iterdir()}
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(style_proposals.main(arguments), 0)
        self.assertTrue(output.getvalue().endswith("ranked 0 issues\n"))
        self.assertEqual({path: path.stat().st_mtime_ns for path in stamps}, stamps)

    def test_assigned_ticket_findings_wait_while_closed_ticket_findings_are_filed(self) -> None:
        with self.ledger() as ledger:
            assigned = self.assign_ticket(ledger, "assigned")
            waiting = (self.hold(ledger, ticket=assigned), self.hold(ledger, ticket=assigned, line=2))
            merged = self.assign_ticket(ledger, "merged")
            ready = self.hold(ledger, ticket=merged, line=3)
            ledger.ticket_checkpoint(merged, self.tip)
            _ = ledger.ticket_merge(merged)
            dropped = self.assign_ticket(ledger, "dropped")
            dropped_finding = self.hold(ledger, ticket=dropped, line=4)
            ledger.ticket_drop(dropped)
            output = io.StringIO()
            with redirect_stdout(output):
                paths = style_proposals.file_proposals(ledger, self.proposals, "parent").filed
            self.assertEqual(len(paths), 1)
            self.assertEqual({finding.finding_id for finding in ledger.proposal_findings(paths[0])}, {ready.finding_id, dropped_finding.finding_id})
            self.assertEqual({item.finding_id for item in ledger.held_findings(style_ledger.HeldFindingsInState("unfiled"))},
                             {item.finding_id for item in waiting})
            self.assertEqual([line for line in output.getvalue().splitlines() if line.startswith("held for open ticket")],
                             [f"held for open ticket {assigned}: 2 findings"])

    def test_filing_retries_held_findings_after_the_holding_ticket_merges(self) -> None:
        with self.ledger() as ledger:
            ticket = self.assign_ticket(ledger, "names")
            finding = self.hold(ledger, ticket=ticket)
            self.assertEqual(self.file(ledger), ())
            self.assertEqual(self.calls, [])
            ledger.ticket_checkpoint(ticket, self.tip)
            _ = ledger.ticket_merge(ticket)
            paths = self.file(ledger)
            self.assertEqual(len(paths), 1)
            self.assertEqual([item.finding_id for item in ledger.proposal_findings(paths[0])], [finding.finding_id])
            self.assertIsInstance(ledger.held_findings(style_ledger.AllHeldFindings())[0].state, style_ledger.Filed)

    def test_checkpointed_ticket_findings_remain_unfiled_with_a_wait_notice(self) -> None:
        with self.ledger() as ledger:
            ticket = self.assign_ticket(ledger, "names")
            finding = self.hold(ledger, ticket=ticket)
            ledger.ticket_checkpoint(ticket, self.tip)
            output = io.StringIO()
            with redirect_stdout(output):
                paths = style_proposals.file_proposals(ledger, self.proposals, "parent").filed
            self.assertEqual(paths, ())
            self.assertEqual(output.getvalue(), "totals: withdraw 0, kept 0, close 0\n" + f"held for open ticket {ticket}: 1 findings\n")
            self.assertEqual(self.calls, [])
            self.assertEqual(ledger.held_findings(style_ledger.HeldFindingsInState("unfiled")), (finding,))

    def test_decisions_group_by_rule_and_crate_with_ledger_sites_and_issue_fields(self) -> None:
        with self.ledger() as ledger:
            first = self.hold(ledger)
            second = self.hold(ledger, line=2)
            _ = self.hold(ledger, kind="type", line=3)
            _ = self.hold(ledger, crate="beta")
            _ = self.hold(ledger, rule=self.structure)
            _ = self.hold(ledger, rule=self.propose, line=4)
            marker = self.proposals.parent / "home-base/issues - alpha.base"
            marker.parent.mkdir(parents=True)
            _ = marker.write_text('filters:\n  and:\n    - project.contains(link("alpha"))\n')
            paths = self.file(ledger)
            self.assertEqual(len(paths), 4)
            findings = ledger.held_findings(style_ledger.HeldFindingsInState("filed"))
            self.assertEqual(len(findings), 6)
            shared = next(path for path in paths if {first.finding_id, second.finding_id} <= {finding.finding_id for finding in ledger.proposal_findings(path)})
            body = shared.read_text()
            self.assertIn("alpha/src/lib.rs:1", body)
            self.assertEqual(len(ledger.proposal_findings(shared)), 3)
            self.assertIn("Before:\n```\nKind::Value", body)
            self.assertIn("After:\n```\nKind::Count", body)
            self.assertIn("Live units holding file: none", body)
            self.assertIn("Recommended answer:", body)
            self.assertTrue(body.split("---", 2)[2].lstrip().startswith("Proposed ranking:"))
            fields = frontmatter(shared)
            self.assertEqual(fields["area"], ["unfiled"])
            self.assertEqual(fields["project"], ["alpha"])
            self.assertEqual(fields["proposed_by"], ["sample-style"])
            self.assertEqual(set(fields["tags"]), {"issue", "proposal", "style"})
            self.assertEqual(fields["backlog_goal"], [])
            self.assertTrue(all(path.name.startswith("style ") for path in paths))
            self.assertEqual(len(open_issues(self.proposals, "proposal")), ledger.summary().proposals)
            self.assertEqual(len(proposals_table.issue_rows(open_issues(self.proposals, "proposal"))), ledger.summary().proposals)
            before = {path: path.read_bytes() for path in paths}
            self.assertEqual(self.file(ledger), ())
            self.assertEqual({path: path.read_bytes() for path in paths}, before)
            self.assertEqual(sum(call[0] == "create" for call in self.calls), 4)

    def test_file_reads_workspace_once_for_many_findings_in_multiple_crates(self) -> None:
        with self.ledger() as ledger:
            findings = tuple(self.hold(ledger, crate=crate, line=line)
                             for crate in ("alpha", "beta") for line in range(1, 5))
            with mock.patch.object(coverage_report, "workspace", wraps=coverage_report.workspace) as workspace:
                paths = self.file(ledger)
                workspace.assert_called_once_with(ledger, self.tip)
            self.assertEqual(len(paths), 2)
            for path in paths:
                crate = next(finding.path.split("/")[0] for finding in findings
                             if finding.finding_id == ledger.proposal_findings(path)[0].finding_id)
                self.assertEqual({finding.finding_id for finding in ledger.proposal_findings(path)},
                                 {finding.finding_id for finding in findings if finding.path.startswith(crate + "/")})
                self.assertIn(f"# Style decision: names in {crate}\n", path.read_text())
                self.assertIn("Proposed ranking: goal 1 - Ship Hana · alignment ⭐⭐ · impact ⭐ · urgency ⭐ · effort ⭐⭐\n",
                              path.read_text())

    def test_rank_reads_workspace_once_and_uses_decision_membership(self) -> None:
        with self.ledger() as ledger:
            for crate in ("alpha", "beta"):
                for line in range(1, 5):
                    _ = self.hold(ledger, crate=crate, line=line)
            paths = self.file(ledger)
            self.assertEqual(len(paths), 2)
            expected = {path: path.read_bytes() for path in paths}
            for path in paths:
                _ = path.write_bytes(re.sub(rb"Proposed ranking: [^\n]*", b"Proposed ranking: ", expected[path]))
            output = io.StringIO()
            with (mock.patch.object(coverage_report, "workspace", wraps=coverage_report.workspace) as workspace,
                  mock.patch.object(ledger, "held_findings", wraps=ledger.held_findings) as held_findings,
                  redirect_stdout(output)):
                style_proposals.rank_proposals(ledger, self.proposals)
                workspace.assert_called_once_with(ledger, self.tip)
                held_findings.assert_not_called()
            self.assertEqual({path: path.read_bytes() for path in paths}, expected)
            self.assertTrue(output.getvalue().endswith("ranked 2 issues\n"))

    def test_rank_skips_an_issue_removed_during_the_run_and_ranks_the_rest(self) -> None:
        with self.ledger() as ledger:
            for crate in ("alpha", "beta"):
                _ = self.hold(ledger, crate=crate)
            gone, kept = sorted(self.file(ledger))
            for path in (gone, kept):
                _ = path.write_text(re.sub(r"Proposed ranking: [^\n]*", "Proposed ranking: ", path.read_text()))
            read_findings = style_proposals.issue_findings

            def remove_first(ledger: style_ledger.Ledger, issue: Path) -> tuple[style_ledger.HeldFinding, ...]:
                findings = read_findings(ledger, issue)
                if issue == gone:
                    issue.unlink()
                return findings
            output = io.StringIO()
            with (mock.patch.object(style_proposals, "issue_findings", side_effect=remove_first),
                  redirect_stdout(output)):
                style_proposals.rank_proposals(ledger, self.proposals)
            self.assertIn(f"skipped {gone}: {gone} is not a file\n", output.getvalue())
            self.assertIn(f"ranked {kept}\n", output.getvalue())
            self.assertTrue(output.getvalue().endswith("ranked 1 issues\n"))

    def test_package_project_uses_its_base_shorthand_and_return_link(self) -> None:
        self.write("alpha/Cargo.toml", '[package]\nname = "hana_diegetic"\nversion = "0.1.0"\n')
        bases = self.proposals.parent / "home-base"
        bases.mkdir(parents=True)
        _ = (bases / "issues - diegetic.base").write_text(
            'filters:\n  and:\n    - project.contains(link("hana_diegetic"))\n'
            + '    - project.contains(link("ignored_second_filter"))\n')
        _ = (bases / "issues - fallback.base").write_text('project.contains(link("sample"))\n')
        with self.ledger() as ledger:
            _ = self.hold(ledger)
            issue = self.file(ledger)[0]
        self.assertEqual(frontmatter(issue)["project"], ["hana_diegetic"])
        self.assertIn('"[[issues - diegetic.base]]"', issue.read_text())

    def test_repository_project_base_is_used_when_package_has_no_base(self) -> None:
        bases = self.proposals.parent / "home-base"
        bases.mkdir(parents=True)
        _ = (bases / "issues - repository.base").write_text('project.contains(link("sample"))\n')
        with self.ledger() as ledger:
            _ = self.hold(ledger)
            issue = self.file(ledger)[0]
        self.assertEqual(frontmatter(issue)["project"], ["sample"])
        self.assertIn('"[[issues - repository.base]]"', issue.read_text())

    def test_missing_project_base_files_under_repository_and_prints_setup_command(self) -> None:
        output = io.StringIO()
        with self.ledger() as ledger:
            _ = self.hold(ledger)
            with redirect_stdout(output):
                issue = style_proposals.file_proposals(ledger, self.proposals, "parent").filed[0]
        self.assertEqual(frontmatter(issue)["project"], ["sample"])
        self.assertIn('"[[issues - sample.base]]"', issue.read_text())
        self.assertIn("no issue base for sample: run /new_issue_base sample in hanadocs", output.getvalue().splitlines())

    def test_filed_body_names_live_holders_from_the_shared_lookup(self) -> None:
        standing = add_style.StandingStyleProduction(self.repo, "sample-style", self.root / "sample-style",
                                                      self.root / "sample-style/docs/production.md", "main")
        with self.ledger() as ledger:
            finding = self.hold(ledger)
            with (mock.patch.object(style_proposals, "standing_style_production", return_value=standing),
                  mock.patch.object(style_proposals, "unit_file_holders", return_value={"product-unit": frozenset({finding.path})}) as holders):
                issue = self.file(ledger)[0]
                holders.assert_called_once_with(self.repo, standing.doc, ledger)
        self.assertIn("Live units holding file: product-unit", issue.read_text())

    def test_filing_outside_a_git_checkout_returns_one_error_line(self) -> None:
        repo = self.root / "not-a-checkout"
        repo.mkdir()
        errors = io.StringIO()
        with redirect_stderr(errors):
            code = style_proposals.main(["file", "--repo", str(repo), "--proposals", str(self.proposals), "--parent", "parent"])
        self.assertEqual(code, 1)
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn("not a git repository", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())

    def test_project_lookup_turns_a_git_failure_into_a_ledger_error(self) -> None:
        repo = self.root / "missing-git"
        repo.mkdir()
        with self.assertRaisesRegex(style_ledger.LedgerError, "not a git repository"):
            _ = style_proposals.issue_project(repo, ".", self.proposals.parent)

    def test_complete_retries_renumber_after_the_issue_was_closed(self) -> None:
        with self.ledger() as ledger:
            finding = self.hold(ledger)
            issue = self.file(ledger)[0]
            self.issue_command("approve", str(issue), "--showrunner", "parent", "--unit", "names-unit", "--plan", "unit.md")
            ledger.ticket_add("names-unit-1", "names-unit", "proposal", "names", "alpha", [finding.path], [self.rename], proposal=str(issue), finding_ids=(finding.finding_id,))
            ledger.ticket_assign("names-unit-1", "main")
            ledger.ticket_checkpoint("names-unit-1", self.tip)
            _ = ledger.ticket_merge("names-unit-1")
            with mock.patch.object(style_proposals, "renumber", side_effect=[style_ledger.LedgerError("renumber failed"), None]) as renumber:
                with self.assertRaisesRegex(style_ledger.LedgerError, "renumber failed"):
                    style_proposals.complete(ledger)
                self.assertEqual(frontmatter(issue)["status"], ["closed"])
                calls = len(self.calls)
                style_proposals.complete(ledger)
                self.assertEqual(renumber.call_count, 2)
                self.assertEqual(len(self.calls), calls)

    def test_decline_retries_renumber_after_the_issue_was_closed(self) -> None:
        with self.ledger() as ledger:
            _ = self.hold(ledger)
            issue = self.file(ledger)[0]
            with mock.patch.object(style_proposals, "renumber", side_effect=[style_ledger.LedgerError("renumber failed"), None]) as renumber:
                with self.assertRaisesRegex(style_ledger.LedgerError, "renumber failed"):
                    style_proposals.decline(ledger, issue, "Keep names")
                self.assertEqual(frontmatter(issue)["status"], ["closed"])
                calls = len(self.calls)
                style_proposals.decline(ledger, issue, "Keep names")
                self.assertEqual(renumber.call_count, 2)
                self.assertEqual(len(self.calls), calls)

    def test_create_recovery_marks_the_whole_batch_after_partial_file_failure(self) -> None:
        with self.ledger() as ledger:
            first, second = self.hold(ledger), self.hold(ledger, line=2)
            original = ledger.file_held
            count = 0
            def partial(finding_id: str, proposal: Path) -> None:
                nonlocal count
                count += 1
                if count == 2:
                    raise OSError("interrupted after create")
                original(finding_id, proposal)
            with mock.patch.object(ledger, "file_held", side_effect=partial):
                with self.assertRaisesRegex(OSError, "interrupted"):
                    _ = self.file(ledger)
            self.assertEqual(len(self.file(ledger)), 1)
            findings = ledger.held_findings(style_ledger.HeldFindingsInState("filed"))
            self.assertEqual({finding.finding_id for finding in findings}, {first.finding_id, second.finding_id})
            self.assertEqual(sum(call[0] == "create" for call in self.calls), 1)
            self.assertEqual(len(tuple(self.proposals.glob("*.md"))), 1)

    def test_recovery_after_create_reports_failure_before_any_finding_is_marked(self) -> None:
        with self.ledger() as ledger:
            finding = self.hold(ledger)
            def create_then_interrupt(*args: str) -> None:
                self.issue_command(*args)
                raise OSError("interrupted before file_held")
            with mock.patch.object(style_proposals, "proposal_command", side_effect=create_then_interrupt):
                with self.assertRaisesRegex(OSError, "before file_held"):
                    _ = self.file(ledger)
            self.assertEqual(len(ledger.held_findings(style_ledger.HeldFindingsInState("unfiled"))), 1)
            self.assertEqual(len(self.file(ledger)), 1)
            self.assertEqual([item.finding_id for item in ledger.held_findings(style_ledger.HeldFindingsInState("filed"))], [finding.finding_id])
            self.assertEqual(sum(call[0] == "create" for call in self.calls), 1)

    def test_changed_rule_is_skipped_while_other_batches_file(self) -> None:
        with self.ledger() as ledger:
            old = self.hold(ledger)
            _ = self.hold(ledger, rule=self.propose)
            rule_file = self.catalog / "names.md"
            _ = rule_file.write_text(rule_file.read_text() + "Changed text.\n")
            output = io.StringIO()
            with redirect_stdout(output):
                paths = style_proposals.file_proposals(ledger, self.proposals, "parent").filed
            self.assertIn(f"skipped {old.finding_id}: rule changed since judged", output.getvalue())
            self.assertEqual(len(paths), 1)
            self.assertEqual([finding.finding_id for finding in ledger.held_findings(style_ledger.HeldFindingsInState("unfiled"))], [old.finding_id])
            self.assertEqual(next(cell.state for cell in ledger.cells(self.rename)), "stale")

    def test_decline_waits_for_both_findings_then_waives_the_same_cell_with_words(self) -> None:
        with self.ledger() as ledger:
            first, second = self.hold(ledger), self.hold(ledger, line=2)
            self.assertIsInstance(ledger.decline_findings((first.finding_id,), "Keep names")[first.finding_id], style_ledger.CellStillHeld)
            result = ledger.decline_findings((second.finding_id,), "Keep names")[second.finding_id]
            self.assertIsInstance(result, style_ledger.CellWaived)
            with closing(sqlite3.connect(ledger.database)) as connection:
                row = cast(tuple[str, str, str, str], connection.execute(
                    "SELECT verdict,verdict_detail,ticket,worker FROM judgments WHERE rule=?", (self.rename,),
                ).fetchone())
            self.assertEqual(row, ("waived", "Keep names", "outside", "test"))
            self.assertEqual(len(ledger.held_findings(style_ledger.HeldFindingsInState("waived"))), 2)
            self.assertEqual(next(cell.state for cell in ledger.cells(self.rename)), "current")

    def test_one_decline_call_waives_many_findings_with_one_catalog_load(self) -> None:
        with self.ledger() as ledger:
            alpha = tuple(self.hold(ledger, line=line) for line in range(1, 6))
            beta = self.hold(ledger, crate="beta")
            kept = self.hold(ledger, crate="beta", line=2)
            declined = (*alpha, beta)
            with (mock.patch.object(rules, "load_rules", wraps=rules.load_rules) as load,
                  mock.patch.object(ledger, "held_findings", wraps=ledger.held_findings) as scans):
                outcomes = ledger.decline_findings(tuple(item.finding_id for item in declined), "Keep names")
            self.assertEqual(load.call_count, 1)
            self.assertEqual(scans.call_count, 0)
            self.assertEqual(list(outcomes), [item.finding_id for item in declined])
            self.assertTrue(all(isinstance(outcomes[item.finding_id], style_ledger.CellWaived) for item in alpha))
            # The other beta finding still holds the beta cell.
            self.assertIsInstance(outcomes[beta.finding_id], style_ledger.CellStillHeld)
            self.assertEqual({item.finding_id for item in ledger.held_findings(style_ledger.HeldFindingsInState("waived"))},
                             {item.finding_id for item in declined})
            self.assertNotIn(kept.finding_id, {item.finding_id for item in ledger.held_findings(style_ledger.HeldFindingsInState("waived"))})
            with closing(sqlite3.connect(ledger.database)) as connection:
                self.assertEqual(sorted(connection.execute("SELECT path,verdict,verdict_detail FROM judgments").fetchall()),
                                 [("alpha/src/lib.rs", "waived", "Keep names"), ("beta/src/lib.rs", "held", "")])

    def test_decline_structure_resolves_the_file_cell(self) -> None:
        with self.ledger() as ledger:
            finding = self.hold(ledger, rule=self.structure)
            result = ledger.decline_findings((finding.finding_id,), "Keep structure")[finding.finding_id]
            self.assertIsInstance(result, style_ledger.CellWaived)
            if isinstance(result, style_ledger.CellWaived):
                self.assertEqual(result.judgment.path, "alpha/src/lib.rs")
            with closing(sqlite3.connect(ledger.database)) as connection:
                self.assertEqual(connection.execute("SELECT path,verdict FROM judgments").fetchall(), [("alpha/src/lib.rs", "waived")])

    def test_decline_changed_input_and_missing_judgment_do_not_record_waived_cells(self) -> None:
        with self.ledger() as ledger:
            missing = self.hold(ledger, line=2, record=False)
            self.assertIsInstance(ledger.decline_findings((missing.finding_id,), "No")[missing.finding_id], style_ledger.NoHeldJudgment)
            held = self.hold(ledger)
            self.write("alpha/src/lib.rs", "fn value() -> u32 { 2 }\n")
            _ = self.git("add", "--all")
            _ = self.git("commit", "-qm", "Input changed")
            ledger.set_tip(self.git("rev-parse", "HEAD"))
            self.assertIsInstance(ledger.decline_findings((held.finding_id,), "No")[held.finding_id], style_ledger.InputChangedSinceJudged)
            with closing(sqlite3.connect(ledger.database)) as connection:
                self.assertEqual(connection.execute("SELECT verdict FROM judgments").fetchall(), [("held",)])

    def test_decline_recovers_after_interruption_before_close_and_rerun_is_noop(self) -> None:
        with self.ledger() as ledger:
            _ = self.hold(ledger)
            issue = self.file(ledger)[0]
            with mock.patch.object(style_proposals, "proposal_command", side_effect=OSError("before close")):
                with self.assertRaisesRegex(OSError, "before close"):
                    style_proposals.decline(ledger, issue, "Keep the API")
            self.assertEqual(frontmatter(issue)["status"], ["open"])
            style_proposals.decline(ledger, issue, "Keep the API")
            self.assertEqual(frontmatter(issue)["reason"], ["declined: Keep the API"])
            self.assertEqual(ledger.summary().proposals, 0)
            self.assertEqual(open_issues(self.proposals, "proposal"), [])
            self.assertEqual(proposals_table.issue_rows(open_issues(self.proposals, "proposal")), [])
            calls = len(self.calls)
            style_proposals.decline(ledger, issue, "Keep the API")
            self.assertEqual(len(self.calls), calls)

    def test_a_decline_stopped_after_its_waive_is_not_closed_as_approved_and_its_rerun_closes_it(self) -> None:
        with self.ledger() as ledger:
            first, second = self.hold(ledger), self.hold(ledger, line=2)
            issue = self.file(ledger)[0]
            self.issue_command("approve", str(issue), "--showrunner", "parent", "--unit", "names-unit", "--plan", "unit.md")
            waive = ledger.decline_findings

            def stop_after_waive(finding_ids: tuple[str, ...], words: str) -> dict[str, style_ledger.DeclineOutcome]:
                _ = waive(finding_ids, words)
                raise OSError("after waive")
            with (mock.patch.object(ledger, "decline_findings", side_effect=stop_after_waive),
                  self.assertRaisesRegex(OSError, "after waive")):
                style_proposals.decline(ledger, issue, "Keep names")
            with redirect_stdout(io.StringIO()):
                style_proposals.complete(ledger)
            self.assertEqual(frontmatter(issue)["status"], ["open"])
            self.assertNotIn("close", [call[0] for call in self.calls])
            style_proposals.decline(ledger, issue, "Keep names")
            self.assertEqual((frontmatter(issue)["status"], frontmatter(issue)["reason"]), (["closed"], ["declined: Keep names"]))
            self.assertEqual({finding.finding_id: type(finding.state) for finding in ledger.proposal_findings(issue)},
                             {first.finding_id: style_ledger.Waived, second.finding_id: style_ledger.Waived})

    def test_complete_leaves_a_legacy_approved_issue_with_every_finding_waived_open_for_migrate(self) -> None:
        with self.ledger() as ledger:
            finding = self.hold(ledger)
            old = self.legacy_issue(ledger, "old", finding)
            _ = ledger.decline_findings((finding.finding_id,), "Keep names")
            self.assertFalse(style_proposals.close_finished_decision(ledger, old))
            with redirect_stdout(io.StringIO()):
                style_proposals.complete(ledger)
            self.assertEqual(frontmatter(old)["status"], ["open"])
            self.assertEqual(self.calls, [])

    def test_proposal_ticket_requires_filed_findings_and_builds_only_after_a_valid_merge(self) -> None:
        with self.ledger() as ledger:
            finding = self.hold(ledger)
            issue = self.proposals / "approved.md"
            with self.assertRaisesRegex(style_ledger.LedgerError, "filed findings"):
                ledger.ticket_add("names-unit-1", "names-unit", "proposal", "names", "alpha", [finding.path], [self.rename], proposal=str(issue), finding_ids=(finding.finding_id,))
            issue = self.file(ledger)[0]
            self.issue_command("approve", str(issue), "--showrunner", "parent", "--unit", "names-unit", "--plan", "unit.md")
            ledger.ticket_add("names-unit-1", "names-unit", "proposal", "names", "alpha", [finding.path], [self.rename], proposal=str(issue), finding_ids=(finding.finding_id,))
            origin = ledger.ticket_origin("names-unit-1")
            self.assertEqual(origin, style_ledger.ApprovedProposal(issue, "names-unit", (finding.finding_id,)))
            ledger.ticket_assign("names-unit-1", "main")
            self.write(finding.path, APPLIED + "fn value() -> u32 { 2 }\n")
            _ = self.git("add", "--all")
            _ = self.git("commit", "-qm", "Approved behavior change")
            checkpoint = self.git("rev-parse", "HEAD")
            ledger.ticket_checkpoint("names-unit-1", checkpoint)
            with self.assertRaisesRegex(style_ledger.LedgerError, "not on"):
                _ = ledger.ticket_merge("names-unit-1")
            self.assertEqual(len(ledger.held_findings(style_ledger.HeldFindingsInState("filed"))), 1)
            _ = self.git("branch", "-f", "style/main", checkpoint)
            _ = ledger.ticket_merge("names-unit-1")
            self.assertEqual(len(ledger.held_findings(style_ledger.HeldFindingsInState("built"))), 1)
            self.assertEqual(ledger.summary().proposals, 0)
            style_proposals.complete(ledger)
            self.assertEqual(frontmatter(issue)["reason"], ["built in names-unit-1"])
            self.assertEqual(frontmatter(issue)["status"], ["closed"])
            calls = len(self.calls)
            _ = ledger.ticket_merge("names-unit-1")
            style_proposals.complete(ledger)
            self.assertEqual(len(self.calls), calls)

    def newer_rule_text(self) -> str:
        rule = self.catalog / "names.md"
        _ = rule.write_text(rule.read_text() + "Newer rule text.\n")
        return next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == self.rename)

    def newer_verdict(self, ledger: style_ledger.Ledger, blob: str, verdict: style_ledger.Verdict,
                      path: str = "alpha/src/lib.rs") -> int:
        ledger.record(self.rename, path, self.tip, blob, verdict, ticket="newer", worker="test",
                      input_scope=style_ledger.DeclaredInputScope.RULE)
        with closing(sqlite3.connect(ledger.database)) as connection:
            return cast(tuple[int], connection.execute("SELECT MAX(id) FROM judgments").fetchone())[0]

    def withdraw(self, ledger: style_ledger.Ledger) -> str:
        with redirect_stdout(io.StringIO()) as output:
            skipped = style_proposals.run_withdrawal(style_proposals.decision_writer(ledger, self.proposals, "parent"), dry_run=False)
        self.assertEqual(skipped, ())
        return output.getvalue()

    def test_an_exception_under_a_newer_rule_text_withdraws_its_filed_findings_and_closes_their_decision(self) -> None:
        with self.ledger() as ledger:
            first, second = self.hold(ledger), self.hold(ledger, line=2)
            issue = self.file(ledger)[0]
            blob = self.newer_rule_text()
            row = self.newer_verdict(ledger, blob, "exception")
            output = self.withdraw(ledger)
            for finding in (first, second):
                self.assertIn(f"withdraw {finding.finding_id} {self.rename} alpha/src/lib.rs:{finding.line}: "
                              + f"judgment {row} exception under {blob[:8]}", output)
            self.assertEqual({finding.state for finding in ledger.proposal_findings(issue)}, {style_ledger.Withdrawn(issue, row)})
            self.assertEqual(frontmatter(issue)["status"], ["closed"])
            reason = frontmatter(issue)["reason"][0]
            self.assertTrue(reason.startswith(style_proposals.NOTHING_LEFT_REASON))
            for finding in (first, second):
                self.assertIn(f"{finding.finding_id} (judgment {row})", reason)
            self.assertIn(f"close {issue}: {reason}", output)
            self.assertIn("totals: withdraw 2, kept 0, close 1", output)
            closed = issue.read_bytes()
            self.assertIn("totals: withdraw 0, kept 0, close 0", self.withdraw(ledger))
            self.assertEqual(issue.read_bytes(), closed)

    def test_a_newer_clean_or_held_verdict_or_an_exception_at_another_site_withdraws_nothing(self) -> None:
        with self.ledger() as ledger:
            finding = self.hold(ledger)
            issue = self.file(ledger)[0]
            before = issue.read_bytes()
            blob = self.newer_rule_text()
            verdicts: tuple[tuple[style_ledger.Verdict, str], ...] = (
                ("clean", finding.path), ("held", finding.path), ("exception", "beta/src/lib.rs"))
            for verdict, path in verdicts:
                _ = self.newer_verdict(ledger, blob, verdict, path)
                self.assertIn("totals: withdraw 0, kept 0, close 0", self.withdraw(ledger))
                self.assertEqual(ledger.proposal_findings(issue)[0].state, style_ledger.Filed(issue))
            self.assertEqual(issue.read_bytes(), before)

    def test_a_finding_that_an_open_ticket_builds_is_kept_and_listed(self) -> None:
        with self.ledger() as ledger:
            finding = self.hold(ledger)
            issue = self.file(ledger)[0]
            self.issue_command("approve", str(issue), "--showrunner", "parent", "--unit", "names-unit", "--plan", "unit.md")
            ledger.ticket_add("names-unit-1", "names-unit", "proposal", "names", "alpha", [finding.path], [self.rename],
                              proposal=str(issue), finding_ids=(finding.finding_id,))
            blob = self.newer_rule_text()
            row = self.newer_verdict(ledger, blob, "exception")
            output = self.withdraw(ledger)
            self.assertIn(f"kept {finding.finding_id} {self.rename} {finding.path}:1: judgment {row} exception under "
                          + f"{blob[:8]}; open ticket names-unit-1 builds it", output)
            self.assertIn("totals: withdraw 0, kept 1, close 0", output)
            self.assertEqual(ledger.proposal_findings(issue)[0].state, style_ledger.Filed(issue))
            self.assertEqual(frontmatter(issue)["status"], ["open"])

    def test_a_decision_with_a_withdrawn_and_a_filed_finding_stays_open_and_shows_only_the_filed_one(self) -> None:
        with self.ledger() as ledger:
            withdrawn = self.hold(ledger)
            filed = ledger.hold(self.rename, "alpha/src/other.rs", 3, "other site", "rename", "Kind::Other", "Kind::Size",
                                withdrawn.rule_blob, "outside")
            issue = self.file(ledger)[0]
            self.assertEqual(frontmatter(issue)["style_count"], ["2"])
            blob = self.newer_rule_text()
            row = self.newer_verdict(ledger, blob, "exception")
            self.assertIn("totals: withdraw 1, kept 0, close 0", self.withdraw(ledger))
            self.assertEqual({finding.finding_id: finding.state for finding in ledger.proposal_findings(issue)},
                             {withdrawn.finding_id: style_ledger.Withdrawn(issue, row), filed.finding_id: style_ledger.Filed(issue)})
            self.assertEqual(frontmatter(issue)["status"], ["open"])
            self.assertEqual(frontmatter(issue)["style_count"], ["1"])
            body = issue.read_text()
            self.assertIn("Parent: parent", body)
            self.assertEqual(re.findall(r"^Site: (.*)$", body, re.MULTILINE), ["alpha/src/other.rs:3"])

    def test_withdraw_dry_run_prints_the_same_lines_and_writes_nothing(self) -> None:
        with self.ledger() as ledger:
            _ = self.hold(ledger)
            issue = self.file(ledger)[0]
            _ = self.newer_verdict(ledger, self.newer_rule_text(), "exception")
            state = ledger.database.parent.parent
        before = {path: path.read_bytes() for path in state.rglob("*") if path.is_file()}
        issue_bytes = issue.read_bytes()
        arguments = ["withdraw", "--repo", str(self.repo), "--proposals", str(self.proposals)]
        with redirect_stdout(io.StringIO()) as dry:
            self.assertEqual(style_proposals.main([*arguments, "--dry-run"]), 0)
        self.assertEqual({path: path.read_bytes() for path in state.rglob("*") if path.is_file()}, before)
        self.assertEqual(issue.read_bytes(), issue_bytes)
        with redirect_stdout(io.StringIO()) as written:
            self.assertEqual(style_proposals.main(arguments), 0)
        self.assertEqual(dry.getvalue(), written.getvalue())
        self.assertIn("totals: withdraw 1, kept 0, close 1", written.getvalue())
        self.assertEqual(frontmatter(issue)["status"], ["closed"])

    def test_the_filing_run_withdraws_before_it_files(self) -> None:
        with self.ledger() as ledger:
            old = self.hold(ledger)
            old_issue = self.file(ledger)[0]
            _ = self.newer_verdict(ledger, self.newer_rule_text(), "exception")
            _ = self.hold(ledger, crate="beta")
            with redirect_stdout(io.StringIO()) as output:
                filed = style_proposals.file_proposals(ledger, self.proposals, "parent").filed
            text = output.getvalue()
            self.assertEqual(len(filed), 1)
            self.assertLess(text.index(f"withdraw {old.finding_id}"), text.index(f"filed {filed[0]}"))
            self.assertEqual(frontmatter(old_issue)["status"], ["closed"])

    def test_an_older_exception_under_a_restored_rule_text_withdraws_no_finding_judged_after_it(self) -> None:
        with self.ledger() as ledger:
            names = self.catalog / "names.md"
            original = names.read_text()
            old_blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == self.rename)
            _ = self.newer_verdict(ledger, old_blob, "exception")
            _ = self.newer_rule_text()
            finding = self.hold(ledger)
            issue = self.file(ledger)[0]
            _ = names.write_text(original)
            self.assertIn("totals: withdraw 0, kept 0, close 0", self.withdraw(ledger))
            self.assertEqual([(item.finding_id, item.state) for item in ledger.proposal_findings(issue)],
                             [(finding.finding_id, style_ledger.Filed(issue))])

    def test_withdrawal_leaves_a_decision_with_a_built_finding_to_complete(self) -> None:
        self.write("alpha/src/other.rs", APPLIED)
        _ = self.git("add", "--all")
        _ = self.git("commit", "-qm", "Other file")
        self.tip = self.git("rev-parse", "HEAD")
        _ = self.git("branch", "-f", "style/main", self.tip)
        with self.ledger() as ledger:
            ledger.set_tip(self.tip, "style/main")
            built = self.hold(ledger)
            withdrawn = ledger.hold(self.rename, "alpha/src/other.rs", 1, "other site", "rename", "Kind::Value", "Kind::Count",
                                    built.rule_blob, "outside")
            ledger.record(self.rename, withdrawn.path, self.tip, built.rule_blob, "held", ticket="outside", worker="test",
                          input_scope=style_ledger.DeclaredInputScope.RULE)
            issue = self.approved_decision(ledger, withdrawn, built)
            self.assertEqual(self.merge_ticket(ledger, "names-unit-1", issue, built), ())
            row = self.newer_verdict(ledger, self.newer_rule_text(), "exception", withdrawn.path)
            self.assertIn("totals: withdraw 1, kept 0, close 0", self.withdraw(ledger))
            self.assertEqual({finding.finding_id: finding.state for finding in ledger.proposal_findings(issue)},
                             {withdrawn.finding_id: style_ledger.Withdrawn(issue, row), built.finding_id: style_ledger.Built(issue)})
            self.assertEqual(frontmatter(issue)["status"], ["open"])
            with redirect_stdout(io.StringIO()):
                style_proposals.complete(ledger)
            self.assertEqual((frontmatter(issue)["status"], frontmatter(issue)["reason"]), (["closed"], ["built in names-unit-1"]))

    def test_a_decision_that_withdrawal_cannot_close_or_refresh_is_skipped_and_the_others_still_file(self) -> None:
        with self.ledger() as ledger:
            _ = self.hold(ledger)
            _ = self.hold(ledger, crate="beta")
            blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == self.rename)
            _ = ledger.hold(self.rename, "alpha/Cargo.toml", 1, "manifest site", "rename", "Kind::Value", "Kind::Count",
                            blob, "outside")
            refreshed, closed = sorted(self.file(ledger), key=lambda issue: frontmatter(issue)["style_crate"])
            _ = refreshed.write_text(refreshed.read_text().replace("Parent: parent\n", ""))
            refreshed_bytes = refreshed.read_bytes()
            newer = self.newer_rule_text()
            for path in ("alpha/src/lib.rs", "beta/src/lib.rs"):
                _ = self.newer_verdict(ledger, newer, "exception", path)
            later = self.hold(ledger, line=5, record=False)

            def refuse_close(*args: str) -> None:
                if args[:2] == ("close", str(closed)):
                    raise style_ledger.LedgerError(f"{closed}: close refused")
                self.issue_command(*args)

            with (mock.patch.object(style_proposals, "proposal_command", side_effect=refuse_close),
                  redirect_stdout(io.StringIO()) as output):
                result = style_proposals.file_proposals(ledger, self.proposals, "parent")
            self.assertIn(f"skipped {closed}: close refused", output.getvalue().splitlines())
            self.assertIn(f"skipped {refreshed}: no Parent line", output.getvalue().splitlines())
            self.assertEqual(set(result.skipped), {refreshed, closed})
            filed, = result.filed
            self.assertEqual(ledger.proposal_findings(filed)[0].finding_id, later.finding_id)
            self.assertEqual(frontmatter(closed)["status"], ["open"])
            self.assertEqual(refreshed.read_bytes(), refreshed_bytes)

    def test_withdrawal_renumbers_once_when_it_closes_decisions_and_never_on_a_dry_run(self) -> None:
        with self.ledger() as ledger:
            _ = self.hold(ledger)
            _ = self.hold(ledger, crate="beta")
            issues = self.file(ledger)
            self.assertEqual(len(issues), 2)
            blob = self.newer_rule_text()
            for path in ("alpha/src/lib.rs", "beta/src/lib.rs"):
                _ = self.newer_verdict(ledger, blob, "exception", path)
            with mock.patch.object(style_proposals, "renumber") as renumber:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(style_proposals.run_withdrawal(
                        style_proposals.decision_writer(ledger, self.proposals, "parent"), dry_run=True), ())
                self.assertEqual(renumber.call_count, 0)
                self.assertIn("totals: withdraw 2, kept 0, close 2", self.withdraw(ledger))
                self.assertEqual(renumber.call_count, 1)
                self.assertIn("totals: withdraw 0, kept 0, close 0", self.withdraw(ledger))
                self.assertEqual(renumber.call_count, 1)
            self.assertEqual({frontmatter(issue)["status"][0] for issue in issues}, {"closed"})


if __name__ == "__main__":
    _ = unittest.main()
