"""Test the sender command and its refusal without live delivery."""
from __future__ import annotations

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

session_send_test_support.forbid_real_sender()
installed_marker = session_send_test_support.refusal_marker
assert isinstance(installed_marker, session_send_test_support.SenderRefusalMarker)
session_send_test_support.forbid_real_sender()
assert session_send_test_support.refusal_marker is installed_marker
marker = installed_marker.path
real = Path(session_send.__file__).resolve().with_name("send.py")
fake = Path(sys.argv[1])
mode = sys.argv[2]
alias = fake.with_name("sender-link.py")
alias.symlink_to(real)

# This safety hook permits only harmless probes and blocks network delivery.
def last_guard(event, arguments):
    if event in ("socket.connect", "socket.sendto", "socket.bind"):
        raise RuntimeError("the sender probe attempted network access")
    if event in ("subprocess.Popen", "os.posix_spawn", "os.exec"):
        command = arguments[1]
    elif event == "os.spawn":
        command = arguments[2]
    elif event == "os.system":
        command = arguments[0]
    else:
        return
    if str(fake) not in str(command) and "--help" not in str(command):
        raise RuntimeError("the sender probe attempted a live command")
sys.addaudithook(last_guard)

class SenderRequest(unittest.TestCase):
    def test_sender(self):
        if mode == "fake":
            with mock.patch.object(session_send, "SEND_SCRIPT", fake):
                result = subprocess.run(session_send.session_send_argv("fixture"),
                                        capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout, "fake sender ran\n")
            self.assertFalse(marker.exists())
            return
        command = shlex.join([sys.executable, str(real), "--help"])
        if mode == "posix_spawn":
            pid = os.posix_spawn(sys.executable, [sys.executable, str(real), "--help"], os.environ)
            _, status = os.waitpid(pid, 0)
            self.assertNotEqual(os.waitstatus_to_exitcode(status), 0)
        elif mode == "shell":
            result = subprocess.run(command, shell=True, capture_output=True, text=True)
            self.check_refusal(result)
        elif mode == "system":
            self.assertNotEqual(os.waitstatus_to_exitcode(os.system(command)), 0)
        elif mode == "cd":
            result = subprocess.run(
                f"cd {shlex.quote(str(real.parent))} && {shlex.quote(sys.executable)} send.py --help",
                shell=True, capture_output=True, text=True)
            self.check_refusal(result)
        elif mode == "module":
            result = subprocess.run([sys.executable, "-m", "scripts.message.send", "--help"],
                                    capture_output=True, text=True)
            self.check_refusal(result)
        elif mode == "grandchild":
            source = "import subprocess,sys; raise SystemExit(subprocess.call(sys.argv[1:]))"
            result = subprocess.run([sys.executable, "-c", source, sys.executable, str(real), "--help"],
                                    capture_output=True, text=True)
            self.check_refusal(result)
        elif mode == "real":
            for command, directory in (
                ([sys.executable, str(real), "--help"], None),
                ([sys.executable, real, "--help"], None),
                ([sys.executable, os.fsencode(real), "--help"], None),
                ([sys.executable, "send.py", "--help"], real.parent),
                ([sys.executable, str(alias), "--help"], None),
            ):
                with self.subTest(command=command):
                    result = subprocess.run(command, cwd=directory, capture_output=True, text=True)
                    self.check_refusal(result)
        else:
            with self.assertRaises(subprocess.CalledProcessError):
                subprocess.run([sys.executable, str(real), "--help"], check=True)
            refused = marker.read_text()
            with mock.patch.object(session_send, "SEND_SCRIPT", fake):
                result = subprocess.run(session_send.session_send_argv("fixture"),
                                        capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout, "fake sender ran\n")
            self.assertEqual(marker.read_text(), refused)
        self.assertEqual(marker.read_text(), "real sender refused\n")

    def check_refusal(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "REFUSED: real scripts/message/send.py is forbidden in tests\n")

result = unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromTestCase(SenderRequest))
assert isinstance(session_send_test_support.refusal_marker, session_send_test_support.SenderGuardNotInstalled)
assert os.environ["TEST_SENDER_GUARD"] == str(marker)
if mode != "fake":
    assert marker.read_text() == "real sender refused\n"
raise SystemExit(not result.wasSuccessful())
'''


class SessionSendTests(unittest.TestCase):
    def probe(self, mode: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sender = root / "send.py"
            _ = sender.write_text("print('fake sender ran')\n")
            _ = (root / "lint.conf").write_text("# range: 1..\nsweep_sender_timeout_seconds=17\n")
            startup = root / "startup"
            startup.mkdir()
            helper = Path(__file__).resolve().parents[1] / "lib/python_startup/sitecustomize.py"
            _ = (startup / "sitecustomize.py").write_text(
                "import runpy, sys\n" + f"startup = runpy.run_path({str(helper)!r})\n"
                + "startup['select_checkout'](sys.argv)\n"
                + "startup['next_sitecustomize'](__file__)\n"
                + "startup['finish_startup'](__file__, sys.argv)\n")
            environment = {**os.environ, "HOME": str(root / "home"), "CONF_DIR": str(root),
                           "BUILDLOG_DIR": str(root / "buildlog"),
                           "TEST_SENDER_GUARD": str(root / "refused"),
                           "PYTHONPATH": os.pathsep.join((str(startup), os.environ.get("PYTHONPATH", "")))}
            return subprocess.run(
                [str(Path(__file__).resolve().parents[2] / "scripts/lib/py"), "-c", GUARD_PROBE, str(sender), mode],
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

    def test_sender_from_its_own_folder_is_refused(self) -> None:
        result = self.probe("cd")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_sender_as_a_module_is_refused(self) -> None:
        result = self.probe("module")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_grandchild_sender_is_refused(self) -> None:
        result = self.probe("grandchild")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_fake_sender_runs_under_unittest(self) -> None:
        result = self.probe("fake")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_caught_refusal_keeps_the_marker_after_a_fake_send(self) -> None:
        result = self.probe("caught")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_general_command_keeps_arguments_and_accepts_a_sender_override(self) -> None:
        arguments = ("retire", "--to", "session:fixture", "--key", "fixture key")
        for sender in (session_send.SEND_SCRIPT, Path("/fixture/sender.py")):
            with self.subTest(sender=sender):
                self.assertEqual(session_send.send_argv(*arguments, send_script=sender),
                                 [sys.executable, str(sender), *arguments])
        self.assertEqual(session_send.send_argv(*arguments),
                         [sys.executable, str(session_send.SEND_SCRIPT), *arguments])
        self.assertEqual(session_send.send_argv(*arguments, python_interpreter=session_send.PYTHON_LAUNCHER),
                         [str(session_send.PYTHON_LAUNCHER), str(session_send.SEND_SCRIPT), *arguments])

    def test_general_command_reads_the_current_sender_at_call_time(self) -> None:
        sender = Path("/fixture/patched-send.py")
        with mock.patch.object(session_send, "SEND_SCRIPT", sender):
            command = session_send.send_argv("x")
        self.assertEqual(command, [sys.executable, str(sender), "x"])

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
