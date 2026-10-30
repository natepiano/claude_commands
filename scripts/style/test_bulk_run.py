"""Workspace bulk runs use temporary Git repos and a fake command runner; never build Rust."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import unittest
from unittest import mock
from collections.abc import Sequence
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import cast, override

import bulk_run as run
import bulk_rename as bulk
import rules
import style_ledger as ledger_api
from test_bulk_rename import CargoProjectTests, ScriptedServer, word_edits


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, tuple[str, ...]]] = []
        self.error_file: str = ""
        self.no_progress: bool = False
        self.red_tests: bool = False
        self.unrelated: bool = False
        self.break_proof: bool = False
        self.test_checkout: Path = Path()
        self.test_tree: str = ""
        self.fail_stage: str = ""
        self.mutate_stage: str = ""
        self.other_tree_record: bool = False

    def __call__(self, repo: Path, arguments: Sequence[str]) -> run.CommandResult:
        args = tuple(arguments)
        self.calls.append((repo, args))
        if "fmt" in args:
            if self.unrelated:
                _ = (repo / "Cargo.toml").write_text("unrelated change")
            if self.break_proof:
                _ = (repo / "src/lib.rs").write_text("pub fn run() {}\n")
        if len(args) > 1 and args[1] in ("check", "clippy"):
            if self.no_progress:
                return run.CommandResult(1, "linker failed without a source span")
            if self.error_file and "let number" in (repo / self.error_file).read_text():
                return run.CommandResult(1, json.dumps({"reason": "compiler-message", "message": {
                    "level": "error", "spans": [{"file_name": self.error_file}], "children": []}}))
        if len(args) >= 3 and args[0] == "bash":
            self.test_checkout = repo
            self.test_tree = run.git(repo, "rev-parse", "HEAD^{tree}")
            stage = args[2]
            if stage == self.mutate_stage:
                _ = (repo / "unexpected.txt").write_text("untracked test output")
            if stage == "final" and self.red_tests:
                return run.CommandResult(1, "FAIL [0.01s] bulk_sample::tests::broken\n")
            if stage == self.fail_stage:
                return run.CommandResult(1, stderr=f"{stage} refused")
            if stage == "final-pass" and (len(args) != 4 or self.other_tree_record or args[3] != self.test_tree):
                return run.CommandResult(1, stderr="no matching tested-tree record")
        return run.CommandResult(0)


class WorkspaceRunTests(CargoProjectTests):
    base: str = ""
    tree: str = ""
    runner: FakeRunner = FakeRunner()
    server: ScriptedServer = ScriptedServer(lambda text, offset, new: word_edits(text, "old", new))

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = (self.repo / "Cargo.toml").write_text((self.repo / "Cargo.toml").read_text() + '\n[workspace]\nmembers=[".", "beta"]\n')
        (self.repo / "beta/src").mkdir(parents=True)
        _ = (self.repo / "beta/Cargo.toml").write_text('[package]\nname="beta"\nversion="0.1.0"\nedition="2021"\n')
        _ = (self.repo / "beta/src/lib.rs").write_bytes((self.repo / "src/lib.rs").read_bytes())
        _ = self.git("add", ".")
        _ = self.git("commit", "-qm", "workspace fixture")
        self.base = self.git("rev-parse", "HEAD")
        self.tree = self.git("rev-parse", "HEAD^{tree}")
        self.runner = FakeRunner()
        self.server = ScriptedServer(lambda text, offset, new: word_edits(text, "old", new))

    def test_metadata_only_rule_edit_selects_approved_findings_but_body_edit_is_stale(self) -> None:
        ledger, definition, findings = self.setup_findings()
        self.addCleanup(ledger.close)
        _ = subprocess.run(["git", "init", "-q", str(definition.path.parent.parent)], check=True)
        _ = subprocess.run(["git", "-C", str(definition.path.parent.parent), "hash-object", "-w", str(definition.path)],
                           check=True, capture_output=True)
        text = definition.path.read_text()
        _ = definition.path.write_text(text.replace("---\n", "---\napproval: always\n", 1))
        current = next(rule for rule in rules.load_rules(self.repo) if rule.rule_id == definition.rule_id)
        self.assertEqual({f.finding_id for f in run.select(ledger, current).findings}, {f.finding_id for f in findings})
        _ = definition.path.write_text(definition.path.read_text() + "Changed requirement\n")
        changed = next(rule for rule in rules.load_rules(self.repo) if rule.rule_id == definition.rule_id)
        selected = run.select(ledger, changed)
        self.assertFalse(selected.findings)
        self.assertEqual({item.reason for item in selected.skipped}, {"rule changed since judged"})

    def setup_findings(self) -> tuple[ledger_api.Ledger, rules.Rule, tuple[ledger_api.HeldFinding, ...]]:
        ledger = self.ledger()
        ledger.set_tip(self.base)
        definition = next(rule for rule in rules.load_rules(self.repo) if rule.name == "prefer-type-named-fields-and-bindings")
        paths = ["src/lib.rs", "beta/src/lib.rs"]
        ledger.ticket_add("read-1", "read", "read", "names", ".", paths, [definition.rule_id])
        ledger.ticket_assign("read-1", "main")
        _ = ledger.ticket_base("read-1")
        findings: list[ledger_api.HeldFinding] = []
        for index, path in enumerate(paths):
            issue = self.root / f"decision-{index}.md"
            _ = issue.write_text('---\nunit: names-unit\nplan: /tmp/standing-names.md\nstatus: open\n---\nApproved rename\n')
            finding = ledger.hold(definition.rule_id, path, 2, (self.repo / path).read_text().splitlines()[1],
                                  "rename", "old", "number", definition.blob, "read-1")
            ledger.file_held(finding.finding_id, issue)
            findings.extend(ledger.named_held((finding.finding_id,)))
        ledger.ticket_drop("read-1")
        return ledger, definition, tuple(findings)

    def execute(self, ledger: ledger_api.Ledger, definition: rules.Rule) -> dict[str, object]:
        path = run.run_rule(self.repo, definition.rule_id, self.tree, ledger, self.server, self.runner,
                            available=lambda repo, path: None)
        return bulk.object_fields(cast(object, json.loads(path.read_text())))

    def test_identical_declaration_line_is_eligible_before_editing(self) -> None:
        ledger, _, findings = self.setup_findings()
        for finding in findings:
            evidence = ledger.finding_site(finding, self.base)
            judged = ledger.judged_commit(finding)
            self.assertIsInstance(evidence, ledger_api.UnchangedSite, repr((evidence, judged)))
            self.assertIsInstance(run.current_rename(ledger, finding, self.base, run.RenameSources()), bulk.RenameFinding,
                                  repr((evidence, judged, finding)))

    def test_field_refactor_runs_through_lineage_and_post_format_guard(self) -> None:
        source = "struct Record {\n old: usize,\n}\nfn f(r: Record) -> usize { r.old }\n"
        for path in ("src/lib.rs", "beta/src/lib.rs"):
            _ = (self.repo / path).write_text(source)
        _ = self.git("add", ".")
        _ = self.git("commit", "-qm", "private field fixture")
        self.base = self.git("rev-parse", "HEAD")
        self.tree = self.git("rev-parse", "HEAD^{tree}")
        ledger, definition, findings = self.setup_findings()
        for finding in findings:
            self.assertIsInstance(run.current_rename(ledger, finding, self.base, run.RenameSources()), bulk.RenameFinding)
        report = self.execute(ledger, definition)
        self.assertEqual(report["applied"], 2)
        self.assertEqual(report["refused"], 0)
        self.assertIn("r.number", (self.repo / "src/lib.rs").read_text())

    def test_workspace_selection_crosses_crates_and_skips_built_withdrawn_and_held_with_reasons(self) -> None:
        ledger, definition, findings = self.setup_findings()
        selected = run.select(ledger, definition)
        self.assertEqual({f.path for f in selected.findings}, {"src/lib.rs", "beta/src/lib.rs"})
        first, second = findings
        ledger.ticket_add("other-1", "other", "proposal", "names", "beta", [second.path], [second.rule],
                          proposal=str(cast(ledger_api.Filed, second.state).proposal), finding_ids=(second.finding_id,))
        # Queued tickets own findings too, before any branch is assigned.
        selected = run.select(ledger, definition)
        self.assertEqual([f.finding_id for f in selected.findings], [first.finding_id])
        self.assertEqual([(f.finding_id, f.reason) for f in selected.skipped], [(second.finding_id, "held by another open ticket")])
        ledger.ticket_drop("other-1")
        with closing(sqlite3.connect(ledger.database)) as connection:
            _ = connection.execute("UPDATE held_findings SET state='built' WHERE finding_id=?", (first.finding_id,))
            _ = connection.execute("UPDATE held_findings SET state='withdrawn' WHERE finding_id=?", (second.finding_id,))
            connection.commit()
        self.assertEqual({f.reason for f in run.select(ledger, definition).skipped}, {"built", "withdrawn"})

    def test_selection_includes_always_approved_but_never_overrides_a_decline(self) -> None:
        ledger, definition, findings = self.setup_findings()
        for finding in findings:
            _ = cast(ledger_api.Filed, finding.state).proposal.write_text('---\nstatus: open\n---\nAwaiting answer\n')
        self.assertFalse(run.select(ledger, definition).findings)
        automatic = replace(definition, approval=rules.AlwaysApproved("names"))
        self.assertEqual(len(run.select(ledger, automatic).findings), 2)
        _ = cast(ledger_api.Filed, findings[1].state).proposal.write_text('---\nstyle_answer: declined\n---\nNo\n')
        self.assertEqual(len(run.select(ledger, automatic).findings), 1)

    def test_rejudge_cut_refuses_changed_declaration_bytes_but_applies_identical_line(self) -> None:
        ledger, definition, findings = self.setup_findings()
        path = self.repo / "src/lib.rs"
        _ = path.write_text(path.read_text().replace("let old = 1", "let old = 2"))
        _ = self.git("add", ".")
        _ = self.git("commit", "-qm", "change one declaration")
        self.tree = self.git("rev-parse", "HEAD^{tree}")
        ledger.set_tip(self.git("rev-parse", "HEAD"))
        report = self.execute(ledger, definition)
        self.assertEqual(report["refusals"], {findings[0].finding_id: "changed since judged"})
        self.assertEqual(report["applied_ids"], [findings[1].finding_id])
        self.assertIn("let old = 2", path.read_text())
        self.assertIn("let number", (self.repo / "beta/src/lib.rs").read_text())

    def test_base_tree_mismatch_and_dirty_index_refuse_before_selection_or_commands(self) -> None:
        ledger, definition, _ = self.setup_findings()
        with self.assertRaisesRegex(bulk.BulkRenameError, "base-tree mismatch"):
            _ = run.run_rule(self.repo, definition.rule_id, "0" * 40, ledger, self.server, self.runner)
        _ = (self.repo / "src/lib.rs").write_text("changed")
        _ = self.git("add", "src/lib.rs")
        with self.assertRaisesRegex(bulk.BulkRenameError, "clean tracked"):
            _ = run.run_rule(self.repo, definition.rule_id, self.tree, ledger, self.server, self.runner)
        self.assertFalse(self.runner.calls)
        self.assertFalse(tuple(ticket for ticket in ledger.proposal_tickets() if ticket["state"] not in ledger_api.FINAL_TICKET_STATES))

    def test_workspace_validation_runs_once_and_one_clean_test_commit_merges_all_decisions(self) -> None:
        ledger, definition, findings = self.setup_findings()
        report = self.execute(ledger, definition)
        commands = [args for _, args in self.runner.calls]
        self.assertEqual([args[1] for args in commands], ["+nightly", "check", "clippy", str(Path.home() / ".claude/scripts/direct/verify.sh"),
                                                                          str(Path.home() / ".claude/scripts/direct/verify.sh"),
                                                                          str(Path.home() / ".claude/scripts/direct/verify.sh")])
        self.assertEqual(commands[0], ("cargo", "+nightly", "fmt", "--package", "beta", "--package", "bulk_sample"))
        self.assertIn("--all-targets", commands[1])
        self.assertIn("--message-format=json", commands[2])
        verify = str(Path.home() / ".claude/scripts/direct/verify.sh")
        self.assertEqual(commands[3:], [("bash", verify, "final"), ("bash", verify, "lint"),
                                       ("bash", verify, "final-pass", str(report["tested_tree"]))])
        self.assertTrue(all(folder == self.runner.test_checkout for folder, args in self.runner.calls if args[0] == "bash"))
        self.assertNotEqual(self.runner.test_checkout, self.repo)
        self.assertFalse(self.runner.test_checkout.exists())
        self.assertEqual(self.runner.test_tree, report["tested_tree"])
        self.assertEqual(self.git("rev-list", "--count", f"{self.base}..HEAD"), "1")
        self.assertIn(f"Tested-Tree: {report['tested_tree']}", self.git("log", "-1", "--format=%B"))
        self.assertEqual(self.git("status", "--porcelain"), "")
        ledger.set_tip(self.git("rev-parse", "HEAD"), "main")
        for ticket in cast(list[str], report["tickets"]):
            self.assertEqual(ledger.ticket_merge(ticket), ())
        self.assertTrue(all(isinstance(finding.state, ledger_api.Built) for finding in ledger.named_held(f.finding_id for f in findings)))

    def test_distinct_decisions_on_one_file_all_build_through_normal_ticket_merge(self) -> None:
        path = self.repo / "src/lib.rs"
        _ = path.write_text(path.read_text() + "fn extra() {\n    let other = 2;\n    let _ = other + 1;\n}\n")
        _ = self.git("add", ".")
        _ = self.git("commit", "-qm", "second binding fixture")
        self.base = self.git("rev-parse", "HEAD")
        self.tree = self.git("rev-parse", "HEAD^{tree}")
        ledger, definition, findings = self.setup_findings()
        ledger.ticket_add("read-extra-1", "read-extra", "read", "names", ".", ["src/lib.rs"], [definition.rule_id])
        ledger.ticket_assign("read-extra-1", "main")
        _ = ledger.ticket_base("read-extra-1")
        extra = ledger.hold(definition.rule_id, "src/lib.rs", 6, path.read_text().splitlines()[5],
                            "rename", "other", "size", definition.blob, "read-extra-1")
        proposal = self.root / "decision-extra.md"
        _ = proposal.write_text('---\nunit: names-unit\nplan: /tmp/standing-names.md\nstatus: open\n---\nApproved extra rename\n')
        ledger.file_held(extra.finding_id, proposal)
        ledger.ticket_drop("read-extra-1")
        self.server = ScriptedServer(lambda text, offset, new: word_edits(text, "old" if new == "number" else "other", new))
        available = mock.Mock()
        command = subprocess.run
        with mock.patch.object(subprocess, "run", wraps=command) as calls:
            path_report = run.run_rule(self.repo, definition.rule_id, self.tree, ledger, self.server, self.runner,
                                       available=available)
        report = bulk.object_fields(cast(object, json.loads(path_report.read_text())))
        self.assertEqual(available.call_count, 4)  # Selection, then every WorkspaceEdit document before writing.
        self.assertEqual({call.args[1] for call in available.call_args_list}, {"src/lib.rs", "beta/src/lib.rs"})
        judged_reads = [call.args[0] for call in calls.call_args_list
                        if call.args[0][:2] == ["git", "show"]]
        self.assertEqual(len(judged_reads), 2, repr(judged_reads))
        self.assertEqual(report["applied"], 3)
        ledger.set_tip(self.git("rev-parse", "HEAD"), "main")
        for ticket in cast(list[str], report["tickets"]):
            self.assertEqual(ledger.ticket_merge(ticket), ())
        self.assertTrue(all(isinstance(finding.state, ledger_api.Built)
                            for finding in ledger.named_held([*(f.finding_id for f in findings), extra.finding_id])))

    def test_checkpoint_publication_failure_preserves_tested_commit_and_recovers(self) -> None:
        ledger, definition, findings = self.setup_findings()
        checkpoint = ledger.ticket_checkpoint
        count = 0

        def interrupted(id: str, commit: str) -> None:
            nonlocal count
            count += 1
            if count == 2:
                raise ledger_api.LedgerError("checkpoint write interrupted")
            checkpoint(id, commit)

        with mock.patch.object(ledger, "ticket_checkpoint", side_effect=interrupted):
            report = self.execute(ledger, definition)
        self.assertIn("publication pending:", str(report["status"]))
        self.assertEqual(report["applied"], 2)
        self.assertEqual(self.git("rev-parse", "HEAD"), report["commit"])
        self.assertEqual(self.git("rev-parse", "HEAD^{tree}"), report["tested_tree"])
        tickets = cast(list[str], report["tickets"])
        for id in tickets:
            ledger.ticket_checkpoint(id, str(report["commit"]))
        ledger.bulk_share_checkpoint(tickets)
        ledger.set_tip(str(report["commit"]), "main")
        for id in tickets:
            self.assertEqual(ledger.ticket_merge(id), ())
        self.assertTrue(all(isinstance(f.state, ledger_api.Built)
                            for f in ledger.named_held(f.finding_id for f in findings)))

    def test_json_error_reverts_only_its_file_then_checks_workspace_again(self) -> None:
        ledger, definition, findings = self.setup_findings()
        self.runner.error_file = "beta/src/lib.rs"
        report = self.execute(ledger, definition)
        self.assertEqual((report["applied"], report["reverted"]), (1, 1))
        self.assertEqual(report["reversions"], {findings[1].finding_id: "workspace diagnostics"})
        self.assertIn("let old", (self.repo / "beta/src/lib.rs").read_text())
        self.assertIn("let number", (self.repo / "src/lib.rs").read_text())
        self.assertEqual(sum(args[1] == "check" for _, args in self.runner.calls), 2)
        self.assertEqual(sum(args[1] == "clippy" for _, args in self.runner.calls), 2)
        self.assertEqual(sum(args[0] == "bash" and args[2] == "final" for _, args in self.runner.calls), 1)
        self.assertEqual(len(cast(list[object], report["validation_loops"])), 2)

    def test_diagnostic_reader_reverts_the_edits_it_reads_transitively(self) -> None:
        files = tuple(bulk.rename_file(self.server, self.repo / path, path, (self.repo / path).read_text(),
                                      (bulk.RenameFinding(path, path, self.finding((self.repo / path).read_text()).rename),))
                      for path in ("src/lib.rs", "beta/src/lib.rs"))
        workspace = run.WorkspaceEdits(self.repo, self.base, files)
        for file in files:
            _ = (self.repo / file.path).write_text(file.edited)
        workspace.dependencies["src/lib.rs"] = frozenset(("src/lib.rs", "beta/src/lib.rs"))
        self.runner.error_file = "src/lib.rs"
        kept, reverted = run.validate(workspace, self.runner, {"check_clippy": 0.0}, [])
        self.assertFalse(kept)
        self.assertEqual(set(reverted), {"src/lib.rs", "beta/src/lib.rs"})
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(sum(args[1] == "check" for _, args in self.runner.calls), 2)

    def test_no_progress_aborts_whole_batch_and_releases_every_finding(self) -> None:
        ledger, definition, findings = self.setup_findings()
        self.runner.no_progress = True
        report = self.execute(ledger, definition)
        self.assertIn("no new edited file", str(report["status"]))
        self.assertEqual((report["applied"], report["reverted"]), (0, 2))
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertFalse(tuple(ticket for ticket in ledger.proposal_tickets() if ticket["state"] not in ledger_api.FINAL_TICKET_STATES))
        self.assertTrue(all(isinstance(f.build_ticket, ledger_api.NoBuildTicket) for f in ledger.named_held(f.finding_id for f in findings)))
        self.assertFalse(any(args[0] == "bash" for _, args in self.runner.calls))

    def test_red_workspace_tests_drop_commit_and_release_all_kept_findings_with_failure(self) -> None:
        ledger, definition, findings = self.setup_findings()
        self.runner.red_tests = True
        report = self.execute(ledger, definition)
        self.assertEqual((report["applied"], report["reverted"], report["commit"]), (0, 2, ""))
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertFalse(tuple(ticket for ticket in ledger.proposal_tickets() if ticket["state"] not in ledger_api.FINAL_TICKET_STATES))
        for ticket in cast(list[str], report["tickets"]):
            self.assertTrue(all(row.reason == "tests failed: FAIL [0.01s] bulk_sample::tests::broken" for row in ledger.bulk_refusals(ticket)))
        self.assertTrue(all(isinstance(f.build_ticket, ledger_api.NoBuildTicket) for f in ledger.named_held(f.finding_id for f in findings)))
        self.assertEqual(sum(args[0] == "bash" and args[2] == "final" for _, args in self.runner.calls), 1)
        self.assertFalse(list((ledger.database.parent / "bulk_review").glob("*.tested.json")))

    def test_report_has_reason_family_crate_counts_stages_and_tested_tree_evidence(self) -> None:
        ledger, definition, _ = self.setup_findings()
        report = self.execute(ledger, definition)
        self.assertEqual(report["applied"], 2)
        self.assertEqual({row["crate"] for row in cast(list[dict[str, object]], report["counts"])}, {".", "beta"})
        self.assertEqual({row["family"] for row in cast(list[dict[str, object]], report["counts"])}, {"names"})
        times = cast(dict[str, float], report["seconds"])
        self.assertEqual(set(times), {"select", "rename", "fmt", "check_clippy", "commit", "tests", "verify_final", "verify_lint", "final_pass"})
        self.assertTrue(all(value >= 0 for value in times.values()))
        self.assertEqual(report["tested_tree"], self.git("rev-parse", "HEAD^{tree}"))
        self.assertFalse(list((ledger.database.parent / "bulk_review").glob("*.tested.json")))
        self.assertEqual(self.runner.test_tree, report["tested_tree"])
        self.assertEqual(len(cast(list[object], report["sample"])), 2)
        self.assertEqual(report["summary"], run.summary(report))

    def test_test_gate_refuses_untracked_files_and_a_different_tree_before_running(self) -> None:
        _ = (self.repo / "untracked.txt").write_text("untracked test input")
        with self.assertRaisesRegex(bulk.BulkRenameError, "clean checkout"):
            _ = run.tested_tree_proof(self.repo, self.tree, self.runner, {})
        (self.repo / "untracked.txt").unlink()
        with self.assertRaisesRegex(bulk.BulkRenameError, "tested tree"):
            _ = run.tested_tree_proof(self.repo, "0" * 40, self.runner, {})
        self.assertFalse(self.runner.calls)

    def test_unrelated_formatter_changes_and_failed_post_format_proofs_abort_and_restore(self) -> None:
        ledger, definition, _ = self.setup_findings()
        self.runner.unrelated = True
        report = self.execute(ledger, definition)
        self.assertIn("unrelated file", str(report["status"]))
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.runner.unrelated = False
        self.runner.break_proof = True
        report = self.execute(ledger, definition)
        self.assertIn("post-format rename edit changed", str(report["status"]))
        self.assertEqual(self.git("status", "--porcelain"), "")


    def test_verify_final_and_lint_both_must_pass_and_have_separate_times(self) -> None:
        for failed in ("", "lint"):
            with self.subTest(failed=failed):
                self.runner = FakeRunner()
                self.runner.fail_stage = failed
                times: dict[str, float] = {}
                result = run.tested_tree_proof(self.repo, self.tree, self.runner, times)
                stages = [args[2] for _, args in self.runner.calls]
                self.assertEqual(stages, ["final", "lint", "final-pass"] if not failed else ["final", "lint"])
                self.assertEqual(result.code, 0 if not failed else 1)
                self.assertTrue(all(folder == self.repo and args[:2] == ("bash", str(Path.home() / ".claude/scripts/direct/verify.sh"))
                                    for folder, args in self.runner.calls))
                self.assertIn("verify_final", times)
                self.assertIn("verify_lint", times)
                self.assertTrue(all(value >= 0 for value in times.values()))

    def assert_pass_record_refuses_batch(self, other_tree: bool) -> None:
        ledger, definition, findings = self.setup_findings()
        self.runner.fail_stage = "" if other_tree else "final-pass"
        self.runner.other_tree_record = other_tree
        report = self.execute(ledger, definition)
        self.assertEqual((report["applied"], report["reverted"], report["commit"]), (0, 2, ""))
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git("status", "--porcelain"), "")
        query = self.runner.calls[-1]
        self.assertEqual(query[1][-2:], ("final-pass", str(report["tested_tree"])))
        self.assertNotEqual(query[0], self.repo)
        self.assertFalse(tuple(ticket for ticket in ledger.proposal_tickets() if ticket["state"] not in ledger_api.FINAL_TICKET_STATES))
        self.assertTrue(all(isinstance(f.build_ticket, ledger_api.NoBuildTicket)
                            for f in ledger.named_held(f.finding_id for f in findings)))

    def test_missing_pass_record_reverts_and_releases_the_batch(self) -> None:
        self.assert_pass_record_refuses_batch(False)

    def test_other_tree_pass_record_reverts_and_releases_the_batch(self) -> None:
        self.assert_pass_record_refuses_batch(True)

    def test_tested_tree_proof_rejects_drift_after_each_command(self) -> None:
        for stage in ("final", "lint", "final-pass"):
            with self.subTest(stage=stage):
                self.runner = FakeRunner()
                self.runner.mutate_stage = stage
                with self.assertRaisesRegex(bulk.BulkRenameError, "clean checkout"):
                    _ = run.test_tree(self.repo, self.base, self.tree, self.runner, {})
                self.assertFalse(self.runner.test_checkout.exists())
                self.assertEqual(self.runner.calls[-1][1][2], stage)

    def test_hidden_untracked_test_output_never_publishes_or_keeps_the_batch_commit(self) -> None:
        ledger, definition, findings = self.setup_findings()
        self.runner.mutate_stage = "final"

        def hide_untracked_output(repo: Path, arguments: Sequence[str]) -> run.CommandResult:
            if tuple(arguments)[-1:] == ("final",):
                _ = run.git(repo, "config", "status.showUntrackedFiles", "no")
            result = self.runner(repo, arguments)
            if tuple(arguments)[-1:] == ("final",):
                self.assertTrue((repo / "unexpected.txt").exists())
                self.assertEqual(run.git(repo, "status", "--porcelain"), "")
            return result

        path = run.run_rule(self.repo, definition.rule_id, self.tree, ledger, self.server,
                            hide_untracked_output, available=lambda repo, path: None)
        report = bulk.object_fields(cast(object, json.loads(path.read_text())))
        self.assertEqual((report["applied"], report["reverted"], report["commit"]), (0, 2, ""))
        self.assertIn("clean checkout", str(report["status"]))
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git("status", "--porcelain=v1", "--untracked-files=all"), "")
        self.assertEqual([args[2] for _, args in self.runner.calls if args[0] == "bash"], ["final"])
        self.assertFalse(self.runner.test_checkout.exists())
        self.assertFalse(tuple(ticket for ticket in ledger.proposal_tickets()
                               if ticket["state"] not in ledger_api.FINAL_TICKET_STATES))
        self.assertTrue(all(isinstance(f.build_ticket, ledger_api.NoBuildTicket)
                            for f in ledger.named_held(f.finding_id for f in findings)))


if __name__ == "__main__":
    _ = unittest.main()
