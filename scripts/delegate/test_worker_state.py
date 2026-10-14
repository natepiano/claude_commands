"""First-read conversions for runs opened before worker names were introduced."""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
import unittest
from pathlib import Path

from scripts.delegate.worker_state import worker_claim_lock, worker_ledger


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
