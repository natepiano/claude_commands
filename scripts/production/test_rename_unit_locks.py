"""Real merge and staff lock ordering across fresh and resumed renames."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import final

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scripts.production.rename_unit_test_support import SLUG, UnitRenameFixture
from scripts.production import staff
import merge_checkpoint
import rename_unit


@final
class UnitRenameLockTests(UnitRenameFixture):
    def test_waiting_rename_leaves_staff_writes_unblocked(self) -> None:
        _ = self.seed_record()
        planned = rename_unit.plan(SLUG, "alpha", "gamma", self.root / "scratch")
        member = staff.by_path("demo/alpha")
        assert member is not None
        planned.staff_id = member.id
        for resumed in (False, True):
            with self.subTest(resumed=resumed):
                if resumed:
                    planned.step = 1
                    rename_unit._save(self.state / SLUG / "rename_unit.json", planned)  # pyright: ignore[reportPrivateUsage]
                with merge_checkpoint.merge_lock(self.checkout, SLUG):
                    process = self.start_apply()
                    try:
                        _ = self.wait_for_output(process, "merge lock: waiting —")
                        program = (
                            "from scripts.production import staff; "
                            "exec(\"with staff.operation():\\n "
                            "staff.ensure('showrunner', name='beside-rename')\"); "
                            "print(staff.by_path('beside-rename').name)"
                        )
                        writer = subprocess.run(
                            [sys.executable, "-c", program],
                            cwd=Path(__file__).resolve().parents[2], env=self.env,
                            capture_output=True, text=True, check=False, timeout=5,
                        )
                        self.assertEqual(writer.returncode, 0, writer.stdout + writer.stderr)
                        self.assertEqual(writer.stdout.strip(), "beside-rename")
                        self.assertIsNone(process.poll(), "rename must still be waiting on the held merge lock")
                    finally:
                        process.kill()
                        _ = process.communicate(timeout=5)

    def test_record_relaunch_and_verify_resume_without_the_merge_lock(self) -> None:
        log = self.seed_record()
        self.assert_refused(self.rename("apply"), "gamma-unit is not running")
        before = log.read_text()
        journal = self.journal()
        journal["step"] = 8  # Resume at the shutdown-record step, after all git writes.
        self.write(self.state / SLUG / "rename_unit.json", json.dumps(journal))
        # A gate may have unrelated changes in progress in the showrunner checkout.
        self.write(self.checkout / "NOTES.md", "merge gate has uncommitted work\n")
        with merge_checkpoint.merge_lock(self.checkout, SLUG):
            process = self.start_apply()
            try:
                output, errors = process.communicate(timeout=5)
            finally:
                if process.poll() is None:
                    process.kill()
                    _ = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 1, output + errors)
            self.assertIn("gamma-unit is not running", errors)
            self.assertNotIn("merge lock: waiting", output)
            self.assertEqual(self.journal()["step"], 10)
            self.assertEqual(len(log.read_text().splitlines()), len(before.splitlines()) + 1)
            _ = self.mark_live("gamma-unit", "sid-alpha")
            process = self.start_apply()
            try:
                output, errors = process.communicate(timeout=5)
            finally:
                if process.poll() is None:
                    process.kill()
                    _ = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, output + errors)
            self.assertNotIn("merge lock: waiting", output)
            self.assertIs(self.journal()["done"], True)
