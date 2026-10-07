"""Tests for held news: when it is sent to the user, when typing in a terminal stops it, and that it is sent once."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import override

SCRIPT = Path(__file__).with_name("escalate.py")
NOTIFIER_STUB = 'print -r -- "notifier $*" >> "$LOG"\n'
SEND_STUB = """import os, sys
with open(os.environ["LOG"], "a") as log:
    _ = log.write("send " + " ".join(sys.argv[1:]) + "\\n")
if os.environ.get("REFUSED"):
    print("FAILED: the user was not reached: exit 1")
    raise SystemExit(3)
"""


class EscalateTests(unittest.TestCase):
    root: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = (self.root / "notifier.sh").write_text(NOTIFIER_STUB, encoding="utf-8")
        _ = (self.root / "send.py").write_text(SEND_STUB, encoding="utf-8")
        _ = (self.root / "log").write_text("", encoding="utf-8")
        self.environment = {
            **os.environ, "ESCALATE_STATE_DIR": str(self.root / "state"), "LOG": str(self.root / "log"),
            "ESCALATE_SEND": str(self.root / "send.py"), "ESCALATE_NOTIFIER": str(self.root / "notifier.sh"),
        }

    def run_script(self, *arguments: str, **environment: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), *arguments], env={**self.environment, **environment},
                              capture_output=True, text=True, check=False)

    def hold(self, minutes: str = "0") -> subprocess.CompletedProcess[str]:
        return self.run_script("hold", "disk", "--summary", "Disk", "--text", "Disk is full.", "--minutes", minutes)

    def log(self) -> str:
        return (self.root / "log").read_text(encoding="utf-8")

    def test_a_hold_starts_the_minute_tick_and_a_second_hold_changes_nothing(self) -> None:
        self.assertEqual(self.hold("15").stdout,
                         "disk: held; sent to the user in 15 min unless they type in a terminal\n")
        self.assertEqual(self.hold("15").stdout, "disk: already held\n")
        self.assertEqual(self.log().count("notifier new escalate --every 1 --run "), 1)
        self.assertIn(f"{SCRIPT.resolve()} due\n", self.log())
        self.assertEqual(self.run_script("list").stdout, "disk - waiting, sent in 15 min - Disk\n")

    def test_nothing_is_sent_before_its_time(self) -> None:
        _ = self.hold("15")
        done = self.run_script("due")
        self.assertEqual((done.returncode, done.stdout), (0, ""))
        self.assertNotIn("send ", self.log())

    def test_unanswered_news_is_sent_to_the_user_once(self) -> None:
        _ = self.hold()
        self.assertEqual(self.run_script("due").stdout, "disk: sent to the user\n")
        self.assertEqual(self.run_script("due").stdout, "")
        self.assertEqual(self.log().count(
            "send --to user --from escalate --summary Disk --need decision --text Disk is full.\n"), 1)
        self.assertEqual(self.run_script("list").stdout, "disk - sent - Disk\n")

    def test_typing_in_a_terminal_after_the_hold_stops_it(self) -> None:
        _ = self.hold()
        _ = self.run_script("typed")
        self.assertEqual(self.run_script("due").stdout, "disk: the user typed in a terminal; not sent\n")
        self.assertNotIn("send ", self.log())
        self.assertEqual(self.run_script("due").stdout, "")

    def test_typing_before_the_hold_does_not_count(self) -> None:
        _ = self.run_script("typed")
        _ = self.hold()
        self.assertEqual(self.run_script("due").stdout, "disk: sent to the user\n")

    def test_a_send_that_fails_is_tried_again(self) -> None:
        _ = self.hold()
        done = self.run_script("due", REFUSED="1")
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout, "disk: NOT sent: FAILED: the user was not reached: exit 1\n")
        self.assertEqual(self.run_script("due").stdout, "disk: sent to the user\n")

    def test_close_forgets_the_key_so_the_next_hold_waits_anew(self) -> None:
        _ = self.hold()
        _ = self.run_script("due")
        self.assertEqual(self.run_script("close", "disk").stdout, "disk: closed\n")
        self.assertEqual(self.run_script("close", "disk").stdout, "disk: not held\n")
        self.assertEqual(self.run_script("list").stdout, "nothing held\n")
        _ = self.hold()
        self.assertEqual(self.run_script("due").stdout, "disk: sent to the user\n")

    def test_any_other_arguments_print_the_usage(self) -> None:
        for arguments in ([], ["hold", "disk"], ["hold", "disk", "--summary", "a", "--text", "b", "--need", "loud"],
                          ["hold", "bad key", "--summary", "a", "--text", "b"], ["close"], ["due", "now"]):
            done = self.run_script(*arguments)
            self.assertEqual(done.returncode, 2, arguments)
            self.assertIn("usage: escalate.py", done.stderr)


if __name__ == "__main__":
    _ = unittest.main()
