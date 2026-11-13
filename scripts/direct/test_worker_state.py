"""Worker task records use fixed staff references."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from contextlib import redirect_stderr
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from scripts.direct import worker_state
from scripts.herdr import worker_panes
from scripts.production import worker
from scripts.direct.worker_state import worker_claim_lock, worker_record_path, worker_slot, worker_status_path
from scripts.production import staff
from scripts.production.staff_test_support import StaffTestCase, fixture_worker, unit_membership
from scripts.lib import process_start
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class WorkerClaimTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        scope = json.dumps(["test-host", "test-boot", "test-namespace"])
        scope_patch = patch.object(worker_state, "_claim_scope", return_value=scope)
        _ = scope_patch.start()
        self.addCleanup(scope_patch.stop)

    def write_claim(self, run: Path, pid: int, started: str) -> Path:
        path = run / "impl_claim_impl.json"
        worker_state.publish_worker_claim(path, "impl")
        record = cast(dict[str, object], json.loads(path.read_text()))
        record.update({"pid": pid, "started": started})
        _ = path.write_text(json.dumps(record))
        return path

    def test_an_absent_file_has_no_worker_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            claim = Path(directory) / "claim.json"
            self.assertIsInstance(worker_state.read_worker_claim(claim), worker_state.NoWorkerClaim)

    def test_malformed_json_has_no_worker_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            claim = Path(directory) / "claim.json"
            _ = claim.write_text("{bad json")
            self.assertIsInstance(worker_state.read_worker_claim(claim), worker_state.NoWorkerClaim)

    def test_an_empty_record_has_no_worker_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            claim = Path(directory) / "claim.json"
            _ = claim.write_text("{}")
            self.assertIsInstance(worker_state.read_worker_claim(claim), worker_state.NoWorkerClaim)

    def test_a_live_pid_without_a_start_keeps_its_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            claim = run / "impl_claim_impl.json"
            worker_state.publish_worker_claim(claim, "impl")
            record = cast(dict[str, object], json.loads(claim.read_text()))
            del record["started"]
            _ = claim.write_text(json.dumps(record))
            status = run / "impl_status_impl"
            _ = status.write_text("implementing\n")
            self.assertEqual(worker_state.read_worker_claim(claim), worker_state.HeldWorkerClaim(os.getpid()))
            _ = worker_state.worker_status_paths(run)
            self.assertEqual(status.read_text(), "implementing\n")
            self.assertFalse((run / "board.log").exists())

    def test_missing_or_foreign_scope_cannot_recover_a_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            claim = self.write_claim(run, os.getpid(), "different-start")
            original = cast(dict[str, object], json.loads(claim.read_text()))
            scope = cast(list[str], json.loads(str(original["scope"])))
            for part in ("missing", "host", "boot", "namespace", "unreadable"):
                with self.subTest(part=part):
                    record = dict(original)
                    if part == "missing":
                        del record["scope"]
                    elif part != "unreadable":
                        changed = list(scope)
                        changed[("host", "boot", "namespace").index(part)] += "-foreign"
                        record["scope"] = json.dumps(changed)
                    _ = claim.write_text(json.dumps(record))
                    status = run / "impl_status_impl"
                    _ = status.write_text("implementing\n")
                    with patch.object(worker_state, "_claim_scope", return_value="" if part == "unreadable" else original["scope"]):
                        self.assertEqual(worker_state.read_worker_claim(claim), worker_state.HeldWorkerClaim(os.getpid()))
                        _ = worker_state.worker_status_paths(run)
                    self.assertEqual(status.read_text(), "implementing\n")
                    self.assertFalse((run / "board.log").exists())

    def test_publication_records_the_launcher_start_and_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            claim = Path(directory) / "claim.json"
            started = process_start.process_start(os.getpid())
            assert isinstance(started, process_start.Started)
            worker_state.publish_worker_claim(claim, "impl")
            record = cast(dict[str, object], json.loads(claim.read_text()))
            scope = record.pop("scope")
            self.assertIsInstance(scope, str)
            self.assertTrue(scope)
            self.assertEqual(record, {"pid": os.getpid(), "started": started.start, "worker": "impl"})

    def test_publication_reports_an_unreadable_start_and_records_the_launcher_and_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            proc = run / "proc" / str(os.getpid())
            (proc / "stat").mkdir(parents=True)
            claim = run / "claim.json"
            errors = io.StringIO()
            with patch.dict(os.environ, {"VERIFY_PROC_ROOT": str(proc.parent)}), patch.object(sys, "platform", "linux"), redirect_stderr(errors):
                worker_state.publish_worker_claim(claim, "impl")
            record = cast(dict[str, object], json.loads(claim.read_text()))
            self.assertTrue(record.pop("scope"))
            self.assertEqual(record, {"pid": os.getpid(), "worker": "impl"})
            self.assertEqual(worker_state.read_worker_claim(claim), worker_state.HeldWorkerClaim(os.getpid()))
            diagnostic, = errors.getvalue().splitlines()
            self.assertTrue(diagnostic.startswith("anomaly: "), diagnostic)

    def test_the_same_process_start_keeps_the_claim_held(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            started = process_start.process_start(os.getpid())
            assert isinstance(started, process_start.Started)
            claim = self.write_claim(Path(directory), os.getpid(), started.start)
            self.assertEqual(worker_state.read_worker_claim(claim), worker_state.HeldWorkerClaim(os.getpid()))

    def test_another_process_start_ends_the_claim_despite_a_live_pid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            started = process_start.process_start(os.getpid())
            assert isinstance(started, process_start.Started)
            claim = self.write_claim(Path(directory), os.getpid(), started.start + "-previous")
            self.assertIsInstance(worker_state.read_worker_claim(claim), worker_state.EndedWorkerClaim)

    def test_a_gone_process_ends_the_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with subprocess.Popen([sys.executable, "-c", "pass"]) as child:
                _ = child.wait(timeout=TEST_HANG_GUARD_SECS)
            claim = self.write_claim(Path(directory), child.pid, "ended-launcher-start")
            self.assertIsInstance(worker_state.read_worker_claim(claim), worker_state.EndedWorkerClaim)

    def test_a_zombie_process_ends_the_claim_even_with_the_same_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            proc = run / "proc" / str(os.getpid())
            proc.mkdir(parents=True)
            _ = (proc / "stat").write_text(f"{os.getpid()} (launcher) Z " + "0 " * 18 + "123 0\n")
            claim = self.write_claim(run, os.getpid(), "123")
            with patch.dict(os.environ, {"VERIFY_PROC_ROOT": str(proc.parent)}), patch.object(sys, "platform", "linux"):
                self.assertIsInstance(worker_state.read_worker_claim(claim), worker_state.EndedWorkerClaim)

    def test_an_unreadable_process_start_keeps_the_claim_held(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            proc = run / "proc" / str(os.getpid())
            (proc / "stat").mkdir(parents=True)
            claim = self.write_claim(run, os.getpid(), "unreadable-launcher-start")
            with patch.dict(os.environ, {"VERIFY_PROC_ROOT": str(proc.parent)}), patch.object(sys, "platform", "linux"):
                self.assertEqual(worker_state.read_worker_claim(claim), worker_state.HeldWorkerClaim(os.getpid()))


class WorkerStateTests(StaffTestCase):
    def prepare_workers(self, run: Path, *names: str) -> list[staff.StaffRow]:
        parent = unit_membership("tasks", "unit")
        self.write_json(run / "progress_history_state.json", {"owner_staff_id": parent.id})
        return [fixture_worker(name, parent.id) for name in names]

    def test_temporary_status_copies_do_not_replace_the_current_phase_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            phase = run / "phases" / "build"
            copied = run / "tmp" / "fixture" / "phases" / "build"
            phase.mkdir(parents=True)
            copied.mkdir(parents=True)
            original = phase / "impl_status_impl"
            _ = original.write_text("implementing\n")
            _ = (run / "impl_status_impl").write_text("implemented\n")
            temporary = copied / "impl_status_impl"
            _ = temporary.write_text("implemented\n")
            os.utime(run / "impl_status_impl", (1, 1))
            os.utime(original, (2, 2))
            os.utime(temporary, (3, 3))
            self.assertEqual(worker_status_path(run, "impl").read_text(), "implementing\n")
            self.assertEqual(worker_status_path(run, "impl"), original)

    def test_an_existing_repair_identity_keeps_its_file_and_a_new_impl_gets_a_distinct_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            fix, impl = self.prepare_workers(run, "project-fix", "project-impl")
            repair = {"slot": "impl", "model": "model", "staff_id": fix.id}
            identity = run / "impl_worker_impl.json"
            _ = identity.write_text(json.dumps(repair))
            with worker_claim_lock(run):
                self.assertEqual(json.loads(identity.read_text()), repair)
                separate = worker_record_path(run, "project-impl", "project-impl")
                self.assertNotEqual(separate, identity)
                write_text_atomic(separate, json.dumps({"slot": "project-impl", "staff_id": impl.id}))
            self.assertEqual(worker_record_path(run, "project-fix", "impl"), identity)
            self.assertEqual(worker_record_path(run, "project-impl", "impl"), separate)
            self.assertEqual(json.loads(identity.read_text()), repair)

    def test_record_path_uses_the_requested_slot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            members = self.prepare_workers(run, "w7", "misleading-fix")
            self.assertEqual(worker_record_path(run, members[0].name, "w7"), run / "impl_worker_w7.json")
            self.assertEqual(worker_record_path(run, "misleading-fix", "impl"), run / "impl_worker_impl.json")

    def test_explicit_claude_repair_slot_uses_its_own_record_without_a_roster(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            member, = self.prepare_workers(run, "w8")
            original = run / "impl_worker_impl.json"
            _ = original.write_text('{"staff_id":"fixed-impl","slot":"impl"}')
            before = original.read_bytes()
            path = worker_record_path(run, member.name, "fix")
            self.assertEqual(path, run / "impl_worker_fix.json")
            _ = path.write_text('{"staff_id":"fixed-fix","slot":"impl"}')
            self.assertEqual(original.read_bytes(), before)

    def test_worker_slot_reads_the_slot_the_identity_was_recorded_under(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            impl, fix = self.prepare_workers(run, "project-impl", "project-fix")
            write_text_atomic(run / "impl_worker_impl.json", json.dumps({"staff_id": impl.id, "slot": "impl"}))
            write_text_atomic(run / "impl_worker_fix.json", json.dumps({"staff_id": fix.id, "slot": "impl"}))
            _ = (run / "impl_worker_test.json").write_text("not json")
            self.assertEqual(worker_slot(run, "project-impl"), "impl")
            self.assertEqual(worker_slot(run, "project-fix"), "impl")
            self.assertIsNone(worker_slot(run, "project-test"))

    def test_an_existing_worker_identity_keeps_its_staff_reference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            identity = run / "impl_worker_impl.json"
            member, = self.prepare_workers(run, "w7")
            write_text_atomic(identity, json.dumps({"slot": "impl", "staff_id": member.id}))
            with worker_claim_lock(run):
                self.assertEqual(worker_record_path(run, "w7", "impl"), identity)
                self.assertEqual(json.loads(identity.read_text())["staff_id"], member.id)

    def test_two_fix_workers_hold_distinct_claims_at_the_same_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            members = self.prepare_workers(run, "fix7", "fix7b")
            ready = Barrier(3, timeout=TEST_HANG_GUARD_SECS)
            release = Barrier(3, timeout=TEST_HANG_GUARD_SECS)
            held: list[worker_panes.WorkerLockHold] = []

            def claim(member: staff.StaffRow) -> None:
                assert isinstance(member.parent, staff.ReportsTo)
                with worker_panes.worker_lock(member.parent.id, staff.WorkerName.parse(member.name)) as lock:
                    assert isinstance(lock, worker_panes.WorkerLockHold)
                    held.append(lock)
                    _ = ready.wait()
                    _ = release.wait()

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(claim, member) for member in members]
                _ = ready.wait()
                try:
                    self.assertEqual({lock.name.value for lock in held}, {"fix7", "fix7b"})
                    self.assertEqual(len(held), 2)
                    for member in members:
                        assert isinstance(member.parent, staff.ReportsTo)
                        self.assertTrue(any(lock.owns(member) for lock in held))
                        with worker_panes.worker_lock(member.parent.id, staff.WorkerName.parse(member.name)) as attempt:
                            self.assertIsInstance(attempt, worker_panes.Kept)
                finally:
                    _ = release.wait()
                for future in futures:
                    future.result()

    def test_refused_same_name_preserves_phase_status_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            member, = self.prepare_workers(run, "fix7")
            assert isinstance(member.parent, staff.ReportsTo)
            phase = run / "phases/7"
            phase.mkdir(parents=True)
            files = [phase / name for name in ("fix_status", "fix_summary.txt", "fix7_status", "fix7_summary.txt")]
            for path in files:
                _ = path.write_text("first worker evidence\n")
            before = {path: path.read_bytes() for path in files}
            errors = io.StringIO()
            with worker_panes.worker_lock(member.parent.id, staff.WorkerName.parse(member.name)) as claim:
                assert isinstance(claim, worker_panes.WorkerLockHold)
                with redirect_stderr(errors):
                    result = worker.dispatch(member.parent.id, staff.WorkerName.parse(member.name), "fix", run / "prompt",
                        session_dir=run, slot=member.name, phase="7", kind="fix", launcher_command=[])
                self.assertEqual(result, worker.ExitCode.BUSY)
                self.assertIn("dispatch lock is held", errors.getvalue())
                self.assertEqual({path: path.read_bytes() for path in files}, before)
                self.assertTrue(claim.owns(member))
