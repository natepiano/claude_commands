"""Test Claude claims and phase baselines."""
from __future__ import annotations

import json
import os
import subprocess
import sys

from scripts.direct.assign_claude_test_support import InProcessAssignClaudeLauncherFixture
from scripts.production.cli_fixture import process_boundary
from scripts.lib import process_start
from scripts.direct import worker_state
from typing import cast
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class AssignClaudeLauncherWorkerTests(InProcessAssignClaudeLauncherFixture):
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


    @process_boundary
    def test_live_claim_refuses_a_second_claude_launcher_without_a_pass(self) -> None:
        session = self.start_phase("live-claim")
        _ = self.claude_worker(session)
        claim = session / "impl_claim_fixture-impl.json"
        started = process_start.process_start(os.getpid())
        assert isinstance(started, process_start.Started)
        worker_state.publish_worker_claim(claim, "fixture-impl")
        scope = cast(dict[str, object], json.loads(claim.read_text()))["scope"]
        saved = json.dumps({"pid": os.getpid(), "started": started.start, "worker": "fixture-impl", "scope": scope})
        _ = claim.write_text(saved)
        before = self.open_passes(session)
        result = self.worker_launch(session, to="fixture-impl", behavior="complete")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("claimed by launcher", result.stderr)
        self.assertIn(f"fixture-impl is claimed by launcher {os.getpid()}", result.stderr)
        self.assertEqual(self.open_passes(session), before)
        self.assertEqual(self.launch_records(session, "pass_started"), [])
        self.assertEqual(claim.read_text(), saved)
        self.assertFalse((self.root / "delivered_message.txt").exists())

    @process_boundary
    def test_a_reused_live_pid_recovers_before_a_claude_follow(self) -> None:
        session = self.start_phase("reused-pid-claim")
        _ = self.claude_worker(session)
        claim = session / "impl_claim_fixture-impl.json"
        started = process_start.process_start(os.getpid())
        assert isinstance(started, process_start.Started)
        worker_state.publish_worker_claim(claim, "fixture-impl")
        scope = cast(dict[str, object], json.loads(claim.read_text()))["scope"]
        _ = claim.write_text(json.dumps({
            "scope": scope, "pid": os.getpid(), "started": started.start + "-previous", "worker": "fixture-impl",
        }))
        _ = (session / "impl_status_fixture-impl").write_text("implementing\n")
        result = self.worker_launch(session, to="fixture-impl", behavior="complete")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((session / "impl_status_fixture-impl").read_text(), "implemented\n")
        self.assertFalse(claim.exists())
        self.assertTrue((self.root / "delivered_message.txt").exists())
        started_pass, = self.launch_records(session, "pass_started")
        self.assertEqual(started_pass["worker_name"], "fixture-impl")
        finished_pass, = self.launch_records(session, "pass_finished")
        self.assertEqual(finished_pass["status"], "completed")

    @process_boundary
    def test_stale_claim_recovers_before_a_claude_follow(self) -> None:
        session = self.start_phase("stale-claim")
        _ = self.claude_worker(session)
        claim = session / "impl_claim_fixture-impl.json"
        with subprocess.Popen([sys.executable, "-c", "pass"]) as child:
            _ = child.wait(timeout=TEST_HANG_GUARD_SECS)
        worker_state.publish_worker_claim(claim, "fixture-impl")
        scope = cast(dict[str, object], json.loads(claim.read_text()))["scope"]
        _ = claim.write_text(json.dumps({"scope": scope, "pid": child.pid, "started": "ended-launcher-start", "worker": "fixture-impl"}))
        result = self.worker_launch(session, to="fixture-impl", behavior="complete")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((session / "impl_status_fixture-impl").read_text(), "implemented\n")
        self.assertFalse(claim.exists())
        self.assertTrue((self.root / "delivered_message.txt").exists())

    def test_a_malformed_claim_allows_an_idle_claude_worker_to_run(self) -> None:
        session = self.start_phase("malformed-claim")
        _ = self.claude_worker(session)
        _ = (session / "impl_claim_fixture-impl.json").write_text("{bad json")
        result = self.worker_launch(session, to="fixture-impl")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        started_pass, = self.launch_records(session, "pass_started")
        self.assertEqual(started_pass["worker_name"], "fixture-impl")

    def test_an_empty_claim_preserves_completed_status_when_the_worker_is_busy(self) -> None:
        session = self.start_phase("empty-claim-busy")
        _ = self.claude_worker(session)
        _ = (session / "impl_claim_fixture-impl.json").write_text("{}")
        status = session / "impl_status_fixture-impl"
        _ = status.write_text("implemented\n")
        _ = (self.root / "worker_state").write_text("busy\n")
        result = self.worker_launch(session, to="fixture-impl")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("busy or gone", result.stderr)
        self.assertEqual(status.read_text(), "implemented\n")
        self.assertEqual(self.launch_records(session, "pass_started"), [])
        self.assertFalse((self.root / "delivered_message.txt").exists())
