"""Check status lines and optional probes without contacting another host."""

from __future__ import annotations

import io
import json
import os
import sys
import subprocess
import time
from collections.abc import Generator, Iterable
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, cast, override
from unittest import mock
import unittest

sys.path.insert(0, str(Path(__file__).parent))

import build_targets
import build_hold
import offload
import mac_test
import targets
if TYPE_CHECKING:
    from ..message import roster
    from ..buildlog import record, store
else:
    from scripts.message import roster
    import record
    import store
from test_targets import END, NOW, SHIPPED_CONFIG, SINCE, IsolatedTargetTest

REAL_SUBPROCESS_RUN = subprocess.run
REAL_SUBPROCESS_POPEN = subprocess.Popen
REAL_PROCESS_START_TIME = build_hold.process_start_time
REAL_RECORD_HOLD_EVENT = build_hold.record_hold_event
REAL_BUILDLOG_ROOT = Path.home() / ".local/state/buildlog"


@contextmanager
def pacific_zone() -> Generator[None, None, None]:
    try:
        with mock.patch.dict(os.environ, {"TZ": "America/Los_Angeles", "USER_ZONE": "America/Los_Angeles"}):
            time.tzset()
            yield
    finally:
        time.tzset()


class FolderInvocationTests(unittest.TestCase):
    def test_folder_invocation_records_hold_events(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "unittest",
             "test_build_targets.WaitLocalTests.test_hold_and_release_write_temp_log_without_writing_real_log"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True, text=True, check=False, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class IsolatedBuildTargetTest(IsolatedTargetTest):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "BUILDLOG_DIR": str(self.root / "buildlog"),
        })))


class StatusTests(IsolatedBuildTargetTest):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.enterContext(pacific_zone())
        _ = self.enterContext(mock.patch.object(targets, "utc_now", return_value=NOW))
        _ = self.enterContext(mock.patch.object(roster, "unit_labels_for", return_value={}))

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
            build_targets.format_state(
                "mac", targets.Running((targets.RunningJob("nextest hana", since, targets.NOT_SEEN_IDLE),)),
            ),
            "mac: running nextest hana since 13:20 PDT",
        )

    def capture_claim_status(self, holder_session: str = "") -> tuple[int, str, str]:
        self.write_claim("claims/hana.json")
        if holder_session:
            path = self.mac_state / "claims" / "hana.json"
            claim = cast(dict[str, object], json.loads(path.read_text()))
            claim["holder_session"] = holder_session
            _ = path.write_text(json.dumps(claim))
        config = self.write_config(
            "targets=mac,natedev\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n"
            + "kind.natedev=local\nplatform.natedev=linux\n",
        )
        with mock.patch.dict(os.environ, {"BUILD_TARGETS_CONFIG": str(config)}), \
             mock.patch.object(mac_test, "process_start_time", return_value="test-run-start"):
            return self.capture_status()

    def test_claim_holder_names_showrunner_and_unit_after_since(self) -> None:
        labels = {"inspector-session": roster.UnitLabels("inspectors", "hana", "inspectors")}
        with mock.patch.object(roster, "unit_labels_for", return_value=labels) as lookup, \
             mock.patch.object(roster, "roster", side_effect=AssertionError("no full roster")) as full:
            code, output, errors = self.capture_claim_status("inspector-session")
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(output,
            "mac: running nextest hana since 13:05 PDT (showrunner hana, unit inspectors)\nnatedev: free\n")
        lookup.assert_called_once()
        self.assertEqual(set(cast(Iterable[str], lookup.call_args.args[0])), {"inspector-session"})
        full.assert_not_called()

    def test_unknown_claim_holder_preserves_running_text(self) -> None:
        with mock.patch.object(roster, "unit_labels_for", return_value={}):
            result = self.capture_claim_status("unknown-session")
        self.assertEqual(result, (0, "mac: running nextest hana since 13:05 PDT\nnatedev: free\n", ""))

    def test_stale_claim_with_same_description_and_time_cannot_replace_live_holder(self) -> None:
        for name, holder, started in (
            ("a-live", "live-session", "test-run-start"),
            ("z-stale", "stale-session", "ended-run-start"),
        ):
            self.write_claim(f"claims/{name}.json")
            path = self.mac_state / "claims" / f"{name}.json"
            claim = cast(dict[str, object], json.loads(path.read_text()))
            claim.update(holder_session=holder, proc_start=started)
            _ = path.write_text(json.dumps(claim))
        config = self.write_config("targets=mac\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n")
        labels = {
            "live-session": roster.UnitLabels("report", "builds", "report"),
            "stale-session": roster.UnitLabels("inspectors", "hana", "inspectors"),
        }
        with mock.patch.dict(os.environ, {"BUILD_TARGETS_CONFIG": str(config)}), \
             mock.patch.object(mac_test, "process_start_time", return_value="test-run-start"), \
             mock.patch.object(mac_test, "read_claims", wraps=mac_test.read_claims) as claims_read, \
             mock.patch.object(roster, "unit_labels_for", return_value=labels) as lookup:
            result = self.capture_status()
        self.assertEqual(result, (
            0, "mac: running nextest hana since 13:05 PDT (showrunner builds, unit report)\n", "",
        ))
        lookup.assert_called_once()
        self.assertEqual(set(cast(Iterable[str], lookup.call_args.args[0])), {"live-session"})
        claims_read.assert_called_once()

    def test_claim_without_holder_session_does_not_read_labels(self) -> None:
        with mock.patch.object(roster, "unit_labels_for") as lookup:
            result = self.capture_claim_status()
        self.assertEqual(result, (0, "mac: running nextest hana since 13:05 PDT\nnatedev: free\n", ""))
        lookup.assert_not_called()

    def test_failed_claim_label_lookup_preserves_successful_status(self) -> None:
        with mock.patch.object(roster, "unit_labels_for", side_effect=RuntimeError("broken registry")):
            result = self.capture_claim_status("inspector-session")
        self.assertEqual(result, (0, "mac: running nextest hana since 13:05 PDT\nnatedev: free\n", ""))

    def test_named_hold_uses_the_same_labels_after_since(self) -> None:
        self.write_holder()
        _ = (self.holders / "hana").rename(self.holders / "report-session")
        config = self.write_config("targets=natedev\nkind.natedev=local\nplatform.natedev=linux\n")
        labels = {"report-session": roster.UnitLabels("report", "builds", "report")}
        with mock.patch.dict(os.environ, {"BUILD_TARGETS_CONFIG": str(config)}), \
             mock.patch.object(roster, "unit_labels_for", return_value=labels):
            result = self.capture_status()
        self.assertEqual(result, (
            0, "natedev: held by hana since 13:05 PDT (showrunner builds, unit report) — render checks\n", "",
        ))

    def test_named_mac_hold_uses_current_labels_and_keeps_expiry(self) -> None:
        self.write_block()
        config = self.write_config(
            "targets=mac\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n",
        )
        labels = {"hana-session": roster.UnitLabels("inspectors", "hana", "inspectors")}
        with mock.patch.dict(os.environ, {"BUILD_TARGETS_CONFIG": str(config)}), \
             mock.patch.object(roster, "unit_labels_for", return_value=labels):
            result = self.capture_status()
        self.assertEqual(result, (
            0, "mac: held by hana since 13:05 PDT (showrunner hana, unit inspectors) until 15:05 PDT — render checks\n", "",
        ))

    def test_mac_running_status_adds_idle_since_from_claim(self) -> None:
        self.write_claim()
        path = self.mac_state / "run.json"
        record = cast(dict[str, object], json.loads(path.read_text()))
        record["idle_since"] = "2026-10-11T20:20:00+00:00"
        _ = path.write_text(json.dumps(record))
        config = self.write_config(
            "targets=mac,natedev\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n"
            + "kind.natedev=local\nplatform.natedev=linux\n",
        )
        with mock.patch.dict(os.environ, {"BUILD_TARGETS_CONFIG": str(config)}), \
             mock.patch.object(mac_test, "process_start_time", return_value="test-run-start"):
            code, output, errors = self.capture_status()
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(output, "mac: running nextest hana since 13:05 PDT, idle since 13:20\nnatedev: free\n")

    def test_mac_running_status_lists_every_repository_claim(self) -> None:
        later = SINCE + timedelta(minutes=10)
        self.write_claim("claims/cargo-liner.json", "nextest cargo-liner", later)
        liner = self.mac_state / "claims" / "cargo-liner.json"
        record = cast(dict[str, object], json.loads(liner.read_text()))
        record["idle_since"] = "2026-10-11T20:20:00+00:00"
        _ = liner.write_text(json.dumps(record))
        self.write_claim("claims/hana.json")
        config = self.write_config(
            "targets=mac,natedev\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n"
            + "kind.natedev=local\nplatform.natedev=linux\n",
        )
        with mock.patch.dict(os.environ, {"BUILD_TARGETS_CONFIG": str(config)}), \
             mock.patch.object(mac_test, "process_start_time", return_value="test-run-start"):
            code, output, errors = self.capture_status()
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(
            output,
            "mac: running nextest hana since 13:05 PDT; "
            + "running nextest cargo-liner since 13:15 PDT, idle since 13:20\nnatedev: free\n",
        )

    def test_mac_running_status_without_idle_time_keeps_existing_line(self) -> None:
        self.write_claim()
        with mock.patch.object(mac_test, "process_start_time", return_value="test-run-start"):
            state = targets.target_state(targets.SshTarget("mac", "macos", self.settings))
            self.assertEqual(build_targets.format_state("mac", state), "mac: running nextest hana since 13:05 PDT")

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
            build_targets.format_state(
                "mac", targets.Running((targets.RunningJob("  nextest\n\t hana\r\nrun  ", SINCE, targets.NOT_SEEN_IDLE),)),
            ),
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
                                targets.Held("hana", "render checks", SINCE, targets.ExpiresAt(END),
                                             targets.HolderSession("hana-session")),
                            )
        reader.assert_called_once_with(self.settings)
        self.assertEqual(calls, [config])
        self.assertEqual(block.read_bytes(), before)


class EligibleCommandTests(IsolatedBuildTargetTest):
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


class StepKindEligibilityTests(IsolatedBuildTargetTest):
    @override
    def setUp(self) -> None:
        super().setUp()
        config = self.write_config(
            "targets=mac,natedev\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n"
            + "kind.natedev=local\nplatform.natedev=linux\n")
        _ = self.settings.write_text("offload=on\nlinux_only.hana=linux-part\nfree_floor_gib=60\n")
        _ = (self.root / "Cargo.toml").write_text('[workspace]\nmembers=["crates/*"]\nexclude=["crates/excluded"]\n')
        for directory, name in (("portable", "portable"), ("excluded", "linux-part")):
            member = self.root / "crates" / directory
            member.mkdir(parents=True)
            _ = (member / "Cargo.toml").write_text(f'[package]\nname="{name}"\nversion="0.1.0"\n')
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"BUILD_TARGETS_CONFIG": str(config)})))
        _ = self.enterContext(mock.patch.object(offload, "repository_identity", return_value=offload.KnownRepository("hana")))

    def kind_output(self, kind: str, package: str = "", free_gib: float = 60) -> str:
        output = io.StringIO()
        with (mock.patch.object(sys, "argv", ["build_targets.py", "eligible", "--repo-root", str(self.root),
                                               "--kind", kind, "--package", package]),
              mock.patch.object(offload, "probe_mac", return_value=offload.ProbeSnapshot(free_gib, "fake-mac")),
              redirect_stdout(output)):
            self.assertEqual(build_targets.main(), 0)
        return output.getvalue()

    def kind_answer(self, kind: str, package: str = "", free_gib: float = 60) -> str:
        return self.kind_output(kind, package, free_gib).splitlines()[0] + "\n"

    def test_kind_table_keeps_target_order_settings_and_each_mac_decision(self) -> None:
        other_settings = self.settings.with_name("other.conf")
        _ = other_settings.write_text("offload=off\nlinux_only.hana=\n")
        _ = self.write_config(
            "targets=natedev,other,mac\nkind.natedev=local\nplatform.natedev=linux\n"
            + "kind.other=ssh\nplatform.other=macos\nsettings.other=other.conf\n"
            + "kind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n")
        self.assertEqual(self.kind_output("check"),
                         "mac_allowed\nnatedev\teligible\t-\t-\n"
                         + f"other\texcluded\toff\t{other_settings}\nmac\teligible\t-\t{self.settings}\n")
        self.assertEqual(self.kind_output("build"),
                         "natedev_only makes natedev artifacts\nnatedev\teligible\t-\t-\n"
                         + f"other\texcluded\tmakes natedev artifacts\t{other_settings}\n"
                         + f"mac\texcluded\tmakes natedev artifacts\t{self.settings}\n")

    def test_local_only_configuration_keeps_artifact_and_gate_reasons(self) -> None:
        _ = self.write_config("targets=natedev\nkind.natedev=local\nplatform.natedev=linux\n")
        for kind, reason in (("build", "makes natedev artifacts"), ("example", "makes natedev artifacts"),
                             ("final", "full gate runs on natedev")):
            with self.subTest(kind=kind):
                self.assertEqual(self.kind_output(kind), f"natedev_only {reason}\nnatedev\teligible\t-\t-\n")

    def test_kind_answers_allow_portable_judgments_and_keep_artifacts_local(self) -> None:
        for kind in ("check", "doc", "lint", "test"):
            with self.subTest(kind=kind):
                self.assertEqual(self.kind_answer(kind, "portable"), "mac_allowed\n")
        for kind in ("build", "example"):
            with self.subTest(kind=kind):
                self.assertEqual(self.kind_answer(kind), "natedev_only makes natedev artifacts\n")
        self.assertEqual(self.kind_answer("final"), "natedev_only full gate runs on natedev\n")

    def test_workspace_linux_only_member_refuses_workspace_verbs_but_not_other_package_tests(self) -> None:
        _ = (self.root / "Cargo.toml").write_text('[workspace]\nmembers=["crates/*"]\n')
        for kind in ("check", "doc", "lint"):
            with self.subTest(kind=kind):
                self.assertEqual(self.kind_answer(kind, "portable"), "natedev_only linux_only\n")
        self.assertEqual(self.kind_answer("test", "portable"), "mac_allowed\n")
        self.assertEqual(self.kind_answer("test", "linux-part"), "natedev_only linux_only\n")

    def test_off_backoff_and_disk_floor_refuse_mac_steps(self) -> None:
        self.assertEqual(self.kind_answer("check", free_gib=59), "natedev_only disk\n")
        offload.write_backoff(time.time() + 60)
        self.assertEqual(self.kind_answer("doc"), "natedev_only backoff\n")
        _ = self.settings.write_text("offload=off\nlinux_only.hana=\n")
        self.assertEqual(self.kind_answer("lint"), "natedev_only off\n")

    def test_unreadable_workspace_cannot_be_sent_to_the_mac(self) -> None:
        _ = (self.root / "Cargo.toml").write_text("[")
        self.assertEqual(self.kind_answer("doc"), "natedev_only repo\n")


class WaitLocalTests(IsolatedBuildTargetTest):
    sleeps: int = 0

    @override
    def setUp(self) -> None:
        super().setUp()
        self.sleeps = 0
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "BUILD_HOLD_RELEASE_DIR": str(self.root / "release"),
            "UNIT_ACTIVE_DIR": str(self.root / "active"),
            "UNIT_RUN_DIR": "", "UNIT_SESSION_DIR": "",
            "BUILD_TARGETS_POLL_S": "0.01",
            "BUILDLOG_HOLD_WAIT_FILE": str(self.root / "wait-seconds"),
            "BUILDLOG_HOLD_WAITER_FILE": str(self.root / "waiter"),
            "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
        })))
        _ = self.enterContext(mock.patch.object(build_hold, "process_start_time", return_value="test-start"))
        _ = self.enterContext(mock.patch.object(build_hold, "socket_for", return_value=build_hold.RecipientLive("/test/socket")))
        _ = self.enterContext(mock.patch.object(build_hold, "send_release", return_value=build_hold.ReleaseDelivered()))
        _ = self.enterContext(mock.patch.object(build_targets, "wait_clock", return_value=NOW))

    def begin_hold(self) -> None:
        _ = build_hold.start_hold(self.holders, build_hold.HolderIdentity("hana", "hana"), "render checks", build_hold.NoReleaseEta(), NOW)

    def release_hold(self, _seconds: float) -> None:
        self.sleeps += 1
        _ = build_hold.release_cycle(self.holders, "hana")

    def wait(self) -> tuple[int, str, str]:
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = build_targets.command_wait_local("call-a", pid=12345)
        return code, output.getvalue(), errors.getvalue()

    def test_damaged_cycle_pointer_waits_for_unlink_without_reading_user_zone(self) -> None:
        release = self.root / "release"
        release.mkdir()
        _ = (release / "current").write_text("damaged")
        for zone in ("America/New_York", "NoSuchZone"):
            with self.subTest(zone=zone), mock.patch.dict(os.environ, {"TZ": "UTC", "USER_ZONE": zone}):
                self.write_holder()
                events: list[str] = []

                def remove_holder(_seconds: float) -> None:
                    self.assertTrue((self.holders / "hana").exists())
                    events.append("wait")
                    (self.holders / "hana").unlink()
                    events.append("unlink")

                with mock.patch.object(build_targets, "wait_sleep", side_effect=remove_holder):
                    code, output, errors = self.wait()
                events.append("return")
                self.assertEqual(code, 0, output + errors)
                self.assertEqual(events, ["wait", "unlink", "return"])
                with mock.patch.object(build_targets, "wait_sleep", side_effect=AssertionError("No holder may delay")):
                    self.assertEqual(self.wait(), (0, "", ""))
                self.assertEqual((release / "current").read_text(), "damaged")



    def test_hold_and_release_write_temp_log_without_writing_real_log(self) -> None:
        def record_now(command: list[str]) -> None:
            self.assertEqual(command[2], "hold-event")
            record.hold_event(command[3:])

        with mock.patch.object(build_hold, "record_hold_event", wraps=REAL_RECORD_HOLD_EVENT), \
             mock.patch.object(build_hold, "start_recorder", side_effect=record_now), \
             mock.patch.object(store, "append_line", wraps=store.append_line) as append:
            self.begin_hold()
            self.release_hold(0)
        destinations = [cast(Path, call.args[0]) for call in append.call_args_list]
        self.assertEqual(len(destinations), 2)
        self.assertTrue(all(path.is_relative_to(self.root / "buildlog") for path in destinations))
        self.assertFalse(any(path.is_relative_to(REAL_BUILDLOG_ROOT) for path in destinations))
        rows = [cast(dict[str, object], json.loads(line))
                for path in set(destinations) for line in path.read_text().splitlines()]
        self.assertEqual([row["event"] for row in rows], ["began", "ended"])

    def test_unheld_call_returns_without_polling_or_creating_cycle(self) -> None:
        with mock.patch.object(build_targets, "wait_sleep", side_effect=AssertionError("No hold needs no poll")):
            code, output, errors = self.wait()
        self.assertEqual((code, output, errors), (0, "", ""))
        self.assertFalse((self.root / "release").exists())

    def test_one_release_frees_every_wait_local_process(self) -> None:
        self.begin_hold()
        processes: list[subprocess.Popen[str]] = []
        with mock.patch.object(subprocess, "run", wraps=REAL_SUBPROCESS_RUN), \
             mock.patch.object(subprocess, "Popen", wraps=REAL_SUBPROCESS_POPEN):
            try:
                for index in range(3):
                    process = subprocess.Popen(
                        [sys.executable, build_targets.__file__, "wait-local", "--call-id", f"call-{index}"],
                        env={**os.environ, "BUILDLOG_HOLD_WAITER_FILE": str(self.root / f"waiter-{index}"),
                             "BUILDLOG_HOLD_WAIT_FILE": "", "BUILD_TARGETS_POLL_S": "3600"},
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    )
                    processes.append(process)
                for process in processes:
                    assert process.stderr is not None
                    self.assertIn("; waiting", cast(str, process.stderr.readline()))
                    self.assertIsNone(process.poll())
                _ = build_hold.release_cycle(self.holders, "hana")
                for process in processes:
                    output, errors = process.communicate(timeout=10)
                    self.assertEqual(process.returncode, 0, output + errors)
                cycle = build_hold.read_cycle()
                assert cycle is not None
                self.assertEqual(cycle["entries"], [])
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                    _ = process.communicate(timeout=10)


    def test_unheld_call_skips_owner_and_admission_lookup(self) -> None:
        with mock.patch.object(build_hold, "owning_session", side_effect=AssertionError("Owner lookup not needed")):
            self.assertEqual(self.wait(), (0, "", ""))



    def test_held_call_names_holder_and_runs_after_its_recorded_release(self) -> None:
        self.begin_hold()
        with mock.patch.object(build_targets, "wait_sleep", side_effect=self.release_hold):
            code, output, errors = self.wait()
        self.assertEqual(code, 0, output + errors)
        self.assertIn("verify.sh: natedev is held by hana for render checks; waiting (tests that can run on the Mac keep running there).", output + errors)
        self.assertEqual(self.sleeps, 1)
        self.assertFalse((self.holders / "hana").exists())

    def leave_wait(self, leave_hook: str) -> tuple[int, str]:
        errors = io.StringIO()
        with mock.patch.object(subprocess, "run", wraps=REAL_SUBPROCESS_RUN), \
             mock.patch.object(subprocess, "Popen", wraps=REAL_SUBPROCESS_POPEN), \
             redirect_stdout(io.StringIO()), redirect_stderr(errors):
            code = build_targets.command_wait_local("call-a", pid=os.getpid(), leave_hook=leave_hook)
        return code, errors.getvalue()

    def count_sleep(self, _seconds: float) -> None:
        self.sleeps += 1

    def test_held_call_leaves_for_a_free_mac_after_one_poll_and_keeps_the_hold(self) -> None:
        self.begin_hold()
        with mock.patch.object(build_targets, "wait_sleep", side_effect=self.count_sleep):
            code, errors = self.leave_wait("exit 0")
        self.assertEqual(code, build_targets.LEFT_QUEUE_STATUS, errors)
        self.assertEqual(self.sleeps, 1)
        self.assertIn("verify.sh: the Mac is free: leaving natedev's hold wait to run there.", errors)
        cycle = build_hold.read_cycle()
        assert cycle is not None
        self.assertEqual(cycle["entries"], [])
        self.assertTrue((self.root / "wait-seconds").read_text().strip())

    def test_held_call_with_a_busy_or_blocked_mac_waits_for_natedev_release(self) -> None:
        self.begin_hold()
        with mock.patch.object(build_targets, "wait_sleep", side_effect=self.release_hold):
            code, errors = self.leave_wait("exit 1")
        self.assertEqual(code, 0, errors)
        self.assertNotIn("the Mac is free", errors)
        self.assertFalse((self.holders / "hana").exists())

    def test_held_call_uses_its_marker_hook_when_no_hook_argument_was_given(self) -> None:
        self.begin_hold()
        with mock.patch.dict(os.environ, {"BUILDLOG_MAC_IDLE_HOOK": "exit 0"}), \
             mock.patch.object(build_targets, "wait_sleep", side_effect=self.count_sleep):
            code, errors = self.leave_wait("")
        self.assertEqual(code, build_targets.LEFT_QUEUE_STATUS, errors)
        self.assertEqual(self.sleeps, 1)
        self.assertTrue((self.holders / "hana").exists())

    def test_unheld_call_never_runs_the_leave_hook(self) -> None:
        marker = self.root / "hook-ran"
        self.assertEqual(self.leave_wait(f"touch {marker}"), (0, ""))
        self.assertFalse(marker.exists())

    def test_waiting_line_collapses_holder_and_reason_whitespace(self) -> None:
        self.begin_hold()
        _ = (self.holders / "hana").write_text(json.dumps({
            "holder": " hana\nteam ", "for": " render\nchecks ",
            "since": SINCE.isoformat(), "release_eta": "unknown",
        }))
        with mock.patch.object(build_targets, "wait_sleep", side_effect=self.release_hold):
            code, output, errors = self.wait()
        self.assertEqual(code, 0)
        self.assertIn("held by hana team for render checks; waiting", output + errors)


    def test_missing_cycle_waits_until_the_hold_itself_clears(self) -> None:
        self.write_holder()

        def clear(_seconds: float) -> None:
            self.sleeps += 1
            (self.holders / "hana").unlink()

        with mock.patch.object(build_targets, "wait_sleep", side_effect=clear):
            self.assertEqual(self.wait()[0], 0)
        self.assertEqual(self.sleeps, 1)

    def test_changed_cycle_waits_until_the_holder_releases(self) -> None:
        self.begin_hold()

        def change_then_release(seconds: float) -> None:
            if self.sleeps == 0:
                self.sleeps += 1
                _ = build_hold.open_cycle(NOW)
            else:
                self.release_hold(seconds)

        with mock.patch.object(build_targets, "wait_sleep", side_effect=change_then_release):
            self.assertEqual(self.wait()[0], 0)
        self.assertEqual(self.sleeps, 2)
        self.assertFalse((self.holders / "hana").exists())

    def test_damaged_cycle_waits_for_unlink_then_passes_without_a_holder(self) -> None:
        self.begin_hold()
        cycle = build_hold.read_cycle()
        assert cycle is not None
        path = self.root / "release" / cycle["id"] / "cycle.json"
        _ = path.write_text("{broken")

        events: list[str] = []

        def release_holder(_seconds: float) -> None:
            self.assertTrue((self.holders / "hana").exists())
            events.append("wait")
            (self.holders / "hana").unlink()
            events.append("unlink")

        with mock.patch.object(build_targets, "wait_sleep", side_effect=release_holder):
            code, output, errors = self.wait()
        events.append("return")
        self.assertEqual(code, 0, output + errors)
        self.assertEqual(events, ["wait", "unlink", "return"])
        with mock.patch.object(build_targets, "wait_sleep", side_effect=AssertionError("No holder may delay")):
            self.assertEqual(self.wait(), (0, "", ""))
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
                self.release_hold(seconds)

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
        self.assertEqual(cycle["entries"], [])

    def check_launch(self, session_id: str) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": str(self.root), "CLAUDE_CODE_SESSION_ID": session_id}), \
             redirect_stdout(output), redirect_stderr(errors):
            code = build_targets.command_check_local()
        return code, output.getvalue(), errors.getvalue()

    def test_holder_session_launch_is_admitted_without_changing_hold_or_release(self) -> None:
        _ = build_hold.start_hold(self.holders, build_hold.HolderIdentity("holder-session", "hana"),
                                  "render checks", build_hold.NoReleaseEta(), NOW)
        cycle = build_hold.read_cycle()
        assert cycle is not None
        cycle_file = self.root / "release" / cycle["id"] / "cycle.json"
        current = self.root / "release" / "current"
        holder_file = build_hold.holder_path(self.holders, "holder-session")
        before = (cycle_file.read_bytes(), current.read_bytes(), holder_file.read_bytes())
        with mock.patch.object(build_hold, "register_wait", wraps=build_hold.register_wait) as register:
            self.assertEqual(self.check_launch("holder-session"), (0, "", ""))
        register.assert_not_called()
        self.assertEqual((cycle_file.read_bytes(), current.read_bytes(), holder_file.read_bytes()), before)
        self.assertEqual([path.name for path in self.holders.iterdir()], ["holder-session"])

    def test_another_session_launch_is_refused_by_the_session_hold(self) -> None:
        _ = build_hold.start_hold(self.holders, build_hold.HolderIdentity("holder-session", "hana"),
                                  "render checks", build_hold.NoReleaseEta(), NOW)
        self.assertEqual(self.check_launch("other-session"), (
            10, "natedev is held by hana for render checks; launch after the release.\n", "",
        ))

    def test_holder_session_launch_is_refused_when_another_hold_is_in_force(self) -> None:
        _ = build_hold.start_hold(self.holders, build_hold.HolderIdentity("holder-session", "hana"),
                                  "render checks", build_hold.NoReleaseEta(), NOW)
        _ = build_hold.start_hold(self.holders, build_hold.HolderIdentity("other-session", "research"),
                                  "timing", build_hold.NoReleaseEta(), NOW + timedelta(seconds=1))
        self.assertEqual(self.check_launch("holder-session"), (
            10, "natedev is held by research for timing; launch after the release.\n", "",
        ))

    def test_wait_and_launch_lines_name_the_oldest_hold_the_caller_does_not_own(self) -> None:
        cases = ((("caller", "peer", "later"), "caller"), (("peer",), "caller"),
                 (("caller",), "caller"), (("peer", "later"), ""))
        for sessions, owner in cases:
            with self.subTest(sessions=sessions, owner=owner):
                for offset, session in enumerate(sessions):
                    _ = build_hold.start_hold(
                        self.holders, build_hold.HolderIdentity(session, session),
                        f"{session} checks", build_hold.NoReleaseEta(), NOW + timedelta(seconds=offset))
                others = tuple(session for session in sessions if session != owner)
                try:
                    with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": owner}):
                        for caller in ("", "verify", "lint"):
                            with self.subTest(command="check-local", caller=caller):
                                output, errors = io.StringIO(), io.StringIO()
                                with redirect_stdout(output), redirect_stderr(errors):
                                    status = build_targets.command_check_local(caller)
                                if others:
                                    line = "natedev is held by peer for peer checks"
                                    expected = (f"{line}; launch after the release.\n" if not caller else
                                                f"{'verify.sh' if caller == 'verify' else 'lint'}: {line}.\n")
                                    self.assertEqual((status, output.getvalue(), errors.getvalue()),
                                                     (10, expected, "") if not caller else (10, "", expected))
                                else:
                                    self.assertEqual((status, output.getvalue(), errors.getvalue()), (0, "", ""))

                        def other_releases(_seconds: float) -> None:
                            self.sleeps += 1
                            for session in others:
                                build_hold.holder_path(self.holders, session).unlink()

                        with self.subTest(command="wait-local"), \
                             mock.patch.object(build_targets, "wait_sleep", side_effect=other_releases):
                            result = self.wait()
                            self.assertEqual(result, (
                                0, "", "verify.sh: natedev is held by peer for peer checks; waiting "
                                + "(tests that can run on the Mac keep running there).\n",
                            ) if others else (0, "", ""))
                            self.assertEqual(self.sleeps, int(bool(others)))
                finally:
                    for session in sessions:
                        build_hold.holder_path(self.holders, session).unlink(missing_ok=True)
                    self.sleeps = 0

    def test_hold_under_a_name_refuses_every_launching_session(self) -> None:
        self.begin_hold()
        for session_id in ("holder-session", "other-session", ""):
            with self.subTest(session_id=session_id):
                self.assertEqual(self.check_launch(session_id), (
                    10, "natedev is held by hana for render checks; launch after the release.\n", "",
                ))

    def verify_calls(self, session_id: str) -> tuple[int, int, int]:
        """register-local, check-local and wait-local, in verify.sh's order, as `session_id`."""
        with mock.patch.dict(os.environ, {"HOME": str(self.root), "CLAUDE_CODE_SESSION_ID": session_id}), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return (build_targets.command_register_local("call-a", pid=12345),
                    build_targets.command_check_local("verify"),
                    build_targets.command_wait_local("call-a", pid=12345))

    def test_holder_session_verify_passes_its_own_hold_without_queueing(self) -> None:
        _ = build_hold.start_hold(self.holders, build_hold.HolderIdentity("holder-session", "hana"),
                                  "render checks", build_hold.NoReleaseEta(), NOW)
        with mock.patch.object(build_targets, "wait_sleep", side_effect=AssertionError("The holder must not wait")), \
             mock.patch.object(build_hold, "register_wait", wraps=build_hold.register_wait) as register:
            self.assertEqual(self.verify_calls("holder-session"), (0, 0, 0))
        register.assert_not_called()
        self.assertFalse((self.root / "registration").exists())
        self.assertEqual([path.name for path in self.holders.iterdir()], ["holder-session"])

    def test_holder_session_verify_waits_while_another_hold_is_in_force(self) -> None:
        _ = build_hold.start_hold(self.holders, build_hold.HolderIdentity("holder-session", "hana"),
                                  "render checks", build_hold.NoReleaseEta(), NOW)
        _ = build_hold.start_hold(self.holders, build_hold.HolderIdentity("other-session", "research"),
                                  "timing", build_hold.NoReleaseEta(), NOW + timedelta(seconds=1))

        def other_releases(_seconds: float) -> None:
            self.sleeps += 1
            build_hold.holder_path(self.holders, "other-session").unlink()

        with mock.patch.object(build_targets, "wait_sleep", side_effect=other_releases):
            self.assertEqual(self.verify_calls("holder-session"), (10, 10, 0))
        self.assertEqual(self.sleeps, 1)
        self.assertFalse((self.root / "registration").exists())

    def test_register_local_is_nonblocking_repeatable_and_uses_given_pid(self) -> None:
        self.begin_hold()
        with mock.patch.object(build_targets, "wait_sleep", side_effect=AssertionError("Registration must return")):
            for _ in range(2):
                self.assertEqual(build_targets.command_register_local("call-a", pid=12345), 10)
        cycle = build_hold.read_cycle()
        assert cycle is not None
        self.assertEqual(cycle["entries"], [])
        self.assertFalse((self.root / "registration").exists())

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




if __name__ == "__main__":
    _ = unittest.main()
