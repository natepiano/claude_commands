"""Check SSH routing with private verification processes."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import TypedDict, cast
from unittest import mock

from scripts.direct import verify_ssh_targets_test_support as support
from scripts.direct.verify_ssh_targets_test_support import REPOSITORY, VerifySshTargetsFixture


class MacWaitRegistration(TypedDict):
    markers: list[bool]
    memory_hook: bool


class VerifySshRoutingTests(VerifySshTargetsFixture):
    def test_failed_mac_reservation_keeps_wait_registration_visible_to_the_idle_reader(self) -> None:
        self.configure("mac,natedev")
        self.enable_mac()
        self.release_memory_after_hook()
        wrapper = self.root / "reserve-refusal.py"
        self.write_executable(wrapper, r'''
import json
import os
from pathlib import Path
import runpy
import sys
from unittest import mock

source = Path(os.environ["TEST_SOURCE_REPO"])
sys.path.insert(0, str(source))
sys.path.insert(0, str(source / "scripts/buildlog"))
from scripts.buildlog import mac_idle
from scripts.direct import step_record

if sys.argv[1] == "reserve":
    row = json.loads(step_record.record_path(os.environ["BUILDLOG_CALL_ID"]).read_text())
    process = mac_idle.LiveProcess(row["pid"], 1, row["proc_start"], ("verify.sh",), {}, row["repo"])
    with mock.patch.object(step_record, "process_start", return_value=process.start):
        calls = mac_idle.waiting_calls(mac_idle.watch_paths(), {process.pid: process})
    snapshot = {"markers": [call.marker_hook for call in calls],
                "memory_hook": bool(os.environ.get("LINT_QUEUE_LEAVE_HOOK"))}
    with (Path(os.environ["TEST_RUNNER_STATE"]) / "idle-registrations.jsonl").open("a") as output:
        output.write(json.dumps(snapshot) + "\n")
    raise SystemExit(1)
''' + support.RUN_BUILD_TARGETS + "\nraise SystemExit(status)\n")
        result = self.verify("test", "sample", "--no-cache", extra={"BUILD_TARGETS_SCRIPT": str(wrapper)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        snapshots = [cast(MacWaitRegistration, json.loads(line))
                     for line in (self.runner_state / "idle-registrations.jsonl").read_text().splitlines()]
        self.assertTrue(snapshots)
        self.assertTrue(any(snapshot["memory_hook"] for snapshot in snapshots))
        self.assertTrue(all(snapshot["markers"] == [True] for snapshot in snapshots), snapshots)
        self.assertEqual(self.ssh_events(), [])
        self.assertEqual(len(self.local_runs()), 1)

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
             mock.patch.object(os, "getcwd", return_value=str(self.root)), \
             mock.patch.object(sys, "path", [str(REPOSITORY / "scripts/buildlog"), *sys.path]):
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
            ("version-mismatch", "aws version mismatch: nextest box=nextest other natedev=nextest fixture; "
             + "move the box pins in nixos hosts/aws-build.nix", "nextest box=nextest other"),
            ("runtime-version-mismatch", "aws version mismatch: rustc box=rustc other natedev=rustc fixture; "
             + "nextest box=nextest other natedev=nextest fixture; move the box pins in nixos hosts/aws-build.nix",
             "nextest box=nextest other"),
            ("probe-leftovers", "busy", "2 leftover processes"),
        ):
            with self.subTest(mode=mode):
                (self.home / ".local/state/build-targets/aws/unreachable_until").unlink(missing_ok=True)
                result = self.verify("test", "sample", "--target", "aws",
                                     extra={"TEST_SSH_MODES": json.dumps({"aws": mode})})
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("target aws refused: " + reason, result.stderr)
                self.assertIn(detail, result.stdout + result.stderr)
                self.assertEqual(self.local_runs(), [])
                self.assertEqual(self.admission.history(), [])
                self.assertEqual(self.attempts()[-1]["reason"], reason)

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
import sys
sys.path.insert(0, str(Path.home() / ".claude"))
from scripts.direct.test_verify_step_state import read_step_row
row = read_step_row(records[0].stem)
Path(os.environ["TEST_RUNNER_STATE"], "waiting-step.json").write_text(json.dumps(row))
Path(os.environ["HOME"], ".local/state/build-targets/aws/controller.json").write_text('{"state":"running"}')
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
