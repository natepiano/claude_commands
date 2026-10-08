"""Tests for a broadcast: who is sent what, all at once, and what each is told about the others."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import override

SCRIPT = Path(__file__).with_name("broadcast.py")
# Each send waits for TOGETHER sends to have started, so sends run one after another fail.
SEND_STUB = """import os, sys, time
from pathlib import Path
root, to = Path(os.environ["LOG"]).parent, sys.argv[sys.argv.index("--to") + 1]
(root / f"started-{to}").touch()
deadline = time.monotonic() + 10
while len(list(root.glob("started-*"))) < int(os.environ["TOGETHER"]) and time.monotonic() < deadline:
    time.sleep(0.01)
with open(os.environ["LOG"], "a") as log:
    _ = log.write("send " + " ".join(sys.argv[1:]) + "\\n")
if to == os.environ.get("REFUSED") or len(list(root.glob("started-*"))) < int(os.environ["TOGETHER"]):
    print("QUEUED: relay timed out after 40 s")
    raise SystemExit(1)
"""


class BroadcastTests(unittest.TestCase):
    root: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for name in ("notifier", "sessions"):
            (self.root / name).mkdir()
        # hana's two units: rows of its production doc, each with a tmux session marked as that unit.
        # Only trunk-unit has a Claude running; its session is called `trunk` now.
        doc = self.root / "show-production.md"
        _ = doc.write_text("\n".join(("## Units", "| Unit | Plan | Worktree | Branch | Port | Owns |",
                                      "| --- | --- | --- | --- | --- | --- |",
                                      "| trunk-unit | docs/plan.md | /tmp/no-worktree-of-trunk | trunk | — | — |",
                                      "| gone-unit | docs/plan.md | /tmp/no-worktree-of-gone | gone | — | — |")),
                           encoding="utf-8")
        _ = (self.root / "tmux.json").write_text(json.dumps({
            f"${index}": {"label": "any-label", "panes": [f"%{index}"],
                          "env": {"SHOWRUNNER_UNIT": "show", "SHOWRUNNER_UNIT_ID": unit}}
            for index, unit in enumerate(("trunk-unit", "gone-unit"), start=1)}), encoding="utf-8")
        _ = (self.root / "showrunners.json").write_text(json.dumps({
            "threshold_percent": 2, "repeat_minutes": 30, "stall_minutes": 5, "faults_to": "hana",
            "always": [], "showrunners": [{"session": "hana", "zone": "America/Los_Angeles", "doc": str(doc)},
                                          {"session": "natedev", "zone": "America/Los_Angeles", "doc": ""}],
        }), encoding="utf-8")
        for name, pid in (("hana", os.getpid()), ("trunk", os.getppid()), ("natedev", 1), ("ups", 2)):
            listener = socket.socket(socket.AF_UNIX)
            self.addCleanup(listener.close)
            listener.bind(str(self.root / f"{name}.sock"))
            _ = (self.root / f"sessions/{pid}.json").write_text(json.dumps({
                "pid": pid, "sessionId": f"id-{name}", "name": name, "updatedAt": 1,
                "tmux": "name-at-start:@1.%1" if name == "trunk" else "",
                "messagingSocketPath": str(self.root / f"{name}.sock"),
            }), encoding="utf-8")
        _ = (self.root / "send.py").write_text(SEND_STUB, encoding="utf-8")
        self.environment = {
            **os.environ, "SHOWRUNNERS_CONFIG": str(self.root / "showrunners.json"),
            "NOTIFIER_STATE_DIR": str(self.root / "notifier"), "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "BROADCAST_SEND": str(self.root / "send.py"), "LOG": str(self.root / "log"), "TOGETHER": "2",
            "BROADCAST_PS": f"cat {self.root / 'ps'}",
            "UNIT_LOOKUP_TMUX": str(Path(__file__).with_name("fake_tmux.py")),
            "FAKE_TMUX_STATE": str(self.root / "tmux.json"),
        }
        _ = (self.root / "ps").write_text(
            "40 1 python3 python3 /u/agents/codex_mesh.py start --session-dir /tmp/d1 --name fps-impl --cwd /w\n"
            + "41 1 codex codex app-server --listen ws://127.0.0.1:1\n", encoding="utf-8")

    def run_script(self, *arguments: str, **environment: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), "--from", "natedev", *arguments],
                              env={**self.environment, **environment}, capture_output=True, text=True, check=False)

    def log(self) -> str:
        return (self.root / "log").read_text(encoding="utf-8")

    def test_all_reaches_every_role_at_once_and_never_the_sender(self) -> None:
        done = self.run_script("--all", "From the user: stop builds.", TOGETHER="4")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(done.stdout.splitlines()[:-1], [
            "hana - showrunner - sent", "trunk - unit director - sent", "gone-unit - unit director - no live session",
            "ups - agent - sent", "fps-impl - agent - sent"])
        self.assertRegex(done.stdout.splitlines()[-1], r"^4 sent in \d+ s$")
        self.assertEqual(self.log().count("--text From the user: stop builds.\n\nEvery showrunner, unit director and"
                                          + " other agent on this machine was sent this directly; do not pass it on.\n"), 4)
        self.assertIn("send --to fps-impl --from natedev --codex --session-dir /tmp/d1 --text From the user", self.log())
        self.assertNotIn("--to natedev", self.log())

    def test_a_registry_file_that_cannot_be_read_is_skipped(self) -> None:
        _ = (self.root / "sessions/broken.json").write_text("{not json", encoding="utf-8")
        _ = (self.root / "sessions/9.json").write_text(json.dumps({"pid": 9}), encoding="utf-8")
        done = self.run_script("--all", "From the user: stop builds.", TOGETHER="4")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(done.stdout.splitlines()[:-1], [
            "hana - showrunner - sent", "trunk - unit director - sent", "gone-unit - unit director - no live session",
            "ups - agent - sent", "fps-impl - agent - sent"])

    def test_each_role_gets_its_own_version(self) -> None:
        done = self.run_script("--showrunners", "count your agents", "--units", "finish your agents")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("send --to hana --from natedev --text count your agents\n\nEvery showrunner and unit director"
                      + " on this machine was sent this directly, and no other agent.", self.log())
        self.assertIn("send --to trunk --from natedev --text finish your agents\n\nEvery showrunner and unit", self.log())

    def test_a_role_flag_after_all_gives_that_role_its_own_version(self) -> None:
        _ = self.run_script("--all", "Codex is back", "--agents", "carry on", TOGETHER="4")
        self.assertEqual(self.log().count("--text Codex is back\n"), 2)
        self.assertEqual(self.log().count("--text carry on\n"), 2)

    def test_one_role_alone_is_told_the_other_was_not_sent_it(self) -> None:
        _ = self.run_script("--showrunners", "merge freeze", TOGETHER="1")
        _ = self.run_script("--units", "builds resume", TOGETHER="1")
        self.assertEqual(self.log().count("send --to"), 2)
        self.assertIn("--to hana --from natedev --text merge freeze\n\nEvery showrunner on this machine was sent"
                      + " this directly, and no unit director or other agent.", self.log())
        self.assertIn("--to trunk --from natedev --text builds resume\n\nEvery unit director on this machine was"
                      + " sent this directly, and no showrunner or other agent.", self.log())

    def test_a_delivery_that_fails_is_named_with_its_reason(self) -> None:
        done = self.run_script("--showrunners", "stop", "--units", "stop", REFUSED="trunk")
        self.assertEqual(done.returncode, 1)
        self.assertIn("trunk - unit director - NOT sent: QUEUED: relay timed out after 40 s", done.stdout)
        self.assertIn("hana - showrunner - sent", done.stdout)

    def test_any_other_arguments_print_the_usage(self) -> None:
        for arguments in ([], ["--everyone", "a"], ["--all"], ["--all", "a", "--units"]):
            done = self.run_script(*arguments)
            self.assertEqual(done.returncode, 2, arguments)
            self.assertIn("usage: broadcast.py", done.stderr)


if __name__ == "__main__":
    _ = unittest.main()
