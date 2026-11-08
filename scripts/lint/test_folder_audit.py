#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Tests for folder_audit.py on a fabricated ~/rust, temp folder and transcript set. A test moves
each folder's time back two days unless it wants the folder recent; a folder holds one file, since
the tmp sweep owns empty temp folders."""

from __future__ import annotations

import os
import io
import json
import fcntl
import socket
import subprocess
import sys
import time
import unittest
from collections.abc import Generator, Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import cast, override

import unittest.mock

from scripts.copies import copies
from scripts.lib.socket_test_support import short_socket_directory
from scripts.lint import tmp_sweep, folder_audit, sweep
from scripts.lib import build_folders, cargo_output, conf, copy_record, scratch
from scripts.lint.memory_test_support import config_environment, write_config_fixture
from scripts.message import session_send

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
                                                                **config_environment(self.root / "config")}))
        _ = write_config_fixture(self.root / "config/hana_instances.conf", "clone_max_hours=2.5\n")
        _ = write_config_fixture(self.root / "config/mac_test.conf", "free_floor_gib=8\n")

    def cargo_checkout(self) -> tuple[Path, Path]:
        checkout = self.root / "cargo-checkout"
        checkout.mkdir()
        _ = (checkout / "Cargo.toml").write_text("private fixture")
        cache = self.root / "external-intermediates"
        cache.mkdir()
        _ = (cache / "output").write_text("build output")
        roots = cargo_output.CargoOutputRoots(checkout, checkout / "target", cache)
        _ = self.enterContext(unittest.mock.patch.object(cargo_output, "read_cargo_output_roots", return_value=roots))
        _ = self.enterContext(unittest.mock.patch.object(scratch, "cargo_checkout_peers", return_value=()))
        _ = self.enterContext(unittest.mock.patch.object(folder_audit, "process_uses", return_value=tmp_sweep.ProcessUsesKnown([])))
        self.enterContext(unittest.mock.patch.dict(os.environ, {"LINT_SWEEP_STATE_DIR": str(self.root / "cargo-state")}))
        index = sweep.cargo_output_cache_path()
        index.parent.mkdir()
        _ = index.write_text(json.dumps([{"workspace_root": str(checkout), "target_directory": str(checkout / "target"), "build_directory": str(cache)}]))
        _ = scratch.save_cargo_outputs(checkout)
        return checkout, cache

    def test_external_cache_cleanup_uses_the_index_and_keeps_shared_outputs(self) -> None:
        checkout, cache = self.cargo_checkout()
        index = sweep.cargo_output_cache_path()
        for other_root in (cache, cache.parent):
            with self.subTest(other_root=other_root):
                _ = index.write_text(json.dumps([{"workspace_root": str(self.root / "unrelated-checkout"),
                    "target_directory": str(other_root), "build_directory": str(self.root / "other-cache")}]))
                folder_audit.clean_checkout_cargo_output(checkout)
                self.assertTrue((cache / "output").exists())
        own = {"workspace_root": str(checkout), "target_directory": str(checkout / "target"), "build_directory": str(cache)}
        history = {"workspace_root": str(self.root / "historical-checkout"),
                   "target_directory": str(self.root / "historical-target"), "build_directory": str(cache)}
        _ = index.write_text(json.dumps({"workspaces": [own], "history": [history]}))
        folder_audit.clean_checkout_cargo_output(checkout)
        self.assertTrue(cache.exists())
        _ = index.write_text(json.dumps({"workspaces": [own], "history": []}))
        folder_audit.clean_checkout_cargo_output(checkout)
        self.assertFalse(cache.exists())
        self.assertTrue(checkout.exists())
        folder_audit.clean_checkout_cargo_output(checkout)

    def test_external_cache_cleanup_keeps_unreadable_index_and_live_or_locked_output(self) -> None:
        checkout, cache = self.cargo_checkout()
        index = sweep.cargo_output_cache_path()
        for contents in ("[]", "broken", "{}", '[{"workspace_root":"/missing"}]'):
            with self.subTest(contents=contents):
                _ = index.write_text(contents)
                with self.assertRaisesRegex(OSError, "index is empty or unreadable"):
                    folder_audit.clean_checkout_cargo_output(checkout)
                self.assertTrue(cache.exists())
        index.unlink()
        with self.assertRaisesRegex(OSError, "index is empty or unreadable"):
            folder_audit.clean_checkout_cargo_output(checkout)
        self.assertTrue(cache.exists())
        _ = index.write_text(json.dumps([{"workspace_root": str(checkout), "target_directory": str(checkout / "target"), "build_directory": str(cache)}]))
        for uses in (tmp_sweep.ProcessUsesKnown([(str(cache / "output"), 123456, "fd")]),
                     tmp_sweep.ProcessUsesUnavailable()):
            with self.subTest(uses=uses), unittest.mock.patch.object(folder_audit, "process_uses", return_value=uses):
                with self.assertRaisesRegex(OSError, "busy or unreadable"):
                    folder_audit.clean_checkout_cargo_output(checkout)
                self.assertTrue(cache.exists())
        with (cache / ".cargo-lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(OSError, "locked"):
                folder_audit.clean_checkout_cargo_output(checkout)
        def new_use(paths: list[Path]) -> tmp_sweep.ProcessUsesKnown:
            self.assertEqual(paths, [cache])
            _ = (cache / scratch.USE_STAMP).write_text("new use")
            stamp(cache / scratch.USE_STAMP, time.time() + 1)
            return tmp_sweep.ProcessUsesKnown([])
        with unittest.mock.patch.object(folder_audit, "process_uses", side_effect=new_use):
            with self.assertRaisesRegex(OSError, "used after"):
                folder_audit.clean_checkout_cargo_output(checkout)
        (cache / scratch.USE_STAMP).unlink()
        folder_audit.clean_checkout_cargo_output(checkout)
        self.assertFalse(cache.exists())

    def test_unreadable_index_keeps_the_source_and_mark_until_cleanup_can_finish(self) -> None:
        checkout, cache = self.cargo_checkout()
        index = sweep.cargo_output_cache_path()
        valid_index = index.read_text()
        _ = index.write_text("broken")
        error = io.StringIO()
        with redirect_stderr(error):
            self.assertEqual(folder_audit.main(["--clean-cargo-output", str(checkout)]), 1)
        self.assertEqual(len(error.getvalue().splitlines()), 1)
        def clean(path: Path, saved: tuple[scratch.SavedCargoOutput, ...]) -> None:
            self.assertEqual(saved[0].roots.build_directory, cache)
            folder_audit.clean_checkout_cargo_output(path)
        with unittest.mock.patch.object(scratch, "clean_cargo_outputs", side_effect=clean):
            with self.assertRaisesRegex(OSError, "index is empty or unreadable"):
                scratch.remove_with_cargo_output(checkout, scratch.remove)
            self.assertTrue(checkout.exists())
            self.assertTrue(cache.exists())
            self.assertTrue(scratch.cargo_removal_mark(checkout).exists())
            _ = index.write_text(valid_index)
            scratch.remove_with_cargo_output(checkout, scratch.remove)
        self.assertFalse(checkout.exists())
        self.assertFalse(cache.exists())
        self.assertFalse(scratch.cargo_removal_mark(checkout).exists())

    def test_shared_symlink_output_survives_a_different_cargo_target_setting(self) -> None:
        checkout, cache = self.cargo_checkout()
        (checkout / "target").symlink_to(cache, target_is_directory=True)
        scratch.cargo_removal_mark(checkout).unlink()
        roots = cargo_output.CargoOutputRoots(checkout, self.root / "inherited-target", cache)
        with unittest.mock.patch.object(cargo_output, "read_cargo_output_roots", return_value=roots):
            saved = scratch.save_cargo_outputs(checkout)
        self.assertEqual(saved[0].shared_output_roots, (cache,))
        sweep.cargo_output_cache_path().unlink()
        with unittest.mock.patch.object(cargo_output, "read_cargo_output_roots", side_effect=AssertionError("saved mapping required")):
            folder_audit.clean_checkout_cargo_output(checkout)
        self.assertTrue((cache / "output").exists())

    def test_a_new_cleanup_attempt_keeps_the_mapping_and_accepts_completed_use(self) -> None:
        checkout, cache = self.cargo_checkout()
        original_mark = scratch.cargo_removal_mark(checkout).read_bytes()
        started = time.time()
        def new_use(paths: list[Path]) -> tmp_sweep.ProcessUsesKnown:
            self.assertEqual(paths, [cache])
            _ = (cache / scratch.USE_STAMP).write_text("new use")
            stamp(cache / scratch.USE_STAMP, started + 10)
            return tmp_sweep.ProcessUsesKnown([])
        with (unittest.mock.patch.object(time, "time", return_value=started),
              unittest.mock.patch.object(folder_audit, "process_uses", side_effect=new_use)):
            with self.assertRaisesRegex(OSError, "used after"):
                folder_audit.clean_checkout_cargo_output(checkout)
        self.assertTrue(cache.exists())
        self.assertEqual(scratch.cargo_removal_mark(checkout).read_bytes(), original_mark)
        with (unittest.mock.patch.object(time, "time", return_value=started + 20),
              unittest.mock.patch.object(cargo_output, "read_cargo_output_roots", side_effect=AssertionError("saved mapping required"))):
            folder_audit.clean_checkout_cargo_output(checkout)
        self.assertFalse(cache.exists())

    def test_external_cleanup_preserves_both_output_roots_inside_another_cache(self) -> None:
        checkout, cache = self.cargo_checkout()
        index = sweep.cargo_output_cache_path()
        own = {"workspace_root": str(checkout), "target_directory": str(checkout / "target"), "build_directory": str(cache)}
        for output_kind in ("target_directory", "build_directory"):
            with self.subTest(output_kind=output_kind):
                artifacts = cache / "artifacts"
                artifacts.mkdir(exist_ok=True)
                _ = (artifacts / "binary").write_text("another workspace")
                peer = {"workspace_root": str(self.root / "other-workspace"),
                        "target_directory": str(self.root / "other-target"),
                        "build_directory": str(self.root / "other-build")}
                peer[output_kind] = str(artifacts)
                _ = index.write_text(json.dumps([own, peer]))
                folder_audit.clean_checkout_cargo_output(checkout)
                self.assertEqual((artifacts / "binary").read_text(), "another workspace")
                self.assertTrue((cache / "output").exists())

    def test_saved_output_mapping_survives_partial_source_removal_and_retries(self) -> None:
        checkout, cache = self.cargo_checkout()
        (checkout / "Cargo.toml").unlink()
        with unittest.mock.patch.object(cargo_output, "read_cargo_output_roots", side_effect=AssertionError("source no longer readable")):
            folder_audit.clean_checkout_cargo_output(checkout)
        self.assertFalse(cache.exists())
        self.assertTrue(scratch.cargo_removal_mark(checkout).exists())
        scratch.remove_with_cargo_output(checkout, lambda path: path.rmdir())
        self.assertFalse(checkout.exists())
        self.assertFalse(scratch.cargo_removal_mark(checkout).exists())

    def clone_folder(self, name: str, hours: float, *, dirty: bool = False) -> Path:
        source = self.root / "clone-source"
        if not source.exists():
            source.mkdir()
            git(source, "init", "-q")
            _ = (source / "asset").write_text("saved source")
            _ = (source / ".gitignore").write_text("target/\n")
            git(source, "add", "asset", ".gitignore")
            git(source, "commit", "-qm", "source")
        checkout = copies.clone(source, f"gate:{name}", name)
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
        _ = write_config_fixture(self.root / "config/hana_instances.conf", "clone_max_hours=1\n")
        self.assertEqual(self.judge_clones([young])[young], folder_audit.REMOVED)
        self.assertFalse(young.exists())

    def test_candidates_keep_the_configured_visible_root_under_rust(self) -> None:
        visible = folder(self.rust / "private-visible")
        loose = folder(self.rust / "loose")
        _ = write_config_fixture(self.root / "config/scratch.conf", f"visible_root={visible}\n")
        with unittest.mock.patch.dict(os.environ):
            del os.environ["SCRATCH_VISIBLE_ROOT"]
            self.assertEqual(folder_audit.candidates(self.rust, [], frozenset()), [loose])

    def test_folder_age_skips_the_names_from_scratch_config(self) -> None:
        _ = write_config_fixture(self.root / "config/scratch.conf", "repo_skip=generated-private\n")
        _ = write_config_fixture(self.root / "config/lint.conf", "folder_remove_after_seconds=604800\n")
        idle = folder(self.temp / "idle/generated-private").parent
        recent = folder(self.temp / "recent/included").parent
        age(idle, 10)
        age(recent, 10)
        stamp(idle / "generated-private/file", time.time())
        stamp(recent / "included/file", time.time())

        outcomes = self.judge_clones([idle, recent], removing=False)

        self.assertEqual(outcomes[idle], folder_audit.WOULD_REMOVE)
        self.assertEqual(outcomes[recent], "idle less than 7 days")

    def test_judge_reads_scan_limits_once_for_multiple_due_folders(self) -> None:
        paths = [folder(self.temp / name, 10) for name in ("first", "second")]
        found = [(path, changed(path)) for path in paths]
        (self.root / "proc").mkdir()
        with unittest.mock.patch("scripts.lint.folder_audit.conf.ConfigRead.whole", side_effect=conf.read("lint").whole) as whole:
            outcomes = folder_audit.judge(found, time.time(), False, proc=str(self.root / "proc"),
                                          sockets=str(self.root / "unix"), mac=False)
        self.assertEqual(outcomes, {path: folder_audit.WOULD_REMOVE for path in paths})
        for key in ("folder_idle_depth", "folder_idle_entry_limit"):
            self.assertEqual(whole.call_args_list.count(unittest.mock.call(key)), 1, key)

    def test_ordinary_folder_removal_age_reads_the_private_config_at_use(self) -> None:
        ordinary = folder(self.temp / "ordinary", 3 / 24)
        config = self.root / "config/lint.conf"
        _ = write_config_fixture(config, "folder_remove_after_seconds=21600\n")
        self.assertEqual(self.judge_clones([ordinary])[ordinary], "idle less than 0.25 days")
        self.assertTrue(ordinary.exists())
        _ = write_config_fixture(config, "folder_remove_after_seconds=7200\n")
        self.assertEqual(self.judge_clones([ordinary])[ordinary], folder_audit.REMOVED)
        self.assertFalse(ordinary.exists())

    def test_state_folder_reads_the_private_config_when_the_environment_has_no_override(self) -> None:
        configured = self.root / "configured-state"
        _ = write_config_fixture(self.root / "config/lint.conf", f"folder_state_dir={configured}\n")
        with unittest.mock.patch.dict(os.environ):
            del os.environ["FOLDER_SWEEP_STATE_DIR"]
            self.assertEqual(folder_audit.state_dir(), configured)
        self.assertEqual(folder_audit.state_dir(), self.root / "state")

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
        _ = write_config_fixture(self.root / "config/mac_test.conf", "free_floor_gib=10\n")
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

    def test_config_failure_keeps_an_active_hold_and_its_clone(self) -> None:
        clone = self.clone_folder("config-held", 3)
        _ = scratch.hold(clone, "gate:keep-config-held", "keep the clone")
        record = scratch.holds_file()
        before = record.read_bytes()
        config = self.root / "config/scratch.conf"
        _ = write_config_fixture(config, "holds_file=\n")
        with unittest.mock.patch.dict(os.environ):
            _ = os.environ.pop(scratch.HOLDS_ENV, None)
            for attempt in range(2):
                with self.subTest(attempt=attempt), self.assertRaises(conf.Unusable):
                    _ = self.judge_clones([clone], mac=True, free_space=0)
                self.assertEqual(record.read_bytes(), before)
                self.assertTrue((clone / "checkout/target/build").exists())
            _ = write_config_fixture(config, f"holds_file={record}\n")
            self.assertEqual(self.judge_clones([clone], mac=True, free_space=0)[clone], folder_audit.HELD)
            self.assertEqual(record.read_bytes(), before)

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

    def test_common_build_names_keep_their_cleanup_classification(self) -> None:
        verify_name = tmp_sweep.verify_temp_name()
        for kind, template in build_folders.FOLDER_PREFIXES.items():
            prefix = template.format(owner="123")
            with self.subTest(kind=kind, prefix=prefix):
                parent = self.temp / kind
                parent.mkdir()
                previous = parent / (prefix + "abc123")
                previous.mkdir()
                _ = (previous / "Cargo.toml").write_text("fixture")
                current = build_folders.create_build_folder(build_folders.UniqueBuildFolder(
                    prefix, build_folders.ExplicitBuildParent(parent)))
                _ = (current / "Cargo.toml").write_text("fixture")
                self.assertEqual(folder_audit.kept_in_temp(current, verify_name), folder_audit.kept_in_temp(previous, verify_name),
                                 f"{prefix} must retain its cleanup reader")

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
        _ = write_config_fixture(self.root / "config/lint.conf", "folder_remove_after_seconds=518400\n")
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
        self.assertEqual({(entry.maker, entry.outcome) for entry in loose}, {("", "idle less than 6 days")})
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
        _ = write_config_fixture(self.root / "config/lint.conf", "folder_remove_after_seconds=518400\n")
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
        self.assertEqual(dry["fresh"], "idle less than 6 days")
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

    def test_the_earliest_session_whose_command_makes_a_folder_is_its_maker(self) -> None:
        stray = folder(self.rust / "stray")
        other = folder(self.rust / "other")
        prefix = folder(self.rust / "stra")
        project = folder(self.rust / "project")
        maker = self.root / "p" / "aaaa.jsonl"
        talker = self.root / "p" / "bbbb.jsonl"
        untimed = self.root / "p" / "cccc.jsonl"
        maker.parent.mkdir()
        _ = maker.write_text(json.dumps({"timestamp": "2026-10-20T10:00:00.000Z", "cwd": str(self.rust),
            "input": {"command": 'mkdir -p stray && echo "x"'}}) + "\n")
        _ = talker.write_text(json.dumps({"timestamp": "2026-10-01T10:00:00.000Z", "cwd": str(self.root),
            "input": {"command": f"ls {stray} {other}/a; cd {other}; du {stray}; rg pattern {other}"}}) + "\n")
        later = self.root / "p" / "eeee.jsonl"
        _ = later.write_text(json.dumps({"timestamp": "2026-10-25T10:00:00.000Z", "cwd": str(self.rust),
            "input": {"command": "mkdir -p stray"}}) + "\n")
        _ = untimed.write_text(json.dumps({"cwd": str(self.rust), "toolUseResult": {"stdout": str(other)},
            "input": {"command": f"cd {other}"}}) + "\n")
        lister = self.root / "p" / "dddd.jsonl"
        _ = lister.write_text(json.dumps({"timestamp": "2026-09-01T10:00:00.000Z", "cwd": str(self.rust),
            "toolUseResult": {"stdout": str(stray)}}) + "\n")
        child_maker = self.root / "p" / "ffff.jsonl"
        _ = child_maker.write_text(json.dumps({"timestamp": "2026-09-15T10:00:00.000Z", "cwd": str(self.rust),
            "input": {"command": f"mkdir -p {stray}/notes {other}/notes"}}) + "\n")
        clone_maker = self.root / "p" / "gggg.jsonl"
        _ = clone_maker.write_text(json.dumps({"timestamp": "2026-10-20T10:00:00.000Z", "cwd": str(self.rust),
            "input": {"command": "git clone https://example.invalid/project.git"}}) + "\n")
        os.utime(untimed, (time.time(), time.time()))
        named = folder_audit.makers([stray, other, prefix, project, self.rust / "unnamed"],
                                    [later, talker, child_maker, clone_maker, untimed, lister, maker])
        self.assertEqual(named, {stray: "aaaa", project: "gggg"})

    def test_a_folder_without_a_creation_command_prints_unknown_and_gets_no_message(self) -> None:
        stray = folder(self.temp / "unproven")
        transcript = self.root / "observer.jsonl"
        _ = transcript.write_text(json.dumps({"timestamp": "2026-10-01T10:00:00.000Z", "cwd": str(self.temp),
            "input": {"command": f"ls {stray}; cd {stray}; du {stray}; rg pattern {stray}"}}) + "\n")
        proc = self.root / "proc"
        proc.mkdir()
        loose = folder_audit.audit(self.rust, [self.temp], frozenset(), time.time(), files=[transcript],
            proc=str(proc), sockets=str(self.root / "unix"), clone_roots=[])
        self.assertEqual([(entry.path, entry.maker) for entry in loose], [(stray, "")])
        output = io.StringIO()
        with (unittest.mock.patch.object(folder_audit, "audit", return_value=loose),
              unittest.mock.patch.object(session_send, "send_argv") as send,
              redirect_stdout(output)):
            self.assertEqual(folder_audit.main(["--send"]), 0)
        self.assertIn(f"loose: {stray} (2.0 d idle) maker unknown:", output.getvalue())
        send.assert_not_called()

    def test_the_codex_daemon_folder_is_not_loose(self) -> None:
        _ = folder(self.temp / "codex-daemon-1000")
        ordinary = folder(self.temp / "ordinary")
        self.assertEqual(folder_audit.candidates(self.rust, [self.temp], frozenset()), [ordinary])

    def test_the_message_lists_each_folder_and_names_scratch(self) -> None:
        loose = [folder_audit.Loose(Path("/home/u/rust/a"), 0.0, "s"),
                 folder_audit.Loose(Path("/tmp/b"), 0.0, "s", folder_audit.REMOVED)]
        text = folder_audit.message(loose)
        self.assertIn("/home/u/rust/a (idle less than 7 days); /tmp/b (removed)", text)
        self.assertIn("scratch.py adopt", text)


if __name__ == "__main__":
    _ = unittest.main()
