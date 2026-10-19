from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast, final, override

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

    def test_the_hook_records_the_pane_launch_from_its_own_claude(self) -> None:
        started = self.start("w1:p2", "sid-alpha", "-n", "alpha", "--remote-control", "alpha",
                             "--model", "opus", "--settings", "{}", "resume checkpoint")
        self.assertEqual(started.returncode, 0, started.stderr)
        entry = self.entries()["w1:p2"]
        self.assertEqual({key: value for key, value in entry.items() if key != "written_at"}, {
            "pane_id": "w1:p2", "session_id": "sid-alpha", "config_dir": str(self.root / ".claude3"),
            "cwd": "/work/alpha", "name": "alpha", "model": "opus", "remote_control": True,
            "environment": {"ENABLE_TOOL_SEARCH": "true", "SHOWRUNNER_UNIT": "demo",
                            "SHOWRUNNER_UNIT_ID": "alpha-unit"}})

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


if __name__ == "__main__":
    _ = unittest.main()
