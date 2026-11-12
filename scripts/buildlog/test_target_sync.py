"""Exchange configured peer records without sharing local admission state."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from typing import cast, override
import unittest
from unittest import mock

from scripts.buildlog import ci
from scripts.buildlog import health
from scripts.buildlog import record
from scripts.buildlog import store
from scripts.buildlog import sync
from scripts.buildlog import vault_pull
from scripts.buildlog.test_index import point_root_at


class TargetSyncTests(unittest.TestCase):
    root: Path = Path()
    registry: Path = Path()
    commands: list[tuple[list[str], float]] = []
    unavailable: set[str] = set()
    failed_pushes: set[str] = set()

    @override
    def setUp(self) -> None:
        temporary = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = temporary / "buildlog"
        self.registry = temporary / "build_targets.conf"
        self.commands = []
        self.unavailable = set()
        self.failed_pushes = set()
        for name in ("mac", "aws"):
            _ = (temporary / f"{name}.conf").write_text(f"host=fake-{name}\nci_repo=\n")
        _ = self.registry.write_text(
            "targets=mac,aws,natedev\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac.conf\n"
            + "kind.aws=ssh\nplatform.aws=linux\nsettings.aws=aws.conf\n"
            + "kind.natedev=local\nplatform.natedev=linux\n"
            + "sync.connect_timeout_s=3\nsync.reach_timeout_s=7\nsync.rsync_timeout_s=11\n"
            + "sync.hosts.mac=Mac,Nates-MacBook-Pro-2023\nsync.hosts.aws=aws-build\n"
            + "sync.hosts.natedev=natedev\n"
            + "# whole: 0..10\nvault.lock_retries=5\n"
            + "# range: 0..5\nvault.lock_retry_delay_s=3\n"
        )
        point_root_at(self, self.root)
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(temporary), "BUILD_TARGETS_CONFIG": str(self.registry), "BUILDLOG_PEER": "",
            "CONF_DIR": str(temporary),
        })))
        _ = self.enterContext(mock.patch.object(store, "host_name", return_value="natedev"))
        _ = self.enterContext(mock.patch.object(health, "write_snapshot"))
        _ = self.enterContext(mock.patch.object(time, "time", return_value=1_793_570_400.0))
        _ = self.enterContext(mock.patch.object(sync, "run", side_effect=self.command_reply))
        _ = self.enterContext(mock.patch.object(subprocess, "run", side_effect=AssertionError("Unexpected external command")))
        for name in ("natedev", "ci", "Mac", "aws-build", "admission", "pending-steps", "pending-launches"):
            directory = self.root / name
            directory.mkdir(parents=True)
            _ = (directory / "owned.jsonl").write_text(name + "\n")

    def command_reply(self, argv: list[str], timeout: float) -> subprocess.CompletedProcess[str] | None:
        self.commands.append((argv, timeout))
        if argv[0] == "ssh":
            peer = argv[-2]
            if peer in self.unavailable:
                return None
            return subprocess.CompletedProcess(argv, 0, "rc=0\n", "")
        self.assertEqual(argv[0], "rsync")
        if argv[-1] in {f"{peer}:{sync.REMOTE_ROOT}" for peer in self.failed_pushes}:
            return subprocess.CompletedProcess(argv, 23, "", "fixture transfer failure")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def exchange(self) -> int:
        with redirect_stdout(io.StringIO()):
            return sync.sync()

    def peers(self) -> list[str]:
        return [argv[-2] for argv, _ in self.commands if argv[0] == "ssh"]

    def pulls(self) -> list[list[str]]:
        return [argv for argv, _ in self.commands if argv[0] == "rsync" and argv[-1] == f"{self.root}/"]

    def status(self, peer: str) -> sync.SyncStatus:
        status = sync.read_status(peer)
        if not isinstance(status, sync.SyncStatus):
            self.fail(f"Missing synchronization status for {peer}")
        return status

    def test_natedev_exchanges_with_every_configured_ssh_peer(self) -> None:
        self.assertEqual(self.exchange(), 0)

        self.assertEqual(self.peers(), ["fake-mac", "fake-aws"])
        self.assertEqual({argv[-2] for argv in self.pulls()}, {
            f"fake-mac:{sync.REMOTE_ROOT}", f"fake-aws:{sync.REMOTE_ROOT}",
        })

    def test_finished_target_exchanges_only_its_host_with_configured_timeouts(self) -> None:
        _ = (self.registry.parent / "aws.conf").write_text("host=aws-build\nci_repo=\n")
        with redirect_stdout(io.StringIO()):
            sync.sync_target("aws")

        self.assertEqual(self.peers(), ["aws-build"])
        pull, = self.pulls()
        self.assertEqual(pull[-2:], [f"aws-build:{sync.REMOTE_ROOT}", f"{self.root}/"])
        self.assertIn("--include=/aws-build/***", pull)
        self.assertNotIn("--include=/Mac/***", pull)
        for argv, timeout in self.commands:
            self.assertEqual(timeout, 7 if argv[0] == "ssh" else 11)
            self.assertIn("ConnectTimeout=3", " ".join(argv))

    def test_finished_target_skips_a_running_sync_without_contacting_the_peer(self) -> None:
        output = io.StringIO()
        with sync.sync_lock(), redirect_stdout(output):
            sync.sync_target("aws")

        self.assertEqual(self.commands, [])
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertIn("another sync holds the lock", output.getvalue())

    def test_finished_target_holds_the_sync_lock_during_every_command(self) -> None:
        def reply(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str] | None:
            with self.assertRaises(BlockingIOError), sync.sync_lock(blocking=False):
                self.fail("The exchange must hold the sync lock")
            return self.command_reply(argv, timeout)

        with mock.patch.object(sync, "run", side_effect=reply), redirect_stdout(io.StringIO()):
            sync.sync_target("aws")
        self.assertEqual(self.peers(), ["fake-aws"])

    def test_finished_target_respects_pause_and_missing_host_mapping(self) -> None:
        with redirect_stdout(io.StringIO()):
            _ = sync.pause("fixture pause")
            sync.sync_target("aws")
            _ = sync.resume()
            _ = self.registry.write_text(self.registry.read_text().replace("sync.hosts.aws=aws-build", "sync.hosts.aws="))
            sync.sync_target("aws")
        self.assertEqual(self.commands, [])

    def test_finished_target_failure_prints_one_line_and_preserves_retry(self) -> None:
        self.unavailable.add("fake-aws")
        output = io.StringIO()
        with redirect_stdout(output):
            sync.sync_target("aws")
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertFalse(self.status("aws").ok)
        self.unavailable.clear()
        with redirect_stdout(io.StringIO()):
            sync.sync_target("aws")
        self.assertTrue(self.status("aws").ok)

    def test_unreachable_mac_does_not_prevent_aws_exchange(self) -> None:
        self.unavailable.add("fake-mac")

        self.assertEqual(self.exchange(), 0)

        self.assertEqual(self.peers(), ["fake-mac", "fake-aws"])
        self.assertEqual([argv[-2] for argv in self.pulls()], [f"fake-aws:{sync.REMOTE_ROOT}"])
        legacy = sync.read_status()
        self.assertIsInstance(legacy, sync.SyncStatus)
        if isinstance(legacy, sync.SyncStatus):
            self.assertFalse(legacy.ok)

    def test_failed_push_does_not_prevent_the_other_peer(self) -> None:
        self.failed_pushes.add("fake-mac")

        self.assertEqual(self.exchange(), 1)

        self.assertEqual(self.peers(), ["fake-mac", "fake-aws"])
        self.assertEqual([argv[-2] for argv in self.pulls()], [f"fake-aws:{sync.REMOTE_ROOT}"])

    def test_transfers_preserve_host_ownership_and_atomic_replacement(self) -> None:
        self.assertEqual(self.exchange(), 0)

        pushes = [argv for argv, _ in self.commands if argv[0] == "rsync" and argv[-1] != f"{self.root}/"]
        self.assertEqual(len(pushes), 2)
        for argv in pushes:
            self.assertIn(str(self.root / "natedev"), argv)
            self.assertIn(str(self.root / "ci"), argv)
            for forbidden in ("Mac", "aws-build", "admission", "pending-steps", "pending-launches"):
                self.assertNotIn(str(self.root / forbidden), argv)
        for argv in self.pulls():
            if argv[-2].startswith("fake-aws:"):
                self.assertIn("--include=/aws-build/***", argv)
                self.assertNotIn("--include=/Mac/***", argv)
            else:
                self.assertIn("--include=/Mac/***", argv)
                self.assertNotIn("--include=/aws-build/***", argv)
            self.assertNotIn("--include=/natedev/***", argv)
            self.assertNotIn("--include=/ci/***", argv)
            first_include = next(i for i, word in enumerate(argv) if word.startswith("--include="))
            for folder in ("admission", "pending-steps", "pending-launches"):
                self.assertLess(argv.index(f"--exclude={folder}/"), first_include)
        for argv, _ in self.commands:
            if argv[0] == "rsync":
                self.assertNotIn("--inplace", argv)
                self.assertFalse(any(word.startswith("--delete") or word.startswith("--append") for word in argv))

    def test_sync_timeouts_reload_from_configuration(self) -> None:
        self.assertEqual(self.exchange(), 0)
        for argv, timeout in self.commands:
            self.assertEqual(timeout, 7 if argv[0] == "ssh" else 11)
            if "fake-aws" in " ".join(argv):
                self.assertIn("ConnectTimeout=3", " ".join(argv))
        self.commands.clear()
        _ = self.registry.write_text(self.registry.read_text().replace("sync.reach_timeout_s=7", "sync.reach_timeout_s=13"))

        self.assertEqual(self.exchange(), 0)

        self.assertEqual([timeout for argv, timeout in self.commands if argv[0] == "ssh"], [13, 13])

    def test_pause_prevents_every_peer_command_and_keeps_success_state(self) -> None:
        self.assertEqual(self.exchange(), 0)
        before = {path: path.read_bytes() for path in (
            sync.status_path(), sync.status_path("fake-mac"), sync.status_path("fake-aws"),
        )}
        with redirect_stdout(io.StringIO()):
            self.assertEqual(sync.pause("fixture pause"), 0)
        self.commands.clear()

        self.assertEqual(self.exchange(), 0)

        self.assertEqual(self.commands, [])
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

    def test_each_peer_retains_its_own_last_success(self) -> None:
        self.assertEqual(self.exchange(), 0)
        mac_before = self.status("fake-mac")
        aws_before = self.status("fake-aws")
        self.unavailable.add("fake-mac")

        with mock.patch.object(time, "time", return_value=1_793_574_000.0):
            self.assertEqual(self.exchange(), 0)

        mac_after = self.status("fake-mac")
        aws_after = self.status("fake-aws")
        self.assertFalse(mac_after.ok)
        self.assertEqual(mac_after.last_good, mac_before.last_good)
        self.assertNotEqual(mac_after.at, mac_before.at)
        self.assertTrue(aws_after.ok)
        self.assertNotEqual(aws_after.last_good, aws_before.last_good)
        self.assertEqual(aws_after.last_good, sync.LastSynced(aws_after.at))
        self.assertEqual(sync.read_status(), mac_after)
        self.assertEqual(self.status("mac"), mac_after)
        self.assertEqual(self.status("aws"), aws_after)

    def test_aws_failure_does_not_replace_the_legacy_mac_status(self) -> None:
        self.assertEqual(self.exchange(), 0)
        mac_before = self.status("fake-mac")
        self.unavailable.add("fake-aws")

        self.assertEqual(self.exchange(), 0)

        self.assertEqual(sync.read_status(), mac_before)
        self.assertFalse(self.status("fake-aws").ok)

    def test_inactive_aws_target_receives_no_connection(self) -> None:
        _ = self.registry.write_text(self.registry.read_text().replace("targets=mac,aws,natedev", "targets=mac,natedev"))

        self.assertEqual(self.exchange(), 0)

        self.assertEqual(self.peers(), ["fake-mac"])
        self.assertIsInstance(sync.read_status("fake-aws"), sync.NeverSynced)

    def test_aws_host_does_not_initiate_peer_connections(self) -> None:
        with mock.patch.object(store, "host_name", return_value="aws-build"):
            self.assertEqual(self.exchange(), 0)

        self.assertEqual(self.commands, [])

    def test_host_mapping_reload_changes_the_pull_allowlist(self) -> None:
        self.assertEqual(self.exchange(), 0)
        self.commands.clear()
        _ = self.registry.write_text(self.registry.read_text().replace("sync.hosts.aws=aws-build", "sync.hosts.aws=renamed-aws"))

        self.assertEqual(self.exchange(), 0)

        for argv in self.pulls():
            if argv[-2].startswith("fake-aws:"):
                self.assertIn("--include=/renamed-aws/***", argv)
            else:
                self.assertNotIn("--include=/renamed-aws/***", argv)
            self.assertNotIn("--include=/aws-build/***", argv)

    def test_each_pull_preserves_the_other_peers_current_records(self) -> None:
        mac_file = self.root / "Mac/owned.jsonl"

        def transfer(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str] | None:
            result = self.command_reply(argv, timeout)
            if argv[0] == "rsync" and argv[-1] == f"{self.root}/" and "--include=/Mac/***" in argv:
                _ = mac_file.write_text("fresh Mac record\n" if argv[-2].startswith("fake-mac:") else "stale Mac record\n")
            return result

        with mock.patch.object(sync, "run", side_effect=transfer):
            self.assertEqual(self.exchange(), 0)

        self.assertEqual(mac_file.read_text(), "fresh Mac record\n")
        mac_pull, aws_pull = self.pulls()
        self.assertIn("--include=/Mac/***", mac_pull)
        self.assertNotIn("--include=/aws-build/***", mac_pull)
        self.assertIn("--include=/aws-build/***", aws_pull)
        self.assertNotIn("--include=/Mac/***", aws_pull)

    def test_first_mac_failure_preserves_the_legacy_success_time(self) -> None:
        previous = "2026-10-31T12:00:00Z"
        _ = sync.status_path().write_text(json.dumps({
            "at": previous, "peer": "fake-mac", "ok": True, "last_ok": previous,
        }))
        self.unavailable.add("fake-mac")

        self.assertEqual(self.exchange(), 0)

        status = self.status("fake-mac")
        self.assertFalse(status.ok)
        self.assertEqual(status.last_good, sync.LastSynced(previous))
        self.assertEqual(sync.read_status(), status)

    def test_health_uses_only_configured_host_aliases(self) -> None:
        _ = self.registry.write_text(self.registry.read_text().replace(
            "sync.hosts.mac=Mac,Nates-MacBook-Pro-2023", "sync.hosts.mac=macbook"))

        self.assertEqual(health.machine_for_host("macbook"), health.Machine.MAC)
        self.assertEqual(health.machine_for_host("aws-build"), health.Machine.AWS)
        self.assertNotIn("Mac", record.synchronized_hosts())
        with self.assertRaises(ValueError):
            _ = health.machine_for_host("Mac")

    def test_health_reads_spaced_target_lists_like_the_registry(self) -> None:
        _ = self.registry.write_text(self.registry.read_text().replace(
            "targets=mac,aws,natedev", "targets=mac, aws, natedev"))

        machines = {state.machine if isinstance(state, health.RecorderHealthUnavailable) else state.snapshot.machine
                    for state in health.read_snapshots()}

        self.assertIn(health.Machine.NATEDEV, machines)

    def test_vault_transport_selects_connect_timeout_by_destination_host(self) -> None:
        commands: list[list[str]] = []

        def reply(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
            self.assertEqual(timeout, vault_pull.COMMAND_TIMEOUT_S)
            if argv[0] == "ssh" or f"{sync.peer_name()}:rust/hanadocs" in argv:
                commands.append(argv)
            outcome = f"VAULT_OUTCOME=merged:0:{'a' * 40}\n" if argv[0] == "ssh" else ""
            return subprocess.CompletedProcess(argv, 0, outcome, "")

        settings = self.registry.read_text()
        for host, timeout in (("aws-build", 3), ("aws-build", 9), ("Mac", 9)):
            with self.subTest(host=host, timeout=timeout), mock.patch.dict(os.environ, {"BUILDLOG_PEER": host}):
                commands.clear()
                _ = self.registry.write_text(settings.replace(
                    "sync.connect_timeout_s=3", f"sync.connect_timeout_s={timeout}"))
                with mock.patch.object(vault_pull, "run", side_effect=reply), mock.patch.object(ci, "warm", return_value=True):
                    self.assertIsInstance(vault_pull.pull(), vault_pull.Current)
                self.assertEqual([argv[0] for argv in commands], ["git", "ssh", "git"])
                for argv in commands:
                    if host == "aws-build":
                        self.assertIn(f"ConnectTimeout={timeout}", " ".join(argv))
                    else:
                        self.assertNotIn("ConnectTimeout", " ".join(argv))


if __name__ == "__main__":
    _ = unittest.main()
