"""Check status lines and optional probes without contacting another host."""

from __future__ import annotations

import io
import json
import os
import sys
import subprocess
import tempfile
import time
from collections.abc import Generator, Iterable
from contextlib import contextmanager, nullcontext, redirect_stderr, redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast, override

from unittest import mock
import unittest


from scripts.build_hold.owning_session import NoOwningSession, OwningSession
from scripts.lib.py_floor_test_support import interpreter_cache_environment
from scripts.build_targets import build_targets
from scripts.lib import process_start as process_identity

from scripts.build_hold import build_hold
from scripts.mac_test import claim_state, mac_test, offload
from scripts.build_targets import targets
from scripts.buildlog.mac_idle_test_support import NotificationGuardedTestCase, write_idle_alert
from scripts.message import roster
from scripts.message import session_send
from scripts.production import staff
from scripts.production.staff_test_support import fixture_member, fixture_runtime_id, isolated_staff_file
from scripts.buildlog import record, store
from scripts.build_targets.test_targets import END, NOW, SHIPPED_CONFIG, SINCE, IsolatedTargetTest
from scripts.lint.memory_test_support import config_environment, write_config_fixture
from scripts.lib import test_hang_guard

SHIPPED_STATUS = "\n".join((
    "mac: free",
    "natedev: free",
    "",
))

REAL_SUBPROCESS_RUN = subprocess.run
REAL_SUBPROCESS_POPEN = subprocess.Popen
REAL_PROCESS_START_TIME = build_hold.process_start_time
REAL_RECORD_HOLD_EVENT = build_hold.record_hold_event
REAL_BUILDLOG_ROOT = Path.home() / ".local/state/buildlog"
WAITING_SESSION = OwningSession("waiting-session")


@contextmanager
def pacific_zone() -> Generator[None, None, None]:
    try:
        with mock.patch.dict(os.environ, {"TZ": "America/Los_Angeles", "USER_ZONE": "America/Los_Angeles"}):
            time.tzset()
            yield
    finally:
        time.tzset()


def setUpModule() -> None:
    cache_home = Path(unittest.enterModuleContext(tempfile.TemporaryDirectory(prefix="targets-cache-")))
    _ = cast(object, unittest.enterModuleContext(mock.patch.dict(os.environ, interpreter_cache_environment(cache_home))))


class FolderInvocationTests(NotificationGuardedTestCase):
    def test_verify_commands_open_each_config_once(self) -> None:
        code = """import io, json, os, runpy, sys
from contextlib import redirect_stdout

configuration = os.path.abspath(os.environ['CONF_DIR']) + os.sep
opens = {}
def record_open(event, arguments):
    if event == 'open' and isinstance(arguments[0], (str, bytes)):
        path = os.path.abspath(os.fsdecode(arguments[0]))
        if path.startswith(configuration):
            name = path[len(configuration):]
            opens[name] = opens.get(name, 0) + 1

sys.addaudithook(record_open)
sys.argv = sys.argv[1:]
with redirect_stdout(io.StringIO()):
    try:
        runpy.run_path(sys.argv[0], run_name='__main__')
    except SystemExit as error:
        if error.code not in (None, 0):
            raise
print(json.dumps({'opens': opens, 'modules': sorted(sys.modules)}))
"""
        entry = Path(__file__).with_name("build_targets.py").resolve()
        cases = (
            ("eligible", "Mac state absent", "mac", False,
             ("scripts.build_targets.targets",)),
            ("eligible", "Mac state present", "mac", True,
             ("scripts.build_targets.targets", "scripts.lint.sweep", "scripts.copies.copies")),
            ("release-wait", "Mac state absent", "mac", False,
             ("scripts.build_targets.targets", "scripts.mac_test.mac_test", "scripts.mac_test.offload",
              "scripts.copies.copies")),
            ("release-wait", "Mac state present", "mac", True,
             ("scripts.build_targets.targets", "scripts.copies.copies", "scripts.mac_test.mac_test")),
        )
        for command, branch, target, present, omitted_modules in cases:
            with self.subTest(command=command, branch=branch):
                directory = self.enterContext(tempfile.TemporaryDirectory())
                root = Path(directory)
                configuration = root / "config"
                environment = {key: value for key, value in os.environ.items()
                               if key not in ("PYTHONPATH", "BUILD_TARGETS_CONFIG", "MAC_TEST_CONFIG")}
                environment.update({**config_environment(configuration), "HOME": directory,
                                    "BUILDLOG_DIR": str(root / "buildlog"),
                                    "BUILD_HOLD_DIR": str(root / "holds"),
                                    "BUILD_HOLD_RELEASE_DIR": str(root / "releases"),
                                    "MAC_TEST_STATE_DIR": str(root / "mac-state"),
                                    "MAC_IDLE_STATE_DIR": str(root / "mac-idle"),
                                    "UNIT_RUN_DIR": str(root / "board"), "UNIT_ACTIVE_DIR": str(root / "active"),
                                    "CLAUDE_CODE_SESSION_ID": "",
                                    "NOTIFIER_SESSIONS_DIR": str(root / "sessions")})
                _ = (configuration / "build_targets.conf").write_text(
                    f"targets=natedev,{target}\nkind.natedev=local\nplatform.natedev=linux\n"
                    + f"kind.{target}=ssh\nplatform.{target}={'linux' if target == 'aws' else 'macos'}\n"
                    + f"settings.{target}={'aws_target' if target == 'aws' else 'mac_test'}.conf\n")
                claims = root / "mac-state" / "claims"
                if present:
                    claims.mkdir(parents=True)
                if present and command == "release-wait":
                    started = process_identity.process_start(os.getpid())
                    self.assertIsInstance(started, process_identity.Started)
                    assert isinstance(started, process_identity.Started)
                    owner = {"claim_id": "fixture-claim", "episode": "fixture-episode",
                             "attempt": "fixture-attempt", "submitting_session": "",
                             "pid": os.getpid(), "proc_start": started.start,
                             "worktree": directory, "since": NOW.isoformat()}
                    _ = (claims / "local.json").write_text(json.dumps({**owner, "what": "local reservation"}))
                    _ = (claims / "remote.json").write_text(json.dumps(
                        {**owner, "what": "remote claim", "remote_host": "test-mac"}))
                arguments = (["eligible", "--new-step", "--probe-marks-file", str(root / "probe-marks"),
                              "--kind", "test", "--repo-root", directory, "--package", ""]
                             if command == "eligible" else ["release-wait", "--pid", str(os.getpid()),
                                 "--claim-id", "fixture-claim", "--episode", "fixture-episode",
                                 "--attempt", "fixture-attempt"])
                result = REAL_SUBPROCESS_RUN(
                    [str(entry.parents[1] / "lib/py"), "-c", code, str(entry), *arguments],
                    cwd=entry.parent, env=environment, capture_output=True, text=True, timeout=30, check=False)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                report = cast(dict[str, object], json.loads(result.stdout))
                opens = cast(dict[str, int], report["opens"])
                self.assertEqual(opens.get("build_targets.conf", 0), 1)
                modules = cast(list[str], report["modules"])
                self.assertEqual(set(omitted_modules).intersection(modules), set())
                if present and command == "release-wait":
                    with mock.patch.dict(os.environ, environment):
                        remaining = mac_test.read_claims(mac_test.state_paths("mac"))
                    self.assertEqual([claim.record["what"] for claim in remaining], ["remote claim"])

    def test_clean_target_import_omits_admission_service_and_loads_mac_test_once(self) -> None:
        environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(directory)
        environment.update({**config_environment(root), "HOME": directory, "BUILDLOG_DIR": directory,
                            "MAC_TEST_STATE_DIR": str(root / "mac-state")})
        result = REAL_SUBPROCESS_RUN([str(Path(__file__).resolve().parents[1] / "lib/py"), "-c",
            "import os,sys; from pathlib import Path; from scripts.build_targets import build_targets; "
            + "assert 'scripts.build_queue.admission_service' not in sys.modules; "
            + "build_targets.target_state(build_targets.SshTarget('mac', 'macos', Path(os.environ['CONF_DIR']) / 'mac_test.conf')); "
            + "names=[n for n,m in list(sys.modules.items()) if getattr(m,'__file__','') "
            + "and m.__file__.endswith('/mac_test/mac_test.py')]; "
            + "assert len(names)==1, names"], cwd=Path(__file__).parent, env=environment,
            capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_local_commands_omit_ssh_modules(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(directory)
        environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        environment.update({**config_environment(root), "HOME": directory,
                            "BUILDLOG_DIR": str(root / "buildlog"),
                            "BUILD_HOLD_DIR": str(root / "holds"),
                            "BUILD_HOLD_RELEASE_DIR": str(root / "releases"),
                            "MAC_TEST_STATE_DIR": str(root / "mac-state"),
                            "MAC_IDLE_STATE_DIR": str(root / "mac-idle"),
                            "UNIT_RUN_DIR": str(root / "board"), "UNIT_ACTIVE_DIR": str(root / "active"),
                                    "CLAUDE_CODE_SESSION_ID": "",
                            "NOTIFIER_SESSIONS_DIR": str(root / "sessions")})
        common = ["--repo-root", directory, "--kind", "test"]
        reservation = ["reserve", *common, "--pid", str(os.getpid()),
                       "--reservation-file", str(root / "reservation")]
        cases = (
            ("bare-eligible", False, ["eligible", "--repo-root", directory], 0),
            ("kind-eligible", False, ["eligible", *common], 0),
            ("release-wait", False, ["release-wait", "--pid", str(os.getpid())], 0),
            ("filtered-reserve", True, [*reservation, "--target", "natedev"], 1),
        )
        for name, ssh_target, arguments, status in cases:
            with self.subTest(command=name):
                _ = (root / "build_targets.conf").write_text(
                    f"targets={'natedev,mac' if ssh_target else 'natedev'}\n"
                    + "kind.natedev=local\nplatform.natedev=linux\n"
                    + "kind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n")
                code = (
                    "import sys; from scripts.build_targets import build_targets; "
                    + "sys.argv=['build_targets.py', *sys.argv[1:]]; "
                    + f"assert build_targets.main()=={status}; "
                    + "loaded=[name for name in sys.modules if name in "
                    + "('scripts.mac_test.mac_test', 'scripts.mac_test.offload')]; "
                    + "assert not loaded, loaded")
                result = REAL_SUBPROCESS_RUN(
                    [str(Path(__file__).resolve().parents[1] / "lib/py"), "-c", code, *arguments],
                    cwd=Path(__file__).parent, env=environment,
                    capture_output=True, text=True, timeout=30, check=False)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_folder_invocation_records_hold_events(self) -> None:
        result = subprocess.run(
            [str(Path(__file__).resolve().parents[1] / "lib/py"), "-m", "unittest",
             "scripts.build_targets.test_build_targets.WaitLocalTests.test_hold_and_release_write_temp_log_without_writing_real_log"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True, text=True, check=False, timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class IsolatedBuildTargetTest(NotificationGuardedTestCase, IsolatedTargetTest):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "BUILDLOG_DIR": str(self.root / "buildlog"),
            "MAC_IDLE_STATE_DIR": str(self.root / "mac-idle"),
        })))

    @override
    def write_claim(self, file_name: str = "run.json", what: str = "nextest hana", since: datetime = SINCE) -> None:
        super().write_claim(file_name, what, since)
        path = self.mac_state / file_name
        record = cast(dict[str, object], json.loads(path.read_text()))
        record.update(claim_id=f"claim-{file_name}", episode=f"episode-{file_name}",
                      attempt="attempt-1", submitting_session="")
        _ = path.write_text(json.dumps(record))


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

    def test_mac_idle_alert_prints_first_with_red_only_on_a_terminal(self) -> None:
        _ = write_idle_alert(self.root / "mac-idle", NOW.timestamp())
        config = self.write_config("targets=natedev\nkind.natedev=local\nplatform.natedev=linux\n")
        expected = ("MAC BLOCKED by research since 13:50 (P15 F11 L0 run): 7 steps wait for the Mac, "
                    "idle 10 min; /build:mac unblock clears it.")
        with mock.patch.dict(os.environ, {"CONF_DIR": str(config.parent)}), \
             mock.patch.object(time, "time", return_value=NOW.timestamp()):
            for terminal in (False, True):
                with self.subTest(terminal=terminal):
                    output, errors = io.StringIO(), io.StringIO()
                    with redirect_stdout(output), redirect_stderr(errors), \
                         mock.patch.object(output, "isatty", return_value=terminal):
                        code = build_targets.command_status()
                    first = f"\033[1;31m{expected}\033[0m" if terminal else expected
                    self.assertEqual((code, errors.getvalue()), (0, ""))
                    self.assertEqual(output.getvalue(), first + "\nnatedev: free\n")

    def test_stale_mac_reading_prints_before_target_status(self) -> None:
        _ = write_idle_alert(self.root / "mac-idle", NOW.timestamp(), written_age=301)
        config = self.write_config("targets=natedev\nkind.natedev=local\nplatform.natedev=linux\n")
        with mock.patch.dict(os.environ, {"CONF_DIR": str(config.parent)}), \
             mock.patch.object(time, "time", return_value=NOW.timestamp()):
            code, output, errors = self.capture_status()
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(output.splitlines(), [
            "MAC IDLE WATCH: no reading since 13:54; check buildlog-sample.timer.", "natedev: free",
        ])

    def test_status_fields_remove_control_characters(self) -> None:
        self.assertEqual(build_targets.format_state("ma\x1bc\n", targets.Free()), "mac: free")

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
        with mock.patch.dict(os.environ, {"CONF_DIR": str(Path(config).parent)}), \
             mock.patch.object(process_identity, "process_start", return_value=process_identity.Started("test-run-start")):
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
        with mock.patch.dict(os.environ, {"CONF_DIR": str(Path(config).parent)}), \
             mock.patch.object(process_identity, "process_start", return_value=process_identity.Started("test-run-start")), \
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
        with mock.patch.dict(os.environ, {"CONF_DIR": str(Path(config).parent)}), \
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
        with mock.patch.dict(os.environ, {"CONF_DIR": str(Path(config).parent)}), \
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
        with mock.patch.dict(os.environ, {"CONF_DIR": str(Path(config).parent)}), \
             mock.patch.object(process_identity, "process_start", return_value=process_identity.Started("test-run-start")):
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
        with mock.patch.dict(os.environ, {"CONF_DIR": str(Path(config).parent)}), \
             mock.patch.object(process_identity, "process_start", return_value=process_identity.Started("test-run-start")):
            code, output, errors = self.capture_status()
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(
            output,
            "mac: running nextest hana since 13:05 PDT; "
            + "running nextest cargo-liner since 13:15 PDT, idle since 13:20\nnatedev: free\n",
        )

    def test_mac_running_status_without_idle_time_keeps_existing_line(self) -> None:
        self.write_claim()
        with mock.patch.object(process_identity, "process_start", return_value=process_identity.Started("test-run-start")):
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
        with mock.patch.dict(os.environ, {"CONF_DIR": str(Path(SHIPPED_CONFIG).parent)}):
            with mock.patch.object(build_targets, "probe_target", side_effect=AssertionError("Probe not requested")):
                self.assertEqual(self.capture_status(), (0, SHIPPED_STATUS, ""))

    def test_command_uses_config_order_and_recorded_hold(self) -> None:
        config = self.write_config(
            "targets=natedev,mac\nkind.natedev=local\nplatform.natedev=linux\n"
            + "kind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n"
        )
        self.write_block()
        with mock.patch.dict(os.environ, {"CONF_DIR": str(Path(config).parent)}):
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
        with mock.patch.dict(os.environ, {"CONF_DIR": str(Path(SHIPPED_CONFIG).parent)}):
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
        with mock.patch.dict(os.environ, {"CONF_DIR": str(Path(SHIPPED_CONFIG).parent)}):
            with mock.patch.object(build_targets, "probe_target", side_effect=AssertionError("Probe not requested")):
                with mock.patch.object(sys, "argv", ["build_targets.py", "status"]):
                    with redirect_stdout(output), redirect_stderr(errors):
                        code = build_targets.main()
        self.assertEqual((code, output.getvalue(), errors.getvalue()), (0, SHIPPED_STATUS, ""))

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
        with mock.patch.dict(os.environ, {"CONF_DIR": str(Path(config).parent)}):
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
        config = offload.OffloadConfig(False, offload.SshConnectionSettings("reader-mac", 2.0, 10.0), 3.0, 4.0, 5.0, "6", {})
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
                _ = write_config_fixture(self.settings, settings, template="mac_test")
                if "linux_only.hana=" not in settings:
                    _ = self.settings.write_text("\n".join(line for line in self.settings.read_text().splitlines()
                        if not line.startswith("linux_only.hana=")) + "\n")
                output = io.StringIO()
                with mock.patch.dict(os.environ, {"CONF_DIR": str(Path(config).parent)}), \
                     mock.patch.object(offload, "repository_identity", return_value=offload.KnownRepository("hana")), \
                     mock.patch.object(sys, "argv", ["build_targets.py", "eligible", "--repo-root", str(self.root),
                                                    "--package", "hana", *local]), redirect_stdout(output):
                    self.assertEqual(build_targets.main(), 0)
                disposition = "eligible" if reason == "-" else "excluded"
                self.assertEqual(output.getvalue(), f"natedev\teligible\t-\t-\t-\nmac\t{disposition}\t{reason}\t{self.settings}\tmacos\n")


class StepKindEligibilityTests(IsolatedBuildTargetTest):
    @override
    def setUp(self) -> None:
        super().setUp()
        config = self.write_config(
            "targets=mac,natedev\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n"
            + "kind.natedev=local\nplatform.natedev=linux\n")
        _ = write_config_fixture(self.settings, "offload=on\nlinux_only.hana=linux-part\nfree_floor_gib=60\n", template="mac_test")
        _ = (self.root / "Cargo.toml").write_text('[workspace]\nmembers=["crates/*"]\nexclude=["crates/excluded"]\n')
        for directory, name in (("portable", "portable"), ("excluded", "linux-part")):
            member = self.root / "crates" / directory
            member.mkdir(parents=True)
            _ = (member / "Cargo.toml").write_text(f'[package]\nname="{name}"\nversion="0.1.0"\n')
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"CONF_DIR": str(Path(config).parent)})))
        _ = self.enterContext(mock.patch.object(offload, "repository_identity", return_value=offload.KnownRepository("hana")))

    def kind_output(self, kind: str, package: str = "", free_gib: float = 60) -> str:
        output = io.StringIO()
        with (mock.patch.object(sys, "argv", ["build_targets.py", "eligible", "--repo-root", str(self.root),
                                               "--kind", kind, "--package", package]),
              mock.patch.object(offload, "probe_mac", return_value=offload.ProbeSnapshot(free_gib, "fake-mac")),
              mock.patch.object(offload.ProcessController, "capture", return_value=offload.CommandOutput(0, "", "")),
              redirect_stdout(output)):
            self.assertEqual(build_targets.main(), 0)
        return output.getvalue()

    def kind_answer(self, kind: str, package: str = "", free_gib: float = 60) -> str:
        return self.kind_output(kind, package, free_gib).splitlines()[0] + "\n"

    def floor_answer(self, after: float, result: offload.CapturedCommandResult) -> tuple[str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with (mock.patch.object(offload, "probe_mac", side_effect=[
                  offload.ProbeSnapshot(59, "fake-mac"), offload.ProbeSnapshot(after, "fake-mac")]),
              mock.patch.object(offload.ProcessController, "capture", return_value=result) as sweep,
              redirect_stdout(output), redirect_stderr(errors)):
            self.assertEqual(build_targets.command_kind_eligible(
                self.root, "portable", "test", targets.ANY_ELIGIBLE_TARGET), 0)
        self.assertEqual(sweep.call_count, 1)
        return output.getvalue(), errors.getvalue()

    def test_floor_sweep_admits_the_mac_after_a_disk_exclusion(self) -> None:
        for background in (False, True):
            with self.subTest(background=background):
                if background:
                    with ThreadPoolExecutor(max_workers=1) as worker:
                        output, errors = worker.submit(
                            self.floor_answer, 60, offload.CommandOutput(0, "", "")).result(timeout=30)
                else:
                    output, errors = self.floor_answer(60, offload.CommandOutput(0, "", ""))
                self.assertEqual(output.splitlines()[0], "any_target")
                self.assertIn("mac\teligible\t-", output)
                self.assertEqual(errors, "")

    def test_floor_sweep_shortfall_keeps_the_disk_route(self) -> None:
        output, _ = self.floor_answer(59, offload.CommandOutput(0, "", ""))
        self.assertEqual(output.splitlines()[0], "any_target")
        self.assertIn("mac\texcluded\tdisk", output)

    def test_floor_sweep_failure_keeps_the_disk_route_and_names_the_cause(self) -> None:
        for result, cause in ((offload.CommandOutput(7, "", "disk busy"), "status 7: disk busy"),
                              (offload.COMMAND_TIMED_OUT, "timed out")):
            with self.subTest(cause=cause):
                output, errors = self.floor_answer(200, result)
                self.assertEqual(output.splitlines()[0], "any_target")
                self.assertIn("mac\texcluded\tdisk", output)
                self.assertIn(cause, errors)

    def test_kind_table_keeps_target_order_settings_and_each_mac_decision(self) -> None:
        other_settings = self.settings.with_name("other.conf")
        _ = write_config_fixture(other_settings, "offload=off\nlinux_only.hana=\n", template="mac_test")
        _ = self.write_config(
            "targets=natedev,other,mac\nkind.natedev=local\nplatform.natedev=linux\n"
            + "kind.other=ssh\nplatform.other=macos\nsettings.other=other.conf\n"
            + "kind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n")
        self.assertEqual(self.kind_output("check"),
                         "any_target\nnatedev\teligible\t-\t-\t-\n"
                         + f"other\texcluded\toff\t{other_settings}\tmacos\nmac\teligible\t-\t{self.settings}\tmacos\n")
        self.assertEqual(self.kind_output("build"),
                         "natedev_only makes natedev artifacts\nnatedev\teligible\t-\t-\t-\n"
                         + f"other\texcluded\tmakes natedev artifacts\t{other_settings}\tmacos\n"
                         + f"mac\texcluded\tmakes natedev artifacts\t{self.settings}\tmacos\n")

    def test_local_only_configuration_keeps_artifact_and_gate_reasons(self) -> None:
        _ = self.write_config("targets=natedev\nkind.natedev=local\nplatform.natedev=linux\n")
        for kind, reason in (("build", "makes natedev artifacts"),
                             ("final", "full gate runs on natedev")):
            with self.subTest(kind=kind):
                self.assertEqual(self.kind_output(kind), f"natedev_only {reason}\nnatedev\teligible\t-\t-\t-\n")

    def test_kind_answers_allow_portable_judgments_and_keep_artifacts_local(self) -> None:
        for kind in ("check", "doc", "lint", "test", "example"):
            with self.subTest(kind=kind):
                self.assertEqual(self.kind_answer(kind, "portable"), "any_target\n")
        for kind in ("build",):
            with self.subTest(kind=kind):
                self.assertEqual(self.kind_answer(kind), "natedev_only makes natedev artifacts\n")
        self.assertEqual(self.kind_answer("final"), "natedev_only full gate runs on natedev\n")

    def test_workspace_linux_only_member_refuses_workspace_verbs_but_not_other_package_tests(self) -> None:
        _ = (self.root / "Cargo.toml").write_text('[workspace]\nmembers=["crates/*"]\n')
        for kind in ("check", "doc", "lint"):
            with self.subTest(kind=kind):
                self.assertEqual(self.kind_answer(kind, "portable"), "any_target\n")
        self.assertEqual(self.kind_answer("test", "portable"), "any_target\n")
        self.assertEqual(self.kind_answer("test", "linux-part"), "any_target\n")

    def test_temporary_refusals_keep_mac_retries_and_off_refuses_mac_steps(self) -> None:
        self.assertEqual(self.kind_answer("check", free_gib=59), "any_target\n")
        offload.write_backoff(time.time() + 60)
        self.assertEqual(self.kind_answer("doc"), "any_target\n")
        _ = write_config_fixture(self.settings, "offload=off\nlinux_only.hana=\n", template="mac_test")
        self.assertEqual(self.kind_answer("lint"), "any_target\n")

    def test_temporary_mac_exclusions_keep_the_leave_route_available(self) -> None:
        target = targets.SshTarget("mac", "macos", self.settings)
        for reason in ("busy", "blocked", "disk", "backoff", "unreachable", "state", "copy", "mac_busy",
                       "spending_limit", "spending_unavailable",
                       "state unreadable: temporary read failure"):
            with self.subTest(reason=reason), mock.patch.object(
                build_targets, "ssh_target_eligibility", return_value=targets.ExcludedTarget(target, reason),
            ):
                decision = "any_target"
                self.assertEqual(self.kind_output("test", "portable"),
                                 decision + "\nmac\texcluded\t" + reason + f"\t{self.settings}\tmacos\n"
                                 + "natedev\teligible\t-\t-\t-\n")

    def test_disk_decision_preserves_evidence_in_every_target_order(self) -> None:
        mac = targets.SshTarget("mac", "macos", self.settings)
        linux = targets.SshTarget("linux", "linux", self.settings.with_name("linux.conf"))
        cases = (
            ((mac, "disk"),),
            ((linux, "disk"),),
            ((mac, "disk"), (linux, "off")),
            ((mac, "off"), (linux, "disk")),
            ((mac, "disk"), (linux, "-")),
        )
        for exclusions in cases:
            for ordered in (exclusions, tuple(reversed(exclusions))):
                results = {target.name: targets.EligibleTarget(target) if reason == "-"
                           else targets.ExcludedTarget(target, reason) for target, reason in ordered}
                with (self.subTest(targets=[(target.name, reason) for target, reason in ordered]),
                      mock.patch.object(build_targets, "read_targets", return_value=tuple(target for target, _ in ordered)),
                      mock.patch.object(build_targets, "ssh_target_eligibility", side_effect=list(results.values()))):
                    self.assertEqual(self.kind_answer("test", "portable"), "any_target\n")

    def test_unresolved_probe_excludes_target_as_busy(self) -> None:
        target = targets.SshTarget("mac", "macos", self.settings)
        with mock.patch.object(offload, "probe_mac", return_value=offload.UnresolvedSlotStartup()) as probe:
            result = build_targets.mac_target_eligibility(
                target, self.root, "portable", "test", targets.AnyEligibleTarget(),
            )
        self.assertIsInstance(result, targets.ExcludedTarget)
        assert isinstance(result, targets.ExcludedTarget)
        self.assertEqual(result.target, target)
        self.assertIn("busy", result.reason)
        self.assertNotIn("unreachable", result.reason)
        probe.assert_called_once()

    def test_unreadable_workspace_cannot_be_sent_to_the_mac(self) -> None:
        _ = (self.root / "Cargo.toml").write_text("[")
        self.assertEqual(self.kind_answer("doc"), "any_target\n")

    def test_explicit_session_keeps_the_block_exemption_out_of_the_environment(self) -> None:
        self.write_block()
        target = targets.SshTarget("mac", "macos", self.settings)
        for session in (OwningSession("hana-session"), OwningSession("other-session"),
                        NoOwningSession(), OwningSession("hana-session")):
            with self.subTest(session=session), \
                 mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "service-session"}), \
                 mock.patch.object(offload, "probe_mac", return_value=offload.ProbeSnapshot(100, "fixture-mac")):
                result = build_targets.ssh_target_eligibility(
                    target, self.root, "portable", "test", targets.AnyEligibleTarget(), session=session)
            if session == OwningSession("hana-session"):
                self.assertEqual(result, targets.EligibleTarget(target))
            else:
                self.assertEqual(result, targets.ExcludedTarget(target, "blocked"))


class WaitLocalTests(IsolatedBuildTargetTest):
    sleeps: int = 0

    @override
    def setUp(self) -> None:
        super().setUp()
        self.sleeps = 0
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "BUILD_HOLD_RELEASE_DIR": str(self.root / "release"),
            "STAFF_FILE": str(self.root / "staff.json"),
            "UNIT_RUN_DIR": "", "UNIT_SESSION_DIR": "",
            "PLAN_DELEGATE_BOARD_DIR": "", "PLAN_DELEGATE_SESSION_DIR": "",
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

    def wait(self, *, session: OwningSession | NoOwningSession | claim_state.CallingSession
             = WAITING_SESSION) -> tuple[int, str, str]:
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = build_targets.command_wait_local("call-a", pid=12345, session=session)
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
                        [str(Path(__file__).resolve().parents[1] / "lib/py"), build_targets.__file__, "wait-local", "--call-id", f"call-{index}"],
                        env={**os.environ, "BUILDLOG_HOLD_WAITER_FILE": str(self.root / f"waiter-{index}"),
                             "CLAUDE_CODE_SESSION_ID": "waiting-session",
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
                    output, errors = process.communicate(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
                    self.assertEqual(process.returncode, 0, output + errors)
                cycle = build_hold.read_cycle()
                assert cycle is not None
                self.assertEqual(cycle["entries"], [])
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                    _ = process.communicate(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)


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

        from scripts.message import send as message_send, sessions
        from scripts.lib.socket_test_support import short_socket_directory
        import socket

        for registered in (False, True):
            with self.subTest(staff_holder=registered), isolated_staff_file(self.root / "notice-staff.json", {}), \
                 short_socket_directory() as socket_directory, socket.socket(socket.AF_UNIX) as live_socket:
                _ = (self.root / "notice-staff.json").write_text(json.dumps({"schema": 2, "rows": []}))
                native = fixture_runtime_id("nonstaff-hold-notice")
                key = native
                if registered:
                    member = fixture_member("producer", name="notice-holder")
                    member = staff.update_current_session(member.current_session, staff.StaffId(native))
                    key = member.id
                socket_path = Path(socket_directory) / "notice.sock"
                live_socket.bind(str(socket_path))
                records = Path(os.environ["NOTIFIER_SESSIONS_DIR"])
                records.mkdir(exist_ok=True)
                path = records / "holder.json"
                started = sessions.process_start(os.getpid())
                assert started is not None
                _ = path.write_text(json.dumps({"pid": os.getpid(), "sessionId": native, "name": "holder",
                    "messagingSocketPath": str(socket_path), "updatedAt": 1, "procStart": started}))
                notice_records = sessions.live_sessions()
                self.assertEqual([record["sessionId"] for record in notice_records], [native])
                entry = roster.RosterEntry(machine="fixture", kind="claude", role="freestanding", name="holder",
                    status="working", reach=roster.Messageable("session:" + native), cwd=str(self.root),
                    showrunner=roster.NoShowrunner(), director=roster.NoDirector(), assignment=roster.NotAWorker(),
                    session=roster.ClaudeSession(native))
                resolved: list[message_send.DeliveryRoute] = []

                def lookup_notice(argv: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
                    address = argv[argv.index("--to") + 1]
                    try:
                        route = message_send.resolve_recipient(address, message_send.LocalOnly(),
                            roster.Roster([entry], []), membership=staff.snapshot(), records=notice_records)
                    except message_send.RecipientNameMissing as error:
                        self.fail(str(error))
                    resolved.append(route)
                    self.assertEqual(address, key if registered else "session:" + native)
                    addressed = sessions.addressed("session:" + native, notice_records)
                    assert addressed is not None
                    self.assertEqual(addressed["sessionId"], native)
                    return subprocess.CompletedProcess(argv, 0, "", "")

                holder = build_hold.Holder(name="holder", since=NOW, purpose="notice checks",
                                           release=build_hold.UnknownReleaseEta(), key=key)
                with mock.patch.object(subprocess, "run", side_effect=lookup_notice):
                    build_targets.tell_wait_holder(holder, "The build waits for this hold.", "verify")
                self.assertEqual(resolved, [message_send.ClaudeRecipient("session:" + native)])

    def leave_wait(self, leave_hook: str) -> tuple[int, str]:
        errors = io.StringIO()
        with mock.patch.object(subprocess, "run", wraps=REAL_SUBPROCESS_RUN), \
             mock.patch.object(subprocess, "Popen", wraps=REAL_SUBPROCESS_POPEN), \
             redirect_stdout(io.StringIO()), redirect_stderr(errors):
            code = build_targets.command_wait_local("call-a", pid=os.getpid(), leave_hook=leave_hook,
                                                    session=OwningSession("waiting-session"))
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
        member = staff.StaffRow(staff.StaffId("fixed-holder"), "producer", "renamed-holder", staff.StaffRoot(),
                                current_session=staff.StaffId("holder-session"))
        _ = holder_file.rename(self.holders / member.id)
        with mock.patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((member,))):
            self.assertEqual(self.check_launch("holder-session"), (0, "", ""))
            state = targets.local_hold_state()
            assert isinstance(state, targets.Held)
            self.assertEqual(state.holder, member.name)
            self.assertEqual(state.holder_session, targets.HolderSession(member.current_session))

    def test_another_session_launch_is_refused_by_the_session_hold(self) -> None:
        _ = build_hold.start_hold(self.holders, build_hold.HolderIdentity("holder-session", "hana"),
                                  "render checks", build_hold.NoReleaseEta(), NOW)
        self.assertEqual(self.check_launch("other-session"), (
            10, "natedev is held by hana for render checks; launch after the release.\n", "",
        ))
        proc = self.root / "proc"
        for pid, parent in ((10, 1), (20, 10), (30, 1)):
            directory = proc / str(pid)
            directory.mkdir(parents=True)
            fields = ["S", str(parent), *(["0"] * 17), str(pid * 100)]
            _ = (directory / "stat").write_text(f"{pid} (gate) " + " ".join(fields))
        _ = build_hold.start_hold(self.holders, build_hold.HolderIdentity("holder-session", "hana"),
                                  "render checks", build_hold.NoReleaseEta(), NOW,
                                  keep=(build_hold.ProcessIdentity(10, "1000"),))
        _ = build_hold.start_hold(self.holders, build_hold.HolderIdentity("kept-session", "peer"),
                                  "peer checks", build_hold.NoReleaseEta(), NOW + timedelta(seconds=1),
                                  keep=(build_hold.ProcessIdentity(10, "1000"),))
        for pid, session, owns_hold, admitted in (
            (10, "other-session", False, True),
            (20, "other-session", False, True),
            (30, "other-session", False, False),
            (10, "", False, True),
            (30, "owned-session", True, False),
        ):
            if owns_hold:
                _ = build_hold.start_hold(self.holders, build_hold.HolderIdentity(session, "caller"),
                                          "caller checks", build_hold.NoReleaseEta(), NOW + timedelta(seconds=1))
            with self.subTest(caller_pid=pid, session=session, owns_hold=owns_hold), \
                 mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": session}), \
                 mock.patch.object(build_hold, "PROC_ROOT", proc), \
                 mock.patch.object(build_hold, "process_start_time", REAL_PROCESS_START_TIME), \
                 mock.patch.object(os, "getpid", return_value=pid):
                with mock.patch.object(build_hold, "caller_processes", wraps=build_hold.caller_processes) as lineage:
                    holds = build_hold.natedev_holds(self.holders)
                    self.assertEqual(build_targets.caller_waits_on_hold(holds, self.holders), not admitted)
                    lineage.assert_called_once_with(proc, pid)
                if admitted:
                    with mock.patch.object(build_targets, "wait_sleep", side_effect=AssertionError("Kept runs must not wait")):
                        self.assertEqual(self.verify_calls(session), (0, 0, 0))
                    self.assertEqual(self.check_launch(session), (0, "", ""))
                else:
                    self.assertEqual(self.check_launch(session), (
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
                             mock.patch.object(build_targets, "wait_sleep", side_effect=other_releases), \
                             mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as send:
                            result = self.wait(session=claim_state.CALLING_SESSION)
                            self.assertEqual(result, (
                                0, "", "verify.sh: natedev is held by peer for peer checks; waiting "
                                + "(tests that can run on the Mac keep running there).\n"
                                + ("verify.sh: build has no session; waits on peer hold for peer checks.\n" if not owner else ""),
                            ) if others else (0, "", ""))
                            self.assertEqual(self.sleeps, int(bool(others)))
                            if others and not owner:
                                send.assert_called_once_with(session_send.send_argv(
                                    "--to", "session:peer", "--text",
                                    "verify.sh: build has no session; waits on peer hold for peer checks."),
                                    capture_output=True, text=True, errors="replace", timeout=30)
                            else:
                                send.assert_not_called()
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
        build_hold.holder_path(self.holders, "holder-session").unlink()

        cases = (
            ("missing-run", True, False, "stale-session", 0, "", "", 30),
            ("resolved-run", True, True, "stale-session", 0, "", "", 30),
            ("no-session", False, False, "", 0, "", "", 30),
            ("refused-send", True, False, "stale-session", 2, "REFUSED: session cannot take messages", "", 30),
            ("send-timeout", True, False, "stale-session", 2, "", "import time; time.sleep(1)", 0.05),
            ("invalid-bytes", True, False, "stale-session", 2, "\ufffd", "import os; os.write(2, bytes([255]))", 30),
            ("unreadable-timeout", True, False, "stale-session", 2, "", "", 30),
        )
        for label, named, resolved, shell_session, send_status, send_error, program, timeout in cases:
            with self.subTest(case=label), isolated_staff_file(self.root / "staff.json", {}):
                self.sleeps = 0
                run = self.root / label
                configuration = self.root / ".claude/config"
                _ = write_config_fixture(configuration / "build_targets.conf",
                                         f"# range: 0.01..300\nhold_sender_timeout_seconds={timeout}\n")
                if label == "unreadable-timeout":
                    _ = (configuration / "build_targets.conf").write_text(
                        "targets=natedev\nkind.natedev=local\nplatform.natedev=linux\n")
                sender = [sys.executable, "-c", (program or f"import sys; sys.stderr.write({send_error!r})")
                          + f"; raise SystemExit({send_status})"]
                sender_arguments = mock.Mock(return_value=sender)
                if resolved:
                    row = fixture_member("showrunner", name="run-owner", runtime_id=fixture_runtime_id("run-owner"))
                    _ = staff.set_run(row.id, staff.OpenRun(run))
                self.begin_hold()

                def release_after_two_polls(_seconds: float) -> None:
                    self.sleeps += 1
                    if self.sleeps == 2:
                        build_hold.holder_path(self.holders, "hana").unlink()

                with mock.patch.dict(os.environ, {
                    "UNIT_RUN_DIR": str(run) if named else "",
                    "CLAUDE_CODE_SESSION_ID": shell_session,
                    "CONF_DIR": str(configuration),
                }), mock.patch.object(build_targets, "wait_sleep", side_effect=release_after_two_polls), \
                     (mock.patch.object(session_send, "send_argv", sender_arguments)
                      if program or label == "unreadable-timeout" else nullcontext()), \
                     mock.patch.object(subprocess, "Popen", REAL_SUBPROCESS_POPEN), \
                     mock.patch.object(subprocess, "run", side_effect=REAL_SUBPROCESS_RUN if program else None,
                                       return_value=subprocess.CompletedProcess([], send_status, "", send_error)) as send:
                    code, output, errors = self.wait(session=claim_state.CALLING_SESSION)
                self.assertEqual((code, output, self.sleeps), (0, "", 2))
                announcement = ("verify.sh: natedev is held by hana for render checks; waiting "
                                + "(tests that can run on the Mac keep running there).\n")
                if resolved:
                    self.assertEqual(errors, announcement)
                    send.assert_not_called()
                else:
                    missing = f"no run pointer ({run})" if named else "no session"
                    line = f"verify.sh: build has {missing}; waits on hana hold for render checks."
                    failure = (f"verify.sh: could not tell hana: stderr: {send_error}\n" if send_status else "")
                    if label == "send-timeout":
                        failure = f"verify.sh: could not tell hana: send timed out after {timeout:g} seconds\n"
                    if label == "unreadable-timeout":
                        self.assertIn(announcement + line + "\nverify.sh: could not tell hana: ", errors)
                        self.assertIn("hold_sender_timeout_seconds", errors)
                        sender_arguments.assert_not_called()
                    else:
                        self.assertEqual(errors, announcement + line + "\n" + failure)
                    self.assertEqual(errors.count(line), 1)
                    if label != "unreadable-timeout":
                        send.assert_called_once_with(sender if program else session_send.send_argv(
                            "--to", "session:hana", "--text", line),
                                                     capture_output=True, text=True, errors="replace", timeout=timeout)


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



class RecordedStepKindTests(unittest.TestCase):
    def test_queue_step_names_map_to_offload_kinds(self) -> None:
        cases = (("nextest", "test"), ("clippy", "lint"), ("mend", "lint"), ("check", "check"), ("doc", "doc"),
                 ("build", "build"))
        for step, kind in cases:
            with self.subTest(step=step):
                self.assertEqual(build_targets.recorded_step_kind(step), kind)
        for step in ("fmt", "taplo", "sweep", "test", "mend-check"):
            with self.subTest(step=step):
                self.assertEqual(build_targets.recorded_step_kind(step), build_targets.KindNotOffloaded())

    def test_step_policy_passes_the_snapshot_backoff_to_each_remote_kind(self) -> None:
        target = targets.SshTarget("mac", "macos", Path("/fixture/mac.conf"))
        unreadable = offload.UnreadableBackoff("bad")
        config = offload.OffloadConfig(True, offload.SshConnectionSettings("dummy", 1, 1), 1, 1, 1, "1", {})
        with mock.patch.object(offload, "read_config", return_value=config), \
             mock.patch.object(offload, "mac_step_eligibility", return_value=offload.KnownRepository("hana")) as eligibility:
            result = build_targets.step_policy(target, Path("/fixture/hana"), "hana", "final", backoff=unreadable)
        self.assertEqual(result, offload.KnownRepository("hana"))
        self.assertEqual([call.kwargs["kind"] for call in eligibility.call_args_list], ["check", "test"])
        self.assertTrue(all(call.kwargs["backoff"] is unreadable for call in eligibility.call_args_list))
        with mock.patch.object(offload, "read_config", return_value=config), \
             mock.patch.object(offload, "mac_step_eligibility", return_value=offload.IneligibleMacStep("state")):
            self.assertEqual(build_targets.step_policy(target, Path("/fixture/hana"), "hana", "check", backoff=unreadable),
                             targets.ExcludedTarget(target, "state"))

if __name__ == "__main__":
    _ = unittest.main()
