"""Check independent coordination records for configured SSH targets."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone, tzinfo
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from typing import TYPE_CHECKING, Self, TypedDict, cast, override

from scripts.build_hold.owning_session import NoOwningSession
from scripts.lib import process_start as process_identity

import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]

if TYPE_CHECKING:
    from scripts.mac_test import mac_test, offload
    from scripts.build_targets import targets
    from scripts.build_hold import build_hold
    from scripts.buildlog import record, store
    from scripts.lib import conf
    from scripts.message import sessions
    from scripts.lint.memory_test_support import write_config_fixture, config_environment
else:
    from scripts.mac_test import mac_test, offload
    from scripts.build_targets import targets
    from scripts.build_hold import build_hold
    from scripts.buildlog import record
    from scripts.buildlog import store
    from scripts.lib import conf
    from scripts.message import sessions
    from scripts.lint.memory_test_support import write_config_fixture, config_environment

NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
TARGET_NAMES = ("mac", "aws", "linux-two")


class HoldClock(datetime):
    @classmethod
    @override
    def now(cls, tz: tzinfo | None = None) -> Self:
        return cls.fromtimestamp(NOW.timestamp(), tz)


class HoldEvent(TypedDict):
    target: str
    event: str
    holder: str
    reason: str


class HoldNotification(TypedDict):
    recipient: str
    summary: str
    key: str
    repeat_minutes: float
    text: str


class IsolatedTargetState(unittest.TestCase):
    """Use temporary records and synchronous command boundaries."""

    root: Path = Path()
    config: Path = Path()
    mac_state: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = self.root / "build_targets.conf"
        self.mac_state = self.root / "legacy-mac"
        lines = ["targets=mac,aws,linux-two,natedev"]
        for name, hours in zip(TARGET_NAMES, (4, 1, 2), strict=True):
            settings = self.root / f"{name}.conf"
            _ = write_config_fixture(settings,
                f"host=fake-{name}\noffload=on\nci_repo=\nblock_hours={hours}\n"
                + "block_max_hours=48\nblock_warn_minutes=15\n"
                + "gh_timeout_s=1\nclaim_idle_minutes=15\nclaim_poll_seconds=60\n"
                + "connect_timeout_s=1\nprobe_timeout_s=1\nunreachable_backoff_s=30\n",
                template="mac_test",
            )
            platform = "macos" if name == "mac" else "linux"
            lines.extend((f"kind.{name}=ssh", f"platform.{name}={platform}",
                          f"settings.{name}={settings.name}"))
        lines.extend(("kind.natedev=local", "platform.natedev=linux"))
        _ = self.config.write_text("\n".join(lines) + "\n", encoding="utf-8")
        _ = write_config_fixture(self.root / "mac_test.conf", "ci_repo=", template="mac_test")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            **config_environment(self.root),
            "HOME": str(self.root),
            "BUILD_TARGETS_CONFIG": str(self.config),
            "MAC_TEST_CONFIG": str(self.root / "mac.conf"),
            "MAC_TEST_STATE_DIR": str(self.mac_state),
            "BUILD_HOLD_DIR": str(self.root / "holders"),
            "BUILD_HOLD_RELEASE_DIR": str(self.root / "release"),
            "BUILDLOG_DIR": str(self.root / "buildlog"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "STAFF_FILE": str(self.root / "staff.json"),
            "CLAUDE_CODE_SESSION_ID": "",
        })))
        _ = self.enterContext(mock.patch.object(Path, "home", return_value=self.root))
        _ = self.enterContext(mock.patch.object(mac_test, "utc_now", return_value=NOW))
        _ = self.enterContext(mock.patch.object(targets, "utc_now", return_value=NOW))
        _ = self.enterContext(mock.patch.object(time, "time", return_value=NOW.timestamp()))
        _ = self.enterContext(mock.patch.object(time, "monotonic", return_value=100.0))
        _ = self.enterContext(mock.patch.object(build_hold, "datetime", HoldClock))
        _ = self.enterContext(mock.patch.object(process_identity, "process_start", return_value=process_identity.Started("fake-start")))
        _ = self.enterContext(mock.patch.object(offload, "repository_identity",
                                              return_value=offload.KnownRepository("project")))
        _ = self.enterContext(mock.patch.object(build_hold, "owning_session", return_value=NoOwningSession()))
        _ = self.enterContext(mock.patch.object(sessions, "live_sessions", return_value=[]))
        _ = self.enterContext(mock.patch.object(mac_test, "start_watcher"))
        _ = self.enterContext(mock.patch.object(mac_test, "start_claim_watcher"))
        _ = self.enterContext(mock.patch.object(mac_test, "send_message", return_value=True))
        _ = self.enterContext(mock.patch.object(threading.Thread, "start"))
        _ = self.enterContext(mock.patch.object(subprocess, "run", side_effect=AssertionError("Unexpected external command")))
        _ = self.enterContext(mock.patch.object(subprocess, "Popen", side_effect=self.record_command))
        _ = self.enterContext(mock.patch.object(store, "host_name", return_value="fixture-host"))

    def use_generic_linux_target(self) -> None:
        _ = self.config.write_text(self.config.read_text().replace("aws", "linux-one"), encoding="utf-8")
        _ = (self.root / "aws.conf").rename(self.root / "linux-one.conf")

    def record_command(self, arguments: list[str], **_options: object) -> mock.Mock:
        """Write hold events synchronously and refuse all other child commands."""
        self.assertEqual(arguments[:3], [str(SCRIPTS / "lib/py"), str(SCRIPTS / "buildlog/record.py"), "hold-event"])
        record.hold_event(arguments[3:])
        return mock.Mock(wait=mock.Mock(return_value=0))

    def directory(self, target: str) -> Path:
        return self.mac_state if target == "mac" else self.root / ".local/state/build-targets" / target

    def selected_target(self, name: str) -> targets.Target:
        return next(target for target in targets.read_targets() if target.name == name)

    def invoke_mac(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", [str(SCRIPTS / "mac_test/mac_test.py"), *arguments]), \
                redirect_stdout(output), redirect_stderr(errors):
            try:
                status = mac_test.main()
            except SystemExit as error:
                status = error.code if isinstance(error.code, int) else 1
        return subprocess.CompletedProcess(arguments, status, output.getvalue(), errors.getvalue())

    def claim(self, target: str, pid: int = 101) -> subprocess.CompletedProcess[str]:
        return self.invoke_mac(["claim", "--target", target, "--pid", str(pid),
                                "--what", f"test {target}", "--worktree", str(self.root / "project")])

    def release(self, target: str = "", pid: int = 101) -> subprocess.CompletedProcess[str]:
        paths = mac_test.state_paths(target or "mac")
        record = next(claim.record for claim in mac_test.read_claims(paths) if claim.record["pid"] == pid)
        selection = ["--target", target] if target else []
        return self.invoke_mac(["release", *selection, "--pid", str(pid), "--claim-id", record["claim_id"],
                                "--episode", record["episode"], "--attempt", record["attempt"]])

    def block(self, target: str, holder: str = "alice") -> subprocess.CompletedProcess[str]:
        return self.invoke_mac(["block", "--target", target, "--holder", holder, "--for", f"inspect {target}"])

    def snapshot(self) -> dict[str, bytes]:
        return {str(path.relative_to(self.root)) + ("/" if path.is_dir() else ""):
                b"" if path.is_dir() else path.read_bytes() for path in self.root.rglob("*")}

    def events(self) -> list[HoldEvent]:
        return [cast(HoldEvent, json.loads(line)) for path in (self.root / "buildlog").rglob("*.jsonl")
                for line in path.read_text(encoding="utf-8").splitlines()]


class TargetStateTests(IsolatedTargetState):
    def test_explicit_mac_refuses_an_unconfigured_or_local_target_before_writes(self) -> None:
        original = self.config.read_text(encoding="utf-8")
        for configuration in (
            original.replace("targets=mac,", "targets="),
            original.replace("kind.mac=ssh", "kind.mac=local"),
        ):
            _ = self.config.write_text(configuration, encoding="utf-8")
            before = self.snapshot()
            result = self.claim("mac")
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("mac", result.stderr)
            self.assertEqual(self.snapshot(), before)

    def test_explicit_mac_reads_registry_settings_and_preserves_legacy_settings(self) -> None:
        legacy = self.root / "mac_test.conf"
        _ = write_config_fixture(legacy, "block_hours=4\nci_repo=", template="mac_test")
        settings = self.root / "mac.conf"
        _ = settings.write_text(settings.read_text().replace("block_hours=4", "block_hours=1"), encoding="utf-8")
        with mock.patch.dict(os.environ, {"MAC_TEST_CONFIG": str(legacy)}):
            result = self.block("mac")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            block = mac_test.read_claim_block(mac_test.state_paths("mac"))
            self.assertNotIsInstance(block, mac_test.NoMacBlock)
            if not isinstance(block, mac_test.NoMacBlock):
                self.assertEqual(block.expires - block.since, timedelta(hours=1))
            result = self.invoke_mac(["unblock", "--target", "mac", "--holder", "alice"])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = self.invoke_mac(["block", "--holder", "alice", "--for", "legacy hold"])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            block = mac_test.read_claim_block(mac_test.state_paths())
            self.assertNotIsInstance(block, mac_test.NoMacBlock)
            if not isinstance(block, mac_test.NoMacBlock):
                self.assertEqual(block.expires - block.since, timedelta(hours=4))

    def test_explicit_mac_free_now_reads_the_registry_offload_setting(self) -> None:
        legacy = self.root / "mac_test.conf"
        _ = write_config_fixture(legacy, "offload=on\nci_repo=", template="mac_test")
        settings = self.root / "mac.conf"
        _ = settings.write_text(settings.read_text().replace("offload=on", "offload=off"), encoding="utf-8")
        with mock.patch.dict(os.environ, {"MAC_TEST_CONFIG": str(legacy)}):
            selected = self.invoke_mac(["free-now", "--target", "mac"])
            default = self.invoke_mac(["free-now"])
        self.assertEqual((selected.returncode, selected.stdout.strip()), (1, "off"))
        self.assertEqual((default.returncode, default.stdout.strip()), (0, "free"))

    def test_macos_peer_accepts_ci_settings_and_linux_peer_refuses_them(self) -> None:
        settings = self.root / "linux-two.conf"
        _ = settings.write_text(settings.read_text().replace("ci_repo=\n", "ci_repo=owner/project\n")
                                + "ci_variable=MACOS_CI\nci_workflow=ci.yml\nci_job=macOS tests\n",
                                encoding="utf-8")
        before = self.snapshot()
        result = self.block("linux-two")
        self.assertEqual(result.returncode, 12, result.stdout + result.stderr)
        self.assertEqual(self.snapshot(), before)
        _ = self.config.write_text(self.config.read_text().replace("platform.linux-two=linux",
                                                                  "platform.linux-two=macos"), encoding="utf-8")
        with mock.patch.object(mac_test, "switch_ci_off", return_value="off_before"), \
                mock.patch.object(mac_test, "ci_activity", return_value=mac_test.CI_IDLE), \
                mock.patch.object(mac_test, "skipped_ci_runs", return_value=""):
            result = self.block("linux-two")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIsInstance(targets.target_state(self.selected_target("linux-two")), targets.Held)
            result = self.invoke_mac(["unblock", "--target", "linux-two", "--holder", "alice"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIsInstance(targets.target_state(self.selected_target("linux-two")), targets.Free)

    def test_local_target_refuses_ssh_state_commands_before_writes(self) -> None:
        before = self.snapshot()
        result = self.claim("natedev")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_state_reader_reports_named_read_errors_and_preserves_unexpected_errors(self) -> None:
        target = self.selected_target("aws")
        for error in (mac_test.StateReadError("bad state"), targets.ConfigError("bad target")):
            with self.subTest(error=type(error)), mock.patch.object(mac_test, "state_paths", side_effect=error):
                state = targets.target_state(target)
                self.assertIsInstance(state, targets.StateUnreadable)
                if isinstance(state, targets.StateUnreadable):
                    self.assertIn(str(error), state.reason)
        with mock.patch.object(mac_test, "read_claims", side_effect=ValueError("unexpected reader defect")):
            with self.assertRaisesRegex(ValueError, "unexpected reader defect"):
                _ = targets.target_state(target)

    def test_aws_drain_reserve_covers_its_configured_maximum_duration(self) -> None:
        values = conf.read(SCRIPTS.parent / "config/aws_target.conf").values()
        reserve = float(values["limit_reserve_usd"])
        minutes = float(values["drain_max_minutes"])
        self.assertGreater(minutes, 0)
        self.assertGreater(reserve, 0)
        self.assertGreaterEqual(reserve, float(values["fallback_usd_per_hour"]) * minutes / 60)
        self.assertLess(reserve, float(values["daily_limit_usd"]))

    def test_aws_repository_budgets_fit_the_build_budget(self) -> None:
        values = conf.read(SCRIPTS.parent / "config/aws_target.conf").values()
        ceiling = float(values["build_budget_gib"])
        for key, value in values.items():
            if key == "target_budget_gib" or key.startswith("target_budget_gib."):
                with self.subTest(setting=key):
                    self.assertGreater(float(value), 0)
                    self.assertLessEqual(float(value), ceiling)

    def test_hold_activation_warning_and_expiry_notify_the_selected_target(self) -> None:
        result = self.block("mac")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        mac_before = (self.mac_state / "block.json").read_bytes()
        result = self.claim("aws")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = self.block("aws")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = self.release("aws")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        paths = mac_test.state_paths("aws")
        config = mac_test.read_config("aws")
        notifications: list[HoldNotification] = []

        def receive_message(recipient: str, summary: str, key: str, repeat_minutes: float, text: str) -> bool:
            notifications.append({"recipient": recipient, "summary": summary, "key": key,
                                  "repeat_minutes": repeat_minutes, "text": text})
            return True

        with mock.patch.object(mac_test, "send_message", side_effect=receive_message) as messages:
            outcome = mac_test.full_settle(paths, config, watcher_holds_lock=True)
            self.assertIsInstance(outcome.block, mac_test.ActiveMacBlock)
            messages.assert_called_once()
            self.assertEqual(notifications[-1]["summary"], "aws is free")
            self.assertTrue(notifications[-1]["key"].startswith("aws-free-"))
            self.assertIn("aws is free", notifications[-1]["text"])
            self.assertNotIn("Mac", notifications[-1]["text"])
            messages.reset_mock()
            with mock.patch.object(mac_test, "utc_now", return_value=NOW + timedelta(minutes=50)):
                _ = mac_test.full_settle(paths, config, watcher_holds_lock=True)
            messages.assert_called_once()
            self.assertEqual(notifications[-1]["summary"], "aws block nearing its time limit")
            self.assertTrue(notifications[-1]["key"].startswith("aws-block-warn-"))
            self.assertIn("aws block", notifications[-1]["text"])
            messages.reset_mock()
            with mock.patch.object(mac_test, "utc_now", return_value=NOW + timedelta(hours=1)):
                outcome = mac_test.full_settle(paths, config, watcher_holds_lock=True)
            self.assertIsInstance(outcome.block, mac_test.NoMacBlock)
            messages.assert_called_once()
            self.assertEqual(notifications[-1]["summary"], "aws block lifted")
            self.assertTrue(notifications[-1]["key"].startswith("aws-block-expired-"))
            self.assertIn("aws block", notifications[-1]["text"])
            self.assertNotIn("Mac", notifications[-1]["text"])
        self.assertEqual((self.mac_state / "block.json").read_bytes(), mac_before)

    def test_target_paths_keep_all_coordination_files_in_the_selected_directory(self) -> None:
        for name in TARGET_NAMES:
            with self.subTest(target=name):
                paths = mac_test.state_paths(name)
                self.assertEqual(paths.directory, self.directory(name))
                for path in (paths.lock, paths.control_lock, paths.watch_lock, paths.claim_watch_lock,
                             paths.run, paths.claims, paths.block):
                    self.assertEqual(path.parent, self.directory(name))
        self.assertFalse(any(self.directory(name).exists() for name in TARGET_NAMES))

    def test_mac_default_paths_and_fixture_override_remain_compatible(self) -> None:
        self.assertEqual(mac_test.state_paths().directory, self.mac_state)
        with mock.patch.dict(os.environ):
            _ = os.environ.pop("MAC_TEST_STATE_DIR")
            self.assertEqual(mac_test.state_paths().directory, self.root / ".local/state/mac-test")

    def test_claims_and_release_are_independent_for_the_same_repository_and_pid(self) -> None:
        self.use_generic_linux_target()
        for name in ("mac", "linux-one", "linux-two"):
            result = self.claim(name)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            state = targets.target_state(self.selected_target(name))
            self.assertIsInstance(state, targets.Running)
            if isinstance(state, targets.Running):
                self.assertEqual([job.what for job in state.jobs], [f"test {name}"])
        self.assertEqual(self.claim("linux-one", 202).returncode, 11)
        mac_before = {path: path.read_bytes() for path in self.mac_state.rglob("*.json")}
        result = self.release("linux-one")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIsInstance(targets.target_state(self.selected_target("linux-one")), targets.Free)
        self.assertIsInstance(targets.target_state(self.selected_target("linux-two")), targets.Running)
        self.assertEqual({path: path.read_bytes() for path in self.mac_state.rglob("*.json")}, mac_before)

    def test_blocks_refuse_only_the_selected_target_and_use_its_settings(self) -> None:
        self.use_generic_linux_target()
        for name, hours in zip(("mac", "linux-one", "linux-two"), (4, 1, 2), strict=True):
            result = self.block(name)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            state = targets.target_state(self.selected_target(name))
            self.assertIsInstance(state, targets.Held)
            if isinstance(state, targets.Held):
                self.assertEqual((state.holder, state.reason), ("alice", f"inspect {name}"))
                self.assertEqual(state.end, targets.ExpiresAt(NOW + timedelta(hours=hours)))
            self.assertEqual(self.claim(name).returncode, 10)
            if name == "mac":
                self.assertIsInstance(targets.target_state(self.selected_target("linux-one")), targets.Free)
        result = self.invoke_mac(["unblock", "--target", "linux-one", "--holder", "alice"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.claim("linux-one").returncode, 0)
        self.assertEqual(self.claim("mac").returncode, 10)
        self.assertEqual(self.claim("linux-two").returncode, 10)

    def test_backoff_writes_and_expiry_are_independent(self) -> None:
        for name, seconds in zip(TARGET_NAMES, (10, 20, 30), strict=True):
            offload.write_backoff(NOW.timestamp() + seconds, name)
            self.assertEqual(float((self.directory(name) / "unreachable_until").read_text()),
                             NOW.timestamp() + seconds)
        before = self.snapshot()
        for name, active in zip(TARGET_NAMES, (False, True, True), strict=True):
            self.assertEqual(isinstance(offload.read_backoff(NOW.timestamp() + 15, name), offload.ActiveBackoff), active)
        self.assertEqual(self.snapshot(), before)

    def test_free_now_uses_only_the_selected_targets_backoff(self) -> None:
        offload.write_backoff(NOW.timestamp() + 30, "aws")
        for name, answer in zip(TARGET_NAMES, ("free", "backoff", "free"), strict=True):
            result = self.invoke_mac(["free-now", "--target", name, "--repo-root", str(self.root)])
            self.assertEqual(result.stdout.strip(), answer, result.stderr)
            self.assertEqual(result.returncode, 0 if answer == "free" else 1)

    def test_unknown_target_commands_refuse_before_any_state_write(self) -> None:
        commands = (["claim", "--pid", "101", "--what", "test"],
                    ["release", "--pid", "101", "--claim-id", "claim-a", "--episode", "episode-a", "--attempt", "attempt-a"],
                    ["block", "--holder", "alice", "--for", "inspect"], ["unblock", "--holder", "alice"],
                    ["status"], ["free-now"])
        before = self.snapshot()
        for arguments in commands:
            with self.subTest(command=arguments[0]):
                result = self.invoke_mac([*arguments, "--target", "missing"])
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("missing", result.stdout + result.stderr)
                self.assertEqual(self.snapshot(), before)

    def test_unknown_backoff_target_refuses_before_any_state_write(self) -> None:
        before = self.snapshot()
        with self.assertRaises(ValueError):
            offload.write_backoff(NOW.timestamp() + 30, "missing")
        self.assertEqual(self.snapshot(), before)

    def test_state_reads_do_not_create_or_rewrite_files(self) -> None:
        self.use_generic_linux_target()
        before = self.snapshot()
        for name in ("mac", "linux-one", "linux-two"):
            self.assertIsInstance(targets.target_state(self.selected_target(name)), targets.Free)
        self.assertEqual(self.snapshot(), before)
        self.directory("linux-one").mkdir(parents=True)
        block = self.directory("linux-one") / "block.json"
        _ = block.write_text("{broken", encoding="utf-8")
        before = self.snapshot()
        self.assertIsInstance(targets.target_state(self.selected_target("linux-one")), targets.StateUnreadable)
        self.assertEqual(self.snapshot(), before)

    def test_explicit_status_reads_only_the_selected_target_without_writes(self) -> None:
        before = self.snapshot()
        for name in TARGET_NAMES:
            result = self.invoke_mac(["status", "--target", name])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.strip(), "free")
        self.assertEqual(self.snapshot(), before)
        result = self.block("aws")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = self.claim("linux-two")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        before = self.snapshot()
        for name, expected in zip(TARGET_NAMES, ("free", "inspect aws", "test linux-two"), strict=True):
            result = self.invoke_mac(["status", "--target", name])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(expected, result.stdout)
            if name != "aws":
                self.assertNotIn("inspect aws", result.stdout)
            if name != "linux-two":
                self.assertNotIn("test linux-two", result.stdout)
        self.assertEqual(self.snapshot(), before)

    def test_malformed_block_refuses_only_its_target(self) -> None:
        self.directory("aws").mkdir(parents=True)
        path = self.directory("aws") / "block.json"
        for content in ("{broken", "[]", '{"holder":"alice"}'):
            with self.subTest(content=content):
                _ = path.write_text(content, encoding="utf-8")
                result = self.claim("aws")
                self.assertEqual(result.returncode, 12, result.stdout + result.stderr)
                self.assertEqual(path.read_text(encoding="utf-8"), content)
                self.assertIsInstance(targets.target_state(self.selected_target("aws")), targets.StateUnreadable)
                self.assertIsInstance(targets.target_state(self.selected_target("mac")), targets.Free)
                self.assertIsInstance(targets.target_state(self.selected_target("linux-two")), targets.Free)
                before = self.snapshot()
                result = self.invoke_mac(["status", "--target", "aws"])
                self.assertEqual(result.returncode, 12, result.stdout + result.stderr)
                self.assertEqual(self.snapshot(), before)

    def test_state_reads_preserve_stale_claims_and_expired_blocks(self) -> None:
        result = self.claim("linux-two")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = self.block("aws")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        before = self.snapshot()
        with mock.patch.object(process_identity, "process_start", return_value=process_identity.Gone()), \
                mock.patch.object(targets, "utc_now", return_value=NOW + timedelta(days=1)):
            self.assertIsInstance(targets.target_state(self.selected_target("linux-two")), targets.Free)
            state = targets.target_state(self.selected_target("aws"))
            self.assertIsInstance(state, targets.Held)
            if isinstance(state, targets.Held):
                self.assertIsInstance(state.end, targets.AwaitingBlockRemoval)
        self.assertEqual(self.snapshot(), before)

    def test_legacy_mac_run_and_backoff_stay_in_place(self) -> None:
        self.use_generic_linux_target()
        self.mac_state.mkdir()
        legacy = self.mac_state / "run.json"
        _ = legacy.write_text(json.dumps({"claim_id": "claim-a", "episode": "episode-a", "attempt": "attempt-a",
                                         "submitting_session": "", "pid": 101, "proc_start": "fake-start", "what": "legacy test",
                                         "worktree": str(self.root), "since": NOW.isoformat()}), encoding="utf-8")
        _ = (self.mac_state / "unreachable_until").write_text(str(NOW.timestamp() + 30), encoding="utf-8")
        before = self.snapshot()
        state = targets.target_state(self.selected_target("mac"))
        self.assertIsInstance(state, targets.Running)
        if isinstance(state, targets.Running):
            self.assertEqual([job.what for job in state.jobs], ["legacy test"])
        self.assertIsInstance(offload.read_backoff(NOW.timestamp()), offload.ActiveBackoff)
        for name in ("linux-one", "linux-two"):
            self.assertIsInstance(targets.target_state(self.selected_target(name)), targets.Free)
            self.assertNotIsInstance(offload.read_backoff(NOW.timestamp(), name), offload.ActiveBackoff)
        self.assertEqual(self.snapshot(), before)
        result = self.release()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(legacy.exists())


if __name__ == "__main__":
    _ = unittest.main()
