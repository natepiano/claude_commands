#!/usr/bin/env python3
"""Tests for folder_audit.py on a fabricated ~/rust, temp folder and transcript set. A test moves
each folder's time back two days unless it wants the folder recent; a folder holds one file, since
the tmp sweep owns empty temp folders."""

from __future__ import annotations

import os
import fcntl
import socket
import subprocess
import sys
import time
import unittest
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, cast, override

import unittest.mock

if TYPE_CHECKING:
    from ..lib.socket_test_support import short_socket_directory
else:
    from scripts.lib.socket_test_support import short_socket_directory

import folder_audit
import scratch
import tmp_sweep
from scripts.lib import copy_record

DAY = 86_400


def age(path: Path, days: float) -> None:
    then = time.time() - days * DAY
    for inner in sorted(path.rglob("*"), reverse=True):
        os.utime(inner, (then, then), follow_symlinks=False)
    os.utime(path, (then, then))


def stamp(path: Path, when: float) -> None:
    os.utime(path, (when, when), follow_symlinks=False)


def changed(path: Path) -> float:
    value = folder_audit.last_change(path)
    assert isinstance(value, float), value
    return value


def folder(path: Path, age_days: float = 2) -> Path:
    path.mkdir(parents=True)
    _ = (path / "file").write_text("x")
    age(path, age_days)
    return path


def git(repo: Path, *arguments: str) -> None:
    _ = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *arguments],
                       check=True, capture_output=True)


class FolderAuditTests(unittest.TestCase):
    root: Path = Path()
    rust: Path = Path()
    temp: Path = Path()
    recorded_ctimes: dict[str, float] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(short_socket_directory()))
        self.rust = self.root / "rust"
        self.temp = self.root / "tmp"
        self.rust.mkdir()
        self.temp.mkdir()
        self.recorded_ctimes = {}
        scan = os.scandir

        @contextmanager
        def recorded_scan(path: str | int | os.PathLike[str]) -> Generator[Iterator[os.DirEntry[str]]]:
            with scan(path) as entries:
                if isinstance(path, int):
                    yield entries
                    return
                recorded: list[os.DirEntry[str]] = []
                for entry in entries:
                    info = entry.stat(follow_symlinks=False)
                    fields = list(info)
                    fields[9] = self.recorded_ctimes.get(entry.path, info.st_mtime)
                    fake = unittest.mock.Mock(wraps=entry)
                    fake.configure_mock(**{"stat.return_value": os.stat_result(fields, {"st_blocks": info.st_blocks})})
                    fake.name = entry.name
                    fake.path = entry.path
                    recorded.append(cast(os.DirEntry[str], fake))
                yield iter(recorded)

        _ = self.enterContext(unittest.mock.patch.object(os, "scandir", side_effect=recorded_scan))
        state = self.root / "state"
        self.enterContext(unittest.mock.patch.dict(os.environ, {"FOLDER_SWEEP_STATE_DIR": str(state),
                                                                "SCRATCH_HOLDS": str(state / "holds.json"),
                                                                "SCRATCH_ROOT": str(self.root / "scratch"),
                                                                "SCRATCH_VISIBLE_ROOT": str(self.root / "repro"),
                                                                "SCRATCH_COPIES_ROOT": str(self.root / "copies"),
                                                                "HANAS_CONFIG": str(self.root / "hanas.conf"),
                                                                "MAC_TEST_CONFIG": str(self.root / "mac_test.conf")}))
        _ = (self.root / "hanas.conf").write_text("clone_max_hours=2.5\n")
        _ = (self.root / "mac_test.conf").write_text("free_floor_gib=8\n")

    def clone_folder(self, name: str, hours: float, *, dirty: bool = False) -> Path:
        source = self.root / "clone-source"
        if not source.exists():
            source.mkdir()
            git(source, "init", "-q")
            _ = (source / "asset").write_text("saved source")
            _ = (source / ".gitignore").write_text("target/\n")
            git(source, "add", "asset", ".gitignore")
            git(source, "commit", "-qm", "source")
        checkout = scratch.clone(source, f"gate:{name}", name)
        if dirty:
            _ = (checkout / "asset").write_text("unsaved source")
        (checkout / "target").mkdir()
        _ = (checkout / "target" / "build").write_text("build output")
        age(checkout.parent, hours / 24)
        return checkout.parent

    def judge_clones(self, paths: list[Path], *, mac: bool = False,
                     free_space: int = 16 * 1024**3, removing: bool = True) -> dict[Path, str]:
        (self.root / "proc").mkdir(exist_ok=True)
        return folder_audit.judge([(path, changed(path)) for path in paths], time.time(), removing,
                                  proc=str(self.root / "proc"), sockets=str(self.root / "unix"),
                                  mac=mac, free_space=lambda _: free_space)

    def test_clone_age_comes_from_runtime_config_without_changing_other_folders_limits(self) -> None:
        old = self.clone_folder("old", 3)
        young = self.clone_folder("young", 2)
        ordinary = folder(self.temp / "ordinary", 3 / 24)

        outcomes = self.judge_clones([young, ordinary, old])

        self.assertEqual(outcomes[old], folder_audit.REMOVED)
        self.assertFalse(old.exists())
        self.assertTrue(young.exists())
        self.assertTrue(ordinary.exists())
        _ = (self.root / "hanas.conf").write_text("clone_max_hours=1\n")
        self.assertEqual(self.judge_clones([young])[young], folder_audit.REMOVED)
        self.assertFalse(young.exists())

    def test_below_the_mac_floor_removes_the_oldest_idle_clone_until_space_recovers(self) -> None:
        younger = self.clone_folder("younger", 0.25)
        older = self.clone_folder("older", 0.5)
        (self.root / "proc").mkdir()
        checked_paths: list[Path] = []

        def free_space(path: Path) -> int:
            checked_paths.append(path)
            return 8 * 1024**3 if not older.exists() else 0

        outcomes = folder_audit.judge(
            [(path, changed(path)) for path in (younger, older)], time.time(), True,
            proc=str(self.root / "proc"), sockets=str(self.root / "unix"), mac=True, free_space=free_space)

        self.assertEqual(outcomes[older], folder_audit.REMOVED)
        self.assertFalse(older.exists())
        self.assertTrue(younger.exists())
        self.assertGreaterEqual(len(checked_paths), 2)

    def test_mac_floor_uses_its_runtime_config_and_does_not_apply_on_linux(self) -> None:
        clone = self.clone_folder("floor", 0.25)
        self.assertTrue(clone.exists())
        _ = self.judge_clones([clone], mac=False, free_space=0)
        self.assertTrue(clone.exists())
        _ = self.judge_clones([clone], mac=True, free_space=9 * 1024**3)
        self.assertTrue(clone.exists())
        _ = (self.root / "mac_test.conf").write_text("free_floor_gib=10\n")
        outcomes = self.judge_clones([clone], mac=True, free_space=9 * 1024**3)
        self.assertEqual(outcomes[clone], folder_audit.REMOVED)
        self.assertFalse(clone.exists())

    def test_below_the_mac_floor_dirty_clone_loses_only_its_target(self) -> None:
        clone = self.clone_folder("dirty-clone", 0.25, dirty=True)
        checkout = clone / "checkout"
        before = (clone / copy_record.RECORD).read_bytes()

        _ = self.judge_clones([clone], mac=True, free_space=16 * 1024**3)
        self.assertTrue((checkout / "target" / "build").exists())

        outcomes = self.judge_clones([clone], mac=True, free_space=0)

        self.assertEqual(outcomes[clone], folder_audit.TARGET_REMOVED)
        self.assertFalse((checkout / "target").exists())
        self.assertEqual((checkout / "asset").read_text(), "unsaved source")
        self.assertTrue((checkout / ".git").is_dir())
        self.assertEqual((clone / copy_record.RECORD).read_bytes(), before)
        _ = self.judge_clones([clone], mac=True, free_space=0)
        self.assertEqual((checkout / "asset").read_text(), "unsaved source")

    def test_clone_cleanup_keeps_process_hold_socket_and_busy_lock_guards(self) -> None:
        used = self.clone_folder("used-clone", 3)
        held = self.clone_folder("held-clone", 3)
        listening = self.clone_folder("socket-clone", 3)
        busy = self.clone_folder("busy-clone", 3)
        pid = self.root / "proc" / "12"
        pid.mkdir(parents=True)
        (pid / "cwd").symlink_to(used / "checkout")
        _ = scratch.hold(held, "gate:keep-clone", "test")
        server = socket.socket(socket.AF_UNIX)
        self.addCleanup(server.close)
        server.bind(str(listening / "socket"))
        _ = (self.root / "unix").write_text(
            f"Num RefCount Protocol Flags Type St Inode Path\n0: 2 0 10000 1 1 7 {listening / 'socket'}\n")
        with (busy / scratch.BUSY).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            outcomes = self.judge_clones([used, held, listening, busy], mac=True, free_space=0)
        self.assertEqual(outcomes[used], "pid 12 works in it")
        self.assertEqual(outcomes[held], folder_audit.HELD)
        self.assertEqual(outcomes[listening], folder_audit.LIVE_SOCKET)
        for path in (used, held, listening, busy):
            self.assertTrue((path / "checkout" / "target" / "build").exists(), path)

    def test_a_held_clone_lock_keeps_normal_and_linked_checkouts_clean(self) -> None:
        clone = self.clone_folder("clean-lock", 3)
        checkout = clone / "checkout"
        linked = self.rust / "linked-clone"
        git(checkout, "worktree", "add", "-q", "--detach", str(linked), "HEAD")
        for path in (checkout, linked):
            with self.subTest(path=path):
                self.assertEqual(tmp_sweep.git_work(path), tmp_sweep.GitWork.SAVED)
                with scratch.removal_lock(path) as idle:
                    self.assertTrue(idle)
                    self.assertEqual(tmp_sweep.git_work(path), tmp_sweep.GitWork.SAVED)
                    with scratch.judgment_lock(path) as available:
                        self.assertFalse(available)
                    self.assertFalse((path / scratch.BUSY).exists())
                self.assertEqual(tmp_sweep.git_work(path), tmp_sweep.GitWork.SAVED)

    def test_audit_finds_young_clones_in_an_explicit_scratch_root_below_the_mac_floor(self) -> None:
        clone = self.clone_folder("discover", 0.25)
        (self.root / "proc").mkdir()
        found = folder_audit.audit(
            self.rust, [], frozenset[str](), time.time(), files=[], removing=True,
            proc=str(self.root / "proc"), sockets=str(self.root / "unix"), mac=True,
            free_space=lambda _: 0, clone_roots=[self.root / "scratch"])
        self.assertIn((clone, folder_audit.REMOVED), [(entry.path, entry.outcome) for entry in found])
        self.assertFalse(clone.exists())

    def test_canonical_clone_paths_keep_private_tmp_lsof_and_hold_guards(self) -> None:
        used = self.clone_folder("alias-used", 3)
        held = self.clone_folder("alias-held", 3)
        private_tmp = self.root / "macfs" / "private" / "tmp"
        private_tmp.mkdir(parents=True)
        (private_tmp / "claude").mkdir()
        real_root = private_tmp / "claude" / "scratch"
        _ = (self.root / "scratch").rename(real_root)
        alias = self.root / "macfs" / "tmp"
        alias.symlink_to(private_tmp, target_is_directory=True)
        used = real_root / used.name
        held = real_root / held.name
        _ = scratch.hold(held, "gate:keep-alias", "test")
        output = f"p42\nfcwd\nn{used / 'checkout'}\n"
        lsof = self.root / "fake-lsof"
        _ = lsof.write_text(f"#!{sys.executable}\nprint({output!r}, end='')\n")
        lsof.chmod(0o755)
        with unittest.mock.patch.object(tmp_sweep, "LSOF", (str(lsof),)):
            found = folder_audit.audit(
                self.rust, [], frozenset[str](), time.time(), files=[], removing=True,
                proc=str(self.root / "missing-proc"), sockets=str(self.root / "unix"),
                clone_roots=[alias / "claude" / "scratch"], mac=True, free_space=lambda _: 0)
        outcomes = {entry.path: entry.outcome for entry in found}
        self.assertEqual(outcomes[used], "pid 42 works in it")
        self.assertEqual(outcomes[held], folder_audit.HELD)
        for path in (used, held):
            self.assertTrue((path / "checkout" / "target" / "build").exists())

    def test_standing_mac_clone_has_no_age_or_floor_exemption(self) -> None:
        expected = folder_audit.expected_rust_names()
        self.assertIsNotNone(expected)
        (self.root / "proc").mkdir()
        for mode in ("age", "floor", "dirty"):
            with self.subTest(mode=mode):
                parent = self.clone_folder(f"standing-{mode}", 3 if mode == "age" else 0.25,
                                           dirty=mode == "dirty")
                standing = self.rust / "hana_catalyst_mac"
                _ = (parent / "checkout").rename(standing)
                age(standing, (3 if mode == "age" else 0.25) / 24)
                found = folder_audit.audit(
                    self.rust, [], expected, time.time(), files=[], removing=True,
                    proc=str(self.root / "proc"), sockets=str(self.root / "unix"),
                    clone_roots=[], mac=True, free_space=lambda _: 16 * 1024**3 if mode == "age" else 0)
                outcomes = {entry.path: entry.outcome for entry in found}
                self.assertEqual(outcomes[standing], folder_audit.TARGET_REMOVED if mode == "dirty"
                                 else folder_audit.REMOVED)
                if mode == "dirty":
                    self.assertEqual((standing / "asset").read_text(), "unsaved source")
                    self.assertTrue((standing / ".git").is_dir())
                    self.assertFalse((standing / "target").exists())
                else:
                    self.assertFalse(standing.exists())

    def test_audit_resumes_a_stopped_git_deletion_after_live_and_hold_checks(self) -> None:
        clone = self.clone_folder("partial-git-removal", 3)
        checkout = clone / "checkout"

        def stop_during_git_delete(path: str) -> None:
            self.assertEqual(Path(path), clone)
            (checkout / ".git" / "HEAD").unlink()
            raise RuntimeError("stopped during git deletion")

        with unittest.mock.patch.object(tmp_sweep, "delete_tree", side_effect=stop_during_git_delete):
            with self.assertRaisesRegex(RuntimeError, "stopped during git deletion"):
                _ = self.judge_clones([clone])
        self.assertIsInstance(scratch.pending_removal(clone), copy_record.Registered)
        self.assertTrue(clone.exists())
        _ = scratch.hold(clone, "gate:keep-partial", "test")
        outcomes = self.judge_clones([clone], mac=True, free_space=0)
        self.assertEqual(outcomes[clone], folder_audit.HELD)
        self.assertTrue(clone.exists())
        _ = scratch.release(clone)
        pid = self.root / "proc" / "42"
        pid.mkdir()
        (pid / "cwd").symlink_to(checkout)
        outcomes = self.judge_clones([clone], mac=True, free_space=0)
        self.assertEqual(outcomes[clone], "pid 42 works in it")
        self.assertTrue(clone.exists())
        (pid / "cwd").unlink()
        found = folder_audit.audit(
            self.rust, [], frozenset[str](), time.time(), files=[], removing=True,
            proc=str(self.root / "proc"), sockets=str(self.root / "unix"),
            clone_roots=[self.root / "scratch"], mac=True, free_space=lambda _: 0)
        self.assertIn((clone, folder_audit.REMOVED), [(entry.path, entry.outcome) for entry in found])
        self.assertFalse(clone.exists())
        self.assertIsInstance(scratch.pending_removal(clone), copy_record.NotRegistered)

    def test_unavailable_process_inventory_keeps_an_overdue_clone_and_its_target(self) -> None:
        clone = self.clone_folder("unknown-use", 3)
        with unittest.mock.patch.object(tmp_sweep, "LSOF", (str(self.root / "no-lsof"),)):
            outcomes = folder_audit.judge(
                [(clone, changed(clone))], time.time(), True,
                proc=str(self.root / "no-proc"), sockets=str(self.root / "no-sockets"),
                mac=True, free_space=lambda _: 0)
        self.assertEqual(outcomes[clone], tmp_sweep.NO_PROC)
        self.assertTrue((clone / "checkout" / "target" / "build").exists())

    def test_copies_root_keeps_an_old_open_phase_copy(self) -> None:
        import json
        copy = folder(self.temp / "copies" / "show-maker" / "21-gate", 30)
        _ = (copy / copy_record.RECORD).write_text(json.dumps({"task": "phase:maker-unit/21"}))
        age(self.temp / "copies", 30)
        proc = self.root / "proc"
        proc.mkdir()
        _ = folder_audit.audit(self.rust, [self.temp], frozenset[str](), time.time(), files=[],
                               removing=True, proc=str(proc), sockets=str(self.root / "unix"))
        self.assertTrue(copy.is_dir())

    def test_copied_folder_needs_seven_days_since_its_last_metadata_change(self) -> None:
        now = time.time()
        proc = self.root / "proc"
        proc.mkdir()
        copied = folder(self.temp / "copied-backup", 8)
        old = folder(self.temp / "old-backup", 8)
        self.recorded_ctimes[str(copied / "file")] = now - 5 * 3600
        self.recorded_ctimes[str(old / "file")] = now - 8 * DAY
        found = folder_audit.audit(self.rust, [self.temp], frozenset[str](), now, files=[],
                                   removing=True, proc=str(proc), sockets=str(self.root / "unix"))
        self.assertTrue(copied.is_dir())
        self.assertFalse(old.exists())
        self.assertIn((old, folder_audit.REMOVED), [(entry.path, entry.outcome) for entry in found])

    def test_process_uses_distinguishes_known_empty_from_unavailable(self) -> None:
        proc = self.root / "proc"
        proc.mkdir()
        candidate = self.temp / "candidate"
        for paths in ([], [candidate]):
            with self.subTest(paths=paths):
                with unittest.mock.patch.object(tmp_sweep, "lsof_paths", side_effect=AssertionError("unexpected fallback")):
                    known = folder_audit.process_uses(paths, str(proc))
                self.assertIsInstance(known, tmp_sweep.ProcessUsesKnown)
                assert isinstance(known, tmp_sweep.ProcessUsesKnown)
                self.assertEqual(known.paths, [])
        with unittest.mock.patch.object(tmp_sweep, "LSOF", (str(self.root / "no-lsof"),)):
            unavailable = folder_audit.process_uses([candidate], str(self.root / "no-proc"))
        self.assertIsInstance(unavailable, tmp_sweep.ProcessUsesUnavailable)

    def test_unavailable_process_uses_keeps_due_folders(self) -> None:
        candidate = folder(self.temp / "candidate", 8)
        now = time.time()
        with (unittest.mock.patch.object(tmp_sweep, "LSOF", (str(self.root / "no-lsof"),)),
              unittest.mock.patch.object(folder_audit, "remove", side_effect=AssertionError("removed without a process scan"))):
            outcomes = folder_audit.judge([(candidate, now - 8 * DAY)], now, True,
                                         proc=str(self.root / "no-proc"), sockets=str(self.root / "no-sockets"))
        self.assertEqual(outcomes[candidate], tmp_sweep.NO_PROC)
        self.assertTrue(candidate.exists())

    def test_lsof_fallback_keeps_used_folders_and_judges_unused_folders(self) -> None:
        used = folder(self.temp / "used", 8)
        idle = folder(self.temp / "idle", 8)
        now = time.time()
        output = f"p42\nfcwd\nn{used}\n"
        completed = subprocess.CompletedProcess(tmp_sweep.LSOF, 1, output, "")
        with unittest.mock.patch.object(subprocess, "run", return_value=completed):
            outcomes = folder_audit.judge([(used, now - 8 * DAY), (idle, now - 8 * DAY)], now, False,
                                         proc=str(self.root / "no-proc"), sockets=str(self.root / "no-sockets"))
        self.assertEqual(outcomes, {used: "pid 42 works in it", idle: folder_audit.WOULD_REMOVE})

    def test_the_roots_file_ignores_comments_and_blank_lines(self) -> None:
        roots = self.root / "rust_roots.txt"
        _ = roots.write_text("# top\nhana\n\nbevy  # engine\n  hana videos\n")
        self.assertEqual(folder_audit.expected_rust_names(roots), frozenset({"hana", "bevy", "hana videos"}))
        self.assertIsNone(folder_audit.expected_rust_names(self.root / "missing.txt"))

    def test_only_unexpected_idle_folders_are_loose(self) -> None:
        _ = folder(self.rust / "hana")
        _ = folder(self.rust / "repro")
        stray = folder(self.rust / "hana_p34d_alone")
        _ = folder(self.rust / "fresh_try", age_days=0.1)
        _ = (self.rust / "notes.md").write_text("a file, not a folder")
        (self.rust / "link").symlink_to(self.rust / "hana")
        for name in ("claude", "claude-1000", "systemd-private-x", ".tmpAbC123", "tmux-1000"):
            _ = folder(self.temp / name)
        (self.temp / "empty").mkdir()
        age(self.temp / "empty", 2)
        loose_temp = folder(self.temp / "my-repro")
        in_scratch = folder(self.rust / "scratch" / "berth-phase1")
        loose = folder_audit.audit(self.rust, [self.temp], frozenset({"hana"}), time.time(), files=[])
        self.assertEqual([entry.path for entry in loose], [stray, in_scratch, loose_temp])
        self.assertEqual({(entry.maker, entry.outcome) for entry in loose}, {("", folder_audit.LISTED)})
        unread = folder_audit.audit(self.rust, [], None, time.time(), files=[])
        self.assertEqual([entry.path for entry in unread], [in_scratch])

    def test_idle_time_counts_the_newest_mtime_inside_and_git_times_never_ctime(self) -> None:
        repo = folder(self.rust / "hana_p34d_alone")
        deep = repo / "a" / "b"
        deep.mkdir(parents=True)
        _ = (deep / "new.rs").write_text("x")
        self.assertGreater(changed(repo), time.time() - 60)
        age(repo, 6)
        git(repo, "init", "-q")
        age(repo / "a", 6)
        stamp(repo, time.time() - 6 * DAY)
        self.assertGreater(changed(repo), time.time() - 60)  # git init wrote HEAD and the index folder
        too_deep = folder(self.rust / "deep", 6)
        nested = too_deep / "1" / "2" / "3" / "4"
        nested.mkdir(parents=True)
        _ = (nested / "f").write_text("x")
        age(too_deep, 6)
        stamp(nested / "f", time.time())
        self.assertLess(changed(too_deep), time.time() - 5 * DAY)
        target = folder(self.rust / "built", 6)
        (target / "target").mkdir()
        _ = (target / "target" / "out").write_text("x")
        age(target, 6)
        stamp(target / "target" / "out", time.time())
        self.assertLess(changed(target), time.time() - 5 * DAY)

    def test_an_idle_loose_folder_goes_once_armed_unless_a_guard_keeps_it(self) -> None:
        plain = folder(self.rust / "plain", 8)
        fresh = folder(self.rust / "fresh", 3)
        dirty = folder(self.rust / "dirty", 0)
        git(dirty, "init", "-q")
        age(dirty, 8)
        held = folder(self.rust / "held", 8)
        (held / "sub").mkdir()
        age(held, 8)
        _ = scratch.hold(held / "sub", "until:2099-01-01T0000Z", "test", time.time())
        used = folder(self.rust / "used", 8)
        proc = self.root / "proc"
        (proc / "12").mkdir(parents=True)
        (proc / "12" / "cwd").symlink_to(used)
        listening = folder(self.rust / "listening", 8)
        server = socket.socket(socket.AF_UNIX)
        self.addCleanup(server.close)
        server.bind(str(listening / "s"))
        stamp(listening / "s", time.time() - 8 * DAY)
        age(listening, 8)
        table = self.root / "unix"
        _ = table.write_text(f"Num RefCount Protocol Flags Type St Inode Path\n0: 2 0 10000 1 1 7 {listening / 's'}\n")
        expected = frozenset[str]()

        def run(removing: bool) -> dict[str, str]:
            found = folder_audit.audit(self.rust, [], expected, time.time(), files=[], removing=removing,
                                       proc=str(proc), sockets=str(table))
            return {entry.path.name: entry.outcome for entry in found}

        dry = run(False)
        self.assertEqual(dry["plain"], folder_audit.WOULD_REMOVE)
        self.assertEqual(dry["fresh"], folder_audit.LISTED)
        self.assertEqual(dry["dirty"], tmp_sweep.UNSAVED_WORK)
        self.assertEqual(dry["held"], folder_audit.HELD)
        self.assertEqual(dry["used"], "pid 12 works in it")
        self.assertEqual(dry["listening"], folder_audit.LIVE_SOCKET)
        self.assertTrue(plain.is_dir())
        self.assertEqual(folder_audit.main(["arm"]), 0)
        self.assertTrue(folder_audit.armed())
        armed = run(folder_audit.armed())
        self.assertEqual(armed["plain"], folder_audit.REMOVED)
        self.assertFalse(plain.exists())
        for kept in (fresh, dirty, held, used, listening):
            self.assertTrue(kept.is_dir(), kept)
        log = (self.root / "state" / folder_audit.REMOVED_LOG).read_text().splitlines()
        self.assertEqual(len(log), 1)
        self.assertEqual(folder_audit.main(["disarm"]), 0)
        self.assertFalse(folder_audit.armed())

    def test_rerun_prunes_source_after_stop_following_delete(self) -> None:
        source = self.root / "source"
        source.mkdir()
        git(source, "init", "-q")
        git(source, "commit", "-q", "--allow-empty", "-m", "c")
        worktree = self.rust / "stopped-worktree"
        git(source, "worktree", "add", "-q", "--detach", str(worktree), "HEAD")
        delete = tmp_sweep.delete_tree
        def stop(path: str) -> None:
            delete(path)
            raise RuntimeError("stopped before prune")
        with unittest.mock.patch.object(tmp_sweep, "delete_tree", side_effect=stop):
            with self.assertRaisesRegex(RuntimeError, "stopped before prune"):
                _ = folder_audit.remove(worktree, 0, 1000)
        self.assertFalse(worktree.exists())
        proc = self.root / "proc"
        proc.mkdir()
        _ = folder_audit.audit(self.rust, [], frozenset[str](), 1001, files=[], removing=True,
                               proc=str(proc), sockets=str(self.root / "unix"))
        listed = subprocess.run(["git", "-C", str(source), "worktree", "list"],
                                capture_output=True, text=True, check=True).stdout
        self.assertNotIn(str(worktree), listed)
        self.assertFalse(list(folder_audit.state_dir().glob("*.prune")))

    def test_missing_source_finishes_removal_and_clears_prune_record(self) -> None:
        candidate = folder(self.rust / "missing-source-worktree")
        source = self.root / "gone.git"
        with unittest.mock.patch.object(folder_audit, "worktree_sources", return_value=[source]):
            result = folder_audit.remove(candidate, 0, 1000)
        self.assertEqual(result, folder_audit.REMOVED)
        self.assertFalse(candidate.exists())
        self.assertFalse(list(folder_audit.state_dir().glob("*.prune")))
        self.assertIn(str(candidate), (folder_audit.state_dir() / folder_audit.REMOVED_LOG).read_text())

    def test_failed_existing_source_keeps_record_until_source_is_gone(self) -> None:
        candidate = folder(self.rust / "failed-prune-worktree")
        source = self.root / "invalid.git"
        source.mkdir()
        with unittest.mock.patch.object(folder_audit, "worktree_sources", return_value=[source]):
            result = folder_audit.remove(candidate, 0, 1000)
        self.assertTrue(result.startswith("removal failed:"))
        self.assertFalse(candidate.exists())
        records = list(folder_audit.state_dir().glob("*.prune"))
        self.assertEqual(len(records), 1)
        before = records[0].read_bytes()
        folder_audit.resume_pending_prunes()
        self.assertEqual(records[0].read_bytes(), before)
        source.rmdir()
        folder_audit.resume_pending_prunes()
        self.assertFalse(records[0].exists())

    def test_a_removed_linked_worktree_is_pruned_from_its_source(self) -> None:
        source = self.root / "source"
        source.mkdir()
        git(source, "init", "-q")
        git(source, "commit", "-q", "--allow-empty", "-m", "c")
        git(source, "remote", "add", "origin", str(source))
        git(source, "fetch", "-q", "origin")
        worktree = self.rust / "hana_p34d_alone"
        git(source, "worktree", "add", "-q", "--detach", str(worktree), "HEAD")
        age(worktree, 8)
        for name in folder_audit.GIT_TIME_FILES:
            target = source / ".git" / "worktrees" / "hana_p34d_alone" / name
            if target.exists():
                stamp(target, time.time() - 8 * DAY)
        proc = self.root / "proc"
        proc.mkdir()
        found = folder_audit.audit(self.rust, [], frozenset[str](), time.time(), files=[], removing=True,
                                   proc=str(proc), sockets=str(self.root / "unix"))
        self.assertEqual([(entry.path, entry.outcome) for entry in found], [(worktree, folder_audit.REMOVED)])
        listed = subprocess.run(["git", "-C", str(source), "worktree", "list"], capture_output=True, text=True,
                                check=True).stdout
        self.assertNotIn(str(worktree), listed)

    def test_the_earliest_started_session_that_names_a_folder_is_its_maker(self) -> None:
        stray = folder(self.rust / "stray")
        other = folder(self.rust / "other")
        prefix = folder(self.rust / "stra")
        maker = self.root / "p" / "aaaa.jsonl"
        talker = self.root / "p" / "bbbb.jsonl"
        untimed = self.root / "p" / "cccc.jsonl"
        maker.parent.mkdir()
        _ = maker.write_text(f'{{"timestamp":"2026-10-20T10:00:00.000Z","input":{{"command":"mkdir -p {stray} && echo \\"x\\""}}}}\n')
        _ = talker.write_text(f'{{"timestamp":"2026-10-25T10:00:00.000Z","input":{{"command":"ls {stray} {other}/a"}}}}\n')
        _ = untimed.write_text(f'{{"toolUseResult":{{"stdout":"{other}"}},"input":{{"command":"cd {other}"}}}}\n')
        lister = self.root / "p" / "dddd.jsonl"
        _ = lister.write_text(f'{{"timestamp":"2026-10-01T10:00:00.000Z","toolUseResult":{{"stdout":"{stray}"}}}}\n')
        os.utime(untimed, (time.time(), time.time()))
        named = folder_audit.makers([stray, other, prefix, self.rust / "unnamed"], [maker, talker, untimed, lister])
        self.assertEqual(named, {stray: "aaaa", other: "bbbb"})

    def test_the_message_lists_each_folder_and_names_scratch(self) -> None:
        loose = [folder_audit.Loose(Path("/home/u/rust/a"), 0.0, "s"),
                 folder_audit.Loose(Path("/tmp/b"), 0.0, "s", folder_audit.REMOVED)]
        text = folder_audit.message(loose)
        self.assertIn("/home/u/rust/a (idle less than 7 days); /tmp/b (removed)", text)
        self.assertIn("scratch.py adopt", text)


if __name__ == "__main__":
    _ = unittest.main()
