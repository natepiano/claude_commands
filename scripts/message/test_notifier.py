"""Notifier CLI behavior with an isolated clock, session and send command."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from typing import override
from unittest import mock
from zoneinfo import ZoneInfo


SCRIPT = Path(__file__).with_name("notifier.sh")
NOW = 1_700_000_123
MINUTE = NOW - NOW % 60


class NotifierTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.state_dir: Path = Path()
        self.sessions_dir: Path = Path()
        self.send_args: Path = Path()
        self.fake_send: Path = Path()
        self.socket_path: Path = Path()
        self.live_socket: socket.socket = socket.socket(socket.AF_UNIX)

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state_dir = self.root / "state"
        self.sessions_dir = self.root / "sessions"
        self.sessions_dir.mkdir()
        self.send_args = self.root / "send.args"
        self.fake_send = self.root / "fake-send"
        _ = self.fake_send.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$FAKE_SEND_ARGS"\nprintf "fake send result\\n"\nexit "$FAKE_SEND_RC"\n')
        self.fake_send.chmod(0o755)
        self.addCleanup(self.live_socket.close)
        self.socket_path = self.root / "live.sock"
        self.live_socket.bind(str(self.socket_path))
        self.record("sid-1")

    def record(self, session_id: str, path: Path | None = None, updated: int = 1) -> None:
        record = {
            "pid": os.getpid(), "name": "showrunner", "sessionId": session_id,
            "messagingSocketPath": str(self.socket_path if path is None else path),
            "updatedAt": updated,
        }
        _ = (self.sessions_dir / "live.json").write_text(json.dumps(record))

    def run_cli(self, *args: str, now: int = NOW, send_rc: int = 0) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["zsh", str(SCRIPT), *args],
            env={
                **os.environ,
                "NOTIFIER_STATE_DIR": str(self.state_dir),
                "NOTIFIER_SESSIONS_DIR": str(self.sessions_dir),
                "NOTIFIER_SEND": str(self.fake_send),
                "NOTIFIER_NOW_EPOCH": str(now),
                "FAKE_SEND_ARGS": str(self.send_args),
                "FAKE_SEND_RC": str(send_rc),
            },
            capture_output=True, text=True, check=False,
        )

    def successful(self, *args: str, now: int = NOW, send_rc: int = 0) -> str:
        result = self.run_cli(*args, now=now, send_rc=send_rc)
        self.assertEqual(result.returncode, 0, (args, result.stdout, result.stderr))
        return result.stdout

    def new(self, *extra: str, every: int = 2) -> str:
        return self.successful(
            "new", "example", "--to", "session:sid-1", "--every", str(every),
            "--command", "the scheduled prompt", *extra,
        )

    def state(self) -> dict[str, str]:
        return dict(line.split("=", 1) for line in (self.state_dir / "example" / "state").read_text().splitlines())

    def conf(self) -> dict[str, str]:
        return dict(line.split("=", 1) for line in (self.state_dir / "example" / "conf").read_text().splitlines())

    def lines(self) -> list[str]:
        path = self.state_dir / "example" / "fire.log"
        return path.read_text().splitlines() if path.exists() else []

    def test_minute_rule_new_preserves_existing_state_and_retargets(self) -> None:
        self.assertTrue(self.new().startswith(f"next_due={MINUTE + 120} ("))
        self.assertEqual(self.state()["ENABLED"], "1")
        self.assertEqual(self.state()["NEXT_DUE"], str(MINUTE + 120))
        before = self.state()
        _ = self.successful(
            "new", "example", "--to", "other", "--every", "5", "--command", "new text",
            now=NOW + 90,
        )
        self.assertEqual(self.state(), before)
        self.assertEqual((self.conf()["TARGET"], self.conf()["EVERY"]), ("other", "5"))

    def test_start_stop_restart_and_interval_schedules(self) -> None:
        _ = self.new()
        _ = self.successful("stop", "example")
        self.assertEqual(self.state()["ENABLED"], "0")
        _ = self.successful("tick", now=MINUTE + 120)
        self.assertEqual(self.lines(), [])
        self.assertTrue(self.successful("start", "example", now=NOW + 75).startswith(f"next_due={MINUTE + 180} ("))
        self.assertEqual(self.state()["ENABLED"], "1")
        self.assertTrue(self.successful("restart", "example", now=NOW + 130).startswith(f"next_due={MINUTE + 240} ("))
        self.assertEqual(self.state()["LAST_RESTART"], str(NOW + 130))
        self.assertTrue(self.successful("interval", "example", "3", now=NOW + 190).startswith(f"next_due={MINUTE + 360} ("))
        self.assertEqual(self.conf()["EVERY"], "3")
        self.assertEqual(self.run_cli("interval", "example", "0").returncode, 2)

    def test_aligned_schedule_lands_on_the_clock(self) -> None:
        def at(zone: str, hour: int, minute: int) -> int:
            return int(datetime(2026, 10, 5, hour, minute, 30, tzinfo=ZoneInfo(zone)).timestamp())

        def next_due(*args: str, now: int) -> int:
            return int(self.successful(*args, now=now).split("=", 1)[1].split(" ", 1)[0])

        la = "America/Los_Angeles"
        with mock.patch.dict(os.environ, {"TZ": la}):
            _ = self.new(every=60)
            self.assertEqual(next_due("align", "example", "on", now=at(la, 10, 5)), at(la, 11, 0) - 30)
            self.assertEqual(self.conf()["ALIGN"], "1")
            self.assertIn("every 60 min on the clock, enabled", self.successful("status", "example"))
            _ = self.successful("tick", now=at(la, 11, 0))
            self.assertEqual(int(self.state()["NEXT_DUE"]), at(la, 12, 0) - 30)
            # A slot under half an interval away is skipped.
            self.assertEqual(next_due("restart", "example", now=at(la, 11, 55)), at(la, 13, 0) - 30)
            self.assertEqual(next_due("interval", "example", "30", now=at(la, 12, 10)), at(la, 12, 30) - 30)
            self.assertEqual(next_due("align", "example", "off", now=at(la, 12, 10)), at(la, 12, 40) - 30)
            self.assertEqual(self.run_cli("align", "example", "maybe").returncode, 2)
        kolkata = "Asia/Kolkata"
        with mock.patch.dict(os.environ, {"TZ": kolkata}):
            _ = self.successful("interval", "example", "60")
            self.assertEqual(next_due("align", "example", "on", now=at(kolkata, 10, 5)), at(kolkata, 11, 0) - 30)

    def test_due_tick_send_arguments_and_log_format(self) -> None:
        _ = self.new("--from", "sender", "--timeout", "7")
        _ = self.successful("tick", now=MINUTE + 120)
        self.assertEqual((self.state_dir / ".last_tick").read_text().strip(), str(MINUTE + 120))
        self.assertEqual(self.send_args.read_text().splitlines(), [
            "--to", f"uds:{self.socket_path}", "--from", "sender", "--key", "notifier-example",
            "--summary", "scheduled update", "--timeout", "7", "--text", "the scheduled prompt",
        ])
        self.assertEqual(self.state()["LAST_SENT"], str(MINUTE + 120))
        self.assertEqual(self.state()["NEXT_DUE"], str(MINUTE + 240))
        self.assertRegex(self.lines()[-1], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d .+ / \d\d:\d\d:\d\d UTC \| exit 0 \| to session:sid-1 uds:.+ \| fake send result$")

    def test_prompt_file_fire_ignores_schedule_and_hold(self) -> None:
        prompt = self.root / "prompt.txt"
        _ = prompt.write_text("prompt contents")
        _ = self.successful("new", "example", "--to", "showrunner", "--every", "2", "--prompt-file", str(prompt), "--hold")
        _ = self.successful("fire", "example")
        self.assertEqual(self.send_args.read_text().splitlines()[-2:], ["--file", str(prompt)])
        self.assertEqual(self.state()["NEXT_DUE"], str(MINUTE + 120))
        _ = self.successful("fire", "example", now=NOW + 1)
        self.assertEqual(len([line for line in self.lines() if " | exit 0 | " in line]), 2)

    def test_check_exit_one_skips_and_exit_two_removes(self) -> None:
        _ = self.new("--check", "/bin/sh -c 'exit 1'")
        _ = self.successful("tick", now=MINUTE + 120)
        self.assertIn(" | skip check exit 1", self.lines()[-1])
        self.assertFalse(self.send_args.exists())
        _ = self.new("--check", "/bin/sh -c 'exit 2'")
        _ = self.successful("fire", "example")
        self.assertFalse((self.state_dir / "example").exists())
        self.assertIn("removed example (check exit 2)", (self.state_dir / "notifier.log").read_text())

    def test_check_timeout_skips_send_and_releases_tick_promptly(self) -> None:
        check = self.root / "slow-check"
        _ = check.write_text("#!/bin/sh\nexec sleep 4\n")
        check.chmod(0o755)
        _ = self.new("--check", str(check), "--timeout", "1")
        started = time.monotonic()
        result = self.run_cli("tick", now=MINUTE + 120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(time.monotonic() - started, 3)
        self.assertIn(" | skip check timeout", self.lines()[-1], result.stderr)
        self.assertFalse(self.send_args.exists())

    def test_empty_prompt_sources_do_not_create_an_instance(self) -> None:
        for source in ("--command", "--prompt-file"):
            with self.subTest(source=source):
                result = self.run_cli(
                    "new", "example", "--to", "session:sid-1", "--every", "2", source, "",
                )
                self.assertEqual(result.returncode, 2)
                self.assertFalse((self.state_dir / "example").exists())

    def test_tick_with_existing_state_does_not_call_mkdir(self) -> None:
        _ = self.new()
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        mkdir_marker = self.root / "mkdir.called"
        fake_mkdir = fake_bin / "mkdir"
        _ = fake_mkdir.write_text(f"#!/bin/sh\nprintf called > '{mkdir_marker}'\nexit 1\n")
        fake_mkdir.chmod(0o755)
        result = subprocess.run(
            ["zsh", str(SCRIPT), "tick"],
            env={
                **os.environ,
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "NOTIFIER_STATE_DIR": str(self.state_dir),
                "NOTIFIER_SESSIONS_DIR": str(self.sessions_dir),
                "NOTIFIER_SEND": str(self.fake_send),
                "NOTIFIER_NOW_EPOCH": str(NOW),
                "FAKE_SEND_ARGS": str(self.send_args),
                "FAKE_SEND_RC": "0",
            },
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(mkdir_marker.exists())

    def test_check_zero_and_missing_session(self) -> None:
        _ = self.new("--check", "/bin/sh -c 'exit 0'")
        _ = self.successful("fire", "example")
        self.assertTrue(self.send_args.exists())
        self.send_args.unlink()
        self.live_socket.close()
        self.socket_path.unlink()
        _ = self.successful("fire", "example")
        self.assertIn(" | skip session not running", self.lines()[-1])
        self.assertFalse(self.send_args.exists())

    def test_hold_skips_then_releases_on_socket_change(self) -> None:
        _ = self.new("--hold")
        _ = self.successful("tick", now=MINUTE + 120)
        _ = self.successful("tick", now=MINUTE + 240)
        self.assertIn(" | skip hold", self.lines()[-1])
        other = socket.socket(socket.AF_UNIX)
        self.addCleanup(other.close)
        new_path = self.root / "replacement.sock"
        other.bind(str(new_path))
        self.record("sid-1", new_path, updated=2)
        _ = self.successful("tick", now=MINUTE + 360)
        self.assertTrue(any("hold released: socket changed" in line for line in self.lines()))
        self.assertEqual(self.state()["LAST_TARGET"], str(new_path))

    def test_hold_releases_after_two_intervals(self) -> None:
        _ = self.new("--hold")
        _ = self.successful("tick", now=MINUTE + 120)
        _ = self.successful("tick", now=MINUTE + 240)
        _ = self.successful("tick", now=MINUTE + 360)
        self.assertTrue(any("hold released: two intervals" in line for line in self.lines()))
        self.assertEqual(len([line for line in self.lines() if " | exit 0 | " in line]), 2)

    def test_health_absent_stale_stopped_and_two_failed_sends(self) -> None:
        self.assertEqual(self.run_cli("health", "missing").stdout.strip(), "failing: no instance")
        _ = self.new()
        self.assertIn("failing: no tick since", self.run_cli("health", "example").stdout)
        _ = self.successful("tick")
        self.assertEqual(self.successful("health", "example").strip(), "ok")
        _ = self.successful("fire", "example", send_rc=3)
        _ = self.successful("fire", "example", send_rc=1)
        failing = self.run_cli("health", "example")
        self.assertEqual(failing.returncode, 1)
        self.assertEqual(failing.stdout.strip(), "failing: last two sends exit 3, 1")
        _ = self.successful("stop", "example")
        self.assertEqual(self.successful("health", "example").strip(), "ok: stopped")
        self.assertIn("failing: no tick since", self.run_cli("health", "example", now=NOW + 121).stdout)

    def test_status_remove_and_usage(self) -> None:
        _ = self.new()
        self.assertIn("example → session:sid-1 every 2 min, enabled", self.successful("status", "example"))
        self.assertIn("example every 2 min next ", self.successful("status"))
        self.assertIn("last_tick=", self.successful("status"))
        for args in (("new", "bad/name", "--to", "x", "--every", "1", "--command", "x"),
                     ("new", "x", "--to", "x", "--every", "0", "--command", "x")):
            self.assertEqual(self.run_cli(*args).returncode, 2)
        _ = self.successful("remove", "example")
        _ = self.successful("remove", "example")
        self.assertEqual(self.run_cli("status", "example").returncode, 1)


if __name__ == "__main__":
    _ = unittest.main()
