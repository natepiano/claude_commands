"""Worker task records use fixed staff references."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

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
                separate = worker_record_path(run, "project-impl", "project-impl", role="impl")
                self.assertNotEqual(separate, identity)
                _ = separate.write_text(json.dumps({"slot": "project-impl", "staff_id": "fixed-impl"}))
            self.assertEqual(worker_record_path(run, "project-fix", "impl", role="impl"), identity)
            self.assertEqual(worker_record_path(run, "project-impl", "impl", role="fix"), separate)
            self.assertEqual(json.loads(identity.read_text()), repair)

    def test_record_path_uses_the_requested_task_role(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            _ = (run / "mesh_roster.json").write_text(json.dumps({
                "w7": {"staff_id": "fixed-fix"}, "misleading-fix": {"staff_id": "fixed-impl"},
            }))
            self.assertEqual(worker_record_path(run, "w7", "impl", role="fix"), run / "impl_worker_fix.json")
            self.assertEqual(worker_record_path(run, "misleading-fix", "impl", role="impl"), run / "impl_worker_impl.json")

    def test_explicit_claude_repair_role_uses_its_own_record_without_a_roster(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            original = run / "impl_worker_impl.json"
            _ = original.write_text('{"staff_id":"fixed-impl","slot":"impl"}')
            before = original.read_bytes()
            path = worker_record_path(run, "w8", "impl", role="fix")
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

    def test_an_existing_worker_identity_is_stable_across_assignment_roles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            identity = run / "impl_worker_impl.json"
            _ = (run / "mesh_roster.json").write_text(
                json.dumps({"w7": {"staff_id": "fixed-worker"}}))
            _ = identity.write_text(json.dumps({"slot": "impl", "staff_id": "fixed-worker"}))
            for role in ("fix", "review", "test", "impl"):
                with self.subTest(role=role), worker_claim_lock(run):
                    self.assertEqual(worker_record_path(run, "w7", "impl", role=role), identity)
                    self.assertEqual(json.loads(identity.read_text())["staff_id"], "fixed-worker")
