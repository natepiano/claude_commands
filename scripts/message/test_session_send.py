"""Test the sender command and its refusal without live delivery."""
from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from scripts.lib import conf
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.message import session_send
from scripts.message.session_send_test_support import forbid_real_sender


def setUpModule() -> None:
    forbid_real_sender()


GUARD_PROBE = r'''
import os
from pathlib import Path
import shlex
import subprocess
import sys
import unittest
from unittest import mock
from scripts.message import session_send, session_send_test_support

with mock.patch.object(sys, "addaudithook", wraps=sys.addaudithook) as install:
    session_send_test_support.forbid_real_sender()
    session_send_test_support.forbid_real_sender()
    assert install.call_count == 1, install.call_count

real = Path(session_send.__file__).resolve().with_name("send.py")

# This second hook stops a live process if the tested guard fails.
def last_guard(event, arguments):
    if event not in ("subprocess.Popen", "os.posix_spawn", "os.exec", "os.spawn", "os.system"):
        return
    if event in ("subprocess.Popen", "os.posix_spawn", "os.exec") and isinstance(arguments[1], (list, tuple)):
        if str(fake) in arguments[1]:
            return
    raise RuntimeError("the tested guard allowed the real sender")
sys.addaudithook(last_guard)

fake = Path(sys.argv[1])
mode = sys.argv[2]
alias = fake.with_name("sender-link.py")
alias.symlink_to(real)

class SenderRequest(unittest.TestCase):
    def test_sender(self):
        if mode == "fake":
            with mock.patch.object(session_send, "SEND_SCRIPT", fake):
                result = subprocess.run(session_send.session_send_argv("fixture"),
                                        capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout, "fake sender ran\n")
            self.assertIsInstance(session_send_test_support.sender_violation, session_send_test_support.NoSenderViolation)
            return
        command = shlex.join([sys.executable, str(real), "--help"])
        if mode == "posix_spawn":
            with self.assertRaises(session_send_test_support.RealSenderForbidden):
                os.posix_spawn(sys.executable, [sys.executable, str(real), "--help"], os.environ)
            return
        if mode == "shell":
            with self.assertRaises(session_send_test_support.RealSenderForbidden):
                subprocess.run(command, shell=True, check=True)
            return
        if mode == "system":
            with self.assertRaises(session_send_test_support.RealSenderForbidden):
                os.system(command)
            return
        if mode == "real":
            for command, directory in (
                ([sys.executable, str(real), "--help"], None),
                ([sys.executable, real, "--help"], None),
                ([sys.executable, os.fsencode(real), "--help"], None),
                ([sys.executable, "send.py", "--help"], real.parent),
                ([sys.executable, str(alias), "--help"], None),
            ):
                with self.subTest(command=command):
                    with self.assertRaises(session_send_test_support.RealSenderForbidden):
                        subprocess.run(command, cwd=directory, check=True)
            return
        with self.assertRaises(session_send_test_support.RealSenderForbidden):
            subprocess.run([sys.executable, str(real), "--help"], check=True)
        refused = session_send_test_support.sender_violation
        self.assertIsInstance(refused, session_send_test_support.RealSenderRefused)
        self.assertEqual(refused.test_id, self.id())
        with mock.patch.object(session_send, "SEND_SCRIPT", fake):
            result = subprocess.run(session_send.session_send_argv("fixture"),
                                    capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout, "fake sender ran\n")
        self.assertIs(session_send_test_support.sender_violation, refused)

result = unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromTestCase(SenderRequest))
raise SystemExit(not result.wasSuccessful())
'''


class SessionSendTests(unittest.TestCase):
    def probe(self, mode: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sender = root / "send.py"
            _ = sender.write_text("print('fake sender ran')\n")
            _ = (root / "lint.conf").write_text("# range: 1..\nsweep_sender_timeout_seconds=17\n")
            environment = {**os.environ, "HOME": str(root / "home"), "CONF_DIR": str(root),
                           "BUILDLOG_DIR": str(root / "buildlog")}
            return subprocess.run(
                [sys.executable, "-c", GUARD_PROBE, str(sender), mode],
                cwd=Path(__file__).resolve().parents[2], env=environment,
                capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS,
            )

    def test_real_sender_is_refused_under_unittest(self) -> None:
        result = self.probe("real")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_posix_spawn_refuses_the_real_sender(self) -> None:
        result = self.probe("posix_spawn")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_shell_command_refuses_the_real_sender(self) -> None:
        result = self.probe("shell")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_system_command_refuses_the_real_sender(self) -> None:
        result = self.probe("system")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_fake_sender_runs_under_unittest(self) -> None:
        result = self.probe("fake")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_caught_refusal_keeps_the_requesting_test_after_a_fake_send(self) -> None:
        result = self.probe("caught")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_sender_callers_install_the_guard_in_set_up_module(self) -> None:
        scripts = Path(__file__).resolve().parents[1]
        targets = {"session_send_argv", "send_holder_message", "send_floor_alert", "deliver_reading_alert"}
        missing: list[str] = []
        for path in sorted(scripts.rglob("*.py")):
            if not (path.name.startswith("test_") or path.name.endswith("_test.py")):
                continue
            if path == scripts / "message/test_send.py":
                continue
            module = ast.parse(path.read_text(), filename=str(path))
            if not any((isinstance(node, ast.Name) and node.id in targets)
                       or (isinstance(node, ast.Attribute) and node.attr in targets)
                       for node in ast.walk(module)):
                continue
            guarded = any(
                isinstance(node, ast.FunctionDef) and node.name == "setUpModule"
                and any(isinstance(call, ast.Call)
                        and ((isinstance(call.func, ast.Name) and call.func.id == "forbid_real_sender")
                             or (isinstance(call.func, ast.Attribute) and call.func.attr == "forbid_real_sender"))
                        for call in ast.walk(node))
                for node in module.body
            )
            if not guarded:
                missing.append(str(path.relative_to(scripts)))
        self.assertEqual(missing, [], "Sender tests must install forbid_real_sender in setUpModule")

    def test_command_uses_the_selected_sender_and_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sender = root / "message/send.py"
            settings = root / "lint.conf"
            _ = settings.write_text("# range: 1..\nsweep_sender_timeout_seconds=17\n")
            with (mock.patch.object(session_send, "SEND_SCRIPT", sender),
                  mock.patch.dict(os.environ, {"CONF_DIR": directory})):
                command = session_send.session_send_argv("natedev", sender="build-admission", lint_settings=conf.read("lint"))
                default_command = session_send.session_send_argv("natedev")
        self.assertEqual(command, [sys.executable, str(sender), "--to", "natedev", "--from", "build-admission", "--timeout", "17"])
        self.assertEqual(default_command, [sys.executable, str(sender), "--to", "natedev", "--from", "disk_floor", "--timeout", "17"])


if __name__ == "__main__":
    _ = unittest.main()
