"""An adhoc review restores only the dailies and footers it paused."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import TYPE_CHECKING, override

import review_pause
if TYPE_CHECKING:
    from ..hooks import showrunner_footer
else:
    import showrunner_footer


SCRIPT = Path(__file__).with_name("review_pause.py")
ZONE = "America/Los_Angeles"


class ReviewPauseTests(unittest.TestCase):
    root: Path = Path()
    instance: Path = Path()
    record: Path = Path()
    switch: Path = Path()
    enabled: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        notifier = self.root / "notifier"
        self.instance = notifier / "showrunner-demo"
        self.instance.mkdir(parents=True)
        doc = self.root / "production.md"
        _ = doc.write_text(
            "> **Status: PRODUCTION — running.** demo\n"
            + f"- **User zone:** {ZONE} — demo\n"
        )
        _ = (self.instance / "conf").write_text(
            f"TARGET=session:current\nCHECK=zsh /x/check.sh {doc}\n"
        )
        _ = (self.instance / "state").write_text("ENABLED=1\nNEXT_DUE=1\n")
        self.enabled = self.instance / "enabled"
        self.enabled.touch()
        state = self.root / "showrunner-state"
        self.record = state / "review-paused/demo.json"
        self.switch = state / "footers-off/demo"
        stub = self.root / "notifier.py"
        _ = stub.write_text(
            "#!/usr/bin/env python3\n"
            + "import os, pathlib, sys\n"
            + "root = pathlib.Path(os.environ['NOTIFIER_STATE_DIR'])\n"
            + "action, name = sys.argv[1:]\n"
            + "enabled = root / name / 'enabled'\n"
            + "state = root / name / 'state'\n"
            + "record = pathlib.Path(os.environ['SHOWRUNNER_STATE_DIR']) / 'review-paused' / (name.removeprefix('showrunner-') + '.json')\n"
            + "if action == 'status':\n"
            + "    print(f'{name} → session:current every 30 min, ' + ('enabled' if enabled.exists() else 'stopped'))\n"
            + "    print('next_due=1 (1970-01-01 00:00 UTC)')\n"
            + "    print('last_sent=never')\n"
            + "    print('log: none')\n"
            + "elif action == 'stop':\n"
            + "    if os.environ.get('REQUIRE_RECORD_BEFORE_STOP') and not record.exists():\n"
            + "        sys.exit(19)\n"
            + "    enabled.unlink(missing_ok=True)\n"
            + "    state.write_text('ENABLED=0\\nNEXT_DUE=1\\n')\n"
            + "elif action == 'start':\n"
            + "    enabled.touch()\n"
            + "    state.write_text('ENABLED=1\\nNEXT_DUE=1\\n')\n"
        )
        stub.chmod(0o755)
        self.environment = {
            **os.environ, "HOME": str(self.root), "CLAUDE_CODE_SESSION_ID": "current",
            "NOTIFIER_STATE_DIR": str(notifier), "SHOWRUNNER_STATE_DIR": str(state),
            "SHOWRUNNER_NOTIFIER": str(stub),
        }

    def run_script(self, *args: str, session: str = "current") -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True,
                              text=True, check=False,
                              env={**self.environment, "CLAUDE_CODE_SESSION_ID": session}, timeout=10)

    def stop_dailies(self) -> None:
        self.enabled.unlink()
        _ = (self.instance / "state").write_text("ENABLED=0\nNEXT_DUE=1\n")

    def test_pause_and_resume_both(self) -> None:
        paused = self.run_script("pause")
        self.assertEqual(paused.returncode, 0, paused.stderr)
        self.assertEqual(paused.stdout, "demo: paused dailies and footers\n")
        self.assertFalse(self.enabled.exists())
        self.assertTrue(self.switch.exists())
        self.assertEqual(json.loads(self.record.read_text()), {"dailies": True, "footers": True})
        self.assertEqual(self.run_script("status").stdout, "demo: review paused dailies, footers\n")
        self.assertEqual(self.run_script("pause").stdout, "demo: review paused dailies, footers\n")
        self.switch.unlink()
        self.assertEqual(self.run_script("pause").stdout, "demo: review paused dailies, footers\n")
        self.assertFalse(self.switch.exists())
        self.switch.touch()
        self.assertEqual(self.run_script("resume", "both").returncode, 0)
        self.assertTrue(self.enabled.exists())
        self.assertFalse(self.switch.exists())
        self.assertFalse(self.record.exists())

    def test_pause_reads_enabled_state_with_multiline_notifier_status(self) -> None:
        paused = self.run_script("pause")
        self.assertEqual(paused.returncode, 0, paused.stderr)
        self.assertEqual(json.loads(self.record.read_text()), {"dailies": True, "footers": True})
        self.assertFalse(self.enabled.exists())

    def test_pause_persists_record_before_stopping_notifier(self) -> None:
        self.environment["REQUIRE_RECORD_BEFORE_STOP"] = "1"
        paused = self.run_script("pause")
        self.assertEqual(paused.returncode, 0, paused.stderr)
        self.assertTrue(self.record.exists())
        self.assertFalse(self.enabled.exists())

    def test_review_pause_uses_shared_footer_switch(self) -> None:
        self.assertIs(review_pause.showrunner_footer, showrunner_footer)

    def test_already_stopped_dailies_stay_stopped(self) -> None:
        self.stop_dailies()
        self.assertEqual(self.run_script("pause").stdout,
                         "demo: paused footers; dailies were already off\n")
        self.assertEqual(json.loads(self.record.read_text()), {"dailies": False, "footers": True})
        self.assertEqual(self.run_script("resume", "both").returncode, 0)
        self.assertFalse(self.enabled.exists())
        self.assertFalse(self.switch.exists())

    def test_resume_footers_and_none_follow_record(self) -> None:
        self.assertEqual(self.run_script("pause").returncode, 0)
        self.assertEqual(self.run_script("resume", "footers").returncode, 0)
        self.assertFalse(self.enabled.exists())
        self.assertFalse(self.switch.exists())
        self.assertFalse(self.record.exists())
        self.assertEqual(self.run_script("pause").stdout, "demo: paused footers; dailies were already off\n")
        self.assertEqual(self.run_script("resume", "none").returncode, 0)
        self.assertFalse(self.enabled.exists())
        self.assertTrue(self.switch.exists())
        self.assertFalse(self.record.exists())

    def test_resume_none_keeps_both_off_and_deletes_record(self) -> None:
        self.assertEqual(self.run_script("pause").returncode, 0)
        self.assertEqual(self.run_script("resume", "none").returncode, 0)
        self.assertFalse(self.enabled.exists())
        self.assertTrue(self.switch.exists())
        self.assertFalse(self.record.exists())

    def test_nothing_to_pause_and_unrelated_session(self) -> None:
        self.stop_dailies()
        self.switch.parent.mkdir(parents=True)
        self.switch.touch()
        self.assertEqual(self.run_script("pause").stdout, "demo: nothing to pause\n")
        self.assertEqual(json.loads(self.record.read_text()), {"dailies": False, "footers": False})
        self.assertEqual(self.run_script("pause", session="other").stdout, "")
        self.assertEqual(self.run_script("status", session="other").stdout, "")
        self.assertEqual(self.run_script("resume", "none").returncode, 0)
        self.assertFalse(self.record.exists())


if __name__ == "__main__":
    _ = unittest.main()
