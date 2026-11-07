"""Check retained SSH slots and claims with private verification processes."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest import mock

from scripts.build_targets import test_ssh_eligibility as eligibility_support
from scripts.build_queue import admission_protocol as wire
from scripts.build_queue.admission_service import Connection
from scripts.direct import verify_ssh_targets_test_support as support
from scripts.direct.verify_ssh_targets_test_support import VerifySshTargetsFixture
from scripts.lint.memory_test_support import write_config_fixture
from scripts.lib.process_start_test_support import process_reader

from scripts.lib import process_start as process_identity


mac_test = eligibility_support.mac_test


class VerifySshRetainedTests(VerifySshTargetsFixture):
    def test_verify_exit_before_first_memory_hook_fails(self) -> None:
        self.release_memory_after_hook()
        with subprocess.Popen(["true"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process, \
             mock.patch.object(self, "start_verify", return_value=process), \
             self.assertRaisesRegex(self.failureException, "verify exited"):
            _ = self.verify("test", "sample", "--no-cache")

    def test_verify_drains_output_after_memory_hook_settles(self) -> None:
        self.release_memory_after_hook()
        command = '''
printf '%s\\n' '{"left": false, "clock": "", "attempts": 0}' > memory-hook-1
head -c 8 memory-hook-ack > /dev/null
exec "$1" -c 'import sys; sys.stdout.write("x" * (128 * 1024))'
'''
        with subprocess.Popen(["sh", "-c", command, "memory-hook-output", sys.executable],
                              cwd=self.runner_state, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process, \
             mock.patch.object(self, "start_verify", return_value=process):
            result = self.verify("test", "sample", "--no-cache")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "x" * (128 * 1024))

    def test_verify_returns_when_settled_hook_skips_acknowledgment(self) -> None:
        self.release_memory_after_hook()
        command = '''
printf '%s\\n' '{"left": true, "clock": "", "attempts": 0}' > memory-hook-1
exit 0
'''
        with subprocess.Popen(["sh", "-c", command], cwd=self.runner_state, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process, \
             mock.patch.object(self, "start_verify", return_value=process):
            result = self.verify("test", "sample", "--no-cache")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def retain_slots(self, capacity: int = 2) -> list[Path]:
        _ = write_config_fixture(self.root / "aws_target.conf",
                                 (self.root / "aws_target.conf").read_text()
                                 + f"slots={capacity}\nslots.hana={capacity}\n",
                                 template="aws_target")
        paths: list[Path] = []
        with mock.patch.dict(os.environ, self.environment), \
             mock.patch.object(Path, "home", return_value=self.home), \
             mock.patch.object(process_identity, "process_start", side_effect=process_reader(eligibility_support.SshEligibilityTests().process_start)), \
             mock.patch.object(mac_test, "target_claim_scope", return_value=mac_test.RepositoryScope(
                 "hana", "https://example.com/team/hana")):
            state = mac_test.state_paths("aws")
            for pid in range(2147483000, 2147483000 + capacity):
                args = mac_test.CliArguments()
                args.target, args.worktree, args.pid = "aws", str(self.root), pid
                args.what, args.remote = "test hana", "fake-aws"
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(mac_test.command_claim(args, state, watch_claim=False), 0)
            paths = [claim.path for claim in mac_test.read_claims(state)]
        self.assertEqual(len(paths), capacity)
        return paths

    def test_queue_retry_cleans_retained_slots_after_their_owners_exit(self) -> None:
        self.configure("natedev,aws")
        claims = self.retain_slots()
        self.release_memory_after_hook()
        result = self.verify("test", "sample", "--no-cache")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        event, = self.ssh_events()
        self.assertIn("cleanup", event["actions"])
        self.assertIn("claim:13", event["actions"])
        self.assertLess(event["actions"].index("cleanup"), event["actions"].index("run"))
        self.assertEqual(event["result_type"], "PassedOnTarget")
        self.assertEqual(self.local_runs(), [])
        self.assertLess(sum(path.exists() for path in claims), len(claims))

    def test_retained_slots_stay_owned_when_retry_cannot_confirm_cleanup(self) -> None:
        self.configure("natedev,aws,spare")
        claims = self.retain_slots()
        _ = self.hold_memory()
        pressure = Path(self.environment["BUILDLOG_MEMORY_PRESSURE"])
        _ = pressure.write_text("some avg10=100 avg60=0 avg300=0 total=0\n"
                                + "full avg10=100 avg60=0 avg300=0 total=0\n")
        before_attempt = r'''
steps_path = root / "memory-steps.json"
steps = json.loads(steps_path.read_text()) if steps_path.exists() else []
if request.target == "aws":
    assert steps == [], steps
    steps.append("aws_attempt")
else:
    assert request.target == "spare", request.target
    assert steps == ["aws_attempt", "aws_cleanup_failed"], steps
    prior = json.loads((root / "ssh-events.jsonl").read_text().splitlines()[-1])
    assert prior["target"] == "aws" and "cleanup" in prior["actions"], prior
    assert prior["holder"] == "LostRunLeftovers", prior
    steps.append("spare_retry")
steps_path.write_text(json.dumps(steps))
'''
        after_attempt = r'''
if request.target == "aws":
    assert isinstance(result, offload.BusyMacDecline), result
    assert isinstance(result.holder, offload.LostRunLeftovers), result
    assert "cleanup" in actions and "run" not in actions, actions
    steps.append("aws_cleanup_failed")
else:
    assert isinstance(result, offload.PassedOnTarget), result
    steps.append("spare_completed")
    Path(os.environ["BUILDLOG_MEMINFO"]).write_text("MemAvailable: 268435456 kB\n")
    Path(os.environ["BUILDLOG_MEMORY_PRESSURE"]).write_text(
        "some avg10=0 avg60=0 avg300=0 total=0\nfull avg10=0 avg60=0 avg300=0 total=0\n")
steps_path.write_text(json.dumps(steps))
'''
        self.write_executable(self.runner, support.SSH_RUNNER.replace(
            "started = time.monotonic()", before_attempt + "\nstarted = time.monotonic()").replace(
            "offload.persist_attempt(request, result,", after_attempt + "\noffload.persist_attempt(request, result,"))
        result = self.verify("test", "sample", "--no-cache", extra={
            "TEST_SSH_MODES": '{"aws":"retained-leftovers"}', "BUILDLOG_MEM_WAIT_SINCE": "100.25"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        first, second = self.ssh_events()
        self.assertEqual((first["target"], second["target"]), ("aws", "spare"))
        self.assertEqual(first["result_type"], "BusyMacDecline")
        self.assertEqual(first["holder"], "LostRunLeftovers")
        self.assertIn("cleanup", first["actions"])
        self.assertNotIn("run", first["actions"])
        self.assertNotIn("release", first["actions"])
        self.assertTrue(all(path.exists() for path in claims))
        self.assertEqual(first["queued_at"], second["queued_at"])
        self.assertFalse((self.target_state("aws") / "unreachable_until").exists())
        self.assertEqual(json.loads((self.runner_state / "memory-steps.json").read_text()),
                         ["aws_attempt", "aws_cleanup_failed", "spare_retry", "spare_completed"])

    def test_mac_memory_retry_cleans_a_retained_claim_after_its_owner_exits(self) -> None:
        self.configure("natedev,mac")
        self.enable_mac()
        with mock.patch.dict(os.environ, self.environment), \
             mock.patch.object(process_identity, "process_start", return_value=process_identity.Started("old-owner")):
            paths = mac_test.state_paths("mac")
            args = mac_test.CliArguments()
            args.target, args.worktree, args.pid = "mac", str(self.root), 2147483000
            args.what, args.remote = "test sample", "test-mac"
            with redirect_stdout(io.StringIO()):
                self.assertEqual(mac_test.command_claim(args, paths, watch_claim=False), 0)
        self.release_memory_after_hook()
        result = self.verify("test", "sample", "--no-cache", extra={
            "VERIFY_MAC_TEST_SCRIPT": str(support.MAC_TEST_SCRIPT)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        event, = self.ssh_events()
        self.assertIn("claim:13", event["actions"])
        self.assertLess(event["actions"].index("cleanup"), event["actions"].index("run"))
        self.assertEqual(self.local_runs(), [])

    def test_mac_memory_retry_recovers_its_claim_without_a_reservation_file(self) -> None:
        self.configure("natedev,mac")
        self.enable_mac()
        claims_before_recovery: list[tuple[Path, mac_test.RunRecord]] = []

        def observe_recovery(event: support.CompletedMemoryHook) -> bool:
            with mock.patch.dict(os.environ, self.environment):
                claims = mac_test.read_claims(mac_test.state_paths("mac"))
            self.assertEqual(len(claims), 1)
            self.assertEqual(event["attempts"], 0)
            self.assertEqual(self.local_runs(), [])
            if not claims_before_recovery:
                self.assertFalse(event["left"])
                claims_before_recovery.extend((claim.path, claim.record) for claim in claims)
                meminfo = Path(self.environment["BUILDLOG_MEMINFO"])
                self.assertEqual(meminfo.read_text(), "MemAvailable: 0 kB\n")
                # The hook blocks on its acknowledgment while the service sends more held replies.
                with self.admission.loop_turn():
                    waiting, = self.admission.server.waiters.values()
                    reply = waiting.last_reply
                    self.assertIsInstance(reply, wire.Wait)
                    assert isinstance(reply, wire.Wait)
                    connection = waiting.connection
                    assert isinstance(connection, Connection)
                    self.admission.server.send_wait(connection, reply)
                    self.admission.server.send_wait(connection, reply)
                    self.admission.server.send_wait(connection, reply)
                self.assertEqual(meminfo.read_text(), "MemAvailable: 0 kB\n")
                return False
            self.assertTrue(event["left"])
            self.assertEqual([(claim.path, claim.record) for claim in claims], claims_before_recovery)
            return True

        self.release_memory_after_hook(observe_recovery)
        wrapper = self.root / "interrupted-reservation.py"
        self.write_executable(wrapper, r'''
import os
from pathlib import Path
import runpy
import sys
source = Path(os.environ["TEST_SOURCE_REPO"])
arguments = sys.argv[1:]
''' + support.RUN_BUILD_TARGETS + r'''
marker = Path(os.environ["TEST_RUNNER_STATE"]) / "reservation-interrupted"
if arguments[0] == "reserve" and status == 0 and not marker.exists():
    destination = Path(arguments[arguments.index("--reservation-file") + 1])
    destination.unlink()
    marker.touch()
    raise SystemExit(1)
raise SystemExit(status)
''')
        result = self.verify("test", "sample", "--no-cache", extra={
            "BUILD_TARGETS_SCRIPT": str(wrapper),
            "VERIFY_MAC_TEST_SCRIPT": str(support.MAC_TEST_SCRIPT)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.runner_state / "reservation-interrupted").exists())
        event, = self.ssh_events()
        self.assertTrue(event["reserved"])
        self.assertTrue(event["same_path"])
        self.assertIn("claim:0", event["actions"])
        self.assertEqual(self.local_runs(), [])
