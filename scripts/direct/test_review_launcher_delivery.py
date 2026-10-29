"""Check launch refusal, early delivery, cancellation and timeout cleanup."""

from __future__ import annotations

import unittest
from scripts.direct import review_launcher_test_support as support
from scripts.direct.test_review_launcher_pool import own_review_with_legacy_flags
from typing import override
from pathlib import Path
from scripts.direct.review_launcher_test_support import ProgressState as ProgressState
from scripts.direct.review_launcher_test_support import TEST_HANG_GUARD_SECS as TEST_HANG_GUARD_SECS
from typing import cast
import json
import os
import select
import signal
import subprocess
import sys
from scripts.direct.wait_for_test_state import wait_for_file, wait_for_pid
from scripts.direct.wait_for_test_state import wait_until


class ReviewLauncherPassTests(support.ReviewLauncherFixture):
    @override
    def pool_command(self, session_dir: Path, *, lens: str = "contract", phase: str = "3",
                     early: str = "", reviewer_type: str = "code", pass_index: int = 1) -> list[str]:
        return own_review_with_legacy_flags(self, session_dir, lens=lens, phase=phase, early=early,
                                           reviewer_type=reviewer_type, pass_index=pass_index)

    def test_ux_launch_checks_identity_before_the_worker_and_records_valid_checks(self) -> None:
        stale = self.start_implementation("ux-stale-identity")
        self.finish_implementation(stale)
        _ = self.prompt_file.write_text("Phase 3 — Old title\nReview the layout.\n")
        refused = self.checked_review(stale, "--phase", "3", lens="ux")
        self.assertNotEqual(refused.returncode, 0, refused.stderr)
        self.assertFalse((stale / "review-worker-started").exists())
        rejected, = [row for row in self.events(stale.name) if row["event_type"] == "launch_refused"]
        self.assertEqual(rejected["prompt_file"], str(self.prompt_file))
        self.assertEqual(rejected["phase_id"], "3")
        self.assertTrue(rejected["reason"])
        self.assertEqual((stale / "phases/3/review_status_ux").read_text().strip(), "error")

        valid = self.start_implementation("ux-valid-identity")
        self.finish_implementation(valid)
        _ = self.prompt_file.write_text("Phase 3 — Retry handling\nReview the layout.\n")
        launched = self.checked_review(valid, "--phase", "3", lens="ux")
        self.assertEqual(launched.returncode, 0, launched.stderr)
        self.assertTrue((valid / "review-worker-started").exists())
        events = self.events(valid.name)
        checked, = [row for row in events if row["event_type"] == "launch_checked"]
        check = cast(dict[str, object], checked["launch_check"])
        self.assertEqual((check["phase_id"], check["phase_title"], check["prompt_phase_line"]),
                         ("3", "Retry handling", "matched"))
        self.assertFalse(any(row["event_type"] == "pass_started" and row["pass_kind"] == "review"
                             for row in events))

    def test_an_early_review_with_no_phase_refuses_before_starting_its_worker(self) -> None:
        session = self.start_implementation("early-missing-phase")
        self.finish_implementation(session)
        result = self.checked_review(session, early=str(session / "not-ready"))
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertFalse((session / "review-worker-started").exists())
        self.assertEqual((session / "review_status").read_text().strip(), "error")
        refused, = [row for row in self.events(session.name) if row["event_type"] == "launch_refused"]
        self.assertEqual(refused["phase_id"], "")
        self.assertIn("phase", str(refused["reason"]).lower())
        self.assertFalse(any(row["event_type"] == "pass_started" and row["pass_kind"] == "review"
                             for row in self.events(session.name)))

    def test_review_passes_the_prompt_file_to_the_launch_check(self) -> None:
        session = self.start_implementation("checked-review")
        self.finish_implementation(session)
        _ = self.prompt_file.write_text("Phase 3 — Retry handling\nReview the diff.\n")
        result = self.checked_review(session, "--phase", "3")
        self.assertEqual(result.returncode, 0, result.stderr)
        review, = [row for row in self.events(session.name)
                   if row["event_type"] == "pass_started" and row["pass_kind"] == "review"]
        check = cast(dict[str, object], review["launch_check"])
        self.assertEqual((check["phase_id"], check["phase_title"], check["prompt_phase_line"]),
                         ("3", "Retry handling", "matched"))
        self.assertTrue((session / "review-worker-started").exists())

    def test_review_stops_before_the_worker_on_a_recorder_refusal(self) -> None:
        for label, options, text in (
            ("stale-prompt", ("--phase", "3"), "Phase 3 — Wrong title\n"),
            ("missing-phase", (), "Review the diff.\n"),
        ):
            with self.subTest(label=label):
                session = self.start_implementation(f"checked-review-{label}")
                self.finish_implementation(session)
                _ = self.prompt_file.write_text(text)
                result = self.checked_review(session, *options)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((session / "review-worker-started").exists())
                records = self.events(session.name)
                self.assertFalse(any(row["event_type"] == "pass_started" and row["pass_kind"] == "review"
                                     for row in records))
                refused, = [row for row in records if row["event_type"] == "launch_refused"]
                self.assertEqual(refused["prompt_file"], str(self.prompt_file))
                self.assertEqual(refused["pass_kind"], "review")
                self.assertTrue(refused["reason"])
                self.assertIn("prompt" if label == "stale-prompt" else "phase", result.stderr.lower())

    def test_early_delivery_probes_default_to_five_seconds_without_test_overrides(self) -> None:
        session = self.start_implementation("default-early-poll")
        worker = self.open_worker(session)
        self.finish_implementation(session)
        ready, finish = session / "ready", session / "finish"
        binaries = session / "bin"
        binaries.mkdir()
        sleep = binaries / "sleep"
        # The watcher also calls sleep. Force it to arrive first, but only the
        # launcher's own probe may publish the delivery sentinel. Match callers
        # by pid, not by the expected argument, so a wrong default still fails.
        _ = sleep.write_text(
            '#!/bin/bash\nset -eu\n'
            + 'if [[ "$PPID" == "$TEST_REVIEW_LAUNCHER_PID" ]]; then\n'
            + '  "$TEST_PYTHON" "$TEST_WAIT_HELPER_DIR/wait_for_test_state.py" "$TEST_HEARTBEAT_ENTERED"\n'
            + '  printf "%s\\n" "$*" >> "$TEST_POLL_ARGUMENTS"\n'
            + '  : > "$TEST_POLL_ENTERED"\n'
            + '  : > "$TEST_EARLY_READY"\n'
            + 'else\n'
            + '  printf "%s\\n" "$*" >> "$TEST_HEARTBEAT_ARGUMENTS"\n'
            + '  : > "$TEST_HEARTBEAT_ENTERED"\n'
            + '  exec "$TEST_PYTHON" "$TEST_WAIT_HELPER_DIR/wait_for_test_state.py" "$STUB_REVIEW_FINISH_FILE"\n'
            + 'fi\n'
        )
        sleep.chmod(0o755)
        entered = session / "poll-entered"
        arguments = session / "poll-arguments"
        heartbeat_arguments = session / "heartbeat-arguments"
        environment = self.environment()
        for key in ("UNIT_WORKER_POLL_SECS", "UNIT_WORKER_WAIT_ATTEMPTS",
                    "UNIT_EARLY_POLL_SECS", "UNIT_TOKEN_CLOCK_FILE"):
            _ = environment.pop(key, None)
        environment.update({"PATH": str(binaries) + os.pathsep + environment["PATH"],
                            "TEST_POLL_ARGUMENTS": str(arguments), "TEST_POLL_ENTERED": str(entered),
                            "TEST_HEARTBEAT_ARGUMENTS": str(heartbeat_arguments),
                            "TEST_HEARTBEAT_ENTERED": str(session / "heartbeat-entered"),
                            "TEST_PYTHON": sys.executable,
                            "TEST_EARLY_READY": str(ready), "STUB_REVIEW_FINISH_FILE": str(finish)})
        command = ["bash", "-c", 'export TEST_REVIEW_LAUNCHER_PID=$$; exec "$@"', "review-under-test",
                   *self.review_command(session, "adversary", str(ready), worker)]
        process = subprocess.Popen(command,
                                   env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   start_new_session=True)
        try:
            wait_until(entered.exists, session, timeout=TEST_HANG_GUARD_SECS, processes=(process,))
            self.assertEqual(heartbeat_arguments.read_text().splitlines(), ["1"])
            self.assertEqual(arguments.read_text().splitlines(), ["5"])
            self.assertIsNone(process.poll())
            wait_until(lambda: any(event.get("pass_kind") == "review" for event in self.pass_events(session.name)),
                       self.history_dir / "runs", timeout=TEST_HANG_GUARD_SECS, processes=(process,))
        finally:
            output, errors = self.finish_early_launcher(process, ready, finish)
        self.assertEqual(process.returncode, 0, output + errors)

    def test_early_launcher_timeout_reaps_its_group_before_fixture_cleanup(self) -> None:
        child_ready = self.root / "blocked-review-child"
        child_code = ("import os, signal, sys; from pathlib import Path; "
                      "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                      "p = Path(sys.argv[1]); staged = p.with_name(p.name + '.tmp'); "
                      "staged.write_text(str(os.getpid())); os.replace(staged, p); signal.pause()")
        program = f"import subprocess, sys; child = subprocess.Popen([sys.executable, '-c', {child_code!r}, {str(child_ready)!r}]); child.wait()"
        process = subprocess.Popen([sys.executable, "-c", program], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True)
        ready, finish = self.root / "early-ready", self.root / "early-finish"
        try:
            descriptor = os.pidfd_open(wait_for_pid(child_ready, processes=(process,)))
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    _ = self.finish_early_launcher(process, ready, finish, timeout=0)
                self.assertIsNotNone(process.poll())
                self.assertEqual(select.select([descriptor], [], [], TEST_HANG_GUARD_SECS)[0], [descriptor])
                self.assertTrue(ready.exists())
                self.assertTrue(finish.exists())
                self.assertTrue(process.stdout is not None and process.stdout.closed)
                self.assertTrue(process.stderr is not None and process.stderr.closed)
            finally:
                os.close(descriptor)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            _ = process.communicate()

    def test_a_reviewer_that_finishes_before_delivery_records_no_pass(self) -> None:
        session_dir = self.start_implementation("early")
        ready = session_dir / "final_diff_1.ready"
        result = self.run_review(session_dir, ready)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((session_dir / "review_status").read_text().strip(), "reviewed")

        recorded = self.pass_events("early")
        self.assertEqual(
            [event["event_type"] for event in recorded],
            ["pass_started"],
            "the implementation pass must be the only pass on the record",
        )
        self.assertEqual(recorded[0].get("pass_kind"), "impl")

        state = cast(
            ProgressState,
            json.loads((session_dir / "progress_history_state.json").read_text()),
        )
        open_passes = state["pass"]
        self.assertEqual(len(open_passes), 1)
        open_pass = next(iter(open_passes.values()))
        self.assertEqual(open_pass["kind"], "impl")
        self.assertEqual(open_pass["status"], "active")

    def test_a_reviewer_records_its_pass_once_the_sentinel_appears(self) -> None:
        session_dir = self.start_implementation("delivered")
        self.recorder("finish-pass", "--session-dir", str(session_dir), "--status", "completed")
        ready = session_dir / "final_diff_1.ready"
        _ = (session_dir / "final_diff_1.diff").write_text("diff\n", encoding="utf-8")
        _ = ready.write_text("", encoding="utf-8")

        result = self.run_review(session_dir, ready)
        self.assertEqual(result.returncode, 0, result.stderr)

        kinds = [
            (event["event_type"], event.get("pass_kind"))
            for event in self.pass_events("delivered")
        ]
        self.assertEqual(
            kinds,
            [
                ("pass_started", "impl"),
                ("pass_finished", "impl"),
                ("pass_started", "review"),
                ("pass_finished", "review"),
            ],
        )

    def test_an_early_assigned_review_baselines_after_the_writers_final_edit(self) -> None:
        session_dir = self.start_implementation("early-writer-edit")
        worker = self.open_worker(session_dir)
        ready = session_dir / "final_diff_1.ready"
        finish_review = session_dir / "finish_review"
        environment = self.environment()
        environment["STUB_REVIEW_FINISH_FILE"] = str(finish_review)
        process = subprocess.Popen(
            self.review_command(session_dir, "adversary", str(ready), worker),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )

        _ = (self.working_dir / "reviewed.txt").write_text(
            "writer final edit\n", encoding="utf-8"
        )
        self.finish_implementation(session_dir)
        ready.touch()
        try:
            wait_until(lambda: any(
                event.get("event_type") == "pass_started" and event.get("pass_kind") == "review"
                for event in self.pass_events("early-writer-edit")
            ), self.history_dir / "runs")
        except TimeoutError:
            process.kill()
            _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            raise
        finish_review.touch()
        _stdout, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)

        self.assertEqual(process.returncode, 0, errors)
        self.assertEqual(
            (session_dir / "review_status_adversary").read_text(encoding="utf-8"),
            "reviewed\n",
        )
        calls = [cast(list[str], json.loads(line)) for line in
                 (self.root / "mesh_runs_calls.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call[:5], ["start", "--session-dir", str(session_dir), "--to", worker])
        self.assertEqual(call[call.index("--label") + 1], "final diff ready")
        stage_two = Path(call[call.index("--done-message-file") + 1]).read_text(encoding="utf-8")
        self.assertIn(f"Stage 2: the final diff is at {session_dir / 'final_diff_1.diff'}", stage_two)
        self.assertIn(str(ready), call)

    def test_a_assigned_early_review_before_delivery_records_no_pass(self) -> None:
        session_dir = self.start_implementation("assigned-early")
        worker = self.open_worker(session_dir)
        ready = session_dir / "final_diff_1.ready"

        result = subprocess.run(
            self.review_command(session_dir, "adversary", str(ready), worker),
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
            timeout=TEST_HANG_GUARD_SECS,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (session_dir / "review_status_adversary").read_text(encoding="utf-8"),
            "reviewed\n",
        )
        recorded = self.pass_events("assigned-early")
        self.assertEqual(
            [(event["event_type"], event.get("pass_kind")) for event in recorded],
            [("pass_started", "impl")],
        )
        heartbeat = (session_dir / "heartbeat.log").read_text(encoding="utf-8")
        self.assertIn("finished before delivery", heartbeat)

    def test_early_verdict_without_delivery_records_void_and_findings_without_a_pass(self) -> None:
        for arm in ("pool", "own"):
            with self.subTest(arm=arm):
                session = self.start_implementation(f"void-{arm}")
                ready = session / "ready"
                if arm == "pool":
                    environment = self.pool_fixture()
                    command = self.pool_command(session, early=str(ready))
                else:
                    environment = self.environment()
                    command = self.review_command(session, "contract", str(ready), self.open_worker(session))
                environment["STUB_FINDINGS"] = "1. A void finding\n"
                result = subprocess.run(command, env=environment, capture_output=True,
                                        text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((session / "review_status_contract").read_text().strip(), "reviewed")
                record, = self.turn_records(session)
                self.assertEqual(record["status"], "void")
                self.assertEqual(record["raised"], 1)
                self.assertFalse(any(event.get("pass_kind") == "review" for event in self.pass_events(session.name)))

    def test_own_term_records_canceled_instead_of_successful_exit_trap(self) -> None:
        session = self.start_implementation("own-signal")
        self.finish_implementation(session)
        worker = self.open_worker(session)
        environment = self.environment()
        finish = self.root / "finish"
        done = self.root / "done"
        environment["STUB_REVIEW_FINISH_FILE"] = str(finish)
        environment["STUB_REVIEW_DONE_FILE"] = str(done)
        # File output lets the launcher exit without an inherited pipe hiding it.
        with (self.root / "signal-output.txt").open("w+") as output:
            process = subprocess.Popen(self.review_command(session, "", "", worker), env=environment,
                                       stdout=output, stderr=output, text=True)
            try:
                wait_until(lambda: (session / "follow_message.txt").exists() or process.poll() is not None, session, processes=(process,))
                self.assertIsNone(process.poll())
                self.assertTrue((session / "follow_message.txt").exists())
                process.send_signal(signal.SIGTERM)
                code = process.wait(timeout=TEST_HANG_GUARD_SECS)
                record, = self.turn_records(session)
                self.assertEqual(record["status"], "canceled")
                self.assertEqual(code, 143)
            finally:
                finish.touch()
                if process.poll() is None:
                    process.terminate()
                _ = process.wait(timeout=TEST_HANG_GUARD_SECS)
                wait_for_file(done)
                self.assertTrue(done.exists(), "the owned stub child must finish before fixture cleanup")

    def test_the_wrapper_names_a_verdict_it_did_not_record(self) -> None:
        session_dir = self.start_implementation("void")
        result = self.run_review(session_dir, session_dir / "final_diff_1.ready")
        self.assertEqual(result.returncode, 0, result.stderr)
        heartbeat = (session_dir / "heartbeat.log").read_text(encoding="utf-8")
        self.assertIn("finished before delivery", heartbeat)
        self.assertIn("verdict void", heartbeat)

    def test_an_unknown_lens_is_refused_before_anything_runs(self) -> None:
        session_dir = self.start_implementation("unknown-lens")
        result = subprocess.run(
            self.review_command(session_dir, "style", ""),
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
            timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("unknown review lens 'style' (adversary, contract, craft, ux, rerun)", result.stderr)
        self.assertFalse((session_dir / "review_status_style").exists())
        self.assertEqual(
            [event["event_type"] for event in self.pass_events("unknown-lens")],
            ["pass_started"],
        )

    def test_unknown_lens_lists_ux_among_accepted_lenses(self) -> None:
        session = self.start_implementation("unknown-lens")
        result = subprocess.run(self.review_command(session, "unknown", ""), env=self.environment(),
                                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("adversary, contract, craft, ux", result.stderr)


if __name__ == "__main__":
    _ = unittest.main()
