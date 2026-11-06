"""Named assignment dispatches through the real recorder and loopback app-server."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import TextIO, cast, final, override
from unittest.mock import patch

from scripts.agents import test_codex_mesh as mesh_fixture
from scripts.direct import assign
from scripts.direct.assign_launcher_test_support import ASSIGN, ROOT, AssignLauncherFixture, LauncherScript, read_object
from scripts.lint import style_review
from scripts.production import staff, test_worker as worker_fixture, worker, worker_admission
from scripts.production.cli_fixture import invoke_cli
from scripts.direct import progress_history
from scripts.lib.atomic_file import write_text_atomic

import herdr


def setUpModule() -> None:
    mesh_fixture.setUpModule()


@final
class PaneReviewLauncherTests(unittest.TestCase):
    """The assignment launcher uses the real dispatch and its transcript receipts."""

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.fixture = worker_fixture.WorkerCommandTests("runTest")
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
        _ = self.fixture.opened("impl", "impl")
        _ = progress_history.begin_dispatch(self.fixture.run_dir, slot="impl", phase="8", role="impl",
            worker_name="impl", launcher_command=["previous-launcher"], prompt=self.fixture.prompt, kind="impl", lens="")
        run_process = cast(Callable[..., subprocess.CompletedProcess[str]], subprocess.run)

        def run_worker_command(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
            if len(command) > 1 and Path(command[1]).resolve() == ROOT / "scripts/production/worker.py":
                result = invoke_cli(worker, lambda: worker.main(command[2:]), command[2:],
                                    cast(dict[str, str], options["env"]), ROOT)
                output = cast(TextIO, options["stdout"])
                _ = output.write(result.stdout + result.stderr)
                return result
            return run_process(command, **options)

        _ = self.enterContext(patch.object(subprocess, "run", side_effect=run_worker_command))
        _ = self.enterContext(patch.object(assign, "require_live_director"))

    def launch_review(self) -> subprocess.CompletedProcess[str]:
        arguments = ["--role", "review", "--to", "review", "--cwd", str(self.fixture.fixture.root),
                     "--phase", "8", "--session-dir", str(self.fixture.run_dir),
                     "--prompt", str(self.fixture.prompt), "--early-ready", str(self.ready)]
        return invoke_cli(assign, assign.main, arguments, os.environ.copy(), ROOT)

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
            worker_name="review", launcher_command=["previous-launcher"], prompt=self.fixture.prompt, kind="impl", lens="")
        before = self.fixture.fixture.staff_state()
        self.assert_start_not_delivered(worker.ExitCode.REFUSED, "role change blocked by active work: active dispatch has another role")
        self.assertEqual(self.fixture.fixture.staff_state(), before)
        self.assertIn(opened.pane, self.fixture.fixture.world.foreground)
        self.assertEqual(self.fixture.fixture.world.stops, [])


@final
class AssignLauncherTests(AssignLauncherFixture):
    def test_herdr_assignment_retains_unknown_workers_without_a_mesh_backend(self) -> None:
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
        _ = self.successful()
        self.assertIn("As your last act, write your summary to",
                      (self.session_dir / "phases/7/dispatch_1_prompt.txt").read_text())
        name = next(iter(self.assignment_records()))
        rows = staff.snapshot().find(kind="worker", parent=staff.StaffId(os.environ["CODEX_THREAD_ID"]))
        self.assertEqual([row.name for row in rows], [name])

    def test_assignment_dispatch_uses_the_backend_thread_as_the_published_worker_id(self) -> None:
        from scripts.production import staff
        _ = self.successful()
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
        _ = self.successful("review", "--lens", "rerun", "--pass", "1")
        starts = self.methods("thread/start")
        self.assertEqual(cast(dict[str, object], starts[0]["params"])["sandbox"], "read-only")
        self.assertTrue((self.session_dir / "phases/7/project-review_status_rerun_1").exists())

    def test_recorder_ad_hoc_phase_dispatches_into_adhoc_directory(self) -> None:
        self.record("start-phase", "--phase-id", "ad hoc", "--phase-title", "Ad hoc review")
        self.write_phase_prompt("ad hoc", "Ad hoc review")
        _ = self.successful("review", "--phase", "ad hoc")
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
                result = self.launch("review", "--phase", phase)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("invalid phase id:", result.stderr)
        self.assertEqual(self.methods("thread/start"), [])

    def test_missing_required_options_and_positionals_refuse_before_opening_a_thread(self) -> None:
        for options in ([], ["--prompt", str(self.prompt)], ["--role", "test"], ["test", str(self.prompt)]):
            with self.subTest(options=options):
                result = self.run_launcher(["bash", str(ASSIGN), *options])
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("usage", (result.stdout + result.stderr).lower())
        self.assertEqual(self.methods("thread/start"), [])

    def test_future_roles_are_refused_before_any_thread_is_opened(self) -> None:
        for role in ("research", "propose"):
            with self.subTest(role=role):
                result = self.launch(role)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.methods("thread/start"), [])

    def test_writer_paths_are_required_and_review_paths_are_refused(self) -> None:
        for role in ("implement", "test", "fix"):
            with self.subTest(role=role):
                self.assertEqual(self.launch(role, paths=False).returncode, 2)
        self.assertEqual(self.launch("review", "--paths", str(self.paths)).returncode, 2)
        self.assertEqual(self.methods("thread/start"), [])

    def test_unsafe_empty_and_escaping_paths_refuse_before_claim(self) -> None:
        outside = self.project.parent / "outside"
        outside.mkdir()
        (self.project / "linked").symlink_to(outside, target_is_directory=True)
        for path in ("", "../secret", "/tmp/secret", "src/../secret", "linked/secret"):
            with self.subTest(path=path):
                _ = self.paths.write_text(path + "\n")
                result = self.launch()
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
        _ = self.successful("review", "--lens", "contract", "--pass", "1")
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
        _ = self.successful("review", "--lens", "contract", "--style-since", "HEAD")
        self.assertEqual(read_object(phase / "style_review_state.json")["state"], "owed")
        name, record = next(iter(self.assignment_records().items()))
        before = prepared[0].read_bytes()
        _ = (self.project / "new.rs").write_text("fn second() {}\n")
        _ = self.successful("review", "--to", name, "--pass", "2", "--style-since", "HEAD")
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
        _ = self.successful("review", "--style-since", "missing-baseline")
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
        _ = self.successful("review", "--style-since", baseline, "--style-until", end)
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
        refused = self.launch("fix")
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertEqual(self.turn_starts, 0)
        _ = self.successful("fix", "--fix-cause", "repair failed acceptance gate")
        self.assertTrue(any(event.get("pass_kind") == "fix" for event in self.events()))
        _ = self.prompt.write_text("Phase 6 — Another phase\nDo the wrong work.\n")
        result = self.launch()
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
