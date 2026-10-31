from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast, final, override
from unittest import mock

import pane_record  # first: it puts the whoami scripts on the path
import account  # noqa: E402  Imported before a test sets the platform to the Mac: urllib checks it on import.

HOOK = Path(__file__).resolve().parent.parent / "hooks/session-start-pane-record.py"
BASH = shutil.which("bash") or "/bin/sh"
# Runs the hook below itself, as Claude runs a hook; started as `claude` with Claude's flags. The
# interpreter is named outright: a process called `claude` finds the real Claude through PATH as
# its sys.executable, and a test must never start it.
RUNNER = """\
import subprocess, sys
payload = sys.stdin.read()
sys.exit(subprocess.run([sys.argv[1], sys.argv[2]], input=payload, text=True).returncode)
"""


@final
class PaneRecordTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.records = Path()
        self.env: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.records = self.root / "herdr-panes"
        _ = (self.root / "runner.py").write_text(RUNNER)
        self.env = {
            # No PATH: nothing the test starts can find a real claude.
            "PATH": "", "HOME": str(self.root),
            "HERDR_PANE_RECORD_DIR": str(self.records), "HERDR_ENV": "1", "HERDR_SESSION": "agents",
            "CLAUDE_CONFIG_DIR": str(self.root / ".claude3"), "ENABLE_TOOL_SEARCH": "true",
            "SHOWRUNNER_UNIT": "demo", "SHOWRUNNER_UNIT_ID": "alpha-unit",
            "CLAUDE_CODE_OAUTH_TOKEN": "secret", "UNRELATED": "x",
        }

    def start(self, pane: str, session_id: str, *flags: str) -> subprocess.CompletedProcess[str]:
        """The hook under a process whose argv is `claude <flags>`, as a pane's Claude runs it."""
        command = [BASH, "-c", 'exec -a claude "$0" "$@"', sys.executable, str(self.root / "runner.py"),
                   sys.executable, str(HOOK), *flags]
        payload = json.dumps({"session_id": session_id, "cwd": "/work/alpha", "source": "startup"})
        return subprocess.run(command, input=payload, env={**self.env, "HERDR_PANE_ID": pane},
                              capture_output=True, text=True, check=False, timeout=60)

    def entries(self) -> dict[str, dict[str, object]]:
        return cast(dict[str, dict[str, object]], json.loads((self.records / "agents.json").read_text()))

    def test_shared_parent_reader_reads_linux_comm_with_parentheses(self) -> None:
        process = self.root / "proc/42"
        process.mkdir(parents=True)
        _ = (process / "stat").write_text("42 (shell (fixture)) S 17 0 0")
        with mock.patch.dict(os.environ, {"HERDR_PROC": str(process.parent), "HERDR_PLATFORM": "linux"}):
            self.assertEqual(pane_record.process_parent(42), 17)
            with self.assertRaises(OSError):
                _ = pane_record.process_parent(99)

    def test_shared_parent_reader_propagates_mac_errors_and_hook_keeps_its_empty_answer(self) -> None:
        failure = subprocess.CalledProcessError(1, ["ps"])
        with (mock.patch.dict(os.environ, {"HERDR_PLATFORM": "darwin", "HERDR_PS": "/fixture/ps"}),
              mock.patch.object(subprocess, "run", side_effect=failure) as parent):
            with self.assertRaises(subprocess.CalledProcessError):
                _ = pane_record.process_parent(42)
            self.assertEqual(pane_record.claude_arguments(42), [])
            self.assertEqual(parent.call_args_list, [mock.call(["/fixture/ps", "-o", "ppid=", "-p", "42"],
                capture_output=True, text=True, check=True, timeout=5)] * 2)

    def test_the_hook_records_the_pane_launch_from_its_own_claude(self) -> None:
        started = self.start("w1:p2", "sid-alpha", "-n", "alpha", "--remote-control", "alpha",
                             "--model", "opus", "--settings", "{}", "resume checkpoint")
        self.assertEqual(started.returncode, 0, started.stderr)
        entry = self.entries()["w1:p2"]
        self.assertEqual({key: value for key, value in entry.items() if key != "written_at"}, {
            "pane_id": "w1:p2", "session_id": "sid-alpha", "config_dir": str(self.root / ".claude3"),
            "cwd": "/work/alpha", "name": "alpha", "model": "opus", "remote_control": True,
            "environment": {"ENABLE_TOOL_SEARCH": "true", "SHOWRUNNER_UNIT": "demo",
                            "SHOWRUNNER_UNIT_ID": "alpha-unit"},
            "flags_read": True, "ended": False})

    def test_hook_preserves_staff_identity_for_restore_and_membership(self) -> None:
        self.env["STAFF_ID"] = "s123456789ab"
        result = self.start("w1:p2", "sid-alpha", "-n", "alpha")
        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.entries()["w1:p2"]
        environment = cast(dict[str, str], entry["environment"])
        self.assertEqual(environment.get("STAFF_ID"), "s123456789ab")
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", environment)

    def test_a_new_start_replaces_its_own_pane_and_keeps_the_others(self) -> None:
        for pane, session_id in (("w1:p2", "sid-alpha"), ("w2:p1", "sid-ups"), ("w1:p2", "sid-alpha-2")):
            self.assertEqual(self.start(pane, session_id).returncode, 0)
        entries = self.entries()
        self.assertEqual({pane: entry["session_id"] for pane, entry in entries.items()},
                         {"w1:p2": "sid-alpha-2", "w2:p1": "sid-ups"})
        self.assertEqual((entries["w2:p1"]["name"], entries["w2:p1"]["remote_control"]), ("", False))

    def test_a_session_outside_herdr_writes_nothing(self) -> None:
        self.env["HERDR_ENV"] = ""
        self.assertEqual(self.start("w1:p2", "sid-alpha").returncode, 0)
        self.assertFalse(self.records.exists())


TOKEN = "sk-ant-oat01-do-not-keep"
# A Mac KERN_PROCARGS2 record: argc, the executable path, NUL padding, the arguments, the
# environment, then the kernel's own `key=value` strings.
MAC_RECORD = (
    (5).to_bytes(4, sys.byteorder) + b"/opt/claude/bin/claude\0\0\0\0"
    + b"claude\0--name\0alpha-unit\0--model\0opus\0"
    + b"CLAUDE_CONFIG_DIR=/Users/n/.claude3\0CLAUDE_CODE_OAUTH_TOKEN=" + TOKEN.encode()
    + b"\0SHOWRUNNER_UNIT_ID=alpha-unit\0ENABLE_TOOL_SEARCH=true\0HOME=/Users/n\0\0"
    + b"executable_path=/opt/claude/bin/claude\0\0\0"
)


@final
class MacRecordTests(unittest.TestCase):
    def test_on_the_mac_a_launch_reads_its_flags_and_never_keeps_the_token(self) -> None:
        parents = {"200": "100\n", "100": "1\n"}

        def ps(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 0, parents[command[-1]], "")

        def kernel(pid: int) -> bytes:
            return MAC_RECORD if pid == 100 else (1).to_bytes(4, sys.byteorder) + b"/bin/zsh\0zsh\0\0"

        with (mock.patch.object(sys, "platform", "darwin"),
              mock.patch.object(subprocess, "run", side_effect=ps),
              mock.patch.object(account, "kern_procargs2", side_effect=kernel)):
            arguments = pane_record.claude_arguments(200)
            environment = pane_record._process_environment(100)  # pyright: ignore[reportPrivateUsage]
        entry = pane_record.launch("w1:p1", "sid-alpha", "/work/alpha", environment, arguments)
        self.assertEqual((entry["name"], entry["model"], entry["flags_read"]), ("alpha-unit", "opus", True))
        self.assertEqual(entry["config_dir"], "/Users/n/.claude3")
        self.assertNotIn(TOKEN, json.dumps(environment) + json.dumps(entry))


if __name__ == "__main__":
    _ = unittest.main()
