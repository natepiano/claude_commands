"""Check status lines and optional probes without contacting another host."""

from __future__ import annotations

import io
import os
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import override
from unittest import mock
import unittest

import build_targets
import offload
import targets
from test_targets import END, NOW, SHIPPED_CONFIG, SINCE, IsolatedTargetTest


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


if __name__ == "__main__":
    _ = unittest.main()
