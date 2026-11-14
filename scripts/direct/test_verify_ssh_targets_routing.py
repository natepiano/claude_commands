"""Check SSH routing with private verification processes."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
from typing import TypedDict, cast
from unittest import mock

from scripts.build_hold.owning_session import OwningSession
from scripts.direct import verify_ssh_targets_test_support as support
from scripts.direct.verify_ssh_targets_test_support import VerifySshTargetsFixture
from scripts.mac_test import claim_state, mac_test


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
with open(source / "scripts/lib/package_root.py") as package_root: exec(package_root.read(), {"__file__": package_root.name})
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
        self.configure("natedev,mac,linux")
        self.enable_mac()
        # Keep local admission before the retry that can reserve the Mac.
        self.control_admission_clock()
        self.write_executable(self.bin_directory / "ssh", '''#!/bin/sh
case "$*" in *fake-linux*) exit 255 ;; esac
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
        self.assertEqual(event["target"], "linux")
        self.assertTrue(event["reserved"])
        self.assertTrue(event["same_path"])
        self.assertGreater(event["owner"], 0)
        self.assertEqual(event["slot"], 1)
        self.assertEqual(event["result_type"], "PassedOnTarget")
        self.assertEqual(event["memory_reservation"], "")
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(self.routing_record()["target"], "linux")

    def test_linux_slot_takes_a_queued_check_without_a_local_hold(self) -> None:
        _ = self.hold_memory()
        result = self.verify("check", "sample", "--no-cache")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        event, = self.ssh_events()
        self.assertEqual(event["target"], "linux")
        self.assertTrue(event["reserved"])
        self.assertTrue(event["same_path"])
        self.assertEqual(event["memory_reservation"], "")
        self.assertEqual(event["actions"][:3], ["claim:0", "scripts-copy", "source-copy"])
        self.assertLess(event["actions"].index("source-copy"), event["actions"].index("probe"))
        self.assertFalse(any(row["args"][:1] == ["check"] for row in self.cargo_events()))
        self.assertEqual(self.routing_record()["target"], "linux")

    def test_held_local_target_does_not_block_linux_check(self) -> None:
        _ = self.routing_fixture()
        result = self.verify("check", "sample", "--no-cache")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        event, = self.ssh_events()
        self.assertEqual(event["target"], "linux")
        self.assertIn("run", event["actions"])
        self.assertFalse(any(event["args"][:1] == ["check"] for event in self.cargo_events()))
        self.assertEqual(self.routing_record()["target"], "linux")

    def test_pinned_linux_test_skips_free_natedev_and_its_admission(self) -> None:
        self.configure("natedev,linux")
        result = self.verify("test", "sample", "--target", "linux")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        event, = self.ssh_events()
        self.assertEqual((event["target"], event["pinned"]), ("linux", "linux"))
        self.assertFalse(event["token_held"])
        self.assertEqual(event["memory_reservation"], "")
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(self.admission.history(), [])
        self.assertEqual(self.routing_record()["target"], "linux")
        self.assertTrue(self.attempts()[0]["pinned"])

    def test_pinned_remote_step_records_the_target_host_and_selection(self) -> None:
        from scripts.buildlog import record, store

        self.configure("natedev,linux")
        with mock.patch.dict(os.environ, {**self.environment, "BUILDLOG_TARGET": "linux",
                                         "BUILDLOG_PINNED_TARGET": "linux"}), \
             mock.patch.object(store, "host_name", return_value="linux-build"), \
             mock.patch.object(os, "getcwd", return_value=str(self.root)):
            record.step(["0", "100", "101", "0", "", "", "cargo", "nextest", "run"])
        rows = [cast(dict[str, object], json.loads(line))
                for path in (self.root / "buildlog/linux-build").glob("*.jsonl")
                for line in path.read_text().splitlines()]
        step, = [row for row in rows if row["kind"] == "step"]
        self.assertEqual((step["target"], step["host"], step["pinned"]), ("linux", "linux-build", True))

    def test_pinned_check_skips_the_initial_local_token(self) -> None:
        self.configure("natedev,linux")
        result = self.verify("check", "sample", "--target", "linux")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        event, = self.ssh_events()
        self.assertEqual(event["target"], "linux")
        self.assertFalse(event["token_held"])
        self.assertFalse(any(row["args"][:1] == ["check"] for row in self.cargo_events()))
        self.assertEqual(self.admission.history(), [])

    def test_pinned_target_refusal_keeps_the_reason_and_never_runs_locally(self) -> None:
        self.configure("natedev,linux")
        settings = self.root / "linux_target.conf"
        _ = settings.write_text(settings.read_text() + "version_pin_file=nixos hosts/linux-build.nix\n")
        for mode, reason, detail in (
            ("disk", "disk", "not running on the linux (disk)"),
            ("version-mismatch", "linux version mismatch: nextest box=nextest other natedev=nextest fixture; "
             + "move the box pins in nixos hosts/linux-build.nix", "nextest box=nextest other"),
            ("runtime-version-mismatch", "linux version mismatch: rustc box=rustc other natedev=rustc fixture; "
             + "nextest box=nextest other natedev=nextest fixture; move the box pins in nixos hosts/linux-build.nix",
             "nextest box=nextest other"),
            ("probe-leftovers", "busy", "2 leftover processes"),
        ):
            with self.subTest(mode=mode):
                (self.home / ".local/state/build-targets/linux/unreachable_until").unlink(missing_ok=True)
                result = self.verify("test", "sample", "--target", "linux",
                                     extra={"TEST_SSH_MODES": json.dumps({"linux": mode})})
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("target linux refused: " + reason, result.stderr)
                self.assertIn(detail, result.stdout + result.stderr)
                self.assertEqual(self.local_runs(), [])
                self.assertEqual(self.admission.history(), [])
                self.assertEqual(self.attempts()[-1]["reason"], reason)

    def test_pinned_toolchain_mismatch_records_a_repository_decline(self) -> None:
        from scripts.mac_test import eligibility, offload

        self.configure("natedev,linux")
        state = self.target_state("linux")
        _ = (state / "box-tools.json").write_text(json.dumps({"versions": {"rustc": "rustc other"}}))
        reason = ("linux version mismatch: rustc box=rustc other natedev=rustc fixture; "
                  "move the box pins in nixos hosts/linux-build.nix")
        with mock.patch.object(eligibility, "state_directory", return_value=state):
            path = eligibility.linux_version_decline_path("linux", self.root.name)
            self.assertFalse(path.exists())
            result = self.verify("test", "sample", "--target", "linux")
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("target linux refused: " + reason, result.stderr)
            self.assertIn("rustc box=rustc other natedev=rustc fixture", result.stdout + result.stderr)
            recorded_at = float(path.read_text())
            self.assertTrue(offload.linux_version_declined_recently("linux", self.root.name, 60, recorded_at + 1))
            self.assertFalse(offload.linux_version_declined_recently("linux", "other-repository", 60, recorded_at + 1))
        self.assertEqual(self.attempts()[-1]["reason"], reason)
        self.assertNotIn("probe", self.ssh_events()[-1]["actions"])
        self.assertNotIn("run", self.ssh_events()[-1]["actions"])
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(self.admission.history(), [])


    def test_pinned_test_keeps_its_live_record_until_linux_becomes_available(self) -> None:
        self.assert_pinned_wait_keeps_its_live_record("test")

    def test_pinned_check_keeps_its_live_record_until_linux_becomes_available(self) -> None:
        self.assert_pinned_wait_keeps_its_live_record("check")

    def assert_pinned_wait_keeps_its_live_record(self, kind: str) -> None:
        self.configure("natedev,linux")
        paths = claim_state.StatePaths(self.target_state("linux"), "linux",
                                       registry_selected=True, platform="linux")
        args = mac_test.CliArguments()
        args.target, args.worktree, args.pid = "linux", str(self.root), os.getpid()
        args.what = "private fixture work"
        owner = OwningSession("86c49103-6143-4766-a2d8-f8041f7a9174")
        with mock.patch.object(mac_test, "target_claim_scope", return_value=claim_state.WHOLE_MAC), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(mac_test.command_claim(args, paths, watch_claim=False, session=owner), 0)
        self.write_executable(self.bin_directory / "sleep", r'''
import json
import os
from pathlib import Path

with open(Path.home() / ".claude/scripts/lib/package_root.py") as package_root: exec(package_root.read(), {"__file__": package_root.name})
from scripts.direct.test_verify_step_state import read_step_row

records = list(Path(os.environ["VERIFY_STEPS_DIR"]).glob("*.json"))
assert len(records) == 1
row = read_step_row(records[0].stem)
Path(os.environ["TEST_RUNNER_STATE"], "waiting-step.json").write_text(json.dumps(row))
Path(os.environ["TEST_BUSY_CLAIM"]).unlink()
''')
        result = self.verify(kind, "sample", "--target", "linux", extra={"TEST_BUSY_CLAIM": str(paths.run)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("waiting for target linux", result.stderr)
        row = cast(dict[str, object], json.loads((self.runner_state / "waiting-step.json").read_text()))
        self.assertEqual((row["pinned_target"], row["state"]), ("linux", "waiting_ssh"))
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(self.admission.history(), [])
        self.assertEqual([event["target"] for event in self.ssh_events()], ["linux"])

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
