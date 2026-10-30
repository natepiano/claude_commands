"""An adhoc review restores the dailies only when it paused them."""

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
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
if TYPE_CHECKING:
    from ..hooks import showrunner_footer
else:
    import showrunner_footer


# Script completion deadlines only bound hangs; startup can be slow under load.
SCRIPT = Path(__file__).with_name("review_pause.py")
ZONE = "America/Los_Angeles"


class ReviewPauseTests(unittest.TestCase):
    root: Path = Path()
    instance: Path = Path()
    record: Path = Path()
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
            + "    if os.environ.get('FAIL_STOP') or os.environ.get('FAIL_STOP_FOR') == name:\n"
            + "        print('notifier stop refused', file=sys.stderr)\n"
            + "        sys.exit(20)\n"
            + "    enabled.unlink(missing_ok=True)\n"
            + "    state.write_text('ENABLED=0\\nNEXT_DUE=1\\n')\n"
            + "    if os.environ.get('FAIL_STOP_AFTER') or os.environ.get('FAIL_STOP_AFTER_FOR') == name:\n"
            + "        print('notifier stop failed after stopping', file=sys.stderr)\n"
            + "        sys.exit(21)\n"
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
                              env={**self.environment, "CLAUDE_CODE_SESSION_ID": session}, timeout=TEST_HANG_GUARD_SECS)

    def stop_dailies(self) -> None:
        self.enabled.unlink()
        _ = (self.instance / "state").write_text("ENABLED=0\nNEXT_DUE=1\n")

    def test_pause_and_resume_both(self) -> None:
        paused = self.run_script("pause")
        self.assertEqual(paused.returncode, 0, paused.stderr)
        self.assertEqual(paused.stdout, "demo: paused dailies\n")
        self.assertFalse(self.enabled.exists())
        self.assertEqual(json.loads(self.record.read_text()), {"dailies": "done"})
        self.assertEqual(self.run_script("status").stdout, "demo: review paused dailies\n")
        self.assertEqual(self.run_script("pause").stdout, "demo: review paused dailies\n")
        self.assertEqual(self.run_script("pause").stdout, "demo: review paused dailies\n")
        self.assertEqual(self.run_script("resume", "dailies").returncode, 0)
        self.assertTrue(self.enabled.exists())
        self.assertFalse(self.record.exists())

    def test_pause_persists_record_before_stopping_notifier(self) -> None:
        self.environment["REQUIRE_RECORD_BEFORE_STOP"] = "1"
        paused = self.run_script("pause")
        self.assertEqual(paused.returncode, 0, paused.stderr)
        self.assertTrue(self.record.exists())
        self.assertFalse(self.enabled.exists())

    def test_failed_stop_can_retry_on_same_record(self) -> None:
        self.environment["FAIL_STOP"] = "1"
        failed = self.run_script("pause")
        self.assertEqual(failed.returncode, 1)
        self.assertIn("notifier stop refused", failed.stderr)
        self.assertEqual(json.loads(self.record.read_text()),
                         {"dailies": "attempted"})
        self.assertTrue(self.enabled.exists())
        _ = self.environment.pop("FAIL_STOP")
        retried = self.run_script("pause")
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertEqual(json.loads(self.record.read_text()), {"dailies": "done"})
        self.assertFalse(self.enabled.exists())
        self.assertEqual(self.run_script("resume", "dailies").returncode, 0)
        self.assertTrue(self.enabled.exists())

    def test_failed_stop_resume_does_not_restore_uncompleted_actions(self) -> None:
        self.environment["FAIL_STOP"] = "1"
        self.assertEqual(self.run_script("pause").returncode, 1)
        self.assertEqual(self.run_script("resume", "dailies").returncode, 0)
        self.assertTrue(self.enabled.exists())
        self.assertFalse(self.record.exists())

    def test_stop_that_takes_effect_then_fails_is_restored_on_resume(self) -> None:
        self.environment["FAIL_STOP_AFTER"] = "1"
        failed = self.run_script("pause")
        self.assertEqual(failed.returncode, 1)
        self.assertIn("failed after stopping", failed.stderr)
        self.assertEqual(json.loads(self.record.read_text()),
                         {"dailies": "attempted"})
        self.assertFalse(self.enabled.exists())
        self.assertEqual(self.run_script("resume", "dailies").returncode, 0)
        self.assertTrue(self.enabled.exists())
        self.assertFalse(self.record.exists())

    def test_status_names_effective_pause_after_stop_reports_failure(self) -> None:
        self.environment["FAIL_STOP_AFTER"] = "1"
        self.assertEqual(self.run_script("pause").returncode, 1)
        self.assertEqual(self.run_script("status").stdout, "demo: review paused dailies\n")
        resumed = self.run_script("resume", "dailies")
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(resumed.stdout, "demo: turned on dailies\n")
        self.assertTrue(self.enabled.exists())
        self.assertFalse(self.record.exists())

    def test_stop_that_takes_effect_then_fails_is_completed_on_retry(self) -> None:
        self.environment["FAIL_STOP_AFTER"] = "1"
        self.assertEqual(self.run_script("pause").returncode, 1)
        _ = self.environment.pop("FAIL_STOP_AFTER")
        retried = self.run_script("pause")
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertEqual(json.loads(self.record.read_text()), {"dailies": "done"})
        self.assertEqual(self.run_script("resume", "dailies").returncode, 0)
        self.assertTrue(self.enabled.exists())

    def test_later_production_failure_keeps_earlier_pause_and_retries_both(self) -> None:
        other = self.instance.with_name("showrunner-zeta")
        other.mkdir()
        _ = (other / "conf").write_text((self.instance / "conf").read_text())
        _ = (other / "state").write_text("ENABLED=1\nNEXT_DUE=1\n")
        (other / "enabled").touch()
        other_record = self.record.with_name("zeta.json")
        self.environment["FAIL_STOP_AFTER_FOR"] = other.name
        failed = self.run_script("pause")
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(json.loads(self.record.read_text()), {"dailies": "done"})
        self.assertEqual(json.loads(other_record.read_text()),
                         {"dailies": "attempted"})
        self.assertFalse(self.enabled.exists())
        self.assertFalse((other / "enabled").exists())
        _ = self.environment.pop("FAIL_STOP_AFTER_FOR")
        retried = self.run_script("pause")
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertEqual(json.loads(other_record.read_text()), {"dailies": "done"})
        self.assertEqual(self.run_script("resume", "dailies").returncode, 0)
        self.assertTrue(self.enabled.exists())
        self.assertTrue((other / "enabled").exists())
        self.assertFalse(self.record.exists())
        self.assertFalse(other_record.exists())

    def test_review_pause_uses_shared_showrunner_state(self) -> None:
        self.assertIs(review_pause.showrunner_footer, showrunner_footer)

    def test_record_path_uses_shared_review_pause_path(self) -> None:
        self.assertEqual(
            review_pause.record_path("demo"),
            showrunner_footer.review_pause_path("demo"),
        )

    def test_already_stopped_dailies_stay_stopped(self) -> None:
        self.stop_dailies()
        self.assertEqual(self.run_script("pause").stdout, "demo: nothing to pause\n")
        self.assertEqual(json.loads(self.record.read_text()),
                         {"dailies": "skipped"})
        self.assertEqual(self.run_script("resume", "dailies").returncode, 0)
        self.assertFalse(self.enabled.exists())

    def test_a_record_written_while_footers_existed_resumes_its_dailies(self) -> None:
        self.stop_dailies()
        self.record.parent.mkdir(parents=True, exist_ok=True)
        _ = self.record.write_text('{"dailies": "done", "footers": "done"}\n')
        self.assertEqual(self.run_script("status").stdout, "demo: review paused dailies\n")
        self.assertEqual(self.run_script("resume", "footers").returncode, 2)
        self.assertEqual(self.run_script("resume", "dailies").stdout, "demo: turned on dailies\n")
        self.assertFalse(self.record.exists())

    def test_resume_none_keeps_both_off_and_deletes_record(self) -> None:
        self.assertEqual(self.run_script("pause").returncode, 0)
        self.assertEqual(self.run_script("resume", "none").returncode, 0)
        self.assertFalse(self.enabled.exists())
        self.assertFalse(self.record.exists())

    def test_nothing_to_pause_and_unrelated_session(self) -> None:
        self.stop_dailies()
        self.assertEqual(self.run_script("pause").stdout, "demo: nothing to pause\n")
        self.assertEqual(json.loads(self.record.read_text()),
                         {"dailies": "skipped"})
        self.assertEqual(self.run_script("pause", session="other").stdout, "")
        self.assertEqual(self.run_script("status", session="other").stdout, "")
        self.assertEqual(self.run_script("resume", "none").returncode, 0)
        self.assertFalse(self.record.exists())

    def test_retry_does_not_capture_actions_that_were_already_off(self) -> None:
        self.stop_dailies()
        self.assertEqual(self.run_script("pause").returncode, 0)
        self.assertEqual(json.loads(self.record.read_text()),
                         {"dailies": "skipped"})
        self.enabled.touch()
        _ = (self.instance / "state").write_text("ENABLED=1\nNEXT_DUE=1\n")
        self.assertEqual(self.run_script("pause").returncode, 0)
        self.assertTrue(self.enabled.exists())
        self.assertEqual(self.run_script("resume", "dailies").returncode, 0)
        self.assertTrue(self.enabled.exists())

    def test_legacy_skipped_actions_stay_on_after_repeated_pause(self) -> None:
        self.record.parent.mkdir(parents=True)
        _ = self.record.write_text('{"dailies": false, "footers": false}\n')
        paused = self.run_script("pause")
        self.assertEqual(paused.returncode, 0, paused.stderr)
        self.assertTrue(self.enabled.exists())
        self.assertEqual(self.run_script("status").stdout, "demo: review paused nothing\n")
        self.assertEqual(self.run_script("resume", "dailies").returncode, 0)
        self.assertTrue(self.enabled.exists())


if __name__ == "__main__":
    _ = unittest.main()
