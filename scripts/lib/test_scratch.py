#!/usr/bin/env python3
"""Tests for scratch.py: folders that belong to a task and go when it ends."""
from __future__ import annotations

import json
import fcntl
import os
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Generator, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event, current_thread
from typing import TYPE_CHECKING, cast, override
from unittest import mock

if TYPE_CHECKING:
    from . import scratch
    from ..lint import sweep
    from ..style import style_sync
else:
    import scratch
    from scripts.lint import sweep
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "style"))
    import style_sync

RUN = "run:2a858b68-0f9a-4af2-b3cf-a776e6b180cb"
ROW = "row:cargo-liner-C54"


class ScratchTests(unittest.TestCase):
    base: Path = Path()

    @override
    def setUp(self) -> None:
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(mock.patch.dict(os.environ, {
            scratch.ROOT_ENV: str(self.base / "scratch"), scratch.VISIBLE_ROOT_ENV: str(self.base / "repro"),
            scratch.HOLDS_ENV: str(self.base / "holds.json"), "CLAUDE_CODE_SESSION_ID": "maker-session"}))

    def git(self, repo: Path, *arguments: str) -> None:
        _ = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *arguments],
                           check=True, capture_output=True)

    def copy(self, name: str = "a", task: str = "gate:copy", **details: str) -> Path:
        copies = self.base / "copies"
        os.environ["SCRATCH_COPIES_ROOT"] = str(copies)
        self.addCleanup(os.environ.pop, "SCRATCH_COPIES_ROOT", None)
        path = copies / "show-maker" / name
        path.mkdir(parents=True)
        self.git(path, "init", "-q")
        self.register(path, task, **details)
        return path

    def register(self, path: Path, task: str, **details: str) -> None:
        scratch.register_copy(path, task=task, owner_unit="maker-unit", source_path=str(path),
                              source_commit="a" * 40, copy_commit="b" * 40, target_mode="none",
                              seed_source="", borrows_from="", **details)

    def shared_source(self, name: str = "source", origin: str = "https://example.invalid/team/sample.git") -> Path:
        source = self.base / name
        source.mkdir()
        self.git(source, "init", "-q")
        self.git(source, "commit", "-qm", "base", "--allow-empty")
        self.git(source, "remote", "add", "origin", origin)
        self.enterContext(mock.patch.dict(os.environ, {
            "SCRATCH_SHARED_BUILD_ROOT": str(self.base / "shared-builds"),
            "SCRATCH_COPIES_ROOT": str(self.base / "copies"),
        }))
        return source

    def test_two_clones_reuse_one_build_folder_after_their_task_ends(self) -> None:
        source = self.shared_source()
        copies = self.base / "copies" / "maker"
        copies.mkdir(parents=True)
        folders: list[Path] = []
        for name in ("a", "b"):
            copy = copies / name
            self.git(source, "clone", "-q", str(source), str(copy))
            result = scratch.shared_build_folder(copy)
            self.assertIsInstance(result, scratch.SharedBuildFolder)
            assert isinstance(result, scratch.SharedBuildFolder)
            scratch.register_copy(copy, task="gate:shared-build", owner_unit="test", source_path=str(source),
                                  source_commit="a" * 40, copy_commit="b" * 40, target_mode="shared",
                                  seed_source="", borrows_from="", shared_target=str(result.path))
            record = scratch.task_of(copy)
            assert isinstance(record, scratch.Registered)
            self.assertEqual(record.fields["shared_target"], str(result.path))
            folders.append(result.path)
        self.assertEqual(folders[0], folders[1])
        artifact = folders[0] / "artifact"
        _ = artifact.write_text("reused")
        self.assertCountEqual(scratch.end("gate:shared-build"), [copies / "a", copies / "b"])
        self.assertEqual(artifact.read_text(), "reused")
        third = copies / "c"
        self.git(source, "clone", "-q", str(source), str(third))
        result = scratch.shared_build_folder(third)
        assert isinstance(result, scratch.SharedBuildFolder)
        self.assertEqual(result.path, folders[0])

    def test_shared_build_folder_depends_on_repository_key_not_checkout_name_or_commit(self) -> None:
        source = self.shared_source()
        other = self.shared_source("other", "https://example.invalid/other/sample.git")
        first = scratch.shared_build_folder(source)
        different = scratch.shared_build_folder(other)
        assert isinstance(first, scratch.SharedBuildFolder)
        assert isinstance(different, scratch.SharedBuildFolder)
        self.assertNotEqual(first.repo_identity, different.repo_identity)
        self.assertNotEqual(first.path, different.path)
        self.git(source, "commit", "-qm", "next", "--allow-empty")
        renamed = self.base / "renamed-checkout"
        _ = source.rename(renamed)
        again = scratch.shared_build_folder(renamed)
        self.assertEqual(again, first)
        self.assertTrue(first.path.is_relative_to(self.base / "shared-builds"))

    def test_repository_without_identity_has_no_shared_build_folder(self) -> None:
        source = self.base / "unborn"
        source.mkdir()
        self.git(source, "init", "-q")
        with mock.patch.dict(os.environ, {"SCRATCH_SHARED_BUILD_ROOT": str(self.base / "shared-builds")}):
            result = scratch.shared_build_folder(source)
        self.assertIsInstance(result, scratch.NoSharedBuildFolder)
        assert isinstance(result, scratch.NoSharedBuildFolder)
        self.assertTrue(result.reason)
        self.assertFalse((self.base / "shared-builds").exists())

    def test_shared_folder_has_one_use_stamp_refreshed_by_each_clone(self) -> None:
        source = self.shared_source()
        first = scratch.shared_build_folder(source)
        assert isinstance(first, scratch.SharedBuildFolder)
        stamp = first.path / scratch.USE_STAMP
        os.utime(stamp, (1, 1))
        clone = self.base / "second-clone"
        self.git(source, "clone", "-q", str(source), str(clone))
        self.git(clone, "remote", "set-url", "origin", "https://example.invalid/team/sample.git")
        second = scratch.shared_build_folder(clone)
        self.assertEqual(second, first)
        self.assertGreater(stamp.stat().st_mtime, 1)
        self.assertEqual(list((self.base / "shared-builds").rglob(scratch.USE_STAMP)), [stamp])

    def test_both_build_folder_helpers_create_stamped_caches_the_sweep_accepts(self) -> None:
        source = self.shared_source()
        started = time.time()
        shared = scratch.shared_build_folder(source)
        assert isinstance(shared, scratch.SharedBuildFolder)
        targets = (shared.path, style_sync.lint_target(source / ".git"))
        for target in targets:
            with self.subTest(target=target):
                # Supply build output, but let the helper supply every cache marker.
                fingerprint = target / "debug" / ".fingerprint" / "sample-0123456789abcdef"
                fingerprint.mkdir(parents=True)
                _ = (fingerprint / "bin-sample.json").write_text("{}")
                self.assertFalse((target / sweep.RUSTC_INFO).exists())
                self.assertTrue(sweep.is_cargo_target(str(target)), "The sweep must accept the helper's cache")
                self.assertGreaterEqual((target / sweep.USE_STAMP).stat().st_mtime, started)

    def test_record_and_lifetime_results_distinguish_missing_bad_and_external_end(self) -> None:
        self.assertIsInstance(scratch.task_of(self.base), scratch.NotRegistered)
        _ = (self.base / scratch.RECORD).write_text("bad json")
        bad = scratch.task_of(self.base)
        self.assertIsInstance(bad, scratch.UnreadableRecord)
        assert isinstance(bad, scratch.UnreadableRecord)
        self.assertIn(str(self.base / scratch.RECORD), bad.reason)
        self.assertIsInstance(scratch.task_end("until:bad"), scratch.Unknown)
        self.assertIsInstance(scratch.task_end("gate:outside"), scratch.Live)
        self.assertIsInstance(scratch.task_end("until:2000-01-01T0000Z"), scratch.Ended)

    def test_copy_record_is_atomic_and_excludes_only_its_two_metadata_files(self) -> None:
        path = self.copy()
        self.git(path, "init", "-q")
        scratch.register_copy(path, task="gate:copy", owner_unit="maker-unit", source_path="/source",
                              source_commit="a" * 40, copy_commit="b" * 40, target_mode="seed",
                              seed_source="/seed", borrows_from="/source/.git", land_target="origin/builds")
        fields = cast(dict[str, object], json.loads((path / scratch.RECORD).read_text()))
        self.assertEqual({key: fields[key] for key in ("source_path", "source_commit", "copy_commit", "target_mode",
                         "seed_source", "borrows_from", "owner_unit", "task", "land_target")},
                         {"source_path": "/source", "source_commit": "a" * 40, "copy_commit": "b" * 40,
                          "target_mode": "seed", "seed_source": "/seed", "borrows_from": "/source/.git",
                          "owner_unit": "maker-unit", "task": "gate:copy", "land_target": "origin/builds"})
        _ = (path / ".claude-busy").touch()
        self.assertIsNone(scratch.repo_problem(path))
        _ = (path / "ordinary").write_text("keep me")
        self.assertEqual(scratch.repo_problem(path), "it has uncommitted changes")
        before = (path / scratch.RECORD).read_bytes()
        with mock.patch.object(os, "replace", side_effect=OSError("injected record stop")):
            with self.assertRaises(OSError):
                scratch.hand_over(path, "next-unit", "gate:next-unit")
        self.assertEqual((path / scratch.RECORD).read_bytes(), before)
        scratch.hand_over(path, "next-unit", "gate:next-unit")
        self.assertIsInstance(scratch.task_of(path), scratch.Registered)

    def test_phase_lifetime_reads_current_and_parked_phases_and_fails_closed(self) -> None:
        run = self.base / "run"
        run.mkdir()
        progress = run / "progress_history_state.json"
        path = self.copy(task="phase:maker-unit/21", run_dir=str(run), phase_id="21")
        for state in ({"phase": {"id": "21", "status": "active"}},
                      {"phase": {"id": "22", "status": "active"},
                       "open_phases": [{"id": "21", "status": "active"}]}):
            _ = progress.write_text(json.dumps(state))
            self.assertIsInstance(scratch.lifetime(path), scratch.Live)
            self.assertEqual(scratch.end("phase:maker-unit/21"), [])
        _ = progress.write_text("bad")
        unknown = scratch.lifetime(path)
        self.assertIsInstance(unknown, scratch.Unknown)
        self.assertEqual(scratch.end("phase:maker-unit/21"), [])
        _ = progress.write_text(json.dumps({"phase": {"id": "21", "status": "finished"}}))
        self.assertIsInstance(scratch.lifetime(path), scratch.Ended)
        self.assertEqual(scratch.end("phase:maker-unit/21"), [path])

    def test_hand_over_recovers_after_a_process_stops_before_record_replace(self) -> None:
        path = self.copy()
        program = ("import os,sys; from pathlib import Path; from scripts.lib import scratch; "
                   "os.replace=lambda *args: os._exit(71); scratch.hand_over(Path(sys.argv[1]), 'next-unit', 'gate:next-unit')")
        stopped = subprocess.run([sys.executable, "-c", program, str(path)], check=False, capture_output=True)
        self.assertEqual(stopped.returncode, 71, stopped.stderr)
        scratch.hand_over(path, "next-unit", "gate:next-unit")
        record = scratch.task_of(path)
        self.assertIsInstance(record, scratch.Registered)
        assert isinstance(record, scratch.Registered)
        self.assertEqual(record.task, "gate:next-unit")
        self.assertEqual(record.fields["owner_unit"], "next-unit")
        self.assertIsNone(scratch.repo_problem(path))

    def test_landed_lifetime_requires_target_ancestry_and_saved_work(self) -> None:
        path = self.copy(task="landed:maker-unit/work", land_target="origin/main")
        self.git(path, "init", "-q")
        self.git(path, "commit", "-qm", "base", "--allow-empty")
        self.git(path, "remote", "add", "origin", str(self.base / "origin"))
        self.git(path, "update-ref", "refs/remotes/origin/work", "HEAD")
        self.assertIsInstance(scratch.lifetime(path), scratch.Unknown)
        self.git(path, "update-ref", "refs/remotes/origin/main", "HEAD")
        self.assertIsInstance(scratch.lifetime(path), scratch.Ended)
        _ = (path / "edit").write_text("unsaved")
        self.assertIsInstance(scratch.lifetime(path), scratch.Live)
        (path / "edit").unlink()
        self.git(path, "checkout", "-qb", "side")
        self.git(path, "commit", "-qm", "unpushed", "--allow-empty")
        self.assertIsInstance(scratch.lifetime(path), scratch.Live)

    def test_phase_end_survives_a_stop_when_the_recorder_moves_to_the_next_phase(self) -> None:
        run = self.base / "run"
        run.mkdir()
        progress = run / "progress_history_state.json"
        path = self.copy(task="phase:maker-unit/21", run_dir=str(run), phase_id="21")
        _ = progress.write_text(json.dumps({"phase": {"id": "21", "status": "finished"}}))
        with mock.patch.object(scratch, "remove", side_effect=OSError("injected removal stop")):
            with self.assertRaises(OSError):
                _ = scratch.end("phase:maker-unit/21")
        _ = progress.write_text(json.dumps({"phase": {"id": "22", "status": "active"}, "open_phases": []}))
        self.assertEqual(scratch.end("phase:maker-unit/21"), [path])

    def test_hand_over_keeps_parent_path_and_child_alternates_then_child_goes_first(self) -> None:
        parent = self.copy("parent")
        self.git(parent, "init", "-q")
        self.git(parent, "commit", "-qm", "base", "--allow-empty")
        child = parent.parent / "child"
        self.git(parent, "clone", "--shared", str(parent), str(child))
        scratch.register_copy(child, task="gate:child", owner_unit="child-unit", source_path=str(parent),
                              source_commit="a" * 40, copy_commit="b" * 40, target_mode="none",
                              seed_source="", borrows_from=str(parent / ".git"))
        alternate = child / ".git/objects/info/alternates"
        before = alternate.read_text()
        scratch.hand_over(parent, "next-unit", "gate:next-unit")
        registered = scratch.task_of(parent)
        self.assertIsInstance(registered, scratch.Registered)
        assert isinstance(registered, scratch.Registered)
        self.assertEqual(registered.task, "gate:next-unit")
        self.assertEqual(registered.fields["owner_unit"], "next-unit")
        self.assertEqual(alternate.read_text(), before)
        self.assertTrue(Path(before.strip()).is_dir())
        self.assertEqual(scratch.end("gate:next-unit"), [])
        self.assertEqual(scratch.end("gate:child"), [child])
        # Save the parent's own commits before allowing its removal.
        self.git(parent, "remote", "add", "origin", str(self.base / "origin"))
        self.git(parent, "update-ref", "refs/remotes/origin/main", "HEAD")
        self.assertEqual(scratch.end("gate:next-unit"), [parent])

    def test_shared_holders_and_crashed_holder_control_scratch_end(self) -> None:
        path = self.copy()
        with scratch.copy_lock(path), scratch.copy_lock(path):
            self.assertIsInstance(scratch.lifetime(path), scratch.Live)
            self.assertEqual(scratch.end("gate:copy"), [])
        program = ("import fcntl,sys; f=open(sys.argv[1],'a'); fcntl.flock(f,fcntl.LOCK_SH); "
                   "print('ready',flush=True); sys.stdin.read()")
        process: subprocess.Popen[str] = subprocess.Popen([sys.executable, "-c", program, str(path / ".claude-busy")],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            assert process.stdout is not None
            self.assertEqual(cast(str, process.stdout.readline()).strip(), "ready")
            self.assertEqual(scratch.end("gate:copy"), [])
        finally:
            process.kill()
            _ = process.communicate(timeout=10)
        self.assertEqual(scratch.end("gate:copy"), [path])

    def test_a_hold_cannot_bypass_a_copys_busy_lock_or_unknown_lifetime(self) -> None:
        path = self.copy()
        _ = scratch.hold(path, "gate:copy", "copy run")
        with scratch.copy_lock(path):
            self.assertEqual(scratch.end("gate:copy"), [])
        self.assertTrue(path.is_dir())
        _ = (path / scratch.RECORD).write_text("bad record")
        self.assertEqual(scratch.end("gate:copy"), [])
        self.assertTrue(path.is_dir())

    def test_missing_child_record_cannot_release_a_possible_borrowed_parent(self) -> None:
        parent = self.copy("parent", task="until:2000-01-01T0000Z")
        child = parent.parent / "child"
        child.mkdir()
        objects = child / ".git/objects/info"
        objects.mkdir(parents=True)
        _ = (objects / "alternates").write_text(str(parent / ".git/objects") + "\n")
        state = scratch.lifetime(parent)
        self.assertIsInstance(state, scratch.Unknown)
        self.assertEqual(scratch.end("until:2000-01-01T0000Z"), [])
        self.assertTrue(parent.is_dir())

    def test_outside_checkout_does_not_inspect_the_copies_root(self) -> None:
        copies = self.base / "unrelated-copies"
        resolve = Path.resolve

        def resolve_private(path: Path, strict: bool = False) -> Path:
            self.assertNotEqual(path, copies, "An outside checkout must not inspect the copies registry")
            return resolve(path, strict)

        with (mock.patch.dict(os.environ, {"SCRATCH_COPIES_ROOT": str(copies)}),
              mock.patch.object(Path, "resolve", resolve_private), scratch.copy_lock(self.base)):
            self.assertFalse((self.base / ".claude-busy").exists())

    def test_symlinked_copies_root_registers_and_locks_the_real_copy(self) -> None:
        path = self.copy()
        alias = self.base / "tmp-link"
        alias.symlink_to(self.base / "copies", target_is_directory=True)
        with mock.patch.dict(os.environ, {scratch.COPIES_ENV: str(alias)}):
            linked = alias / "show-maker" / "a"
            self.assertEqual(scratch.copy_folder(linked), path)
            scratch.register_copy(linked, task="gate:copy", owner_unit="maker-unit", source_path=str(self.base),
                                  source_commit="a", copy_commit="b", target_mode="none", seed_source="", borrows_from="")
            with scratch.copy_lock(linked), (path / scratch.BUSY).open("r") as probe:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_hand_over_clears_end_evidence_and_other_tasks_evidence_is_ignored(self) -> None:
        path = self.copy()
        with scratch.copy_lock(path):
            self.assertEqual(scratch.end("gate:copy"), [])
        scratch.hand_over(path, "next-unit", "gate:next")
        record = scratch.task_of(path)
        assert isinstance(record, scratch.Registered)
        self.assertNotIn("ended", record.fields)
        scratch.atomic_record(path / scratch.RECORD, {**record.fields, "ended": 123, "ended_task": "gate:copy"})
        self.assertIsInstance(scratch.lifetime(path), scratch.Live)

    def test_a_busy_tasks_end_evidence_survives_until_the_last_holder_exits(self) -> None:
        path = self.copy()
        with scratch.copy_lock(path):
            self.assertEqual(scratch.end("gate:copy"), [])
            self.assertIsInstance(scratch.lifetime(path), scratch.Live)
        self.assertIsInstance(scratch.lifetime(path), scratch.Ended)
        record = scratch.task_of(path)
        assert isinstance(record, scratch.Registered)
        self.assertEqual(record.fields["ended_task"], "gate:copy")

    def test_end_rechecks_task_after_a_hand_over_between_snapshot_and_lock(self) -> None:
        path = self.copy(task="gate:old")
        snapshot = scratch.folders()

        def transfer() -> list[scratch.Folder]:
            scratch.hand_over(path, "next-unit", "gate:next")
            return snapshot

        with mock.patch.object(scratch, "folders", side_effect=transfer):
            self.assertEqual(scratch.end("gate:old"), [])
        self.assertTrue(path.is_dir())
        self.assertIsInstance(scratch.lifetime(path), scratch.Live)

    def test_ancestor_hold_keeps_busy_open_phase_and_borrowed_copies(self) -> None:
        busy = self.copy("busy")
        run = self.base / "run"
        run.mkdir()
        _ = (run / "progress_history_state.json").write_text(json.dumps({"phase": {"id": "21", "status": "active"}}))
        phase = self.copy("phase", task="phase:maker-unit/21", run_dir=str(run), phase_id="21")
        parent = self.copy("parent", task="until:2000-01-01T0000Z")
        child = self.copy("child")
        record = scratch.task_of(child)
        assert isinstance(record, scratch.Registered)
        scratch.atomic_record(child / scratch.RECORD, {**record.fields, "borrows_from": str(parent / ".git")})
        for held in (busy.parent, self.base / "copies", self.base):
            with self.subTest(held=held):
                _ = scratch.hold(held, "gate:ancestor", "copy container")
                with scratch.copy_lock(busy):
                    self.assertEqual(scratch.end("gate:ancestor"), [])
                for path in (busy, phase, parent, child):
                    self.assertTrue(path.is_dir())
                self.assertTrue(scratch.release(held))

    def test_ancestor_hold_removes_ended_copies_and_only_empty_pairs(self) -> None:
        path = self.copy(task="until:2000-01-01T0000Z")
        _ = scratch.hold(path.parent, "gate:container", "ended copy")
        _ = scratch.end("gate:container")
        self.assertFalse(path.exists())
        self.assertFalse(path.parent.exists())
        self.assertTrue((self.base / "copies").is_dir())

    def test_end_resumes_after_the_record_was_unlinked_during_removal(self) -> None:
        path = self.copy()

        def stopped_removal(folder: Path) -> None:
            (folder / scratch.RECORD).unlink()
            raise OSError("stopped after record unlink")

        with mock.patch.object(scratch, "remove", side_effect=stopped_removal):
            with self.assertRaises(OSError):
                _ = scratch.end("gate:copy")
        self.assertEqual(scratch.end("gate:copy"), [path])
        self.assertFalse(path.exists())

    def test_all_recorder_terminal_states_end_a_phase(self) -> None:
        run = self.base / "run"
        run.mkdir()
        path = self.copy(task="phase:maker-unit/21", run_dir=str(run), phase_id="21")
        for status in ("completed", "stopped", "error"):
            with self.subTest(status=status):
                _ = (run / "progress_history_state.json").write_text(json.dumps(
                    {"phase": {"id": "21", "status": status, "finished_at": 123}}))
                self.assertIsInstance(scratch.lifetime(path), scratch.Ended)

    def test_landing_uses_fresh_source_refs_instead_of_stale_copy_refs(self) -> None:
        source = self.base / "source"
        source.mkdir()
        self.git(source, "init", "-q")
        self.git(source, "commit", "--allow-empty", "-qm", "base")
        self.git(source, "update-ref", "refs/remotes/origin/main", "HEAD")
        self.git(source, "commit", "--allow-empty", "-qm", "work")
        self.git(source, "update-ref", "refs/remotes/origin/work", "HEAD")
        path = self.copy(task="landed:maker-unit/work", land_target="origin/main")
        record = scratch.task_of(path)
        assert isinstance(record, scratch.Registered)
        scratch.atomic_record(path / scratch.RECORD, {**record.fields, "source_path": str(source)})
        self.assertIsInstance(scratch.lifetime(path), scratch.Live)
        self.git(source, "update-ref", "refs/remotes/origin/main", "HEAD")
        self.assertIsInstance(scratch.lifetime(path), scratch.Ended)
        os.rename(source, self.base / "gone")
        state = scratch.lifetime(path)
        self.assertIsInstance(state, scratch.Unknown)
        assert isinstance(state, scratch.Unknown)
        self.assertIn("source", state.reason)

    def test_hand_over_validates_the_complete_new_task_without_replacing_its_id(self) -> None:
        path = self.copy()
        for task in ("run:run-id", "gate:gate-id", "row:next-C9", "until:2099-01-01T0000Z", "phase:next-unit/22"):
            with self.subTest(task=task):
                scratch.hand_over(path, "next-unit", task)
                record = scratch.task_of(path)
                assert isinstance(record, scratch.Registered)
                self.assertEqual(record.task, task)
                self.assertEqual(record.fields["owner_unit"], "next-unit")
        before = (path / scratch.RECORD).read_bytes()
        with self.assertRaises(scratch.ScratchError):
            scratch.hand_over(path, "next-unit", "row:invalid")
        self.assertEqual((path / scratch.RECORD).read_bytes(), before)

    def test_lifetime_judgment_does_not_create_a_busy_mark(self) -> None:
        path = self.copy()
        (path / scratch.RECORD).unlink()
        self.assertIsInstance(scratch.lifetime(path), scratch.Unknown)
        self.assertFalse((path / scratch.BUSY).exists())

    def test_removal_claims_an_absent_busy_mark_before_a_late_holder_can_start(self) -> None:
        path = self.copy()
        self.assertFalse((path / scratch.BUSY).exists())
        with scratch.removal_lock(path) as idle:
            self.assertTrue(idle)
            with (path / scratch.BUSY).open("a") as late_holder:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(late_holder, fcntl.LOCK_SH | fcntl.LOCK_NB)
            scratch.remove_marked(path, scratch.remove)
        self.assertFalse(path.exists())

    def test_holder_cannot_restart_work_after_removal_unlinks_its_busy_mark(self) -> None:
        path = self.copy()

        def remove_after_late_holder_attempt(folder: Path) -> None:
            (folder / scratch.BUSY).unlink()
            with self.assertRaises(scratch.ScratchError):
                with scratch.copy_lock(folder):
                    self.fail("a holder entered a copy whose removal had started")
            scratch.remove(folder)

        with scratch.removal_lock(path) as idle:
            self.assertTrue(idle)
            scratch.remove_marked(path, remove_after_late_holder_attempt)
        self.assertFalse(path.exists())

    def test_record_writers_cannot_restore_the_task_read_before_a_hand_over(self) -> None:
        for operation in ("renew", "register"):
            with self.subTest(operation=operation):
                path = self.copy(operation, task="gate:old")
                read = Event()
                resume = Event()
                publish = scratch.atomic_record
                flock = fcntl.flock
                caller = current_thread()

                def paused_publish(destination: Path, fields: object) -> None:
                    if destination == path / scratch.RECORD and current_thread() is not caller:
                        read.set()
                        if not resume.wait(10):
                            raise AssertionError("record writer did not resume")
                    publish(destination, fields)

                def no_wait(descriptor: int, mode: int) -> None:
                    flock(descriptor, mode | fcntl.LOCK_NB)

                def write() -> None:
                    if operation == "renew":
                        scratch.renew(path, 123.0)
                    else:
                        self.register(path, "gate:old")

                with ThreadPoolExecutor(max_workers=1) as worker, mock.patch.object(scratch, "atomic_record", paused_publish):
                    writing = worker.submit(write)
                    transferred = False
                    try:
                        self.assertTrue(read.wait(10), "writer did not reach record publication")
                        # A writer that owns the record lock makes this transfer retry.
                        # Otherwise the transfer finishes while the writer has old fields.
                        with mock.patch.object(fcntl, "flock", no_wait):
                            try:
                                scratch.hand_over(path, "next-unit", "gate:next")
                                transferred = True
                            except BlockingIOError:
                                pass
                    finally:
                        resume.set()
                    writing.result(timeout=10)
                if not transferred:
                    scratch.hand_over(path, "next-unit", "gate:next")
                record = scratch.task_of(path)
                assert isinstance(record, scratch.Registered)
                self.assertEqual(record.task, "gate:next")
                self.assertEqual(record.fields["owner_unit"], "next-unit")
                self.assertEqual(scratch.end("gate:old"), [])
                self.assertTrue(path.is_dir())

    def test_waiting_record_writer_relocks_a_replacement_and_preserves_its_hand_over(self) -> None:
        path = self.copy(task="gate:old")
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, descriptor)
        directory = os.fstat(descriptor)
        old_identity = (directory.st_dev, directory.st_ino)
        flock = fcntl.flock
        flock(descriptor, fcntl.LOCK_EX)
        waiting = Event()
        publishing = Event()
        resume = Event()
        caller = current_thread()
        locked: list[tuple[int, int]] = []
        publish = scratch.atomic_record

        def observed_flock(lock: int, mode: int) -> None:
            if current_thread() is caller:
                flock(lock, mode | fcntl.LOCK_NB)
                return
            held = os.fstat(lock)
            identity = (held.st_dev, held.st_ino)
            if identity == old_identity:
                waiting.set()
            flock(lock, mode)
            locked.append(identity)

        def paused_publish(destination: Path, fields: object) -> None:
            if destination == path / scratch.RECORD and current_thread() is not caller:
                publishing.set()
                if not resume.wait(10):
                    raise AssertionError("waiting record writer did not resume")
            publish(destination, fields)

        with (ThreadPoolExecutor(max_workers=1) as worker,
              mock.patch.object(fcntl, "flock", observed_flock),
              mock.patch.object(scratch, "atomic_record", paused_publish)):
            writing = worker.submit(self.register, path, "gate:old")
            transferred = False
            try:
                self.assertTrue(waiting.wait(10), "writer did not open the old directory")
                scratch.remove(path)
                self.assertEqual(self.copy(task="gate:replacement"), path)
                replacement = path.lstat()
                replacement_identity = (replacement.st_dev, replacement.st_ino)
                self.assertNotEqual(replacement_identity, old_identity)
                flock(descriptor, fcntl.LOCK_UN)
                self.assertTrue(publishing.wait(10), "writer did not reach record publication")
                try:
                    scratch.hand_over(path, "next-unit", "gate:next")
                    transferred = True
                except BlockingIOError:
                    pass
            finally:
                flock(descriptor, fcntl.LOCK_UN)
                resume.set()
            writing.result(timeout=10)
        if not transferred:
            scratch.hand_over(path, "next-unit", "gate:next")
        self.assertIn(replacement_identity, locked, "writer must lock the replacement directory")
        record = scratch.task_of(path)
        assert isinstance(record, scratch.Registered)
        self.assertEqual(record.task, "gate:next")
        self.assertEqual(record.fields["owner_unit"], "next-unit")
        self.assertEqual(scratch.end("gate:old"), [])
        self.assertTrue(path.is_dir())

    def test_reused_copy_path_does_not_inherit_an_old_removal_certificate(self) -> None:
        path = self.copy(task="gate:old")
        # Keep the removed directory's inode allocated while reusing its name.
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, descriptor)
        remove = scratch.remove

        def stopped_after_tree_removal(folder: Path) -> None:
            remove(folder)
            raise OSError("stopped before removal certificate unlink")

        with mock.patch.object(scratch, "remove", stopped_after_tree_removal):
            with self.assertRaises(OSError):
                _ = scratch.end("gate:old")
        self.assertFalse(path.exists())
        self.assertTrue(scratch.removal_mark(path).is_file())
        reused = self.copy(task="gate:new")
        self.assertEqual(reused, path)
        self.assertFalse(scratch.removal_mark(path).exists())
        _ = (path / "ordinary").write_text("new owner's work")
        self.assertIsInstance(scratch.lifetime(path), scratch.Live)
        scratch.hand_over(path, "next-unit", "gate:next")
        self.assertEqual(scratch.end("gate:old"), [])
        self.assertTrue((path / "ordinary").is_file())

    def test_hand_over_replaces_phase_context_and_clears_it_for_an_external_task(self) -> None:
        old_run = self.base / "old-run"
        new_run = self.base / "new-run"
        old_run.mkdir()
        new_run.mkdir()
        for run, phase, status in ((old_run, "21", "completed"), (new_run, "22", "active")):
            _ = (run / "progress_history_state.json").write_text(json.dumps(
                {"phase": {"id": phase, "status": status}}))
        path = self.copy(task="phase:maker-unit/21", run_dir=str(old_run), phase_id="21", land_target="origin/old")
        self.assertIsInstance(scratch.lifetime(path), scratch.Ended)
        scratch.hand_over(path, "next-unit", "phase:next-unit/22")
        self.assertIsInstance(scratch.lifetime(path), scratch.Unknown)
        scratch.hand_over(path, "next-unit", "phase:next-unit/22", run_dir=str(new_run))
        record = scratch.task_of(path)
        assert isinstance(record, scratch.Registered)
        self.assertEqual(record.fields["run_dir"], str(new_run))
        self.assertEqual(record.fields["phase_id"], "22")
        self.assertIsInstance(scratch.lifetime(path), scratch.Live)
        self.assertEqual(scratch.end("phase:maker-unit/21"), [])
        scratch.hand_over(path, "next-unit", "gate:next")
        record = scratch.task_of(path)
        assert isinstance(record, scratch.Registered)
        for field in ("run_dir", "phase_id", "land_target", "ended", "ended_task"):
            self.assertNotIn(field, record.fields)

    def test_external_tasks_say_they_wait_for_an_end_instead_of_a_landing(self) -> None:
        for task in (RUN, ROW, "gate:copy"):
            with self.subTest(task=task):
                state = scratch.task_end(task)
                assert isinstance(state, scratch.Live)
                self.assertIn("has not ended", state.why)
                self.assertNotIn("landed", state.why)

    def test_no_task_lasts_a_day_and_until_ends_at_its_time(self) -> None:
        path = scratch.new(now=1_000_000.0)
        record = scratch.task_of(path)
        self.assertIsInstance(record, scratch.Registered)
        assert isinstance(record, scratch.Registered)
        task = record.task
        deadline = scratch.task_end(task, 1_000_000.0 + scratch.DEFAULT_LIFE_SECONDS)
        self.assertIsInstance(deadline, scratch.Ended)
        assert isinstance(deadline, scratch.Ended)
        self.assertEqual(deadline.at, 1_000_000.0 + scratch.DEFAULT_LIFE_SECONDS - 40)
        self.assertFalse(scratch.ended(task, 1_000_000.0))
        self.assertTrue(scratch.ended(task, 1_000_000.0 + scratch.DEFAULT_LIFE_SECONDS))
        self.assertFalse(scratch.ended(RUN, 9e12))
        self.assertTrue(scratch.parsed_until("2026-10-28 09:30").startswith("until:2026-10-28T"))
        with self.assertRaises(scratch.ScratchError):
            _ = scratch.parsed_until("tomorrow")

    def test_a_held_folder_goes_with_its_task_unless_a_repo_in_it_has_unsaved_work(self) -> None:
        saved, unsaved = self.base / "saved", self.base / "unsaved" / "repo"
        for repo in (saved, unsaved):
            repo.mkdir(parents=True)
            self.git(repo, "init", "-q")
        _ = (unsaved / "a").write_text("x")
        self.git(unsaved, "add", "a")
        self.git(unsaved, "commit", "-qm", "a")
        task = "row:natedev-C9"
        for folder in (saved, unsaved.parent):
            _ = scratch.hold(folder, task, "change review C9")
        self.assertEqual(scratch.holds()[str(saved)]["maker"], "maker-session")
        self.assertEqual(scratch.unsaved_work(unsaved.parent), f"{unsaved}: it has commits and no remote")
        self.assertEqual(scratch.end(task), [saved])
        self.assertFalse(saved.exists())
        self.assertTrue(unsaved.is_dir())
        self.assertGreater(scratch.holds()[str(unsaved.parent)]["ended"], 0)
        self.assertTrue(scratch.release(unsaved.parent))
        self.assertEqual(scratch.holds(), {})

    def test_new_makes_a_recorded_folder_for_its_task(self) -> None:
        path = scratch.new(RUN, "gate")
        self.assertEqual(path.parent, self.base / "scratch")
        self.assertRegex(path.name, r"^run-2a858b68-0f9a-4af2-b3cf-a776e6b180cb-gate-[a-z0-9]{6}$")
        record = cast(dict[str, object], json.loads((path / scratch.RECORD).read_text()))
        self.assertEqual((record["task"], record["maker"]), (RUN, "maker-session"))

    def test_a_visible_repro_goes_where_cargo_port_lists_it(self) -> None:
        self.assertEqual(scratch.new(ROW, "demo", visible=True), self.base / "repro" / "cargo-liner-C54-demo")
        with self.assertRaises(scratch.ScratchError):
            _ = scratch.new(RUN, "x", visible=True)

    def test_a_malformed_task_or_name_is_refused(self) -> None:
        for task in ("run", "session:abc", "row:cargo-liner", "row:a/C1", "phase:"):
            with self.assertRaises(scratch.ScratchError, msg=task):
                _ = scratch.new(task)
        with self.assertRaises(scratch.ScratchError):
            _ = scratch.new(RUN, "../out")

    def test_end_removes_only_the_tasks_folders(self) -> None:
        mine = [scratch.new(ROW, "demo", visible=True), scratch.new(ROW, "copy")]
        other = scratch.new("phase:builds-unit/7")
        loose = self.base / "scratch" / "loose"
        loose.mkdir()
        self.assertEqual(sorted(scratch.end(ROW)), sorted(mine))
        for path in mine:
            self.assertFalse(path.exists())
        self.assertTrue(other.is_dir())
        self.assertTrue(loose.is_dir())

    def test_stopped_removal_keeps_its_task_record_until_the_rerun_finishes(self) -> None:
        path = scratch.new(ROW, "stopped", visible=True)
        first, second = path / "a", path / "b"
        _ = first.write_text("first child")
        _ = second.write_text("second child")
        record = (path / scratch.RECORD).read_bytes()
        other = scratch.new(RUN, "kept")
        real_unlink = os.unlink
        real_scandir = os.scandir

        @contextmanager
        def record_first(directory: int | str | os.PathLike[str]) -> Generator[Iterator[os.DirEntry[str]]]:
            with real_scandir(directory) as entries:
                yield iter(sorted(entries, key=lambda entry: (entry.name != scratch.RECORD, entry.name)))

        def stop_after_child(child: str | os.PathLike[str], *, dir_fd: int | None = None) -> None:
            real_unlink(child, dir_fd=dir_fd)
            if Path(child).name == first.name:
                raise KeyboardInterrupt

        with (mock.patch.object(os, "scandir", side_effect=record_first),
              mock.patch.object(os, "unlink", side_effect=stop_after_child),
              self.assertRaises(KeyboardInterrupt)):
            _ = scratch.end(ROW)
        self.assertFalse(first.exists())
        self.assertTrue(second.is_file())
        self.assertEqual((path / scratch.RECORD).read_bytes(), record)
        self.assertIn(path, [folder.path for folder in scratch.folders() if folder.task == ROW])
        self.assertEqual(scratch.end(ROW), [path])
        self.assertFalse(path.exists())
        self.assertTrue(other.is_dir())

    def test_removal_rerun_finds_the_task_after_its_record_or_folder_is_deleted(self) -> None:
        for index, boundary in enumerate(("record removed", "before rmdir", "after rmdir")):
            with self.subTest(boundary=boundary):
                path = scratch.new(ROW, f"final-stop-{index}", visible=True)
                other = scratch.new(RUN, "kept")
                real_unlink = os.unlink
                real_rmdir = Path.rmdir

                def stop_after_record(child: str | os.PathLike[str], *, dir_fd: int | None = None) -> None:
                    real_unlink(child, dir_fd=dir_fd)
                    if boundary == "record removed" and Path(child).name == scratch.RECORD:
                        raise KeyboardInterrupt

                def stop_at_folder(folder: Path) -> None:
                    if folder == path and boundary == "before rmdir":
                        raise KeyboardInterrupt
                    real_rmdir(folder)
                    if folder == path and boundary == "after rmdir":
                        raise KeyboardInterrupt

                with (mock.patch.object(os, "unlink", side_effect=stop_after_record),
                      mock.patch.object(Path, "rmdir", autospec=True, side_effect=stop_at_folder),
                      self.assertRaises(KeyboardInterrupt)):
                    _ = scratch.end(ROW)
                self.assertFalse((path / scratch.RECORD).exists())
                self.assertEqual(path.exists(), boundary != "after rmdir")
                self.assertIn(path, [folder.path for folder in scratch.folders() if folder.task == ROW])
                self.assertEqual(scratch.end(ROW), [path])
                self.assertFalse(path.exists())
                self.assertNotIn(path, [folder.path for folder in scratch.folders()])
                self.assertTrue(other.is_dir())
                self.assertEqual(list(path.parent.iterdir()), [])

    def test_end_keep_predicate_preserves_folders_and_holds_until_the_command_finishes(self) -> None:
        kept = scratch.new(RUN, "live")
        removed = scratch.new(RUN, "old")
        held = self.base / "held-command"
        held.mkdir()
        _ = scratch.hold(held, RUN, "command cwd")
        before = scratch.holds()[str(held)].copy()
        other = scratch.new(ROW, "other")
        self.assertEqual(scratch.end(RUN, keep=lambda path: path in (kept, held)), [removed])
        self.assertTrue(kept.is_dir())
        self.assertTrue(held.is_dir())
        self.assertTrue(other.is_dir())
        self.assertEqual(scratch.holds()[str(held)], before)
        self.assertCountEqual(scratch.end(RUN), [kept, held])
        self.assertEqual(scratch.holds(), {})
        with self.assertRaises(scratch.ScratchError):
            _ = scratch.end("invalid-task", keep=lambda path: True)

    def test_adopt_moves_a_loose_folder_in(self) -> None:
        loose = self.base / "rust" / "c54demo"
        (loose / "src").mkdir(parents=True)
        path = scratch.adopt(loose, ROW, "demo", visible=True)
        self.assertFalse(loose.exists())
        self.assertTrue((path / "src").is_dir())
        record = scratch.task_of(path)
        self.assertIsInstance(record, scratch.Registered)
        assert isinstance(record, scratch.Registered)
        self.assertEqual(record.task, ROW)

    def test_renew_moves_the_records_time(self) -> None:
        path = scratch.new(RUN)
        scratch.renew(path, now=1_000.0)
        self.assertEqual((path / scratch.RECORD).stat().st_mtime, 1_000.0)
        with self.assertRaises(scratch.ScratchError):
            scratch.renew(self.base)

    def test_end_removes_a_worktree_through_its_source_repo(self) -> None:
        source = self.base / "source"
        environment = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}

        def git(*arguments: str) -> None:
            _ = subprocess.run(["git", "-C", str(source), *arguments], check=True, capture_output=True,
                               env=environment)

        source.mkdir()
        git("init", "-q")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "c")
        path = scratch.target(RUN, "wt", False)
        git("worktree", "add", "-q", str(path))
        scratch.write_record(path, RUN, 0.0)
        _ = scratch.end(RUN)
        self.assertFalse(path.exists())
        listed = subprocess.run(["git", "-C", str(source), "worktree", "list"], check=True, capture_output=True,
                                text=True, env=environment).stdout
        self.assertNotIn(str(path), listed)


if __name__ == "__main__":
    _ = unittest.main()
