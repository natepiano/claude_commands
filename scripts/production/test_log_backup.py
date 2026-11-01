"""A production's LOG is copied on every update tick, and the newest copies are kept."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import override
from zoneinfo import ZoneInfo

import log_backup
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

# Script completion deadlines only bound hangs; startup can be slow under load.
SCRIPT = Path(__file__).with_name("log_backup.py")
CHECK = Path(__file__).with_name("production_check.sh")
ZONE = ZoneInfo("America/Los_Angeles")
NOON = datetime(2026, 10, 12, 12, 0, tzinfo=ZONE)


class LogBackupTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.log: Path = Path()
        self.doc: Path = Path()
        self.state: Path = Path()
        self.backups: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        checkout = self.root / "checkout"
        self.log = checkout / "docs/plans/demo-log.md"
        self.log.parent.mkdir(parents=True)
        self.state = self.root / "state"
        self.backups = self.state / "demo" / "log-backups"
        self.doc = self.root / "demo-production.md"
        self.write_doc("running")

    def write_doc(self, status: str, *, log_field: bool = True) -> None:
        fields = [
            f"> **Status: PRODUCTION — {status}.** demo",
            "- **Merge branch:** `demo`",
            f"- **Showrunner checkout:** `{self.root / 'checkout'}`",
            "- **User zone:** America/Los_Angeles — demo",
            "## Units",
        ]
        if log_field:
            fields.insert(2, "- **Log:** `docs/plans/demo-log.md`")
        _ = self.doc.write_text("\n".join(fields) + "\n", encoding="utf-8")

    def names(self) -> list[str]:
        return sorted(path.name for path in self.backups.iterdir()) if self.backups.exists() else []

    def run_script(self, script: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(script, capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
                              env={**os.environ, "HOME": str(self.root), "SHOWRUNNER_STATE_DIR": str(self.state)})

    def test_a_log_is_copied_under_its_stamp(self) -> None:
        _ = self.log.write_text("- 11:59 PDT: merged\n", encoding="utf-8")
        target = log_backup.backup(self.log, self.backups, NOON)
        self.assertEqual(target, self.backups / "log-20261012T1200.md")
        self.assertEqual(self.names(), ["log-20261012T1200.md"])
        self.assertEqual((self.backups / "log-20261012T1200.md").read_text(encoding="utf-8"), "- 11:59 PDT: merged\n")

    def test_an_unchanged_log_is_not_copied_again(self) -> None:
        _ = self.log.write_text("- 11:59 PDT: merged\n", encoding="utf-8")
        _ = log_backup.backup(self.log, self.backups, NOON)
        self.assertIsNone(log_backup.backup(self.log, self.backups, NOON + timedelta(minutes=15)))
        self.assertEqual(self.names(), ["log-20261012T1200.md"])
        _ = self.log.write_text("- 11:59 PDT: merged\n- 12:20 PDT: gate green\n", encoding="utf-8")
        self.assertIsNotNone(log_backup.backup(self.log, self.backups, NOON + timedelta(minutes=30)))
        self.assertEqual(self.names(), ["log-20261012T1200.md", "log-20261012T1230.md"])

    def test_an_emptied_or_missing_log_is_never_copied(self) -> None:
        _ = self.log.write_text("- 11:59 PDT: merged\n", encoding="utf-8")
        _ = log_backup.backup(self.log, self.backups, NOON)
        for emptied in ("", " \n\n"):
            with self.subTest(emptied=emptied):
                _ = self.log.write_text(emptied, encoding="utf-8")
                self.assertIsNone(log_backup.backup(self.log, self.backups, NOON + timedelta(minutes=15)))
        self.log.unlink()
        self.assertIsNone(log_backup.backup(self.log, self.backups, NOON + timedelta(minutes=30)))
        self.assertEqual(self.names(), ["log-20261012T1200.md"])
        self.assertEqual((self.backups / "log-20261012T1200.md").read_text(encoding="utf-8"), "- 11:59 PDT: merged\n")

    def test_only_the_newest_copies_are_kept(self) -> None:
        for tick in range(log_backup.KEEP + 2):
            _ = self.log.write_text(f"- event {tick}\n", encoding="utf-8")
            _ = log_backup.backup(self.log, self.backups, NOON + timedelta(minutes=15 * tick))
        names = self.names()
        self.assertEqual(len(names), log_backup.KEEP)
        self.assertEqual(names[0], "log-20261012T1230.md")
        self.assertEqual((self.backups / names[-1]).read_text(encoding="utf-8"), f"- event {log_backup.KEEP + 1}\n")

    def test_the_command_copies_into_the_production_s_own_directory(self) -> None:
        _ = self.log.write_text("- 11:59 PDT: merged\n", encoding="utf-8")
        result = self.run_script([sys.executable, str(SCRIPT), str(self.doc)])
        self.assertEqual(result.returncode, 0, result.stderr)
        names = self.names()
        self.assertEqual(len(names), 1)
        self.assertRegex(names[0], r"^log-\d{8}T\d{4}\.md$")
        self.assertEqual(result.stdout, f"copied to {self.backups / names[0]}\n")
        self.assertEqual(self.run_script([sys.executable, str(SCRIPT), str(self.doc)]).stdout, "nothing new to copy\n")

    def test_the_command_refuses_a_doc_it_cannot_read(self) -> None:
        self.write_doc("running", log_field=False)
        result = self.run_script([sys.executable, str(SCRIPT), str(self.doc)])
        self.assertEqual(result.returncode, 1)
        self.assertIn("log_backup: production doc lacks Log", result.stderr)
        self.assertEqual(self.run_script([sys.executable, str(SCRIPT)]).returncode, 2)

    def test_the_update_check_copies_the_log_of_a_running_production_only(self) -> None:
        _ = self.log.write_text("- 11:59 PDT: merged\n", encoding="utf-8")
        for status, code, copied in (("wrapped", 2, 0), ("paused", 1, 0), ("running", 0, 1)):
            with self.subTest(status=status):
                self.write_doc(status)
                result = self.run_script(["zsh", str(CHECK), str(self.doc)])
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(len(self.names()), copied)

    def test_a_failed_copy_never_changes_the_check_s_answer(self) -> None:
        self.write_doc("running", log_field=False)
        result = self.run_script(["zsh", str(CHECK), str(self.doc)])
        self.assertEqual(result.returncode, 0)
        self.assertIn("log_backup: production doc lacks Log", result.stderr)
        self.assertEqual(self.names(), [])


if __name__ == "__main__":
    _ = unittest.main()
