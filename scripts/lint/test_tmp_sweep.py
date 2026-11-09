#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Tests for tmp_sweep.py on fabricated /tmp roots and a fabricated /proc. Times are two days ahead
of the clock, so every file a test writes is old, ctime included, unless the test moves it."""

from __future__ import annotations

from scripts.shutdown import remote

import io
import fcntl
import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Generator, Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from functools import cached_property
from typing import TextIO, cast, override
from unittest import mock

from scripts.lint import tmp_sweep
from scripts.copies.copies_test_support import copies_environment
from scripts.lib import build_folders, copy_record, scratch, test_hang_guard
from scripts.message import sessions
from scripts.production import staff
from scripts.production.staff_test_support import StaffTestCase, fixture_member, fixture_runtime_id, fixture_worker, isolated_staff_file

from scripts.lib import conf, unit_runs
from scripts.herdr import worker_panes
from scripts.herdr.test_worker_panes import PaneWorld
from scripts.lib.atomic_file import write_text_atomic
from scripts.herdr import herdr
from scripts.lib.cargo_output_test_support import fake_cargo_output_roots
from scripts.lint.memory_test_support import config_environment, write_config_fixture

DAY = 86_400
LIVE_SESSION = "11111111-2222-3333-4444-555555555555"
ENDED_SESSION = "66666666-7777-8888-9999-000000000000"
WORKER_SESSION = fixture_runtime_id("tmp-sweep-worker")
PROJECT = "-home-natepiano-rust-hana"


def setUpModule() -> None:
    _ = unittest.enterModuleContext(fake_cargo_output_roots())


class ImportTests(unittest.TestCase):
    def test_tmp_sweep_import_omits_unused_modules(self) -> None:
        omitted = ("scripts.production.session_staff", "scripts.production.staff")
        with tempfile.TemporaryDirectory() as directory:
            environment = {**os.environ, "BUILDLOG_DIR": directory}
            result = subprocess.run(
                [sys.executable, "-c", "import sys; from scripts.lint import tmp_sweep; "
                 + f"assert not set({omitted!r}).intersection(sys.modules)"],
                cwd=Path(__file__).resolve().parents[2], env=environment,
                capture_output=True, text=True, timeout=test_hang_guard.TEST_HANG_GUARD_SECS, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


def write(path: Path, text: str = "x" * 4096) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(text)
    return path


def remove_marker(path: Path) -> None:
    if path.is_dir():
        path.rmdir()
    else:
        path.unlink()


def fake_process(proc: Path, pid: int, *, cwd: str = "/", fds: tuple[str, ...] = (), maps: str = "") -> None:
    """A /proc/<pid> entry as the scan reads it."""
    entry = proc / str(pid)
    (entry / "fd").mkdir(parents=True)
    (entry / "cwd").symlink_to(cwd)
    for number, target in enumerate(fds):
        (entry / "fd" / str(number)).symlink_to(target)
    _ = (entry / "maps").write_text(maps)


BOOT = 1000


def fake_start(proc: Path, pid: int, start: float, *arguments: str) -> None:
    """A /proc/<pid> entry that started at start with arguments, as verify_running reads it."""
    _ = write(proc / "stat", f"cpu 0\nbtime {BOOT}\n")
    ticks = round((start - BOOT) * os.sysconf("SC_CLK_TCK"))
    _ = write(proc / str(pid) / "stat", f"{pid} (a b) S " + "0 " * 18 + f"{ticks} 0 0\n")
    _ = write(proc / str(pid) / "cmdline", "\0".join(arguments) + "\0")


class TmpSweepCase(unittest.TestCase):
    @cached_property
    def pane_world(self) -> PaneWorld:
        return PaneWorld()

    tmp: Path = Path()
    unit: Path = Path()
    delegate: Path = Path()
    session_root: Path = Path()
    project: Path = Path()
    proc: Path = Path()
    state: Path = Path()
    now: float = 0.0
    live: frozenset[str] | None = None

    @override
    def setUp(self) -> None:
        base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(mock.patch.dict(os.environ, {"HOME": str(base)}))
        _ = (base / "staff.json").write_text('{"schema": 2, "rows": []}')
        self.enterContext(isolated_staff_file(base / "staff.json", {"CODEX_HOME": str(base / "codex")}))
        self.tmp = base / "claude"
        self.unit = self.tmp / "unit"
        self.delegate = self.tmp / "delegate"
        self.session_root = base / "claude-1000"
        self.project = self.session_root / PROJECT
        self.proc = base / "proc"
        self.state = base / "state"
        for folder in (self.unit, self.delegate, self.project, self.proc):
            folder.mkdir(parents=True)
        registry = base / "sessions"
        registry.mkdir()
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "NOTIFIER_SESSIONS_DIR": str(registry), "SHUTDOWN_STATE_DIR": str(base / "shutdown"),
            "BUILDLOG_DIR": str(base / "buildlog"),
            **config_environment(base / "config"),
        })))
        self.addCleanup(self.pane_world.process_directory.cleanup)
        pane_patches = [
            cast(object, self.enterContext(mock.patch.object(worker_panes, "_client", return_value=self.pane_world))),
            cast(object, self.enterContext(mock.patch.object(herdr, "native_process", side_effect=self.pane_world.native_process))),
            cast(object, self.enterContext(mock.patch.object(herdr, "_startup_processes", side_effect=self.pane_world.descendants))),
        ]
        self.addCleanup(pane_patches.clear)
        self.now = time.time() + 2 * DAY
        _ = self.enterContext(mock.patch.object(tmp_sweep, "state_dir", return_value=self.state))
        # A private default root exposes callers that fail to set their session roots.
        default_session = write(base / "default-session" / ENDED_SESSION / "file").parents[1]
        _ = self.enterContext(mock.patch.object(tmp_sweep, "SESSION_TEMP_ROOT", str(default_session)))
        self.live = frozenset({LIVE_SESSION})
        _ = self.enterContext(mock.patch.object(tmp_sweep, "running_session_ids", side_effect=lambda: self.live))

    def roots(self) -> tmp_sweep.Roots:
        return tmp_sweep.default_roots(str(self.tmp), (str(self.delegate), str(self.unit)), (str(self.session_root),), (),
                                       (str(self.tmp / "scratch"),), (), (str(self.tmp / "session"),))

    def run_owner(self, run: Path, session: str) -> staff.StaffRow:
        owner = fixture_member("showrunner", name="sweep")
        unit = fixture_member("unit", name=run.name, parent=owner.id, runtime_id=session)
        _ = write(run / "progress_history_state.json", json.dumps({unit_runs.OWNER_FIELD: unit.id}))
        return unit

    def unreadable_sessions(self) -> None:
        self.live = None

    def recent(self, path: Path) -> None:
        os.utime(path, (self.now - 3600, self.now - 3600), follow_symlinks=False)

    def sweep(self, dry_run: bool = False) -> tmp_sweep.Sweep:
        return tmp_sweep.sweep(dry_run, self.roots(), self.now, str(self.proc))

    def run_once(self, dry_run: bool = False) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            status = tmp_sweep.run(dry_run, self.now, self.roots(), str(self.proc))
        return status, output.getvalue()

    def command(self, *arguments: str) -> tuple[int, str]:
        output = io.StringIO()
        with (redirect_stdout(output), redirect_stderr(output),
              mock.patch.object(tmp_sweep, "default_roots", return_value=self.roots()),
              mock.patch.object(tmp_sweep, "PROC_DIR", str(self.proc)),
              mock.patch("time.time", return_value=self.now)):
            status = tmp_sweep.main(list(arguments))
        return status, output.getvalue()

    def log(self) -> list[dict[str, object]]:
        lines = (self.state / tmp_sweep.LOG_NAME).read_text().splitlines()
        return [cast(dict[str, object], json.loads(line)) for line in lines]


class CopyTests(TmpSweepCase):
    now: float = 0.0

    def test_unregistered_checkout_removal_uses_the_cargo_output_guard(self) -> None:
        checkout = self.tmp / "old-checkout"
        checkout.mkdir(parents=True)
        events: list[str] = []
        def clean(path: Path, saved: tuple[scratch.SavedCargoOutput, ...]) -> None:
            self.assertEqual(path, checkout)
            self.assertEqual(saved, ())
            self.assertTrue(path.exists())
            events.append("guard")
        entry = tmp_sweep.RemovableEntry(str(checkout), str(self.tmp), self.now, 0)
        result = tmp_sweep.Sweep()
        with mock.patch.object(scratch, "clean_cargo_outputs", side_effect=clean):
            self.assertTrue(tmp_sweep.remove_idle_entry(entry, result, False))
        self.assertEqual(events, ["guard"])
        self.assertFalse(checkout.exists())

    def test_private_root_never_selects_the_live_copies_authority(self) -> None:
        authority_root = self.tmp.parent / "authority copies"
        with mock.patch.object(copy_record, "copies_root", return_value=authority_root) as authority:
            roots = tmp_sweep.default_roots(str(self.tmp), (), (), (), (), (), ())
        self.assertEqual(roots.copies, (str(self.tmp / "copies"),))
        self.assertNotIn(str(authority_root), roots.all())
        authority.assert_not_called()

    def test_default_root_reads_the_current_copies_authority(self) -> None:
        for name in ("first copies", "second copies"):
            authority_root = self.tmp.parent / name
            with mock.patch.object(copy_record, "copies_root", return_value=authority_root) as authority:
                roots = tmp_sweep.default_roots(run_roots=(), session_roots=(), temp_roots=(),
                                               scratch_roots=(), wide_roots=(), session_temp_roots=())
            self.assertEqual(roots.copies, (str(authority_root),))
            authority.assert_called_once_with()

    def copy(self, name: str, **fields: object) -> Path:
        path = self.tmp / "copies" / "show-maker" / name
        path.mkdir(parents=True)
        record: dict[str, object] = {"task": "until:2000-01-01T0000Z", "owner_unit": "maker-unit"}
        record.update(fields)
        _ = write(path / copy_record.RECORD, json.dumps(record))
        self.enterContext(mock.patch.dict(os.environ, {"SCRATCH_COPIES_ROOT": str(self.tmp / "copies")}))
        self.now = self.now + 8 * DAY
        return path

    def test_copy_entries_are_individual_and_empty_pair_goes_after_last_copy(self) -> None:
        old = self.copy("old")
        live = self.copy("live", task="gate:live")
        result = self.sweep()
        self.assertFalse(old.exists())
        self.assertTrue(live.exists())
        self.assertTrue(live.parent.exists())
        self.assertIn(str(self.tmp / "copies"), result.kept[tmp_sweep.ROOT])
        _ = write(live / copy_record.RECORD, json.dumps({"task": "until:2000-01-01T0000Z"}))
        self.now = self.now + 8 * DAY
        _ = self.sweep()
        self.assertFalse(live.parent.exists())
        self.assertTrue((self.tmp / "copies").is_dir())

    def test_dry_run_does_not_add_a_busy_mark_to_an_unregistered_copy(self) -> None:
        path = self.copy("unknown")
        (path / copy_record.RECORD).unlink()
        _ = self.sweep(True)
        self.assertFalse((path / scratch.BUSY).exists())

    def test_sweep_resumes_recordless_removal_and_does_not_keep_a_possible_parent(self) -> None:
        parent = self.copy("parent")
        child = self.copy("child", borrows_from=str(parent / ".git"))

        def stopped_removal(folder: str) -> None:
            (Path(folder) / copy_record.RECORD).unlink()
            raise OSError("stopped after record unlink")

        with mock.patch.object(tmp_sweep, "delete_tree", side_effect=stopped_removal):
            result = self.sweep()
        self.assertTrue(result.failed)
        self.assertFalse((child / copy_record.RECORD).exists())
        _ = self.sweep()
        self.assertFalse(child.exists())
        self.assertFalse(parent.exists())

    def test_failed_resumed_removal_keeps_its_borrowed_parent(self) -> None:
        parent = self.copy("parent")
        child = self.copy("child", borrows_from=str(parent / ".git"))

        def stopped_removal(folder: str) -> None:
            (Path(folder) / copy_record.RECORD).unlink()
            raise OSError("stopped after record unlink")

        with mock.patch.object(tmp_sweep, "delete_tree", side_effect=stopped_removal):
            self.assertTrue(self.sweep().failed)
        with mock.patch.object(tmp_sweep, "delete_tree", side_effect=OSError("still stopped")) as remove:
            result = self.sweep()
        remove.assert_called_once_with(str(child))
        self.assertTrue(result.failed)
        self.assertTrue(child.exists())
        self.assertTrue(parent.exists())
        self.assertIn(str(parent), result.kept[f"borrowed: {child} still uses its git objects"])

    def test_reused_path_with_unsaved_work_does_not_resume_the_old_copys_removal(self) -> None:
        path = self.copy("reused")
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, descriptor)
        delete_tree = tmp_sweep.delete_tree

        def stopped_after_tree_removal(folder: str) -> None:
            delete_tree(folder)
            raise KeyboardInterrupt("stopped before removal certificate unlink")

        with mock.patch.object(tmp_sweep, "delete_tree", stopped_after_tree_removal):
            with self.assertRaises(KeyboardInterrupt):
                _ = self.sweep()
        self.assertFalse(path.exists())
        self.assertTrue(scratch.removal_mark(path).is_file())
        _ = self.copy("reused")
        _ = subprocess.run(["git", "-C", str(path), "init", "-q"], check=True, capture_output=True)
        scratch.exclude_copy_metadata(path)
        _ = write(path / "ordinary", "new owner's unsaved work")
        self.now = self.now + 8 * DAY
        result = self.sweep()
        self.assertTrue((path / "ordinary").is_file())
        self.assertIn(str(path), result.kept[tmp_sweep.UNSAVED_WORK])
        self.assertFalse(scratch.removal_mark(path).exists())

    def test_busy_copy_keeps_even_without_a_record_and_rechecks_before_delete(self) -> None:
        path = self.copy("busy")
        (path / copy_record.RECORD).unlink()
        with (path / ".claude-busy").open("w") as holder:
            fcntl.flock(holder, fcntl.LOCK_SH)
            result = self.sweep()
        self.assertTrue(path.is_dir())
        self.assertTrue(any("busy" in reason and str(path) in paths for reason, paths in result.kept.items()))
        _ = write(path / copy_record.RECORD, json.dumps({"task": "until:2000-01-01T0000Z"}))
        original = tmp_sweep.remove_entry
        with (path / ".claude-busy").open("a") as holder:
            def start_before_remove(entry: tmp_sweep.RemovableEntry, result: tmp_sweep.Sweep, dry_run: bool,
                                    *, scratch_settings: conf.ConfigRead) -> bool:
                fcntl.flock(holder, fcntl.LOCK_SH)
                return original(entry, result, dry_run, scratch_settings=scratch_settings)
            with mock.patch.object(tmp_sweep, "remove_entry", side_effect=start_before_remove):
                _ = self.sweep()
        self.assertTrue(path.is_dir())

    def test_open_phase_and_unknown_records_survive_at_any_age(self) -> None:
        run = self.tmp.parent / "run"
        progress = write(run / "progress_history_state.json", json.dumps({"phase": {"id": "21", "status": "active"}}))
        live = self.copy("phase", task="phase:maker-unit/21", run_dir=str(run), phase_id="21")
        unknown = self.copy("unknown")
        _ = (unknown / copy_record.RECORD).write_text("bad json")
        self.now += 30 * DAY
        result = self.sweep(True)
        self.assertTrue(live.is_dir())
        self.assertTrue(unknown.is_dir())
        self.assertNotIn(str(live), [entry["path"] for entry in result.deleted])
        self.assertTrue(any("record" in reason and str(unknown) in paths for reason, paths in result.kept.items()))
        _ = progress.write_text("bad progress")
        result = self.sweep()
        self.assertTrue(live.is_dir())
        self.assertTrue(any("progress" in reason and str(live) in paths for reason, paths in result.kept.items()))

    def test_landed_copy_needs_seven_idle_days_and_readable_target(self) -> None:
        path = self.copy("landed", task="landed:maker-unit/work", land_target="origin/main",
                         source_path=str(self.tmp / "copies" / "show-maker" / "landed"))
        environment = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
        for arguments in (("init", "-q"),
                          ("-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-qm", "base"),
                          ("remote", "add", "origin", "https://example.invalid/repo.git"),
                          ("update-ref", "refs/remotes/origin/work", "HEAD"),
                          ("update-ref", "refs/remotes/origin/main", "HEAD")):
            _ = subprocess.run(["git", "-C", str(path), *arguments], check=True, capture_output=True, env=environment)
        scratch.exclude_copy_metadata(path)
        self.now = time.time() + DAY
        _ = self.sweep()
        self.assertTrue(path.is_dir())
        _ = subprocess.run(["git", "-C", str(path), "update-ref", "-d", "refs/remotes/origin/main"],
                           check=True, capture_output=True, env=environment)
        self.now += 30 * DAY
        result = self.sweep()
        self.assertTrue(path.is_dir())
        self.assertTrue(any("land target" in reason and str(path) in paths for reason, paths in result.kept.items()))
        _ = subprocess.run(["git", "-C", str(path), "update-ref", "refs/remotes/origin/main", "HEAD"],
                           check=True, capture_output=True, env=environment)
        _ = self.sweep()
        self.assertFalse(path.exists())

    def test_new_unsaved_work_between_scan_and_remove_keeps_copy(self) -> None:
        path = self.copy("edited")
        _ = subprocess.run(["git", "-C", str(path), "init", "-q"], check=True, capture_output=True)
        scratch.exclude_copy_metadata(path)
        original = tmp_sweep.remove_entry

        def edit_before_remove(entry: tmp_sweep.RemovableEntry, result: tmp_sweep.Sweep, dry_run: bool,
                              *, scratch_settings: conf.ConfigRead) -> bool:
            if entry.path == str(path):
                _ = write(path / "ordinary", "unsaved work")
            return original(entry, result, dry_run, scratch_settings=scratch_settings)

        with mock.patch.object(tmp_sweep, "remove_entry", side_effect=edit_before_remove):
            result = self.sweep()
        self.assertTrue((path / "ordinary").is_file())
        self.assertIn(str(path), result.kept[tmp_sweep.UNSAVED_WORK])

    def test_borrowed_parent_stays_until_its_last_child_is_gone(self) -> None:
        parent = self.copy("parent")
        child = self.copy("child", task="gate:child", borrows_from=str(parent / ".git"))
        result = self.sweep()
        self.assertTrue(parent.is_dir())
        self.assertTrue(child.is_dir())
        self.assertTrue(any("borrow" in reason and str(parent) in paths for reason, paths in result.kept.items()))
        _ = (child / copy_record.RECORD).write_text(json.dumps({"task": "until:2000-01-01T0000Z",
                                                           "borrows_from": str(parent / ".git")}))
        _ = self.sweep()
        self.assertTrue(parent.is_dir())
        self.assertFalse(child.exists())
        _ = self.sweep()
        self.assertFalse(parent.exists())
        self.assertFalse(child.exists())


class IdleTests(TmpSweepCase):
    def test_unrecorded_folder_uses_the_shared_removal_age(self) -> None:
        _ = write_config_fixture(Path(os.environ["CONF_DIR"]) / "lint.conf",
                                 "folder_remove_after_seconds=86400\n")
        output = write(self.tmp / "shots" / "image.png")

        result = self.sweep()

        self.assertFalse(output.parent.exists())
        self.assertEqual([entry["path"] for entry in result.deleted], [str(output.parent)])

    def test_a_recent_entry_stops_the_scan_before_the_rest_is_loaded(self) -> None:
        folder = write(self.tmp / "large" / "recent").parent
        self.recent(folder / "recent")
        with os.scandir(folder) as entries:
            first = next(entries)
        closed: list[bool] = []

        def remaining_entries() -> Iterator[os.DirEntry[str]]:
            yield first
            self.fail("The scan loaded more entries after it could decide to keep this folder")

        @contextmanager
        def streamed_entries(_path: str) -> Generator[Iterator[os.DirEntry[str]]]:
            try:
                yield remaining_entries()
            finally:
                closed.append(True)

        with mock.patch.object(os, "scandir", side_effect=streamed_entries):
            result = tmp_sweep.last_change(str(folder), tmp_sweep.Limit(self.now - DAY, tmp_sweep.RECENT))

        self.assertEqual(result, tmp_sweep.RECENT)
        self.assertEqual(closed, [True])
        self.assertTrue((folder / "recent").is_file())

    def test_streamed_scan_counts_each_hard_linked_file_once(self) -> None:
        folder = self.tmp / "linked"
        source = write(folder / "first" / "output")
        second = folder / "second"
        second.mkdir()
        os.link(source, second / "copy")
        independent = write(second / "independent")

        result = tmp_sweep.last_change(str(folder), tmp_sweep.Limit(self.now - DAY, tmp_sweep.RECENT))

        self.assertIsInstance(result, tuple)
        assert isinstance(result, tuple)
        self.assertEqual(result[1], (source.stat().st_blocks + independent.stat().st_blocks) * 512)

    def test_an_idle_entry_goes_and_a_recent_one_stays(self) -> None:
        self.now: float = time.time() + 8 * DAY
        idle = write(self.tmp / "shots" / "1.png")
        loose = write(self.tmp / "notes.txt")
        busy = write(self.tmp / "trace" / "deep" / "run.json")
        self.recent(busy)
        result = self.sweep()
        self.assertFalse(idle.parent.exists())
        self.assertFalse(loose.exists())
        self.assertTrue(busy.is_file())
        self.assertEqual(result.kept[tmp_sweep.RECENT], [str(self.tmp / "trace")])
        self.assertEqual(sorted(entry["path"] for entry in result.deleted),
                         [str(self.tmp / "notes.txt"), str(self.tmp / "shots")])

    def test_a_folders_own_time_does_not_count_unless_it_is_empty(self) -> None:
        self.now = time.time() + 8 * DAY
        touched = write(self.tmp / "touched" / "old.txt").parent
        self.recent(touched)
        empty = self.tmp / "empty"
        empty.mkdir()
        self.recent(empty)
        _ = self.sweep()
        self.assertFalse(touched.exists())
        self.assertTrue(empty.is_dir())

    def test_a_link_out_of_an_entry_is_not_followed(self) -> None:
        self.now = time.time() + 8 * DAY
        outside = write(self.tmp.parent / "outside" / "kept")
        (write(self.tmp / "links" / "file").parent / "out").symlink_to(outside.parent)
        (self.tmp / "loose-link").symlink_to(outside)
        _ = self.sweep()
        self.assertFalse((self.tmp / "links").exists())
        self.assertFalse((self.tmp / "loose-link").is_symlink())
        self.assertTrue(outside.is_file())

    def test_a_read_only_folder_is_deleted(self) -> None:
        self.now = time.time() + 8 * DAY
        locked = write(self.tmp / "store" / "nix" / "file").parent
        locked.chmod(0o500)
        self.addCleanup(lambda: locked.chmod(0o700) if locked.exists() else None)
        result = self.sweep()
        self.assertEqual(result.failed, [])
        self.assertFalse((self.tmp / "store").exists())

    def test_an_entry_that_cannot_be_read_stays(self) -> None:
        hidden = write(self.tmp / "hidden" / "inner" / "file").parent
        hidden.chmod(0o000)
        self.addCleanup(hidden.chmod, 0o700)
        result = self.sweep()
        self.assertTrue((self.tmp / "hidden").is_dir())
        self.assertEqual(result.kept[tmp_sweep.UNREADABLE], [str(self.tmp / "hidden")])

    def test_a_dry_run_deletes_nothing_and_logs_nothing(self) -> None:
        self.now = time.time() + 8 * DAY
        idle = write(self.tmp / "shots" / "1.png")
        status, output = self.run_once(dry_run=True)
        self.assertEqual(status, 0)
        self.assertTrue(idle.is_file())
        self.assertIn(f"tmp sweep: would delete {self.tmp / 'shots'} (0.0 GiB), last changed ", output)
        self.assertFalse((self.state / tmp_sweep.LOG_NAME).exists())


class OwnerEndTests(TmpSweepCase):
    now: float = 0.0

    def test_unknown_folder_waits_seven_days_and_logs_why(self) -> None:
        for hours in (5, 6 * 24, 8 * 24):
            with self.subTest(hours=hours):
                folder = write(self.tmp / f"backup-{hours}" / "wip.patch").parent
                self.now = time.time() + hours * 3600
                result = self.sweep()
                logged = tmp_sweep.record(result, False, self.now)
                if hours < 7 * 24:
                    self.assertTrue(folder.exists())
                    self.assertIn(str(folder), logged["kept_paths"][
                        "no record proves its owner ended, and it changed in the last 7 days"])
                else:
                    self.assertFalse(folder.exists())
                    row = next(row for row in logged["deleted"] if row["path"] == str(folder))
                    self.assertEqual(row.get("reason"), "no record proves its owner ended; unchanged for 7 days and unused")

    def test_old_unknown_folder_in_use_stays(self) -> None:
        folder = write(self.tmp / "backup" / "files.tar").parent
        fake_process(self.proc, 10, cwd=str(folder))
        self.now = time.time() + 8 * DAY
        result = self.sweep()
        self.assertTrue(folder.exists())
        self.assertIn(str(folder) + ": pid 10 works in it", result.kept[tmp_sweep.IN_USE])

    def test_run_without_end_evidence_waits_seven_days(self) -> None:
        folder = write(self.unit / "unknown-run" / "board.log").parent
        _ = write(folder / "director", ENDED_SESSION)
        self.now = time.time() + 5 * 3600
        result = self.sweep()
        self.assertTrue(folder.exists())
        self.assertIn(str(folder), result.kept[
            "no record proves its owner ended, and it changed in the last 7 days"])
        self.now = self.now + 8 * DAY
        result = self.sweep()
        self.assertFalse(folder.exists())
        self.assertEqual(result.deleted[0].get("reason"),
                         "no record proves its owner ended; unchanged for 7 days and unused")


    def test_top_level_file_and_link_wait_but_tempfile_does_not(self) -> None:
        file = write(self.tmp / "backup.patch")
        link = self.tmp / "backup-link"
        link.symlink_to(file)
        temporary = write(self.tmp / "tmpabcdefgh" / "file").parent
        self.now = time.time() + 5 * 3600
        _ = self.sweep()
        self.assertTrue(file.exists())
        self.assertTrue(link.is_symlink())
        self.assertFalse(temporary.exists())
        self.now = self.now + 8 * DAY
        _ = self.sweep()
        self.assertFalse(file.exists())
        self.assertFalse(link.is_symlink())


class ProcessUseTests(TmpSweepCase):
    def test_an_entry_a_process_works_in_holds_open_or_maps_stays(self) -> None:
        self.now: float = time.time() + 8 * DAY
        worked = write(self.tmp / "worked" / "a").parent
        held = write(self.tmp / "held" / "deep" / "log")
        mapped = write(self.tmp / "mapped" / "lib.so")
        loose = write(self.tmp / "loose.txt")
        idle = write(self.tmp / "idle" / "a").parent
        fake_process(self.proc, 10, cwd=str(worked / "sub"))
        fake_process(self.proc, 11, fds=("/dev/null", f"{loose} (deleted)", str(held)))
        fake_process(self.proc, 12, maps=f"7f00-7f01 r-xp 00000000 00:1f 42   {mapped}\n")
        result = self.sweep()
        for path in (worked, held, mapped, loose):
            self.assertTrue(path.exists(), path)
        self.assertFalse(idle.exists())
        self.assertEqual(sorted(result.kept[tmp_sweep.IN_USE]), [
            f"{self.tmp / 'held'}: pid 11 holds a file open in it",
            f"{loose}: pid 11 holds a file open in it",
            f"{self.tmp / 'mapped'}: pid 12 maps a file in it",
            f"{worked}: pid 10 works in it",
        ])

    def test_with_no_proc_and_no_lsof_nothing_goes(self) -> None:
        idle = write(self.tmp / "idle" / "a")
        self.proc.rmdir()
        with mock.patch.object(tmp_sweep, "LSOF", (str(self.tmp.parent / "no-lsof"),)):
            result = self.sweep()
        self.assertTrue(idle.is_file())
        self.assertEqual(result.deleted, [])
        self.assertIn(str(self.tmp / "idle"), result.kept[tmp_sweep.NO_PROC])


    def test_with_no_proc_an_lsof_scan_shows_which_processes_use_an_entry(self) -> None:
        self.now = time.time() + 8 * DAY
        idle = write(self.tmp / "idle" / "a")
        used = write(self.tmp / "used" / "a")
        self.proc.rmdir()
        lines = f"p42\\nfcwd\\nn{used.parent}\\nf3\\nn/elsewhere\\n"
        with mock.patch.object(tmp_sweep, "LSOF", ("printf", lines)):
            result = self.sweep()
        self.assertFalse(idle.exists())
        self.assertTrue(used.is_file())
        self.assertEqual(result.kept[tmp_sweep.IN_USE], [f"{used.parent}: pid 42 works in it"])

    def test_process_scan_distinguishes_known_empty_from_unavailable(self) -> None:
        known = tmp_sweep.process_paths(str(self.proc), str(self.tmp) + os.sep)
        self.assertIsInstance(known, tmp_sweep.ProcessUsesKnown)
        assert isinstance(known, tmp_sweep.ProcessUsesKnown)
        self.assertEqual(known.paths, [])
        unavailable = tmp_sweep.process_paths(str(self.proc / "missing"), str(self.tmp) + os.sep)
        self.assertIsInstance(unavailable, tmp_sweep.ProcessUsesUnavailable)
        fake_process(self.proc, 17, cwd=str(self.tmp / "used"))
        used = tmp_sweep.process_paths(str(self.proc), str(self.tmp) + os.sep)
        assert isinstance(used, tmp_sweep.ProcessUsesKnown)
        self.assertEqual(used.paths, [(str(self.tmp / "used"), 17, "works in it")])

    def test_lsof_scan_has_explicit_known_and_unavailable_results(self) -> None:
        for output in ("p17\nfcwd\nn/tmp/used\n", "p17\nfcwd\nn/elsewhere\n"):
            with self.subTest(output=output):
                completed = subprocess.CompletedProcess(tmp_sweep.LSOF, 1, output, "")
                with mock.patch.object(subprocess, "run", return_value=completed):
                    known = tmp_sweep.lsof_paths("/tmp/")
                self.assertIsInstance(known, tmp_sweep.ProcessUsesKnown)
                assert isinstance(known, tmp_sweep.ProcessUsesKnown)
                expected = [("/tmp/used", 17, "works in it")] if "/tmp/used" in output else []
                self.assertEqual(known.paths, expected)
        empty = subprocess.CompletedProcess(tmp_sweep.LSOF, 1, "", "")
        with mock.patch.object(subprocess, "run", return_value=empty):
            self.assertIsInstance(tmp_sweep.lsof_paths("/tmp/"), tmp_sweep.ProcessUsesUnavailable)
        for error in (OSError("no lsof"), subprocess.TimeoutExpired("lsof", 1)):
            with self.subTest(error=type(error).__name__), mock.patch.object(subprocess, "run", side_effect=error):
                self.assertIsInstance(tmp_sweep.lsof_paths("/tmp/"), tmp_sweep.ProcessUsesUnavailable)

    def test_lsof_output_names_how_each_process_uses_a_path(self) -> None:
        text = "p7\nfcwd\nn/tmp/a\nftxt\nn/tmp/b/lib\nf4\nn/tmp/c/log\nf5\nn/var/x\np8\nfmem\nn/tmp/d\n"
        self.assertEqual(tmp_sweep.parse_lsof(text, "/tmp/"), [
            ("/tmp/a", 7, "works in it"), ("/tmp/b/lib", 7, "maps a file in it"),
            ("/tmp/c/log", 7, "holds a file open in it"), ("/tmp/d", 8, "maps a file in it")])


class ScratchRootTests(TmpSweepCase):
    def test_common_task_folder_keeps_live_ownership_and_expires_through_the_sweep(self) -> None:
        with mock.patch.dict(os.environ, {copy_record.ROOT_ENV: str(self.tmp / "scratch")}):
            live = scratch.new(scratch.until_task(self.now + DAY), "build")
            due = scratch.new(scratch.until_task(self.now - 60), "build")
            _ = write(live / "Cargo.toml")
            _ = write(due / "Cargo.toml")
            result = self.sweep()
        self.assertTrue(live.is_dir())
        self.assertFalse(due.exists())
        self.assertIn(str(live), result.kept[tmp_sweep.TASK_OPEN])
        self.assertIn(str(due), [entry["path"] for entry in result.deleted])

    def test_a_task_folder_stays_while_its_task_is_open_and_goes_when_its_time_passes(self) -> None:
        scratch_root = self.tmp / "scratch"
        run = write(scratch_root / "run-x-gate-abc123" / "a").parent
        row = write(scratch_root / "row-natedev-C5-copy-abc123" / "a").parent
        due = write(scratch_root / "until-due-abc123" / "a").parent
        later = write(scratch_root / "until-later-abc123" / "a").parent
        loose = write(scratch_root / "loose-abc123" / "a").parent
        tasks = ((run, "run:x"), (row, "row:natedev-C5"), (due, scratch.until_task(self.now - 60)),
                 (later, scratch.until_task(self.now + 3 * DAY)))
        for folder, task in tasks:
            scratch.write_record(folder, task, 0.0)
        self.recent(due / "a")
        files = ((write(scratch_root / "old.txt"), False),
                 (write(scratch_root / "recent.json"), True))
        for file, recent in files:
            if recent:
                self.recent(file)
        planned = self.sweep(dry_run=True)
        planned_paths = [entry["path"] for entry in planned.deleted]
        for file, recent in files:
            with self.subTest(file=file.name, recent=recent):
                self.assertTrue(file.is_file())
                if recent:
                    self.assertIn(str(file), planned.kept.get(tmp_sweep.RECENT, []))
                    self.assertNotIn(str(file), planned_paths)
                else:
                    self.assertIn(str(file), planned_paths)
                self.assertFalse(any("Not a directory" in reason for reason in planned.kept))
        result = self.sweep()
        for file, recent in files:
            with self.subTest(file=file.name, recent=recent):
                self.assertEqual(file.exists(), recent)
        self.assertFalse(due.exists())
        self.assertFalse(loose.exists())
        for folder in (run, row, later):
            self.assertTrue(folder.is_dir(), folder)
        self.assertEqual(sorted(result.kept[tmp_sweep.TASK_OPEN]), [str(run), str(later)])
        self.assertEqual(result.kept[tmp_sweep.ROW_WAITS], [str(row)])
        self.assertIn(str(scratch_root), result.kept[tmp_sweep.ROOT])
        _ = tmp_sweep.sweep(False, self.roots(), time.time() + 8 * DAY, str(self.proc))
        self.assertFalse(run.exists())
        self.assertTrue(row.is_dir())


class TempParentTests(TmpSweepCase):
    def test_common_session_owner_keeps_its_root_and_sweeps_only_idle_entries(self) -> None:
        session_parent = self.tmp / "session"
        owner = build_folders.create_build_folder(build_folders.StableOwnerFolder(
            LIVE_SESSION, build_folders.ExplicitBuildParent(session_parent), mode=0o700))
        idle = build_folders.create_build_folder(build_folders.UniqueBuildFolder(
            "tmp", build_folders.ExplicitBuildParent(owner)))
        recent = build_folders.create_build_folder(build_folders.UniqueBuildFolder(
            "tmp", build_folders.ExplicitBuildParent(owner)))
        _ = write(idle / "output")
        self.recent(write(recent / "output"))
        _ = self.sweep()
        self.assertTrue(owner.is_dir())
        self.assertFalse(idle.exists())
        self.assertTrue(recent.is_dir())

    def test_entries_in_a_live_sessions_temp_root_and_a_run_temp_go_after_4_h(self) -> None:
        live_root = self.tmp / "session" / LIVE_SESSION
        idle = write(live_root / ".tmpAb12Cd" / "a").parent
        fresh = write(live_root / "pymp-x" / "a").parent
        self.recent(fresh / "a")
        gone = write(self.tmp / "session" / "ended-session" / "a").parent
        run_idle = write(self.unit / "run-1" / "tmp" / "tmp.abcdefgh" / "a").parent
        result = self.sweep()
        self.assertFalse(idle.exists())
        self.assertTrue(fresh.is_dir())
        self.assertTrue(live_root.is_dir())
        self.assertFalse(gone.exists())
        self.assertFalse(run_idle.exists())
        self.assertIn(str(live_root), result.kept[tmp_sweep.SESSION_RUNS])

    def test_a_folders_ctime_is_no_change_in_a_temp_folder(self) -> None:
        top = self.tmp / "old"
        deeper = top / "inner" / "deeper"
        deeper.mkdir(parents=True)
        for folder in (deeper, deeper.parent):
            os.utime(folder, (1000, 1000))
        since = time.time() - 60
        self.assertEqual(tmp_sweep.last_change(str(top), tmp_sweep.Limit(since, tmp_sweep.RECENT, folder_ctime=False)),
                         (1000, 0))
        self.assertEqual(tmp_sweep.last_change(str(top), tmp_sweep.Limit(since, tmp_sweep.RECENT)), tmp_sweep.RECENT)
        # An empty folder falls back to its own times, and its own ctime is no change either.
        os.utime(deeper, (1000, 1000))
        self.assertEqual(tmp_sweep.last_change(str(deeper), tmp_sweep.Limit(since, tmp_sweep.RECENT, folder_ctime=False)),
                         (1000, 0))
        self.assertEqual(tmp_sweep.last_change(str(deeper), tmp_sweep.Limit(since, tmp_sweep.RECENT)), tmp_sweep.RECENT)


class TempFolderTests(TmpSweepCase):
    def test_verify_prefix_changes_after_import(self) -> None:
        config = self.tmp / "config"
        config.mkdir()
        source = Path(build_folders.__file__).resolve().parents[2] / "config/build_folders.conf"
        settings = source.read_text()
        name = "changed-hold.123.abcdefgh"
        folder = self.tmp / name
        _ = write(folder / "content")
        with mock.patch.dict(os.environ, {"CONF_DIR": str(config)}), os.scandir(self.tmp) as entries:
            entry = next(entry for entry in entries if entry.name == name)
            _ = write(config / "build_folders.conf", settings)
            roots = self.roots()
            self.assertFalse(roots.judged_temp(str(self.tmp), entry))
            _ = write(config / "build_folders.conf", settings.replace(
                "prefixes.verify-hold=verify-hold.{owner}.",
                "prefixes.verify-hold=changed-hold.{owner}."))
            self.assertFalse(roots.judged_temp(str(self.tmp), entry))
            self.assertTrue(self.roots().judged_temp(str(self.tmp), entry))

    def test_every_common_prefix_keeps_its_sweep_classification(self) -> None:
        pairs: list[tuple[str, Path, Path]] = []
        parents: list[str] = []
        for kind, template in build_folders.FOLDER_PREFIXES.items():
            parent = self.tmp.parent / "common-prefixes" / kind
            parent.mkdir(parents=True)
            parents.append(str(parent))
            prefix = template.format(owner="123")
            allocated = build_folders.create_build_folder(build_folders.UniqueBuildFolder(
                prefix, build_folders.ExplicitBuildParent(parent)))
            previous = parent / (prefix + "Ab12Cd")
            previous.mkdir()
            _ = write(allocated / "Cargo.toml")
            _ = write(previous / "Cargo.toml")
            pairs.append((kind, previous, allocated))
        roots = tmp_sweep.default_roots(str(self.tmp.parent / "plain"), (), (), tuple(parents), (),
                                       tuple(parents), session_temp_roots=())
        _ = tmp_sweep.sweep(False, roots, self.now, str(self.proc))
        for kind, previous, allocated in pairs:
            with self.subTest(kind=kind):
                self.assertEqual(allocated.exists(), previous.exists(),
                                 "the common suffix must retain the cleanup classification")
                if kind in ("verify-copy", "verify-hold"):
                    self.assertFalse(allocated.exists(), "ended verify runs must release their folders")

    def test_only_an_idle_unused_tempfile_folder_in_a_temp_folder_goes(self) -> None:
        temp = self.tmp.parent / "temp"
        idle = write(temp / ".tmpAb12Cd" / "Cargo.toml").parent
        idle_mktemp = write(temp / "tmp.johjtSnxbj" / "a").parent
        idle_python = write(temp / "tmp8m5h288i" / "a").parent
        recent = write(temp / ".tmpQq11Rr" / "a").parent
        self.recent(recent / "a")
        used = write(temp / ".tmpUu22Vv" / "a").parent
        fake_process(self.proc, 10, cwd=str(used))
        others = [write(temp / ".tmpAb12Cde" / "a").parent, write(temp / ".tmpXy34Zw"),
                  write(temp / "notes" / "a").parent, write(temp / ".tmp-b12Cd" / "a").parent,
                  write(temp / "tmp.short" / "a").parent, write(temp / "tmpUPPERCASE" / "a").parent]
        roots = tmp_sweep.default_roots(str(self.tmp.parent / "plain"), (), (), (str(temp),), (),
                                       session_temp_roots=())
        result = tmp_sweep.sweep(False, roots, self.now, str(self.proc))
        for gone in (idle, idle_mktemp, idle_python):
            self.assertFalse(gone.exists(), gone)
        self.assertTrue(recent.is_dir())
        self.assertTrue(used.is_dir())
        for other in others:
            self.assertTrue(other.exists(), other)
        self.assertEqual(sorted(entry["path"] for entry in result.deleted),
                         sorted(str(gone) for gone in (idle, idle_mktemp, idle_python)))
        self.assertEqual(result.kept[tmp_sweep.IN_USE], [f"{used}: pid 10 works in it"])
        kept = [path for paths in result.kept.values() for path in paths]
        self.assertFalse([path for path in kept if any(str(other) in path for other in others)])

    def test_only_empty_folders_and_invoke_logs_in_the_wide_root_go(self) -> None:
        wide = self.tmp.parent / "wide"
        empty = wide / "mend-config-root-test-179035"
        nix_empty = wide / "nix-shell.aN6y2p"
        nested = wide / "p86_r10_crops" / "a" / "b"
        nix_nested = wide / "nix-shell.0tYoAS" / "hana-runtime-1466697-Gv2eJJ" / "artifacts"
        for folder in (empty, nix_empty, nested, nix_nested):
            folder.mkdir(parents=True)
        deep_file = write(wide / "crops-with-file" / "a" / "b" / "f").parents[2]
        full = write(wide / "loose-clone" / "a").parent
        nix_full = write(wide / "nix-build-x" / "a").parent
        tempfile_folder = write(wide / "tmp.johjtSnxbj" / "a").parent
        logs = [write(wide / "lint_invoke.1121749.27492.log"), write(wide / "buildlog.35612.67614768.log"),
                write(wide / "buildlog.35612.676.peak.cgroup")]
        other_file = write(wide / "notes.log")
        used = wide / "tui_pane_theme_loader_badparse_0_1"
        used.mkdir()
        fake_process(self.proc, 11, cwd=str(used))
        kept = [write(wide / "tmux-1000" / "default").parent, wide / ".X11-unix", wide / "ssh-empty"]
        for folder in kept[1:]:
            folder.mkdir()
        for path in (used, *kept[1:]):
            os.utime(path, (1000, 1000))
        roots = tmp_sweep.default_roots(str(self.tmp.parent / "plain"), (), (), (str(wide),), (), (str(wide),),
                                       session_temp_roots=())

        def wide_deleted(result: tmp_sweep.Sweep) -> list[str]:
            return sorted(entry["path"] for entry in result.deleted if Path(entry["path"]).parent == wide)

        early = tmp_sweep.sweep(False, roots, time.time() + 5 * 3600, str(self.proc))
        self.assertEqual(wide_deleted(early), sorted([str(tempfile_folder), *map(str, logs)]))
        empties = sorted(str(path) for path in (empty, nix_empty, nested.parents[1], nix_nested.parents[1]))
        self.assertEqual(sorted(early.kept[tmp_sweep.RECENT_WIDE]), empties)
        late = tmp_sweep.sweep(False, roots, time.time() + 8 * DAY, str(self.proc))
        self.assertEqual(wide_deleted(late), empties)
        self.assertEqual(late.kept[tmp_sweep.IN_USE], [f"{used}: pid 11 works in it"])
        for path in [full, nix_full, deep_file, other_file, used, *kept]:
            self.assertTrue(path.exists(), path)

    def test_a_verify_folder_stays_only_while_the_run_that_made_it_runs(self) -> None:
        wide = self.tmp.parent / "wide"
        made = time.time() - 100
        live = write(wide / "verify-hold.20.Ab12Cd" / "seconds").parent
        copy = write(live / "verify-copy.20.Ef34Gh" / "step").parent
        reused = write(wide / "verify-hold.21.Ab12Cd" / "seconds").parent
        other = write(wide / "verify-copy.22.Ab12Cd" / "step").parent
        gone = write(wide / "verify-hold.23.Ab12Cd" / "seconds").parent
        old_name = write(wide / "verify-hold.0a7oDb" / "seconds").parent
        for folder in (live, copy, reused, other, gone, old_name):
            os.utime(folder, (made, made))
        fake_start(self.proc, 20, made - 50, "bash", "/home/u/.claude/scripts/delegate/verify.sh", "test")
        fake_start(self.proc, 21, made + 50, "bash", "/home/u/.claude/scripts/delegate/verify.sh", "test")
        fake_start(self.proc, 22, made - 50, "python3", "copy_watch.py")
        roots = tmp_sweep.default_roots(str(self.tmp.parent / "plain"), (), (), (str(wide),), (), (str(wide),),
                                       session_temp_roots=())
        soon = tmp_sweep.sweep(False, roots, time.time() + 60, str(self.proc))
        self.assertEqual(sorted(entry["path"] for entry in soon.deleted), sorted(map(str, (reused, other, gone))))
        self.assertEqual(soon.kept[tmp_sweep.VERIFY_RUNS], [str(live)])
        self.assertEqual(soon.kept[tmp_sweep.RECENT], [str(old_name)])
        later = tmp_sweep.sweep(False, roots, time.time() + 8 * DAY, str(self.proc))
        self.assertEqual([entry["path"] for entry in later.deleted], [str(old_name)])
        self.assertTrue(copy.is_dir())

    def test_common_verify_suffix_keeps_live_runs_and_removes_ended_runs(self) -> None:
        made = time.time() - 100
        fake_start(self.proc, 20, made - 50, "bash", "/private/scripts/direct/verify.sh", "test")
        for prefix in ("verify-hold.", "verify-copy."):
            with self.subTest(prefix=prefix):
                parent = self.tmp.parent / prefix
                live = write(parent / (prefix + "20.a1b_2c3d") / "output").parent
                ended = write(parent / (prefix + "21.a1b_2c3d") / "output").parent
                unnamed = write(parent / (prefix + "a1b_2c3d") / "output").parent
                for path in (live, ended, unnamed):
                    os.utime(path, (made, made))
                roots = tmp_sweep.default_roots(str(self.tmp.parent / "plain"), (), (), (str(parent),), (),
                                               (str(parent),), session_temp_roots=())
                soon = tmp_sweep.sweep(False, roots, time.time() + 60, str(self.proc))
                self.assertEqual([entry["path"] for entry in soon.deleted], [str(ended)])
                self.assertEqual(soon.kept[tmp_sweep.VERIFY_RUNS], [str(live)])
                self.assertEqual(soon.kept[tmp_sweep.RECENT], [str(unnamed)])
                self.assertTrue((live / "output").is_file())
                self.assertFalse(ended.exists())
                later = tmp_sweep.sweep(False, roots, time.time() + 8 * DAY, str(self.proc))
                self.assertEqual([entry["path"] for entry in later.deleted], [str(unnamed)])
                self.assertTrue((live / "output").is_file())

    def test_ps_elapsed_time_reads_in_seconds(self) -> None:
        self.assertEqual(tmp_sweep.elapsed_seconds("05:07"), 307)
        self.assertEqual(tmp_sweep.elapsed_seconds("02:05:07"), 7507)
        self.assertEqual(tmp_sweep.elapsed_seconds("3-02:05:07"), 3 * DAY + 7507)
        self.assertIsNone(tmp_sweep.elapsed_seconds(""))
        self.assertIsNone(tmp_sweep.elapsed_seconds("x-05:07"))

    def test_the_wide_root_is_tmp_alone_and_only_when_it_is_a_temp_folder(self) -> None:
        temp = self.tmp.parent / "temp"
        temp.mkdir()
        self.assertEqual(tmp_sweep.default_roots(str(self.tmp), (), (), (str(temp),), (), session_temp_roots=()).wide, ())
        self.assertEqual(tmp_sweep.default_roots(str(self.tmp), (), (), ("/tmp",), (), session_temp_roots=()).wide,
                         (os.path.realpath("/tmp"),))

    def test_roots_are_real_paths(self) -> None:
        """The Mac's /tmp is a link to /private/tmp, and lsof prints real paths."""
        link = self.tmp.parent / "link"
        link.symlink_to(self.tmp.parent)
        roots = tmp_sweep.default_roots(str(link / "claude"), (str(link / "claude" / "unit"),), (), (str(link),), (),
                                       session_temp_roots=())
        self.assertEqual(roots.plain, (str(self.tmp),))
        self.assertEqual(roots.runs, (str(self.unit),))
        self.assertEqual(roots.temp, (str(self.tmp.parent),))


class OwnerTests(TmpSweepCase):
    def markers(self) -> list[Path]:
        return [write(self.unit / "active" / LIVE_SESSION, f"{self.unit / 'run'}\n"),
                write(self.delegate / "active" / ENDED_SESSION, f"{self.delegate / 'old'}\n")]

    def test_the_run_marker_folders_and_their_markers_never_go(self) -> None:
        markers = self.markers()
        for marker in markers:
            os.utime(marker, (self.now - 3 * DAY, self.now - 3 * DAY))
        result = self.sweep()
        for marker in markers:
            self.assertTrue(marker.is_file(), marker)
        self.assertEqual(sorted(result.kept[tmp_sweep.ROOT]), sorted(
            [str(self.delegate), str(self.unit), str(self.delegate / "active"), str(self.unit / "active")]))

    def test_a_run_folder_stays_while_its_director_runs(self) -> None:
        self.now: float = time.time() + 8 * DAY
        live = write(self.unit / "live" / "board.log").parent
        ended = write(self.delegate / "ended" / "board.log").parent
        for run, session in ((live, LIVE_SESSION), (ended, ENDED_SESSION)):
            _ = self.run_owner(run, session)
        unowned = write(self.unit / "unowned" / "board.log").parent
        result = self.sweep()
        self.assertTrue(live.is_dir())
        self.assertFalse(ended.exists())
        self.assertFalse(unowned.exists())
        self.assertEqual(result.kept[tmp_sweep.DIRECTOR_RUNS], [str(live)])

    def test_a_run_folder_a_live_sessions_marker_names_stays_with_no_director_file(self) -> None:
        """A run no worker has dispatched in has no `director` file; its marker names it."""
        marked = write(self.unit / "marked" / "progress_history_state.json", "{}").parent
        _ = write(self.unit / "active" / LIVE_SESSION, f"{marked}\n")
        ended = write(self.delegate / "ended" / "board.log").parent
        _ = write(self.delegate / "active" / ENDED_SESSION, f"{ended}\n")
        result = self.sweep()
        self.assertTrue(marked.is_dir())
        self.assertTrue(ended.is_dir())
        self.assertEqual(sorted(result.kept[tmp_sweep.RunKeepReason.ACTIVE_MARKER.value]),
                         sorted([str(marked), str(ended)]))

    def test_an_empty_roster_lock_does_not_keep_idle_runs(self) -> None:
        self.now = time.time() + 8 * DAY
        marked = write(self.unit / "marked" / "board.log").parent
        idle = write(self.delegate / "idle" / "board.log").parent
        _ = write(self.unit / "active" / LIVE_SESSION, f"{marked}\n")
        _ = write(self.unit / "active" / "progress_history_state.json.lock", "")
        result = self.sweep()
        self.assertTrue(marked.is_dir())
        self.assertEqual(result.kept[tmp_sweep.RunKeepReason.ACTIVE_MARKER.value], [str(marked)])
        self.assertFalse(idle.exists())
        self.assertIn(str(idle), [entry["path"] for entry in result.deleted])

    def test_a_good_marker_keeps_its_reason_when_another_marker_is_bad(self) -> None:
        self.now = time.time() + 8 * DAY
        run = write(self.unit / "marked" / "board.log").parent
        _ = self.run_owner(run, ENDED_SESSION)
        _ = write(self.unit / "active" / ENDED_SESSION, f"{run}\n")
        _ = write(self.delegate / "active" / ENDED_SESSION, "")
        result = self.sweep()
        self.assertTrue((run / "board.log").is_file())
        self.assertEqual(result.kept[tmp_sweep.RunKeepReason.ACTIVE_MARKER.value], [str(run)])
        self.assertNotIn(str(run), result.kept.get(tmp_sweep.RunKeepReason.UNREADABLE_RECORD.value, []))

    def test_a_bad_marker_keeps_only_its_directors_run(self) -> None:
        self.now = time.time() + 8 * DAY
        _ = self.run_owner(self.unit / "owned", ENDED_SESSION)
        active = self.unit / "active"
        active.mkdir()
        marker = active / ENDED_SESSION
        contents = {"empty": b"", "relative": b"relative/path", "invalid-utf8": b"\xff\n",
                    "nul-byte": b"/bad\x00path\n"}
        for kind in (*contents, "directory", "broken-link"):
            with self.subTest(marker=kind):
                run = write(self.unit / "owned" / "board.log").parent
                other = write(self.delegate / "other" / "board.log").parent
                if kind == "directory":
                    marker.mkdir()
                elif kind == "broken-link":
                    marker.symlink_to(self.tmp / "missing-marker")
                else:
                    _ = marker.write_bytes(contents[kind])
                result = self.sweep()
                self.assertTrue((run / "board.log").is_file())
                self.assertEqual(result.kept[tmp_sweep.RunKeepReason.UNREADABLE_RECORD.value], [str(run)])
                self.assertFalse(other.exists())
                self.assertIn(str(other), [entry["path"] for entry in result.deleted])
                remove_marker(marker)

    def test_a_broken_marker_folder_keeps_only_run_folders(self) -> None:
        self.now = time.time() + 8 * DAY
        runs = [write(root / "idle" / "board.log").parent for root in (self.unit, self.delegate)]
        files = [write(self.unit / "wo.json"), write(self.delegate / ".scratch.json")]
        (self.unit / "active").symlink_to(self.tmp / "missing-markers", target_is_directory=True)
        with mock.patch.object(tmp_sweep, "record_object", wraps=tmp_sweep.record_object) as reads:
            result = self.sweep()
        self.assertEqual(sorted(result.kept[tmp_sweep.RunKeepReason.UNREADABLE_RECORD.value]),
                         sorted(str(run) for run in runs))
        for run in runs:
            self.assertTrue((run / "board.log").is_file())
            self.assertIn(mock.call(run / "progress_history_state.json"), reads.call_args_list)
        for file in files:
            self.assertFalse(file.exists())
            self.assertIn(str(file), [entry["path"] for entry in result.deleted])
            self.assertNotIn(mock.call(file / "progress_history_state.json"), reads.call_args_list)
        self.assertFalse(any("Not a directory" in reason for reason in result.kept))

    def test_a_run_folder_whose_history_opens_with_a_live_session_stays(self) -> None:
        self.now = time.time() + 8 * DAY
        history = self.tmp.parent / "history"
        runs = {LIVE_SESSION: self.unit / "started", ENDED_SESSION: self.unit / "started-ended"}
        for session, run in runs.items():
            unit = self.run_owner(run, session)
            log = write(history / f"{run.name}.jsonl", json.dumps(
                {"event_type": "run_started", "main_agent": {"session_id": session}}) + "\n")
            _ = write(run / "progress_history_state.json", json.dumps({"owner_staff_id": unit.id, "history_file": str(log)}))
        result = self.sweep()
        self.assertTrue(runs[LIVE_SESSION].is_dir())
        self.assertFalse(runs[ENDED_SESSION].exists())
        self.assertEqual(result.kept[tmp_sweep.DIRECTOR_RUNS], [str(runs[LIVE_SESSION])])

    def test_a_running_sessions_folder_stays_and_only_its_large_idle_entries_go(self) -> None:
        """The size line is three 4 KiB files: an entry at it goes, one a file under it stays."""
        live = self.project / LIVE_SESSION
        large = live / "scratchpad" / "merge"
        for name in ("a", "b", "c"):
            _ = write(large / name)
        small = live / "scratchpad" / "template"
        for name in ("a", "b"):
            _ = write(small / name)
        fresh = live / "scratchpad" / "fresh"
        for name in ("a", "b", "c"):
            self.recent(write(fresh / name))
        output = write(live / "tasks" / "b1.output")
        ended = write(self.project / ENDED_SESSION / "scratchpad" / "a").parents[1]
        other = write(self.project / "rust" / "kept")
        with mock.patch.object(tmp_sweep, "SESSION_ENTRY_MIN_BYTES", 3 * 4096):
            result = self.sweep()
        self.assertTrue((live / "scratchpad").is_dir())
        self.assertFalse(large.exists())
        self.assertTrue(small.is_dir())
        self.assertTrue(fresh.is_dir())
        self.assertTrue(output.is_file())
        self.assertFalse(ended.exists())
        self.assertFalse(other.exists())
        self.assertEqual(result.kept[tmp_sweep.SESSION_RUNS], [str(live)])
        self.assertEqual(sorted(result.kept[tmp_sweep.SMALL_IN_LIVE_SESSION]), [str(small), str(output)])
        self.assertIn(str(large), [entry["path"] for entry in result.deleted])

    def test_an_unreadable_session_record_keeps_session_and_run_folders(self) -> None:
        self.now = time.time() + 8 * DAY
        self.unreadable_sessions()
        session = write(self.project / ENDED_SESSION / "scratchpad" / "a")
        run = write(self.unit / "run" / "director", f"{ENDED_SESSION}\n")
        marked = write(self.unit / "marked" / "board.log")
        _ = write(self.unit / "active" / ENDED_SESSION, f"{marked.parent}\n")
        plain = write(self.tmp / "plain" / "a")
        result = self.sweep()
        self.assertTrue(session.is_file())
        self.assertTrue(run.is_file())
        self.assertTrue(marked.is_file())
        self.assertFalse(plain.exists())
        self.assertEqual(sorted(result.kept[tmp_sweep.NO_SESSIONS]),
                         [str(self.project / ENDED_SESSION), str(self.unit / "marked"), str(self.unit / "run")])

    def test_a_project_folder_left_empty_goes(self) -> None:
        _ = write(self.project / ENDED_SESSION / "scratchpad" / "a")
        _ = self.sweep()
        self.assertFalse(self.project.exists())
        self.assertTrue(self.session_root.is_dir())

    def test_only_dash_folders_are_projects(self) -> None:
        agent = write(self.session_root / "bash-edit-diff" / "a")
        self.assertEqual(self.roots().projects, (str(self.project),))
        _ = self.sweep()
        self.assertTrue(agent.is_file())


class LogTests(TmpSweepCase):
    def test_log_retention_keeps_the_boundary_and_newer_records_in_order(self) -> None:
        # Whole seconds keep the boundary exact after the ISO timestamp round trip.
        now = float(int(self.now))
        cutoff = now - tmp_sweep.LOG_KEEP_SECONDS
        records = [{"at": datetime.fromtimestamp(at, UTC).isoformat(), "marker": marker}
                   for at, marker in ((cutoff - 1, "expired"), (cutoff, "boundary"), (now - 60, "recent"))]
        path = write(self.state / tmp_sweep.LOG_NAME,
                     "".join(json.dumps(record) + "\n" for record in records))
        current = tmp_sweep.record(tmp_sweep.Sweep(), False, now)

        tmp_sweep.append_log(path, current, now)

        self.assertEqual(self.log(), [*records[1:], current])

    def test_each_run_logs_what_went_and_why_the_rest_stayed(self) -> None:
        self.now: float = time.time() + 8 * DAY
        _ = write(self.tmp / "shots" / "1.png")
        self.recent(write(self.tmp / "fresh.txt"))
        fake_process(self.proc, 10, cwd=str(write(self.tmp / "busy" / "a").parent))
        old: dict[str, object] = {"at": "2026-01-01T00:00:00-04:00", "deleted": []}
        _ = write(self.state / tmp_sweep.LOG_NAME, json.dumps(old) + "\n")
        status, output = self.run_once()
        self.assertEqual(status, 0)
        self.assertRegex(output, r"tmp sweep: deleted 1 entries \(0\.0 GiB\), kept 4: ")
        [entry] = self.log()
        deleted = cast(list[dict[str, object]], entry["deleted"])
        self.assertEqual([item["path"] for item in deleted], [str(self.tmp / "shots")])
        self.assertEqual(entry["kept"], {tmp_sweep.ROOT: 2, tmp_sweep.IN_USE: 1, tmp_sweep.RECENT: 1})
        kept_paths = cast(dict[str, list[str]], entry["kept_paths"])
        self.assertNotIn(tmp_sweep.RECENT, kept_paths)
        self.assertEqual(kept_paths[tmp_sweep.IN_USE], [f"{self.tmp / 'busy'}: pid 10 works in it"])
        self.assertIsInstance(entry["freed_gib"], float)

    def test_due_failure_and_not_due_skip_have_distinct_assessments(self) -> None:
        self.now = time.time() + 8 * DAY
        payload = write(self.tmp / "idle-file")
        unlink = os.unlink

        def refuse_payload(path: str, *, dir_fd: int | None = None) -> None:
            if path == str(payload):
                raise PermissionError("private removal failed")
            unlink(path, dir_fd=dir_fd)

        with (mock.patch.object(tmp_sweep, "default_roots", return_value=self.roots()),
              mock.patch.object(tmp_sweep, "PROC_DIR", str(self.proc)),
              mock.patch.object(os, "unlink", side_effect=refuse_payload) as remove,
              redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            self.assertEqual(tmp_sweep.run_if_due(False, self.now), tmp_sweep.CompletedSweepAssessment(1))
            self.assertTrue(payload.is_file())
            calls = remove.call_count
            self.assertEqual(tmp_sweep.run_if_due(False, self.now + 1), tmp_sweep.SkippedSweepAssessment())
            self.assertEqual(remove.call_count, calls)
        with (mock.patch.object(tmp_sweep, "default_roots", return_value=self.roots()),
              mock.patch.object(tmp_sweep, "PROC_DIR", str(self.proc)),
              redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            self.assertEqual(tmp_sweep.run_if_due(False, self.now + tmp_sweep.RUN_INTERVAL_SECONDS),
                             tmp_sweep.CompletedSweepAssessment(0))
        self.assertFalse(payload.exists())
        self.assertEqual(len(self.log()), 2)

    def test_a_run_waits_an_hour_after_the_last(self) -> None:
        self.now = time.time() + 8 * DAY
        idle = write(self.tmp / "shots" / "1.png")
        run = tmp_sweep.run

        def fixture_run(dry_run: bool, now: float, *, lint_settings: conf.ConfigRead,
                        scratch_settings: conf.ConfigRead) -> int:
            return run(dry_run, now, self.roots(), str(self.proc),
                       lint_settings=lint_settings, scratch_settings=scratch_settings)

        with (mock.patch.object(tmp_sweep, "default_roots", return_value=self.roots()),
              mock.patch.object(tmp_sweep, "run", side_effect=fixture_run),
              redirect_stdout(io.StringIO())):
            self.assertEqual(tmp_sweep.run_if_due(False, self.now), tmp_sweep.CompletedSweepAssessment(0))
            self.assertFalse(idle.exists())
            idle = write(self.tmp / "shots" / "2.png")
            self.assertEqual(tmp_sweep.run_if_due(False, self.now + 3500), tmp_sweep.SkippedSweepAssessment())
            self.assertTrue(idle.is_file())
            self.assertEqual(tmp_sweep.run_if_due(False, self.now + 3600), tmp_sweep.CompletedSweepAssessment(0))
            self.assertFalse(idle.exists())
        self.assertEqual(len(self.log()), 2)


class RunFolderTests(TmpSweepCase):
    def test_an_active_marker_keeps_a_run_after_its_session_stops(self) -> None:
        for root in (self.unit, self.delegate):
            with self.subTest(root=root.name):
                payload = write(root / "marked" / "tmp" / "old" / "file")
                _ = write(root / "active" / ENDED_SESSION, f"{payload.parents[2]}\n")
                _ = self.sweep()
                self.assertTrue(payload.is_file())



    def test_stale_pause_does_not_keep_a_dead_units_worktree(self) -> None:
        self.now: float = time.time() + 8 * DAY
        worktree = self.tmp.parent / "worktree"
        payload = write(self.unit / "paused-unit" / "board.log")
        _ = write(payload.parent / "progress_history_state.json", json.dumps({"working_dir": str(worktree)}))
        _ = write(Path(os.environ["SHUTDOWN_STATE_DIR"]) / "account" / "sessions" / "unit.json", json.dumps({
            "session": {"kind": "unit", "cwd": str(worktree), "run_dirs": []},
            "progress": {"kind": "stopped"},
        }))
        other = write(self.delegate / "ended" / "board.log")
        result = self.sweep()
        self.assertFalse(payload.exists())
        self.assertFalse(other.exists())
        self.assertNotIn(str(payload.parent), result.kept.get(tmp_sweep.RunKeepReason.SESSION_WORKTREE.value, []))

    def test_stale_legacy_pause_does_not_keep_a_dead_units_run(self) -> None:
        self.now = time.time() + 8 * DAY
        worktree = self.tmp.parent / "legacy-worktree"
        payload = write(self.delegate / "legacy-unit" / "board.log")
        _ = write(payload.parent / "progress_history_state.json", json.dumps({"working_dir": str(worktree)}))
        _ = write(Path(os.environ["SHUTDOWN_STATE_DIR"]) / "account" / "record.json", json.dumps({
            "state": "down", "entries": [{"session": {"kind": "unit", "cwd": str(worktree), "run_dirs": []},
                                            "progress": {"kind": "stopped"}}],
        }))
        _ = self.sweep()
        self.assertFalse(payload.exists())

    def test_a_live_units_worktree_keeps_a_run_without_a_director_record(self) -> None:
        worktree = self.tmp.parent / "worktree"
        payload = write(self.delegate / "live-unit" / "board.log")
        _ = write(payload.parent / "progress_history_state.json", json.dumps({"working_dir": str(worktree)}))
        _ = write(Path(os.environ["NOTIFIER_SESSIONS_DIR"]) / "123.json", json.dumps({
            "pid": 123, "sessionId": LIVE_SESSION, "name": "unit", "messagingSocketPath": "",
            "updatedAt": 1, "cwd": str(worktree),
        }))
        _ = self.sweep()
        self.assertTrue(payload.is_file())

    def test_unreadable_ownership_records_keep_the_run(self) -> None:
        for name in ("progress_history_state.json", "history", "marker", "session"):
            with self.subTest(record=name):
                payload = write(self.unit / name / "tmp" / "old" / "file")
                run = payload.parents[2]
                if name == "marker":
                    _ = self.run_owner(run, ENDED_SESSION)
                    broken = self.unit / "active" / ENDED_SESSION
                    broken.mkdir(parents=True)
                elif name == "history":
                    broken = run / name
                    broken.mkdir()
                    _ = write(run / "progress_history_state.json", json.dumps({"history_file": str(broken)}))
                elif name == "session":
                    broken = write(Path(os.environ["NOTIFIER_SESSIONS_DIR"]) / "bad.json", "{")
                else:
                    broken = write(run / name, "{")
                result = self.sweep()
                self.assertTrue(payload.is_file())
                self.assertIn(str(run), result.kept[tmp_sweep.RunKeepReason.UNREADABLE_RECORD.value])
                remove_marker(broken)

    def test_an_ended_idle_run_is_still_removed(self) -> None:
        for root in (self.unit, self.delegate):
            with self.subTest(root=root.name):
                run = write(root / "ended" / "board.log").parent
                owner = fixture_member("showrunner", name="sweep")
                unit = fixture_member("unit", name=root.name, parent=owner.id,
                                      worktree=staff.PresentWorktree(remote.local_machine(), self.tmp.parent / root.name))
                child = fixture_worker(root.name + "-fix", unit.id)
                self.pane_world.add(unit, f"w1:p{len(self.pane_world.locations) + 1}")
                self.pane_world.add(child, f"w1:p{len(self.pane_world.locations) + 1}", live=False)
                write_text_atomic(run / unit_runs.STATE_FILE, json.dumps({unit_runs.OWNER_FIELD: unit.id}))
                result = self.sweep()
                self.assertFalse(run.exists())
                self.assertIn(str(run), [entry["path"] for entry in result.deleted])


class HandCleanupTests(TmpSweepCase):
    def test_stopped_plan_write_can_be_planned_again(self) -> None:
        ended = self.ended_run("ended")
        plan = self.tmp.parent / "plan.json"
        with mock.patch.object(json, "dumps", side_effect=RuntimeError("stopped during serialization")):
            with self.assertRaisesRegex(RuntimeError, "stopped during serialization"):
                _ = tmp_sweep.write_cleanup_plan(plan, self.roots(), self.now, str(self.proc))
        self.assertFalse(plan.exists())
        self.assertEqual(tmp_sweep.write_cleanup_plan(plan, self.roots(), self.now, str(self.proc)), 0)
        self.assertEqual(tmp_sweep.planned_removals(plan), [str(ended)])
        before = plan.read_bytes()
        with self.assertRaises(FileExistsError):
            _ = tmp_sweep.write_cleanup_plan(plan, self.roots(), self.now, str(self.proc))
        self.assertEqual(plan.read_bytes(), before)

    def test_incomplete_legacy_plan_names_file_and_clear_action(self) -> None:
        plan = self.tmp.parent / "plan.json"
        _ = plan.write_text('{"version":')
        with self.assertRaisesRegex(FileExistsError, str(plan) + ".*remove"):
            _ = tmp_sweep.write_cleanup_plan(plan, self.roots(), self.now, str(self.proc))
        self.assertEqual(plan.read_text(), '{"version":')

    def ended_run(self, name: str) -> Path:
        run = write(self.unit / name / "board.log").parent
        owner = fixture_member("showrunner", name="sweep")
        unit = fixture_member("unit", name=name, parent=owner.id, worktree=staff.PresentWorktree(remote.local_machine(), self.tmp.parent / name))
        child = fixture_worker(name + "-fix", unit.id)
        self.pane_world.add(unit, f"w1:p{len(self.pane_world.locations) + 1}")
        self.pane_world.add(child, f"w1:p{len(self.pane_world.locations) + 1}", live=False)
        write_text_atomic(run / unit_runs.STATE_FILE, json.dumps({unit_runs.OWNER_FIELD: unit.id}))
        return run

    def test_plan_lists_keep_and_remove_reasons_and_changes_nothing(self) -> None:
        ended = self.ended_run("ended")
        paused = self.ended_run("paused")
        write_text_atomic(self.unit / "active" / "paused", str(paused) + "\n")
        plain = write(self.tmp / "scratch" / "old.txt")
        plan = self.tmp.parent / "plan.json"
        status, output = self.command("--plan", str(plan))
        self.assertEqual(status, 0, output)
        self.assertTrue((ended / "board.log").is_file())
        self.assertTrue((paused / "board.log").is_file())
        self.assertIn(f"remove {ended}: all pane workers stopped or closed", output)
        self.assertIn(f"keep {paused}: an active run marker", output)
        self.assertIn(f"tmp sweep: plan {plan}", output)
        self.assertEqual(tmp_sweep.planned_removals(plan), [str(ended)])
        saved = cast(dict[str, object], json.loads(plan.read_text()))
        entries = cast(list[tmp_sweep.PlanEntry], saved["entries"])
        self.assertIn(tmp_sweep.PlanEntry(path=str(plain), action="keep",
            reason=tmp_sweep.CleanupKeepReason.NO_END.value), entries)
        self.assertFalse(any("Not a directory" in entry["reason"] for entry in entries))
        self.assertTrue(plain.is_file())
        self.assertFalse(self.state.exists())

    def test_age_without_end_evidence_keeps_an_unused_folder(self) -> None:
        idle = write(self.tmp / "unknown" / "file").parent
        plan = self.tmp.parent / "plan.json"
        status, output = self.command("--plan", str(plan))
        self.assertEqual(status, 0, output)
        self.assertIn(f"keep {idle}: no recorded end", output)
        status, output = self.command("--apply", str(plan))
        self.assertEqual(status, 0, output)
        self.assertTrue((idle / "file").is_file())

    def test_apply_removes_only_planned_paths_and_logs_each_removal(self) -> None:
        ended = self.ended_run("ended")
        kept = self.ended_run("kept")
        write_text_atomic(self.unit / "active" / "kept", str(kept) + "\n")
        plan = self.tmp.parent / "plan.json"
        status, output = self.command("--plan", str(plan))
        self.assertEqual(status, 0, output)
        added = self.ended_run("added-after-plan")
        (self.unit / "active" / "kept").unlink()
        status, output = self.command("--apply", str(plan))
        self.assertEqual(status, 0, output)
        self.assertFalse(ended.exists())
        self.assertTrue((added / "board.log").is_file())
        self.assertTrue((kept / "board.log").is_file())
        self.assertIn(f"tmp sweep: deleted {ended}", output)
        deleted = cast(list[dict[str, object]], self.log()[0]["deleted"])
        self.assertEqual([entry["path"] for entry in deleted], [str(ended)])

    def test_apply_rechecks_worker_marker_process_and_unreadable_record(self) -> None:
        for change in ("worker", "marker", "process", "unreadable"):
            with self.subTest(change=change):
                run = self.ended_run(change)
                plan = self.tmp.parent / f"{change}.json"
                status, output = self.command("--plan", str(plan))
                self.assertEqual(status, 0, output)
                if change == "worker":
                    parent = staff.snapshot().by_id(staff.StaffId(str(cast(dict[str, object], json.loads((run / unit_runs.STATE_FILE).read_text()))[unit_runs.OWNER_FIELD])))
                    assert parent is not None
                    child = staff.snapshot().find(kind="worker", parent=parent.id)[0]
                    pane = next(pane for pane, row in self.pane_world.locations.items() if row.get("label") == child.name)
                    self.pane_world.activate(child.current_session, pane, "claude")
                elif change == "marker":
                    _ = write(self.unit / "active" / change, str(run) + "\n")
                elif change == "process":
                    fake_process(self.proc, 123, cwd=str(run))
                else:
                    write_text_atomic(run / unit_runs.STATE_FILE, "{")
                status, output = self.command("--apply", str(plan))
                self.assertEqual(status, 0, output)
                self.assertTrue((run / "board.log").is_file())
                self.assertIn(f"keep {run}:", output)
                self.assertNotIn(f"deleted {run}", output)

    def test_apply_cannot_delete_a_path_outside_the_current_candidates(self) -> None:
        outside = write(self.tmp.parent / "outside" / "file").parent
        plan = write(self.tmp.parent / "plan.json", json.dumps({"version": 1, "entries": [
            {"path": str(outside), "action": "remove", "reason": "all recorded workers ended"},
        ]}))
        status, output = self.command("--apply", str(plan))
        self.assertEqual(status, 0, output)
        self.assertTrue((outside / "file").is_file())
        self.assertIn(f"keep {outside}: no current sweep candidate", output)

    def test_an_existing_plan_is_preserved_and_bad_plans_remove_nothing(self) -> None:
        run = self.ended_run("ended")
        plan = write(self.tmp.parent / "plan.json", "not json")
        status, output = self.command("--plan", str(plan))
        self.assertEqual(status, 1, output)
        self.assertEqual(plan.read_text(), "not json")
        status, output = self.command("--apply", str(plan))
        self.assertEqual(status, 1, output)
        self.assertTrue((run / "board.log").is_file())


class WorkerAndGitTests(TmpSweepCase):
    def test_finished_detached_records_do_not_keep_a_folder_for_a_worker(self) -> None:
        self.now = time.time() + 8 * DAY
        for guard in ("paused worker", "active run"):
            with self.subTest(guard=guard):
                folder = write(self.tmp / "scratch" / guard.replace(" ", "-") / "repo" / "file").parents[1]
                run = self.live_worker(folder / "repo", run_status="finished")
                if guard == "active run":
                    self.pane_world.remove_leaf("w1:p2")
                    write_text_atomic(self.unit / "active" / LIVE_SESSION, str(run))
                for exit_code in (0, 1, 143):
                    write_text_atomic(run / "detached_runs" / f"finished-{exit_code}.json", json.dumps({
                        "cwd": str(folder / "repo"), "status": "finished", "exit_code": exit_code,
                    }))
                result = self.sweep()
                self.assertNotIn(str(folder), result.kept.get(tmp_sweep.WORKER_USES, []))
                self.assertIn(str(folder), [entry["path"] for entry in result.deleted])
                self.assertFalse(folder.exists())

    def test_sweep_removes_a_killed_fresh_gate_but_keeps_unmarked_git_work(self) -> None:
        source = self.tmp.parent / "fresh-source"
        source.mkdir()
        self.git(source, "init", "-q")
        _ = write(source / "file", "saved\n")
        self.git(source, "add", "file")
        self.git(source, "commit", "-qm", "saved")
        _ = write(source / "file", "working edit\n")
        code = '''
from pathlib import Path
import signal
import sys
from scripts.copies import copies
source = Path(sys.argv[1])
with copies.standing("verify-unit", source, copies.WorktreeSource("HEAD", source)) as tree:
    (tree / "untracked").write_text("test output\\n")
    print(tree.parent, flush=True)
    signal.pause()
'''
        environment = {**os.environ, **copies_environment(self.tmp.parent / "fresh-settings"),
                       "SCRATCH_ROOT": str(self.tmp / "scratch"),
                       "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
        for marked in (True, False):
            with self.subTest(marked=marked), mock.patch.dict(os.environ, environment):
                with subprocess.Popen([sys.executable, "-c", code, str(source)], env=environment,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as child:
                    try:
                        assert child.stdout is not None
                        output = cast(TextIO, child.stdout)
                        ready, _, _ = select.select([output.fileno()], [], [], 30)
                        self.assertTrue(ready, "The child must enter the fresh gate before the kill")
                        line = output.readline().strip()
                        self.assertTrue(line, "The child must report its fresh task folder")
                        folder = Path(line)
                        self.now = time.time() + 8 * DAY
                        _ = self.sweep()
                        self.assertTrue(folder.exists(), "The busy lock must protect an expired fresh gate")
                    finally:
                        child.kill()
                        _, errors = child.communicate(timeout=30)
                    self.assertEqual(child.returncode, -signal.SIGKILL, errors)
                checkout = folder / "checkout"
                self.assertEqual((checkout / "file").read_text(), "working edit\n")
                self.assertEqual((checkout / "untracked").read_text(), "test output\n")
                if not marked:
                    record_path = folder / copy_record.RECORD
                    fields = cast(dict[str, object], json.loads(record_path.read_text()))
                    del fields["throwaway"]
                    _ = record_path.write_text(json.dumps(fields))
                result = self.sweep()
                if marked:
                    self.assertFalse(folder.exists())
                    self.assertIn(str(folder), [entry["path"] for entry in result.deleted])
                else:
                    self.assertTrue(folder.exists())
                    self.assertIn(str(folder), result.kept[tmp_sweep.UNSAVED_WORK])

    def git(self, repo: Path, *arguments: str) -> None:
        _ = subprocess.run(["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@test",
                            *arguments], check=True, capture_output=True, timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
                           env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})

    def clone(self, name: str, *options: str) -> Path:
        source = self.tmp.parent / f"source-{name}"
        source.mkdir()
        self.git(source, "init", "-q")
        _ = write(source / "file", "saved\n")
        self.git(source, "add", "file")
        self.git(source, "commit", "-qm", "saved")
        destination = self.tmp / "scratch" / name / "repo"
        destination.parent.mkdir(parents=True)
        self.git(source, "clone", "-q", *options, str(source), str(destination))
        self.now: float = time.time() + 5 * 3600
        return destination

    def live_worker(self, cwd: Path, status: str = "paused", *, run_status: str = "running") -> Path:
        run = self.unit / "worker-run"
        owner = fixture_member("showrunner", name="sweep")
        unit = fixture_member("unit", name="sweep-unit", parent=owner.id, worktree=staff.PresentWorktree(remote.local_machine(), self.tmp.parent))
        worker = fixture_worker("impl", unit.id, runtime_id=WORKER_SESSION,
                                backend=staff.CodexBackend(self.tmp.parent / "codex"))
        write_text_atomic(run / unit_runs.STATE_FILE, json.dumps({unit_runs.OWNER_FIELD: unit.id}))
        self.pane_world.add(unit, "w1:p1")
        self.pane_world.add(worker, "w1:p2", family="codex", live=status != "ended")
        _ = write(run / "detached_runs" / "run.json", json.dumps({"cwd": str(cwd), "status": run_status}))
        return run

    def test_dirty_clone_without_worker_stays_and_plan_names_unsaved_work(self) -> None:
        repo = self.clone("dirty")
        _ = write(repo / "file", "unsaved edit\n")
        plan = self.tmp.parent / "dirty-plan.json"
        status, output = self.command("--plan", str(plan))
        self.assertEqual(status, 0, output)
        self.assertIn(f"keep {repo.parent}: {tmp_sweep.UNSAVED_WORK}", output)
        result = self.sweep()
        self.assertEqual((repo / "file").read_text(), "unsaved edit\n")
        self.assertIn(str(repo.parent), result.kept[tmp_sweep.UNSAVED_WORK])
        self.assertIn(str(repo.parent), tmp_sweep.record(result, False, self.now)["kept_paths"][tmp_sweep.UNSAVED_WORK])

    def test_untracked_file_and_unpublished_commit_keep_clean_or_dirty_clones(self) -> None:
        for kind in ("untracked", "commit", "detached"):
            with self.subTest(kind=kind):
                repo = self.clone(kind)
                _ = write(repo / "new", "work\n")
                if kind != "untracked":
                    self.git(repo, "add", "new")
                    self.git(repo, "commit", "-qm", "local work")
                    if kind == "detached":
                        self.git(repo, "checkout", "-q", "--detach")
                result = self.sweep()
                self.assertTrue((repo / "new").exists())
                self.assertIn(str(repo.parent), result.kept[tmp_sweep.UNSAVED_WORK])

    def test_clean_pushed_clone_without_worker_is_removed(self) -> None:
        repo = self.clone("pushed")
        result = self.sweep()
        self.assertFalse(repo.exists())
        self.assertIn(str(repo.parent), [entry["path"] for entry in result.deleted])

    def test_clone_source_refs_save_a_local_clone_without_remote_tracking_refs(self) -> None:
        for kind in ("local", "shared"):
            with self.subTest(kind=kind):
                repo = self.clone(kind, *(('--shared',) if kind == "shared" else ()))
                self.git(repo, "remote", "remove", "origin")
                if kind == "local":
                    self.git(repo, "remote", "add", "origin", str(self.tmp.parent / f"source-{kind}"))
                result = self.sweep()
                self.assertFalse(repo.exists())
                self.assertIn(str(repo.parent), [entry["path"] for entry in result.deleted])

    def test_git_file_worktree_with_an_unsaved_edit_stays(self) -> None:
        repo = self.clone("linked-source")
        linked = self.tmp / "scratch" / "linked"
        self.git(repo, "worktree", "add", "-q", "--detach", str(linked))
        _ = write(linked / "file", "local edit\n")
        result = self.sweep()
        self.assertTrue((linked / ".git").is_file())
        self.assertIn(str(linked), result.kept[tmp_sweep.UNSAVED_WORK])

    def test_source_tag_without_a_branch_does_not_replace_a_remote_copy(self) -> None:
        for kind in ("local-tag", "shared-tag"):
            with self.subTest(kind=kind):
                repo = self.clone(kind, *(('--shared',) if kind == "shared-tag" else ()))
                source = self.tmp.parent / f"source-{kind}"
                self.git(source, "branch", "-M", "source-branch")
                self.git(source, "tag", "saved")
                self.git(source, "update-ref", "-d", "refs/heads/source-branch")
                self.git(repo, "remote", "remove", "origin")
                if kind == "local-tag":
                    self.git(repo, "remote", "add", "origin", str(source))
                result = self.sweep()
                self.assertTrue(repo.exists())
                self.assertIn(str(repo.parent), result.kept[tmp_sweep.UNSAVED_WORK])

    def test_git_failure_or_timeout_keeps_the_idle_clone(self) -> None:
        repo = self.clone("error")
        for error in (ValueError("git failed"), subprocess.TimeoutExpired("git", 10)):
            with self.subTest(error=type(error).__name__), mock.patch.object(tmp_sweep, "checked_git", side_effect=error):
                result = self.sweep()
                self.assertTrue(repo.exists())
                self.assertIn(str(repo.parent), result.kept[tmp_sweep.UNREADABLE])

    def test_git_directory_link_is_not_followed_and_keeps_the_entry(self) -> None:
        repo = self.clone("git-link")
        outside = self.tmp.parent / "outside-git"
        os.rename(repo / ".git", outside)
        (repo / ".git").symlink_to(outside)
        with mock.patch.object(tmp_sweep, "checked_git", side_effect=AssertionError("followed git link")):
            result = self.sweep()
        self.assertTrue(repo.exists())
        self.assertTrue(outside.exists())
        self.assertIn(str(repo.parent), result.kept[tmp_sweep.UNREADABLE])

    def test_live_worker_cwd_keeps_idle_folder_until_all_workers_end(self) -> None:
        folder = write(self.tmp / "scratch" / "worker-clone" / "repo" / "file").parents[1]
        run = self.live_worker(folder / "repo")
        plan = self.tmp.parent / "worker-plan.json"
        status, output = self.command("--plan", str(plan))
        self.assertEqual(status, 0, output)
        self.assertIn(f"keep {folder}: {tmp_sweep.WORKER_USES}", output)
        result = self.sweep()
        self.assertTrue(folder.exists())
        self.assertIn(str(folder), tmp_sweep.record(result, False, self.now)["kept_paths"][tmp_sweep.WORKER_USES])
        self.pane_world.foreground.clear()
        write_text_atomic(run / "detached_runs" / "run.json", json.dumps({"status": "finished"}))
        _ = self.sweep()
        self.assertFalse(folder.exists())


    def test_a_running_detached_record_keeps_a_folder_among_finished_records(self) -> None:
        self.now = time.time() + 8 * DAY
        folder = write(self.tmp / "scratch" / "running-worker" / "repo" / "file").parents[1]
        run = self.live_worker(folder / "repo")
        for exit_code in (0, 1):
            _ = write(run / "detached_runs" / f"finished-{exit_code}.json", json.dumps({
                "cwd": str(folder / "repo"), "status": "finished", "exit_code": exit_code,
            }))
        result = self.sweep()
        self.assertTrue(folder.exists())
        self.assertIn(str(folder), result.kept[tmp_sweep.WORKER_USES])
        _ = write(run / "detached_runs" / "run.json", json.dumps({
            "cwd": str(folder / "repo"), "status": "finished", "exit_code": 0,
        }))
        result = self.sweep()
        self.assertFalse(folder.exists())
        self.assertIn(str(folder), [entry["path"] for entry in result.deleted])

    def test_active_run_detached_record_keeps_its_working_folder_under_any_root(self) -> None:
        folder = write(self.tmp / "copy" / "repo" / "file").parents[1]
        run = self.unit / "marked-run"
        _ = write(run / "detached_runs" / "run.json", json.dumps({"cwd": str(folder / "repo"), "status": "running"}))
        _ = write(self.unit / "active" / LIVE_SESSION, str(run))
        result = self.sweep()
        self.assertTrue(folder.exists())
        self.assertIn(str(folder), result.kept[tmp_sweep.WORKER_USES])

    def test_empty_pending_and_detached_run_files_do_not_keep_an_idle_pushed_clone(self) -> None:
        for kind in ("pending", "detached", "both"):
            with self.subTest(kind=kind):
                repo = self.clone(f"empty-{kind}")
                run = self.live_worker(self.tmp.parent / "outside")
                if kind in ("pending", "both"):
                    _ = write(run / "impl.pending.json", "")
                if kind in ("detached", "both"):
                    _ = write(run / "detached_runs" / "empty.json", "")
                errors = io.StringIO()
                with redirect_stderr(errors):
                    result = self.sweep()
                self.assertFalse(repo.exists())
                self.assertIn(str(repo.parent), [entry["path"] for entry in result.deleted])
                self.assertEqual(errors.getvalue(), "")

    def test_unreadable_live_worker_record_keeps_scratch_and_plain_entries_and_names_record(self) -> None:
        folder = write(self.tmp / "scratch" / "unknown-clone" / "file").parent
        other = write(self.tmp / "unknown-copy" / "file").parent
        run = self.live_worker(self.tmp.parent / "unknown")
        temp = write(self.tmp / "session" / WORKER_SESSION / "tmpabcdefgh" / "file").parent
        broken = write(run / "detached_runs" / "bad.json", "{")
        errors = io.StringIO()
        with redirect_stderr(errors):
            result = self.sweep()
        self.assertTrue(folder.exists())
        self.assertTrue(other.exists())
        self.assertFalse(temp.exists())
        self.assertIn(str(temp), [entry["path"] for entry in result.deleted])
        self.assertIn(str(broken), errors.getvalue())
        self.assertIn(str(folder), result.kept[tmp_sweep.UNREADABLE])
        self.assertIn(str(other), result.kept[tmp_sweep.UNREADABLE])

    def test_known_worker_folder_keeps_its_reason_when_another_record_is_unreadable(self) -> None:
        folder = write(self.tmp / "scratch" / "known" / "file").parent
        run = self.live_worker(folder)
        _ = write(run / "detached_runs" / "bad.json", "{")
        with redirect_stderr(io.StringIO()):
            result = self.sweep()
        self.assertTrue(folder.exists())
        self.assertIn(str(folder), result.kept[tmp_sweep.WORKER_USES])

    def test_only_detached_run_cwd_keeps_a_working_folder(self) -> None:
        self.now = time.time() + 8 * DAY
        folder = write(self.tmp / "scratch" / "listed" / "file").parent
        unrelated = write(self.tmp / "unrelated" / "file").parent
        run = self.live_worker(folder)
        _ = write(run / "evidence.json", json.dumps(["pass", {"cwd": str(unrelated)}]))
        _ = write(run / "impl.pending.json", json.dumps({"working_dir": str(unrelated)}))
        _ = write(run / "detached_runs" / "results.json", json.dumps(["pass", {"cwd": str(unrelated)}]))
        _ = write(run / "detached_runs" / "run.json", json.dumps({
            "cwd": str(folder), "status": "running", "metadata": {"cwd": str(unrelated)},
        }))
        result = self.sweep()
        self.assertTrue(folder.exists())
        self.assertFalse(unrelated.exists())
        self.assertIn(str(folder), result.kept[tmp_sweep.WORKER_USES])

    def test_live_worker_session_parent_stays_while_old_entries_go(self) -> None:
        parent = self.tmp / "session" / WORKER_SESSION
        run = self.live_worker(parent)
        old = write(parent / "old" / "file").parent
        fresh = write(parent / "fresh" / "file")
        self.recent(fresh)
        _ = self.sweep()
        self.assertTrue(parent.exists())
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        self.pane_world.foreground.clear()
        write_text_atomic(run / "detached_runs" / "run.json", json.dumps({"status": "finished"}))
        os.utime(fresh, (0, 0))
        _ = self.sweep()
        self.assertFalse(parent.exists())

    def test_apply_rechecks_unsaved_work_added_after_a_remove_plan(self) -> None:
        repo = self.clone("apply")
        scratch.write_record(repo.parent, scratch.until_task(self.now - 60), 0.0)
        plan = self.tmp.parent / "apply-plan.json"
        status, output = self.command("--plan", str(plan))
        self.assertEqual(status, 0, output)
        self.assertIn(f"remove {repo.parent}:", output)
        _ = write(repo / "file", "unsaved after plan\n")
        status, output = self.command("--apply", str(plan))
        self.assertEqual(status, 0, output)
        self.assertTrue(repo.exists())
        self.assertIn(f"keep {repo.parent}: {tmp_sweep.UNSAVED_WORK}", output)


class RunningSessionTests(unittest.TestCase):
    def test_running_session_ids_names_only_running_sessions(self) -> None:
        def record(pid: int, session_id: str) -> sessions.SessionRecord:
            return cast(sessions.SessionRecord, cast(object, {
                "pid": pid, "sessionId": session_id, "name": "", "messagingSocketPath": "",
                "updatedAt": 1, "tmux": "", "formerNames": []}))
        def running(found: sessions.SessionRecord) -> bool:
            return found["pid"] == 1
        registry = sessions.RegistryRead((
            sessions.RegistryObservation(record(1, LIVE_SESSION), {}),
            sessions.RegistryObservation(record(2, ENDED_SESSION), {}),
        ), ())
        with (mock.patch.object(sessions, "read_registry", return_value=registry),
              mock.patch.object(sessions, "running_session", side_effect=running)):
            self.assertEqual(tmp_sweep.running_session_ids(), frozenset({LIVE_SESSION}))

    def test_an_unreadable_record_reads_as_none(self) -> None:
        registry = sessions.RegistryRead((), (Path("/sessions/1.json"),))
        with mock.patch.object(sessions, "read_registry", return_value=registry):
            self.assertIsNone(tmp_sweep.running_session_ids())


class PaneRunTests(TmpSweepCase):
    def test_live_director_keeps_its_run_without_an_extra_staff_read(self) -> None:
        parent = fixture_member("unit", name="native-unit", parent=fixture_member("showrunner", name="runner").id,
                                runtime_id=LIVE_SESSION)
        payload = write(self.unit / "native-run" / "payload")
        write_text_atomic(payload.parent / unit_runs.STATE_FILE, json.dumps({unit_runs.OWNER_FIELD: parent.id}))
        with mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as snapshot, mock.patch.object(
                staff, "local_snapshot", side_effect=AssertionError("extra local staff read")):
            result = self.sweep()
        self.assertTrue(payload.exists())
        self.assertIn(str(payload.parent), result.kept[tmp_sweep.DIRECTOR_RUNS])
        self.assertEqual(snapshot.call_count, 1)

    now: float = 0.0



    def test_five_states_keep_only_open_or_unproved_runs(self) -> None:
        parent = fixture_member("unit", name="pane-unit", parent=fixture_member("showrunner", name="pane-runner").id, worktree=staff.PresentWorktree(remote.local_machine(), self.tmp.parent))
        worker = fixture_worker("reader", parent.id)
        run = self.unit / "pane-run"
        self.now = self.now + 8 * DAY
        cases: tuple[tuple[worker_panes.WorkerPane, bool], ...] = (
            (worker_panes.OpenWorker(worker, "p1", "t1", herdr.AgentStatus.IDLE), True),
            (worker_panes.StoppedWorker(worker, "p1", "t1"), False),
            (worker_panes.ClosedWorker(worker), False),
            (worker_panes.ProcessUnknownWorker(worker, "p1", "t1", "process unreadable"), True),
            (worker_panes.UncertainWorker(worker, "pane read incomplete"), True),
        )
        for state, keep in cases:
            payload = write(run / "payload")
            write_text_atomic(run / unit_runs.STATE_FILE, json.dumps({unit_runs.OWNER_FIELD: parent.id}))
            error = io.StringIO()
            with self.subTest(state=state), mock.patch.object(unit_runs, "held", return_value=[str(run)]), mock.patch.object(
                    worker_panes, "observe_parents", return_value={parent.id: [state]}), mock.patch.object(
                    staff, "snapshot", wraps=staff.snapshot) as snapshot, redirect_stderr(error):
                result = self.sweep()
            self.assertEqual(payload.exists(), keep)
            self.assertEqual(snapshot.call_count, 1)
            if isinstance(state, (worker_panes.ProcessUnknownWorker, worker_panes.UncertainWorker)):
                self.assertIn(state.reason, error.getvalue())
                self.assertIn("reader", error.getvalue())
                self.assertIn(str(run), result.kept[tmp_sweep.RunKeepReason.UNENDED_WORKER.value])

    def test_pane_worker_thread_keeps_its_session_folder(self) -> None:
        parent = fixture_member("unit", name="pane-unit", parent=fixture_member("showrunner", name="pane-runner").id, worktree=staff.PresentWorktree(remote.local_machine(), self.tmp.parent))
        worker = fixture_worker("reader", parent.id)
        run = self.unit / "pane-run"
        write_text_atomic(run / "payload", "payload")
        write_text_atomic(run / unit_runs.STATE_FILE, json.dumps({unit_runs.OWNER_FIELD: parent.id}))
        state = worker_panes.ProcessUnknownWorker(worker, "p1", "t1", "process unreadable")
        with mock.patch.object(unit_runs, "held", return_value=[str(run)]), mock.patch.object(
                worker_panes, "observe_parents", return_value={parent.id: [state]}):
            use = tmp_sweep.pane_run_use()
            observed = tmp_sweep.worker_use({str(run): tmp_sweep.RunKeepReason.UNENDED_WORKER}, use)
        self.assertIn(worker.current_session, observed.threads)
        self.assertEqual(observed.unreadable_records, ())

    def test_missing_run_member_keeps_the_folder_and_names_the_reference(self) -> None:
        payload = write(self.unit / "missing-member" / "payload")
        missing = fixture_runtime_id("absent-sweep-parent")
        write_text_atomic(payload.parent / unit_runs.STATE_FILE, json.dumps({unit_runs.OWNER_FIELD: missing}))
        error = io.StringIO()
        with redirect_stderr(error):
            result = self.sweep()
        self.assertTrue(payload.exists())
        self.assertIn(missing, error.getvalue())
        self.assertIn(str(payload.parent), result.kept[tmp_sweep.RunKeepReason.UNREADABLE_RECORD.value])

    def test_malformed_staff_keeps_the_folder_and_names_the_cause(self) -> None:
        payload = write(self.unit / "bad-record" / "payload")
        write_text_atomic(self.tmp.parent / "staff.json", "not json")
        error = io.StringIO()
        with redirect_stderr(error):
            result = self.sweep()
        self.assertTrue(payload.exists())
        self.assertIn("staff", error.getvalue())
        self.assertIn(str(payload.parent), result.kept[tmp_sweep.RunKeepReason.UNREADABLE_RECORD.value])


class PaneSweepOwnerTests(StaffTestCase):
    def test_unreachable_owner_keeps_the_run_and_names_the_cause(self) -> None:
        root = self.root / "tmp"
        run = root / "unit" / "run"
        payload = write(run / "payload")
        roots = tmp_sweep.default_roots(str(root), (str(root / "unit"),), (), (), (), (), ())
        error = io.StringIO()
        with self.machine("mac"), mock.patch.dict(os.environ, {"STAFF_TEST_SSH_FAIL": "1"}), mock.patch.object(
                tmp_sweep, "running_session_ids", return_value=frozenset()), mock.patch.object(
                tmp_sweep, "process_paths", return_value=tmp_sweep.ProcessUsesKnown([])), redirect_stderr(error):
            entries = list(tmp_sweep.judge_entries(roots, time.time() + 8 * DAY, str(self.root / "proc")))
        self.assertTrue(payload.exists())
        self.assertIn("natedev not reached", error.getvalue())
        self.assertTrue(any(isinstance(entry, tmp_sweep.KeptEntry) and entry.path == str(run)
                            and entry.reason == tmp_sweep.RunKeepReason.UNREADABLE_RECORD.value for entry in entries))


if __name__ == "__main__":
    _ = unittest.main()
