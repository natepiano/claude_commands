"""Test Claude claims and phase baselines."""
from __future__ import annotations

import json
import os
import subprocess

from scripts.direct.implement_launcher_test_support import ImplementLauncherFixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class ImplementLauncherWorkerTests(ImplementLauncherFixture):
    def test_phase_launch_records_its_baseline_before_the_worker_opens(self) -> None:
        session = self.start_phase("first-baseline")
        head = subprocess.run(["git", "-C", str(self.working_dir), "rev-parse", "HEAD"], check=True,
                              capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS).stdout
        status = subprocess.run(["git", "-C", str(self.working_dir), "status", "--short"], check=True,
                                capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS).stdout
        result = self.checked_launch(session, "--phase", "3")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((session / "phases/3/phase_baseline").is_file())
        self.assertEqual((session / "phases/3/phase_baseline").read_text(), head)
        self.assertEqual((session / "phases/3/progress_baseline_status").read_text(), status)
        self.assertEqual((session / "baseline-at-worker-open").read_text(), head)


    def test_live_claim_refuses_a_second_claude_launcher_without_a_pass(self) -> None:
        session = self.start_phase("live-claim")
        _ = self.claude_worker(session)
        claim = session / "impl_claim_impl.json"
        saved = json.dumps({"pid": os.getpid(), "worker": "fixture-impl"})
        _ = claim.write_text(saved)
        before = self.open_passes(session)
        result = self.worker_launch(session, to="fixture-impl", behavior="complete")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("claimed by launcher", result.stderr)
        self.assertEqual(self.open_passes(session), before)
        self.assertEqual(claim.read_text(), saved)
        self.assertFalse((self.root / "delivered_message.txt").exists())

    def test_stale_claim_recovers_before_a_claude_follow(self) -> None:
        session = self.start_phase("stale-claim")
        _ = self.claude_worker(session)
        claim = session / "impl_claim_impl.json"
        _ = claim.write_text(json.dumps({"pid": 99999999, "worker": "fixture-impl"}))
        result = self.worker_launch(session, to="fixture-impl", behavior="complete")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((session / "impl_status_impl").read_text(), "implemented\n")
        self.assertFalse(claim.exists())
        self.assertTrue((self.root / "delivered_message.txt").exists())
