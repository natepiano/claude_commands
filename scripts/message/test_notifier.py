"""Notifier CLI behavior with an isolated clock, session and send command."""

from __future__ import annotations

import json
import os
import shutil
import signal
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
        self.launcher_dir: Path = Path()
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
        launcher_dir = self.root / "launcher-bin"
        launcher_dir.mkdir()
        launcher = launcher_dir / "systemd-run"
        _ = launcher.write_text(
            "\n".join((
                "#!/usr/bin/env python3",
                "import subprocess",
                "import sys",
                "subprocess.Popen(sys.argv[sys.argv.index('--') + 1:], "
                + "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
                + "stderr=subprocess.DEVNULL, start_new_session=True)",
            )) + "\n"
        )
        launcher.chmod(0o755)
        self.launcher_dir = launcher_dir
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
                "PATH": f"{self.launcher_dir}{os.pathsep}{os.environ['PATH']}",
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

    def wait_for_text(self, path: Path, expected: str) -> str:
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if path.exists():
                contents = path.read_text()
                if expected in contents:
                    return contents
            time.sleep(0.02)
        self.fail(f"{path} did not contain {expected!r} before the deadline")

    def fail_mv_to(self, filename: str) -> Path:
        real_mv = shutil.which("mv")
        self.assertIsNotNone(real_mv)
        assert real_mv is not None
        stub = self.launcher_dir / "mv"
        _ = stub.write_text(
            "\n".join((
                "#!/bin/sh",
                "destination=",
                'for argument in "$@"; do destination=$argument; done',
                f'case "$destination" in */{filename}) exit 23 ;; esac',
                f'exec "{real_mv}" "$@"',
            )) + "\n"
        )
        stub.chmod(0o755)
        return stub

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

    def test_new_state_write_failure_leaves_no_conf(self) -> None:
        _ = self.fail_mv_to("state")
        result = self.run_cli(
            "new", "example", "--to", "session:sid-1", "--every", "2",
            "--command", "the scheduled prompt",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.state_dir / "example" / "conf").exists())

    def test_new_conf_write_failure_is_completed_by_next_new(self) -> None:
        stub = self.fail_mv_to("conf")
        result = self.run_cli(
            "new", "example", "--to", "session:sid-1", "--every", "2",
            "--command", "the scheduled prompt",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.state_dir / "example" / "state").exists())
        self.assertFalse((self.state_dir / "example" / "conf").exists())
        self.assertNotIn("example", self.successful("status"))

        stub.unlink()
        _ = self.new()
        self.assertTrue((self.state_dir / "example" / "conf").exists())
        _ = self.successful("tick", now=MINUTE + 120)
        self.assertEqual(len(self.lines()), 1)

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

    def test_resume_keeps_an_upcoming_due_time_and_enabled_state(self) -> None:
        _ = self.new()
        due = self.state()["NEXT_DUE"]
        _ = self.successful("stop", "example")
        output = self.successful("resume", "example", now=MINUTE + 60)
        self.assertTrue(output.startswith(f"next_due={due} ("), output)
        self.assertEqual(self.state()["ENABLED"], "1")
        self.assertEqual(self.state()["NEXT_DUE"], due)

        before = self.state()
        output = self.successful("resume", "example", now=MINUTE + 90)
        self.assertTrue(output.startswith(f"next_due={due} ("), output)
        self.assertEqual(self.state(), before)

    def test_resume_keeps_a_past_due_time_then_tick_sends_once(self) -> None:
        _ = self.new()
        due = self.state()["NEXT_DUE"]
        _ = self.successful("stop", "example")
        output = self.successful("resume", "example", now=MINUTE + 180)
        self.assertTrue(output.startswith(f"next_due={due} ("), output)
        self.assertEqual(self.state()["ENABLED"], "1")
        self.assertEqual(self.state()["NEXT_DUE"], due)

        _ = self.successful("tick", now=MINUTE + 180)
        self.assertEqual(len(self.lines()), 1)
        self.assertEqual(self.state()["LAST_SENT"], str(MINUTE + 180))
        _ = self.successful("tick", now=MINUTE + 180)
        self.assertEqual(len(self.lines()), 1)

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

    def test_run_only_tick_executes_once_without_sending_or_logging_success(self) -> None:
        output = self.root / "runs"
        command = f"/bin/sh -c 'printf run >> {output}'"
        _ = self.successful("new", "example", "--every", "1", "--run", command)
        self.assertIn(f"example runs {command} every 1 min", self.successful("status", "example"))
        _ = self.successful("tick", now=MINUTE + 60)
        self.assertEqual(self.wait_for_text(output, "run"), "run")
        self.assertFalse(self.send_args.exists())
        self.assertEqual(self.lines(), [])
        _ = self.successful("tick", now=MINUTE + 60)
        self.assertEqual(output.read_text(), "run")
        _ = self.successful("tick", now=MINUTE + 120)
        self.assertEqual(self.wait_for_text(output, "runrun"), "runrun")

    def test_run_only_tick_logs_exit_failure_and_timeout(self) -> None:
        _ = self.successful("new", "example", "--every", "1", "--run", "/bin/sh -c 'exit 7'")
        _ = self.successful("tick", now=MINUTE + 60)
        _ = self.wait_for_text(self.state_dir / "example" / "fire.log", "exit 7")
        self.assertTrue(any("exit 7" in line for line in self.lines()))
        self.assertFalse(self.send_args.exists())
        slow = self.root / "slow-run"
        _ = slow.write_text("#!/bin/sh\nexec sleep 4\n")
        slow.chmod(0o755)
        _ = self.successful("new", "example", "--every", "1", "--run", str(slow), "--timeout", "1")
        started = time.monotonic()
        _ = self.successful("tick", now=MINUTE + 120)
        self.assertLess(time.monotonic() - started, 3)
        _ = self.wait_for_text(self.state_dir / "example" / "fire.log", "run timeout")
        self.assertTrue(any("timeout" in line for line in self.lines()))
        self.assertFalse(self.send_args.exists())

    def test_run_only_job_survives_tick_process_group_exit_and_logs_failure(self) -> None:
        started = self.root / "started"
        finished = self.root / "finished"
        job = self.root / "job"
        _ = job.write_text(
            f"#!/bin/sh\nprintf started > {started}\nsleep 0.5\n"
            + f"printf finished > {finished}\nexit 7\n"
        )
        job.chmod(0o755)
        _ = self.successful("new", "example", "--every", "1", "--run", str(job))
        tick = subprocess.Popen(
            ["zsh", str(SCRIPT), "tick"],
            env={
                **os.environ,
                "PATH": f"{self.launcher_dir}{os.pathsep}{os.environ['PATH']}",
                "NOTIFIER_STATE_DIR": str(self.state_dir),
                "NOTIFIER_SESSIONS_DIR": str(self.sessions_dir),
                "NOTIFIER_NOW_EPOCH": str(MINUTE + 60),
            },
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
        try:
            _, stderr = tick.communicate(timeout=3)
            self.assertEqual(tick.returncode, 0, stderr)
            _ = self.wait_for_text(started, "started")
            self.assertFalse(finished.exists())
        finally:
            try:
                os.killpg(tick.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.assertEqual(self.wait_for_text(finished, "finished"), "finished")
        _ = self.wait_for_text(self.state_dir / "example" / "fire.log", "run exit 7")

    def test_mac_run_only_job_removes_label_after_fire_log_and_runs_once(self) -> None:
        launcher = self.launcher_dir / "launchctl"
        _ = launcher.write_text(
            """#!/usr/bin/env python3
import os
from pathlib import Path
import subprocess
import sys
import time

state = Path(os.environ["FAKE_LAUNCHCTL_STATE"])
action = sys.argv[1]
if action == "submit":
    label = sys.argv[sys.argv.index("-l") + 1]
    (state / "submitted").write_text(label)
    command = sys.argv[sys.argv.index("--") + 1:]
    job = subprocess.Popen(
        [sys.executable, __file__, "loop", label, *command],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True,
    )
    (state / "pid").write_text(str(job.pid))
elif action == "loop":
    label = sys.argv[2]
    removed = state / f"{label}.removed"
    while not removed.exists():
        subprocess.run(sys.argv[3:], check=False)
        time.sleep(0.05)
elif action == "remove":
    label = sys.argv[2]
    fire_log = Path(os.environ["FAKE_LAUNCHCTL_FIRE"])
    result = "after-fire" if fire_log.exists() and "run exit 7" in fire_log.read_text() else "before-fire"
    (state / f"{label}.removed").write_text(result)
"""
        )
        launcher.chmod(0o755)
        launch_state = self.root / "launchctl-state"
        launch_state.mkdir()

        def stop_fake_job() -> None:
            pid_file = launch_state / "pid"
            if pid_file.exists():
                try:
                    os.killpg(int(pid_file.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

        self.addCleanup(stop_fake_job)
        runs = self.root / "mac-runs"
        fire_log = self.state_dir / "example" / "fire.log"
        with mock.patch.dict(os.environ, {
            "NOTIFIER_PLATFORM": "darwin",
            "FAKE_LAUNCHCTL_STATE": str(launch_state),
            "FAKE_LAUNCHCTL_FIRE": str(fire_log),
        }):
            _ = self.successful(
                "new", "example", "--every", "1", "--run",
                f"/bin/sh -c 'printf run >> {runs}; exit 7'",
            )
            _ = self.successful("tick", now=MINUTE + 60)
        self.assertEqual(self.wait_for_text(fire_log, "run exit 7").count("run exit 7"), 1)
        self.assertEqual(self.wait_for_text(runs, "run"), "run")
        label = (launch_state / "submitted").read_text()
        removed = launch_state / f"{label}.removed"
        self.assertEqual(self.wait_for_text(removed, "after-fire"), "after-fire")
        self.assertEqual(runs.read_text(), "run")

    def test_run_only_long_run_does_not_hold_tick_or_other_due_instance(self) -> None:
        started_file = self.root / "started"
        finished_file = self.root / "finished"
        slow = self.root / "slow-run"
        _ = slow.write_text(f"#!/bin/sh\nprintf started > {started_file}\nsleep 3\nprintf finished > {finished_file}\n")
        slow.chmod(0o755)
        _ = self.successful("new", "example", "--every", "1", "--run", str(slow), "--timeout", "2")
        _ = self.successful("new", "other", "--to", "session:sid-1", "--every", "1", "--command", "due")
        started = time.monotonic()
        _ = self.successful("tick", now=MINUTE + 60)
        self.assertLess(time.monotonic() - started, 1.5)
        _ = self.wait_for_text(started_file, "started")
        self.assertFalse(finished_file.exists())
        self.assertTrue(self.send_args.exists())
        _ = self.wait_for_text(self.state_dir / "example" / "fire.log", "run timeout")

    def test_run_only_run_log_captures_stderr_and_replaces_previous_run(self) -> None:
        run_log = self.state_dir / "example" / "run.log"
        _ = self.successful("new", "example", "--every", "1", "--run", "/bin/sh -c 'echo first >&2'")
        _ = self.successful("tick", now=MINUTE + 60)
        self.assertEqual(self.wait_for_text(run_log, "first"), "first\n")
        _ = self.successful("new", "example", "--every", "1", "--run", "/bin/sh -c 'echo second >&2'")
        _ = self.successful("tick", now=MINUTE + 120)
        self.assertEqual(self.wait_for_text(run_log, "second"), "second\n")
        self.assertEqual(self.lines(), [])

    def test_run_only_rejects_delivery_and_check_options(self) -> None:
        for option, value in (("--to", "session:sid-1"), ("--command", "prompt"),
                              ("--prompt-file", "/tmp/prompt"), ("--check", "/bin/true")):
            with self.subTest(option=option):
                result = self.run_cli("new", "example", "--every", "1", "--run", "/bin/true", option, value)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse((self.state_dir / "example").exists())


if __name__ == "__main__":
    _ = unittest.main()
