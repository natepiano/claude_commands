"""Exercise checkpoint merges against disposable repositories and command stubs."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from typing import cast, final

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Importable once merge_checkpoint has put scripts/lib on the path.


from scripts.production.merge_checkpoint_test_support import MergeCheckpointFixture, SCRIPT, configure_test_environment
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


def setUpModule() -> None:
    configure_test_environment()


@final
class MergeCheckpointTests(MergeCheckpointFixture):
    def test_checkpoint_receipt_names_pushed_merge_and_source_once_on_retry(self) -> None:
        checkpoint = self.checkpoint()
        first = self.run_checkpoint(checkpoint)
        self.assertEqual(first.process.returncode, 0, first.process.stdout + first.process.stderr)
        merge = self.git("rev-parse", "HEAD")
        receipt, = self.receipts()
        self.assertEqual((receipt["project"], receipt["phase_id"], receipt["merge_commit"],
                          receipt["source_checkpoint"], receipt["target_branch"]),
                         ("hana", "2", merge, checkpoint, "production"))
        self.assertEqual(receipt["timestamp_epoch"], float(self.git("show", "-s", "--format=%ct", merge)))
        self.assertTrue(str(receipt["merged_at"]).endswith("+00:00"))
        retried = self.run_checkpoint(checkpoint)
        self.assertEqual(retried.process.returncode, 0, retried.process.stdout + retried.process.stderr)
        self.assertEqual(self.receipts(), [receipt])
        readback = subprocess.run([sys.executable, str(SCRIPT.parent.parent / "direct" / "progress_history.py"),
                                   "receipts", "--project", "hana", "--merge-commit", merge], cwd=self.checkout, env=self.env,
                                  capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(readback.returncode, 0, readback.stderr)
        report = cast(dict[str, object], json.loads(readback.stdout))
        self.assertEqual((report["covered_n"], report["missing_n"], report["receipts"]), (1, 0, [receipt]))
    def test_rolled_back_checkpoint_writes_no_receipt(self) -> None:
        checkpoint = self.checkpoint()
        _ = (self.state / "fail-checkpoint-test").touch()
        failed = self.run_checkpoint(checkpoint)
        self.assertNotEqual(failed.process.returncode, 0)
        self.assertEqual(self.git("rev-parse", "HEAD"), failed.before)
        self.assertEqual(self.receipts(), [])
    def test_abbreviated_checkpoint_keeps_full_hash_wait_attribution_and_receipt(self) -> None:
        checkpoint = self.checkpoint()
        short = self.git("rev-parse", "--short", checkpoint, cwd=self.unit)
        session = self.start_history_run("abbreviated-checkpoint", self.unit)
        _ = self.record_history("start-phase", "--session-dir", str(session), "--phase-id", "2", "--phase-title", "Gate")
        _ = self.record_history("checkpoint", "--session-dir", str(session), "--phase", "2", "--commit", checkpoint)
        _ = self.record_history("finish-phase", "--session-dir", str(session), "--phase-id", "2", "--status", "completed")

        merged = self.run_checkpoint(short)
        self.assertEqual(merged.process.returncode, 0, merged.process.stdout + merged.process.stderr)
        report = self.record_history("waits")
        self.assertEqual(report["attribution_miss_n"], 0)
        wait, = cast(list[dict[str, object]], report["waits"])
        attribution = cast(dict[str, object], wait["wait_attribution"])
        self.assertEqual(attribution["state"], "CheckpointPhase")
        self.assertEqual(wait["checkpoint_commit"], checkpoint)
        phase, = cast(list[dict[str, object]], report["phases"])
        self.assertEqual(wait["phase_instance_id"], phase["phase_instance_id"])
        receipt, = self.receipts()
        self.assertEqual(receipt["source_checkpoint"], checkpoint)

        retried = self.run_checkpoint(checkpoint)
        self.assertEqual(retried.process.returncode, 0, retried.process.stdout + retried.process.stderr)
        self.assertEqual(self.receipts(), [receipt])
    def test_child_receipt_survives_retry_and_rebuilds_a_missed_write(self) -> None:
        _ = self.child_production()
        checkpoint = self.checkpoint()
        first = self.run_handoff(self.git("rev-parse", "--short", checkpoint, cwd=self.unit))
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        merge = self.git("rev-parse", "HEAD")
        receipt, = self.receipts()
        self.assertEqual((receipt["merge_commit"], receipt["source_checkpoint"], receipt["issue"]),
                         (merge, checkpoint, "example-style hand-off"))
        retried = self.run_handoff(checkpoint)
        self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)
        self.assertEqual(self.receipts(), [receipt])
        (self.root / "history" / "merge_receipts.jsonl").unlink()
        recovered = self.run_handoff(checkpoint)
        self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
        self.assertEqual(self.receipts(), [receipt])
    def test_receipt_reader_names_a_merge_that_has_no_receipt(self) -> None:
        checkpoint = self.checkpoint()
        _ = self.git("merge", "--no-ff", "-m", "fixture merge without recording", checkpoint)
        merge = self.git("rev-parse", "HEAD")
        result = subprocess.run([sys.executable, str(SCRIPT.parent.parent / "direct" / "progress_history.py"),
                                 "receipts", "--merge-commit", merge], cwd=self.checkout, env=self.env,
                                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = cast(dict[str, object], json.loads(result.stdout))
        self.assertEqual((report["covered_n"], report["missing_n"], report["missing"]),
                         (0, 1, [{"state": "ReceiptMissing", "merge_commit": merge}]))
    def test_gate_wait_uses_the_units_named_phase_and_closes_on_red(self) -> None:
        checkpoint = self.checkpoint()
        session = self.root / "unit-runs" / "alpha-run"
        recorder = SCRIPT.parent.parent / "direct" / "progress_history.py"
        for arguments in (
            ["start-run", "--session-dir", str(session), "--working-dir", str(self.unit),
             "--main-family", "codex", "--main-model", "fixture", "--main-effort", "high",
             "--main-session-id", "unit-director"],
            ["start-phase", "--session-dir", str(session), "--phase-id", "2", "--phase-title", "Gate"],
            ["checkpoint", "--session-dir", str(session), "--phase", "2", "--commit", checkpoint],
            ["start-phase", "--session-dir", str(session), "--phase-id", "3", "--phase-title", "Parallel"],
        ):
            result = subprocess.run([sys.executable, str(recorder), *arguments], cwd=self.unit, env=self.env,
                                    capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 0, result.stderr)
        _ = (self.state / "fail-checkpoint-test").touch()
        failed = self.run_checkpoint(checkpoint)
        self.assertNotEqual(failed.process.returncode, 0)
        rows = [cast(dict[str, object], json.loads(line)) for line in
                (self.root / "history" / "runs" / "alpha-run.jsonl").read_text().splitlines()]
        rows.extend(cast(dict[str, object], json.loads(line)) for line in
                    (self.root / "history" / "wait_attribution.jsonl").read_text().splitlines())
        started, = [row for row in rows if row["event_type"] == "wait_started"]
        finished, = [row for row in rows if row["event_type"] == "wait_finished"]
        phase, = [row for row in rows if row["event_type"] == "phase_started" and row["phase_id"] == "2"]
        self.assertEqual((started["wait_kind"], started["phase_instance_id"], started["unit"], started["owner_session"]),
                         ("WaitingForMergeGate", phase["phase_instance_id"], "alpha-unit", "unit-director"))
        self.assertEqual(started["wait_id"], finished["wait_id"])
        self.assertEqual(cast(dict[str, object], finished["wait_end"])["state"], "Finished")
    def test_merge_after_phase_finish_records_the_checkpoint_instances_gate_wait(self) -> None:
        checkpoint = self.checkpoint()
        session = self.root / "unit-runs" / "finished-alpha"
        recorder = SCRIPT.parent.parent / "direct" / "progress_history.py"
        for arguments in (
            ["start-run", "--session-dir", str(session), "--working-dir", str(self.unit),
             "--main-family", "codex", "--main-model", "fixture", "--main-session-id", "unit-director"],
            ["start-phase", "--session-dir", str(session), "--phase-id", "2", "--phase-title", "Finished"],
            ["checkpoint", "--session-dir", str(session), "--phase", "2", "--commit", checkpoint],
            ["finish-phase", "--session-dir", str(session), "--phase-id", "2", "--status", "completed"],
        ):
            recorded = subprocess.run([sys.executable, str(recorder), *arguments], env=self.env, cwd=self.unit,
                                      capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(recorded.returncode, 0, recorded.stderr)
        merged = self.run_checkpoint(checkpoint)
        self.assertEqual(merged.process.returncode, 0, merged.process.stdout + merged.process.stderr)
        read = subprocess.run([sys.executable, str(recorder), "waits", "--session-dir", str(session)],
                              env=self.env, cwd=self.unit, capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(read.returncode, 0, read.stderr)
        report = cast(dict[str, object], json.loads(read.stdout))
        wait, = cast(list[dict[str, object]], report["waits"])
        phase, = cast(list[dict[str, object]], report["phases"])
        self.assertEqual((wait["wait_kind"], wait["phase_instance_id"], phase["wait_n"]),
                         ("WaitingForMergeGate", phase["phase_instance_id"], 1))
        self.assertEqual(cast(dict[str, object], wait["wait_end"])["state"], "Finished")
    def test_checkpoint_event_selects_the_first_repeated_phase_instance(self) -> None:
        for across_runs in (False, True):
            for second_active in (False, True):
                with self.subTest(across_runs=across_runs, second_active=second_active):
                    # Each matrix cell owns a disposable repository and history root.
                    fixture = MergeCheckpointTests("runTest")
                    fixture.setUp()
                    try:
                        fixture.assert_checkpoint_provenance(across_runs=across_runs, second_active=second_active,
                                                             checkpoint_events=1)
                    finally:
                        self.assertTrue(fixture.doCleanups())
    def test_missing_or_duplicate_checkpoint_events_make_every_candidate_uncertain(self) -> None:
        for across_runs in (False, True):
            for second_active in (False, True):
                for checkpoint_events in (0, 2):
                    with self.subTest(across_runs=across_runs, second_active=second_active, checkpoint_events=checkpoint_events):
                        fixture = MergeCheckpointTests("runTest")
                        fixture.setUp()
                        try:
                            fixture.assert_checkpoint_provenance(across_runs=across_runs, second_active=second_active,
                                                                 checkpoint_events=checkpoint_events)
                        finally:
                            self.assertTrue(fixture.doCleanups())
    def test_child_handoff_wait_has_production_ownership_without_phase_coverage(self) -> None:
        _ = self.child_production()
        # A real child run may have phases; its branch checkpoint still belongs to the production.
        session = self.start_history_run("child", self.unit)
        _ = self.record_history("start-phase", "--session-dir", str(session), "--phase-id", "2", "--phase-title", "Child unit")
        checkpoint = self.checkpoint()
        merged = self.run_handoff(checkpoint)
        self.assertEqual(merged.returncode, 0, merged.stdout + merged.stderr)
        report = self.record_history("waits")
        wait, = cast(list[dict[str, object]], report["waits"])
        attribution = cast(dict[str, object], wait["wait_attribution"])
        self.assertEqual(attribution["state"], "ProductionHandoff")
        self.assertTrue(attribution["reason"])
        self.assertNotIn("phase_instance_id", wait)
        self.assertEqual(report["missing_record_n"], 0)
        phase, = cast(list[dict[str, object]], report["phases"])
        self.assertEqual((phase["state"], phase["wait_n"]), ("Covered", 0))


if __name__ == "__main__":
    _ = unittest.main()
