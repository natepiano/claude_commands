"""Workspace bulk runs use temporary Git repos and a fake command runner; never build Rust."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import unittest
from io import StringIO
from unittest import mock
from collections import Counter
from collections.abc import Iterable, Sequence
from contextlib import closing, redirect_stdout
from dataclasses import replace
from pathlib import Path
from typing import cast, override

import bulk_run as run
import bulk_rename as bulk
import rules
import style_ledger as ledger_api
from approved_renames import BindingRename, NoBindingScope, rename_declaration
from test_bulk_rename import CargoProjectTests, ScriptedServer, word_edits, stop_after_edit, stop_process


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

    def test_unnormalizable_shorthand_refusal_keeps_independent_plan_before_any_write(self) -> None:
        source = 'pub fn run() {\n    let old = 1;\n    consume(Outcome::Honored { old });\n}\n'
        _ = (self.repo / 'src/lib.rs').write_text(source)
        _ = self.git('add', '.')
        _ = self.git('commit', '-qm', 'candidate shorthand fixture')
        self.base, self.tree = self.git('rev-parse', 'HEAD'), self.git('rev-parse', 'HEAD^{tree}')
        ledger, _definition, findings = self.setup_findings()
        self.addCleanup(ledger.close)
        original_beta = (self.repo / 'beta/src/lib.rs').read_text()
        def edits(text: str, _offset: int, new: str) -> tuple[bulk.SourceEdit, ...]:
            return tuple(replace(edit, text=new + ' /* analyzer fragment */')
                         if text[edit.end:].startswith(' });') else edit
                         for edit in word_edits(text, 'old', new))
        server = ScriptedServer(edits)
        plan = run.plan_renames(self.repo, ledger, self.base, run.Selection(findings, ()), server,
                               run.PlanningFileAvailability(self.repo, self.available))
        self.assertEqual([item.finding_id for item in plan.refusals], [findings[0].finding_id])
        self.assertIn('record shorthand', plan.refusals[0].reason)
        self.assertEqual([file.path for file in plan.files], ['beta/src/lib.rs'])
        self.assertIn('let number', plan.files[0].edited)
        self.assertEqual((self.repo / 'src/lib.rs').read_text(), source)
        self.assertEqual((self.repo / 'beta/src/lib.rs').read_text(), original_beta)

    def test_recovery_preserves_another_users_ticket_after_plan_record(self) -> None:
        ledger, definition, findings = self.setup_findings()
        self.addCleanup(ledger.close)
        begin = bulk.BatchIntent.begin

        def stopped(intent: bulk.BatchIntent, files: tuple[bulk.FileRenames, ...],
                    tickets: Sequence[str] = (), identities: Sequence[str] = ()) -> None:
            begin(intent, files, tickets, identities)
            os._exit(73)

        # Lose the process after the durable plan, before any ticket claim.
        with mock.patch.object(bulk.BatchIntent, "begin", stopped):
            stop_process(lambda: self.execute(ledger, definition))
        intent_path = Path(self.git("rev-parse", "--path-format=absolute", "--git-path", "style-batch-intent.json"))
        record = bulk.object_fields(cast(object, json.loads(intent_path.read_text())))
        planned = cast(list[str], record["tickets"])
        ticket = planned[0]
        finding = findings[0]
        self.assertIsInstance(finding.state, ledger_api.Filed)
        assert isinstance(finding.state, ledger_api.Filed)
        # An independent ledger connection takes the still-unreserved ID.
        other = ledger_api.Ledger(self.repo, ledger.database)
        self.addCleanup(other.close)
        other.ticket_add(ticket, ticket.rsplit("-", 1)[0], "proposal", "names", ".",
                         [finding.path], [finding.rule], proposal=str(finding.state.proposal),
                         finding_ids=(finding.finding_id,))
        before = next(item for item in other.proposal_tickets() if item["id"] == ticket)
        output = StringIO()
        with redirect_stdout(output):
            _ = self.execute(ledger, definition)
        after = next(item for item in other.proposal_tickets() if item["id"] == ticket)
        self.assertEqual(after, before)
        self.assertEqual(other.named_held((finding.finding_id,))[0].build_ticket, ledger_api.BuildTicket(ticket))
        self.assertIn(ticket, output.getvalue())

    def test_recovery_names_another_users_read_ticket_after_plan_record(self) -> None:
        ledger, definition, findings = self.setup_findings()
        self.addCleanup(ledger.close)
        begin = bulk.BatchIntent.begin

        def stopped(intent: bulk.BatchIntent, files: tuple[bulk.FileRenames, ...],
                    tickets: Sequence[str] = (), identities: Sequence[str] = ()) -> None:
            begin(intent, files, tickets, identities)
            os._exit(73)

        with mock.patch.object(bulk.BatchIntent, "begin", stopped):
            stop_process(lambda: self.execute(ledger, definition))
        intent_path = Path(self.git("rev-parse", "--path-format=absolute", "--git-path", "style-batch-intent.json"))
        record = bulk.object_fields(cast(object, json.loads(intent_path.read_text())))
        ticket = cast(list[str], record["tickets"])[0]
        unit = ticket.rsplit("-", 1)[0]
        with ledger_api.Ledger(self.repo, ledger.database) as other:
            other.ticket_add(ticket, unit, "read", "names", ".", [findings[0].path], [definition.rule_id])
            before = other.queued_tickets(unit)[0]
            output = StringIO()
            with redirect_stdout(output):
                _ = self.execute(ledger, definition)
            self.assertEqual(other.queued_tickets(unit)[0], before)
            self.assertIn(ticket, output.getvalue())

    def test_workspace_recovers_claim_committed_before_analysis(self) -> None:
        ledger, definition, findings = self.setup_findings()
        self.addCleanup(ledger.close)
        add = ledger_api.Ledger.ticket_add

        def stopped(instance: ledger_api.Ledger, ticket: str, unit: str, kind: ledger_api.TicketKind,
                    family: str, crate: str, files: list[str], rule_ids: list[str], *,
                    proposal: str, finding_ids: tuple[str, ...],
                    batch_owner: ledger_api.BatchOwner | ledger_api.UnbatchedTicket) -> None:
            # The fault is after ticket_add's commit, before claim() can record its return.
            add(instance, ticket, unit, kind, family, crate, files, rule_ids,
                proposal=proposal, finding_ids=finding_ids, batch_owner=batch_owner)
            os._exit(73)

        with mock.patch.object(ledger_api.Ledger, "ticket_add", stopped):
            stop_process(lambda: self.execute(ledger, definition))
        interrupted = {ticket["id"] for ticket in ledger.proposal_tickets()
                       if ticket["state"] not in ledger_api.FINAL_TICKET_STATES}
        self.assertTrue(interrupted)
        report = self.execute(ledger, definition)
        self.assertEqual(report["applied"], len(findings))
        self.assertTrue(all(ticket["state"] in ledger_api.FINAL_TICKET_STATES
                            for ticket in ledger.proposal_tickets() if ticket["id"] in interrupted))
        self.assertTrue(all(ticket["checkpoint"] for ticket in ledger.proposal_tickets()
                            if ticket["state"] not in ledger_api.FINAL_TICKET_STATES))

    def test_workspace_reruns_after_process_loss_on_first_edit(self) -> None:
        ledger, definition, findings = self.setup_findings()
        self.addCleanup(ledger.close)
        stop_after_edit(self.repo / "src/lib.rs", lambda: self.execute(ledger, definition))
        self.assertTrue(ledger.proposal_tickets())
        report = self.execute(ledger, definition)
        self.assertEqual(report["applied"], len(findings))
        self.assertEqual(report["refused"], 0)
        self.assertEqual(report["reverted"], 0)
        self.assertTrue(all(ticket["checkpoint"] for ticket in ledger.proposal_tickets()
                            if ticket["id"] in cast(list[str], report["tickets"])))

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

    def test_doc_parameter_and_derive_field_share_one_tested_rename_commit(self) -> None:
        source = '/// Add `old`.\nfn run(old: usize) -> usize { old + 1 }\n'
        derived = '#[derive(serde::Serialize)]\nstruct R { old: usize }\n'
        _ = (self.repo / 'src/lib.rs').write_text(source)
        _ = (self.repo / 'beta/src/lib.rs').write_text(derived)
        _ = (self.repo / 'README.md').write_text('run takes `old`.\n')
        _ = self.git('add', '.')
        _ = self.git('commit', '-qm', 'documented and derived fixture')
        self.base, self.tree = self.git('rev-parse', 'HEAD'), self.git('rev-parse', 'HEAD^{tree}')
        ledger, definition, _ = self.setup_findings()
        self.addCleanup(ledger.close)
        self.server = self.derive_server(lambda text, offset, new: word_edits(text, 'old', new))
        report = self.execute(ledger, definition)
        self.assertEqual((report['applied'], report['refused'], report['reverted']), (2, 0, 0))
        self.assertEqual((self.repo / 'src/lib.rs').read_text(), source.replace('old', 'number'))
        self.assertIn('#[serde(rename = "old")] number:', (self.repo / 'beta/src/lib.rs').read_text())
        self.assertEqual((self.repo / 'README.md').read_text(), 'run takes `old`.\n')
        self.assertEqual(self.git('rev-list', '--count', f'{self.base}..HEAD'), '1')
        self.assertEqual([args[2] for _, args in self.runner.calls if args[0] == 'bash'], ['final', 'lint', 'final-pass'])

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

    def available(self, _repo: Path, _path: str) -> None:
        pass

    def execute(self, ledger: ledger_api.Ledger, definition: rules.Rule) -> dict[str, object]:
        path = run.run_rule(self.repo, definition.rule_id, self.tree, ledger, self.server, self.runner,
                            available=self.available)
        return bulk.object_fields(cast(object, json.loads(path.read_text())))

    def approve_extra_rename(self, ledger: ledger_api.Ledger, definition: rules.Rule,
                             line: int, old: str, new: str, path: str = 'src/lib.rs') -> ledger_api.HeldFinding:
        ledger.ticket_add('read-extra-1', 'read-extra', 'read', 'names', '.', [path], [definition.rule_id])
        ledger.ticket_assign('read-extra-1', 'main')
        _ = ledger.ticket_base('read-extra-1')
        finding = ledger.hold(definition.rule_id, path, line,
                              (self.repo / path).read_text().splitlines()[line - 1],
                              'rename', old, new, definition.blob, 'read-extra-1')
        proposal = self.root / 'decision-extra.md'
        _ = proposal.write_text('---\nunit: names-unit\nplan: /tmp/standing-names.md\nstatus: open\n---\nApproved extra rename\n')
        ledger.file_held(finding.finding_id, proposal)
        ledger.ticket_drop('read-extra-1')
        return finding

    def setup_field_reference(self, tail: str = '') -> tuple[ledger_api.Ledger, rules.Rule, tuple[ledger_api.HeldFinding, ...]]:
        source = 'struct Record {\n    old: usize,\n}\nmod child;\n' + tail
        child = self.repo / 'src/child.rs'
        child_source = 'fn read(r: super::Record) -> usize { r.old }\n'
        _ = (self.repo / 'src/lib.rs').write_text(source)
        _ = child.write_text(child_source)
        _ = self.git('add', '.')
        _ = self.git('commit', '-qm', 'field references in another module')
        self.base = self.git('rev-parse', 'HEAD')
        self.tree = self.git('rev-parse', 'HEAD^{tree}')
        ledger, definition, findings = self.setup_findings()
        rename = self.server.rename
        definitions = self.server.definitions

        def references(path: Path, text: str, offset: int, new: str) -> bulk.WorkspaceRename:
            result = rename(path, text, offset, new)
            if path == self.repo / 'src/lib.rs' and new == 'number':
                return bulk.WorkspaceRename((*result.documents, bulk.DocumentEdits(
                    child, child_source, word_edits(child_source, 'old', new))))
            return result

        def resolve(path: Path, text: str, offsets: tuple[int, ...]) -> tuple[bulk.DefinitionResolution, ...]:
            if path == child:
                target = bulk.DefinitionTarget((self.repo / 'src/lib.rs').as_uri(), (1, 4), (1, 10))
                return tuple(target for _ in offsets)
            return definitions(path, text, offsets)

        _ = self.enterContext(mock.patch.object(self.server, 'rename', side_effect=references))
        _ = self.enterContext(mock.patch.object(self.server, 'definitions', side_effect=resolve))
        return ledger, definition, findings

    def test_names_held_reference_refuses_only_its_edit_group(self) -> None:
        ledger, definition, findings = self.setup_field_reference()
        source = (self.repo / 'src/lib.rs').read_text()
        child = self.repo / 'src/child.rs'
        child_source = child.read_text()
        checked: Counter[str] = Counter()
        reason = 'src/child.rs: another worktree holds the file'

        def available(_repo: Path, path: str) -> None:
            checked[path] += 1
            if path == 'src/child.rs':
                raise bulk.BulkRenameError(reason)

        with mock.patch.object(self, 'available', side_effect=available):
            report = self.execute(ledger, definition)
        self.assertEqual((report['applied'], report['refused'], report['reverted']), (1, 1, 0))
        self.assertEqual(report['refusals'], {findings[0].finding_id: reason})
        self.assertEqual(checked, {'src/lib.rs': 1, 'src/child.rs': 1, 'beta/src/lib.rs': 2})
        self.assertEqual((self.repo / 'src/lib.rs').read_text(), source)
        self.assertEqual(child.read_text(), child_source)
        self.assertIn('let number', (self.repo / 'beta/src/lib.rs').read_text())
        self.assertEqual(self.git('rev-list', '--count', f'{self.base}..HEAD'), '1')
        self.assertEqual([args[2] for _, args in self.runner.calls if args[0] == 'bash'], ['final', 'lint', 'final-pass'])
        self.assertIsInstance(ledger.named_held((findings[0].finding_id,))[0].state, ledger_api.Filed)

    def test_held_field_reference_preserves_an_independent_local_rename_in_the_same_file(self) -> None:
        ledger, definition, findings = self.setup_field_reference('fn extra() { let other = 2; consume(other); }\n')
        extra = self.approve_extra_rename(ledger, definition, 5, 'other', 'size')
        self.server.edit = lambda text, offset, new: word_edits(text, 'other' if new == 'size' else 'old', new)
        child = self.repo / 'src/child.rs'
        original = (self.repo / 'src/lib.rs').read_text()
        child_source = child.read_text()
        checked: Counter[str] = Counter()
        reason = 'src/child.rs: another worktree holds the file'

        def available(_repo: Path, path: str) -> None:
            checked[path] += 1
            if path == 'src/child.rs':
                raise bulk.BulkRenameError(reason)

        with mock.patch.object(self, 'available', side_effect=available):
            report = self.execute(ledger, definition)
        self.assertEqual((report['applied'], report['refused'], report['reverted']), (2, 1, 0))
        self.assertEqual(report['refusals'], {findings[0].finding_id: reason})
        self.assertEqual((self.repo / 'src/lib.rs').read_text(), original.replace('other', 'size'))
        self.assertEqual(child.read_text(), child_source)
        self.assertEqual(checked, {'src/lib.rs': 2, 'src/child.rs': 1, 'beta/src/lib.rs': 2})
        self.assertEqual(self.git('rev-list', '--count', f'{self.base}..HEAD'), '1')
        self.assertEqual([args[2] for _, args in self.runner.calls if args[0] == 'bash'], ['final', 'lint', 'final-pass'])
        self.assertIsInstance(ledger.named_held((findings[0].finding_id,))[0].state, ledger_api.Filed)
        self.assertNotIn(extra.finding_id, cast(dict[str, str], report['refusals']))

    def test_later_rename_is_proved_without_a_held_traits_overlay(self) -> None:
        self.check_trait_overlay_rollback('planning')

    def test_formatter_rollback_reproves_and_refuses_a_new_capture(self) -> None:
        self.check_trait_overlay_rollback('formatting')

    def check_trait_overlay_rollback(self, stage: str) -> None:
        source = ('struct Record;\n'
                  'trait Named { fn old(&self); }\n'
                  'impl Named for Record { fn old(&self) {} }\n'
                  'mod methods;\n'
                  'mod child;\n'
                  'fn call(record: Record) { record.old(); }\n')
        root = self.repo / 'src/lib.rs'
        child = self.repo / 'src/child.rs'
        methods = self.repo / 'src/methods.rs'
        child_source = 'fn call<T: super::Named>(record: &T) { record.old(); }\n'
        methods_source = 'impl super::Record { fn other(&self) {} }\n'
        _ = root.write_text(source)
        _ = child.write_text(child_source)
        _ = methods.write_text(methods_source)
        _ = self.git('add', '.')
        _ = self.git('commit', '-qm', 'trait and inherent method fixture')
        self.base = self.git('rev-parse', 'HEAD')
        self.tree = self.git('rev-parse', 'HEAD^{tree}')
        ledger, definition, findings = self.setup_findings()
        inherent = self.approve_extra_rename(ledger, definition, 1, 'other', 'old', 'src/methods.rs')
        named_held = ledger.named_held

        def trait_first(names: Iterable[str]) -> tuple[ledger_api.HeldFinding, ...]:
            # Finding hashes normally choose cross-file order. Exercise the dangerous
            # order explicitly: the held trait edit precedes the independent method edit.
            return tuple(sorted(named_held(names), key=lambda finding: finding.path != 'src/lib.rs'))

        original_rename = self.server.rename
        original_definitions = self.server.definitions
        checked_trees: list[str] = []

        def edits(text: str, _offset: int, new: str) -> tuple[bulk.SourceEdit, ...]:
            # The probe leaves the concrete call untouched, exercising destination-name proof.
            old = 'other' if new == 'old' else 'old'
            return tuple(edit for edit in word_edits(text, old, new)
                         if text.count('\n', 0, edit.start) != 5)

        self.server.edit = edits

        def references(path: Path, text: str, offset: int, new: str) -> bulk.WorkspaceRename:
            result = original_rename(path, text, offset, new)
            if path == root:
                return bulk.WorkspaceRename((*result.documents, bulk.DocumentEdits(
                    child, child_source, word_edits(child_source, 'old', new))))
            return result

        def resolve(path: Path, text: str, offsets: tuple[int, ...]) -> tuple[bulk.DefinitionResolution, ...]:
            if path not in {root, child, methods}:
                return original_definitions(path, text, offsets)
            current = text if path == root else self.server.documents[root]
            method = text if path == methods else self.server.documents.get(methods, methods_source)
            trait_name = 'number' if 'fn number' in current else 'old'
            method_name = 'old' if 'fn old' in method else 'other'
            trait_start = current.splitlines()[1].index('fn ' + trait_name) + 3
            trait = bulk.DefinitionTarget(root.as_uri(), (1, trait_start), (1, trait_start + len(trait_name)))
            method_start = method.index('fn ' + method_name) + 3
            own = bulk.DefinitionTarget(methods.as_uri(), (0, method_start), (0, method_start + len(method_name)))
            answers: list[bulk.DefinitionResolution] = []
            for offset in offsets:
                if path == root and text.count('\n', 0, offset) == 5:
                    checked_trees.append(current)
                    answers.append(own if method_name == 'old' or trait_name == 'number' else trait)
                else:
                    answers.append(own if path == methods else trait)
            return tuple(answers)

        reason = ('src/child.rs: another worktree holds the file' if stage == 'planning'
                  else 'post-format rename edit changed: src/lib.rs')
        self.runner.break_proof = stage == 'formatting'

        def available(_repo: Path, path: str) -> None:
            if stage == 'planning' and path == 'src/child.rs':
                raise bulk.BulkRenameError(reason)

        with mock.patch.object(self.server, 'rename', side_effect=references), mock.patch.object(
                self.server, 'definitions', side_effect=resolve), mock.patch.object(self, 'available', side_effect=available), mock.patch.object(
                ledger, 'named_held', side_effect=trait_first):
            report = self.execute(ledger, definition)
        self.assertEqual((report['applied'], report['refused'], report['reverted']), (1, 2, 0))
        refusals = cast(dict[str, str], report['refusals'])
        self.assertEqual(refusals[findings[0].finding_id], reason)
        self.assertIn("capture in an unedited file for 'old'", refusals[inherent.finding_id])
        self.assertTrue(checked_trees)
        if stage == 'planning':
            self.assertTrue(all('trait Named { fn old' in text for text in checked_trees))
        else:
            self.assertTrue(any('trait Named { fn old' in text for text in checked_trees))
        self.assertEqual(root.read_text(), source)
        self.assertEqual(self.server.documents[root], source)
        self.assertEqual(methods.read_text(), methods_source)
        self.assertEqual(self.server.documents[methods], methods_source)
        self.assertEqual(child.read_text(), child_source)
        self.assertTrue(all(isinstance(row.state, ledger_api.Filed)
                            for row in ledger.named_held((findings[0].finding_id, inherent.finding_id))))

    def test_values_held_reference_refuses_the_whole_shared_target_group(self) -> None:
        ledger, definition, findings = self.setup_values(dual_target=True)
        third = next(finding for finding in findings if finding.path == 'src/worker.rs' and finding.line == 4)
        proposal = cast(ledger_api.Filed, third.state).proposal
        _ = proposal.write_text(proposal.read_text().replace('declined', 'approved'))
        before = {path: (self.repo / path).read_bytes() for path in ('src/lib.rs', 'src/main.rs', 'src/worker.rs')}
        checked: Counter[str] = Counter()
        reason = 'src/main.rs: another worktree holds the file'

        def available(_repo: Path, path: str) -> None:
            checked[path] += 1
            if path == 'src/main.rs':
                raise bulk.BulkRenameError(reason)

        with mock.patch.object(self, 'available', side_effect=available), mock.patch.object(
                bulk, 'RustAnalyzer', side_effect=AssertionError('values started rust-analyzer')):
            report = self.execute(ledger, definition)
        self.assertEqual((report['applied'], report['refused'], report['reverted']), (1, 3, 0))
        held = {finding.finding_id for finding in findings if finding.path == 'src/worker.rs' and finding.line in (2, 4)}
        refusals = cast(dict[str, str], report['refusals'])
        self.assertEqual({identity: refusals[identity] for identity in held}, dict.fromkeys(held, reason))
        self.assertTrue(all(isinstance(row.state, ledger_api.Filed) for row in ledger.named_held(held)))
        self.assertEqual({path: (self.repo / path).read_bytes() for path in before}, before)
        self.assertFalse((self.repo / 'src/constants.rs').exists())
        self.assertIn('const FIRST: usize = 42', (self.repo / 'beta/src/constants.rs').read_text())
        self.assertTrue(all(count == (2 if path.startswith('beta/') else 1) for path, count in checked.items()))
        self.assertEqual(self.git('rev-list', '--count', f'{self.base}..HEAD'), '1')
        self.assertEqual([args[2] for _, args in self.runner.calls if args[0] == 'bash'], ['final', 'lint', 'final-pass'])

    def test_values_hold_appearing_after_planning_still_aborts(self) -> None:
        ledger, definition, _ = self.setup_values()
        checked: Counter[str] = Counter()
        reason = 'src/constants.rs: another worktree holds the file'

        def available(_repo: Path, path: str) -> None:
            checked[path] += 1
            if path == 'src/constants.rs' and checked[path] == 2:
                raise bulk.BulkRenameError(reason)

        with mock.patch.object(self, 'available', side_effect=available):
            report = self.execute(ledger, definition)
        self.assertEqual((report['applied'], report['reverted']), (0, 2))
        self.assertEqual(report['status'], 'batch refused: ' + reason)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.base)
        self.assertEqual(self.git('diff', self.base), '')
        self.assertFalse(self.runner.calls)
        self.assertFalse((self.repo / 'src/constants.rs').exists())

    def test_stale_lineage_or_missing_judged_commit_names_its_cause(self) -> None:
        ledger, _, findings = self.setup_findings()
        finding = findings[0]
        for evidence, judged in ((ledger_api.NeedsJudgment('item changed'), ledger_api.JudgedCommit(self.base)),
                                 (ledger_api.UnchangedSite(finding.path, finding.line), ledger_api.NoJudgedCommit())):
            with self.subTest(evidence=evidence, judged=judged), mock.patch.object(
                    ledger, 'finding_site', return_value=evidence), mock.patch.object(ledger, 'judged_commit', return_value=judged):
                result = run.current_rename(ledger, finding, self.base, run.RenameSources())
                self.assertEqual(result, bulk.Refusal(finding.finding_id,
                    'changed since judged: site lineage is not unchanged or the judged commit is missing'))

    def test_stale_unreadable_judged_file_names_its_cause(self) -> None:
        ledger, _, findings = self.setup_findings()
        finding = findings[0]
        with mock.patch.object(run.RenameSources, 'judged_file', return_value=run.CommandResult(1)):
            result = run.current_rename(ledger, finding, self.base, run.RenameSources())
        self.assertEqual(result, bulk.Refusal(finding.finding_id,
            'changed since judged: the file cannot be read at the judged commit'))

    def test_stale_physical_line_bytes_names_its_cause(self) -> None:
        ledger, _, findings = self.setup_findings()
        finding = findings[0]
        # Token-equivalent CRLF is still different judged physical line evidence.
        source = (self.repo / finding.path).read_bytes().decode().replace('\n', '\r\n')
        with mock.patch.object(run.RenameSources, 'judged_file', return_value=run.CommandResult(0, source)):
            result = run.current_rename(ledger, finding, self.base, run.RenameSources())
        self.assertEqual(result, bulk.Refusal(finding.finding_id,
            'changed since judged: the judged line bytes differ from the current line'))

    def test_stale_declaration_off_current_line_names_its_cause(self) -> None:
        ledger, _, findings = self.setup_findings()
        finding = findings[0]
        source = (self.repo / finding.path).read_text()
        declaration = rename_declaration(source, BindingRename(finding.line, finding.text, 'old', 'number'))
        self.assertNotIsInstance(declaration, NoBindingScope)
        assert not isinstance(declaration, NoBindingScope)
        with mock.patch.object(run, 'rename_declaration', return_value=replace(declaration, offset=0)):
            result = run.current_rename(ledger, finding, self.base, run.RenameSources())
        self.assertEqual(result, bulk.Refusal(finding.finding_id,
            'changed since judged: the declaration is not on the current line'))

    def setup_values(self, *, dual_target: bool = False) -> tuple[ledger_api.Ledger, rules.Rule, tuple[ledger_api.HeldFinding, ...]]:
        source = 'pub fn run() {\n    let first: usize = 42;\n    let second: usize = 43;\n    let third: usize = 44;\n}\n'
        for prefix in ('', 'beta/'):
            _ = (self.repo / (prefix + 'src/lib.rs')).write_text('mod worker;\n')
            _ = (self.repo / (prefix + 'src/worker.rs')).write_text(source)
        if dual_target:
            _ = (self.repo / 'src/main.rs').write_text('mod worker;\nfn main() {}\n')
        _ = self.git('add', '.')
        _ = self.git('commit', '-qm', 'values fixture')
        self.base = self.git('rev-parse', 'HEAD')
        self.tree = self.git('rev-parse', 'HEAD^{tree}')
        ledger = self.ledger()
        _ = (self.root / 'style/rust/no-magic-values.md').write_text(
            '---\ntier: B\nfamily: values\ninput_scope: file\npre_filter: magic-values\napproval: always\n---\n# Values\n')
        definition = next(rule for rule in rules.load_rules(self.repo) if rule.name == 'no-magic-values')
        ledger.set_tip(self.base)
        paths = ['src/worker.rs', 'beta/src/worker.rs']
        ledger.ticket_add('values-read-1', 'values-read', 'read', 'values', '.', paths, [definition.rule_id])
        ledger.ticket_assign('values-read-1', 'main')
        _ = ledger.ticket_base('values-read-1')
        identities: list[str] = []
        for index, (path, line, after, answer) in enumerate((
            (paths[0], 2, 'Add `const FIRST: usize = 42;` in constants.rs.', ''),
            (paths[1], 2, 'Add `const FIRST: usize = 42;` in constants.rs.', 'approved'),
            (paths[0], 3, 'Compute a better value instead.', ''),
            (paths[0], 4, 'Add `const THIRD: usize = 44;` in constants.rs.', 'declined'),
        )):
            issue = self.root / f'values-{index}.md'
            _ = issue.write_text('---\nstatus: open\n' + (f'style_answer: {answer}\n' if answer else '') + '---\nValues decision\n')
            finding = ledger.hold(definition.rule_id, path, line, source.splitlines()[line - 1],
                                  'restructure', str(40 + line), after, definition.blob, 'values-read-1')
            ledger.file_held(finding.finding_id, issue)
            identities.append(finding.finding_id)
        ledger.ticket_drop('values-read-1')
        return ledger, definition, ledger.named_held(identities)

    def test_values_apply_in_one_tested_commit_and_keep_refusals_filed(self) -> None:
        ledger, definition, findings = self.setup_values()
        with mock.patch.object(bulk, 'RustAnalyzer', side_effect=AssertionError('values started rust-analyzer')):
            path = run.run_rule(self.repo, definition.rule_id, self.tree, ledger, runner=self.runner,
                                available=lambda repo, path: None)
        report = bulk.object_fields(cast(object, json.loads(path.read_text())))
        self.assertEqual((report['applied'], report['refused'], report['reverted']), (2, 1, 0))
        self.assertIn('no exact constant', str(report['refusals']))
        self.assertEqual(report['skip_counts'], {'decision not approved': 1})
        self.assertEqual([args[2] for _, args in self.runner.calls if args[0] == 'bash'], ['final', 'lint', 'final-pass'])
        self.assertEqual(self.git('rev-list', '--count', f'{self.base}..HEAD'), '1')
        self.assertIn('checkpointed', str(report['status']))
        for prefix in ('', 'beta/'):
            self.assertIn('pub(super) const FIRST: usize = 42;', (self.repo / (prefix + 'src/constants.rs')).read_text())
            self.assertIn('let first: usize = FIRST;', (self.repo / (prefix + 'src/worker.rs')).read_text())
        self.assertTrue(all(row['family'] == 'values' for row in cast(list[dict[str, object]], report['counts'])))
        rows = ledger.named_held(finding.finding_id for finding in findings)
        refused = set(cast(dict[str, str], report['refusals']))
        for row in rows:
            if row.finding_id in refused:
                self.assertIsInstance(row.state, ledger_api.Filed)
                self.assertIsInstance(row.build_ticket, ledger_api.NoBuildTicket)
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_values_diagnostics_revert_created_module_and_its_whole_extraction(self) -> None:
        ledger, definition, _ = self.setup_values()
        def fail_root(repo: Path, arguments: Sequence[str]) -> run.CommandResult:
            result = self.runner(repo, arguments)
            if len(arguments) > 1 and arguments[1] in ('check', 'clippy') and (repo / 'src/constants.rs').exists():
                return run.CommandResult(1, json.dumps({'level': 'error', 'spans': [{'file_name': 'src/constants.rs'}]}))
            return result
        path = run.run_rule(self.repo, definition.rule_id, self.tree, ledger, runner=fail_root,
                            available=lambda repo, path: None)
        report = bulk.object_fields(cast(object, json.loads(path.read_text())))
        self.assertEqual((report['applied'], report['refused'], report['reverted']), (1, 2, 0))
        self.assertFalse((self.repo / 'src/constants.rs').exists())
        self.assertEqual((self.repo / 'src/lib.rs').read_text(), 'mod worker;\n')
        self.assertIn('let first: usize = 42;', (self.repo / 'src/worker.rs').read_text())
        self.assertTrue((self.repo / 'beta/src/constants.rs').is_file())

    def test_values_formatter_drift_and_red_tests_restore_new_and_existing_files(self) -> None:
        ledger, definition, _ = self.setup_values()
        self.runner.break_proof = True
        report = self.execute(ledger, definition)
        self.assertEqual((report['applied'], report['reverted']), (0, 2))
        self.assertIn('post-format constant proof', str(report['status']))
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.base)
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.runner = FakeRunner()
        self.runner.red_tests = True
        report = self.execute(ledger, definition)
        self.assertEqual((report['applied'], report['reverted']), (0, 2))
        self.assertIn('tests failed', str(report['status']))
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertFalse((self.repo / 'src/constants.rs').exists())

    def test_values_resume_after_process_loss_removes_recorded_creations(self) -> None:
        ledger, definition, _ = self.setup_values()
        stop_after_edit(self.repo / 'src/constants.rs', lambda: self.execute(ledger, definition))
        self.assertTrue((self.repo / 'src/constants.rs').is_file())
        report = self.execute(ledger, definition)
        self.assertEqual((report['applied'], report['refused'], report['reverted']), (2, 1, 0))
        self.assertIn('checkpointed', str(report['status']))

    def test_values_shared_module_in_two_targets_is_proved_for_both_and_counted_once(self) -> None:
        ledger, definition, _ = self.setup_values(dual_target=True)
        report = self.execute(ledger, definition)
        self.assertEqual((report['applied'], report['refused'], report['reverted']), (2, 1, 0))
        self.assertIn('mod constants;', (self.repo / 'src/lib.rs').read_text())
        self.assertIn('mod constants;', (self.repo / 'src/main.rs').read_text())
        self.assertIn('checkpointed', str(report['status']))

    def test_values_unproved_target_route_refuses_shared_worker(self) -> None:
        ledger, definition, findings = self.setup_values(dual_target=True)
        shared = next(finding for finding in findings if finding.path == 'src/worker.rs' and finding.line == 2)
        binary = ('#[path="worker.rs"] mod worker;\n'
                  + 'mod constants { pub(super) const FIRST: usize = 99; }\n'
                  + 'fn main() { worker::run(); }\n')
        _ = (self.repo / 'src/main.rs').write_text(binary)
        _ = self.git('add', '.')
        _ = self.git('commit', '-qm', 'attributed binary instance with a different constant')
        self.base = self.git('rev-parse', 'HEAD')
        self.tree = self.git('rev-parse', 'HEAD^{tree}')
        ledger.set_tip(self.base)
        plan = run.plan_constants(self.repo, ledger, self.base, run.select(ledger, definition))
        self.assertNotIn('src/worker.rs', {edit.path for edit in plan.files})
        self.assertTrue(any(item.finding_id == shared.finding_id
                            and 'target src/main.rs' in item.reason and 'attributed module route' in item.reason
                            for item in plan.refusals), repr(plan.refusals))
        report = self.execute(ledger, definition)
        self.assertEqual(report['applied'], 1, 'the independently proved beta target still applies')
        self.assertIn(shared.finding_id, cast(dict[str, str], report['refusals']))
        self.assertIn('let first: usize = 42;', (self.repo / 'src/worker.rs').read_text())
        self.assertEqual((self.repo / 'src/main.rs').read_text(), binary)
        self.assertFalse((self.repo / 'src/constants.rs').exists())
        self.assertIsInstance(ledger.named_held((shared.finding_id,))[0].state, ledger_api.Filed)

    def test_values_dangling_creation_symlink_refuses_before_any_write(self) -> None:
        ledger, definition, _ = self.setup_values()
        outside = self.root / 'outside-constants.rs'
        link = self.repo / 'src/constants.rs'
        link.symlink_to(outside)
        report = self.execute(ledger, definition)
        self.assertEqual(report['applied'], 0)
        self.assertFalse(outside.exists(), 'a refused creation must never write outside the workspace')
        self.assertIn('symlink', str(report['status']))
        self.assertTrue(link.is_symlink(), 'rollback must preserve a symlink it did not create')
        self.assertEqual(link.readlink(), outside)
        self.assertFalse((self.repo / 'beta/src/constants.rs').exists())
        self.assertEqual(self.git('diff', self.base), '')
        self.assertFalse(self.runner.calls, 'preflight must precede formatting and validation')

    def test_values_rollback_removes_ignored_owned_creations(self) -> None:
        ledger, definition, findings = self.setup_values()
        exclude = Path(self.git('rev-parse', '--path-format=absolute', '--git-path', 'info/exclude'))
        _ = exclude.write_text(exclude.read_text() + '\nconstants.rs\n')
        self.runner.break_proof = True
        report = self.execute(ledger, definition)
        self.assertEqual((report['applied'], report['reverted']), (0, 2))
        self.assertIn('post-format constant proof', str(report['status']))
        self.assertEqual(self.git('diff', self.base), '')
        for prefix in ('', 'beta/'):
            self.assertFalse((self.repo / (prefix + 'src/constants.rs')).exists())
            self.assertEqual((self.repo / (prefix + 'src/lib.rs')).read_text(), 'mod worker;\n')
        intent = run.WorkspaceIntent(self.repo, 'unused')
        self.assertFalse(intent.path.exists(), 'all owned files must be removed before clearing the journal')
        rows = ledger.named_held(item.finding_id for item in findings)
        self.assertTrue(all(isinstance(row.state, ledger_api.Filed) for row in rows))

    def test_values_preserve_preexisting_untracked_constant_file(self) -> None:
        ledger, definition, _ = self.setup_values()
        untracked = self.repo / 'src/constants.rs'
        _ = untracked.write_text('// user work\n')
        report = self.execute(ledger, definition)
        self.assertEqual(report['applied'], 0)
        self.assertIn('overwrite an untracked file', str(report['status']))
        self.assertEqual(untracked.read_text(), '// user work\n')
        self.assertEqual(self.git('diff', self.base), '')

    def test_values_recovery_refuses_modified_creation_without_restoring_any_other_file(self) -> None:
        ledger, definition, _ = self.setup_values()
        stop_after_edit(self.repo / 'src/constants.rs', lambda: self.execute(ledger, definition))
        _ = (self.repo / 'src/constants.rs').write_text('// independently changed\n')
        before = self.git('diff', self.base)
        with self.assertRaisesRegex(bulk.BulkRenameError, 'bytes do not match the recorded creation'):
            _ = self.execute(ledger, definition)
        self.assertEqual(self.git('diff', self.base), before)
        self.assertEqual((self.repo / 'src/constants.rs').read_text(), '// independently changed\n')

    def test_values_skip_a_file_held_by_another_open_ticket(self) -> None:
        ledger, definition, findings = self.setup_values()
        other = next(finding for finding in findings if finding.path.startswith('beta/'))
        assert isinstance(other.state, ledger_api.Filed)
        ledger.ticket_add('other-values-1', 'other-values', 'proposal', 'values', 'beta', [other.path],
                          [definition.rule_id], proposal=str(other.state.proposal), finding_ids=(other.finding_id,))
        report = self.execute(ledger, definition)
        self.assertEqual((report['applied'], report['refused']), (1, 1))
        self.assertIn('held by another open ticket', str(report['skip_counts']))
        self.assertFalse((self.repo / 'beta/src/constants.rs').exists())

    def test_values_planner_reads_base_sources_and_configured_cargo_target(self) -> None:
        ledger, definition, _ = self.setup_values()
        manifest = self.repo / 'Cargo.toml'
        _ = manifest.write_text(manifest.read_text() + '\n[lib]\npath="src/entry.rs"\n')
        _ = self.git('mv', 'src/lib.rs', 'src/entry.rs')
        _ = self.git('add', '.')
        _ = self.git('commit', '-qm', 'custom library target')
        base = self.git('rev-parse', 'HEAD')
        _ = (self.repo / 'src/worker.rs').write_text('dirty checkout bytes must not enter the snapshot')
        # Site lineage is held fixed to inspect the planner's source boundary directly.
        selected = run.select(ledger, definition)
        def unchanged(ledger: ledger_api.Ledger, finding: ledger_api.HeldFinding, head: str,
                      sources: run.RenameSources) -> ledger_api.HeldFinding:
            del ledger, head, sources
            return finding
        with mock.patch.object(run, 'current_finding', side_effect=unchanged):
            plan = run.plan_constants(self.repo, ledger, base, selected)
        self.assertEqual(len({item.finding.finding_id for file in plan.files for item in file.kept}), 2)
        self.assertTrue(any(file.path == 'src/entry.rs' for file in plan.files))
        self.assertTrue(all('dirty checkout' not in file.original for file in plan.files))

    def test_third_rule_family_refuses_and_names_both_supported_families(self) -> None:
        ledger, definition, _ = self.setup_findings()
        with mock.patch.object(rules, 'load_rules', return_value=(replace(definition, family='types'),)):
            with self.assertRaisesRegex(bulk.BulkRenameError, 'names or values'):
                _ = self.execute(ledger, definition)
        self.assertFalse(self.runner.calls)

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
        self.assertEqual(report["refusals"], {findings[0].finding_id:
            "changed since judged: site lineage is not unchanged or the judged commit is missing"})
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
        extra = self.approve_extra_rename(ledger, definition, 6, 'other', 'size')
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
        self.assertEqual((report["applied"], report["refused"], report["reverted"]), (1, 1, 0))
        self.assertEqual(report["refusals"], {findings[1].finding_id: "workspace diagnostics"})
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
        kept, reverted = run.validate(workspace, self.runner, {"check_clippy": 0.0}, [], lambda paths: paths)
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

    def test_unexpected_runner_failure_is_reported_as_a_tool_defect(self) -> None:
        ledger, definition, _ = self.setup_findings()
        self.addCleanup(ledger.close)

        def defective_runner(repo: Path, arguments: Sequence[str]) -> run.CommandResult:
            _ = repo, arguments
            raise RuntimeError('formatter adapter broke')

        path = run.run_rule(self.repo, definition.rule_id, self.tree, ledger, self.server,
                            defective_runner, available=self.available)
        report = bulk.object_fields(cast(object, json.loads(path.read_text())))
        self.assertEqual(report['status'], 'tool defect: formatter adapter broke')
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_unrelated_formatter_changes_abort_and_restore(self) -> None:
        ledger, definition, _ = self.setup_findings()
        self.runner.unrelated = True
        report = self.execute(ledger, definition)
        self.assertIn("unrelated file", str(report["status"]))
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_formatter_token_change_refuses_only_its_file_and_keeps_one_tested_commit(self) -> None:
        ledger, definition, findings = self.setup_findings()
        self.addCleanup(ledger.close)
        source = (self.repo / 'src/lib.rs').read_text()
        self.runner.break_proof = True
        report = self.execute(ledger, definition)
        reason = 'post-format rename edit changed: src/lib.rs'
        self.assertEqual((report['applied'], report['refused'], report['reverted']), (1, 1, 0))
        self.assertEqual(report['refusals'], {findings[0].finding_id: reason})
        self.assertEqual((self.repo / 'src/lib.rs').read_text(), source)
        self.assertIn('let number', (self.repo / 'beta/src/lib.rs').read_text())
        self.assertIsInstance(ledger.named_held((findings[0].finding_id,))[0].state, ledger_api.Filed)
        self.assertTrue(any(item.reason == reason for ticket in cast(list[str], report['tickets'])
                            for item in ledger.bulk_refusals(ticket)))
        self.assertEqual(self.git('rev-list', '--count', f'{self.base}..HEAD'), '1')
        self.assertEqual([args[2] for _, args in self.runner.calls if args[0] == 'bash'], ['final', 'lint', 'final-pass'])
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_formatter_failure_in_a_reference_restores_its_whole_rename_group(self) -> None:
        ledger, definition, findings = self.setup_field_reference()
        self.addCleanup(ledger.close)
        originals = {path: (self.repo / path).read_text() for path in ('src/lib.rs', 'src/child.rs')}

        def formatter(repo: Path, arguments: Sequence[str]) -> run.CommandResult:
            result = self.runner(repo, arguments)
            if 'fmt' in arguments:
                _ = (repo / 'src/child.rs').write_text('fn read() {}\n')
            return result

        path = run.run_rule(self.repo, definition.rule_id, self.tree, ledger, self.server, formatter,
                            available=self.available)
        report = bulk.object_fields(cast(object, json.loads(path.read_text())))
        self.assertEqual((report['applied'], report['refused'], report['reverted']), (1, 1, 0))
        self.assertEqual(report['refusals'], {findings[0].finding_id:
                                             'post-format rename edit changed: src/child.rs'})
        for name, original in originals.items():
            self.assertEqual((self.repo / name).read_text(), original)
        self.assertIn('let number', (self.repo / 'beta/src/lib.rs').read_text())

    def test_formatter_rollback_reproves_and_keeps_an_independent_reader(self) -> None:
        extra_path = self.repo / 'src/extra.rs'
        extra_source = 'fn extra() { let other = 2; consume(other); }\n'
        _ = extra_path.write_text(extra_source)
        ledger, definition, findings = self.setup_field_reference('mod extra;\n')
        self.addCleanup(ledger.close)
        extra = self.approve_extra_rename(ledger, definition, 1, 'other', 'number', 'src/extra.rs')
        self.server.edit = lambda text, offset, new: word_edits(text, 'other' if 'let other' in text else 'old', new)
        named_held = ledger.named_held

        def declaration_first(names: Iterable[str]) -> tuple[ledger_api.HeldFinding, ...]:
            return tuple(sorted(named_held(names), key=lambda finding: finding.path != 'src/lib.rs'))

        self.runner.break_proof = True
        with mock.patch.object(ledger, 'named_held', side_effect=declaration_first), mock.patch.object(
                run, 'plan_renames', wraps=run.plan_renames) as planner:
            report = self.execute(ledger, definition)
        self.assertEqual(planner.call_count, 2)
        self.assertEqual((report['applied'], report['refused'], report['reverted']), (2, 1, 0))
        self.assertEqual(report['refusals'], {findings[0].finding_id: 'post-format rename edit changed: src/lib.rs'})
        self.assertNotIn(extra.finding_id, cast(dict[str, str], report['refusals']))
        self.assertEqual(extra_path.read_text(), extra_source.replace('other', 'number'))
        self.assertIn('let number', (self.repo / 'beta/src/lib.rs').read_text())

    def check_formatter_retry_after_reproof_excludes_another_file(self, interrupted: bool) -> None:
        extra = self.repo / 'src/extra.rs'
        _ = extra.write_text('fn extra() { let old = 2; consume(old); }\n')
        library = self.repo / 'src/lib.rs'
        _ = library.write_text(library.read_text() + '\nmod extra;\n')
        _ = self.git('add', '.')
        _ = self.git('commit', '-qm', 'retry survivor fixture')
        self.base, self.tree = self.git('rev-parse', 'HEAD'), self.git('rev-parse', 'HEAD^{tree}')
        ledger, definition, findings = self.setup_findings()
        self.addCleanup(ledger.close)
        _ = self.approve_extra_rename(ledger, definition, 1, 'old', 'number', 'src/extra.rs')
        original = (self.repo / 'beta/src/lib.rs').read_text()
        planner = run.plan_renames
        passes = 0

        def replan(repo: Path, store: ledger_api.Ledger, base: str, selected: run.Selection,
                   server: bulk.RenameServer, availability: run.PlanningFileAvailability) -> run.PlannedEdits:
            plan = planner(repo, store, base, selected, server, availability)
            if passes:
                # The remaining reader fails fresh resolution after the first edit is restored.
                return run.PlannedEdits(tuple(file for file in plan.files if file.path != 'beta/src/lib.rs'),
                                        (*plan.refusals, bulk.Refusal(findings[1].finding_id, 'reader no longer proved')))
            return plan

        def formatter(repo: Path, arguments: Sequence[str]) -> run.CommandResult:
            nonlocal passes
            result = self.runner(repo, arguments)
            if 'fmt' in arguments:
                passes += 1
                if passes == 1:
                    _ = (repo / 'src/lib.rs').write_text('fn changed() {}\n')
                else:
                    _ = (repo / 'beta/src/lib.rs').write_text('\n' + original)
                    if interrupted:
                        os._exit(73)
            return result

        with mock.patch.object(run, 'plan_renames', side_effect=replan):
            if interrupted:
                stop_process(lambda: run.run_rule(self.repo, definition.rule_id, self.tree, ledger,
                                                  self.server, formatter, available=self.available))
            else:
                path = run.run_rule(self.repo, definition.rule_id, self.tree, ledger,
                                    self.server, formatter, available=self.available)
                report = bulk.object_fields(cast(object, json.loads(path.read_text())))
                self.assertEqual((report['applied'], report['refused'], report['reverted']), (1, 2, 0))
                self.assertEqual(passes, 2)
                self.assertIn('let number', extra.read_text())
                self.assertEqual((self.repo / 'beta/src/lib.rs').read_text(), original)
                self.assertEqual(self.git('status', '--porcelain'), '')
                return
        tickets = {ticket['id'] for ticket in ledger.proposal_tickets()
                   if ticket['state'] not in ledger_api.FINAL_TICKET_STATES}
        self.assertTrue(tickets)
        self.assertEqual((self.repo / 'beta/src/lib.rs').read_text(), '\n' + original)
        # Stop the recovered run at selection, so only recovery can restore the dirty files.
        with mock.patch.object(run, 'select', return_value=run.Selection((), ())):
            try:
                report = self.execute(ledger, definition)
            except bulk.BulkRenameError as error:
                self.fail(f'recovery must restore the retry and release its tickets: {error}')
        self.assertEqual(report['applied'], 0)
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertTrue(all(ticket['state'] in ledger_api.FINAL_TICKET_STATES
                            for ticket in ledger.proposal_tickets() if ticket['id'] in tickets))
        self.assertTrue(all(isinstance(row.build_ticket, ledger_api.NoBuildTicket)
                            for row in ledger.named_held(tuple(finding.finding_id for finding in findings))))

    def test_formatter_retry_restores_paths_excluded_by_survivor_reproof(self) -> None:
        self.check_formatter_retry_after_reproof_excludes_another_file(False)

    def test_recovery_restores_a_formatter_retry_on_an_excluded_path_and_releases_tickets(self) -> None:
        self.check_formatter_retry_after_reproof_excludes_another_file(True)

    def test_recovery_refuses_content_changes_in_an_excluded_formatter_path(self) -> None:
        files = tuple(bulk.FileRenames(path, (self.repo / path).read_text(),
                                      (self.repo / path).read_text().replace('old', 'number'), (), ())
                      for path in ('src/lib.rs', 'beta/src/lib.rs'))
        intent = run.WorkspaceIntent(self.repo, 'retry recovery')
        intent.begin(())
        intent.record_plan(run.PlannedEdits(files, ()))
        intent.record_plan(run.PlannedEdits(files[1:], ()))
        _ = (self.repo / files[0].path).write_text('fn different_item() {}\n')
        _ = (self.repo / files[1].path).write_text(files[1].edited)
        with self.assertRaisesRegex(bulk.BulkRenameError, 'bytes do not match the recorded edit'):
            _ = intent.recover()
        self.assertEqual((self.repo / files[1].path).read_text(), files[1].edited)
        self.assertEqual((self.repo / files[0].path).read_text(), 'fn different_item() {}\n')

    def test_rebuilt_plan_rechecks_a_path_that_became_unavailable(self) -> None:
        ledger, definition, findings = self.setup_findings()
        self.addCleanup(ledger.close)
        held = False
        reason = 'beta/src/lib.rs: another worktree holds the file'

        def available(_repo: Path, path: str) -> None:
            if held and path == 'beta/src/lib.rs':
                raise bulk.BulkRenameError(reason)

        def formatter(repo: Path, arguments: Sequence[str]) -> run.CommandResult:
            nonlocal held
            result = self.runner(repo, arguments)
            if 'fmt' in arguments:
                held = True
                _ = (repo / 'src/lib.rs').write_text('fn changed() {}\n')
            return result

        path = run.run_rule(self.repo, definition.rule_id, self.tree, ledger, self.server,
                            formatter, available=available)
        report = bulk.object_fields(cast(object, json.loads(path.read_text())))
        self.assertEqual((report['applied'], report['refused'], report['reverted']), (0, 2, 0))
        self.assertEqual(bulk.object_fields(report['refusals'])[findings[1].finding_id], reason)
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_lengthening_rename_reflow_keeps_identifier_token_positions(self) -> None:
        source = ('fn run() {\n'
                  '    let old = input.first_method().second_method().third_method().fourth_method().fifth_method();\n'
                  '    consume(old);\n}\n')
        _ = (self.repo / 'src/lib.rs').write_text(source)
        _ = self.git('add', '.')
        _ = self.git('commit', '-qm', 'long method chain')
        self.base, self.tree = self.git('rev-parse', 'HEAD'), self.git('rev-parse', 'HEAD^{tree}')
        ledger, definition, _ = self.setup_findings()
        self.addCleanup(ledger.close)
        planned = source.replace('old', 'number')

        def formatter(repo: Path, arguments: Sequence[str]) -> run.CommandResult:
            result = self.runner(repo, arguments)
            if 'fmt' in arguments:
                # Model rustfmt's method-chain reflow; the scratch replay runs the real formatter.
                formatted = planned.replace('input.', 'input\n        .').replace('().', '()\n        .')
                _ = (repo / 'src/lib.rs').write_text(formatted)
            return result

        path = run.run_rule(self.repo, definition.rule_id, self.tree, ledger, self.server, formatter,
                            available=self.available)
        report = bulk.object_fields(cast(object, json.loads(path.read_text())))
        formatted = (self.repo / 'src/lib.rs').read_text()
        self.assertNotEqual(formatted, planned)
        self.assertIn('\n        .first_method()', formatted)
        self.assertEqual((report['applied'], report['refused'], report['reverted']), (2, 0, 0))
        self.assertTrue(bulk.file_edits_preserved(bulk.FileRenames('src/lib.rs', source, planned, (), ()), formatted))

    def test_all_formatter_failures_leave_findings_filed_without_tests_or_a_commit(self) -> None:
        ledger, definition, findings = self.setup_findings()
        self.addCleanup(ledger.close)

        def formatter(repo: Path, arguments: Sequence[str]) -> run.CommandResult:
            result = self.runner(repo, arguments)
            if 'fmt' in arguments:
                for name in ('src/lib.rs', 'beta/src/lib.rs'):
                    _ = (repo / name).write_text('fn changed() {}\n')
            return result

        path = run.run_rule(self.repo, definition.rule_id, self.tree, ledger, self.server, formatter,
                            available=self.available)
        report = bulk.object_fields(cast(object, json.loads(path.read_text())))
        self.assertEqual((report['applied'], report['refused'], report['reverted']), (0, 2, 0))
        self.assertEqual(report['status'], 'no edits')
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.base)
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertFalse(any(args[0] == 'bash' for _, args in self.runner.calls))
        self.assertTrue(all(isinstance(row.state, ledger_api.Filed)
                            for row in ledger.named_held(tuple(finding.finding_id for finding in findings))))

    def test_formatter_wraps_plain_comment_text_at_the_same_token(self) -> None:
        for marker in ('//', '///', '//!'):
            with self.subTest(marker=marker):
                before = f'{marker} Recorded `answers` keep the same text and attachment.\nfn run() {{}}\n'
                after = f'{marker} Recorded `answers` keep the same text\n{marker} and attachment.\nfn run() {{}}\n'
                file = bulk.FileRenames('src/lib.rs', before, before, (), ())
                self.assertTrue(bulk.file_edits_preserved(file, after))
                self.assertFalse(bulk.file_edits_preserved(file, after.replace('same', 'other')))
                self.assertFalse(bulk.file_edits_preserved(file, 'fn run() {}\n' + after.split('fn run()')[0]))
        before = '/// First paragraph.\n///\n/// Second paragraph.\nfn run() {}'
        after = '/// First paragraph. Second paragraph.\nfn run() {}'
        self.assertFalse(bulk.file_edits_preserved(bulk.FileRenames('src/lib.rs', before, before, (), ()), after))

    def test_formatter_wraps_prose_starting_with_inline_code_without_changing_its_attachment(self) -> None:
        before = ('// `tree.insert_into_hierarchy` inserts `root_item` under an existing parent workspace or as a\n'
                  '// top-level peer; `regroup_members` reapplies `inline_dirs`.\nfn run() {}\n')
        after = ('// `tree.insert_into_hierarchy` inserts `root_item` under an existing parent workspace or as\n'
                 '// a top-level peer; `regroup_members` reapplies `inline_dirs`.\nfn run() {}\n')
        file = bulk.FileRenames('src/lib.rs', before, before, (), ())
        self.assertTrue(bulk.file_edits_preserved(file, after))
        self.assertFalse(bulk.file_edits_preserved(file, after.replace('root_item', 'other')))
        self.assertFalse(bulk.file_edits_preserved(file, 'fn run() {}\n' + after.split('fn run()')[0]))

    def test_formatter_preserves_structured_comments_and_item_attachment(self) -> None:
        for body in ('/// ```\n/// first\n/// second\n/// ```\n',
                     '/// first  \n/// second\n', '/// - first\n/// - second\n',
                     '/* first\nsecond */\n'):
            with self.subTest(body=body):
                before = body + 'fn first() {}\nfn second() {}\n'
                file = bulk.FileRenames('src/lib.rs', before, before, (), ())
                self.assertFalse(bulk.file_edits_preserved(file, 'fn first() {}\n' + body + 'fn second() {}\n'))
                self.assertFalse(bulk.file_edits_preserved(file, before.replace('first\n', 'first second\n').replace('/// second\n', '')))

    def test_formatter_preserves_line_comment_like_contents_inside_a_block_comment(self) -> None:
        before = '/**\n// first\n// second\n*/\nfn run() {}\n'
        file = bulk.FileRenames('src/lib.rs', before, before, (), ())
        self.assertFalse(bulk.file_edits_preserved(file, '/**\n// first second\n*/\nfn run() {}\n'))
        self.assertFalse(bulk.file_edits_preserved(file, '// first second\nfn run() {}\n'))

    def test_formatter_preserves_backslash_hard_breaks_in_comment_paragraphs(self) -> None:
        before = '/// first\\\n/// second\nfn run() {}\n'
        file = bulk.FileRenames('src/lib.rs', before, before, (), ())
        self.assertFalse(bulk.file_edits_preserved(file, before.replace('first\\\n/// second', 'first second')))
        self.assertFalse(bulk.file_edits_preserved(file, before.replace('first\\\n/// second', 'first\\ second')))
        plain = '// first\n// second\nfn run() {}\n'
        self.assertTrue(bulk.file_edits_preserved(bulk.FileRenames('src/lib.rs', plain, plain, (), ()),
                                                 '// first second\nfn run() {}\n'))

    def test_compiler_failure_is_refused_with_its_cause_before_the_survivor_commit(self) -> None:
        ledger, definition, findings = self.setup_findings()
        self.addCleanup(ledger.close)
        def diagnostic_runner(repo: Path, arguments: Sequence[str]) -> run.CommandResult:
            result = self.runner(repo, arguments)
            if (len(arguments) > 1 and arguments[1] in ('check', 'clippy')
                    and 'let number' in (repo / 'src/lib.rs').read_text()):
                return run.CommandResult(101, json.dumps({'reason': 'compiler-message', 'message': {
                    'level': 'error', 'message': 'struct `Record` has no field named `number`',
                    'code': {'code': 'E0560'}, 'spans': [{'file_name': 'src/lib.rs'}]}}))
            return result
        path = run.run_rule(self.repo, definition.rule_id, self.tree, ledger, self.server,
                            diagnostic_runner, available=self.available)
        report = bulk.object_fields(cast(object, json.loads(path.read_text())))
        self.assertEqual((report['applied'], report['refused'], report['reverted']), (1, 1, 0))
        refusals = bulk.object_fields(report['refusals'])
        self.assertIn('E0560: struct `Record` has no field named `number`', str(refusals[findings[0].finding_id]))
        self.assertIn('let old', self.git('show', 'HEAD:src/lib.rs'))
        self.assertIn('let number', self.git('show', 'HEAD:beta/src/lib.rs'))
        self.assertIsInstance(ledger.named_held((findings[0].finding_id,))[0].state, ledger_api.Filed)
        self.assertEqual(sum(args[0] == 'bash' and args[2] == 'final' for _, args in self.runner.calls), 1)

    def test_read_only_proof_dependencies_do_not_join_the_edit_group(self) -> None:
        files = (bulk.FileRenames('src/lib.rs', '', '', (), (), proof_dependencies=frozenset({'src/other.rs'})),
                 bulk.FileRenames('src/other.rs', '', '', (), ()))
        workspace = run.WorkspaceEdits(self.repo, self.base, files)
        self.assertEqual(run.dependent_files(workspace.dependencies, {'src/other.rs'}), {'src/other.rs'})

    def test_formatter_comma_and_block_brace_changes_keep_identifiers_and_comments(self) -> None:
        for before, after in (
                ('fn run() { make(number) }', 'fn run() { { make(number,) } }'),
                ('fn run() { Record { number } }', 'fn run() { Record { number, } }'),
                ('fn run() { { number } }', 'fn run() { number }'),
                ('fn run() { Record { number /* retained */, } }',
                 'fn run() { Record { number, /* retained */ } }')):
            with self.subTest(before=before):
                file = bulk.FileRenames('src/lib.rs', before, before, (), ())
                self.assertTrue(bulk.file_edits_preserved(file, after))
                self.assertFalse(bulk.file_edits_preserved(file, after.replace('number', 'other')))
                if 'retained' in after:
                    self.assertFalse(bulk.file_edits_preserved(file, after.replace('retained', 'changed')))

    def test_formatter_import_reordering_still_refuses(self) -> None:
        before = 'pub(crate) use z::Z;\npub(crate) use a::A;\n'
        after = 'pub(crate) use a::A;\npub(crate) use z::Z;\n'
        self.assertFalse(bulk.file_edits_preserved(bulk.FileRenames('src/lib.rs', before, before, (), ()), after))

    def test_formatter_proof_rejects_added_removed_changed_or_reordered_tokens(self) -> None:
        # These proof tests belong to test_bulk_rename, kept here within this phase's file scope.
        before = 'fn run() { let number = input.first().second(); consume(number); }\n'
        file = bulk.FileRenames('src/lib.rs', before, before, (), ())
        for changed in (before.replace('.second()', ''),
                        before.replace('consume(number)', 'consume(other)'),
                        before.replace('input.first().second()', 'input.second().first()')):
            with self.subTest(changed=changed):
                self.assertFalse(bulk.file_edits_preserved(file, changed))
        self.assertFalse(bulk.file_edits_preserved(file, before + '// unexpected comment\n'))


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
