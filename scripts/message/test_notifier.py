"""Notifier CLI behavior with an isolated clock, session and send command."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import cast, override
from unittest import mock
from zoneinfo import ZoneInfo

from scripts.production import staff
from scripts.production.production_doc import read_production
from scripts.production.update_registration import scheduled_prompt
from scripts.production.staff_test_support import isolated_staff_file, fixture_runtime_id, recorded_showrunner
from scripts.message.process_test_support import OwnedTestProcesses
from scripts.direct.wait_for_test_state import wait_until
from scripts.lib.socket_test_support import short_socket_directory
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib.checkout_test_support import copy_runtime
from scripts.lib import user_zone
from scripts.production.cli_fixture import invoke_cli, process_boundary, process_boundary_for_case


SCRIPT = Path(__file__).with_name("notifier.sh")
NOW = 1_700_000_123
MINUTE = NOW - NOW % 60


class NotifierTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.owned: OwnedTestProcesses = OwnedTestProcesses()
        self.launch_pids: Path = Path()
        self.blocked_pids: Path = Path()
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
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"__ETC_ZSHENV_SOURCED": "1"})))
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        self.owned = OwnedTestProcesses()
        self.addCleanup(self.owned.finish)
        self.launch_pids = self.root / "launch-pids"
        self.owned.group_markers.append(self.launch_pids)
        self.blocked_pids = self.root / "blocked-pids"
        self.owned.markers.append(self.blocked_pids)
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
        monitor = """import os
import subprocess
import sys
from pathlib import Path
code = 127
try:
    code = subprocess.run(sys.argv[2:], check=False).returncode
finally:
    Path(sys.argv[1], 'run-exit-' + str(os.getpid())).write_text(str(code))
"""
        _ = launcher.write_text(
            "\n".join((
                "#!/usr/bin/env python3",
                "import subprocess, os",
                "from pathlib import Path",
                "import sys",
                "# The caller installs the inherited ownership marker before launch.",
                f"monitor = {monitor!r}",
                f"child = subprocess.Popen([sys.executable, '-c', monitor, {str(self.root)!r}, *sys.argv[sys.argv.index('--') + 1:]], "
                + "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
                + "stderr=subprocess.DEVNULL, start_new_session=True)",
                f"with Path({str(self.launch_pids)!r}).open('a') as marker: marker.write(str(child.pid) + '\\n')",
            )) + "\n"
        )
        launcher.chmod(0o755)
        self.launcher_dir = launcher_dir
        self.addCleanup(self.live_socket.close)
        self.socket_path = Path(self.enterContext(short_socket_directory())) / "live.sock"
        self.live_socket.bind(str(self.socket_path))
        self.record("sid-1")

    def scheduled_owner(self) -> tuple[staff.StaffRow, Path]:
        doc = self.root / "production.md"
        _ = doc.write_text(f"# Production — demo\n\n## Production Context\n\n"
                           + f"- **Merge branch:** main\n- **Showrunner checkout:** `{self.root}`\n"
                           + "- **Log:** production.log\n- **Updates:** every 2 minutes\n")
        owner = recorded_showrunner("demo", doc)
        self.record(owner.current_session)
        return owner, doc

    def test_fixed_timer_delivers_to_current_session_after_clear_without_rewriting_its_target(self) -> None:
        owner, doc = self.scheduled_owner()
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"HOME": str(self.root / "home")})))
        current = staff.native_id(fixture_runtime_id("timer-after-clear"))
        _ = self.successful("new", "example", "--to", "session:" + owner.id,
                            "--every", "2", "--prompt", "scheduled")
        configuration = self.state_dir / "example" / "conf"
        before = configuration.read_bytes(), configuration.stat().st_mtime_ns
        for index, change in enumerate(("session", "name", "document"), start=1):
            with self.subTest(change=change):
                if change == "session":
                    owner = staff.update_current_session(owner.current_session, current)
                    self.record(current)
                elif change == "name":
                    owner = staff.rename(owner.id, "renamed-demo")
                else:
                    moved = doc.with_name("moved-production.md")
                    _ = doc.rename(moved)
                    _ = moved.write_text(moved.read_text().replace("every 2 minutes", "every 7 minutes"))
                    assert isinstance(owner.doc, staff.LiveDoc)
                    owner = staff.set_doc(owner.id, staff.LiveDoc(owner.doc.machine, moved))
                _ = self.successful("tick", now=MINUTE + 120 * index)
                arguments = self.send_args.read_text().splitlines()
                self.assertEqual(arguments[arguments.index("--to") + 1], "uds:" + str(self.socket_path))
                self.assertEqual(arguments[arguments.index("--from") + 1], "showrunner-timer-" + owner.name)
                prompt = arguments[arguments.index("--text") + 1:]
                delivered = "\n".join(prompt)
                self.assertIn("--production " + owner.name, delivered)
                self.assertIn("every 7 minutes" if change == "document" else "every 2 minutes", delivered)
                self.assertEqual(self.conf()["TARGET"], "session:" + owner.id)
                self.assertEqual(self.conf()["PROMPT"], "scheduled")
                self.assertNotIn("PROMPT_FILE", self.conf())
                self.assertNotIn("FROM", self.conf())
                self.assertEqual((configuration.read_bytes(), configuration.stat().st_mtime_ns), before)
        retained = staff.by_id(owner.id)
        assert retained is not None
        self.assertEqual(retained.current_session, current)

    def test_tick_drains_repeat_notices_only_from_the_configured_local_state(self) -> None:
        pending = self.root / "repeat-outbox"
        pending.mkdir()
        launched = self.root / "drain.argv"
        launcher = self.launcher_dir / "systemd-run"
        _ = launcher.write_text("#!/usr/bin/env python3\nimport json,sys\nfrom pathlib import Path\n"
            + f"Path({str(launched)!r}).write_text(json.dumps(sys.argv[1:]))\n")
        config = self.root / "selected.conf"
        with mock.patch.dict(os.environ, {"NOTIFIER_REPEAT_STATE": str(pending),
                                         "SEND_REPEAT_CONFIG": str(config), "XDG_STATE_HOME": str(self.root / "xdg")}):
            _ = self.successful("tick")
            _ = self.successful("tick")
        arguments = cast(list[str], json.loads(launched.read_text()))
        self.assertIn("--no-block", arguments)
        self.assertIn("SEND_REPEAT_STATE=" + str(pending), arguments)
        self.assertIn("SEND_REPEAT_CONFIG=" + str(config), arguments)
        self.assertIn("XDG_STATE_HOME=" + str(self.root / "xdg"), arguments)
        self.assertEqual(arguments[-2:], [str(self.fake_send), "repeat-notices"])
        self.assertIn("notices", arguments)
        self.assertTrue(any(value.endswith("/repeat_guard.py") for value in arguments))
        self.assertFalse(self.send_args.exists(), "the tick ran the drain synchronously")

    def record(self, session_id: str, path: Path | None = None, updated: int = 1) -> None:
        record = {
            "pid": os.getpid(), "name": "showrunner", "sessionId": session_id,
            "messagingSocketPath": str(self.socket_path if path is None else path),
            "updatedAt": updated,
        }
        _ = (self.sessions_dir / "live.json").write_text(json.dumps(record))

    def run_cli(self, *args: str, now: int = NOW, send_rc: int = 0) -> subprocess.CompletedProcess[str]:
        owned = OwnedTestProcesses()
        command = ["zsh", str(SCRIPT), *args]
        environment = self.owned.environment({
            **os.environ,
            "NOTIFIER_STATE_DIR": str(self.state_dir),
            "NOTIFIER_SESSIONS_DIR": str(self.sessions_dir),
            "NOTIFIER_SEND": str(self.fake_send),
            "NOTIFIER_NOW_EPOCH": str(now),
            "FAKE_SEND_ARGS": str(self.send_args),
            "FAKE_SEND_RC": str(send_rc),
            "PATH": f"{self.launcher_dir}{os.pathsep}{os.environ['PATH']}",
        })
        if not process_boundary_for_case(self) and "SHOWN_ZONE" not in environment:
            config = environment.get("USER_ZONE_CONF") or str(
                Path(user_zone.__file__).resolve().parents[2] / "config/user_zone.conf")
            if not Path(config).is_absolute():
                config = str(Path.cwd() / config)
            try:
                with mock.patch.object(user_zone, "CONF", config):
                    zone = invoke_cli(user_zone, user_zone.main, [], environment, self.root)
            except Exception:
                # The shell ignores helper failures and uses the machine zone.
                environment["SHOWN_ZONE"] = ""
            else:
                environment["SHOWN_ZONE"] = zone.stdout.rstrip("\n") if zone.returncode == 0 else ""
        child = subprocess.Popen(
            command,
            env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
        owned.track(child, group=True)
        try:
            output, errors = child.communicate(timeout=TEST_HANG_GUARD_SECS)
            return subprocess.CompletedProcess(command, child.wait(), output, errors)
        finally:
            owned.finish()

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

    def wait_for_text(self, path: Path, expected: str, *, observe_launch: bool = False) -> str:
        def ready() -> bool:
            if path.exists() and expected in path.read_text():
                return True
            if observe_launch:
                self.assertTrue(self.launch_pids.exists(), "the fake launcher did not start a child")
                pid = self.launch_pids.read_text().splitlines()[-1]
                status = self.root / ("run-exit-" + pid)
                self.assertFalse(status.exists(), f"child {pid} exited before {path}: "
                                 + (status.read_text() if status.exists() else ""))
            return False

        wait_until(ready, path.parent)
        return path.read_text()

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

    @process_boundary
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

    def test_unpause_restarts_only_a_timer_the_pause_stopped_and_nobody_stopped_since(self) -> None:
        _ = self.new()
        _ = self.successful("pause", "example")
        self.assertEqual((self.state()["ENABLED"], self.state()["PAUSED"]), ("0", "1"))
        self.assertIn("next_due=", self.successful("unpause", "example"))
        self.assertEqual(self.state()["ENABLED"], "1")
        self.assertNotIn("PAUSED", self.state())
        # Stopped during the pause: the stop clears the mark, so unpause leaves it stopped.
        _ = self.successful("pause", "example")
        _ = self.successful("stop", "example")
        self.assertEqual(self.successful("unpause", "example"), "")
        self.assertEqual(self.state()["ENABLED"], "0")
        # Stopped before the pause: the pause has nothing to stop and leaves no mark.
        _ = self.successful("pause", "example")
        self.assertNotIn("PAUSED", self.state())
        self.assertEqual(self.successful("unpause", "example"), "")
        self.assertEqual(self.state()["ENABLED"], "0")
        # Started again during the pause: the start clears the mark, and the timer keeps running.
        _ = self.successful("start", "example")
        _ = self.successful("pause", "example")
        _ = self.successful("resume", "example")
        self.assertNotIn("PAUSED", self.state())
        _ = self.successful("stop", "example")
        self.assertEqual(self.successful("unpause", "example"), "")
        # A tick keeps the mark of a paused timer.
        _ = self.successful("start", "example")
        _ = self.successful("pause", "example")
        _ = self.successful("tick", now=NOW + 600)
        self.assertEqual(self.state()["PAUSED"], "1")

    @process_boundary
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

    def test_scheduled_prompt_fire_ignores_schedule_and_hold(self) -> None:
        owner = self.scheduled_owner()[0]
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"HOME": str(self.root / "home")})))
        _ = self.successful("new", "example", "--to", "session:" + owner.id, "--every", "2",
                            "--prompt", "scheduled", "--hold")
        configuration = self.state_dir / "example" / "conf"
        with configuration.open("a") as output:
            _ = output.write("FROM=showrunner-timer-old-name\n")
        before = configuration.read_bytes()
        scripts = self.root / "scripts"
        (scripts / "message").mkdir(parents=True)
        (scripts / "lib").mkdir()
        notifier = scripts / "message/notifier.sh"
        _ = notifier.write_text(SCRIPT.read_text())
        calls = self.root / "delivery.calls"
        python = scripts / "lib/py"
        source = SCRIPT.parents[1]
        _ = python.write_text(
            "#!/bin/sh\n"
            + 'case "$1" in */production/update_registration.py) '
            + f'printf "%s\\n" "$2" >> {shlex.quote(str(calls))} ;; esac\n'
            + f'script=${{1#{shlex.quote(str(scripts))}/}}\nshift\n'
            + f'exec {shlex.quote(str(source / "lib/py"))} {shlex.quote(str(source))}/"$script" "$@"\n'
        )
        python.chmod(0o755)
        with mock.patch(__name__ + ".SCRIPT", notifier):
            _ = self.successful("fire", "example")
        self.assertEqual(calls.read_text().splitlines(), ["delivery"])
        arguments = self.send_args.read_text().splitlines()
        self.assertIn("--text", arguments)
        self.assertEqual(arguments[arguments.index("--from") + 1], "showrunner-timer-" + owner.name)
        self.assertIn("--production " + owner.name, "\n".join(arguments[arguments.index("--text") + 1:]))
        self.assertEqual("\n".join(arguments[arguments.index("--text") + 1:]),
                         scheduled_prompt(2, read_production(owner.id)))
        self.assertEqual(self.state()["NEXT_DUE"], str(MINUTE + 120))
        with mock.patch(__name__ + ".SCRIPT", notifier):
            _ = self.successful("fire", "example", now=NOW + 1)
        self.assertEqual(calls.read_text().splitlines(), ["delivery", "delivery"])
        self.assertEqual(len([line for line in self.lines() if " | exit 0 | " in line]), 2)
        self.assertEqual(configuration.read_bytes(), before)

    def test_failed_scheduled_prompt_skips_delivery_with_one_error_line(self) -> None:
        owner, doc = self.scheduled_owner()
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"HOME": str(self.root / "home")})))
        _ = self.successful("new", "example", "--to", "session:" + owner.id, "--every", "2",
                            "--prompt", "scheduled")
        document = doc.read_bytes()
        doc.unlink()
        result = self.run_cli("tick", now=MINUTE + 120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertIn("prompt", result.stderr)
        self.assertFalse(self.send_args.exists())
        _ = doc.write_bytes(document)
        scripts = self.root / "scripts"
        (scripts / "message").mkdir(parents=True)
        (scripts / "production").mkdir()
        (scripts / "lib").mkdir()
        notifier = scripts / "message/notifier.sh"
        _ = notifier.write_text(SCRIPT.read_text())
        source = SCRIPT.parents[1]
        _ = shutil.copyfile(source / "production/update_registration.py", scripts / "production/update_registration.py")
        (scripts / "message/sessions.py").symlink_to(source / "message/sessions.py")
        template = self.root / "commands/showrunner/produce.md"
        template.parent.mkdir(parents=True)
        _ = template.write_text("The prompt:\n\n> \n>\n\n**A tick**\n")
        copy_runtime(self.root)
        python = scripts / "lib/py"
        wrapper = ("#!/bin/sh\n"
                   + f'exec {shlex.quote(str(source / "lib/py"))} "$@"\n')
        _ = python.write_text(wrapper)
        python.chmod(0o755)
        for defect in ("empty template", "missing separator", "empty framed prompt", "old command"):
            with self.subTest(defect=defect):
                self.send_args.unlink(missing_ok=True)
                if defect != "empty template":
                    output = "showrunner-timer-demo" + ("\n\n" if defect == "empty framed prompt" else "")
                    response = ("printf '%s\\n' 'invalid choice: delivery' >&2; exit 2" if defect == "old command"
                                else f"printf '%s' {shlex.quote(output)}; exit 0")
                    _ = python.write_text(
                        "#!/bin/sh\n"
                        + 'case "$1" in */production/update_registration.py) '
                        + response + " ;; esac\n"
                        + wrapper.removeprefix("#!/bin/sh\n")
                    )
                with mock.patch(__name__ + ".SCRIPT", notifier):
                    result = self.run_cli("fire", "example")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
                self.assertIn("prompt", result.stderr)
                self.assertFalse(self.send_args.exists())
                if defect == "old command":
                    self.assertIn("invalid choice: delivery", result.stderr)

    def test_check_exit_one_skips_and_exit_two_removes(self) -> None:
        _ = self.new("--check", "/bin/sh -c 'exit 1'")
        _ = self.successful("tick", now=MINUTE + 120)
        self.assertIn(" | skip check exit 1", self.lines()[-1])
        self.assertFalse(self.send_args.exists())
        _ = self.new("--check", "/bin/sh -c 'exit 2'")
        _ = self.successful("fire", "example")
        self.assertFalse((self.state_dir / "example").exists())
        self.assertIn("removed example (check exit 2)", (self.state_dir / "notifier.log").read_text())

    def test_check_timeout_skips_send_and_releases_tick(self) -> None:
        check = self.root / "slow-check"
        blocked = self.root / "check-blocked"
        os.mkfifo(blocked)
        _ = check.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$$\" >> {self.blocked_pids}\nexec /bin/cat < {blocked}\n")
        check.chmod(0o755)
        _ = self.new("--check", str(check), "--timeout", "1")
        result = self.run_cli("tick", now=MINUTE + 120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(" | skip check timeout", self.lines()[-1], result.stderr)
        self.assertFalse(self.send_args.exists())

    def test_empty_prompt_sources_do_not_create_an_instance(self) -> None:
        for source in ("--command", "--prompt"):
            with self.subTest(source=source):
                result = self.run_cli(
                    "new", "example", "--to", "session:sid-1", "--every", "2", source, "",
                )
                self.assertEqual(result.returncode, 2)
                self.assertFalse((self.state_dir / "example").exists())

    def test_tick_runs_clear_check_only_while_emergencies_are_recorded(self) -> None:
        emergencies = self.root / "emergencies.json"
        with mock.patch.dict(os.environ, {"NOTIFIER_EMERGENCIES": str(emergencies)}):
            _ = self.successful("tick")
            self.assertFalse(self.send_args.exists())
            _ = emergencies.write_text('[{"key": "github-cold"}]\n')
            _ = self.successful("tick")
            self.assertEqual(self.send_args.read_text(), "clear-check\n")
            self.assertFalse((self.state_dir / "notifier.log").exists())
            _ = self.successful("tick", send_rc=3)
        self.assertIn("clear-check: fake send result", (self.state_dir / "notifier.log").read_text())

    def test_tick_on_another_notifier_state_never_reads_the_real_emergencies(self) -> None:
        state = self.root / "xdg"
        (state / "message").mkdir(parents=True)
        _ = (state / "message" / "emergencies.json").write_text('[{"key": "github-cold"}]\n')
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(state)}):
            _ = self.successful("tick")
        self.assertFalse(self.send_args.exists())

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
            env=self.owned.environment({
                **os.environ,
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "NOTIFIER_STATE_DIR": str(self.state_dir),
                "NOTIFIER_SESSIONS_DIR": str(self.sessions_dir),
                "NOTIFIER_SEND": str(self.fake_send),
                "NOTIFIER_NOW_EPOCH": str(NOW),
                "FAKE_SEND_ARGS": str(self.send_args),
                "FAKE_SEND_RC": "0",
            }),
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(mkdir_marker.exists())

    def test_unreadable_session_records_are_logged_as_a_failed_lookup(self) -> None:
        _ = self.new()
        self.live_socket.close()
        self.socket_path.unlink()
        _ = (self.sessions_dir / "broken.json").write_text("{ not json")
        _ = self.successful("fire", "example")
        self.assertIn(" | skip session lookup exit 3", self.lines()[-1])
        self.assertFalse(self.send_args.exists())

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
        new_path = Path(self.enterContext(short_socket_directory())) / "replacement.sock"
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
        blocked = self.root / "run-blocked"
        os.mkfifo(blocked)
        _ = slow.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$$\" >> {self.blocked_pids}\nexec /bin/cat < {blocked}\n")
        slow.chmod(0o755)
        _ = self.successful("new", "example", "--every", "1", "--run", str(slow), "--timeout", "1")
        _ = self.successful("tick", now=MINUTE + 120)
        _ = self.wait_for_text(self.state_dir / "example" / "fire.log", "run timeout")
        self.assertTrue(any("timeout" in line for line in self.lines()))
        self.assertFalse(self.send_args.exists())

    def test_run_marker_wait_reports_an_exited_child(self) -> None:
        job = self.root / "job"
        _ = job.write_text("#!/bin/sh\nexit 9\n")
        job.chmod(0o755)
        _ = self.successful("new", "example", "--every", "1", "--run", str(job))
        _ = self.successful("tick", now=MINUTE + 60)
        with self.assertRaisesRegex(AssertionError, "exited before"):
            _ = self.wait_for_text(self.root / "missing-marker", "started", observe_launch=True)

    def test_run_marker_wait_reports_a_missing_child(self) -> None:
        with self.assertRaisesRegex(AssertionError, "did not start a child"):
            _ = self.wait_for_text(self.root / "missing-marker", "started", observe_launch=True)

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
            env=self.owned.environment({
                **os.environ,
                "PATH": f"{self.launcher_dir}{os.pathsep}{os.environ['PATH']}",
                "NOTIFIER_STATE_DIR": str(self.state_dir),
                "NOTIFIER_SESSIONS_DIR": str(self.sessions_dir),
                "NOTIFIER_NOW_EPOCH": str(MINUTE + 60),
            }),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
        self.owned.track(tick, group=True)
        try:
            _, stderr = tick.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(tick.returncode, 0, stderr)
            _ = self.wait_for_text(started, "started", observe_launch=True)
            self.assertFalse(finished.exists())
        finally:
            if tick.poll() is None:
                tick.kill()
            _ = tick.wait(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(self.wait_for_text(finished, "finished"), "finished")
        _ = self.wait_for_text(self.state_dir / "example" / "fire.log", "run exit 7")

    def fake_launchctl(self) -> Path:
        """Install a fake launchctl that, like macOS 27, starts a submitted job only on kickstart."""
        launcher = self.launcher_dir / "launchctl"
        _ = launcher.write_text(
            """#!/usr/bin/env python3
import json
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
    (state / f"{label}.command").write_text(json.dumps(sys.argv[sys.argv.index("--") + 1:]))
elif action == "kickstart":
    target = sys.argv[2]
    (state / "kickstarted").write_text(target)
    kickstart_rc = int(os.environ["FAKE_LAUNCHCTL_KICKSTART_RC"])
    if kickstart_rc:
        sys.exit(kickstart_rc)
    label = target.split("/", 2)[2]
    command = json.loads((state / f"{label}.command").read_text())
    job = subprocess.Popen(
        [sys.executable, __file__, "loop", label, *command],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True,
    )
    (state / "pid.tmp").write_text(str(job.pid))
    os.replace(state / "pid.tmp", state / "pid")
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

        self.owned.group_markers.append(launch_state / "pid")
        return launch_state

    def tick_mac_run_only(self, launch_state: Path, runs: Path, kickstart_rc: int = 0) -> Path:
        fire_log = self.state_dir / "example" / "fire.log"
        with mock.patch.dict(os.environ, {
            "NOTIFIER_PLATFORM": "darwin",
            "FAKE_LAUNCHCTL_STATE": str(launch_state),
            "FAKE_LAUNCHCTL_FIRE": str(fire_log),
            "FAKE_LAUNCHCTL_KICKSTART_RC": str(kickstart_rc),
        }):
            _ = self.successful(
                "new", "example", "--every", "1", "--run",
                f"/bin/sh -c 'printf run >> {runs}; exit 7'",
            )
            _ = self.successful("tick", now=MINUTE + 60)
        return fire_log

    def test_mac_run_only_job_removes_label_after_fire_log_and_runs_once(self) -> None:
        launch_state = self.fake_launchctl()
        runs = self.root / "mac-runs"
        fire_log = self.tick_mac_run_only(launch_state, runs)
        self.assertEqual(self.wait_for_text(fire_log, "run exit 7").count("run exit 7"), 1)
        self.assertEqual(self.wait_for_text(runs, "run"), "run")
        label = (launch_state / "submitted").read_text()
        self.assertEqual((launch_state / "kickstarted").read_text(), f"gui/{os.getuid()}/{label}")
        removed = launch_state / f"{label}.removed"
        self.assertEqual(self.wait_for_text(removed, "after-fire"), "after-fire")
        self.assertEqual(runs.read_text(), "run")

    def test_mac_run_only_failed_kickstart_removes_label_and_runs_after_tick(self) -> None:
        launch_state = self.fake_launchctl()
        runs = self.root / "mac-runs"
        fire_log = self.tick_mac_run_only(launch_state, runs, kickstart_rc=1)
        label = (launch_state / "submitted").read_text()
        self.assertEqual((launch_state / f"{label}.removed").read_text(), "before-fire")
        self.assertIn(
            "run launcher unavailable: example; waiting after tick",
            (self.state_dir / "notifier.log").read_text(),
        )
        self.assertEqual(fire_log.read_text().count("run exit 7"), 1)
        self.assertEqual(runs.read_text(), "run")
        self.assertFalse((launch_state / "pid").exists())

    def test_run_only_long_run_does_not_hold_tick_or_other_due_instance(self) -> None:
        started_file = self.root / "started"
        finished_file = self.root / "finished"
        slow = self.root / "slow-run"
        release_file = self.root / "release-run"
        _ = slow.write_text(
            f"#!/bin/sh\nprintf started > {started_file}\n"
            + f"while [ ! -e {release_file} ]; do sleep 0.01; done\n"
            + f"printf finished > {finished_file}\nexit 7\n"
        )
        slow.chmod(0o755)
        _ = self.successful("new", "example", "--every", "1", "--run", str(slow), "--timeout", "30")
        _ = self.successful("new", "other", "--to", "session:sid-1", "--every", "1", "--command", "due")
        _ = self.successful("tick", now=MINUTE + 60)
        _ = self.wait_for_text(started_file, "started")
        self.assertFalse(finished_file.exists())
        self.assertTrue(self.send_args.exists())
        _ = release_file.write_text("release")
        _ = self.wait_for_text(finished_file, "finished")
        _ = self.wait_for_text(self.state_dir / "example" / "fire.log", "run exit 7")

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
