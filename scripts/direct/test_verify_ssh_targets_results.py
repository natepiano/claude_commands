"""Check SSH results with private verification processes."""

from __future__ import annotations

import json

from scripts.direct.verify_ssh_targets_test_support import VerifySshTargetsFixture


class VerifySshResultsTests(VerifySshTargetsFixture):
    def only_attempt(self) -> dict[str, object]:
        attempts = self.attempts()
        self.assertEqual(len(attempts), 1,
                         json.dumps({"attempts": attempts, "events": self.ssh_events()})
                         + repr(self.admission.server.assignments))
        return attempts[0]

    def test_failed_probe_with_live_leftovers_continues_without_backoff(self) -> None:
        self.configure("linux,spare,natedev")
        with self.spare_after_linux_refusal():
            result = self.verify("test", "sample", "--no-cache", extra={
                "TEST_SSH_MODES": '{"linux":"probe-leftovers"}', "BUILDLOG_MEM_WAIT_SINCE": "100.25"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        events = self.ssh_events()
        self.assertEqual(len(events), 2, json.dumps(events) + repr(self.admission.server.assignments))
        first, second = events
        self.assertEqual((first["target"], second["target"]), ("linux", "spare"))
        self.assertEqual(first["result_type"], "BusyMacDecline")
        self.assertEqual(first["holder"], "LostRunLeftovers")
        self.assertNotIn("run", first["actions"])
        self.assertNotIn("release", first["actions"])
        self.assertEqual(first["queued_at"], second["queued_at"])
        state = self.target_state("linux")
        self.assertTrue(list((state / "claims").glob("**/*.json")))
        self.assertFalse((state / "unreachable_until").exists())
        attempt = self.attempts()[0]
        self.assertEqual((attempt["cleanup"], attempt["unresolved_count"]), ("unconfirmed", 2))

    def test_no_matching_tests_have_a_distinct_durable_outcome(self) -> None:
        self.configure("linux,natedev")
        result = self.verify("test", "sample", "--no-cache", extra={"TEST_SSH_MODES": '{"linux":"no-tests"}'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        attempt = self.only_attempt()
        self.assertEqual((attempt["outcome"], attempt["completion"], attempt["cleanup"]),
                         ("no_tests", "confirmed", "confirmed"))
        self.assertEqual(len(self.local_runs()), 1)

    def test_linux_loss_keeps_the_attempt_and_queue_age_after_local_success(self) -> None:
        self.configure("linux,natedev")
        first, wait_environment = self.first_wait()
        result = self.verify("test", "sample", "--no-cache", extra={
            "TEST_SSH_MODES": '{"linux":"lost"}', **wait_environment})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.local_runs()), 1)
        self.assertTrue(self.ssh_events(), result.stdout + result.stderr)
        self.assertTrue(self.attempts(), json.dumps(self.ssh_events()) + result.stdout + result.stderr)
        attempt = self.only_attempt()
        self.assertEqual((attempt["target"], attempt["slot"], attempt["outcome"], attempt["queued_at"]),
                         ("linux", 1, "lost", 100.25))
        self.assertEqual(attempt["repo_identity"], "https://example.com/team/hana")
        self.assertEqual(self.routing_record()["target"], "natedev")
        self.assertEqual(first.read_text().strip(), "100.25")

    def test_confirmed_linux_failure_never_repeats_the_test_locally(self) -> None:
        self.configure("linux,natedev")
        result = self.verify("test", "sample", "--no-cache", extra={"TEST_SSH_MODES": '{"linux":"failed"}'})
        self.assertEqual(result.returncode, 42, result.stdout + result.stderr)
        self.assertEqual(self.local_runs(), [])
        attempt = self.only_attempt()
        self.assertEqual(self.ssh_events()[0]["result_type"], "FailedOnTarget")
        self.assertEqual((attempt["outcome"], attempt["completion"], attempt["cleanup"]),
                         ("failed", "confirmed", "confirmed"))
