#!/usr/bin/env python3
"""Admission claims keep startup parallel and let a later shutdown revoke it."""
from __future__ import annotations

import fcntl
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "herdr"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import launch_permission
import shutdown_record
import herdr


class LaunchClaimTests(unittest.TestCase):
    def test_machine_shutdown_refuses_a_late_restore_on_an_unseen_account(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SHUTDOWN_STATE_DIR": directory}):
            with launch_permission.block_launches_during_shutdown():
                with self.assertRaisesRegex(launch_permission.LaunchAdmissionRefused, "machine-wide shutdown"):
                    with launch_permission.admitted_launch("unseen-account", "recorded-session", "restore", lambda: None):
                        self.fail("a restore started after shutdown revocation")
                self.assertEqual(launch_permission.pending_launches(), ())
                self.assertEqual(shutdown_record.all_session_records(), [])
            with launch_permission.admitted_launch("unseen-account", "recorded-session", "restore", lambda: None):
                self.assertEqual(len(launch_permission.pending_launches()), 1)

    def test_parallel_claims_release_the_barrier_before_startup(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SHUTDOWN_STATE_DIR": directory}):
            together = threading.Barrier(9, timeout=10)
            def launch(index: int) -> None:
                with launch_permission.admitted_launch("account", str(index), str(index), lambda: None):
                    _ = together.wait()
                    if index == 0:
                        with (Path(directory) / "launch.lock").open("a") as lock:
                            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                    _ = together.wait()
            with ThreadPoolExecutor(max_workers=9) as pool:
                futures = [pool.submit(launch, index) for index in range(9)]
                for future in futures:
                    future.result(timeout=15)
            self.assertEqual(launch_permission.pending_launches(), ())

    def test_down_before_pane_registration_refuses_and_closes_only_its_new_pane(self) -> None:
        with (tempfile.TemporaryDirectory() as directory,
              patch.dict(os.environ, {"SHUTDOWN_STATE_DIR": directory}),
              patch.object(herdr, "close_agent") as close):
            with self.assertRaisesRegex(OSError, "revoked"):
                with launch_permission.admitted_launch("account", "session", "name", lambda: None):
                    self.assertEqual(shutdown_record.all_session_records(), [])
                    claims = launch_permission.revoke_pending_launches("account")
                    self.assertEqual(len(claims), 1)
                    self.assertEqual(claims[0].session_id, "session")
                    launch_permission.claim_launch_pane("agents", "new:pane")
            close.assert_called_once_with("agents", "new:pane")
            self.assertEqual(launch_permission.pending_launches(), ())

    def test_down_can_close_the_exact_pane_during_startup(self) -> None:
        with (tempfile.TemporaryDirectory() as directory,
              patch.dict(os.environ, {"SHUTDOWN_STATE_DIR": directory}),
              patch.object(herdr, "close_agent") as close):
            with self.assertRaisesRegex(OSError, "revoked"):
                with launch_permission.admitted_launch("account", "session", "name", lambda: None):
                    launch_permission.claim_launch_pane("agents", "new:pane")
                    claims = launch_permission.revoke_pending_launches("account")
                    self.assertEqual((claims[0].herdr_session, claims[0].pane), ("agents", "new:pane"))
            close.assert_called_once_with("agents", "new:pane")

    def test_failed_admission_leaves_no_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SHUTDOWN_STATE_DIR": directory}):
            def refuse() -> None:
                raise OSError("held by shutdown")
            with self.assertRaisesRegex(OSError, "held by shutdown"):
                with launch_permission.admitted_launch("account", "session", "name", refuse):
                    self.fail("refused work started")
            self.assertEqual(launch_permission.pending_launches(), ())


if __name__ == "__main__":
    _ = unittest.main()
