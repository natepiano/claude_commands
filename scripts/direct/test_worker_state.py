"""Worker task records use fixed staff references."""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from scripts.direct import assign, workers
from scripts.direct.worker_state import worker_claim_lock, worker_record_path, worker_slot


class WorkerStateTests(unittest.TestCase):
    def test_an_existing_repair_identity_keeps_its_file_and_a_new_impl_gets_a_distinct_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            repair = {"slot": "impl", "model": "model", "staff_id": "fixed-fix"}
            _ = (run / "mesh_roster.json").write_text(json.dumps({"project-fix": {"staff_id": "fixed-fix"}, "project-impl": {"staff_id": "fixed-impl"}}))
            identity = run / "impl_worker_impl.json"
            _ = identity.write_text(json.dumps(repair))
            with worker_claim_lock(run):
                self.assertEqual(json.loads(identity.read_text()), repair)
                separate = worker_record_path(run, "project-impl", "project-impl")
                self.assertNotEqual(separate, identity)
                _ = separate.write_text(json.dumps({"slot": "project-impl", "staff_id": "fixed-impl"}))
            self.assertEqual(worker_record_path(run, "project-fix", "impl"), identity)
            self.assertEqual(worker_record_path(run, "project-impl", "impl"), separate)
            self.assertEqual(json.loads(identity.read_text()), repair)

    def test_record_path_uses_the_requested_slot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            _ = (run / "mesh_roster.json").write_text(json.dumps({
                "w7": {"staff_id": "fixed-fix"}, "misleading-fix": {"staff_id": "fixed-impl"},
            }))
            self.assertEqual(worker_record_path(run, "w7", "w7"), run / "impl_worker_w7.json")
            self.assertEqual(worker_record_path(run, "misleading-fix", "impl"), run / "impl_worker_impl.json")

    def test_explicit_claude_repair_slot_uses_its_own_record_without_a_roster(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            original = run / "impl_worker_impl.json"
            _ = original.write_text('{"staff_id":"fixed-impl","slot":"impl"}')
            before = original.read_bytes()
            path = worker_record_path(run, "w8", "fix")
            self.assertEqual(path, run / "impl_worker_fix.json")
            _ = path.write_text('{"staff_id":"fixed-fix","slot":"impl"}')
            self.assertEqual(original.read_bytes(), before)

    def test_worker_slot_reads_the_slot_the_identity_was_recorded_under(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            _ = (run / "mesh_roster.json").write_text(json.dumps({
                "project-impl": {"staff_id": "fixed-impl"}, "project-fix": {"staff_id": "fixed-fix"}}))
            _ = (run / "impl_worker_impl.json").write_text(json.dumps({"staff_id": "fixed-impl", "slot": "impl"}))
            _ = (run / "impl_worker_fix.json").write_text(json.dumps({"staff_id": "fixed-fix", "slot": "impl"}))
            _ = (run / "impl_worker_test.json").write_text("not json")
            self.assertEqual(worker_slot(run, "project-impl"), "impl")
            self.assertEqual(worker_slot(run, "project-fix"), "impl")
            self.assertIsNone(worker_slot(run, "project-test"))

    def test_an_existing_worker_identity_keeps_its_staff_reference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            identity = run / "impl_worker_impl.json"
            _ = (run / "mesh_roster.json").write_text(
                json.dumps({"w7": {"staff_id": "fixed-worker"}}))
            _ = identity.write_text(json.dumps({"slot": "impl", "staff_id": "fixed-worker"}))
            with worker_claim_lock(run):
                self.assertEqual(worker_record_path(run, "w7", "impl"), identity)
                self.assertEqual(json.loads(identity.read_text())["staff_id"], "fixed-worker")

    def dispatch(self, run: Path, name: str) -> assign.Dispatch:
        return assign.Dispatch(assign.Arguments(), run, run, run / "prompt", "7", "fix", name,
                               "codex", "model", "high", "default", {}, {})

    def test_two_fix_workers_hold_distinct_claims_at_the_same_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            _ = (run / "mesh_roster.json").write_text(json.dumps({
                "fix7": {"staff_id": "first"}, "fix7b": {"staff_id": "second"}}))
            with patch.object(assign.Dispatch, "claim_follow"), ThreadPoolExecutor(max_workers=2) as pool:
                claims = list(pool.map(assign.claim_worker,
                                       [self.dispatch(run, name) for name in ("fix7", "fix7b")]))
            self.assertEqual({claim.slot for claim in claims}, {"fix7", "fix7b"})
            self.assertEqual(len({claim.identity_path for claim in claims}), 2)
            for claim in claims:
                self.assertEqual(json.loads((run / f"impl_claim_{claim.slot}.json").read_text())["pid"], os.getpid())

    def test_refused_same_name_preserves_phase_status_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            _ = (run / "mesh_roster.json").write_text(json.dumps({"fix7": {"staff_id": "first"}}))
            dispatch = self.dispatch(run, "fix7")
            with patch.object(assign.Dispatch, "claim_follow"):
                claim = assign.claim_worker(dispatch)
            phase = run / "phases/7"
            phase.mkdir(parents=True)
            files = [phase / name for name in ("fix_status", "fix_summary.txt", "fix7_status", "fix7_summary.txt")]
            for path in files:
                _ = path.write_text("first worker evidence\n")
            before = {path: path.read_bytes() for path in files}
            errors = io.StringIO()
            with patch.object(workers, "shown_name", return_value="first"), redirect_stderr(errors):
                self.assertEqual(assign.execute(dispatch, []), 2)
            self.assertIn(f"claimed by launcher {os.getpid()}", errors.getvalue())
            self.assertEqual({path: path.read_bytes() for path in files}, before)
            self.assertTrue((run / f"impl_claim_{claim.slot}.json").exists())
