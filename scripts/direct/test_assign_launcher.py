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

from scripts.agents import codex_mesh, test_codex_mesh as mesh_fixture
from scripts.direct import assign, progress_history
from scripts.direct.assign_claude_test_support import AssignClaudeLauncherFixture
from scripts.direct.assign_launcher_test_support import ASSIGN, ROOT, AssignLauncherFixture, InProcessAssignment, LauncherScript, invoke_assignment, read_object
from scripts.direct.findings_test_support import invoke_findings
from scripts.herdr import herdr
from scripts.lint import style_review
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.test_models_registry import RegistryReadProbe, write_registry
from scripts.production import staff, test_worker as worker_fixture, worker, worker_admission
from scripts.production.cli_fixture import invoke_cli
from scripts.production.staff_test_support import fixture_runtime_id, fixture_worker, transcript_fixture
from scripts.message import sessions
from scripts.usage import agents_log

def setUpModule() -> None:
    mesh_fixture.setUpModule()


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

    def failing_worker(self, code: int = 2, *, recursive_history: bool = False) -> Path:
        """Run a failing child at the worker boundary of the real launcher."""
        command = self.session_dir.parent / "worker-python"
        calls = self.session_dir.parent / "failed-worker-commands.jsonl"
        _ = command.write_text(f"#!{sys.executable}\n" + "\n".join((
            "import json, os, sys",
            "from pathlib import Path",
            "arguments = sys.argv[1:]",
            "if len(arguments) > 1 and ((Path(arguments[0]).name == 'codex_mesh.py' and arguments[1] == 'follow')",
            "        or (Path(arguments[0]).name == 'worker.py' and arguments[1] == 'assign')):",
            f"    with Path({str(calls)!r}).open('a') as output:",
            "        output.write(json.dumps(arguments) + '\\n')",
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
            f"    (run / 'mesh_server.json').write_text({(self.session_dir / 'mesh_server.json').read_text()!r})",
            "    return run, phase",
            "assign.prepare_run = prepare",
            *(("real_cleanup = assign.Dispatch.cleanup",
               "def cleanup(dispatch):",
               "    real_cleanup(dispatch)",
               "    history = assign.read_object(dispatch.run / 'progress_history_state.json')['history_file']",
               "    with Path(history).open('a') as output:",
               "        output.write('[' * 10000 + '0' + ']' * 10000 + '\\n')",
               "assign.Dispatch.cleanup = cleanup") if recursive_history else ()),
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
        self.assertEqual(command[1], "follow")
        self.assertEqual(len(self.methods("thread/start")), 1)

    def test_a_claude_session_marker_is_removed_after_its_only_worker_fails(self) -> None:
        _ = self.failing_worker()
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": staff.runtime_id()}):
            self.assert_run_ended(self.launch())

    def assert_prepared_marker_is_removed(self) -> None:
        (self.session_dir / "mesh_server.json").unlink()
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

    def failed_claude(self) -> Path:
        (self.session_dir / "mesh_server.json").unlink()
        self.commit_project()
        _ = write_registry(self.registry, {
            "assignments": {"direct": "claude"},
            "direct.claude": {"impl": "opus:high", "review": "opus:xhigh"},
        }, update=True)
        binary = self.session_dir.parent / "bin" / "claude"
        binary.parent.mkdir()
        calls = self.session_dir.parent / "claude-commands.jsonl"
        _ = binary.write_text(f"#!{sys.executable}\n" + "\n".join((
            "import json, sys",
            "from pathlib import Path",
            "if sys.argv[1:] == ['agents', '--json']:",
            "    print('[]')",
            "    raise SystemExit(0)",
            f"with Path({str(calls)!r}).open('a') as output:",
            "    output.write(json.dumps(sys.argv[1:]) + '\\n')",
            "raise SystemExit(2)",
        )) + "\n")
        binary.chmod(0o755)
        shim = self.home / ".claude/scripts/lib/py"
        shim.parent.mkdir(parents=True, exist_ok=True)
        shim.symlink_to(ROOT / "scripts/lib/py")
        _ = (self.home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "fixture@example.invalid"},
        }))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "CLAUDE_BIN": str(binary),
            "PATH": str(binary.parent) + os.pathsep + os.environ["PATH"],
            "UNIT_WORKER_POLL_SECS": "0",
        })))
        _ = os.environ.pop("CLAUDE_CONFIG_DIR", None)
        return calls

    def test_a_sole_claude_implementation_failure_ends_the_run_after_its_pass_starts(self) -> None:
        calls = self.failed_claude()
        result = self.launch()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertFalse(self.marker().exists(), result.stdout + result.stderr)
        self.assertEqual(read_object(self.session_dir / "progress_history_state.json")["status"], "error")
        commands = [cast(list[str], json.loads(line)) for line in calls.read_text().splitlines()]
        self.assertEqual([command[0] for command in commands], ["--bg"])
        started = [event for event in self.events() if event.get("event_type") == "pass_started"]
        self.assertEqual([event["worker_name"] for event in started], ["project-impl"])
        finished = [event for event in self.events() if event.get("event_type") == "run_finished"]
        self.assertEqual([event["status"] for event in finished], ["error"])
        self.assertIn("could not start a background session", result.stderr)
        self.assertIn("run ended", result.stderr.lower())

    def test_a_claude_slot_refusal_before_its_pass_starts_keeps_the_run(self) -> None:
        calls = self.failed_claude()
        result = self.launch("implement", "--new-worker", "slot-" + "x" * 35)
        self.assert_run_active(result)
        self.assertIn("team_role must be", result.stderr)
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
        _ = self.failing_worker()
        self.assert_run_active(self.launch("implement", "--to", name))
        self.assertEqual(len(self.assignment_records()), 1)
        finished = [event for event in self.events() if event.get("event_type") == "pass_finished"]
        self.assertEqual([event["status"] for event in finished], ["completed", "error"])

    def test_a_second_worker_keeps_the_run_after_a_launch_failure(self) -> None:
        from scripts.direct import workers

        other = workers.resolve(self.session_dir, "test", working_dir=self.project)
        self.next_thread_id = fixture_runtime_id("second-worker")
        _ = self.failing_worker()
        self.assert_run_active(self.launch())
        self.assertIn(other, self.assignment_records())
        self.assertEqual(len(self.assignment_records()), 2)

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
        _ = (self.session_dir / "worker_transport").write_text("herdr\n")
        calls = self.failing_worker(7)
        self.assert_run_active(self.launch(), 7)
        command = cast(list[str], json.loads(calls.read_text()))
        self.assertEqual(command[1], "assign")
        self.assertEqual(self.methods("thread/start"), [])

    def test_a_refusal_before_the_worker_command_keeps_the_run(self) -> None:
        calls = self.failing_worker()
        self.assert_run_active(self.launch(paths=False))
        self.assertFalse(calls.exists())
        self.assertEqual(self.methods("thread/start"), [])

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
        self.next_thread_id = fixture_runtime_id("retry-worker")
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
        self.next_thread_id = fixture_runtime_id("retry-worker")
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
        write_text_atomic(self.fixture.run_dir / "worker_transport", "herdr\n")
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

    def test_review_incomplete_start_closes_the_pane_without_either_stage(self) -> None:
        self.fixture.fixture.world.outcome = herdr.PromptNotSubmitted("startup hook failed")
        self.assert_start_not_delivered(worker.ExitCode.UNCERTAIN, "publication failed: startup hook failed")
        self.assertEqual(len(self.fixture.fixture.world.launches), 2)
        self.assertEqual(self.fixture.fixture.world.closed, [("w1:p2", "terminal-w1:p2")])

    def test_review_failed_publication_closes_the_backend_without_either_stage(self) -> None:
        rows = staff.snapshot().find(kind="worker", parent=self.fixture.fixture.parent.id)
        with patch.object(staff, "ensure_worker", side_effect=staff.StaffRefused("publication unavailable")):
            self.assert_start_not_delivered(worker.ExitCode.UNCERTAIN, "publication refused: publication unavailable")
        self.assertIn("review: publication failed: staff: review:", (self.phase / "review_agent_1.log").read_text())
        self.assertEqual(staff.snapshot().find(kind="worker", parent=self.fixture.fixture.parent.id), rows)
        self.assertEqual(len(self.fixture.fixture.world.launches), 2)
        self.assertEqual(self.fixture.fixture.world.closed, [("w1:p2", "terminal-w1:p2")])

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
        with patch("scripts.agents.mesh_runs.live_runs", return_value=[object()]):
            self.assert_start_not_delivered(worker.ExitCode.REFUSED, "role change blocked by active work: a detached run is live")
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
        Path(os.environ["NOTIFIER_SESSIONS_DIR"]).mkdir(parents=True, exist_ok=True)
        owner = staff.native_id(os.environ["CODEX_THREAD_ID"])
        row = fixture_worker("switch1", owner, backend=staff.ClaudeBackend())
        source, evidence = transcript_fixture(row, staff.ClaudeBackend())
        agents_log.append(row.current_session, "interactive", None, datetime.now(timezone.utc))
        roster = self.session_dir / codex_mesh.ROSTER_FILE
        _ = roster.write_text(json.dumps({row.name: {"staff_id": row.id, "status": "done"}}))
        commands = self.session_dir.parent / "mesh-commands.jsonl"
        releases = self.session_dir.parent / "source-releases.jsonl"
        directory = self.session_dir.parent / "instrument-mesh"
        directory.mkdir()
        code = "\n".join((
            "import json, os, sys",
            "from pathlib import Path",
            "def enter(frame, event, argument):",
            "    if event != 'call' or frame.f_code.co_name != 'main' or not frame.f_code.co_filename.endswith('/agents/codex_mesh.py'): return",
            "    sys.setprofile(None)",
            "    mesh = sys.modules['__main__']",
            f"    with Path({str(commands)!r}).open('a') as output: output.write(json.dumps(sys.argv[1:]) + chr(10))",
            "    def start_server(session_dir, workers_home, *extra):",
            f"        Path(session_dir, mesh.SERVER_FILE).write_text(json.dumps({{'pid': os.getpid(), 'port': {self.server.port}, 'codex_home': str(workers_home), 'auto_compact_tokens': mesh.AUTO_COMPACT_TOKENS, 'dev_shell_worktree': {str(self.project)!r}}}))",
            f"        return {self.server.port}",
            "    mesh._start_server = start_server",
            "    mesh._start_watcher = lambda *args: mesh.ExistingServerWatcher()",
            "    def observe_release(frame, event, result):",
            "        if event == 'return' and frame.f_code.co_name == 'release_source' and frame.f_code.co_filename.endswith('/shutdown/stop_resume.py'):",
            "            row = frame.f_locals['row']",
            f"            with Path({str(releases)!r}).open('a') as output: output.write(json.dumps({{'session': row.current_session, 'result': type(result).__name__}}) + chr(10))",
            "    sys.setprofile(observe_release)",
            "sys.setprofile(enter)",
        ))
        _ = (directory / "sitecustomize.py").write_text(code)
        with patch.dict(os.environ, {"PYTHONPATH": str(directory) + os.pathsep + str(ASSIGN.parents[2])}):
            for live_server, named in ((False, True), (True, True), (True, False)):
                with self.subTest(live_server=live_server, named=named):
                    if not live_server:
                        (self.session_dir / codex_mesh.SERVER_FILE).unlink(missing_ok=True)
                    else:
                        mesh_fixture.write_server_record(self.session_dir, self.server.port, self.home / ".codex",
                                                         str(self.project))
                    if not named:
                        agents_log.append(row.current_session, "impl", None, datetime.now(timezone.utc))
                    commands.unlink(missing_ok=True)
                    self.server.requests.clear()
                    image = self.session_dir.parent / "attached.png"
                    image.touch()
                    result = self.launch("implement", "--image", str(image), *(("--to", row.name) if named else ()))
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    called = [cast(list[str], json.loads(line))[0] for line in commands.read_text().splitlines()]
                    self.assertIn("start", called)
                    self.assertNotIn("can-follow", called)
                    self.assertNotIn("follow", called)
                    current = staff.by_id(row.id)
                    assert current is not None
                    self.assertNotEqual(current.current_session, row.current_session)
                    self.assertEqual(current.to_json() | {"current_session": row.current_session}, row.to_json())
                    self.assertEqual(len(self.methods("turn/start")), 1)
                    extract = worker_admission.handoff_path(self.session_dir, row)
                    self.assertIn(evidence[0][0], extract.read_text())
                    opening = self.turn_inputs[-1]
                    self.assertLess(opening.index(str(extract)), opening.index(self.prompt.read_text()))
                    self.assertIn(str(source), opening)
                    self.assertIn(str(image.resolve()), opening)
                    stopped = [cast(dict[str, object], json.loads(line)) for line in releases.read_text().splitlines()]
                    self.assertEqual(stopped[-1], {"session": row.current_session, "result": "SourceReleased"})
                    commands.unlink()
                    self.server.requests.clear()
                    follow = self.launch("implement", "--to", row.name)
                    self.assertEqual(follow.returncode, 0, follow.stdout + follow.stderr)
                    called = [cast(list[str], json.loads(line))[0] for line in commands.read_text().splitlines()]
                    self.assertIn("can-follow", called)
                    self.assertIn("follow", called)
                    self.assertNotIn("start", called)
                    self.assertEqual(staff.by_id(row.id), current)
                    self.assertEqual(len(self.methods("turn/start")), 1)
                    _ = staff.update_current_session(current.current_session, staff.native_id(row.current_session))
                    _ = roster.write_text(json.dumps({row.name: {"staff_id": row.id, "status": "done"}}))

    def test_codex_retry_starts_with_the_saved_handoff_and_accepted_or_unrelated_work_follows(self) -> None:
        self.commit_project()
        _ = self.successful("implement", "--new-worker", "retry1")
        row = worker_admission.member(self.session_dir, "retry1", staff.local_snapshot())
        assert row is not None
        roster = self.session_dir / codex_mesh.ROSTER_FILE
        original = read_object(roster)
        _ = self.prompt.write_text("Continue the interrupted switch.\n")
        request = worker_admission.assignment_id(self.prompt)
        extract = worker_admission.handoff_path(self.session_dir, row)
        _ = extract.write_text(f"{worker_admission.HANDOFF_SOURCE_PREFIX}{self.session_dir / 'source.jsonl'}\nKeep the source work.\n")
        commands = self.session_dir.parent / "retry-commands.jsonl"
        directory = self.session_dir.parent / "retry-observer"
        directory.mkdir()
        _ = (directory / "sitecustomize.py").write_text("\n".join((
            "import json, sys",
            "from pathlib import Path",
            "def observe(frame, event, argument):",
            "    if event == 'call' and frame.f_code.co_name == 'main' and frame.f_code.co_filename.endswith('/agents/codex_mesh.py'):",
            f"        with Path({str(commands)!r}).open('a') as output: output.write(json.dumps(sys.argv[1:]) + chr(10))",
            "        sys.setprofile(None)",
            "sys.setprofile(observe)",
        )))
        cases = (("prepared", request, True), ("sending", request, True), ("", request, True),
                 ("accepted", request, False), ("sending", "another-prompt", False))
        with patch.dict(os.environ, {"PYTHONPATH": str(directory) + os.pathsep + str(ASSIGN.parents[2])}):
            for state, identity, pending in cases:
                with self.subTest(state=state, identity=identity):
                    entry = cast(dict[str, object], original[row.name]).copy()
                    entry.update(assignment_id=identity, dispatch_state=state)
                    _ = roster.write_text(json.dumps({row.name: entry}))
                    commands.unlink(missing_ok=True)
                    self.server.requests.clear()
                    result = self.launch("implement", "--to", row.name)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    called = [cast(list[str], json.loads(line))[0] for line in commands.read_text().splitlines()]
                    self.assertIn("start" if pending else "follow", called)
                    self.assertNotIn("can-follow" if pending else "start", called)
                    if pending:
                        self.assertNotIn("follow", called)
                        opening = self.turn_inputs[-1]
                        self.assertLess(opening.index(str(extract)), opening.index(self.prompt.read_text()))
                    self.assertEqual(self.methods("thread/start"), [])
                    self.assertEqual(staff.by_id(row.id), row)

    def test_assignment_shares_one_generation_through_allocation_and_refreshes_the_next_launch(self) -> None:
        probe = RegistryReadProbe(self.session_dir.parent, self.registry)
        _ = write_registry(self.registry, {"direct.codex": {"impl": "gpt-kept-sol:high"},
                                           "codex.agents": {"gpt-kept-sol": "high", "gpt-other-sol": "high,max"},
                                           "direct.options": {"codex_service_tier": "fast"}})
        original = self.server.respond
        edited = False

        @self.hang_guard.callback
        def change_after_creation(request: dict[str, object]) -> list[dict[str, object]] | None:
            nonlocal edited
            if request.get("method") == "thread/start" and not edited:
                edited = True
                _ = write_registry(self.registry, {"direct.codex": {"impl": "gpt-other-sol:max"},
                                                   "codex.agents": {"gpt-other-sol": "high,max"},
                                                   "direct.options": {"codex_service_tier": "flex"}})
            return original(request)

        self.server.respond = change_after_creation
        with patch.dict(os.environ, probe.environment(os.environ)):
            for expected in (("gpt-kept-sol", "high", "fast"), ("gpt-other-sol", "max", "flex")):
                with self.subTest(expected=expected):
                    probe.clear()
                    self.server.requests.clear()
                    _ = self.successful(execution=LauncherScript(ASSIGN))
                    turns = self.methods("turn/start")
                    self.assertEqual(len(turns), 1)
                    params = cast(dict[str, object], turns[0]["params"])
                    self.assertEqual((params.get("model"), params.get("effort"), params.get("serviceTier")), expected)
                    self.assertEqual(len(probe.reads()), 1, probe.reads())
        self.assertTrue(edited)

    def test_herdr_assignment_retains_unknown_workers_without_a_mesh_backend(self) -> None:
        self.commit_project()
        _ = (self.session_dir / "worker_transport").write_text("herdr\n")
        before = Path(os.environ["STAFF_FILE"]).read_bytes()
        prompt = self.prompt.read_bytes()
        with patch.dict(os.environ, {"HERDR_BIN": "/nonexistent/herdr"}):
            result = self.launch(execution=LauncherScript(ASSIGN))
        self.assertEqual(result.returncode, 8, result.stdout + result.stderr)
        self.assertEqual(self.methods("thread/start"), [])
        self.assertEqual(self.methods("turn/start"), [])
        self.assertEqual(Path(os.environ["STAFF_FILE"]).read_bytes(), before)
        self.assertEqual(self.prompt.read_bytes(), prompt)
        self.assertEqual((self.session_dir / "phases/7/impl_status").read_text(), "error\n")

    def herdr_claim_launch(self, claim_status: str, role: str = "implement") -> subprocess.CompletedProcess[str]:
        self.commit_project()
        self.record("start-phase", "--phase-id", "3", "--phase-title", "Claim before assignment")
        write_text_atomic(self.prompt, "Phase 3 — Claim before assignment\nImplement the assigned change.\n")
        write_text_atomic(self.session_dir / "worker_transport", "herdr\n")
        write_text_atomic(self.session_dir / "mesh_events.txt", "")
        environment, calls = HerdrClaimFixture().claim_fixture(self.session_dir, claim_status)
        environment["HERDR_BIN"] = "/nonexistent/herdr"
        with patch.dict(os.environ, environment):
            self.instrument_launcher("\n".join((
                "from pathlib import Path",
                "from scripts.direct import assign",
                "from scripts.lib.atomic_file import write_text_atomic",
                "run_command = assign.subprocess.run",
                "def observe(command, *arguments, **options):",
                "    if len(command) > 2 and Path(command[1]).name == 'worker.py' and command[2] == 'assign':",
                f"        events = Path({str(self.session_dir / 'mesh_events.txt')!r})",
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
        self.assertEqual((self.session_dir / "mesh_events.txt").read_text().splitlines(), ["claim", "assign"])
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
        self.assertEqual((self.session_dir / "mesh_events.txt").read_text().splitlines(), ["claim"])
        self.assertEqual((self.session_dir / "phases/3/impl_status").read_text(), "error\n")
        self.assertIn("launcher: phase 3 could not claim its files; its worker did not open",
                      (self.session_dir / "board.log").read_text())

    def test_herdr_review_assigns_the_worker_without_a_phase_claim(self) -> None:
        result = self.herdr_claim_launch("blocked", "review")
        self.assertEqual(result.returncode, 8, result.stdout + result.stderr)
        self.assertEqual((self.session_dir / "mesh_events.txt").read_text().splitlines(), ["assign"])

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
        self.outcomes = ["failed"]
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
            self.outcomes = ["failed"]
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
        dispatch = assign.Dispatch(args, self.session_dir, self.project, self.prompt, "7", "fix", "unstarted-fix",
                                   "codex", "gpt-kept-sol", "high", "default",
                                   {**os.environ, "UNIT_RESOLVES_ROUND": "1"}, {})
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
        self.assertEqual(self.methods("thread/start"), [])

    def test_dispatch_has_a_durable_worker_row_before_submitting_work(self) -> None:
        from scripts.production import staff
        _ = self.successful(execution=InProcessAssignment())
        self.assertIn("As your last act, write your summary to",
                      (self.session_dir / "phases/7/dispatch_1_prompt.txt").read_text())
        name = next(iter(self.assignment_records()))
        rows = staff.snapshot().find(kind="worker", parent=staff.StaffId(os.environ["CODEX_THREAD_ID"]))
        self.assertEqual([row.name for row in rows], [name])

    def test_assignment_dispatch_uses_the_backend_thread_as_the_published_worker_id(self) -> None:
        from scripts.production import staff
        _ = self.successful(execution=InProcessAssignment())
        records = self.assignment_records()
        self.assertEqual(len(records), 1)
        name, record = next(iter(records.items()))
        thread = str(record["thread_id"])
        row = staff.by_id(staff.StaffId(thread))
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual((row.id, row.name, row.parent), (thread, name, staff.ReportsTo(staff.StaffId(os.environ["CODEX_THREAD_ID"]))))
        self.assertTrue(self.claims)
        self.assertTrue(all(str(entry["thread_id"]) == row.id for entry in self.claims))

    def test_unreadable_staff_starts_no_worker(self) -> None:
        record = Path(os.environ["STAFF_FILE"])
        _ = record.write_text("{broken")
        result = self.launch(execution=LauncherScript(ASSIGN))
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("invalid staff JSON:", result.stderr)
        self.assertEqual(self.methods("thread/start"), [])
        self.assertEqual(record.read_text(), "{broken")

    def test_rerun_review_dispatch_keeps_its_lens_and_read_only_thread(self) -> None:
        _ = self.successful("review", "--lens", "rerun", "--pass", "1", execution=InProcessAssignment())
        starts = self.methods("thread/start")
        self.assertEqual(cast(dict[str, object], starts[0]["params"])["sandbox"], "read-only")
        self.assertTrue((self.session_dir / "phases/7/project-review_status_rerun_1").exists())

    def test_recorder_ad_hoc_phase_dispatches_into_adhoc_directory(self) -> None:
        self.record("start-phase", "--phase-id", "ad hoc", "--phase-title", "Ad hoc review")
        self.write_phase_prompt("ad hoc", "Ad hoc review")
        _ = self.successful("review", "--phase", "ad hoc", execution=InProcessAssignment())
        phase = self.session_dir / "phases/adhoc"
        self.assertEqual((phase / "project-review_status_1").read_text(), "done\n")
        self.assertTrue((phase / "dispatch_1_prompt.txt").is_file())
        self.assertTrue((phase / "project-review_summary_1.txt").read_text().strip())
        self.assertFalse((self.session_dir / "phases/ad hoc").exists())
        self.assertEqual(cast(dict[str, object], self.methods("thread/start")[0]["params"])["sandbox"], "read-only")
        self.assertEqual(len(self.methods("turn/start")), 1)

    def test_unsafe_phase_ids_are_refused_before_opening_a_thread(self) -> None:
        for phase in (".", "..", "../escape", "nested/phase", "other phase"):
            with self.subTest(phase=phase):
                result = self.launch("review", "--phase", phase, execution=InProcessAssignment())
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("invalid phase id:", result.stderr)
        self.assertEqual(self.methods("thread/start"), [])

    def test_missing_required_options_and_positionals_refuse_before_opening_a_thread(self) -> None:
        for options in ([], ["--prompt", str(self.prompt)], ["--role", "test"], ["test", str(self.prompt)]):
            with self.subTest(options=options):
                result = invoke_assignment(options, os.environ.copy())
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("usage", (result.stdout + result.stderr).lower())
        self.assertEqual(self.methods("thread/start"), [])

    def test_future_roles_are_refused_before_any_thread_is_opened(self) -> None:
        for role in ("research", "propose"):
            with self.subTest(role=role):
                result = self.launch(role, execution=InProcessAssignment())
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.methods("thread/start"), [])

    def test_writer_paths_are_required_and_review_paths_are_refused(self) -> None:
        for role in ("implement", "test", "fix"):
            with self.subTest(role=role):
                self.assertEqual(self.launch(role, paths=False, execution=InProcessAssignment()).returncode, 2)
        self.assertEqual(self.launch("review", "--paths", str(self.paths), execution=InProcessAssignment()).returncode, 2)
        self.assertEqual(self.methods("thread/start"), [])

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
        self.assertEqual(self.methods("thread/start"), [])

    def test_writer_dispatch_records_paths_rules_identity_and_a_finished_pass(self) -> None:
        _ = self.successful(execution=LauncherScript(ASSIGN))
        name, record = next(iter(self.assignment_records().items()))
        self.assertEqual(record["role"], "impl")
        self.assertEqual(self.turn_starts, 1)
        prompt = self.turn_inputs[0]
        self.assertIn(self.prompt.read_text().strip(), prompt)
        for text in ("mesh_runs.py start", "Simple, Fast and Beautiful", "poll"):
            self.assertIn(text, prompt)
        manifest = self.session_dir / "phases/7/dispatch_1_paths.json"
        self.assertEqual(read_object(manifest), {"worker": name, "round": 1,
                                               "paths": ["src/worker.py", "tests/test_worker.py"]})
        self.assertEqual((manifest.parent / "impl_status").read_text().strip(), "done")
        self.assertTrue((manifest.parent / "impl_summary.txt").is_file())
        self.assertTrue((manifest.parent / "impl_agent.log").is_file())
        finished = [event for event in self.events() if event.get("event_type") == "pass_finished"]
        self.assertEqual(len(finished), 1, self.events())
        self.assertTrue(self.claims)
        launcher_pid = self.claims[0].get("launcher_pid")
        self.assertIsInstance(launcher_pid, int)
        self.assertTrue(self.reservations, "reservation must survive until can-follow probes the server")
        self.assertEqual(self.reservations[-1].get("allocation_owner_pid"), launcher_pid)

    def test_review_is_read_only_and_thread_board_and_summary_use_the_shown_context(self) -> None:
        _ = self.successful("review", "--lens", "contract", "--pass", "1", execution=InProcessAssignment())
        name = next(iter(self.assignment_records()))
        starts = self.methods("thread/start")
        self.assertEqual(cast(dict[str, object], starts[0]["params"])["sandbox"], "read-only")
        names = [str(cast(dict[str, object], request["params"])["name"])
                 for request in self.methods("thread/name/set")]
        self.assertTrue(names)
        for shown in names:
            self.assertIn("fixture-production · fixture ·", shown)
            self.assertNotIn(f"({name})", shown)
        board = (self.session_dir / "board.log").read_text()
        self.assertIn(names[-1], board)
        self.assertTrue((self.session_dir / "phases/7/project-review_status_contract_1").exists())

    def test_style_assignment_is_prepared_and_captured_through_assign_on_retry(self) -> None:
        self.prepare_style_project()
        _ = (self.project / "new.rs").write_text("fn first() {}\n")
        original = self.server.respond
        outcomes = ["Error", "Findings"]
        prepared: list[Path] = []

        @self.hang_guard.callback
        def style_reply(request: dict[str, object]) -> list[dict[str, object]] | None:
            frames = original(request)
            if request.get("method") != "turn/start" or frames is None:
                return frames
            params = cast(dict[str, object], request["params"])
            inputs = cast(list[dict[str, object]], params["input"])
            prompt = str(inputs[0]["text"])
            self.assertEqual(prompt.count("## Phase style review"), 1)
            self.assertNotIn("write your summary to", prompt)
            diff_line = next(line for line in prompt.splitlines() if line.startswith("- Diff "))
            diff = Path(diff_line.split(": ", 1)[1])
            self.assertIn("+fn ", diff.read_text())
            prepared.append(diff)
            report = ("1. minor: fixture finding at new.rs:1\n" + style_review.BEGIN +
                      "\n| # | Rule | Result |\n| 1 | Fixture | Pass |\nStyle review: " +
                      outcomes.pop(0) + "\n" + style_review.END)
            frames.insert(-1, {"method": "item/completed", "params": {
                "threadId": params["threadId"], "item": {"type": "agentMessage", "text": report}}})
            return frames

        self.server.respond = style_reply
        phase = self.session_dir / "phases/7"
        _ = self.successful("review", "--lens", "contract", "--style-since", "HEAD", execution=InProcessAssignment())
        self.assertEqual(read_object(phase / "style_review_state.json")["state"], "owed")
        name, record = next(iter(self.assignment_records().items()))
        before = prepared[0].read_bytes()
        _ = (self.project / "new.rs").write_text("fn second() {}\n")
        _ = self.successful("review", "--to", name, "--pass", "2", "--style-since", "HEAD", execution=InProcessAssignment())
        self.assertEqual(read_object(phase / "style_review_state.json")["state"], "complete")
        self.assertIn("Style review: Findings", (phase / "phase_style_review.md").read_text())
        self.assertIn("fixture finding", (phase / "project-review_summary_2.txt").read_text())
        self.assertNotEqual(prepared[0].parent, prepared[1].parent)
        self.assertEqual(prepared[0].read_bytes(), before)
        self.assertIn("+fn second", prepared[1].read_text())
        self.assertEqual(self.assignment_records()[name]["thread_id"], record["thread_id"])
        self.assertEqual(len(self.methods("thread/start")), 1)
        self.assertEqual(cast(dict[str, object], self.methods("turn/start")[-1]["params"])["sandboxPolicy"],
                         {"type": "readOnly", "networkAccess": False})

    def test_style_preparation_failure_through_assign_keeps_code_review_running(self) -> None:
        self.prepare_style_project()
        _ = self.successful("review", "--style-since", "missing-baseline", execution=InProcessAssignment())
        phase = self.session_dir / "phases/7"
        self.assertEqual(read_object(phase / "style_review_state.json")["state"], "owed")
        self.assertIn("invalid baseline", (phase / "phase_style_review.md").read_text())
        self.assertEqual((phase / "project-review_status_1").read_text().strip(), "done")
        self.assertEqual(len(self.methods("turn/start")), 1)

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
            result = self.launch(session=False, execution=LauncherScript(ASSIGN))
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
            result = self.launch(session=False, execution=LauncherScript(ASSIGN))
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
        self.outcomes = ["failed"]
        result = self.launch()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual((self.session_dir / "phases/7/impl_status").read_text().strip(), "error")
        ended = [event for event in self.events() if event.get("event_type") == "pass_finished"]
        self.assertEqual(len(ended), 1, self.events())
        self.assertEqual(ended[0].get("status"), "error")
        self.assertNotIn("launcher_pid", next(iter(self.assignment_records().values())))
