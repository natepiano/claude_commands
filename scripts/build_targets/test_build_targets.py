"""Check status lines and optional probes without contacting another host."""

from __future__ import annotations

import io
import json
import os
import sys
import subprocess
import time
from collections.abc import Generator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast, override
from unittest import mock
import unittest

import build_targets
import build_hold
import offload
import targets
from test_targets import END, NOW, SHIPPED_CONFIG, SINCE, IsolatedTargetTest

REAL_SUBPROCESS_RUN = subprocess.run
REAL_SUBPROCESS_POPEN = subprocess.Popen
REAL_PROCESS_START_TIME = build_hold.process_start_time
REAL_CALL_IS_LIVE = build_hold.call_is_live


@contextmanager
def pacific_zone() -> Generator[None, None, None]:
    try:
        with mock.patch.dict(os.environ, {"TZ": "America/Los_Angeles"}):
            time.tzset()
            yield
    finally:
        time.tzset()


class StatusTests(IsolatedTargetTest):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.enterContext(pacific_zone())
        _ = self.enterContext(mock.patch.object(targets, "utc_now", return_value=NOW))

    def capture_status(self, probe: bool = False) -> tuple[int, str, str]:
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = build_targets.command_status(probe)
        return code, output.getvalue(), errors.getvalue()

    def test_free_line(self) -> None:
        self.assertEqual(build_targets.format_state("natedev", targets.Free()), "natedev: free")

    def test_running_line_uses_local_zone(self) -> None:
        since = datetime(2026, 10, 11, 20, 20, tzinfo=timezone.utc)
        self.assertEqual(
            build_targets.format_state("mac", targets.Running("nextest hana", since)),
            "mac: running nextest hana since 13:20 PDT",
        )

    def test_expiring_hold_line(self) -> None:
        self.assertEqual(
            build_targets.format_state("mac", targets.Held(
                "hana", "render checks", SINCE, targets.ExpiresAt(END),
            )),
            "mac: held by hana since 13:05 PDT until 15:05 PDT — render checks",
        )

    def test_expected_release_line(self) -> None:
        self.assertEqual(
            build_targets.format_state("natedev", targets.Held(
                "hana", "render checks", SINCE, targets.ReleaseEstimated(END),
            )),
            "natedev: held by hana since 13:05 PDT release expected 15:05 PDT — render checks",
        )

    def test_hold_without_end_line(self) -> None:
        self.assertEqual(
            build_targets.format_state("natedev", targets.Held(
                "hana", "render checks", SINCE, targets.UntilReleased(),
            )),
            "natedev: held by hana since 13:05 PDT — render checks",
        )

    def test_unreachable_line(self) -> None:
        self.assertEqual(
            build_targets.format_state("mac", targets.Unreachable("ssh timed out")),
            "mac: unreachable — ssh timed out",
        )

    def test_hold_reason_whitespace_collapses_to_one_line(self) -> None:
        self.assertEqual(
            build_targets.format_state("mac", targets.Held(
                "hana", "  render\n\tchecks\r\n now  ", SINCE, targets.UntilReleased(),
            )),
            "mac: held by hana since 13:05 PDT — render checks now",
        )

    def test_holder_whitespace_collapses_to_one_line(self) -> None:
        self.assertEqual(
            build_targets.format_state("mac", targets.Held(
                "  hana\n\t team  ", "render checks", SINCE, targets.UntilReleased(),
            )),
            "mac: held by hana team since 13:05 PDT — render checks",
        )

    def test_run_description_whitespace_collapses_to_one_line(self) -> None:
        self.assertEqual(
            build_targets.format_state("mac", targets.Running("  nextest\n\t hana\r\nrun  ", SINCE)),
            "mac: running nextest hana run since 13:05 PDT",
        )

    def test_unreachable_reason_whitespace_collapses_to_one_line(self) -> None:
        self.assertEqual(
            build_targets.format_state("mac", targets.Unreachable("  ssh\n\t timed\r\nout  ")),
            "mac: unreachable — ssh timed out",
        )

    def test_unreadable_reason_whitespace_collapses_to_one_line(self) -> None:
        self.assertEqual(
            build_targets.format_state("mac", targets.StateUnreadable("  invalid\n\t claim\r\nrecord  ")),
            "mac: state unreadable — invalid claim record",
        )

    def test_retained_expired_hold_has_no_past_deadline(self) -> None:
        self.write_block(NOW - timedelta(seconds=1))
        state = targets.target_state(targets.SshTarget("mac", "macos", self.settings))
        self.assertEqual(
            build_targets.format_state("mac", state),
            "mac: held by hana since 13:05 PDT — render checks",
        )

    def test_holder_read_failure_has_exact_status_line(self) -> None:
        self.write_holder()
        holder = self.holders / "hana"
        original_stat = Path.stat

        def stat(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
            if path == holder:
                raise PermissionError("holder access denied")
            return original_stat(path, follow_symlinks=follow_symlinks)

        with mock.patch.object(Path, "stat", stat):
            state = targets.target_state(targets.LocalTarget("natedev", "linux"))
        self.assertEqual(build_targets.format_state("natedev", state), "natedev: state unreadable — holder access denied")

    def test_block_read_failure_has_exact_status_line(self) -> None:
        block = self.mac_state / "block.json"
        _ = block.write_text("{}", encoding="utf-8")
        state = targets.target_state(targets.SshTarget("mac", "macos", self.settings))
        self.assertEqual(build_targets.format_state("mac", state), f"mac: state unreadable — {block} has an invalid block record")

    def test_claim_read_failure_has_exact_status_line(self) -> None:
        claim = self.mac_state / "run.json"
        _ = claim.write_text("{}", encoding="utf-8")
        state = targets.target_state(targets.SshTarget("mac", "macos", self.settings))
        self.assertEqual(build_targets.format_state("mac", state), f"mac: state unreadable — {claim} has an invalid run record")

    def test_local_time_observes_winter_offset(self) -> None:
        instant = datetime(2026, 12, 11, 21, 5, tzinfo=timezone.utc)
        self.assertEqual(build_targets.local_time(instant), "13:05 PST")

    def test_shipped_config_prints_one_line_per_target_without_probe(self) -> None:
        with mock.patch.dict(os.environ, {"BUILD_TARGETS_CONFIG": str(SHIPPED_CONFIG)}):
            with mock.patch.object(build_targets, "probe_target", side_effect=AssertionError("Probe not requested")):
                self.assertEqual(self.capture_status(), (0, "mac: free\nnatedev: free\n", ""))

    def test_command_uses_config_order_and_recorded_hold(self) -> None:
        config = self.write_config(
            "targets=natedev,mac\nkind.natedev=local\nplatform.natedev=linux\n"
            + "kind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n"
        )
        self.write_block()
        with mock.patch.dict(os.environ, {"BUILD_TARGETS_CONFIG": str(config)}):
            self.assertEqual(self.capture_status(), (
                0,
                "natedev: free\nmac: held by hana since 13:05 PDT until 15:05 PDT — render checks\n",
                "",
            ))

    def test_probe_flag_reports_unreachable_mac_and_keeps_local_state(self) -> None:
        probed: list[targets.SshTarget] = []

        def unavailable(target: targets.SshTarget) -> targets.TargetState:
            probed.append(target)
            return targets.Unreachable("ssh timed out")

        self.write_holder()
        output = io.StringIO()
        errors = io.StringIO()
        with mock.patch.dict(os.environ, {"BUILD_TARGETS_CONFIG": str(SHIPPED_CONFIG)}):
            with mock.patch.object(build_targets, "probe_target", side_effect=unavailable):
                with mock.patch.object(sys, "argv", ["build_targets.py", "status", "--probe"]):
                    with redirect_stdout(output), redirect_stderr(errors):
                        code = build_targets.main()
        self.assertEqual(code, 0)
        self.assertEqual(output.getvalue(), (
            "mac: unreachable — ssh timed out\n"
            "natedev: held by hana since 13:05 PDT — render checks\n"
        ))
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(probed, [targets.SshTarget("mac", "macos", SHIPPED_CONFIG.with_name("mac_test.conf"))])

    def test_status_argument_defaults_to_recorded_state(self) -> None:
        output = io.StringIO()
        errors = io.StringIO()
        with mock.patch.dict(os.environ, {"BUILD_TARGETS_CONFIG": str(SHIPPED_CONFIG)}):
            with mock.patch.object(build_targets, "probe_target", side_effect=AssertionError("Probe not requested")):
                with mock.patch.object(sys, "argv", ["build_targets.py", "status"]):
                    with redirect_stdout(output), redirect_stderr(errors):
                        code = build_targets.main()
        self.assertEqual((code, output.getvalue(), errors.getvalue()), (0, "mac: free\nnatedev: free\n", ""))

    def test_failed_mac_probe_becomes_unreachable_status(self) -> None:
        calls: list[str] = []

        def unavailable(
            _controller: offload.ProcessController,
            config: offload.OffloadConfig,
        ) -> offload.HostReachability:
            calls.append(config.host)
            return offload.UnreachableHost(f"SSH probe failed for {config.host}")

        config = self.write_config(
            "targets=mac,natedev\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n"
            + "kind.natedev=local\nplatform.natedev=linux\n"
        )
        with mock.patch.dict(os.environ, {"BUILD_TARGETS_CONFIG": str(config)}):
            with mock.patch.object(
                offload, "repository_identity",
                side_effect=AssertionError("Reachability needs no repository"),
            ):
                with mock.patch.object(offload, "probe_reachability", side_effect=unavailable):
                    self.assertEqual(self.capture_status(probe=True), (
                        0, "mac: unreachable — SSH probe failed for test-mac\nnatedev: free\n", "",
                    ))
        self.assertEqual(calls, ["test-mac"])

    def test_probe_uses_shared_settings_reader_without_building_config(self) -> None:
        target = targets.SshTarget("mac", "macos", self.settings)
        config = offload.OffloadConfig(False, "reader-mac", 2.0, 3.0, 4.0, 5.0, "6", {})
        calls: list[offload.OffloadConfig] = []

        def reachable(
            _controller: offload.ProcessController,
            received: offload.OffloadConfig,
        ) -> offload.HostReachability:
            calls.append(received)
            return offload.ReachableHost()

        self.write_block()
        block = self.mac_state / "block.json"
        before = block.read_bytes()
        with mock.patch.object(offload, "read_config", return_value=config) as reader:
            with mock.patch.object(offload, "OffloadConfig", side_effect=AssertionError("Use the shared config reader")):
                with mock.patch.object(offload, "repository_identity", side_effect=AssertionError("No repository required")):
                    with mock.patch.object(offload, "probe_mac", side_effect=AssertionError("Use reachability only")):
                        with mock.patch.object(offload, "probe_reachability", side_effect=reachable):
                            self.assertEqual(
                                build_targets.probe_target(target),
                                targets.Held("hana", "render checks", SINCE, targets.ExpiresAt(END)),
                            )
        reader.assert_called_once_with(self.settings)
        self.assertEqual(calls, [config])
        self.assertEqual(block.read_bytes(), before)


class EligibleCommandTests(IsolatedTargetTest):
    def test_cli_prints_every_target_in_config_order_with_resolved_settings(self) -> None:
        config = self.write_config(
            "targets=natedev,mac\nkind.natedev=local\nplatform.natedev=linux\n"
            + "kind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n"
        )
        for settings, local, reason in (("linux_only.hana=\n", [], "-"),
                                        ("", [], "repo"),
                                        ("linux_only.hana=hana\n", [], "linux_only"),
                                        ("", ["--local", "Linux filesystem required"], "Linux filesystem required")):
            with self.subTest(reason=reason):
                _ = self.settings.write_text(settings)
                output = io.StringIO()
                with mock.patch.dict(os.environ, {"BUILD_TARGETS_CONFIG": str(config)}), \
                     mock.patch.object(offload, "repository_identity", return_value=offload.KnownRepository("hana")), \
                     mock.patch.object(sys, "argv", ["build_targets.py", "eligible", "--repo-root", str(self.root),
                                                    "--package", "hana", *local]), redirect_stdout(output):
                    self.assertEqual(build_targets.main(), 0)
                disposition = "eligible" if reason == "-" else "excluded"
                self.assertEqual(output.getvalue(), f"natedev\teligible\t-\t-\nmac\t{disposition}\t{reason}\t{self.settings}\n")


class WaitLocalTests(IsolatedTargetTest):
    sleeps: int = 0

    @override
    def setUp(self) -> None:
        super().setUp()
        self.sleeps = 0
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "BUILD_HOLD_RELEASE_DIR": str(self.root / "release"),
            "PLAN_DELEGATE_ACTIVE_DIR": str(self.root / "active"),
            "PLAN_DELEGATE_BOARD_DIR": "", "PLAN_DELEGATE_SESSION_DIR": "",
            "BUILD_TARGETS_POLL_S": "0.01",
            "BUILDLOG_HOLD_WAIT_FILE": str(self.root / "wait-seconds"),
            "BUILDLOG_HOLD_ADMISSION_FILE": str(self.root / "admission"),
            "BUILDLOG_HOLD_STARTED_FILE": str(self.root / "started"),
            "BUILDLOG_HOLD_WAITER_FILE": str(self.root / "waiter"),
            "BUILDLOG_HOLD_REGISTRATION_FILE": str(self.root / "registration"),
            "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
        })))
        _ = self.enterContext(mock.patch.object(build_hold, "process_start_time", return_value="test-start"))
        _ = self.enterContext(mock.patch.object(build_hold, "call_is_live", return_value=True))
        _ = self.enterContext(mock.patch.object(build_hold, "socket_for", return_value=build_hold.RecipientLive("/test/socket")))
        _ = self.enterContext(mock.patch.object(build_hold, "send_release", return_value=build_hold.ReleaseDelivered()))
        _ = self.enterContext(mock.patch.object(build_targets, "wait_clock", return_value=NOW))

    def begin_hold(self) -> None:
        _ = build_hold.start_hold(self.holders, "hana", "render checks", build_hold.NoReleaseEta(), NOW)

    def release_turn(self, _seconds: float) -> None:
        self.sleeps += 1
        cycle = build_hold.read_cycle()
        assert cycle is not None
        cycle["release_started_at"] = NOW.isoformat()
        build_hold.save_cycle(cycle)
        _ = build_hold.advance_release(cycle, NOW)

    def wait(self) -> tuple[int, str, str]:
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = build_targets.command_wait_local("call-a", pid=12345)
        return code, output.getvalue(), errors.getvalue()

    def test_check_local_admits_a_launch_after_the_release_began(self) -> None:
        self.begin_hold()
        build_hold.record_recipients([("owner name", "owner")])
        _ = build_hold.register_wait("owner")
        cycle = build_hold.read_cycle()
        assert cycle is not None
        cycle["release_started_at"] = NOW.isoformat()
        build_hold.save_cycle(cycle)
        with mock.patch.object(build_hold, "send_release", return_value=build_hold.ReleaseDelivered()) as send:
            _ = build_hold.advance_release(cycle, NOW, clock=lambda: NOW)
            later = NOW + timedelta(seconds=build_hold.NO_ADMISSION_ACK_S)
            cycle = build_hold.read_cycle()
            assert cycle is not None
            self.assertTrue(build_hold.advance_release(cycle, later, clock=lambda: later)[0])
            path = self.root / "release" / cycle["id"] / "cycle.json"
            before = path.read_bytes()
            current = build_hold.read_cycle()
            assert current is not None
            self.assertEqual(current["entries"][0]["state"], "NoAdmissionAck")
            output, errors = io.StringIO(), io.StringIO()
            with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "owner"}), \
                 redirect_stdout(output), redirect_stderr(errors):
                self.assertEqual(build_targets.command_check_local(), 0)
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(errors.getvalue(), "")
            self.assertEqual(path.read_bytes(), before)
            send.assert_called_once()
            entry = cast(build_hold.ReleaseEntry, send.call_args.args[0])
            self.assertEqual(entry["session_id"], "owner")

    def test_wait_local_returns_at_once_for_a_call_that_arrives_during_a_release(self) -> None:
        self.begin_hold()
        cycle = build_hold.read_cycle()
        assert cycle is not None
        cycle["release_started_at"] = NOW.isoformat()
        build_hold.save_cycle(cycle)
        path = self.root / "release" / cycle["id"] / "cycle.json"
        before = path.read_bytes()
        with mock.patch.object(build_targets, "wait_sleep", side_effect=AssertionError("Late calls never poll")):
            self.assertEqual(self.wait(), (0, "", ""))
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.root / "registration").exists())
        self.assertFalse((self.root / "waiter").exists())
        self.assertEqual((self.root / "admission").read_text().strip(),
                         f"{cycle['id']} {cycle.get('holder_generation', 1)}")

    def test_unheld_call_returns_without_polling_or_creating_cycle(self) -> None:
        with mock.patch.object(build_targets, "wait_sleep", side_effect=AssertionError("No hold needs no poll")):
            code, output, errors = self.wait()
        self.assertEqual((code, output, errors), (0, "", ""))
        self.assertFalse((self.root / "release").exists())

    def test_unheld_call_skips_owner_and_admission_lookup(self) -> None:
        with mock.patch.object(build_hold, "owning_session", side_effect=AssertionError("Owner lookup not needed")):
            with mock.patch.object(build_hold, "read_admission", side_effect=AssertionError("Admission lookup not needed")):
                self.assertEqual(self.wait(), (0, "", ""))

    def test_finish_without_registration_skips_release_state_and_call_removal(self) -> None:
        self.begin_hold()
        with mock.patch.object(build_hold, "read_cycle", side_effect=AssertionError("Release read not needed")):
            with mock.patch.object(build_hold, "remove_call", side_effect=AssertionError("No call to remove")):
                with mock.patch.object(build_hold, "owning_session", side_effect=AssertionError("Owner lookup not needed")):
                    for registration in ("", str(self.root / "registration")):
                        with self.subTest(registration=registration):
                            with mock.patch.dict(os.environ, {"BUILDLOG_HOLD_REGISTRATION_FILE": registration}):
                                self.assertEqual(build_targets.command_finish_local("call-a"), 0)

    def test_held_call_names_holder_and_runs_after_its_recorded_release(self) -> None:
        self.begin_hold()
        with mock.patch.object(build_targets, "wait_sleep", side_effect=self.release_turn):
            code, output, errors = self.wait()
        self.assertEqual(code, 0, output + errors)
        self.assertIn("verify.sh: natedev is held by hana for render checks; waiting (tests that can run on the Mac keep running there).", output + errors)
        self.assertEqual(self.sleeps, 1)
        decision = build_hold.read_admission("verify:call-a", "call-a")
        self.assertIsInstance(decision, build_hold.AdmittedTurn)
        self.assertTrue((self.holders / "hana").exists())

    def test_waiting_line_collapses_holder_and_reason_whitespace(self) -> None:
        self.begin_hold()
        holder = targets.Held(" hana\nteam ", " render\nchecks ", SINCE, targets.UntilReleased())
        with mock.patch.object(build_targets, "target_state", return_value=holder):
            with mock.patch.object(build_targets, "wait_sleep", side_effect=self.release_turn):
                code, output, errors = self.wait()
        self.assertEqual(code, 0)
        self.assertIn("held by hana team for render checks; waiting", output + errors)

    def test_wait_registers_under_run_owner_with_stale_shell_id(self) -> None:
        self.begin_hold()
        active = self.root / "active"
        active.mkdir()
        run = self.root / "delegate-run"
        _ = (active / "owner").write_text(str(run) + "\n")
        with mock.patch.dict(os.environ, {"PLAN_DELEGATE_BOARD_DIR": str(run), "CLAUDE_CODE_SESSION_ID": "stale"}):
            with mock.patch.object(build_targets, "wait_sleep", side_effect=self.release_turn):
                self.assertEqual(self.wait()[0], 0)
        cycle = build_hold.read_cycle()
        assert cycle is not None
        self.assertEqual([entry["session_id"] for entry in cycle["entries"]], ["owner"])

    def test_missing_cycle_waits_until_the_hold_itself_clears(self) -> None:
        self.write_holder()

        def clear(_seconds: float) -> None:
            self.sleeps += 1
            (self.holders / "hana").unlink()

        with mock.patch.object(build_targets, "wait_sleep", side_effect=clear):
            self.assertEqual(self.wait()[0], 0)
        self.assertEqual(self.sleeps, 1)

    def test_changed_cycle_registers_again_before_admission(self) -> None:
        self.begin_hold()

        def change_then_release(seconds: float) -> None:
            if self.sleeps == 0:
                self.sleeps += 1
                _ = build_hold.open_cycle(NOW)
            else:
                self.release_turn(seconds)

        with mock.patch.object(build_targets, "wait_sleep", side_effect=change_then_release):
            self.assertEqual(self.wait()[0], 0)
        self.assertEqual(self.sleeps, 2)
        self.assertIsInstance(build_hold.read_admission("verify:call-a", "call-a"), build_hold.AdmittedTurn)

    def test_unreadable_cycle_exits_twelve_without_polling(self) -> None:
        self.begin_hold()
        cycle = build_hold.read_cycle()
        assert cycle is not None
        path = self.root / "release" / cycle["id"] / "cycle.json"
        _ = path.write_text("{broken")
        with mock.patch.object(build_targets, "wait_sleep", side_effect=AssertionError("Invalid record cannot admit")):
            code, output, errors = self.wait()
        self.assertEqual(code, 12)
        self.assertIn("hold state cannot be read (", output + errors)
        self.assertIn("Expecting property name", output + errors)
        self.assertIn("); not starting.", output + errors)
        self.assertEqual(path.read_text(), "{broken")

    def test_unreadable_holder_state_exits_twelve_without_waiting(self) -> None:
        with mock.patch.object(build_targets, "target_state", return_value=targets.StateUnreadable("permission denied")):
            with mock.patch.object(build_targets, "wait_sleep", side_effect=AssertionError("Unreadable state cannot admit")):
                code, output, errors = self.wait()
        self.assertEqual(code, 12)
        self.assertIn("verify.sh: natedev's hold state cannot be read (permission denied); not starting.", output + errors)

    def test_two_hour_notice_uses_injected_clock_and_keeps_waiting(self) -> None:
        self.begin_hold()

        def clock() -> datetime:
            return NOW + timedelta(hours=2 if self.sleeps else 0)

        def later_release(seconds: float) -> None:
            if self.sleeps == 0:
                self.sleeps += 1
            else:
                self.release_turn(seconds)

        with mock.patch.object(build_targets, "wait_clock", side_effect=clock):
            with mock.patch.object(build_targets, "wait_sleep", side_effect=later_release):
                code, output, errors = self.wait()
        self.assertEqual(code, 0)
        self.assertEqual(self.sleeps, 2)
        self.assertIn("verify.sh: natedev has been held for 2 h by hana; ask them.", output + errors)

    def test_local_check_denies_unreleased_session_without_memory_mark(self) -> None:
        self.begin_hold()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(build_targets.command_check_local(), 10)
        cycle = build_hold.read_cycle()
        assert cycle is not None
        self.assertFalse(any(entry["state"] == "MemoryGateReturned" for entry in cycle["entries"]))

    def test_register_local_is_nonblocking_repeatable_and_uses_given_pid(self) -> None:
        self.begin_hold()
        with mock.patch.object(build_targets, "wait_sleep", side_effect=AssertionError("Registration must return")):
            for _ in range(2):
                self.assertEqual(build_targets.command_register_local("call-a", pid=12345), 10)
        cycle = build_hold.read_cycle()
        assert cycle is not None
        self.assertEqual(len(cycle["entries"]), 1)
        calls = cycle["entries"][0]["calls"]
        self.assertEqual([(call["call_id"], call["pid"]) for call in calls], [("call-a", 12345)])
        self.assertEqual(json.loads((self.root / "registration").read_text()),
                         {"key": "verify:call-a", "cycle_id": cycle["id"]})

    def test_register_local_renews_after_cycle_and_generation_change(self) -> None:
        self.begin_hold()
        self.assertEqual(build_targets.command_register_local("call-a", pid=12345), 10)
        first = build_hold.read_cycle()
        assert first is not None
        _ = build_hold.open_cycle(NOW + timedelta(seconds=1))
        self.assertEqual(build_targets.command_register_local("call-a", pid=12345), 10)
        second = build_hold.read_cycle()
        assert second is not None
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(second["entries"][0]["calls"][0]["call_id"], "call-a")
        self.release_turn(0)
        self.assertEqual(build_targets.command_register_local("call-a", pid=12345), 0)
        _ = build_hold.start_hold(self.holders, "another", "new hold", build_hold.NoReleaseEta(), NOW + timedelta(seconds=2))
        self.assertEqual(build_targets.command_register_local("call-a", pid=12345), 10)
        renewed = build_hold.read_cycle()
        assert renewed is not None
        self.assertGreater(renewed.get("holder_generation", 1), second.get("holder_generation", 1))
        self.assertEqual(len(renewed["entries"][0]["calls"]), 1)

    def test_register_local_free_and_unreadable_states_do_not_create_registration(self) -> None:
        self.assertEqual(build_targets.command_register_local("call-a", pid=12345), 0)
        self.assertFalse((self.root / "registration").exists())
        with mock.patch.object(build_targets, "target_state", return_value=targets.StateUnreadable("permission denied")), \
             redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(build_targets.command_register_local("call-a", pid=12345), 12)
        self.assertIn("permission denied", errors.getvalue())
        self.assertFalse((self.root / "registration").exists())

    def test_nonblocking_registration_keeps_holder_and_two_hour_diagnostic(self) -> None:
        self.begin_hold()
        with mock.patch.object(build_targets, "wait_clock", return_value=NOW + timedelta(hours=2)), \
             redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(build_targets.command_register_local("call-a", pid=12345), 10)
        self.assertEqual(errors.getvalue(), "verify.sh: natedev is held by hana for render checks.\n"
                         + "verify.sh: natedev has been held for 2 h by hana; ask them.\n")

    def test_register_local_rejects_nonpositive_pid_before_reading_state(self) -> None:
        for pid in ("0", "-1"):
            with self.subTest(pid=pid), \
                 mock.patch.object(sys, "argv", ["build_targets.py", "register-local", "--call-id", "call-a", "--pid", pid]), \
                 mock.patch.object(build_targets, "target_state", side_effect=AssertionError("Invalid arguments must not read state")), \
                 redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit) as refused:
                _ = build_targets.main()
            self.assertEqual(refused.exception.code, 2)
            self.assertIn("argument --pid:", errors.getvalue())
            self.assertIn("positive", errors.getvalue())
        self.assertFalse((self.root / "registration").exists())

    def test_registration_outlives_helper_and_is_owned_by_waiting_process(self) -> None:
        self.begin_hold()
        environment = dict(os.environ)
        environment.update({"CLAUDE_CODE_SESSION_ID": "", "PLAN_DELEGATE_BOARD_DIR": "",
                            "PLAN_DELEGATE_SESSION_DIR": ""})
        # Restore only this subprocess boundary: the CLI must leave its parent's live PID in the record.
        with mock.patch.object(subprocess, "run", wraps=REAL_SUBPROCESS_RUN), \
             mock.patch.object(subprocess, "Popen", wraps=REAL_SUBPROCESS_POPEN):
            result = subprocess.run([sys.executable, build_targets.__file__, "register-local", "--call-id", "call-a",
                                     "--pid", str(os.getpid())], env=environment, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 10, result.stdout + result.stderr)
        cycle = build_hold.read_cycle()
        assert cycle is not None
        call = cycle["entries"][0]["calls"][0]
        self.assertEqual(call["pid"], os.getpid())
        with mock.patch.object(build_hold, "process_start_time", wraps=REAL_PROCESS_START_TIME), \
             mock.patch.object(build_hold, "call_is_live", wraps=REAL_CALL_IS_LIVE):
            self.assertTrue(build_hold.call_is_live(call))

    def test_finish_local_on_admitted_unmarked_call_releases_next_entry(self) -> None:
        self.begin_hold()
        build_hold.record_recipients([("owner", "owner"), ("peer", "peer")])
        cycle = build_hold.read_cycle()
        assert cycle is not None
        cycle["release_started_at"] = NOW.isoformat()
        build_hold.save_cycle(cycle)
        _ = build_hold.advance_release(cycle, NOW, clock=lambda: NOW)
        with mock.patch.object(build_hold, "waiter_key", return_value="owner"):
            self.assertEqual(build_targets.command_register_local("call-a", pid=12345), 0)
        self.assertIsInstance(build_hold.read_admission("owner", "call-a"), build_hold.AdmittedTurn)
        self.assertEqual(build_targets.command_finish_local("call-a"), 0)
        cycle = build_hold.read_cycle()
        assert cycle is not None
        with mock.patch.object(build_hold, "send_release", return_value=build_hold.ReleaseDelivered()) as sent:
            _ = build_hold.advance_release(cycle, NOW, clock=lambda: NOW)
        self.assertEqual(sent.call_count, 1)
        self.assertEqual(sent.call_args.args[0]["session_id"], "peer")


if __name__ == "__main__":
    _ = unittest.main()
