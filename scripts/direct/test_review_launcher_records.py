"""Check review passes and turns are recorded in the requested phase."""

from __future__ import annotations

import unittest
from scripts.direct import review_launcher_test_support as support
from scripts.direct.review_launcher_test_support import TEST_HANG_GUARD_SECS as TEST_HANG_GUARD_SECS
from typing import cast
import json
import subprocess


class ReviewLauncherPassTests(support.ReviewLauncherFixture):
    def test_own_turn_records_survive_two_phases_in_the_same_session(self) -> None:
        session = self.start_implementation("two-phases")
        self.finish_implementation(session)
        worker = self.open_worker(session)
        first = self.run_assigned_review(session, worker)
        self.assertEqual(first.returncode, 0, first.stderr)
        first_record, = self.turn_records(session)
        self.recorder("finish-phase", "--session-dir", str(session), "--status", "completed")
        self.recorder("start-phase", "--session-dir", str(session), "--phase-id", "4", "--phase-title", 'Next "phase"')
        second = self.run_assigned_review(session, worker)
        self.assertEqual(second.returncode, 0, second.stderr)
        records = self.turn_records(session)
        self.assertEqual(len(records), 2)
        self.assertIn(first_record, records)
        self.assertEqual(len({str(record["phase_instance_id"]) for record in records}), 2)
        self.assertTrue(all(record["unit"] == record["phase"] == "" for record in records))

    def test_own_ux_lens_records_ux_kind_without_pass_calls(self) -> None:
        session = self.start_implementation("own-ux")
        self.finish_implementation(session)
        worker = self.open_worker(session)
        environment = self.environment()
        self.trace_pass_recorder(environment)
        result = subprocess.run(self.review_command(session, "ux", "", worker), env=environment,
                                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.turn_records(session)[0]["kind"], "ux")
        self.assertEqual(self.call_records("pass-calls.jsonl"), [])

    def test_a_review_naming_an_older_open_phase_starts_its_pass_in_that_phase(self) -> None:
        session = self.start_implementation("named-phase-pass")
        self.finish_implementation(session)
        self.recorder("start-phase", "--session-dir", str(session), "--phase-id", "4", "--phase-title", "Next phase")
        environment = self.pool_fixture()
        self.trace_pass_recorder(environment)
        command = self.pool_command(session)
        command[2:2] = ["--phase", "3"]
        result = subprocess.run(command, env=environment, capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        start, = [call for call in self.call_records("pass-calls.jsonl") if call["verb"] == "start-pass"]
        self.assertEqual(cast("list[str]", start["argv"])[-2:], ["--phase-id", "3"])

    def test_a_pool_review_naming_an_older_open_phase_records_its_turn_in_that_phase(self) -> None:
        session = self.start_implementation("named-phase-turn")
        self.finish_implementation(session)
        older = cast("dict[str, object]", json.loads(
            (session / "progress_history_state.json").read_text(encoding="utf-8"))["phase"])
        self.recorder("start-phase", "--session-dir", str(session), "--phase-id", "4", "--phase-title", "Next phase")
        environment = self.pool_fixture()
        environment["STUB_FINDINGS"] = "1. First finding\n2. Second finding\n"
        command = self.pool_command(session)
        command[2:2] = ["--phase", "3"]
        result = subprocess.run(command, env=environment, capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        record, = self.turn_records(session)
        current = cast("dict[str, object]", json.loads(
            (session / "progress_history_state.json").read_text(encoding="utf-8"))["phase"])
        self.assertNotEqual(older["instance_id"], current["instance_id"])
        self.assertEqual(record["phase_instance_id"], older["instance_id"])
        self.assertEqual(record["raised"], 2)

    def test_a_review_naming_its_phase_writes_its_files_in_that_phase_directory(self) -> None:
        # Two phases reviewing at once each pass their own id, so neither reads the other's findings.
        session = self.start_implementation("phase-directory-files")
        self.finish_implementation(session)
        environment = self.pool_fixture()
        command = self.pool_command(session)
        command[2:2] = ["--phase", "3"]
        result = subprocess.run(command, env=environment, capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        phase_dir = session / "phases" / "3"
        self.assertTrue(list(phase_dir.glob("review_findings_*.txt")))
        self.assertTrue(list(phase_dir.glob("review_status*")))
        self.assertEqual(sorted(path.name for path in session.glob("review_*")), [])

    def test_a_pool_review_names_the_director_in_the_unit_run_folder(self) -> None:
        session = self.start_implementation("director")
        self.finish_implementation(session)
        environment = self.pool_fixture()
        environment["CLAUDE_CODE_SESSION_ID"] = "sid-director"
        result = subprocess.run(self.pool_command(session), env=environment, capture_output=True, text=True,
                                check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((session / "director").read_text(encoding="utf-8"), "sid-director\n")

    def test_a_phase_id_with_a_space_names_a_directory_without_it(self) -> None:
        session = self.start_implementation("phase-directory-name")
        self.finish_implementation(session)
        command = self.pool_command(session)
        command[2:2] = ["--phase", "ad hoc"]
        result = subprocess.run(command, env=self.pool_fixture(), capture_output=True, text=True,
                                check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertNotEqual(result.returncode, 2, result.stderr)
        self.assertTrue(list((session / "phases" / "adhoc").glob("review_status*")))

    def test_own_failed_closure_turn_records_its_findings_count(self) -> None:
        session = self.start_implementation("own-failed")
        self.finish_implementation(session)
        worker = self.open_worker(session)
        environment = self.environment()
        environment["STUB_TURN_FAILURE"] = "1"
        environment["STUB_FINDINGS"] = "1. Partial finding\n"
        command = self.review_command(session, "", "", worker)
        command[-3] = "2"
        result = subprocess.run(command, env=environment, capture_output=True,
                                text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 1, result.stderr)
        record, = self.turn_records(session)
        self.assertEqual(record["kind"], "closure")
        self.assertEqual(record["pass"], 2)
        self.assertEqual(record["raised"], 1)
        self.assertEqual(record["status"], "error")
        self.assertEqual(record["phase"], "")

    def test_failed_pool_attempt_and_own_retry_leave_one_record_each_and_ignore_review_lease(self) -> None:
        session = self.start_implementation("pool-retry")
        self.finish_implementation(session)
        environment = self.pool_fixture()
        environment["STUB_POOL_FAILURE"] = "1"
        failed = subprocess.run(self.pool_command(session), env=environment, capture_output=True,
                                text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(failed.returncode, 1, failed.stderr)
        self.assertEqual((session / "review_status_contract").read_text().strip(), "error")
        self.assertEqual([call["verb"] for call in self.call_records("pool-calls.jsonl")], ["allocate"])
        worker = self.open_worker(session)
        retry = subprocess.run(self.review_command(session, "contract", "", worker), env=environment,
                               capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(retry.returncode, 0, retry.stderr)
        records = {str(record["arm"]): record for record in self.turn_records(session)}
        self.assertEqual(set(records), {"pool", "own"})
        self.assertEqual(records["pool"]["status"], "error")
        self.assertEqual(records["own"]["status"], "reviewed")
        self.assertEqual(records["own"]["thread_id"], "thread-fixture")
        self.assertEqual((records["own"]["unit"], records["own"]["phase"]), ("", ""))

    def test_code_pool_review_still_starts_and_finishes_a_pass(self) -> None:
        session = self.start_implementation("code-pass")
        self.finish_implementation(session)
        environment = self.pool_fixture()
        self.trace_pass_recorder(environment)
        result = subprocess.run(self.pool_command(session), env=environment, capture_output=True,
                                text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call["verb"] for call in self.call_records("pass-calls.jsonl")],
                         ["start-pass", "finish-pass"])


if __name__ == "__main__":
    _ = unittest.main()
