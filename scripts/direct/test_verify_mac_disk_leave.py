"""Keep a Mac retry available while disk pressure holds a local step."""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
import unittest
from typing import final
from unittest import mock

from scripts.direct import step_record
from scripts.build_queue import admission_protocol as wire, admission_records as records, placement
from scripts.build_queue.test_placement import (
    PlacementFixture, adopt_assignment, consume_permission, placement_request, publish_candidate,
)
from scripts.mac_test.claim_state import RepositoryScope, WholeMacScope
from scripts.direct.verify_mac_memory_retry_test_support import VerifyMacMemoryRetryFixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import write_config_fixture
from scripts.lint import memory_admit
from scripts.message.unit_labels import UnitLabels
from scripts.build_targets import build_targets, target_config


@final
class VerifyMacDiskLeaveTests(VerifyMacMemoryRetryFixture):
    def test_exit_eleven_retries_busy_at_the_normal_poll_interval(self) -> None:
        _ = self.hold_memory()
        wrapper = self.root / "busy-reserve.py"
        self.write_executable(wrapper, '''import os, runpy, sys
from pathlib import Path
marker = Path(os.environ["TEST_RUNNER_STATE"]) / "busy-reserve"
if sys.argv[1] == "reserve" and not marker.exists():
    marker.write_text("11\\n")
    raise SystemExit(11)
runpy.run_path(os.environ["TEST_REAL_TARGETS"], run_name="__main__")
''')
        process = self.start_verify("test", "sample", "--no-cache", extra={
            "BUILD_TARGETS_SCRIPT": str(wrapper),
            "TEST_REAL_TARGETS": str(build_targets.__file__),
            "TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0"})
        self.addCleanup(self.stop_verify, process)
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual((self.runner_state / "busy-reserve").read_text(), "11\n")
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(len((self.runner_state / "authorized-executions").read_text().splitlines()), 1)
        self.assertNotIn("180", errors)

    def test_disk_decline_during_eligibility_leaves_memory_wait_for_a_free_mac(self) -> None:
        self.assert_disk_decline_leaves_for_mac()

    def test_mac_disk_with_linux_off_leaves_memory_wait_for_a_free_mac(self) -> None:
        config = self.root / "build_targets.conf"
        _ = config.write_text(config.read_text().replace("targets=mac,natedev", "targets=mac,natedev,linux")
                              + "kind.linux=ssh\nplatform.linux=linux\nsettings.linux=linux.conf\n")
        _ = write_config_fixture(self.root / "linux.conf", "offload=off\n", template="aws_target")
        self.assert_disk_decline_leaves_for_mac(disabled_linux=True)

    def assert_disk_decline_leaves_for_mac(self, *, disabled_linux: bool = False) -> None:
        _ = self.hold_memory()
        available = self.runner_state / "available-gib"
        _ = available.write_text("0\n")
        self.write_executable(self.bin_directory / "ssh", f'''#!/bin/sh
printf 'free_gib=%s\\nhost=test-mac\\nrc=0\\n' "$(cat '{available}')"
''')
        process = self.start_verify("test", "sample", "--no-cache", extra={
            "TEST_RUNNER_BUSY_UNTIL": str(self.runner_state / "mac-free"),
            "TEST_RUNNER_BUSY_REASON": "disk",
            "TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0"})
        self.addCleanup(self.stop_verify, process)
        self.wait_for_memory_wait(process)
        paths = list((self.root / "verify-steps").glob("*.json"))
        self.assertEqual(len(paths), 1)
        with mock.patch.dict(os.environ, {"VERIFY_STEPS_DIR": str(self.root / "verify-steps")}):
            record = step_record.read(paths[0].stem)
        if not isinstance(record, step_record.StepRecord):
            self.fail(f"Expected a live step record, got {record!r}")
        self.assertEqual(record.runs_on, step_record.AnyTarget(
            ("mac", "natedev", "linux") if disabled_linux else ("mac", "natedev"), ("mac",)))
        self.assertEqual(len(list((self.root / "mac-idle/calls").glob("*.json"))), 0)
        self.assertEqual(len(self.runner_events()), 0)
        _ = available.write_text("1000\n")
        _ = (self.runner_state / "mac-free").write_text("free\n")
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(len(self.runner_events()), 1)
        self.assertEqual(len((self.runner_state / "authorized-executions").read_text().splitlines()), 1)
        self.assertEqual(self.routing_record()["target"], "mac")

    def test_runner_disk_decline_retries_as_soon_as_the_mac_is_free(self) -> None:
        _ = self.hold_memory()
        _ = self.control_routing_clock()
        process = self.start_verify("test", "sample", "--no-cache", extra={
            "TEST_RUNNER_BUSY_UNTIL": str(self.runner_state / "mac-free"),
            "TEST_RUNNER_BUSY_REASON": "disk",
            "TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0"})
        self.addCleanup(self.stop_verify, process)
        self.wait_for_memory_wait(process)
        self.assertEqual(len(self.runner_events()), 0)
        _ = (self.runner_state / "mac-free").write_text("free\n")
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(len(self.runner_events()), 1)
        self.assertNotIn("waiting 180 s before each retry", errors)
        self.assertEqual(self.routing_record()["target"], "mac")


class QueueDiskRecoveryTests(unittest.TestCase):
    def test_disk_refresh_preserves_the_episode_and_authorizes_only_the_mac(self) -> None:
        for disabled_linux in (False, True):
            with self.subTest(disabled_linux=disabled_linux), PlacementFixture() as fixture:
                _ = fixture.occupy_host()
                fixture.free(0)
                fixture.hold_starts()
                request = placement_request(fixture)
                _ = fixture.enter_context(mock.patch.object(memory_admit, "priority_labels", return_value={
                    request.identity.session_id: UnitLabels("fixture", unit="fixture")}))
                mac = request.constraints.candidates[0]
                local = placement.PlacementCandidate(placement.PlacementDestination(
                    "local", "natedev", fixture.server.host, WholeMacScope()))
                candidates = (mac, local)
                if disabled_linux:
                    linux = placement.PlacementCandidate(placement.PlacementDestination(
                        "ssh", "disabled-linux", "fixture-linux", RepositoryScope("hana", "key", 1), 1))
                    candidates += (linux,)
                    key = fixture.server.host_evidence_key(request, linux.destination)
                    evidence = placement.FreshHostEvidence(key, fixture.clock.now(), "linux-generation",
                                                          placement.TemporaryExclusion("off"))
                    fixture.provider_evidence[key] = evidence
                    fixture.server.publish_host_evidence(evidence)
                request = replace(request, constraints=replace(request.constraints, candidates=candidates))
                _ = publish_candidate(fixture, request, placement.TemporaryExclusion("disk"))
                fixture.server.priority = records.QueuePriority(({"unit": "fixture", "why": "disk recovery",
                                           "set_at": fixture.clock.now(), "set_by": "fixture-director"},))
                reply, = fixture.connect(request).replies()
                self.assertIsInstance(reply, wire.PlacementWait)
                self.assertEqual(set(fixture.server.placement_requests), {request.episode})
                self.assertEqual(fixture.caller_reservations(), {})
                original_place = dict(fixture.server.run_places)
                self.assertTrue(original_place)
                original_priority = fixture.server.priority
                remote_commands = fixture.root / "remote-commands"
                for reason in ("disk", "busy", "unreachable", "blocked", "spending"):
                    _ = publish_candidate(fixture, request, placement.TemporaryExclusion(reason))
                    reply, = fixture.connect(request).replies()
                    self.assertIsInstance(reply, wire.PlacementWait)
                    assert isinstance(reply, wire.PlacementWait)
                    self.assertEqual((reply.episode, reply.since), (request.episode, request.since))
                    self.assertEqual(fixture.caller_reservations(), {})
                _ = publish_candidate(fixture, request)
                _ = fixture.run_pass()
                reply, = fixture.connect(request).replies()
                self.assertIsInstance(reply, wire.Assignment)
                if not isinstance(reply, wire.Assignment):
                    self.fail(f"Refreshed Mac evidence did not produce an assignment: {reply!r}")
                assignment = reply.assignment
                self.assertEqual(assignment.destination, mac.destination)
                self.assertEqual((assignment.episode, assignment.since), (request.episode, request.since))
                self.assertEqual(fixture.server.run_places, original_place)
                self.assertEqual(fixture.server.priority, original_priority)
                self.assertEqual(fixture.server.placement_requests[request.episode], request)
                claim = adopt_assignment(fixture, request, assignment)
                consume = consume_permission("disk-ready", assignment, claim)
                authorization, = fixture.connect(consume).replies()
                self.assertIsInstance(authorization, wire.FirstAuthorization)
                if isinstance(authorization, wire.FirstAuthorization):
                    result = subprocess.run([sys.executable, "-c",
                        "from pathlib import Path; import sys; "
                        + "Path(sys.argv[1]).open('a').write('mac\\n')", str(remote_commands)],
                        capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                duplicate, = fixture.connect(replace(consume, request_id="disk-ready-repeated")).replies()
                self.assertIsInstance(duplicate, wire.DispatchStatus)
                self.assertEqual(remote_commands.read_text().splitlines(), ["mac"])
                self.assertEqual(fixture.caller_reservations(), {})


class StepRetryPermissionTests(unittest.TestCase):
    def test_retry_permission_uses_all_target_rows_in_every_order(self) -> None:
        cases = (
            (("mac", "disk"),),
            (("linux", "disk"),),
            (("mac", "disk"), ("linux", "off")),
            (("mac", "off"), ("linux", "disk")),
            (("mac", "off"), ("linux", "repo")),
        )
        contexts: tuple[tuple[str, build_targets.StepKind, str], ...] = (
            ("0", "test", "0"), ("1", "test", "0"), ("0", "build", "0"), ("0", "final", "0"),
            ("0", "lint", "1"), ("0", "test", "1"))
        for rows in cases:
            for ordered in (rows, tuple(reversed(rows))):
                for local_only, kind, writes_tree in contexts:
                    with self.subTest(rows=ordered, local_only=local_only, kind=kind, writes_tree=writes_tree), \
                            PlacementFixture() as fixture:
                        fixture.free(0)
                        fixture.hold_starts()
                        repository = fixture.root / "hana"
                        _ = subprocess.run(["git", "init", "-q", str(repository)], check=True,
                            capture_output=True, timeout=TEST_HANG_GUARD_SECS)
                        configuration = "targets=" + ",".join(name for name, _ in ordered) + ",natedev\n"
                        for name in dict(ordered):
                            configuration += f"kind.{name}=ssh\nplatform.{name}={'macos' if name == 'mac' else 'linux'}\n"
                            configuration += f"settings.{name}={name}.conf\n"
                            _ = write_config_fixture(fixture.root / f"{name}.conf",
                                "offload=on\nlinux_only.hana=\nslots=1\nkinds=test,check,lint,doc,build,example\n",
                                template="mac_test")
                        configuration += "kind.natedev=local\nplatform.natedev=linux\n"
                        _ = (fixture.root / "build_targets.conf").write_text(configuration)
                        request = placement_request(fixture)
                        constraints = build_targets.fixed_placement_constraints(repository, "hana", kind,
                            ("cargo", kind), request.constraints.session, "fixed-source",
                            routing=target_config.LocalRequired("requires local files") if local_only == "1"
                                    else target_config.ANY_ELIGIBLE_TARGET,
                            writes_tree=writes_tree == "1")
                        request = replace(request, constraints=constraints)
                        reasons = dict(ordered)
                        for candidate in constraints.candidates:
                            if candidate.destination.provider == "local":
                                continue
                            key = fixture.server.host_evidence_key(request, candidate.destination)
                            evidence = placement.FreshHostEvidence(key, fixture.clock.now(), "",
                                placement.TemporaryExclusion(reasons[candidate.destination.target]))
                            fixture.provider_evidence[key] = evidence
                            fixture.server.publish_host_evidence(evidence)
                        self.assertIsInstance(fixture.connect(request).replies()[-1], wire.PlacementWait)
                        for candidate in constraints.candidates:
                            if reasons.get(candidate.destination.target) != "disk":
                                continue
                            key = fixture.server.host_evidence_key(request, candidate.destination)
                            evidence = placement.FreshHostEvidence(key, fixture.clock.now(), "", placement.AllowedHost())
                            fixture.provider_evidence[key] = evidence
                            fixture.server.publish_host_evidence(evidence)
                        reply, = fixture.connect(request).replies()
                        expected = (local_only == "0" and kind == "test" and writes_tree == "0"
                                    and any(reason == "disk" for _, reason in rows))
                        self.assertEqual(isinstance(reply, wire.Assignment), expected, repr(reply))


if __name__ == "__main__":
    _ = unittest.main()
