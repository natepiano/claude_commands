"""Check SSH retry deadlines and candidates with private verification processes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from scripts.direct import verify_ssh_targets_test_support as support
from scripts.direct.verify_ssh_targets_test_support import VerifySshTargetsFixture
from scripts.lint.memory_test_support import write_config_fixture
from scripts.build_queue.admission_test_support import caller_provider_controls


class VerifySshRetryTests(VerifySshTargetsFixture):
    def test_mac_memory_retry_waits_until_its_retry_deadline(self) -> None:
        self.configure("mac,natedev")
        self.enable_mac()
        clock = self.control_routing_clock()

        def observe_retry(event: support.CompletedMemoryHook) -> bool:
            if event["clock"] == "100":
                self.assertFalse(event["left"])
                self.assertEqual(event["attempts"], 1, json.dumps(self.ssh_events())
                                 + repr(self.admission.server.assignments))
                self.assertEqual(len(self.ssh_events()), 1)
                self.assertEqual(clock.read_text(), "100")
                polls = (self.runner_state / "memory-polls.jsonl").read_text().splitlines()
                if len(polls) >= 2:
                    _ = clock.write_text("281")
                    caller_provider_controls(self, self.admission).advance(181)
                return False
            self.assertTrue(event["left"])
            self.assertEqual(event["clock"], "281")
            return True

        self.release_memory_after_hook(observe_retry, after_attempts=1)
        result = self.verify("test", "sample", "--no-cache", extra={"TEST_SSH_MODES": '{"mac":"lost-once"}'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        first, second = self.ssh_events()
        self.assertEqual((first["clock"], second["clock"]), ("100", "281"))
        polls = [cast(dict[str, int], json.loads(line))
                 for line in (self.runner_state / "memory-polls.jsonl").read_text().splitlines()]
        self.assertEqual([row["attempts"] for row in polls[:2]], [1, 1])
        self.assertEqual(self.local_runs(), [])

    def test_named_mac_retry_reserves_its_target_while_the_default_mac_has_backoff(self) -> None:
        self.configure("natedev,mac,mac2")
        self.enable_mac()
        registry = self.root / "build_targets.conf"
        _ = registry.write_text(registry.read_text()
                                + "kind.mac2=ssh\nplatform.mac2=macos\nsettings.mac2=mac2.conf\n")
        _ = write_config_fixture(self.root / "mac2.conf", (self.root / "mac_test.conf").read_text(),
                                 template="mac_test")
        state = Path(self.environment["MAC_TEST_STATE_DIR"])
        state.mkdir(parents=True, exist_ok=True)
        _ = (state / "unreachable_until").write_text(str(10**12))
        self.release_memory_after_hook()
        result = self.verify("test", "sample", "--no-cache", extra={
            "VERIFY_MAC_TEST_SCRIPT": str(support.MAC_TEST_SCRIPT)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.ssh_events(), result.stdout + result.stderr)
        event, = self.ssh_events()
        self.assertEqual(event["target"], "mac2")
        self.assertTrue(event["reserved"])
        self.assertEqual(self.local_runs(), [])

    def test_permanent_mac_refusal_leaves_no_memory_retry_candidate(self) -> None:
        self.configure("mac,natedev")
        self.enable_mac()
        self.release_memory_after_hook()
        result = self.verify("test", "sample", "--no-cache", extra={"TEST_SSH_MODES": '{"mac":"no-tests"}'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.ssh_events()), 1)
        self.assertEqual(len(self.local_runs()), 1)
        attempt, = self.attempts()
        self.assertEqual((attempt["outcome"], attempt["completion"]), ("no_tests", "confirmed"))

    def test_permanent_mac_refusal_keeps_other_ssh_retry_candidates(self) -> None:
        self.configure("mac,natedev,aws")
        self.enable_mac()
        self.release_memory_after_hook()
        result = self.verify("test", "sample", "--no-cache", extra={"TEST_SSH_MODES": '{"mac":"no-tests"}'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([event["target"] for event in self.ssh_events()], ["mac", "aws"])
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(self.routing_record()["target"], "aws")
