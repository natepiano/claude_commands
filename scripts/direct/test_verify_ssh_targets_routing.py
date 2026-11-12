"""Check SSH routing with private verification processes."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast
from unittest import mock

from scripts.direct import verify_ssh_targets_test_support as support
from scripts.build_queue import admission_protocol as wire, admission_service as service, placement
from scripts.direct.verify_ssh_targets_test_support import VerifySshTargetsFixture


class VerifySshRoutingTests(VerifySshTargetsFixture):
    def test_failed_mac_reservation_keeps_its_episode_for_local_fallback(self) -> None:
        self.configure("mac,natedev")
        self.enable_mac()
        _ = self.hold_memory()
        wrapper = self.root / "reserve-refusal.py"
        self.write_executable(wrapper, r'''
import json
import os
from pathlib import Path
import runpy
import sys

source = Path(os.environ["TEST_SOURCE_REPO"])
with open(source / "scripts/lib/package_root.py") as package_root:
    exec(package_root.read(), {"__file__": package_root.name})
from scripts.build_queue import admission_protocol as wire

if sys.argv[1] == "reserve":
    assigned = wire.parse_reply(Path(sys.argv[sys.argv.index("--assignment-file") + 1]).read_text())
    assert isinstance(assigned, wire.Assignment)
    assignment = assigned.assignment
    with (Path(os.environ["TEST_RUNNER_STATE"]) / "refused-assignments.jsonl").open("a") as output:
        output.write(json.dumps({"episode": assignment.episode, "since": assignment.since}) + "\n")
    Path(os.environ["BUILDLOG_MEMINFO"]).write_text("MemAvailable: 67108864 kB\n")
    raise SystemExit(1)
''' + support.RUN_BUILD_TARGETS + "\nraise SystemExit(status)\n")
        result = self.verify("test", "sample", "--no-cache", extra={"BUILD_TARGETS_SCRIPT": str(wrapper)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        snapshots = [cast(dict[str, object], json.loads(line))
                     for line in (self.runner_state / "refused-assignments.jsonl").read_text().splitlines()]
        self.assertTrue(snapshots)
        with self.admission.loop_turn():
            local = [saved.assignment for saved in self.admission.server.assignments.values()
                     if saved.assignment.destination.provider == "local"]
        self.assertTrue(local)
        self.assertTrue(all((snapshot["episode"], snapshot["since"]) == (local[0].episode, local[0].since)
                            for snapshot in snapshots), snapshots)
        self.assertEqual(self.ssh_events(), [])
        self.assertEqual(len(self.local_runs()), 1)

        self.clear_observations()
        history = self.admission.history()
        pinned = self.verify("test", "sample", "--no-cache", "--target", "mac",
                             extra={"BUILD_TARGETS_SCRIPT": str(wrapper)})
        self.assertNotEqual(pinned.returncode, 0, pinned.stdout + pinned.stderr)
        self.assertIn("target mac refused: state", pinned.stderr)
        self.assertEqual(self.ssh_events(), [])
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(self.admission.history(), history)

    def test_saved_reservation_survives_a_lost_reserve_response(self) -> None:
        self.configure("mac,natedev")
        self.enable_mac()
        wrapper = self.root / "reserve-response-loss.py"
        self.write_executable(wrapper, r'''
from contextlib import ExitStack
import json
import os
from pathlib import Path
import runpy
import sys
import time
from unittest import mock
source = Path(os.environ["TEST_SOURCE_REPO"])
with open(source / "scripts/lib/package_root.py") as package_root:
    exec(package_root.read(), {"__file__": package_root.name})
from scripts.lib import process_start
root = Path(os.environ["TEST_RUNNER_STATE"])
recovering = sys.argv[1] == "recover-reservation"
observations = root / "recovery-observations.jsonl"
with ExitStack() as patches:
    observations_seen = len(observations.read_text().splitlines()) if observations.exists() else 0
    if recovering and observations_seen < int(os.environ.get("TEST_RECOVERY_UNKNOWN_READS", "0")):
        owner = int(sys.argv[sys.argv.index("--pid") + 1])
        original_start = process_start.process_start
        def owner_start(pid):
            return process_start.Unreadable("the private owner reading is unavailable") if pid == owner else original_start(pid)
        patches.enter_context(mock.patch.object(process_start, "process_start", side_effect=owner_start))
''' + "\n".join("    " + line if line else line for line in support.RUN_BUILD_TARGETS.splitlines()) + r'''
if recovering:
    identity_file = Path(sys.argv[sys.argv.index("--claim-identity-file") + 1])
    with observations.open("a") as output:
        output.write(json.dumps({"status": status, "identity": identity_file.read_text(),
                                 "at": time.monotonic()}) + "\n")
if sys.argv[1] == "reserve" and status == 0:
    marker = root / "saved-reserve.json"
    if not marker.exists():
        assignment_file = Path(sys.argv[sys.argv.index("--assignment-file") + 1])
        identity_file = Path(sys.argv[sys.argv.index("--claim-identity-file") + 1])
        marker.write_text(json.dumps({"assignment": assignment_file.read_text(),
                                     "identity": identity_file.read_text()}))
        raise SystemExit(1)
raise SystemExit(status)
''')
        for unknown_reads, poll_seconds in ((0, "0.05"), (1, "0.05"), (3, "0.25")):
            with self.subTest(unknown_reads=unknown_reads, poll_seconds=poll_seconds):
                self.clear_observations()
                for name in ("saved-reserve.json", "recovery-observations.jsonl", "ssh-events.jsonl"):
                    (self.runner_state / name).unlink(missing_ok=True)
                failures: list[wire.ClaimFailure] = []
                previous = self.admission.server.handle_request

                def observe(request: wire.Request,
                            connection: service.Connection | service.NoConnection = service.NO_CONNECTION) -> list[wire.Reply]:
                    if isinstance(request, wire.ClaimFailure):
                        failures.append(request)
                    return previous(request, connection)

                with mock.patch.object(self.admission.server, "handle_request", side_effect=observe):
                    result = self.verify("test", "sample", "--no-cache", extra={
                        "BUILD_TARGETS_SCRIPT": str(wrapper),
                        "TEST_RECOVERY_UNKNOWN_READS": str(unknown_reads),
                        "BUILD_TARGETS_POLL_S": poll_seconds})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(failures, [])
                recovery = [cast(dict[str, object], json.loads(line)) for line in
                            (self.runner_state / "recovery-observations.jsonl").read_text().splitlines()]
                self.assertEqual([row["status"] for row in recovery], [78] * unknown_reads + [0])
                if unknown_reads == 3:
                    for earlier, later in zip(recovery, recovery[1:]):
                        self.assertGreaterEqual(cast(float, later["at"]) - cast(float, earlier["at"]),
                                                float(poll_seconds), recovery)
                saved = cast(dict[str, str], json.loads((self.runner_state / "saved-reserve.json").read_text()))
                self.assertTrue(all(row["identity"] == saved["identity"] for row in recovery))
                assigned = wire.parse_reply(saved["assignment"])
                self.assertIsInstance(assigned, wire.Assignment)
                assert isinstance(assigned, wire.Assignment)
                identity = saved["identity"].split("\t")
                self.assertEqual(tuple(identity[1:3]), (assigned.assignment.episode, assigned.assignment.attempt))
                self.assertEqual(self.local_runs(), [])
                self.assertEqual(sum("run" in event["actions"] for event in self.ssh_events()), 1)
                with self.admission.loop_turn():
                    attempts = [attempt.assignment.attempt for attempt in self.admission.server.assignments.values()
                                if attempt.assignment.episode == assigned.assignment.episode]
                self.assertEqual(attempts, [assigned.assignment.attempt])

    def test_mac_backoff_table_runs_locally_without_a_remote_attempt(self) -> None:
        self.configure("mac,natedev")
        self.enable_mac()
        state = Path(self.environment["MAC_TEST_STATE_DIR"])
        state.mkdir(parents=True, exist_ok=True)
        _ = (state / "unreachable_until").write_text(str(10**12))
        result = self.verify("test", "sample", "--no-cache")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.local_runs()), 1)
        self.assertEqual(self.ssh_events(), [])
        self.assertEqual(self.routing_record()["target"], "natedev")

    def test_excluded_linux_target_does_not_record_a_mac_decline(self) -> None:
        self.configure("natedev,mac,aws")
        self.enable_mac()
        self.write_executable(self.bin_directory / "ssh", '''#!/bin/sh
case "$*" in *fake-aws*) exit 255 ;; esac
printf 'free_gib=1000\\nhost=test-mac\\nrc=0\\n'
''')
        result = self.verify("test", "sample", "--no-cache")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.ssh_events(), [])
        self.assertEqual(self.last_call()["mac"], "")
        self.assertEqual(self.last_call()["mac_reason"], "")
        self.assertEqual(len(self.local_runs()), 1)

    def test_free_linux_target_takes_a_queued_test_without_a_local_hold(self) -> None:
        _ = self.hold_memory()
        result = self.verify("test", "sample", "--no-cache")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        event, = self.ssh_events()
        self.assertEqual(event["target"], "aws")
        self.assertTrue(event["reserved"])
        self.assertTrue(event["same_path"])
        self.assertGreater(event["owner"], 0)
        self.assertEqual(event["slot"], 1)
        self.assertEqual(event["result_type"], "PassedOnTarget")
        self.assertEqual(event["memory_reservation"], "")
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(self.routing_record()["target"], "aws")

    def test_linux_slot_takes_a_queued_check_without_a_local_hold(self) -> None:
        _ = self.hold_memory()
        result = self.verify("check", "sample", "--no-cache")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        event, = self.ssh_events()
        self.assertEqual(event["target"], "aws")
        self.assertTrue(event["reserved"])
        self.assertTrue(event["same_path"])
        self.assertEqual(event["memory_reservation"], "")
        self.assertEqual(event["actions"][:3], ["claim:0", "scripts-copy", "source-copy"])
        self.assertLess(event["actions"].index("source-copy"), event["actions"].index("probe"))
        self.assertFalse(any(row["args"][:1] == ["check"] for row in self.cargo_events()))
        self.assertEqual(self.routing_record()["target"], "aws")

    def test_held_local_target_does_not_block_linux_check(self) -> None:
        _ = self.routing_fixture()
        result = self.verify("check", "sample", "--no-cache")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        event, = self.ssh_events()
        self.assertEqual(event["target"], "aws")
        self.assertIn("run", event["actions"])
        self.assertFalse(any(event["args"][:1] == ["check"] for event in self.cargo_events()))
        self.assertEqual(self.routing_record()["target"], "aws")

    def test_pinned_aws_test_skips_free_natedev_and_its_admission(self) -> None:
        self.configure("natedev,aws")
        result = self.verify("test", "sample", "--target", "aws")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        event, = self.ssh_events()
        self.assertEqual((event["target"], event["pinned"]), ("aws", "aws"))
        self.assertFalse(event["token_held"])
        self.assertEqual(event["memory_reservation"], "")
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(self.admission.history(), [])
        self.assertEqual(self.routing_record()["target"], "aws")
        self.assertTrue(self.attempts()[0]["pinned"])

    def test_pinned_remote_step_records_the_target_host_and_selection(self) -> None:
        from scripts.buildlog import record, store

        self.configure("natedev,aws")
        with mock.patch.dict(os.environ, {**self.environment, "BUILDLOG_TARGET": "aws",
                                         "BUILDLOG_PINNED_TARGET": "aws"}), \
             mock.patch.object(store, "host_name", return_value="aws-build"), \
             mock.patch.object(os, "getcwd", return_value=str(self.root)):
            record.step(["0", "100", "101", "0", "", "", "cargo", "nextest", "run"])
        rows = [cast(dict[str, object], json.loads(line))
                for path in (self.root / "buildlog/aws-build").glob("*.jsonl")
                for line in path.read_text().splitlines()]
        step, = [row for row in rows if row["kind"] == "step"]
        self.assertEqual((step["target"], step["host"], step["pinned"]), ("aws", "aws-build", True))

    def test_pinned_check_skips_the_initial_local_token(self) -> None:
        self.configure("natedev,aws")
        result = self.verify("check", "sample", "--target", "aws")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        event, = self.ssh_events()
        self.assertEqual(event["target"], "aws")
        self.assertFalse(event["token_held"])
        self.assertFalse(any(row["args"][:1] == ["check"] for row in self.cargo_events()))
        self.assertEqual(self.admission.history(), [])

    def test_pinned_target_refusal_keeps_the_reason_and_never_runs_locally(self) -> None:
        self.configure("natedev,aws")
        settings = self.root / "aws_target.conf"
        _ = settings.write_text(settings.read_text() + "version_pin_file=nixos hosts/aws-build.nix\n")
        for mode, reason, detail in (
            ("disk", "disk", "not running on the aws (disk)"),
            ("completed-decline", "state", "not running on the aws (state)"),
            ("version-mismatch", "aws version mismatch: nextest box=nextest other natedev=nextest fixture; "
             + "move the box pins in nixos hosts/aws-build.nix", "nextest box=nextest other"),
            ("runtime-version-mismatch", "aws version mismatch: rustc box=rustc other natedev=rustc fixture; "
             + "nextest box=nextest other natedev=nextest fixture; move the box pins in nixos hosts/aws-build.nix",
             "nextest box=nextest other"),
            ("probe-leftovers", "busy", "2 leftover processes"),
        ):
            with self.subTest(mode=mode):
                self.clear_observations()
                (self.home / ".local/state/build-targets/aws/unreachable_until").unlink(missing_ok=True)
                result = self.verify("test", "sample", "--target", "aws",
                                     extra={"TEST_SSH_MODES": json.dumps({"aws": mode})})
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("target aws refused: " + reason, result.stderr)
                self.assertIn(detail, result.stdout + result.stderr)
                self.assertEqual(self.local_runs(), [])
                self.assertEqual(self.admission.history(), [])
                self.assertEqual(self.attempts()[-1]["reason"], reason)

                if mode == "completed-decline":
                    with self.admission.loop_turn():
                        saved = [saved for saved in self.admission.server.assignments.values()
                                 if isinstance(saved.permission, placement.PermissionConsumed)]
                        self.assertTrue(saved)
                        for attempt in saved:
                            if isinstance(attempt.state, placement.CurrentAssignment):
                                submitted = self.admission.server.placement_requests[attempt.assignment.episode]
                                key = self.admission.server.host_evidence_key(submitted, attempt.assignment.destination)
                                self.admission.server.publish_host_evidence(self.admission.server.host_probe(key))
                    _ = self.admission.run_pass()
                    with self.admission.loop_turn():
                        self.assertTrue(all(isinstance(self.admission.server.assignments[placement.attempt_key(
                            attempt.assignment.episode, attempt.assignment.attempt)].state, placement.ClosedAssignment)
                            for attempt in saved), repr(saved) + result.stdout + result.stderr)

    def test_pinned_toolchain_mismatch_records_a_repository_decline(self) -> None:
        from scripts.mac_test import offload

        self.configure("natedev,aws")
        state = self.target_state("aws")
        _ = (state / "box-tools.json").write_text(json.dumps({"versions": {"rustc": "rustc other"}}))
        reason = ("aws version mismatch: rustc box=rustc other natedev=rustc fixture; "
                  "move the box pins in nixos hosts/aws-build.nix")
        with mock.patch.object(offload, "state_directory", return_value=state):
            path = offload.linux_version_decline_path("aws", self.root.name)
            self.assertFalse(path.exists())
            result = self.verify("test", "sample", "--target", "aws")
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("target aws refused: " + reason, result.stderr)
            self.assertIn("rustc box=rustc other natedev=rustc fixture", result.stdout + result.stderr)
            recorded_at = float(path.read_text())
            self.assertTrue(offload.linux_version_declined_recently("aws", self.root.name, 60, recorded_at + 1))
            self.assertFalse(offload.linux_version_declined_recently("aws", "other-repository", 60, recorded_at + 1))
        self.assertEqual(self.attempts()[-1]["reason"], reason)
        self.assertNotIn("probe", self.ssh_events()[-1]["actions"])
        self.assertNotIn("run", self.ssh_events()[-1]["actions"])
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(self.admission.history(), [])

    def test_pinned_wait_keeps_its_live_record_until_aws_becomes_available(self) -> None:
        self.assert_pinned_wait_keeps_its_live_record("test")


    def test_pinned_check_waits_until_aws_becomes_available(self) -> None:
        self.assert_pinned_wait_keeps_its_live_record("check")


    def assert_pinned_wait_keeps_its_live_record(self, kind: str) -> None:
        self.configure("natedev,aws")
        control = self.home / ".local/state/build-targets/aws/controller.json"
        _ = control.write_text('{"state":"stopped"}')
        self.write_executable(self.bin_directory / "sleep", r'''
import json
import os
from pathlib import Path
records = list(Path(os.environ["VERIFY_STEPS_DIR"]).glob("*.json"))
assert len(records) == 1
with open(Path.home() / ".claude/scripts/lib/package_root.py") as package_root: exec(package_root.read(), {"__file__": package_root.name})
from scripts.direct.test_verify_step_state import read_step_row
from scripts.lib.atomic_file import write_text_atomic
row = read_step_row(records[0].stem)
Path(os.environ["TEST_RUNNER_STATE"], "waiting-step.json").write_text(json.dumps(row))
write_text_atomic(Path(os.environ["HOME"], ".local/state/build-targets/aws/controller.json"), '{"state":"running"}')
''')
        result = self.verify(kind, "sample", "--target", "aws")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        row = cast(dict[str, object], json.loads((self.runner_state / "waiting-step.json").read_text()))
        self.assertEqual((row["pinned_target"], row["state"]), ("aws", "waiting_ssh"))
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(self.admission.history(), [])
        self.assertEqual([event["target"] for event in self.ssh_events()], ["aws"])

    def test_pinned_target_rejects_unknown_names_and_accepts_natedev(self) -> None:
        result = self.verify("test", "sample", "--target", "unknown")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown SSH target: unknown", result.stderr)
        self.assertEqual(self.ssh_events(), [])
        self.assertEqual(self.local_runs(), [])
        result = self.verify("test", "sample", "--target", "natedev")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.ssh_events(), [])
        self.assertEqual(len(self.local_runs()), 1)
