"""Keep live owners and check zombie leaders before group cleanup."""
from __future__ import annotations

from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from scripts.direct.verify_token_wait_test_support import BOARD
from scripts.direct import step_record
from scripts.lib import process_start as identity
from scripts.lib.shell_test_support import shell_function
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class BoardProcessStartTests(unittest.TestCase):
    def test_expired_owner_policy_distinguishes_unreadable_gone_and_pid_reuse(self) -> None:
        for state, expected in (("live", 1), ("unreadable", 1), ("gone", 0), ("reused", 0)):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                lock = root / "locks/cargo.d"
                lock.mkdir(parents=True)
                _ = (lock / "holder").write_text("first")
                _ = (lock / "holder_pid").write_text(str(os.getpid()))
                _ = (lock / "expires").write_text("1")
                _ = (lock / "owned").write_text(json.dumps({"group": 4242, "start": "123", "start_format": identity.process_start_format()}))
                proc = root / "proc/4242"
                proc.mkdir(parents=True)
                if state == "unreadable":
                    _ = (proc / "stat").write_text("broken")
                elif state != "gone":
                    _ = (proc / "stat").write_text("4242 (step) S " + "0 " * 18 + ("456" if state == "reused" else "123"))
                result = subprocess.run(["bash", str(BOARD), "acquire", str(root), "second", "cargo", "--wait", "0"],
                    env={**os.environ, "VERIFY_PROC_ROOT": str(root / "proc")},
                    capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS, check=False)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                self.assertEqual((lock / "holder").read_text(), "first" if expected else "second")

    def test_darwin_owned_format_controls_retention_and_cleanup(self) -> None:
        source = BOARD.read_text()
        canonical = "Wed Nov  4 22:00:00 2026"
        local = "Wed Nov  4 17:00:00 2026"
        cases = (("legacy", "", local, 0, 1),
                 ("marked", "darwin-lstart-utc-c-v1", canonical, 0, 0),
                 ("legacy_zone_collision", "", canonical, 0, 1),
                 ("reused_across_zones", "darwin-lstart-utc-c-v1", local, 1, 1),
                 ("unknown_format", "future-format", canonical, 0, 1))
        for name, marker, saved_start, keep_status, cleanup_status in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as directory:
                record = {"group": 4242, "start": saved_start}
                if marker:
                    record["start_format"] = marker
                path = Path(directory) / "owned"
                _ = path.write_text(json.dumps(record))
                for function, delimiter, expected in (("owned_group_matches", "PYTHON", keep_status),
                                                       ("cleanup_owned_group", "PY", cleanup_status)):
                    body = shell_function(source, function).split("<<'" + delimiter + "'\n", 1)[1].rsplit("\n" + delimiter, 1)[0]
                    replies = [subprocess.CompletedProcess[str]([], 0, canonical + " S\n", ""),
                               subprocess.CompletedProcess[str]([], 0, "", "")]
                    errors = io.StringIO()
                    with mock.patch.object(sys, "platform", "darwin"), \
                         mock.patch.object(sys, "argv", ["-", str(path), str(BOARD.parent.parent / "lib")]), \
                         mock.patch.object(subprocess, "run", side_effect=replies) as run, \
                         mock.patch.object(subprocess, "check_output", return_value="4242 S\n" if expected else ""), \
                         redirect_stderr(errors):
                        try:
                            exec(compile(body, str(BOARD), "exec"), {})
                        except SystemExit as error:
                            status = error.code
                        else:
                            status = 0
                    self.assertEqual(status, expected, errors.getvalue())
                    expected_queries = (0 if function == "cleanup_owned_group" and marker != "darwin-lstart-utc-c-v1"
                                        else 2 if function == "cleanup_owned_group" and not expected else 1)
                    self.assertEqual(run.call_count, expected_queries)
                    if expected_queries:
                        self.assertEqual(run.call_args_list[0].kwargs["env"]["TZ"], "UTC")

    @unittest.skipUnless(sys.platform == "linux", "Linux exposes zombie state through proc")
    def test_zombie_leader_matches_for_cleanup_but_is_stale_for_step_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ready = root / "ready"
            helper = '''import os, signal, sys
from pathlib import Path
child = os.fork()
if child:
    Path(sys.argv[1]).write_text(str(child))
    os._exit(0)
signal.pause()
'''
            leader = subprocess.Popen([sys.executable, "-c", helper, str(ready)], start_new_session=True)
            try:
                # Observe the leader's exit without reaping it.
                _ = os.waitid(os.P_PID, leader.pid, os.WEXITED | os.WNOWAIT)
                start = identity.process_start(leader.pid)
                self.assertIs(type(start), identity.Zombie)
                assert isinstance(start, identity.Zombie)
                lock = root / "locks/cargo.d"
                lock.mkdir(parents=True)
                _ = (lock / "holder").write_text("first")
                _ = (lock / "holder_pid").write_text(str(os.getpid()))
                _ = (lock / "owned").write_text(json.dumps({"group": leader.pid, "start": start.start, "start_format": identity.process_start_format()}))
                with mock.patch.dict(os.environ, {"VERIFY_STEPS_DIR": str(root / "steps")}):
                    step_record.write(step_record.StepRecord("zombie", leader.pid, start.start, root, "check", None,
                        step_record.MacAllowed(), step_record.StepState.ON_NATEDEV, "now"))
                    self.assertIs(type(step_record.read("zombie")), step_record.StaleRecord)
                result = subprocess.run(["bash", str(BOARD), "release", str(root), "first", "cargo", "--pid", str(os.getpid())],
                    capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS, check=False)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(lock.exists())
                self.assertNotIsInstance(identity.process_start(int(ready.read_text())), identity.Started)
            finally:
                try:
                    os.killpg(leader.pid, 9)
                except ProcessLookupError:
                    pass
                _ = leader.wait(timeout=TEST_HANG_GUARD_SECS)
