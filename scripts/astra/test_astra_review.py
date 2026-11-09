"""Tests for asking gpt-6-astra to review a document, against a stand-in accounts tool."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from scripts.agents import codex_test_support as codex_fixture
from scripts.astra import astra_review  # noqa: E402
from scripts.astra import astra_roles, astra_work, design_roles  # noqa: E402
from scripts.production.staff_test_support import StaffTestCase, fixture_runtime_id, fixture_worker, transcript_fixture, unit_membership
from scripts.production import staff  # noqa: E402
from scripts.production import test_worker as worker_tests
from scripts.production.cli_fixture import invoke_cli
from scripts.herdr.test_worker_panes import PaneWorld
from scripts.herdr import herdr, side_pane, turn_end
from scripts.direct import assign
from scripts.lib.signal_test_support import install_signal_pid_guard  # noqa: E402
from scripts.lib import unit_runs
from scripts.lib.test_models_registry import write_registry

STUB = """#!/usr/bin/env python3
import os, sys
arguments = sys.argv[1:]
if arguments == ["role", "workers"]:
    if os.environ.get("STUB_ROLE_FAILS"):
        print("accounts: no workers account is set", file=sys.stderr)
        sys.exit(1)
    print("codex1")
    sys.exit(0)
raise AssertionError(arguments)
"""


def setUpModule() -> None:
    install_signal_pid_guard()
    codex_fixture.setUpModule()


class AstraReviewTests(StaffTestCase):
    root: Path = Path()
    document: Path = Path()
    environment: dict[str, str] = {}

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.fixture: worker_tests.WorkerCommandTests = worker_tests.WorkerCommandTests("runTest")
        self.router: worker_tests.PaneCommandRouter = worker_tests.PaneCommandRouter()

    @property
    def world(self) -> PaneWorld:
        return self.fixture.fixture.world

    @override
    def setUp(self) -> None:
        super().setUp()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.fixture.root
        self.router.setup(self, self.root)
        self.fixture.agent.consultations(self)
        self.fixture.agent.final = "Verdict: clear.\n\n1. Section Why: the cause is not shown."
        self.fixture.agent.without_done = True
        stub = self.root / "accounts"
        _ = stub.write_text(STUB, encoding="utf-8")
        stub.chmod(0o755)
        self.document = self.root / "proposal.md"
        _ = self.document.write_text("# Proposal\n", encoding="utf-8")
        self.environment = {**os.environ,
                            "DESIGN_ROLES_STATE": str(self.root / "design-roles"),
                            "DESIGN_OWNERSHIP_STATE": str(self.root / "design-ownership"),
                            "DESIGN_ROLES_HOST": "fixture-host",
                            "ASTRA_ROLES_STATE": str(self.root / "roles"),
                            "HERDR_PANE_ID": "w1:p0",
                            "CLAUDE_CODE_SESSION_ID": "",
                            "CODEX_THREAD_ID": fixture_runtime_id("unregistered-review-caller")}
        _ = self.enterContext(patch.object(astra_review, "ACCOUNTS_COMMAND", str(stub)))
        _ = self.enterContext(patch.object(unit_runs, "ASTRA_RUN_ROOT", self.root / "runs"))
        self.world.activate(self.environment["CODEX_THREAD_ID"], "w1:p0", "codex")

    def run_script(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return invoke_cli(astra_review, lambda: astra_review.main(list(arguments)), list(arguments),
                          self.environment, self.root)

    def saved_prompt(self) -> Path:
        return max((self.root / "runs").glob("*/prompt.md"), key=lambda path: path.stat().st_mtime_ns)

    def held_reviewer(self, *, busy: bool = False) -> design_roles.HeldDesign:
        worker = fixture_worker("demo-proposal-astra-reviewer", unit_membership("demo", "proposal-unit").id,
                                runtime_id=fixture_runtime_id("held-thread"),
                                backend=staff.CodexBackend(self.root / "codex-home"))
        design_id = "00000000-0000-4000-8000-000000000016"
        design = design_roles.HeldDesign(design_roles.DesignPlacement(design_id, "fixture-host", 1), "fixture-repo",
                                  str(self.root), str(self.document), "proposal", "2026-10-21T12:00:00+00:00",
                                  design_roles.DesignUnbound(),
                                  (design_roles.RoleInstance("designer", "demo-proposal-astra-designer", design_roles.RoleNotOpened()),
                                   design_roles.RoleInstance("reviewer", "demo-proposal-astra-reviewer", design_roles.RoleOpen(worker.id),
                                                          assignment_claim="active-dispatch" if busy else "")))
        design_roles._save(design)  # pyright: ignore[reportPrivateUsage]
        return design

    def test_document_with_a_held_reviewer_resumes_that_thread_and_saves_each_reply(self) -> None:
        assignments: list[tuple[str, design_roles.ThreadAccessPolicy]] = []

        def dispatch(design: design_roles.HeldDesign, role: design_roles.Role, prompt: Path, reply: Path) -> design_roles.SubmittedRoleTurn:
            instance = next(item for item in design.roles if item.role == role)
            self.assertIsInstance(instance.lifecycle, design_roles.RoleOpen)
            self.assertTrue(prompt.is_file())
            if isinstance(instance.lifecycle, design_roles.RoleOpen):
                assignments.append((instance.lifecycle.thread_id, design_roles.access_policy(design, role)))
            _ = reply.write_text("Held reviewer evidence.\n")
            return design_roles.SubmittedRoleTurn(reply, design_roles.design_directory(design.placement.design_id))

        with patch.dict(os.environ, {**self.environment, "CLAUDE_CODE_SESSION_ID": "fixture-holder"}), \
                patch.object(staff, "runtime_id", return_value="fixture-holder"), \
                patch.object(design_roles, "dispatch_turn", side_effect=dispatch), \
                patch.object(astra_review, "workers_label", side_effect=AssertionError("held review started a fresh exec")), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            design = self.held_reviewer()
            for focus in ("first round", "repair round"):
                self.assertEqual(astra_review.main([str(self.document), focus]), 0)
            prompts = sorted(design_roles.design_directory(design.placement.design_id).glob("prompts/*.md"))
            self.assertEqual(len(prompts), 2)
            replies: list[str] = []
            for prompt in prompts:
                header = astra_work.read_header(prompt)
                self.assertIsInstance(header, astra_work.AstraWork)
                if isinstance(header, astra_work.AstraWork):
                    self.assertEqual((header.role, header.member), ("astra-reviewer", "demo-proposal-astra-reviewer"))
                    self.assertEqual(Path(header.reply_path).read_text(), "Held reviewer evidence.\n")
                    replies.append(header.reply_path)
        self.assertEqual(assignments, [(fixture_runtime_id("held-thread"), design_roles.ReadOnlyReviewer()), (fixture_runtime_id("held-thread"), design_roles.ReadOnlyReviewer())])
        self.assertEqual(len(set(replies)), 2)
        self.assertIn("Submitted:", output.getvalue())
        self.assertNotIn("Held reviewer evidence.", output.getvalue())
        self.assertEqual(self.router.commands, [])
        self.assertEqual(self.world.requests, [])

    def test_busy_held_reviewer_is_reported_without_a_fresh_thread_or_prompt(self) -> None:
        with patch.dict(os.environ, {**self.environment, "CLAUDE_CODE_SESSION_ID": "fixture-holder"}), \
                patch.object(staff, "runtime_id", return_value="fixture-holder"), \
                patch.object(design_roles, "dispatch_turn", side_effect=AssertionError("busy role dispatched")), \
                patch.object(astra_review, "workers_label", side_effect=AssertionError("busy role used fresh exec")), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            design = self.held_reviewer(busy=True)
            self.assertEqual(astra_review.main([str(self.document)]), 1)
            self.assertEqual(list(design_roles.design_directory(design.placement.design_id).glob("prompts/*.md")), [])
        self.assertIn("busy", errors.getvalue())
        self.assertEqual(self.router.commands, [])
        self.assertEqual(self.world.requests, [])

    def header(self) -> dict[str, object]:
        path = self.saved_prompt()
        first = path.read_text(encoding="utf-8").splitlines()[0]
        self.assertTrue(first.startswith("<!-- astra_work "), first)
        self.assertTrue(first.endswith(" -->"), first)
        self.assertIn(f"Read {path}.\n{path.read_text()}", self.fixture.agent.submitted[-1][1])
        return cast(dict[str, object], json.loads(first[len("<!-- astra_work "):-len(" -->")]))

    def test_standalone_prompt_saves_assignment_identity_and_utc_time(self) -> None:
        result = self.run_script(str(self.document))
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = self.header()
        prompt = self.saved_prompt()
        self.assertEqual({key: value for key, value in saved.items() if key != "assigned_at_utc"}, {
            "schema": 1, "role": "astra-reviewer", "member": str(prompt.parent),
            "document": str(self.document), "reply_path": str(prompt.with_name("review.md")),
            "work": {"kind": "standalone"},
        })
        stamp = datetime.fromisoformat(cast(str, saved["assigned_at_utc"]).replace("Z", "+00:00"))
        self.assertEqual(stamp.utcoffset(), timedelta(0))

    def test_phase_prompt_copies_the_selected_open_phase_and_history_identity(self) -> None:
        unit = self.root / "unit-run"
        unit.mkdir()
        history = self.root / "unit-history.jsonl"
        _ = (unit / "progress_history_state.json").write_text(json.dumps({
            "run_id": "saved-run-id", "history_file": str(history),
            "open_phases": [{"id": "2", "instance_id": "selected-instance", "status": "active"}],
            "phase": {"id": "4", "instance_id": "other-instance", "status": "active"},
        }), encoding="utf-8")
        result = self.run_script("--run-directory", str(unit), "--phase", "2", str(self.document), "scope")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.header()["work"], {
            "kind": "phase", "run_directory": str(unit), "run_id": "saved-run-id", "phase_id": "2",
            "phase_instance_id": "selected-instance", "history_file": str(history),
        })
        self.assertIn("focus on: scope", self.saved_prompt().read_text())

    def test_unresolved_or_incomplete_phase_context_is_refused_before_codex(self) -> None:
        for flags in (("--run-directory", str(self.root), "--phase", "99"),
                      ("--run-directory", str(self.root)), ("--phase", "4")):
            with self.subTest(flags=flags):
                result = self.run_script(*flags, str(self.document))
                self.assertEqual((result.returncode, result.stdout), (2, ""))
                self.assertEqual(len(result.stderr.strip().splitlines()), 1, result.stderr)
                self.assertEqual(self.router.commands, [])
                self.assertEqual(self.world.requests, [])

    def test_the_review_runs_astra_read_only_on_the_workers_account_and_is_printed(self) -> None:
        _ = write_registry(Path(os.environ["MODELS_CONFIG_FILE"]), {
            "assignments": {"design_roles": "claude"},
            "design_roles.claude": {"designer": "sonnet:high", "reviewer": "opus:high"},
        })
        result = self.run_script(str(self.document), "the", "rollout order")
        self.assertEqual(result.returncode, 0, result.stderr)
        family, argv, environment = self.world.requests[-1]
        self.assertEqual(family, "codex")
        for flag, value in (("--model", "gpt-6-astra"), ("--sandbox", "read-only")):
            self.assertEqual(argv[argv.index(flag) + 1], value)
        settings = [argv[index + 1] for index, word in enumerate(argv) if word == "-c"]
        self.assertIn('model_reasoning_effort="xhigh"', settings)
        self.assertFalse(any("model_auto_compact_token_limit" in argument for argument in argv))
        self.assertEqual(argv[argv.index("--ask-for-approval") + 1], "never")
        self.assertEqual(environment["CODEX_HOME"], str(self.root / "codex"))
        self.assertNotIn("STAFF_ID", environment)
        command = self.router.commands[-1]
        self.assertEqual(command[command.index("--cwd") + 1], str(self.root))
        self.assertIn("Verdict: clear.\n\n1. Section Why: the cause is not shown.", result.stdout)
        self.assertIn("workers account codex1", result.stdout)
        review = self.saved_prompt().with_name("review.md")
        self.assertEqual(review.parent.parent, self.root / "runs")
        self.assertEqual(review.read_text().strip(), self.fixture.agent.final)
        self.assertIn(f"Saved: {review}", result.stdout)

    def test_consultation_launch_uses_the_supplied_review_model_and_effort(self) -> None:
        report = self.root / "selected-review.md"
        arguments = ["--role", "review", "--operation", "astra-review", "--target", "consultation-pane",
                     "--policy", "astra-reviewer", "--new-worker", "selected-reviewer",
                     "--prompt", str(self.document), "--summary", str(report),
                     "--model", "gpt-review-fixture", "--effort", "high"]
        result = invoke_cli(assign, assign.main, arguments, self.environment, self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        family, command, _ = self.world.requests[-1]
        self.assertEqual(family, "codex")
        self.assertEqual(command[command.index("--model") + 1], "gpt-review-fixture")
        settings = [command[index + 1] for index, word in enumerate(command) if word == "-c"]
        self.assertIn('model_reasoning_effort="high"', settings)
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertEqual(command[command.index("--ask-for-approval") + 1], "never")
        self.assertEqual(report.read_text().strip(), self.fixture.agent.final)

    def test_consultation_waits_after_idle_until_the_question_and_reply_reach_the_transcript(self) -> None:
        self.fixture.agent.consultation_delivery = "second wait"
        result = self.run_script(str(self.document))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreaterEqual(self.fixture.agent.wait_count, 2)
        review = self.saved_prompt().with_name("review.md")
        self.assertEqual(review.read_text().strip(), self.fixture.agent.final)
        self.assertIn(self.fixture.agent.final, result.stdout)
        self.assertEqual(len(self.world.closed), 1)

    def test_consultation_that_never_accepts_the_question_fails_and_closes_without_a_report(self) -> None:
        self.fixture.agent.consultation_delivery = "never"
        with patch.object(herdr, "PROGRAM_TIMEOUT_SECONDS", 0.05):
            result = self.run_script(str(self.document))
        self.assertEqual(result.returncode, 1)
        self.assertIn("question was not accepted", result.stderr)
        self.assertGreaterEqual(self.fixture.agent.wait_count, 1)
        self.assertLessEqual(self.fixture.agent.wait_count, 2)
        self.assertEqual(self.fixture.agent.replies, [])
        self.assertEqual(list((self.root / "runs").glob("*/review.md")), [])
        self.assertEqual(len(self.world.closed), 1)

    def test_unproved_consultation_close_fails_and_preserves_the_report(self) -> None:
        retained_panes: list[str] = []
        close_results: list[side_pane.SidePaneCloseUnavailable] = []
        close = side_pane.close

        def record_close(label: str) -> side_pane.ClosedSidePane | side_pane.SidePaneCloseUnavailable:
            closed = close(label)
            if isinstance(closed, side_pane.SidePaneCloseUnavailable):
                close_results.append(closed)
            return closed

        def duplicate_terminal(pid: int, number: int) -> None:
            pane = next(pane for pane, process in self.world.foreground.items() if process[0] == pid)
            retained_panes.append(pane)
            self.world.signal(pid, number)
            self.world.locations["w2:p9"] = {**self.world.locations[pane], "pane_id": "w2:p9"}

        with patch.object(os, "kill", side_effect=duplicate_terminal), \
                patch.object(side_pane, "close", side_effect=record_close):
            result = self.run_script(str(self.document))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.saved_prompt().with_name("review.md").read_text().strip(), self.fixture.agent.final)
        self.assertIn("exit 1", result.stderr)
        self.assertEqual(self.world.closed, [])
        self.assertEqual(len(self.world.signals), 1)
        self.assertIn(f"Saved: {self.saved_prompt().with_name('review.md')}", result.stderr)
        self.assertIn(f"assign.sh: consultation close unproved: {close_results[0].reason};", result.stderr)
        label = self.world.locations[retained_panes[0]]["label"]
        self.assertIn(f"pane {retained_panes[0]} ({label}) stays open", result.stderr)

    def test_the_prompt_names_the_document_and_the_focus_and_forbids_building(self) -> None:
        self.assertEqual(self.run_script(str(self.document), "the rollout order").returncode, 0)
        prompt = self.saved_prompt().read_text()
        self.assertIn(str(self.document), prompt)
        self.assertIn("focus on: the rollout order", prompt)
        for asked in ("Clarity", "Correctness", "Gaps", "Risks", "What you would change"):
            self.assertIn(asked, prompt)
        self.assertIn("Do not build, run tests", prompt)
        self.assertIn("section heading", prompt)
        self.assertNotIn("send.py", self.fixture.agent.submitted[-1][1])

    def test_without_a_focus_the_prompt_asks_for_none(self) -> None:
        self.assertEqual(self.run_script(str(self.document)).returncode, 0)
        self.assertNotIn("focus on", self.saved_prompt().read_text())

    def test_a_missing_document_is_refused_before_codex_starts(self) -> None:
        for arguments in ((str(self.root / "absent.md"),), ()):
            with self.subTest(arguments=arguments):
                result = self.run_script(*arguments)
                self.assertEqual((result.returncode, result.stdout), (2, ""))
                self.assertEqual(self.router.commands, [])
                self.assertEqual(self.world.requests, [])
        self.assertIn("no document at", self.run_script(str(self.root / "absent.md")).stderr)

    def test_a_codex_failure_prints_its_error_and_the_account(self) -> None:
        self.fixture.agent.error = "model gpt-6-astra is not available on this plan"
        result = self.run_script(str(self.document))
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertIn("failed on workers account codex1 (exit 8)", result.stderr)
        self.assertIn("model gpt-6-astra is not available", result.stderr)

    def test_a_run_that_writes_no_review_fails(self) -> None:
        self.fixture.agent.final = ""
        result = self.run_script(str(self.document))
        self.assertEqual(result.returncode, 1)
        self.assertIn("no review was written", result.stderr)

    def test_an_unreadable_workers_account_fails_before_codex_starts(self) -> None:
        self.environment["STUB_ROLE_FAILS"] = "1"
        result = self.run_script(str(self.document))
        self.assertEqual(result.returncode, 1)
        self.assertIn("no workers account is set", result.stderr)
        self.assertEqual(self.router.commands, [])
        self.assertEqual(self.world.requests, [])

    def test_consultation_review_has_no_staff_row_during_the_prompt(self) -> None:
        caller = fixture_worker("review-caller", self.fixture.fixture.parent.id,
                                backend=staff.CodexBackend(self.root / "codex"))
        before = staff.snapshot().rows
        for identity in (self.environment["CODEX_THREAD_ID"], str(caller.current_session)):
            with self.subTest(identity=identity):
                self.environment["CODEX_THREAD_ID"] = identity
                self.world.activate(identity, "w1:p0", "codex")
                self.world.commands.clear()
                self.world.report_reads_at_wait.clear()
                self.world.closed.clear()
                during: list[tuple[staff.StaffRow, ...]] = []
                role_records: list[list[Path]] = []

                def observe_roles() -> None:
                    role_records.append(list(astra_roles.state_root().glob("*.json")))

                def observe_prompt() -> None:
                    during.append(staff.snapshot().rows)
                    observe_roles()

                self.fixture.agent.during_prompt = observe_prompt
                self.fixture.agent.after_complete = lambda pane: observe_roles()
                result = self.run_script(str(self.document))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(during, [before])
                self.assertEqual(staff.snapshot().rows, before)
                self.assertEqual(role_records, [[], []])
                with patch.dict(os.environ, self.environment):
                    self.assertEqual(list(astra_roles.state_root().glob("*.json")), [])
                self.assertIn(self.fixture.agent.final, result.stdout)
                self.assertEqual(self.router.worker_commands, [])
                command = self.router.commands[-1]
                self.assertEqual(command[command.index("--target") + 1], "consultation-pane")
                self.assertEqual(command[command.index("--policy") + 1], "astra-reviewer")
                self.assertEqual([entry[:2] for entry in self.world.commands
                                  if entry[:2] in (("agent", "prompt"), ("agent", "wait"), ("pane", "close"))],
                                 [("agent", "prompt"), ("agent", "wait"), ("pane", "close")])
                self.assertEqual(self.world.report_reads_at_wait, [0])
                self.assertEqual(len(self.world.closed), 1)
                self.assertIn("w1:p0", self.world.locations)

    def test_staff_review_uses_its_staff_target_and_saves_the_report(self) -> None:
        self.environment["CODEX_THREAD_ID"] = str(self.fixture.fixture.parent.current_session)
        self.world.activate(self.environment["CODEX_THREAD_ID"], "w1:p0", "codex")
        self.fixture.agent.without_done = False
        self.fixture.agent.finish_after = 1
        with self.world.process_starts():
            result = self.run_script(str(self.document))
        self.assertEqual(result.returncode, 0, result.stderr)
        command = self.router.commands[-1]
        self.assertEqual(command[command.index("--target") + 1], "staff-worker")
        self.assertEqual(command[command.index("--policy") + 1], "astra-reviewer")
        review = self.saved_prompt().with_name("review.md")
        self.assertFalse(review.exists())
        self.assertIn(f"Submitted: {review}", result.stdout)
        self.assertNotIn(self.fixture.agent.final, result.stdout)
        pending = self.run_script("--read", str(review), "--run-directory", str(review.parent))
        self.assertEqual(pending.returncode, 1)
        self.assertIn("not completed", pending.stderr)
        pane = self.fixture.agent.submitted[-1][0]
        row = self.fixture.agent.row(pane)
        self.fixture.agent.complete(pane)
        with patch.dict(os.environ, self.environment):
            self.assertEqual(self.fixture.finish(row.name), turn_end.TurnOutcome.DONE)
        saved = self.run_script("--read", str(review), "--run-directory", str(review.parent))
        self.assertEqual(saved.returncode, 0, saved.stderr)
        self.assertEqual(review.read_text().strip(), self.fixture.agent.replies[-1])
        self.assertIn(self.fixture.agent.replies[-1], saved.stdout)
        self.assertEqual(self.world.requests[-1][1][self.world.requests[-1][1].index("--sandbox") + 1], "read-only")

    def test_showrunner_review_opens_a_worker_under_the_calling_showrunner(self) -> None:
        record = staff.snapshot()
        owner = record.parent_of(self.fixture.fixture.parent)
        assert owner is not None
        self.environment["CODEX_THREAD_ID"] = str(owner.current_session)
        source, _ = transcript_fixture(owner, staff.CodexBackend(Path.home() / ".codex"))
        with source.open("a") as output:
            _ = output.write(json.dumps({"type": "turn_context", "payload": {
                "model": "gpt-main", "effort": "high"}}) + "\n")
        self.world.activate(str(owner.current_session), "w1:p0", "codex")
        self.fixture.agent.without_done = False
        published: list[staff.StaffRow] = []
        def observe() -> None:
            published.extend(staff.snapshot().find(kind="worker", parent=owner.id))
        self.fixture.agent.during_prompt = observe
        with self.world.process_starts():
            result = self.run_script(str(self.document))
        self.assertEqual(result.returncode, 0, result.stderr)
        reviewer, = published
        with patch.dict(os.environ, self.environment):
            event = turn_end.WorkerEvent(str(reviewer.current_session), reply=self.fixture.agent.replies[-1])
            self.assertEqual(turn_end.handle(event), turn_end.TurnOutcome.DONE)
        self.assertEqual(reviewer.parent, staff.ReportsTo(owner.id))
        self.assertEqual(len(self.world.launches), 1)
        self.assertEqual(self.router.commands[-1][self.router.commands[-1].index("--target") + 1], "staff-worker")
        self.assertEqual(self.saved_prompt().with_name("review.md").read_text().strip(), self.fixture.agent.replies[-1])


if __name__ == "__main__":
    _ = unittest.main()
