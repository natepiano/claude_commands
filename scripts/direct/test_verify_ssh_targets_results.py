"""Check SSH results with private verification processes."""

from __future__ import annotations

import json
from typing import cast

from scripts.direct.verify_ssh_targets_test_support import VerifySshTargetsFixture


class VerifySshResultsTests(VerifySshTargetsFixture):
    def only_attempt(self) -> dict[str, object]:
        attempts = self.attempts()
        self.assertEqual(len(attempts), 1,
                         json.dumps({"attempts": attempts, "events": self.ssh_events()})
                         + repr(self.admission.server.assignments))
        return attempts[0]

    def test_failed_probe_with_live_leftovers_continues_without_backoff(self) -> None:
        self.configure("aws,spare,natedev")
        with self.spare_after_aws_refusal():
            result = self.verify("test", "sample", "--no-cache", extra={
                "TEST_SSH_MODES": '{"aws":"probe-leftovers"}', "BUILDLOG_MEM_WAIT_SINCE": "100.25"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        events = self.ssh_events()
        self.assertEqual(len(events), 2, json.dumps(events) + repr(self.admission.server.assignments))
        first, second = events
        self.assertEqual((first["target"], second["target"]), ("aws", "spare"))
        self.assertEqual(first["result_type"], "BusyMacDecline")
        self.assertEqual(first["holder"], "LostRunLeftovers")
        self.assertNotIn("run", first["actions"])
        self.assertNotIn("release", first["actions"])
        self.assertEqual(first["queued_at"], second["queued_at"])
        state = self.target_state("aws")
        self.assertTrue(list((state / "claims").glob("**/*.json")))
        self.assertFalse((state / "unreachable_until").exists())
        attempt = self.attempts()[0]
        self.assertEqual((attempt["cleanup"], attempt["unresolved_count"]), ("unconfirmed", 2))

    def test_no_matching_tests_have_a_distinct_durable_outcome(self) -> None:
        self.configure("aws,natedev")
        result = self.verify("test", "sample", "--no-cache", extra={"TEST_SSH_MODES": '{"aws":"no-tests"}'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        attempt = self.only_attempt()
        self.assertEqual((attempt["outcome"], attempt["completion"], attempt["cleanup"]),
                         ("no_tests", "confirmed", "confirmed"))
        self.assertEqual(len(self.local_runs()), 1)

    def test_linux_loss_keeps_the_attempt_and_queue_age_after_local_success(self) -> None:
        self.configure("aws,natedev")
        first, wait_environment = self.first_wait()
        result = self.verify("test", "sample", "--no-cache", extra={
            "TEST_SSH_MODES": '{"aws":"lost"}', **wait_environment})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.local_runs()), 1)
        self.assertTrue(self.ssh_events(), result.stdout + result.stderr)
        self.assertTrue(self.attempts(), json.dumps(self.ssh_events()) + result.stdout + result.stderr)
        attempt = self.only_attempt()
        self.assertEqual((attempt["target"], attempt["slot"], attempt["outcome"], attempt["queued_at"]),
                         ("aws", 1, "lost", 100.25))
        self.assertEqual(attempt["repo_identity"], "https://example.com/team/hana")
        self.assertEqual(self.routing_record()["target"], "natedev")
        self.assertEqual(first.read_text().strip(), "100.25")

    def test_interruption_preserves_queue_age_and_records_local_state_before_compilation(self) -> None:
        self.configure("aws,natedev")
        self.seed_controller()
        first, wait_environment = self.first_wait()
        cargo = self.bin_directory / "cargo"
        original = self.bin_directory / "cargo-original"
        _ = cargo.rename(original)
        self.write_executable(cargo, r'''
import json
import os
from pathlib import Path
import sys

with open(Path(os.environ["TEST_SOURCE_REPO"]) / "scripts/lib/package_root.py") as package_root: exec(package_root.read(), {"__file__": package_root.name})
if sys.argv[1:3] == ["nextest", "run"]:
    from scripts.direct.test_verify_step_state import read_step_row
    record = read_step_row(os.environ["BUILDLOG_CALL_ID"])
    path = Path(os.environ["TEST_RUNNER_STATE"]) / "local-step.json"
    path.write_text(json.dumps(record))
os.execv(str(Path(__file__).with_name("cargo-original")), [str(Path(__file__).with_name("cargo-original")), *sys.argv[1:]])
''')
        result = self.verify("test", "sample", "--no-cache", extra={
            "TEST_SSH_MODES": '{"aws":"interrupted"}', **wait_environment})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        event, = self.ssh_events()
        self.assertEqual(event["result_type"], "InterruptedTargetRun")
        attempt = self.only_attempt()
        self.assertEqual((attempt["target"], attempt["slot"], attempt["outcome"], attempt["queued_at"]),
                         ("aws", 1, "lost", 100.25))
        local = cast(dict[str, object], json.loads((self.runner_state / "local-step.json").read_text()))
        self.assertEqual(local["state"], "on_natedev")
        self.assertEqual(local["selected_target"], "natedev")
        self.assertEqual(first.read_text().strip(), "100.25")
        self.assertEqual(len(self.local_runs()), 1)

    def test_probe_interruption_preserves_the_attempt_and_original_queue_timestamp(self) -> None:
        self.configure("aws,natedev")
        self.seed_controller()
        first, wait_environment = self.first_wait()
        result = self.verify("test", "sample", "--no-cache", extra={
            "TEST_SSH_MODES": '{"aws":"probe-interrupted"}', **wait_environment})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        event, = self.ssh_events()
        self.assertEqual(event["result_type"], "InterruptedTargetRun")
        attempt = self.only_attempt()
        self.assertEqual((attempt["target"], attempt["outcome"], attempt["queued_at"]), ("aws", "lost", 100.25))
        self.assertEqual(first.read_text().strip(), "100.25")
        self.assertEqual(len(self.local_runs()), 1)

    def test_interruption_from_another_generation_remains_an_unconfirmed_loss(self) -> None:
        self.configure("aws,natedev")
        self.seed_controller()
        result = self.verify("check", "sample", "--no-cache", extra={
            "TEST_SSH_MODES": '{"aws":"other-generation"}'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        event, = self.ssh_events()
        self.assertEqual(event["result_type"], "LostMacRun")
        attempt = self.only_attempt()
        self.assertEqual(attempt["outcome"], "lost")

    def test_sigterm_runner_output_preserves_the_sender_interruption_result(self) -> None:
        self.configure("aws,natedev")
        self.seed_controller()

        result = self.verify("test", "sample", "--no-cache", extra={
            "TEST_SSH_MODES": '{"aws":"runner-sigterm"}', "BUILDLOG_MEM_WAIT_SINCE": "100.25",
        })

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        event, = self.ssh_events()
        self.assertEqual(event["result_type"], "InterruptedTargetRun", json.dumps(event))
        self.assertEqual(event["interruption_generation"], "fixture-generation")
        self.assertEqual(event["remote_status"], 143)
        lines = event["remote_output"].splitlines()
        self.assertEqual([line for line in lines if line.startswith("aws_interrupted=")],
                         ["aws_interrupted=fixture-generation"])
        attempt = self.only_attempt()
        self.assertEqual((attempt["target"], attempt["outcome"], attempt["queued_at"]), ("aws", "lost", 100.25))
        self.assertEqual(len(self.local_runs()), 1)

    def test_confirmed_linux_failure_never_repeats_the_test_locally(self) -> None:
        self.configure("aws,natedev")
        result = self.verify("test", "sample", "--no-cache", extra={"TEST_SSH_MODES": '{"aws":"failed"}'})
        self.assertEqual(result.returncode, 42, result.stdout + result.stderr)
        self.assertEqual(self.local_runs(), [])
        attempt = self.only_attempt()
        self.assertEqual(self.ssh_events()[0]["result_type"], "FailedOnTarget")
        self.assertEqual((attempt["outcome"], attempt["completion"], attempt["cleanup"]),
                         ("failed", "confirmed", "confirmed"))
