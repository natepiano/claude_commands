#!/usr/bin/env python3
"""Sync pauses and Mac command status stay within a temporary state root."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import cast, override
from unittest import mock

import ci
import cli
import index
import rust_release
import store
import sync
from test_index import point_root_at


class SyncTests(unittest.TestCase):
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    commands: list[list[str]] = []
    responses: list[subprocess.CompletedProcess[str]] = []

    @override
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(temporary) / "buildlog"
        point_root_at(self, self.root)
        self.enterContext(mock.patch.dict(os.environ, {"HOME": temporary, "BUILDLOG_PEER": "test-mac"}))
        self.commands = []
        self.responses = []
        _ = self.enterContext(mock.patch.object(subprocess, "run", new=self.fake_run))
        _ = self.enterContext(mock.patch.object(ci, "gh_get", new=self.forbid_gh_get))

    def fake_run(self, args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        self.commands.append(args)
        return self.responses.pop(0)

    def forbid_gh_get(self, path: str, fields: dict[str, str]) -> object:
        self.fail(f"unexpected GitHub request: {path} {fields}")

    def status(self) -> dict[str, object]:
        return cast(dict[str, object], json.loads((self.root / "sync.json").read_text()))

    def test_mkdir_printed_failure_overrides_successful_ssh_exit(self) -> None:
        self.responses = [subprocess.CompletedProcess(["ssh"], 0, stdout="rc=1\n", stderr="")]

        self.assertEqual(sync.sync(), 1)

        self.assertIs(self.status()["ok"], False)
        self.assertEqual(len(self.commands), 1)

    def test_mkdir_without_printed_status_fails(self) -> None:
        self.responses = [subprocess.CompletedProcess(["ssh"], 0, stdout="created\n", stderr="")]

        self.assertEqual(sync.sync(), 1)

        self.assertIs(self.status()["ok"], False)
        self.assertEqual(len(self.commands), 1)

    def test_mkdir_uses_last_printed_status_and_then_runs_rsync(self) -> None:
        (self.root / "natedev").mkdir(parents=True)
        self.responses = [
            subprocess.CompletedProcess(["ssh"], 0, stdout="rc=1\nmessage\nrc=0\n", stderr=""),
            subprocess.CompletedProcess(["rsync"], 0, stdout="", stderr=""),
            subprocess.CompletedProcess(["rsync"], 0, stdout="", stderr=""),
        ]
        with mock.patch.object(store, "host_name", return_value="natedev"):
            self.assertEqual(sync.sync(), 0)

        self.assertEqual([command[0] for command in self.commands], ["ssh", "rsync", "rsync"])
        self.assertIs(self.status()["ok"], True)

    def test_non_255_ssh_exit_uses_printed_success(self) -> None:
        self.responses = [
            subprocess.CompletedProcess(["ssh"], 1, stdout="rc=0\n", stderr="login returned 1"),
            subprocess.CompletedProcess(["rsync"], 0, stdout="", stderr=""),
        ]

        self.assertEqual(sync.sync(), 0)

        self.assertEqual([command[0] for command in self.commands], ["ssh", "rsync"])
        self.assertIn('echo "rc=$?"', self.commands[0][-1])
        self.assertIs(self.status()["ok"], True)

    def test_ssh_255_is_peer_unavailable(self) -> None:
        self.responses = [subprocess.CompletedProcess(["ssh"], 255, stdout="", stderr="unreachable")]

        self.assertEqual(sync.sync(), 0)

        self.assertIs(self.status()["ok"], False)
        self.assertEqual(len(self.commands), 1)

    def test_pause_skips_peer_and_preserves_status_bytes(self) -> None:
        self.root.mkdir(parents=True)
        status_path = self.root / "sync.json"
        prior = b'{"ok": true, "last_ok": "2026-10-06T10:00:00Z"}\n'
        _ = status_path.write_bytes(prior)

        self.assertEqual(cli.main(["sync", "pause", "Mac hold"]), 0)
        pause = cast(dict[str, object], json.loads((self.root / "sync_paused.json").read_text()))
        self.assertEqual(pause["why"], "Mac hold")
        self.assertIsInstance(pause["since"], str)
        self.assertEqual(sync.sync(), 0)

        self.assertEqual(self.commands, [])
        self.assertEqual(status_path.read_bytes(), prior)

    def test_pause_waits_for_running_sync_to_write_status(self) -> None:
        waiting = threading.Event()
        returned = threading.Event()
        lines: list[str] = []
        status_at_return: list[dict[str, object]] = []

        def record_line(line: str) -> None:
            lines.append(line)
            if line == "buildlog sync: waiting for the running sync to finish":
                waiting.set()

        def pause_on_thread() -> None:
            _ = sync.pause("Mac hold")
            status_at_return.append(self.status())
            returned.set()

        worker = threading.Thread(target=pause_on_thread, daemon=True)

        def run_ssh(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            self.commands.append(args)
            worker.start()
            self.assertTrue(waiting.wait(2))
            self.assertFalse(returned.is_set())
            return subprocess.CompletedProcess(args, 255, stdout="", stderr="unreachable")

        with (
            mock.patch.object(subprocess, "run", side_effect=run_ssh),
            mock.patch.object(sync, "print", record_line, create=True),
        ):
            self.assertEqual(sync.sync(), 0)
            worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertTrue(returned.is_set())
        self.assertIs(status_at_return[0]["ok"], False)
        self.assertIn("buildlog sync: waiting for the running sync to finish", lines)
        self.assertTrue((self.root / "sync_paused.json").exists())

    def test_pause_without_running_sync_does_not_wait(self) -> None:
        lines: list[str] = []
        with mock.patch.object(sync, "print", lines.append, create=True):
            self.assertEqual(sync.pause("Mac hold"), 0)

        self.assertEqual(lines, ["buildlog sync: paused (Mac hold)"])
        self.assertTrue((self.root / "sync_paused.json").exists())

    def test_paused_sync_prints_local_time_with_zone(self) -> None:
        self.root.mkdir(parents=True)
        _ = (self.root / "sync_paused.json").write_text(
            json.dumps({"since": "2026-10-06T07:00:07.413+00:00", "why": "Mac hold"})
        )
        output = StringIO()
        self.addCleanup(time.tzset)
        with mock.patch.dict(os.environ, {"TZ": "America/New_York"}):
            time.tzset()
            with redirect_stdout(output):
                self.assertEqual(sync.sync(), 0)

        self.assertIn("paused since", output.getvalue())
        self.assertIn("03:00 EDT (Mac hold)", output.getvalue())
        self.assertNotIn("2026-10-06T07:00:07.413+00:00", output.getvalue())
        self.assertEqual(self.commands, [])

    def test_hourly_still_polls_ci_while_sync_is_paused(self) -> None:
        self.assertEqual(cli.main(["sync", "pause", "Mac hold"]), 0)
        with (
            mock.patch.object(ci, "ci", return_value=0) as poll,
            mock.patch.object(cli, "screenshot_hourly", return_value=0) as screenshots,
            mock.patch.object(rust_release, "check_release", return_value=0) as release,
            mock.patch.object(index, "update", return_value=0),
        ):
            self.assertEqual(cli.hourly(), 0)

        poll.assert_called_once_with()
        screenshots.assert_called_once_with()
        release.assert_called_once_with()
        self.assertEqual(self.commands, [])

    def test_screenshot_hourly_invokes_report_hourly_once(self) -> None:
        commands: list[tuple[list[str], bool, int]] = []

        def fake_run(args: list[str], *, check: bool, timeout: int) -> subprocess.CompletedProcess[str]:
            commands.append((args, check, timeout))
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            self.assertEqual(cli.screenshot_hourly(), 0)
        self.assertEqual(len(commands), 1)
        command, check, timeout = commands[0]
        self.assertEqual(command[-1], "hourly")
        self.assertTrue(command[-2].endswith("scripts/shot_report/shot_report.py"))
        self.assertFalse(check)
        self.assertEqual(timeout, 300)

    def test_resume_removes_pause_and_next_sync_contacts_peer(self) -> None:
        self.assertEqual(cli.main(["sync", "pause", "Mac hold"]), 0)
        self.assertEqual(cli.main(["sync", "resume"]), 0)
        self.assertFalse((self.root / "sync_paused.json").exists())
        self.responses = [subprocess.CompletedProcess(["ssh"], 255, stdout="", stderr="unreachable")]

        self.assertEqual(sync.sync(), 0)

        self.assertEqual(len(self.commands), 1)


if __name__ == "__main__":
    _ = unittest.main()
