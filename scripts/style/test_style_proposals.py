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
from pathlib import Path
from typing import cast, final, override
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rules
import coverage_report
import style_ledger
import style_proposals
import add_style
import style_rules
import proposal_issue
import proposals_table
from issues import frontmatter, open_issues

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
        })))
        _ = self.enterContext(mock.patch.object(style_rules, "NATE_STYLE_DIR", self.catalog.parent))
        _ = self.git("init", "-q", "--initial-branch=main")
        _ = self.git("config", "user.name", "Proposal tests")
        _ = self.git("config", "user.email", "proposals@example.invalid")
        self.write("Cargo.toml", '[workspace]\nmembers = ["alpha", "beta"]\n')
        for crate in ("alpha", "beta"):
            self.write(f"{crate}/Cargo.toml", f'[package]\nname = "{crate}"\nversion = "0.1.0"\n')
            self.write(f"{crate}/src/lib.rs", "fn value() -> u32 { 1 }\n")
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
             ticket: str = "outside") -> style_ledger.HeldFinding:
        blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule)
        finding = ledger.hold(rule, f"{crate}/src/lib.rs", line, f"site {line}", kind, "value", "count", blob, ticket)
        if record:
            ledger.record(rule, finding.path, self.tip, blob, "held", ticket=ticket, worker="test")
        return finding

    def file(self, ledger: style_ledger.Ledger) -> tuple[Path, ...]:
        with redirect_stdout(io.StringIO()):
            return style_proposals.file_proposals(ledger, self.proposals, "parent")

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

    def test_goal_counts_only_open_non_style_project_issues_and_breaks_ties(self) -> None:
        self.ranked_issue("one", "2 - Find Collaborators")
        for name, options in (("style", {"tags": "issue, style"}), ("closed", {"status": "closed"}),
                              ("other", {"project": "other"})):
            self.ranked_issue(name, "1 - Ship Hana", **options)
        self.ranked_issue("invalid", "not a goal")
        self.assertEqual(style_proposals.project_goal(self.proposals, "sample"), "2 - Find Collaborators")
        self.ranked_issue("tie", "1 - Ship Hana")
        self.assertEqual(style_proposals.project_goal(self.proposals, "sample"), "1 - Ship Hana")

    def test_goal_default_names_project_once_for_multiple_batches(self) -> None:
        output = io.StringIO()
        with self.ledger() as ledger:
            _ = self.hold(ledger)
            _ = self.hold(ledger, kind="type", line=2)
            with redirect_stdout(output):
                paths = style_proposals.file_proposals(ledger, self.proposals, "parent")
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
            issue = self.file(ledger)[0]
        self.assertEqual(frontmatter(issue)["style_rules"], [
            "https://github.com/example/nate_style/blob/main/rust/propose.md",
            "https://github.com/example/sample/blob/main/docs/style/modules.md",
        ])
        self.assertIn(f"Rule: {local_rule}\n", issue.read_text())
        self.assertIn(f"Rule: {self.propose}#uncovered\n", issue.read_text())

    def test_link_rules_backfills_open_and_closed_issues_and_is_idempotent(self) -> None:
        with self.ledger() as ledger:
            _ = self.hold(ledger)
            opened = self.file(ledger)[0]
            _ = self.hold(ledger, kind="type", line=2)
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
        self.assertIn(str(unknown), output.getvalue())
        self.assertTrue(output.getvalue().endswith("linked 2, already linked 1, skipped 1\n"))
        for issue in (opened, closed):
            self.assertEqual(frontmatter(issue)["style_rules"],
                             ["https://github.com/example/nate_style/blob/main/rust/names.md"])
        self.assertEqual(frontmatter(closed)["status"], ["closed"])
        self.assertEqual({path: path.read_bytes() for path in untouched}, untouched)
        stamps = {path: path.stat().st_mtime_ns for path in self.proposals.glob("*.md")}
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(style_proposals.main(args), 0)
        self.assertTrue(output.getvalue().endswith("linked 0, already linked 3, skipped 1\n"))
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
        self.assertRegex(issue.name, r"^style sample [0-9a-f]{8}\.md$")
        self.assertIn("# Style restructure: sample\n", issue.read_text())
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
        self.assertIn("# Style rename: names\n", issue.read_text())

    def test_rank_fills_only_empty_ranking_and_root_title_preserving_other_bytes_and_is_idempotent(self) -> None:
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
        _ = root_issue.write_bytes(expected.replace(ranking, b"Proposed ranking: ")
                                  .replace(b"# Style restructure: sample\r\n", b"# Style restructure: \r\n"))
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
        self.assertIn(f"skipped {unknown}: {unknown}: unknown or absent style_findings\n", output.getvalue())
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
            ledger.ticket_merge(merged)
            dropped = self.assign_ticket(ledger, "dropped")
            dropped_finding = self.hold(ledger, ticket=dropped, line=4)
            ledger.ticket_drop(dropped)
            output = io.StringIO()
            with redirect_stdout(output):
                paths = style_proposals.file_proposals(ledger, self.proposals, "parent")
            self.assertEqual(len(paths), 1)
            self.assertEqual(set(frontmatter(paths[0])["style_findings"]), {ready.finding_id, dropped_finding.finding_id})
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
            ledger.ticket_merge(ticket)
            paths = self.file(ledger)
            self.assertEqual(len(paths), 1)
            self.assertEqual(frontmatter(paths[0])["style_findings"], [finding.finding_id])
            self.assertIsInstance(ledger.held_findings(style_ledger.AllHeldFindings())[0].state, style_ledger.Filed)

    def test_checkpointed_ticket_findings_remain_unfiled_with_a_wait_notice(self) -> None:
        with self.ledger() as ledger:
            ticket = self.assign_ticket(ledger, "names")
            finding = self.hold(ledger, ticket=ticket)
            ledger.ticket_checkpoint(ticket, self.tip)
            output = io.StringIO()
            with redirect_stdout(output):
                paths = style_proposals.file_proposals(ledger, self.proposals, "parent")
            self.assertEqual(paths, ())
            self.assertEqual(output.getvalue(), f"held for open ticket {ticket}: 1 findings\n")
            self.assertEqual(self.calls, [])
            self.assertEqual(ledger.held_findings(style_ledger.HeldFindingsInState("unfiled")), (finding,))

    def test_batches_by_kind_and_crate_include_every_site_and_issue_fields(self) -> None:
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
            shared = next(path for path in paths if {first.finding_id, second.finding_id} <= set(frontmatter(path)["style_findings"]))
            body = shared.read_text()
            self.assertIn("alpha/src/lib.rs:1 — site 1", body)
            self.assertIn("alpha/src/lib.rs:2 — site 2", body)
            self.assertIn("Before:\n```\nvalue", body)
            self.assertIn("After:\n```\ncount", body)
            self.assertIn("Live units holding file: none", body)
            self.assertIn("Recommended answer:", body)
            self.assertTrue(body.split("---", 2)[2].lstrip().startswith("Proposed ranking:"))
            fields = frontmatter(shared)
            self.assertEqual(fields["area"], ["unfiled"])
            self.assertEqual(fields["project"], ["alpha"])
            self.assertEqual(fields["proposed_by"], ["sample-style"])
            self.assertEqual(set(fields["tags"]), {"issue", "proposal", "style"})
            self.assertEqual(fields["backlog_goal"], [])
            self.assertTrue(all(len(path.name) <= 50 for path in paths))
            self.assertEqual(len(open_issues(self.proposals, "proposal")), ledger.summary().proposals)
            self.assertEqual(len(proposals_table.issue_rows(self.proposals)), ledger.summary().proposals)
            before = {path: path.read_bytes() for path in paths}
            self.assertEqual(self.file(ledger), ())
            self.assertEqual({path: path.read_bytes() for path in paths}, before)
            self.assertEqual(len(self.calls), 4)

    def test_file_reads_workspace_once_for_many_findings_in_multiple_crates(self) -> None:
        with self.ledger() as ledger:
            findings = tuple(self.hold(ledger, crate=crate, line=line)
                             for crate in ("alpha", "beta") for line in range(1, 5))
            with mock.patch.object(coverage_report, "workspace", wraps=coverage_report.workspace) as workspace:
                paths = self.file(ledger)
                workspace.assert_called_once_with(ledger, self.tip)
            self.assertEqual(len(paths), 2)
            for path in paths:
                fields = frontmatter(path)
                crate = next(finding.path.split("/")[0] for finding in findings
                             if finding.finding_id == fields["style_findings"][0])
                self.assertEqual(set(fields["style_findings"]),
                                 {finding.finding_id for finding in findings if finding.path.startswith(crate + "/")})
                self.assertIn("# Style rename: names\n", path.read_text())
                self.assertIn("Proposed ranking: goal 1 - Ship Hana · alignment ⭐⭐ · impact ⭐ · urgency ⭐ · effort ⭐⭐\n",
                              path.read_text())

    def test_rank_reads_workspace_and_held_findings_once_for_multiple_issues_and_crates(self) -> None:
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
                held_findings.assert_called_once_with(style_ledger.AllHeldFindings())
            self.assertEqual({path: path.read_bytes() for path in paths}, expected)
            self.assertTrue(output.getvalue().endswith("ranked 2 issues\n"))

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
                issue = style_proposals.file_proposals(ledger, self.proposals, "parent")[0]
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
            ledger.ticket_add("names-unit-1", "names-unit", "proposal", "names", "alpha", [finding.path], [self.rename], proposal=str(issue))
            ledger.ticket_assign("names-unit-1", "main")
            ledger.ticket_checkpoint("names-unit-1", self.tip)
            ledger.ticket_merge("names-unit-1")
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
            self.assertEqual(self.file(ledger), ())
            findings = ledger.held_findings(style_ledger.HeldFindingsInState("filed"))
            self.assertEqual({finding.finding_id for finding in findings}, {first.finding_id, second.finding_id})
            self.assertEqual(len(self.calls), 1)
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
            self.assertEqual(self.file(ledger), ())
            self.assertEqual([item.finding_id for item in ledger.held_findings(style_ledger.HeldFindingsInState("filed"))], [finding.finding_id])
            self.assertEqual(len(self.calls), 1)

    def test_changed_rule_is_skipped_while_other_batches_file(self) -> None:
        with self.ledger() as ledger:
            old = self.hold(ledger)
            _ = self.hold(ledger, rule=self.propose)
            rule_file = self.catalog / "names.md"
            _ = rule_file.write_text(rule_file.read_text() + "Changed text.\n")
            output = io.StringIO()
            with redirect_stdout(output):
                paths = style_proposals.file_proposals(ledger, self.proposals, "parent")
            self.assertIn(f"skipped {old.finding_id}: rule changed since judged", output.getvalue())
            self.assertEqual(len(paths), 1)
            self.assertEqual([finding.finding_id for finding in ledger.held_findings(style_ledger.HeldFindingsInState("unfiled"))], [old.finding_id])
            self.assertEqual(next(cell.state for cell in ledger.cells(self.rename)), "stale")

    def test_decline_waits_for_both_findings_then_waives_the_same_cell_with_words(self) -> None:
        with self.ledger() as ledger:
            first, second = self.hold(ledger), self.hold(ledger, line=2)
            self.assertIsInstance(ledger.decline_held(first.finding_id, "Keep names"), style_ledger.CellStillHeld)
            result = ledger.decline_held(second.finding_id, "Keep names")
            self.assertIsInstance(result, style_ledger.CellWaived)
            with closing(sqlite3.connect(ledger.database)) as connection:
                row = cast(tuple[str, str, str, str], connection.execute(
                    "SELECT verdict,verdict_detail,ticket,worker FROM judgments WHERE rule=?", (self.rename,),
                ).fetchone())
            self.assertEqual(row, ("waived", "Keep names", "outside", "test"))
            self.assertEqual(len(ledger.held_findings(style_ledger.HeldFindingsInState("waived"))), 2)
            self.assertEqual(next(cell.state for cell in ledger.cells(self.rename)), "current")

    def test_decline_structure_resolves_the_file_cell(self) -> None:
        with self.ledger() as ledger:
            finding = self.hold(ledger, rule=self.structure)
            result = ledger.decline_held(finding.finding_id, "Keep structure")
            self.assertIsInstance(result, style_ledger.CellWaived)
            if isinstance(result, style_ledger.CellWaived):
                self.assertEqual(result.judgment.path, "alpha/src/lib.rs")
            with closing(sqlite3.connect(ledger.database)) as connection:
                self.assertEqual(connection.execute("SELECT path,verdict FROM judgments").fetchall(), [("alpha/src/lib.rs", "waived")])

    def test_decline_changed_input_and_missing_judgment_do_not_record_waived_cells(self) -> None:
        with self.ledger() as ledger:
            missing = self.hold(ledger, line=2, record=False)
            self.assertIsInstance(ledger.decline_held(missing.finding_id, "No"), style_ledger.NoHeldJudgment)
            held = self.hold(ledger)
            self.write("alpha/src/lib.rs", "fn value() -> u32 { 2 }\n")
            _ = self.git("add", "--all")
            _ = self.git("commit", "-qm", "Input changed")
            ledger.set_tip(self.git("rev-parse", "HEAD"))
            self.assertIsInstance(ledger.decline_held(held.finding_id, "No"), style_ledger.InputChangedSinceJudged)
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
            self.assertEqual(proposals_table.issue_rows(self.proposals), [])
            calls = len(self.calls)
            style_proposals.decline(ledger, issue, "Keep the API")
            self.assertEqual(len(self.calls), calls)

    def test_proposal_ticket_requires_filed_findings_and_builds_only_after_a_valid_merge(self) -> None:
        with self.ledger() as ledger:
            finding = self.hold(ledger)
            issue = self.proposals / "approved.md"
            with self.assertRaisesRegex(style_ledger.LedgerError, "filed findings"):
                ledger.ticket_add("names-unit-1", "names-unit", "proposal", "names", "alpha", [finding.path], [self.rename], proposal=str(issue))
            issue = self.file(ledger)[0]
            ledger.ticket_add("names-unit-1", "names-unit", "proposal", "names", "alpha", [finding.path], [self.rename], proposal=str(issue))
            origin = ledger.ticket_origin("names-unit-1")
            self.assertEqual(origin, style_ledger.ApprovedProposal(issue, "names-unit", (finding.finding_id,)))
            ledger.ticket_assign("names-unit-1", "main")
            self.write(finding.path, "fn value() -> u32 { 2 }\n")
            _ = self.git("add", "--all")
            _ = self.git("commit", "-qm", "Approved behavior change")
            checkpoint = self.git("rev-parse", "HEAD")
            ledger.ticket_checkpoint("names-unit-1", checkpoint)
            with self.assertRaisesRegex(style_ledger.LedgerError, "not on"):
                ledger.ticket_merge("names-unit-1")
            self.assertEqual(len(ledger.held_findings(style_ledger.HeldFindingsInState("filed"))), 1)
            _ = self.git("branch", "-f", "style/main", checkpoint)
            ledger.ticket_merge("names-unit-1")
            self.assertEqual(len(ledger.held_findings(style_ledger.HeldFindingsInState("built"))), 1)
            self.assertEqual(ledger.summary().proposals, 0)
            style_proposals.complete(ledger)
            self.assertEqual(frontmatter(issue)["reason"], ["built in names-unit-1"])
            self.assertEqual(frontmatter(issue)["status"], ["closed"])
            calls = len(self.calls)
            ledger.ticket_merge("names-unit-1")
            style_proposals.complete(ledger)
            self.assertEqual(len(self.calls), calls)


if __name__ == "__main__":
    _ = unittest.main()
