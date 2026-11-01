"""Upkeep preserves due state and kills a hung worker."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from typing import override
from unittest.mock import patch

from scripts.maintenance import herdr_upkeep as job
from scripts.herdr import herdr, detection_rules


class UpkeepTests(unittest.TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root),
            "HERDR_UPKEEP_CONFIG": str(self.root / "config.toml")}))
        _ = (self.root / "config.toml").write_text("[job]\ninterval_seconds=120\ntimeout_seconds=0.2\n")

    def test_runtime_interval_skips_completed_work_without_changing_state(self) -> None:
        script = self.root / "worker.py"
        _ = script.write_text("from pathlib import Path\nPath(__file__).with_suffix('.ran').touch()\n")
        with patch.object(job, "__file__", str(script)):
            self.assertEqual(job.run_job(), 0)
        completed = self.root / "herdr-upkeep/completed"
        before = completed.read_bytes(), completed.stat().st_mtime_ns
        script.with_suffix(".ran").unlink()
        with patch.object(job, "__file__", str(script)):
            self.assertEqual(job.run_job(), 0)
        self.assertFalse(script.with_suffix(".ran").exists())
        self.assertEqual((completed.read_bytes(), completed.stat().st_mtime_ns), before)
        _ = (self.root / "config.toml").write_text("[job]\ninterval_seconds=0.001\ntimeout_seconds=0.2\n")
        time.sleep(0.002)
        with patch.object(job, "__file__", str(script)):
            self.assertEqual(job.run_job(), 0)
        self.assertTrue(script.with_suffix(".ran").exists())

    def test_hung_worker_is_killed_and_remains_due_for_retry(self) -> None:
        script = self.root / "worker.py"
        _ = script.write_text("import signal\nsignal.pause()\n")
        started = time.monotonic()
        with patch.object(job, "__file__", str(script)):
            self.assertEqual(job.run_job(), 124)
        self.assertLess(time.monotonic() - started, 2)
        self.assertFalse((self.root / "herdr-upkeep/completed").exists())
        _ = script.write_text("pass\n")
        with patch.object(job, "__file__", str(script)):
            self.assertEqual(job.run_job(), 0)

    def test_layout_and_rule_refresh_share_the_current_running_sessions(self) -> None:
        current = herdr.SessionEntry(name="shared", running=True, default=False,
            session_dir="/tmp/shared", socket_path="/tmp/shared.sock")
        with (patch.object(herdr, "live_sessions", return_value=[current]),
              patch.object(herdr, "save_shared_layout") as layout,
              patch.object(detection_rules, "refresh") as rules):
            job.upkeep()
        layout.assert_called_once_with({"shared"})
        rules.assert_called_once_with({"shared"}, herdr.reload_agent_rules)
