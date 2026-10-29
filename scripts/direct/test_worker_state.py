"""First-read conversions for runs opened before worker names were introduced."""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
import unittest
from pathlib import Path

from scripts.direct.worker_state import worker_claim_lock, worker_ledger, worker_record_path, worker_slot


class WorkerStateTests(unittest.TestCase):
    def test_first_read_converts_identity_claim_and_ledger_to_worker_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            identity = run / "impl_seat_impl.json"
            _ = identity.write_text(json.dumps({"name": "builder", "slot": "impl", "family": "codex",
                                               "model": "model", "seat_id": "thread-one"}))
            claim = run / "impl_claim_impl.json"
            _ = claim.write_text(json.dumps({"pid": 123, "seat": "builder", "turn_id": "turn-one"}))
            _ = (run / "seats").write_text("id-one\tbuilder\n")
            with worker_claim_lock(run):
                self.assertEqual(json.loads((run / "impl_worker_impl.json").read_text()),
                                 {"name": "builder", "slot": "impl", "family": "codex",
                                  "model": "model", "worker_id": "thread-one"})
                self.assertEqual(json.loads(claim.read_text()),
                                 {"pid": 123, "worker": "builder", "turn_id": "turn-one"})
                self.assertEqual(worker_ledger(run).read_text(), "id-one\tbuilder\n")
            self.assertFalse(identity.exists())
            self.assertTrue((run / "seats").samefile(run / "workers"))
            self.assertFalse((run / "impl_seat_claim.lock").exists())
            with worker_claim_lock(run):
                self.assertEqual(worker_ledger(run), run / "workers")
            self.assertEqual(sorted(path.name for path in run.iterdir()),
                             ["impl_claim_impl.json", "impl_worker_claim.lock", "impl_worker_impl.json", "seats", "workers"])

    def test_an_existing_repair_identity_keeps_its_file_and_a_new_impl_gets_a_distinct_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            repair = {"name": "project-fix", "slot": "impl", "family": "codex", "model": "model", "worker_id": "thread-fix"}
            _ = (run / "mesh_roster.json").write_text(json.dumps({"project-fix": {"role": "fix"}}))
            identity = run / "impl_worker_impl.json"
            _ = identity.write_text(json.dumps(repair))
            with worker_claim_lock(run):
                self.assertEqual(json.loads(identity.read_text()), repair)
                separate = worker_record_path(run, "project-impl", "impl", role="impl")
                self.assertNotEqual(separate, identity)
                _ = separate.write_text(json.dumps({"name": "project-impl", "slot": "impl",
                                                    "role": "impl", "worker_id": "thread-impl"}))
            self.assertEqual(worker_record_path(run, "project-fix", "impl", role="impl"), identity)
            self.assertEqual(worker_record_path(run, "project-impl", "impl", role="fix"), separate)
            self.assertEqual(json.loads(identity.read_text()), repair)

    def test_record_path_uses_stored_role_instead_of_the_worker_address(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            _ = (run / "mesh_roster.json").write_text(json.dumps({
                "w7": {"role": "fix"}, "misleading-fix": {"role": "impl"},
            }))
            self.assertEqual(worker_record_path(run, "w7", "impl"), run / "impl_worker_fix.json")
            self.assertEqual(worker_record_path(run, "misleading-fix", "impl"), run / "impl_worker_impl.json")

    def test_explicit_claude_repair_role_uses_its_own_record_without_a_roster(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            original = run / "impl_worker_impl.json"
            _ = original.write_text('{"name":"w7","role":"impl","family":"claude"}')
            before = original.read_bytes()
            path = worker_record_path(run, "w8", "impl", role="fix")
            self.assertEqual(path, run / "impl_worker_fix.json")
            _ = path.write_text('{"name":"w8","role":"fix","family":"claude"}')
            self.assertEqual(original.read_bytes(), before)

    def test_worker_slot_reads_the_slot_the_identity_was_recorded_under(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            _ = (run / "impl_worker_impl.json").write_text(json.dumps({"name": "project-impl", "slot": "impl"}))
            _ = (run / "impl_worker_fix.json").write_text(json.dumps({"name": "project-fix", "slot": "impl"}))
            _ = (run / "impl_worker_test.json").write_text("not json")
            self.assertEqual(worker_slot(run, "project-impl"), "impl")
            self.assertEqual(worker_slot(run, "project-fix"), "impl")
            self.assertIsNone(worker_slot(run, "project-test"))

    def test_an_existing_worker_identity_is_stable_across_assignment_roles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            identity = run / "impl_worker_impl.json"
            _ = identity.write_text(json.dumps({"name": "w7", "slot": "impl", "role": "impl",
                                               "family": "claude", "worker_id": "original-session"}))
            for role in ("fix", "review", "test", "impl"):
                with self.subTest(role=role), worker_claim_lock(run):
                    self.assertEqual(worker_record_path(run, "w7", "impl", role=role), identity)
                    self.assertEqual(json.loads(identity.read_text())["worker_id"], "original-session")

    def test_converted_lock_keeps_excluding_a_launcher_with_an_open_old_handle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            previous = run / "impl_seat_claim.lock"
            with previous.open("a+") as holder:
                with worker_claim_lock(run) as reader:
                    self.assertTrue(previous.exists())
                    self.assertEqual((run / "impl_worker_claim.lock").stat().st_ino,
                                     os.fstat(holder.fileno()).st_ino)
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.assertFalse(reader.closed)
                fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_old_launcher_reopens_the_same_lock_after_conversion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            previous = run / "impl_seat_claim.lock"
            previous.touch()
            with worker_claim_lock(run):
                pass
            with previous.open("a+") as holder:
                fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with (run / "impl_worker_claim.lock").open("a+") as reader:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(reader, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with worker_claim_lock(run) as reader:
                with previous.open("a+") as holder:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertFalse(reader.closed)

    def test_old_launcher_append_after_conversion_reaches_the_worker_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            previous = run / "seats"
            _ = previous.write_text("id-one\tbuilder\n")
            current = worker_ledger(run)
            with previous.open("a") as writer:
                _ = writer.write("id-two\treviewer\n")
            self.assertEqual(worker_ledger(run), current)
            self.assertEqual(current.read_text(), "id-one\tbuilder\nid-two\treviewer\n")

    def test_existing_worker_ledger_is_the_only_ledger_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            _ = (run / "seats").write_text("old-id\told-worker\n")
            _ = (run / "workers").write_text("new-id\tnew-worker\n")
            self.assertEqual(worker_ledger(run).read_text(), "new-id\tnew-worker\n")
