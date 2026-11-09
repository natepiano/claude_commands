"""Named assignment dispatches through the real recorder and loopback app-server."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict, cast, final, override
from unittest.mock import patch

from scripts.agents import codex_test_support as codex_fixture
from scripts.direct import assign, progress_history
from scripts.direct.assign_claude_test_support import AssignClaudeLauncherFixture
from scripts.direct.assign_launcher_test_support import ASSIGN, ROOT, AssignLauncherFixture, InProcessAssignment, invoke_assignment, read_object
from scripts.direct.findings_test_support import invoke_findings
from scripts.herdr import herdr
from scripts.lint import style_review
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.test_models_registry import write_registry
from scripts.production import staff, test_worker as worker_fixture, worker, worker_admission
from scripts.production.cli_fixture import invoke_cli
from scripts.production.staff_test_support import fixture_session, fixture_runtime_id, fixture_worker, transcript_fixture
from scripts.message import sessions
from scripts.shutdown import stop_resume
from scripts.usage import agents_log

def setUpModule() -> None:
    codex_fixture.setUpModule()


def fail_worker_after_prompt() -> None:
    raise SystemExit(1)


class RepairState(TypedDict):
    rounds: list[dict[str, object]]
    findings: list[dict[str, object]]
    in_flight: list[str]


@final
class FailedLaunchRunTests(AssignLauncherFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "UNIT_RUN_ROOT": str(self.session_dir.parent),
            "SCRATCH_ROOT": str(self.session_dir.parent / "scratch"),
        })))
        for key in ("CLAUDE_CODE_SESSION_ID", "UNIT_RUN_DIR", "PLAN_DELEGATE_RUN_DIR"):
            _ = os.environ.pop(key, None)
        self.marker().parent.mkdir(parents=True, exist_ok=True)
        _ = self.marker().write_text(f"{self.session_dir}\n")
        directory = Path(os.environ["NOTIFIER_SESSIONS_DIR"])
        directory.mkdir(parents=True, exist_ok=True)
        _ = (directory / "director.json").write_text(json.dumps({
            "pid": os.getpid(), "sessionId": staff.runtime_id(), "name": "fixture director",
            "messagingSocketPath": "", "updatedAt": 1, "procStart": sessions.process_start(os.getpid()),
        }))

    def marker(self) -> Path:
        return Path(os.environ["UNIT_ACTIVE_DIR"]) / staff.runtime_id()

    def failing_worker(self, code: int = 2, *, recursive_history: bool = False, after_pass_start: bool = False) -> Path:
        """Run a failing child at the worker boundary of the real launcher."""
        command = self.session_dir.parent / "worker-python"
        calls = self.session_dir.parent / "failed-worker-commands.jsonl"
        existing = next(iter(self.assignment_records().values())) if after_pass_start else {}
        _ = command.write_text(f"#!{sys.executable}\n" + "\n".join((
            "import json, os, sys",
            "from pathlib import Path",
            "arguments = sys.argv[1:]",
            "if len(arguments) > 1 and Path(arguments[0]).name == 'worker.py' and arguments[1] == 'assign':",
            f"    with Path({str(calls)!r}).open('a') as output:",
            "        output.write(json.dumps(arguments) + '\\n')",
            *(("    from scripts.direct import progress_history",
               "    from scripts.production import staff",
               "    def option(flag): return arguments[arguments.index(flag) + 1]",
               f"    progress_history.begin_dispatch(Path(option('--session-dir')), slot=option('--slot'), phase=option('--phase'), role=arguments[2], worker_name=option('--name'), worker_id=staff.StaffId({str(existing.get('staff_id', ''))!r}), launcher_command=json.loads(option('--launcher-command')), prompt=Path(arguments[3]), kind=option('--kind'), lens=option('--lens'))")
              if after_pass_start else ()),
            "    print('The worker command failed at launch.', file=sys.stderr)",
            f"    raise SystemExit({code})",
            f"os.execv({assign.PYTHON!r}, [{assign.PYTHON!r}, *arguments])",
        )) + "\n")
        command.chmod(0o755)
        self.instrument_launcher("\n".join((
            "from pathlib import Path",
            "from scripts.direct import assign",
            f"assign.PYTHON = {str(command)!r}",
            "prepare_run = assign.prepare_run",
            "def prepare(*arguments):",
            "    run, phase = prepare_run(*arguments)",
            "    return run, phase",
            "assign.prepare_run = prepare",
            *(("real_cleanup = assign.execute_herdr",
               "def cleanup(*args):",
               "    code = real_cleanup(*args)",
               "    history = assign.read_object(args[1] / 'progress_history_state.json')['history_file']",
               "    with Path(history).open('a') as output:",
               "        output.write('[' * 10000 + '0' + ']' * 10000 + '\\n')",
               "    return code",
               "assign.execute_herdr = cleanup") if recursive_history else ()),
        )))
        return calls

    def assert_run_ended(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertFalse(self.marker().exists(), result.stdout + result.stderr)
        self.assertEqual(read_object(self.session_dir / "progress_history_state.json")["status"], "error")
        finished = [event for event in self.events() if event.get("event_type") == "run_finished"]
        self.assertEqual([event["status"] for event in finished], ["error"])
        self.assertIn("run ended", result.stderr.lower())

    def assert_run_active(self, result: subprocess.CompletedProcess[str], code: int = 2) -> None:
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertEqual(self.marker().read_text().strip(), str(self.session_dir))
        self.assertEqual(read_object(self.session_dir / "progress_history_state.json")["status"], "active")
        self.assertEqual([event for event in self.events() if event.get("event_type") == "run_finished"], [])

    def test_a_codex_directors_only_failed_worker_ends_its_run(self) -> None:
        calls = self.failing_worker()
        result = self.launch()
        self.assert_run_ended(result)
        command = cast(list[str], json.loads(calls.read_text()))
        self.assertEqual(command[1], "assign")
        self.assertEqual(self.native.fixture.world.launches, [])

    def test_a_claude_session_marker_is_removed_after_its_only_worker_fails(self) -> None:
        _ = self.failing_worker()
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": staff.runtime_id()}):
            self.assert_run_ended(self.launch())

    def assert_prepared_marker_is_removed(self) -> None:
        active = self.session_dir.parent / "unit" / "active"
        with patch.dict(os.environ, {"UNIT_ACTIVE_DIR": str(active)}):
            prepared = self.run_launcher(["bash", str(ROOT / "scripts/direct/prepare_session.sh")])
            self.assertEqual(prepared.returncode, 0, prepared.stdout + prepared.stderr)
            run = Path(prepared.stdout.split("Session ready at ")[-1].strip())
            marker = active / staff.runtime_id()
            self.assertEqual(marker.read_text().strip(), str(run), prepared.stdout + prepared.stderr)
            ended = self.run_launcher(["bash", str(ROOT / "scripts/direct/end_session.sh")])
            self.assertEqual(ended.returncode, 0, ended.stdout + ended.stderr)
            self.assertFalse(marker.exists(), ended.stdout + ended.stderr)
            self.assertIn("marker cleared", ended.stdout)

    def test_prepare_and_end_use_the_codex_id_when_the_claude_id_differs(self) -> None:
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": fixture_runtime_id("other-director")}):
            self.assert_prepared_marker_is_removed()

    def test_prepare_and_end_use_the_codex_id_without_a_claude_id(self) -> None:
        self.assert_prepared_marker_is_removed()


    def test_a_sole_claude_implementation_failure_ends_the_run_after_its_pass_starts(self) -> None:
        _ = write_registry(self.registry, {"assignments": {"direct": "claude"},
                                          "direct.claude": {"impl": "opus:high"}}, update=True)
        self.native.agent.during_prompt = fail_worker_after_prompt
        result = self.launch()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertFalse(self.marker().exists(), result.stdout + result.stderr)
        self.assertEqual(read_object(self.session_dir / "progress_history_state.json")["status"], "error")
        started = [event for event in self.events() if event.get("event_type") == "pass_started"]
        self.assertEqual([event["worker_name"] for event in started], ["impl"])
        finished = [event for event in self.events() if event.get("event_type") == "run_finished"]
        self.assertEqual([event["status"] for event in finished], ["error"])
        self.assertEqual(self.native.fixture.world.requests[-1][0], "claude")
        self.assertIn("run ended", result.stderr.lower())


    def test_a_claude_slot_refusal_before_its_pass_starts_keeps_the_run(self) -> None:
        _ = write_registry(self.registry, {"assignments": {"direct": "claude"},
                                          "direct.claude": {"impl": "opus:high"}}, update=True)
        calls = self.failing_worker()
        result = self.launch("implement", "--new-worker", "invalid/name")
        self.assert_run_active(result)
        self.assertIn("invalid worker name", result.stderr)
        self.assertEqual([event for event in self.events() if event.get("event_type") == "pass_started"], [])
        self.assertFalse(calls.exists())

    def assert_unnamed_review_keeps_run(self, *, finished: bool) -> None:
        self.record("start-phase", "--phase-id", "8", "--phase-title", "Parallel review")
        prompt = self.session_dir.parent / "parallel-review.txt"
        _ = prompt.write_text("Phase 8 — Parallel review\nReview the assigned change.\n")
        with patch.dict(os.environ, {"UNIT_TEAM_ROLE": "other-review", "UNIT_PASS_OWNER": "launcher"}):
            self.record("start-pass", "--pass-kind", "review", "--phase-id", "8",
                        "--activity", "Parallel review",
                        "--prompt-file", str(prompt), "--called-task", "direct.review",
                        "--called-family", "claude", "--called-model", "opus")
            if finished:
                self.record("finish-pass", "--status", "error")
        state = read_object(self.session_dir / "progress_history_state.json")
        review = assign.object_map(state["pass"])["other-review"]
        self.assertFalse(assign.object_map(review)["worker_name"])
        _ = self.failing_worker()
        self.assert_run_active(self.launch())
        after = read_object(self.session_dir / "progress_history_state.json")
        self.assertEqual(assign.object_map(after["pass"])["other-review"], review)

    def test_an_open_review_without_a_worker_name_keeps_the_run(self) -> None:
        self.assert_unnamed_review_keeps_run(finished=False)

    def test_a_finished_review_without_a_worker_name_keeps_the_run(self) -> None:
        self.assert_unnamed_review_keeps_run(finished=True)

    def test_a_recursive_history_keeps_the_worker_exit_code_and_reports_one_cleanup_error(self) -> None:
        calls = self.failing_worker(recursive_history=True)
        result = self.launch()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(len(calls.read_text().splitlines()), 1)
        self.assertEqual(self.marker().read_text().strip(), str(self.session_dir))
        self.assertEqual(read_object(self.session_dir / "progress_history_state.json")["status"], "active")
        diagnostics = [line for line in result.stderr.splitlines() if "unable to end failed run" in line]
        self.assertEqual(len(diagnostics), 1, result.stderr)
        self.assertIn("recursion", diagnostics[0])
        self.assertNotIn("Traceback", result.stderr)

    def test_a_completed_pass_keeps_the_run_after_a_later_launch_failure(self) -> None:
        _ = self.successful()
        name = next(iter(self.assignment_records()))
        _ = self.failing_worker(after_pass_start=True)
        self.assert_run_active(self.launch("implement", "--to", name))
        self.assertEqual(len(self.assignment_records()), 1)
        finished = [event for event in self.events() if event.get("event_type") == "pass_finished"]
        self.assertEqual([event["status"] for event in finished], ["completed", "error"])

    def test_a_second_worker_keeps_the_run_after_a_launch_failure(self) -> None:
        other = self.native.fixture.worker("other-test")
        _ = self.failing_worker()
        self.assert_run_active(self.launch())
        self.assertIn(other.name, self.assignment_records())
        self.assertEqual(staff.by_id(other.id), other)

    def test_a_second_held_run_keeps_both_runs_after_a_launch_failure(self) -> None:
        other = self.session_dir.parent / "other-run"
        other.mkdir()
        state = self.session_dir / "progress_history_state.json"
        _ = (other / state.name).write_bytes(state.read_bytes())
        before = (other / state.name).read_bytes()
        _ = self.failing_worker()
        self.assert_run_active(self.launch())
        self.assertEqual((other / state.name).read_bytes(), before)

    def test_a_herdr_exit_that_keeps_a_worker_keeps_the_run(self) -> None:
        self.commit_project()
        calls = self.failing_worker(7)
        self.assert_run_active(self.launch(), 7)
        command = cast(list[str], json.loads(calls.read_text()))
        self.assertEqual(command[1], "assign")
        self.assertEqual(self.native.fixture.world.launches, [])

    def test_a_refusal_before_the_worker_command_keeps_the_run(self) -> None:
        calls = self.failing_worker()
        self.assert_run_active(self.launch(paths=False))
        self.assertFalse(calls.exists())
        self.assertEqual(self.native.fixture.world.launches, [])

    def test_a_retry_without_a_run_starts_a_new_run_after_launch_failure(self) -> None:
        self.commit_project()
        calls = self.failing_worker()
        self.assert_run_ended(self.launch())
        director = staff.runtime_id()
        transcript = self.home / ".codex/sessions" / f"rollout-{director}.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        _ = transcript.write_text("\n".join(json.dumps(record) for record in (
            {"type": "session_meta", "payload": {"id": director}},
            {"type": "turn_context", "payload": {"model": "fixture-director", "effort": "high"}},
        )) + "\n")
        result = self.launch(session=False)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        commands = [cast(list[str], json.loads(line)) for line in calls.read_text().splitlines()]
        runs = [Path(command[command.index("--session-dir") + 1]) for command in commands]
        self.assertEqual(runs[0], self.session_dir)
        self.assertNotEqual(runs[1], self.session_dir)
        self.assertEqual(read_object(runs[1] / "progress_history_state.json")["owner_staff_id"],
                         read_object(self.session_dir / "progress_history_state.json")["owner_staff_id"])

    def test_a_retry_with_the_ended_run_still_runs_the_worker_command(self) -> None:
        calls = self.failing_worker()
        self.assert_run_ended(self.launch())
        result = self.launch()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        commands = [cast(list[str], json.loads(line)) for line in calls.read_text().splitlines()]
        self.assertEqual([command[command.index("--session-dir") + 1] for command in commands],
                         [str(self.session_dir), str(self.session_dir)])


@final
class PaneReviewLauncherTests(unittest.TestCase):
    """The assignment launcher uses the real dispatch and its transcript receipts."""

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.fixture = worker_fixture.WorkerCommandTests("runTest")
        self.router = worker_fixture.PaneCommandRouter()
        self.ready = Path()
        self.phase = Path()

    @override
    def setUp(self) -> None:
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.ready = self.fixture.run_dir / "final.ready"
        self.phase = self.fixture.run_dir / "phases/8"
        write_text_atomic(self.fixture.prompt, "Phase 8 — Workers\nReview Stage 1, then review the final diff in Stage 2.\n")
        opened = self.fixture.opened("impl", "impl")
        _ = progress_history.begin_dispatch(self.fixture.run_dir, slot="impl", phase="8", role="impl",
            worker_name="impl", worker_id=opened.row.id, launcher_command=["previous-launcher"], prompt=self.fixture.prompt, kind="impl", lens="")
        self.router.setup(self, self.fixture.fixture.root)

    def launch_review(self) -> subprocess.CompletedProcess[str]:
        arguments = ["--role", "review", "--to", "review", "--cwd", str(self.fixture.fixture.root),
                     "--phase", "8", "--session-dir", str(self.fixture.run_dir),
                     "--prompt", str(self.fixture.prompt), "--early-ready", str(self.ready)]
        return invoke_cli(assign, assign.main, arguments, os.environ.copy(), ROOT)

    def test_durable_design_dispatch_without_a_summary_saves_the_reply_beside_the_prompt(self) -> None:
        arguments = ["--role", "review", "--target", "durable-design-role", "--policy", "astra-reviewer",
                     "--parent", str(self.fixture.fixture.parent.id), "--new-worker", "TEST-astra-reviewer",
                     "--cwd", str(self.fixture.fixture.root), "--phase", "8",
                     "--session-dir", str(self.fixture.run_dir), "--prompt", str(self.fixture.prompt)]
        result = invoke_cli(assign, assign.main, arguments, os.environ.copy(), ROOT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        reply = self.fixture.prompt.with_name(self.fixture.prompt.stem + "_reply.txt")
        self.assertTrue(reply.read_text().startswith(self.fixture.agent.final))
        self.assertEqual(len(self.fixture.agent.submitted), 1)

    def test_review_stage_two_uses_its_own_done_line_and_reuses_both_receipts(self) -> None:
        reports: list[str] = []
        summary = self.phase / "review_summary_1.txt"
        summary.parent.mkdir(parents=True)
        write_text_atomic(summary, "The previous full report.\n")

        def final_diff_ready() -> None:
            if len(self.fixture.agent.submitted) == 1:
                write_text_atomic(self.ready.with_suffix(".diff"), "+the final change\n")
                write_text_atomic(self.ready, "ready\n")
            else:
                reports.append(summary.read_text())

        self.fixture.agent.during_prompt = final_diff_ready
        prompt = self.fixture.prompt.read_bytes()
        result = self.launch_review()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        first, second = self.fixture.agent.submitted
        self.assertEqual(first[0], second[0])
        dispatch_id = first[1].split("DONE ")[-1].split(".")[0]
        self.assertIn(f"DONE {dispatch_id}-2.", second[1])
        self.assertIn(str(self.ready.with_suffix(".diff")), second[1])
        self.assertEqual(reports, ["The previous full report.\n"])
        self.assertTrue(summary.read_text().endswith(f"DONE {dispatch_id}-2\n"))
        self.assertEqual((self.phase / "review_status_1").read_text(), "done\n")
        panes = self.fixture.fixture.world.locations.copy()
        repeated = self.launch_review()
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertEqual(self.fixture.agent.submitted, [first, second])
        self.assertEqual(self.fixture.fixture.world.locations, panes)
        self.assertEqual(self.fixture.prompt.read_bytes(), prompt)

    def test_review_stage_two_retry_waits_for_the_submitted_turn_without_another_prompt(self) -> None:
        def interrupt_stage_two() -> None:
            if len(self.fixture.agent.submitted) == 1:
                write_text_atomic(self.ready, "ready\n")
            else:
                pane = self.fixture.agent.submitted[-1][0]
                self.fixture.fixture.world.locations[pane]["agent_status"] = "working"
                self.fixture.agent.interrupt = "prompt"
                self.fixture.agent.finish_after = 1

        self.fixture.agent.during_prompt = interrupt_stage_two
        first = self.launch_review()
        self.assertNotEqual(first.returncode, 0, first.stdout + first.stderr)
        submitted = self.fixture.agent.submitted.copy()
        self.assertEqual(len(submitted), 2)
        self.fixture.agent.during_prompt = lambda: None
        repeated = self.launch_review()
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertEqual(self.fixture.agent.submitted, submitted)
        self.assertEqual(self.fixture.agent.waited, [submitted[-1][0]])
        second_id = submitted[-1][1].split("DONE ")[-1].split(".")[0]
        self.assertTrue((self.phase / "review_summary_1.txt").read_text().endswith(f"DONE {second_id}\n"))

    def test_review_stage_one_done_does_not_complete_stage_two_or_replace_the_report(self) -> None:
        def omit_stage_two_done() -> None:
            if len(self.fixture.agent.submitted) == 1:
                write_text_atomic(self.ready, "ready\n")
            else:
                self.fixture.agent.without_done = True

        self.fixture.agent.during_prompt = omit_stage_two_done
        summary = self.phase / "review_summary_1.txt"
        summary.parent.mkdir(parents=True)
        write_text_atomic(summary, "The previous full report.\n")
        result = self.launch_review()
        self.assertEqual(result.returncode, worker.ExitCode.NO_DONE, result.stdout + result.stderr)
        self.assertEqual(len(self.fixture.agent.submitted), 2)
        self.assertEqual(summary.read_text(), "The previous full report.\n")
        self.assertEqual((self.phase / "review_status_1").read_text(), "error\n")
        state = read_object(self.fixture.run_dir / "progress_history_state.json")
        self.assertEqual(assign.object_map(assign.object_map(state["pass"])["review"])["status"], "active")

    def assert_start_not_delivered(self, code: worker.ExitCode, reason: str) -> None:
        result = self.launch_review()
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertIn(reason, (self.phase / "review_agent_1.log").read_text())
        self.assertEqual(self.fixture.agent.submitted, [])
        self.assertEqual((self.phase / "review_status_1").read_text(), "error\n")

    def test_review_incomplete_start_keeps_the_pane_without_either_stage(self) -> None:
        self.fixture.fixture.world.outcome = herdr.PromptNotSubmitted("startup hook failed")
        self.assert_start_not_delivered(worker.ExitCode.UNCERTAIN, "publication failed: startup hook failed")
        self.assertEqual(len(self.fixture.fixture.world.launches), 2)
        self.assertEqual(self.fixture.fixture.world.closed, [])

    def test_review_failed_publication_keeps_the_backend_without_either_stage(self) -> None:
        rows = staff.snapshot().find(kind="worker", parent=self.fixture.fixture.parent.id)
        with patch.object(staff, "ensure_worker", side_effect=staff.StaffRefused("publication unavailable")):
            self.assert_start_not_delivered(worker.ExitCode.UNCERTAIN, "publication refused: publication unavailable")
        self.assertIn("review: publication failed: staff: review:", (self.phase / "review_agent_1.log").read_text())
        self.assertEqual(staff.snapshot().find(kind="worker", parent=self.fixture.fixture.parent.id), rows)
        self.assertEqual(len(self.fixture.fixture.world.launches), 2)
        self.assertEqual(self.fixture.fixture.world.closed, [])

    def test_review_admission_failure_sends_neither_stage(self) -> None:
        launches = self.fixture.fixture.world.launches.copy()
        with patch.object(worker_admission, "worker_operation", side_effect=worker_admission.AdmissionRefused("admission unavailable")):
            self.assert_start_not_delivered(worker.ExitCode.REFUSED, "staff: review: not opened: admission unavailable")
        self.assertEqual(self.fixture.fixture.world.launches, launches)

    def test_review_unknown_process_keeps_the_worker_without_either_stage(self) -> None:
        opened = self.fixture.opened("review", "review")
        self.fixture.fixture.world.unknown[opened.pane] = "native reader unavailable"
        before = self.fixture.fixture.staff_state()
        self.assert_start_not_delivered(worker.ExitCode.UNCERTAIN, "native reader unavailable")
        self.assertEqual(self.fixture.fixture.staff_state(), before)

    def test_review_missing_terminal_keeps_the_worker_without_either_stage(self) -> None:
        opened = self.fixture.opened("review", "review")
        self.fixture.fixture.world.locations[opened.pane]["terminal_id"] = ""
        before = self.fixture.fixture.staff_state()
        self.assert_start_not_delivered(worker.ExitCode.UNCERTAIN, "terminal")
        self.assertEqual(self.fixture.fixture.staff_state(), before)

    def test_review_role_refusal_keeps_the_active_worker_and_types_neither_stage(self) -> None:
        opened = self.fixture.opened("review", "impl")
        _ = progress_history.begin_dispatch(self.fixture.run_dir, slot="review", phase="8", role="impl",
            worker_name="review", worker_id=opened.row.id, launcher_command=["previous-launcher"], prompt=self.fixture.prompt, kind="impl", lens="")
        before = self.fixture.fixture.staff_state()
        self.assert_start_not_delivered(worker.ExitCode.REFUSED, "role change blocked by active work: active dispatch has another role")
        self.assertEqual(self.fixture.fixture.staff_state(), before)
        self.assertIn(opened.pane, self.fixture.fixture.world.foreground)
        self.assertEqual(self.fixture.fixture.world.stops, [])


@final
class HerdrClaimFixture(AssignClaudeLauncherFixture):
    """Use the shared claim fixture with the active launcher environment."""

    @override
    def environment(self) -> dict[str, str]:
        return os.environ.copy()


@final
class AssignLauncherTests(AssignLauncherFixture):
    def test_claude_worker_returns_through_start_and_then_follows_its_codex_thread(self) -> None:
        self.commit_project()
        cases = ((False, True), (True, True), (True, False))
        for source_live, named in cases:
            with self.subTest(source_live=source_live, named=named):
                name = "impl"
                self.prompt = self.session_dir.parent / f"switch-{source_live}-{named}.md"
                current = worker_admission.find_worker(staff.snapshot(), self.native.fixture.parent.id, name)
                world = self.native.fixture.world
                if current is not None:
                    for pane, (pid, _, _) in tuple(world.foreground.items()):
                        if world.processes[pid].identity.id == current.current_session:
                            world.end_agent(pane)
                source_identity = staff.native_id(fixture_runtime_id(f"source-{source_live}-{named}"))
                fixture_session(source_identity, staff.ClaudeBackend())
                row = (staff.update_current_session(current.current_session, source_identity) if current is not None else
                       fixture_worker(name, self.native.fixture.parent.id, runtime_id=source_identity,
                                      backend=staff.ClaudeBackend()))
                source, evidence = transcript_fixture(row, staff.ClaudeBackend())
                self.native.fixture.world.add(row, "w1:p1", family="claude", live=source_live)
                agents_log.append(row.current_session, "impl", None, datetime.now(timezone.utc))
                self.write_phase_prompt("7", "Assignment core", f"Switch the private worker {source_live} {named}.")
                source_pass = progress_history.begin_dispatch(self.session_dir, slot=name, phase="7", role="impl",
                    worker_name=name, worker_id=row.id, launcher_command=["source-launcher"], prompt=self.prompt,
                    kind="assign", lens="")
                with source.open("a") as stream:
                    _ = stream.write(json.dumps({"type": "assistant", "message": {
                        "role": "assistant", "content": "DONE " + source_pass}}) + "\n")
                with patch.dict(os.environ, {"UNIT_TEAM_ROLE": name, "UNIT_PASS_OWNER": "launcher"}):
                    self.record("finish-pass", "--status", "completed")
                image = self.session_dir.parent / "attached.png"
                image.touch()
                arguments = ["--role", "implement", "--cwd", str(self.project), "--phase", "7",
                             "--prompt", str(self.prompt), "--paths", str(self.paths), "--session-dir", str(self.session_dir),
                             "--image", str(image)]
                if named:
                    arguments += ["--to", name]
                def stop_source(reference: str, kind: str = "") -> stop_resume.Stopped | stop_resume.NotRunning:
                    del kind
                    source_row = staff.snapshot().by_member_reference(reference)
                    assert source_row is not None
                    world = self.native.fixture.world
                    pid = next((pid for pid, _, _ in world.foreground.values()
                                if world.processes[pid].current_identity == source_row.current_session), 0)
                    _ = world.stop(source_row, world)
                    return (stop_resume.Stopped(str(source_row.current_session), pid) if pid else
                            stop_resume.NotRunning(str(source_row.current_session)))

                with patch.object(stop_resume, "stop", side_effect=stop_source):
                    result = invoke_assignment(arguments, os.environ.copy())
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr +
                                 "\n".join(path.read_text() for path in (self.session_dir / "phases/7").glob("*_agent.log")))
                moved = staff.by_id(row.id)
                assert moved is not None
                self.assertNotEqual(moved.current_session, row.current_session)
                self.assertIsInstance(staff.backend(moved), staff.CodexBackend)
                self.assertEqual(moved.parent, row.parent)
                self.assertEqual(moved.to_json() | {"current_session": row.current_session}, row.to_json())
                self.assertNotEqual(self.native.agent.submitted[-1][0], "w1:p1")
                self.assertIsInstance(world.launches[-1], herdr.SplitPane)
                extract = worker_admission.handoff_path(self.session_dir, row)
                self.assertIn(str(source), extract.read_text())
                self.assertIn(evidence[0][0], extract.read_text())
                self.assertIn(str(extract), self.native.agent.submitted[-1][1])
                passed = assign.object_map(assign.object_map(read_object(
                    self.session_dir / "progress_history_state.json")["pass"])[name])
                self.assertIn(str(image.resolve()), Path(str(passed["prompt_file"])).read_text())
                before = moved.to_json(), len(self.native.fixture.world.launches)
                _ = self.successful("implement", "--to", name)
                current = staff.by_id(row.id)
                assert current is not None
                self.assertEqual((current.to_json(), len(self.native.fixture.world.launches)), before)


    def test_incomplete_dispatch_rerun_keeps_its_pass_and_only_waits(self) -> None:
        self.native.agent.without_done = True
        first = self.launch()
        self.assertEqual(first.returncode, worker.ExitCode.NO_DONE, first.stdout + first.stderr)
        state = read_object(self.session_dir / "progress_history_state.json")
        passed = assign.object_map(assign.object_map(state["pass"])["impl"])
        self.assertEqual(passed["status"], "active")
        identity = passed["instance_id"]
        submitted = self.native.agent.submitted.copy()
        self.assertEqual(len(submitted), 1)
        pane = submitted[0][0]
        self.native.fixture.world.locations[pane]["agent_status"] = "working"
        self.native.agent.without_done = False
        self.native.agent.finish_after = 1
        repeated = self.launch()
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        state = read_object(self.session_dir / "progress_history_state.json")
        finished = assign.object_map(assign.object_map(state["pass"])["impl"])
        self.assertEqual((finished["instance_id"], finished["status"]), (identity, "completed"))
        self.assertEqual(self.native.agent.submitted, submitted)
        self.assertEqual(self.native.agent.waited, [pane])
        self.assertEqual(sum(event.get("event_type") == "pass_started" for event in self.events()), 1)

    def test_unnamed_assignment_finishes_the_selected_workers_pass_and_artifacts(self) -> None:
        opened = self.native.opened("impl2")
        dispatch = progress_history.begin_dispatch(self.session_dir, slot="impl2", phase="7", role="impl",
            worker_name="impl2", worker_id=opened.row.id, launcher_command=["previous-launcher"],
            prompt=self.prompt, kind="assign", lens="")
        self.native.fixture.reply_to_dispatch(opened.row, dispatch)
        with patch.dict(os.environ, {"UNIT_TEAM_ROLE": "impl2", "UNIT_PASS_OWNER": "launcher"}):
            self.record("finish-pass", "--status", "completed")
        arguments = ["--role", "implement", "--cwd", str(self.project), "--phase", "7",
                     "--prompt", str(self.prompt), "--paths", str(self.paths), "--session-dir", str(self.session_dir)]
        result = invoke_assignment(arguments, os.environ.copy())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = read_object(self.session_dir / "progress_history_state.json")
        passed = assign.object_map(assign.object_map(state["pass"])["impl"])
        self.assertEqual((passed["worker_name"], passed["worker_id"], passed["status"]),
                         ("impl2", opened.row.id, "completed"))
        phase = self.session_dir / "phases/7"
        self.assertEqual((phase / "impl2_status").read_text(), "done\n")
        self.assertIn(self.native.agent.final, (phase / "impl2_summary.txt").read_text())
        self.assertIn("done", (phase / "impl2_agent.log").read_text())
        self.assertEqual(read_object(phase / "dispatch_2_paths.json")["worker"], "impl2")
        self.assertEqual(staff.by_id(opened.row.id), opened.row)

    def test_assignment_reads_current_model_settings_for_the_next_launch(self) -> None:
        _ = self.successful("implement", "--new-worker", "first")
        first = self.native.fixture.world.requests[-1][1]
        self.assertEqual(first[first.index("--model") + 1], "gpt-kept-sol")
        _ = write_registry(self.registry, {"direct.codex": {"impl": "gpt-other-sol:max"}}, update=True)
        _ = self.successful("implement", "--new-worker", "second")
        second = self.native.fixture.world.requests[-1][1]
        self.assertEqual(second[second.index("--model") + 1], "gpt-other-sol")
        self.assertIn('model_reasoning_effort="max"', second)
        self.assertEqual(self.turn_starts, 2)

    def test_native_assignment_retains_unknown_workers(self) -> None:
        self.commit_project()
        before = Path(os.environ["STAFF_FILE"]).read_bytes()
        prompt = self.prompt.read_bytes()
        self.native.fixture.world.unreadable = True
        with patch.dict(os.environ, {"HERDR_BIN": "/nonexistent/herdr"}):
            result = self.launch(execution=InProcessAssignment())
        self.assertEqual(result.returncode, 8, result.stdout + result.stderr)
        self.assertEqual(self.native.fixture.world.launches, [])
        self.assertEqual(self.native.agent.submitted, [])
        self.assertEqual(Path(os.environ["STAFF_FILE"]).read_bytes(), before)
        self.assertEqual(self.prompt.read_bytes(), prompt)
        self.assertEqual((self.session_dir / "phases/7/impl_status").read_text(), "error\n")

    def herdr_claim_launch(self, claim_status: str, role: str = "implement") -> subprocess.CompletedProcess[str]:
        self.commit_project()
        self.record("start-phase", "--phase-id", "3", "--phase-title", "Claim before assignment")
        write_text_atomic(self.prompt, "Phase 3 — Claim before assignment\nImplement the assigned change.\n")
        write_text_atomic(self.session_dir / "dispatch_events.txt", "")
        environment, calls = HerdrClaimFixture().claim_fixture(self.session_dir, claim_status)
        environment["HERDR_BIN"] = "/nonexistent/herdr"
        self.native.fixture.world.unreadable = True
        with patch.dict(os.environ, environment):
            self.instrument_launcher("\n".join((
                "from pathlib import Path",
                "from scripts.direct import assign",
                "from scripts.lib.atomic_file import write_text_atomic",
                "run_command = assign.subprocess.run",
                "def observe(command, *arguments, **options):",
                "    if len(command) > 2 and Path(command[1]).name == 'worker.py' and command[2] == 'assign':",
                f"        events = Path({str(self.session_dir / 'dispatch_events.txt')!r})",
                "        write_text_atomic(events, events.read_text() + 'assign\\n')",
                *((f"        baseline = Path({str(self.session_dir / 'phases/3/phase_baseline')!r})",
                   f"        write_text_atomic(Path({str(self.session_dir / 'baseline-at-worker-assign')!r}), baseline.read_text())")
                  if role != "review" else ()),
                "    return run_command(command, *arguments, **options)",
                "assign.subprocess.run = observe",
            )))
            result = self.launch(role, "--phase", "3")
        if role != "review":
            self.assertEqual(calls.read_text().splitlines(), ["sid-director.phase-3 check reviewed.txt --json"])
        return result

    def test_herdr_assignment_claims_the_phase_before_the_worker_assignment(self) -> None:
        result = self.herdr_claim_launch("clear")
        self.assertEqual(result.returncode, 8, result.stdout + result.stderr)
        self.assertEqual((self.session_dir / "dispatch_events.txt").read_text().splitlines(), ["claim", "assign"])
        head = subprocess.run(["git", "-C", str(self.project), "rev-parse", "HEAD"],
                              check=True, capture_output=True, text=True).stdout
        self.assertEqual((self.session_dir / "phases/3/phase_baseline").read_text(), head)
        self.assertEqual((self.session_dir / "baseline-at-worker-assign").read_text(), head)

    def test_herdr_assignment_refuses_a_failed_phase_claim_before_the_worker_assignment(self) -> None:
        result = self.herdr_claim_launch("blocked")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        log = self.session_dir / "phases/3/impl_agent.log"
        self.assertEqual(result.stderr,
                         f"ERROR: phase 3 could not claim its files before its worker opens; see {log}.\n")
        self.assertEqual((self.session_dir / "dispatch_events.txt").read_text().splitlines(), ["claim"])
        self.assertEqual((self.session_dir / "phases/3/impl_status").read_text(), "error\n")
        self.assertIn("launcher: phase 3 could not claim its files; its worker did not open",
                      (self.session_dir / "board.log").read_text())

    def test_herdr_review_assigns_the_worker_without_a_phase_claim(self) -> None:
        result = self.herdr_claim_launch("blocked", "review")
        self.assertEqual(result.returncode, 8, result.stdout + result.stderr)
        self.assertEqual((self.session_dir / "dispatch_events.txt").read_text().splitlines(), ["assign"])

    def finding_command(self, verb: str, *options: str, phase: str = "7") -> str:
        result = invoke_findings([verb, "--session-dir", str(self.session_dir), "--phase", phase, *options],
                                 os.environ.copy(), self.project)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def repair_state(self, phase: str = "7") -> RepairState:
        return cast(RepairState, json.loads(self.finding_command("status", phase=phase)))

    def start_repair(self, phase: str = "7") -> None:
        _ = self.finding_command("open", "--severity", "blocker", "--title", "Repair the worker",
                                 "--caught-by", "main", phase=phase)
        _ = self.finding_command("dispatch", "--covers", "F001", "--accepted-checks",
                                 "none: the fixture has no accepted behavior", phase=phase)

    def test_a_clean_fix_resolves_only_its_named_phase(self) -> None:
        self.start_repair()
        self.record("start-phase", "--phase-id", "8", "--phase-title", "Other repairs")
        self.start_repair("8")
        other = self.repair_state("8")
        with patch.dict(os.environ, {"UNIT_RESOLVES_ROUND": "1"}):
            _ = self.successful("fix", execution=InProcessAssignment())
        repaired = self.repair_state()
        self.assertEqual(repaired["rounds"][-1]["outcome"], "landed")
        self.assertEqual(repaired["findings"][0]["state"], "fixed_pending_review")
        self.assertEqual(self.repair_state("8"), other)
        self.assertEqual(self.turn_starts, 1)

    def test_a_failed_fix_abandons_the_round_without_refunding_the_attempt(self) -> None:
        self.start_repair()
        self.native.agent.during_prompt = fail_worker_after_prompt
        with patch.dict(os.environ, {"UNIT_RESOLVES_ROUND": "1"}):
            result = self.launch("fix", execution=InProcessAssignment())
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        repaired = self.repair_state()
        self.assertEqual(repaired["rounds"][-1]["outcome"], "abandoned")
        self.assertTrue(repaired["rounds"][-1]["edits_landed"])
        self.assertEqual(repaired["findings"][0]["state"], "open")
        self.assertEqual(repaired["findings"][0]["fix_attempts"], 1)

    def test_a_fix_with_round_resolution_disabled_keeps_the_round_in_flight(self) -> None:
        self.start_repair()
        with patch.dict(os.environ, {"UNIT_RESOLVES_ROUND": "0"}):
            _ = self.successful("fix", execution=InProcessAssignment())
        self.assertEqual(self.repair_state()["in_flight"], ["F001"])

    def test_an_implementation_dispatch_does_not_resolve_a_repair_round(self) -> None:
        self.start_repair()
        with patch.dict(os.environ, {"UNIT_RESOLVES_ROUND": "1"}):
            _ = self.successful(execution=InProcessAssignment())
        self.assertEqual(self.repair_state()["in_flight"], ["F001"])

    def run_resolution_commands(self, stderr: str) -> subprocess.CompletedProcess[str]:
        printed = stderr.split("assign.sh: repair resolution failed; run these commands:\n", 1)[1]
        commands = "\n".join(line for line in printed.splitlines() if line.startswith("env "))
        environment = os.environ.copy()
        _ = environment.pop("PYTHONPATH", None)
        return subprocess.run(["bash", "-e", "-c", commands], env=environment, cwd=self.session_dir,
                              capture_output=True, text=True)

    def complete_repair_after_ledger_failure(self, worker_failed: bool) -> None:
        self.start_repair()
        if worker_failed:
            self.native.agent.during_prompt = fail_worker_after_prompt
        ledger = self.session_dir / "findings_state.json"
        saved = self.session_dir / "saved-findings.json"
        self.instrument_launcher("\n".join((
            "from pathlib import Path",
            "from scripts.direct import assign",
            "original_call = assign.call",
            "def call(command, env, **options):",
            f"    ledger, saved = Path({str(ledger)!r}), Path({str(saved)!r})",
            "    if any(verb in command for verb in ('landed', 'abandon')) and not saved.exists():",
            "        ledger.rename(saved)",
            "        ledger.mkdir()",
            "    return original_call(command, env, **options)",
            "assign.call = call",
        )))
        with patch.dict(os.environ, {"UNIT_RESOLVES_ROUND": "1"}):
            result = self.launch("fix")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("repair resolution failed", result.stderr)
        name = next(iter(self.assignment_records()))
        status = self.session_dir / f"phases/7/{name}_status"
        self.assertEqual(status.read_text(), "error\n")
        ended = [event for event in self.events() if event.get("event_type") == "pass_finished"]
        self.assertEqual([event["status"] for event in ended], ["error"])
        ledger.rmdir()
        _ = saved.rename(ledger)
        repaired = self.run_resolution_commands(result.stderr)
        self.assertEqual(repaired.returncode, 0, repaired.stdout + repaired.stderr)
        self.assertEqual(self.turn_starts, 1)
        self.assertEqual(self.repair_state()["rounds"][-1]["outcome"], "abandoned" if worker_failed else "landed")
        self.assertEqual(status.read_text(), "error\n")

    def test_manual_resolution_lands_a_clean_fix_without_another_worker_turn(self) -> None:
        self.complete_repair_after_ledger_failure(False)

    def test_manual_resolution_abandons_a_failed_fix_without_another_worker_turn(self) -> None:
        self.complete_repair_after_ledger_failure(True)

    def test_a_fix_without_an_open_pass_resolves_without_finishing_a_pass(self) -> None:
        self.start_repair()
        directory = self.session_dir / "phases/7"
        directory.mkdir(parents=True, exist_ok=True)
        summary, status, reply = (directory / name for name in ("fix-summary.txt", "fix-status", "fix-reply.txt"))
        _ = summary.write_text("The worker failed before its pass opened.\n")
        args = assign.Arguments()
        args.role = "fix"
        dispatch = assign.AssignmentResult(args, self.session_dir, "7", "fix", "unstarted-fix",
                                   {**os.environ, "UNIT_RESOLVES_ROUND": "1"})
        finished: list[list[str]] = []
        original_call = assign.call

        def observe(command: list[str], env: dict[str, str], *, capture: bool = False,
                    check: bool = True) -> subprocess.CompletedProcess[str]:
            if any("finish-pass" in arg for arg in command):
                finished.append(command)
            return original_call(command, env, capture=capture, check=check)

        with patch.object(assign, "call", observe):
            result = assign.settle_dispatch(dispatch, 2, False, "unstarted-fix", status, summary, reply)
        self.assertEqual(result, 2)
        self.assertEqual(self.repair_state()["rounds"][-1]["outcome"], "abandoned")
        self.assertEqual(finished, [])
        self.assertEqual(status.read_text(), "error\n")

    def test_each_pass_records_the_staff_id_from_the_worker_roster(self) -> None:
        _ = self.successful(execution=InProcessAssignment())
        name, worker = next(iter(self.assignment_records().items()))
        passes = [event for event in self.events() if event.get("event_type") == "pass_started"]
        self.assertEqual([event["worker_id"] for event in passes], [worker["staff_id"]])
        self.assertEqual([event["worker_name"] for event in passes], [name])
        command = cast(list[str], passes[0]["launcher_command"])
        self.assertEqual(command[:4], ["bash", str(ASSIGN), "--role", "implement"])
        self.assertEqual(command[command.index("--session-dir") + 1], str(self.session_dir))

    def test_only_a_live_claude_or_codex_ancestor_permits_assignment(self) -> None:
        for family in ("claude", "codex"):
            with self.subTest(family=family):
                result = self.run_launcher(["bash", str(ASSIGN), "--help"], family=family)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for family in ("systemd", "bash", "codex-helper"):
            with self.subTest(parent=family):
                result = self.run_launcher(["bash", str(ASSIGN), "--help"], family=family,
                                           isolate_ancestors=True)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("requires a live Claude or Codex ancestor", result.stderr)
        self.assertEqual(self.native.fixture.world.launches, [])

    def test_dispatch_has_a_durable_worker_row_before_submitting_work(self) -> None:
        observed: list[staff.StaffRow] = []
        def before_prompt() -> None:
            observed.extend(staff.snapshot().find(kind="worker", parent=self.native.fixture.parent.id))
        self.native.agent.during_prompt = before_prompt
        _ = self.successful()
        row, = observed
        self.assertEqual(row.name, "impl")
        self.assertEqual(self.assignment_records()[row.name]["staff_id"], row.id)
        self.assertEqual(self.turn_starts, 1)

    def test_assignment_dispatch_uses_the_backend_thread_as_the_published_worker_id(self) -> None:
        _ = self.successful()
        name, saved = next(iter(self.assignment_records().items()))
        process = self.native.fixture.world.processes[self.native.fixture.world.foreground["w1:p1"][0]]
        row = staff.by_id(staff.StaffId(str(saved["staff_id"])))
        assert row is not None
        self.assertEqual(row.current_session, process.current_identity)
        self.assertEqual(row.name, name)

    def test_unreadable_staff_starts_no_worker(self) -> None:
        record = Path(os.environ["STAFF_FILE"])
        _ = record.write_text("{broken")
        result = self.launch(execution=InProcessAssignment())
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("invalid staff JSON:", result.stderr)
        self.assertEqual(self.native.fixture.world.launches, [])
        self.assertEqual(record.read_text(), "{broken")

    def test_rerun_review_dispatch_keeps_its_lens_and_read_only_thread(self) -> None:
        _ = self.successful("review", "--lens", "rerun", "--pass", "1", execution=InProcessAssignment())
        self.assertEqual(self.native.fixture.world.requests[0][1][self.native.fixture.world.requests[0][1].index("--sandbox") + 1], "read-only")
        self.assertTrue((self.session_dir / "phases/7/review_status_rerun_1").exists())

    def test_recorder_ad_hoc_phase_dispatches_into_adhoc_directory(self) -> None:
        self.record("start-phase", "--phase-id", "ad hoc", "--phase-title", "Ad hoc review")
        self.write_phase_prompt("ad hoc", "Ad hoc review")
        _ = self.successful("review", "--phase", "ad hoc", execution=InProcessAssignment())
        phase = self.session_dir / "phases/adhoc"
        self.assertEqual((phase / "review_status_1").read_text(), "done\n")
        self.assertTrue((phase / "review_prompt_1.txt").is_file())
        self.assertTrue((phase / "review_summary_1.txt").read_text().strip())
        self.assertFalse((self.session_dir / "phases/ad hoc").exists())
        self.assertEqual(self.native.fixture.world.requests[0][1][self.native.fixture.world.requests[0][1].index("--sandbox") + 1], "read-only")
        self.assertEqual(len(self.native.agent.submitted), 1)

    def test_unsafe_phase_ids_are_refused_before_opening_a_thread(self) -> None:
        for phase in (".", "..", "../escape", "nested/phase", "other phase"):
            with self.subTest(phase=phase):
                result = self.launch("review", "--phase", phase, execution=InProcessAssignment())
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("invalid phase id:", result.stderr)
        self.assertEqual(self.native.fixture.world.launches, [])

    def test_missing_required_options_and_positionals_refuse_before_opening_a_thread(self) -> None:
        for options in ([], ["--prompt", str(self.prompt)], ["--role", "test"], ["test", str(self.prompt)]):
            with self.subTest(options=options):
                result = invoke_assignment(options, os.environ.copy())
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("usage", (result.stdout + result.stderr).lower())
        self.assertEqual(self.native.fixture.world.launches, [])

    def test_future_roles_are_refused_before_any_thread_is_opened(self) -> None:
        for role in ("research", "propose"):
            with self.subTest(role=role):
                result = self.launch(role, execution=InProcessAssignment())
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.native.fixture.world.launches, [])

    def test_writer_paths_are_required_and_review_paths_are_refused(self) -> None:
        for role in ("implement", "test", "fix"):
            with self.subTest(role=role):
                self.assertEqual(self.launch(role, paths=False, execution=InProcessAssignment()).returncode, 2)
        self.assertEqual(self.launch("review", "--paths", str(self.paths), execution=InProcessAssignment()).returncode, 2)
        self.assertEqual(self.native.fixture.world.launches, [])

    def test_unsafe_empty_and_escaping_paths_refuse_before_claim(self) -> None:
        outside = self.project.parent / "outside"
        outside.mkdir()
        (self.project / "linked").symlink_to(outside, target_is_directory=True)
        for path in ("", "../secret", "/tmp/secret", "src/../secret", "linked/secret"):
            with self.subTest(path=path):
                _ = self.paths.write_text(path + "\n")
                result = self.launch(execution=InProcessAssignment())
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                if path:
                    self.assertIn(path, result.stdout + result.stderr)
        self.assertEqual(self.native.fixture.world.launches, [])

    def test_writer_dispatch_records_paths_rules_identity_and_a_finished_pass(self) -> None:
        _ = self.successful()
        name, record = next(iter(self.assignment_records().items()))
        self.assertEqual(self.turn_starts, 1)
        prompt = (self.session_dir / f"phases/7/{name}_prompt.txt").read_text()
        self.assertIn(self.prompt.read_text().strip(), prompt)
        for text in ("detached_runs.py start", "Simple, Fast and Beautiful", "poll"):
            self.assertIn(text, prompt)
        manifest = self.session_dir / "phases/7/dispatch_1_paths.json"
        self.assertEqual(read_object(manifest), {"worker": name, "round": 1,
                                               "paths": ["src/worker.py", "tests/test_worker.py"]})
        self.assertEqual((manifest.parent / f"{name}_status").read_text().strip(), "done")
        self.assertTrue((manifest.parent / f"{name}_summary.txt").read_text().strip())
        self.assertTrue((manifest.parent / f"{name}_agent.log").is_file())
        finished = [event for event in self.events() if event.get("event_type") == "pass_finished"]
        self.assertEqual(len(finished), 1, self.events())
        started = next(event for event in self.events() if event.get("event_type") == "pass_started")
        self.assertEqual(started["worker_id"], record["staff_id"])

    def test_review_is_read_only_and_thread_board_and_summary_use_the_shown_context(self) -> None:
        _ = self.successful("review", "--lens", "contract", "--pass", "1")
        name = next(iter(self.assignment_records()))
        command = self.native.fixture.world.requests[0][1]
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        board = (self.session_dir / "board.log").read_text()
        self.assertIn(f"[{name}]", board)
        summary = self.session_dir / f"phases/7/{name}_summary_contract_1.txt"
        self.assertIn("fixture", summary.read_text())
        self.assertEqual((summary.parent / f"{name}_status_contract_1").read_text(), "done\n")

    def test_style_assignment_is_prepared_and_captured_through_assign_on_retry(self) -> None:
        self.prepare_style_project()
        _ = (self.project / "new.rs").write_text("fn first() {}\n")
        outcomes = ["Error", "Findings"]
        prepared: list[Path] = []
        def style_reply() -> None:
            state = read_object(self.session_dir / "phases/7/style_review_state.json")
            directory = Path(str(state["input_directory"]))
            prompt = (directory / "prompt.md").read_text()
            self.assertEqual(prompt.count("## Phase style review"), 1)
            self.assertNotIn("write your summary to", prompt)
            diff = Path(next(line for line in prompt.splitlines() if line.startswith("- Diff ")).split(": ", 1)[1])
            self.assertIn("+fn ", diff.read_text())
            prepared.append(diff)
            self.native.agent.final = ("1. minor: fixture finding at new.rs:1\n" + style_review.BEGIN +
                "\n| # | Rule | Result |\n| 1 | Fixture | Pass |\nStyle review: " + outcomes.pop(0) + "\n" + style_review.END)
        self.native.agent.during_prompt = style_reply
        phase = self.session_dir / "phases/7"
        _ = self.successful("review", "--lens", "contract", "--style-since", "HEAD")
        self.assertEqual(read_object(phase / "style_review_state.json")["state"], "owed")
        name, record = next(iter(self.assignment_records().items()))
        before = prepared[0].read_bytes()
        _ = (self.project / "new.rs").write_text("fn second() {}\n")
        _ = self.successful("review", "--to", name, "--pass", "2", "--style-since", "HEAD")
        self.assertEqual(read_object(phase / "style_review_state.json")["state"], "complete")
        self.assertIn("Style review: Findings", (phase / "phase_style_review.md").read_text())
        self.assertIn("fixture finding", (phase / f"{name}_summary_2.txt").read_text())
        self.assertNotEqual(prepared[0].parent, prepared[1].parent)
        self.assertEqual(prepared[0].read_bytes(), before)
        self.assertIn("+fn second", prepared[1].read_text())
        self.assertEqual(self.assignment_records()[name]["thread_id"], record["thread_id"])
        command = self.native.fixture.world.requests[-1][1]
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")

    def test_style_preparation_failure_through_assign_keeps_code_review_running(self) -> None:
        self.prepare_style_project()
        _ = self.successful("review", "--style-since", "missing-baseline", execution=InProcessAssignment())
        phase = self.session_dir / "phases/7"
        self.assertEqual(read_object(phase / "style_review_state.json")["state"], "owed")
        self.assertIn("invalid baseline", (phase / "phase_style_review.md").read_text())
        self.assertEqual((phase / "review_status_1").read_text().strip(), "done")
        self.assertEqual(len(self.native.agent.submitted), 1)

    def test_assign_style_range_excludes_other_work_in_flight(self) -> None:
        self.prepare_style_project()
        baseline = style_review.git(self.project, "rev-parse", "HEAD").stdout.decode().strip()
        _ = (self.project / "included.txt").write_text("pinned addition\n")
        self.commit_project("pinned range")
        end = style_review.git(self.project, "rev-parse", "HEAD").stdout.decode().strip()
        _ = (self.project / "included.txt").write_text("outside the range\n")
        _ = (self.project / "untracked.rs").write_text("fn outside() {}\n")
        _ = self.successful("review", "--style-since", baseline, "--style-until", end, execution=InProcessAssignment())
        phase = self.session_dir / "phases/7"
        state = read_object(phase / "style_review_state.json")
        directory = Path(str(state["input_directory"]))
        self.assertEqual(state["baseline"], baseline)
        self.assertEqual(state["until"], end)
        self.assertEqual((directory / "style-review-additions.txt").read_text(), "+pinned addition\n")
        self.assertIn("- Rust change: no", (directory / "prompt.md").read_text())
        self.assertIn(f"- Range: {baseline}..{end}", (directory / "prompt.md").read_text())

    def test_missing_run_calls_prepare_and_prints_the_prepared_folder(self) -> None:
        prepared = self.prepare_launcher()
        environment = os.environ.copy()
        for key in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "UNIT_RUN_DIR", "UNIT_SESSION_DIR"):
            _ = environment.pop(key, None)
        with patch.dict(os.environ, environment, clear=True):
            result = self.launch(session=False, execution=InProcessAssignment())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(prepared.exists())
        self.assertIn(str(self.session_dir), result.stdout)
        self.assertEqual(self.turn_starts, 1)

    def test_a_fresh_run_takes_its_phase_title_from_the_dispatch_prompt(self) -> None:
        (self.session_dir / "progress_history_state.json").unlink()
        shutil.rmtree(self.history)
        shutil.rmtree(self.session_dir / "phases", ignore_errors=True)
        prepared = self.prepare_launcher()
        director = os.environ["CODEX_THREAD_ID"]
        transcript = Path.home() / ".codex/sessions" / f"rollout-{director}.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        _ = transcript.write_text("\n".join(json.dumps(record) for record in (
            {"type": "session_meta", "payload": {"id": director}},
            {"type": "turn_context", "payload": {"model": "fixture-director", "effort": "high"}},
        )) + "\n")
        self.addCleanup(transcript.unlink, missing_ok=True)
        environment = os.environ.copy()
        for key in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "UNIT_RUN_DIR", "UNIT_SESSION_DIR"):
            _ = environment.pop(key, None)
        environment["CODEX_THREAD_ID"] = director
        with patch.dict(os.environ, environment, clear=True):
            result = self.launch(session=False, execution=InProcessAssignment())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(prepared.exists())
        phase = cast(dict[str, object], read_object(self.session_dir / "progress_history_state.json")["phase"])
        self.assertEqual((phase["id"], phase["title"]), ("7", "Assignment core"))
        self.assertEqual(self.turn_starts, 1)
        self.assertTrue(any(event.get("event_type") == "pass_finished" for event in self.events()))

    def test_default_session_uses_the_callers_marker_instead_of_starting_another_run(self) -> None:
        caller = os.environ["CODEX_THREAD_ID"]
        marker = Path(os.environ["UNIT_ACTIVE_DIR"]) / caller
        marker.parent.mkdir(parents=True, exist_ok=True)
        _ = marker.write_text(str(self.session_dir))
        with patch.dict(os.environ, {"CODEX_THREAD_ID": caller}):
            result = self.launch(session=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.turn_starts, 1)
        self.assertEqual(marker.read_text(), str(self.session_dir))

    def test_fix_requires_a_cause_and_a_wrong_phase_prompt_never_claims_the_worker(self) -> None:
        refused = self.launch("fix", execution=InProcessAssignment())
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertEqual(self.turn_starts, 0)
        _ = self.successful("fix", "--fix-cause", "repair failed acceptance gate", execution=InProcessAssignment())
        self.assertTrue(any(event.get("pass_kind") == "fix" for event in self.events()))
        _ = self.prompt.write_text("Phase 6 — Another phase\nDo the wrong work.\n")
        result = self.launch(execution=InProcessAssignment())
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.turn_starts, 1)

    def test_a_failed_worker_records_an_error_pass_and_returns_worker_failure(self) -> None:
        self.native.agent.during_prompt = fail_worker_after_prompt
        result = self.launch()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual((self.session_dir / "phases/7/impl_status").read_text().strip(), "error")
        ended = [event for event in self.events() if event.get("event_type") == "pass_finished"]
        self.assertEqual(len(ended), 1, self.events())
        self.assertEqual(ended[0].get("status"), "error")
        self.assertNotIn("launcher_pid", next(iter(self.assignment_records().values())))
