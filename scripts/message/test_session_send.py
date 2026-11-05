"""Test the sender command and the process guard without live delivery."""
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
from scripts.message import session_send
from scripts.message.session_send_test_support import forbid_real_sender


def setUpModule() -> None:
    forbid_real_sender()


GUARD_PROBE = r'''
import os
from pathlib import Path
import subprocess
import sys
from unittest import mock
from scripts.message import session_send
from scripts.message.session_send_test_support import forbid_real_sender

real = session_send.SEND_SCRIPT.resolve()
with mock.patch.object(sys, "addaudithook", wraps=sys.addaudithook) as install:
    forbid_real_sender()
    forbid_real_sender()
    assert install.call_count == 1, install.call_count

# This second hook prevents a live process if the tested hook fails.
def last_guard(event, arguments):
    if event != "subprocess.Popen":
        return
    directory = Path(arguments[2]) if arguments[2] is not None else Path.cwd()
    for value in arguments[1]:
        if not isinstance(value, (str, bytes, os.PathLike)):
            continue
        path = Path(os.fsdecode(value))
        if not path.is_absolute():
            path = directory / path
        if path.resolve() == real:
            raise RuntimeError("the tested guard allowed the real sender")
sys.addaudithook(last_guard)

fake = Path(sys.argv[1])
alias = fake.with_name("sender-link.py")
alias.symlink_to(real)
for command, directory in (
    ([sys.executable, str(real), "--help"], None),
    ([sys.executable, real, "--help"], None),
    ([sys.executable, os.fsencode(real), "--help"], None),
    ([sys.executable, "send.py", "--help"], real.parent),
    ([sys.executable, str(alias), "--help"], None),
):
    try:
        subprocess.run(command, cwd=directory, check=True)
    except AssertionError as error:
        assert "real session sender forbidden" in str(error), str(error)
    else:
        raise AssertionError("the real sender was not blocked")

result = subprocess.run([sys.executable, str(fake)], capture_output=True, text=True, check=True)
assert result.stdout == "fake sender ran\n", result.stdout
print("Real sender blocked; fake sender ran; hook installed once.")
'''


class SessionSendTests(unittest.TestCase):
    def test_guard_blocks_real_paths_and_allows_a_fake_sender_in_a_child(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sender = Path(directory) / "send.py"
            _ = sender.write_text("print('fake sender ran')\n")
            result = subprocess.run(
                [sys.executable, "-c", GUARD_PROBE, str(sender)],
                cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Real sender blocked; fake sender ran; hook installed once.", result.stdout)

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
