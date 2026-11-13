"""Build folder allocation preserves parent, ownership, and cleanup rules."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable, Generator
import os
import fcntl
import json
import signal
import shutil
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from typing import Never, cast, override
from unittest import mock
from contextlib import contextmanager
from typing import ContextManager

from scripts.lib import build_folders as folders, conf, copy_record, scratch, unit_runs
from scripts.lint.memory_test_support import config_environment


def _release_folder(path: Path) -> None:
    release = cast(Callable[[Path], None], getattr(folders, "_release_build_folder"))
    release(path)


class BuildFolderTests(unittest.TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        config = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            **config_environment(config), "TMPDIR": str(self.root), copy_record.ROOT_ENV: str(self.root / "scratch"),
            copy_record.COPIES_ENV: str(self.root / "copies"),
            scratch.HOLDS_ENV: str(self.root / "holds.json"),
            "STAFF_FILE": str(self.root / "staff.json"),
            "CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": "",
        })))

    def command(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(Path(folders.__file__)), *arguments],
                              text=True, capture_output=True, timeout=10)

    def test_mkdir_collision_preserves_the_other_creators_folder(self) -> None:
        spec = folders.BuildFolderSpec(kind="temporary", parent=self.root, suffix_length=1,
                                       repository="repo", owner="unit", purpose="race", suffix="fixed")
        path = folders.build_folder_path(spec)
        mkdir = os.mkdir

        def collision(name: str, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
            mkdir(name, mode, dir_fd=dir_fd)
            if name == path.name:
                _ = (path / "keep").write_text("another creator")
                raise FileExistsError(name)

        with mock.patch.object(os, "mkdir", side_effect=collision):
            with self.assertRaises(FileExistsError):
                _ = folders.create_build_folder(spec)
        self.assertEqual((path / "keep").read_text(), "another creator")
        self.assertTrue(path.with_name(path.name + ".lock").is_file())
        self.assertEqual(json.loads(path.with_name(path.name + ".lease.json").read_text())["purpose"], "race")
        self.assertEqual(folders.build_folder_state(path), "free")

    def test_stable_lease_stays_complete_when_publication_stops(self) -> None:
        request = folders.StableOwnerFolder("lease-owner", folders.ExplicitBuildParent(self.root))
        path = folders.create_build_folder(request)
        self.addCleanup(_release_folder, path)
        lease = path.with_name(path.name + ".lease.json")
        before = lease.read_text()
        for target in ("json.dumps", "os.replace"):
            with self.subTest(target=target):
                with mock.patch(target, side_effect=KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        _ = folders.create_build_folder(request)
                self.assertEqual(lease.read_text(), before)
                self.assertEqual(set(self.root.iterdir()), {path, lease, path.with_name(path.name + ".lock")})
        self.assertTrue(path.is_dir())
        self.assertTrue(path.with_name(path.name + ".lock").is_file())
        self.assertEqual(lease.read_text(), before)
        self.assertEqual(json.loads(lease.read_text())["pid"], os.getpid())
        self.assertEqual(folders.build_folder_state(path), "held")

    def test_tool_lock_files_do_not_keep_a_temporary_folder(self) -> None:
        with folders.temporary_build_folder(folders.UniqueBuildFolder("rust-project-")) as path:
            _ = (path / "Cargo.lock").write_text("version = 4\n")
            git = path / ".git"
            git.mkdir()
            _ = (git / "index.lock").write_text("tool lock")
            _ = (git / "orphan.lease.json").write_text("unpaired record")
        self.assertFalse(path.exists())
        self.assertFalse(path.with_name(path.name + ".lock").exists())
        self.assertFalse(path.with_name(path.name + ".lease.json").exists())

    def test_outer_cleanup_removes_a_free_nested_record(self) -> None:
        with folders.temporary_build_folder(folders.UniqueBuildFolder("outer-free-")) as outer:
            inner = folders.create_build_folder(folders.UniqueBuildFolder(
                "inner-free-", folders.ExplicitBuildParent(outer)))
            _ = (inner / "output").write_text("temporary output")
            _release_folder(inner)
            self.assertEqual(folders.build_folder_state(inner), "free")
            self.assertTrue(inner.with_name(inner.name + ".lock").is_file())
            self.assertTrue(inner.with_name(inner.name + ".lease.json").is_file())
        for path in (outer, inner):
            self.assertFalse(path.exists())
            self.assertFalse(path.with_name(path.name + ".lock").exists())
            self.assertFalse(path.with_name(path.name + ".lease.json").exists())

    def test_cancelled_collision_keeps_another_requests_reused_descriptor(self) -> None:
        registry: dict[int, Path] = {}
        collision = self.root / "collision"
        collision.mkdir()
        closed: list[int] = []
        open_lock = cast(Callable[..., int], getattr(folders, "_open_lock"))

        def remember(path: Path, directory: int, *, create: bool) -> int:
            descriptor = open_lock(path, directory, create=create)
            closed.append(descriptor)
            return descriptor

        with mock.patch.object(folders, "_locks", registry):
            other = folders.create_build_folder(folders.StableOwnerFolder(
                "other", folders.ExplicitBuildParent(self.root)))
            original = folders.build_folder_lock_fd(other)

            def cancel_after_reuse(spec: folders.BuildFolderSpec) -> Path:
                del spec
                if not closed:
                    return collision
                reused = os.dup2(original, closed[0])
                self.assertEqual(reused, closed[0])
                registry[reused] = other
                _ = registry.pop(original)
                os.close(original)
                raise KeyboardInterrupt()

            try:
                with mock.patch.object(folders, "_open_lock", side_effect=remember), \
                        mock.patch.object(folders, "build_folder_path", side_effect=cancel_after_reuse):
                    with self.assertRaises(KeyboardInterrupt):
                        _ = folders.create_build_folder(folders.UniqueBuildFolder("collision-"))
                self.assertEqual(registry.get(closed[0]), other)
                self.assertEqual(folders.build_folder_lock_fd(other), closed[0])
                _ = os.fstat(closed[0])
                self.assertEqual(folders.build_folder_state(other), "held")
                self.assertTrue(collision.is_dir())
            finally:
                _release_folder(other)

    def test_interrupted_collision_close_keeps_another_requests_reused_descriptor(self) -> None:
        registry: dict[int, Path] = {}
        collision = self.root / "close-collision"
        collision.mkdir()
        collided: list[int] = []
        reused: list[int] = []
        open_lock = cast(Callable[..., int], getattr(folders, "_open_lock"))
        close = os.close

        def remember(path: Path, directory: int, *, create: bool) -> int:
            descriptor = open_lock(path, directory, create=create)
            collided.append(descriptor)
            return descriptor

        with mock.patch.object(folders, "_locks", registry):
            other = folders.create_build_folder(folders.StableOwnerFolder(
                "close-other", folders.ExplicitBuildParent(self.root)))
            original = folders.build_folder_lock_fd(other)

            def cancel_close(descriptor: int) -> None:
                close(descriptor)
                if collided and descriptor == collided[0] and not reused:
                    replacement = os.dup2(original, descriptor)
                    self.assertEqual(replacement, descriptor)
                    reused.append(replacement)
                    registry[replacement] = other
                    _ = registry.pop(original)
                    close(original)
                    raise KeyboardInterrupt()

            try:
                with mock.patch.object(folders, "_open_lock", side_effect=remember), \
                        mock.patch.object(folders, "build_folder_path", return_value=collision), \
                        mock.patch.object(os, "close", side_effect=cancel_close):
                    with self.assertRaises(KeyboardInterrupt):
                        _ = folders.create_build_folder(folders.UniqueBuildFolder("close-collision-"))
                self.assertEqual(reused, collided)
                self.assertEqual(registry.get(reused[0]), other)
                self.assertEqual(folders.build_folder_lock_fd(other), reused[0])
                _ = os.fstat(reused[0])
                self.assertEqual(folders.build_folder_state(other), "held")
                self.assertTrue(collision.is_dir())
            finally:
                _release_folder(other)

    def test_release_closes_the_descriptor_when_registry_removal_stops(self) -> None:
        class InterruptedRegistry(dict[int, Path]):
            @override
            def pop(self, key: int, default: object = None) -> Never:
                _ = super().pop(key, default)
                raise KeyboardInterrupt()

        registry = InterruptedRegistry()
        with mock.patch.object(folders, "_locks", registry):
            path = folders.create_build_folder(folders.UniqueBuildFolder("release-stop-"))
            descriptor = folders.build_folder_lock_fd(path)
            try:
                with self.assertRaises(KeyboardInterrupt):
                    _release_folder(path)
                self.assertNotIn(descriptor, registry)
                with self.assertRaises(OSError):
                    _ = os.fstat(descriptor)
                self.assertEqual(folders.build_folder_state(path), "free")
            finally:
                registry.clear()
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def test_nested_context_removes_its_records_before_the_parent_ends(self) -> None:
        with folders.temporary_build_folder(folders.UniqueBuildFolder("outer-")) as outer:
            with folders.temporary_build_folder(folders.UniqueBuildFolder(
                    "inner-", folders.ExplicitBuildParent(outer))) as inner:
                self.assertTrue(inner.is_dir())
            self.assertTrue(outer.is_dir())
            self.assertFalse(inner.exists())
            self.assertFalse(inner.with_name(inner.name + ".lease.json").exists())
            self.assertFalse(inner.with_name(inner.name + ".lock").exists())
        self.assertFalse(outer.exists())

    def test_direct_allocation_releases_the_hold_when_parent_close_stops(self) -> None:
        close = os.close
        descriptors: list[int] = []
        set_inheritable = os.set_inheritable

        def remember(descriptor: int, inheritable: bool) -> None:
            descriptors.append(descriptor)
            set_inheritable(descriptor, inheritable)

        def stop_close(descriptor: int) -> None:
            directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
            close(descriptor)
            if directory:
                raise KeyboardInterrupt()

        request = folders.StableOwnerFolder("parent-close", folders.ExplicitBuildParent(self.root))
        path = self.root / "parent-close"
        try:
            with mock.patch.object(os, "set_inheritable", side_effect=remember), \
                    mock.patch.object(os, "close", side_effect=stop_close):
                with self.assertRaises(KeyboardInterrupt):
                    _ = folders.create_build_folder(request)
            self.assertEqual(len(descriptors), 1)
            with self.assertRaises(StopIteration):
                _ = folders.build_folder_lock_fd(path)
            with self.assertRaises(OSError):
                _ = os.fstat(descriptors[0])
            self.assertEqual(folders.build_folder_state(path), "free")
            self.assertTrue(path.with_name(path.name + ".lease.json").is_file())
        finally:
            _release_folder(path)

    def test_temporary_context_rejects_a_missing_explicit_parent(self) -> None:
        parent = self.root / "missing"
        request = folders.UniqueBuildFolder("explicit-", folders.ExplicitBuildParent(parent))
        for temporary in (False, True):
            with self.subTest(temporary=temporary):
                with self.assertRaises(FileNotFoundError):
                    if temporary:
                        with folders.temporary_build_folder(request):
                            self.fail("the missing parent was accepted")
                    else:
                        _ = folders.create_build_folder(request)
                self.assertFalse(parent.exists())

    def test_query_retries_another_queries_probe(self) -> None:
        path = folders.create_build_folder(folders.StableOwnerFolder("probe", folders.ExplicitBuildParent(self.root)))
        _release_folder(path)
        flock = fcntl.flock
        attempts: list[int] = []

        def overlap(descriptor: int, mode: int) -> None:
            if mode == fcntl.LOCK_EX | fcntl.LOCK_NB:
                attempts.append(descriptor)
                if len(attempts) == 1:
                    raise BlockingIOError()
            flock(descriptor, mode)

        with mock.patch.object(fcntl, "flock", side_effect=overlap):
            self.assertEqual(folders.build_folder_state(path), "free")
        self.assertGreaterEqual(len(attempts), 2)

    @unittest.skipUnless(hasattr(os, "fork"), "fork is unavailable")
    def test_fork_inside_nested_contexts_keeps_each_inherited_lock(self) -> None:
        probe = """import os, pathlib, signal
from scripts.lib import build_folders as f
root = pathlib.Path(os.environ['TMPDIR'])
read, write = os.pipe()
outer_spec = f.BuildFolderSpec(kind='target', parent=root, suffix_length=0, purpose='outer')
inner_spec = f.BuildFolderSpec(kind='target', parent=root, suffix_length=0, purpose='inner')
with f.temporary_build_folder(outer_spec) as outer:
    with f.temporary_build_folder(inner_spec) as inner:
        with f.temporary_build_folder(outer_spec) as shared:
            assert shared == outer
        child = os.fork()
        if child == 0:
            signal.alarm(3)
            assert os.read(read, 1) == b'x'
            assert outer.is_dir() and inner.is_dir()
            assert f.build_folder_state(outer) == f.build_folder_state(inner) == 'held'
    if child == 0:
        assert not inner.exists() and f.build_folder_state(inner) == 'free'
        assert outer.is_dir() and f.build_folder_state(outer) == 'held'
if child == 0:
    assert not outer.exists() and f.build_folder_state(outer) == 'free'
    os._exit(0)
assert outer.is_dir() and inner.is_dir()
assert f.build_folder_state(outer) == f.build_folder_state(inner) == 'held'
os.write(write, b'x')
_, status = os.waitpid(child, 0)
assert os.waitstatus_to_exitcode(status) == 0, status
assert not outer.exists() and not inner.exists()
assert f.build_folder_state(outer) == f.build_folder_state(inner) == 'free'
print('nested')
"""
        result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=6)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "nested\n")

    def test_allocation_interrupts_check_the_folder_lock_and_lease(self) -> None:
        probe = """import errno, fcntl, json, os, pathlib, shutil, signal, stat, sys
from unittest import mock
from scripts.lib import build_folders as f
root = pathlib.Path(os.environ['TMPDIR'])
stage = sys.argv[1]
spec = f.BuildFolderSpec(kind='temporary', parent=root, suffix_length=2, repository='repo',
                       owner='unit', purpose=stage, suffix='fixed')
path = f.build_folder_path(spec)
lock = path.with_name(path.name + '.lock')
lease_path = path.with_name(path.name + '.lease.json')
armed = [True]
descriptors = []
def interrupt():
    if armed[0]:
        armed[0] = False
        if stage not in ('removal', 'folder_removed', 'lease_removed', 'lock_removed'):
            assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
        os.kill(os.getpid(), signal.SIGINT)
flock, mkdir, lease, fstat, remove = fcntl.flock, os.mkdir, getattr(f, '_write_lease'), os.fstat, shutil.rmtree
unlink, remove_folder = os.unlink, getattr(f, '_remove_folder')
def lock_step(fd, mode):
    result = flock(fd, mode)
    if mode == fcntl.LOCK_EX | fcntl.LOCK_NB:
        interrupt()
    return result
def mkdir_step(name, mode=0o777, *, dir_fd=None):
    if name == path.name and dir_fd is not None:
        assert lease_path.is_file(), 'the folder has no prior lease'
        result = mkdir(name, mode, dir_fd=dir_fd)
        interrupt()
        return result
    return mkdir(name, mode, dir_fd=dir_fd)
def lease_step(*args):
    lease(*args)
    interrupt()
def fstat_step(fd):
    inode = fstat(fd)
    if stat.S_ISREG(inode.st_mode):
        descriptors.append(fd)
        interrupt()
    return inode
class PublishStep(dict):
    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        interrupt()
def removal_step(name, *, dir_fd, onexc):
    os.unlink(str(path / 'partial'))
    interrupt()
    return remove(name, dir_fd=dir_fd, onexc=onexc)
def folder_removed(*args):
    remove_folder(*args)
    interrupt()
def unlink_step(name, *, dir_fd=None):
    unlink(name, dir_fd=dir_fd)
    if (stage == 'lease_removed' and str(name).endswith('.lease.json')) or (stage == 'lock_removed' and str(name).endswith('.lock')):
        interrupt()
patches = {'lock': mock.patch.object(fcntl, 'flock', side_effect=lock_step),
           'mkdir': mock.patch.object(os, 'mkdir', side_effect=mkdir_step),
           'lease': mock.patch.object(f, '_write_lease', side_effect=lease_step),
           'fstat': mock.patch.object(os, 'fstat', side_effect=fstat_step),
           'publish': mock.patch.object(f, '_locks', PublishStep()),
           'folder_removed': mock.patch.object(f, '_remove_folder', side_effect=folder_removed),
           'lease_removed': mock.patch.object(os, 'unlink', side_effect=unlink_step),
           'lock_removed': mock.patch.object(os, 'unlink', side_effect=unlink_step),
           'removal': mock.patch.object(shutil, 'rmtree', side_effect=removal_step)}
with patches[stage]:
    try:
        if stage in ('removal', 'folder_removed', 'lease_removed', 'lock_removed'):
            with f.temporary_build_folder(spec) as made:
                (made / 'partial').write_text('remove this')
        else:
            f.create_build_folder(spec)
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError('SIGINT did not reach the caller')
    assert not armed[0]
    assert not getattr(f, '_locks'), 'the interrupted caller retained a descriptor'
assert path.exists() == (stage in ('mkdir', 'removal')), (stage, path)
assert lock.exists() == (stage in ('lock', 'fstat', 'mkdir', 'removal', 'folder_removed', 'lease_removed')), (stage, lock)
assert lease_path.exists() == (stage in ('mkdir', 'removal', 'folder_removed')), (stage, lease_path)
if path.exists():
    assert json.loads(lease_path.read_text())['purpose'] == stage
assert f.build_folder_state(path) == 'free'
for descriptor in descriptors:
    try:
        os.fstat(descriptor)
    except OSError as error:
        assert error.errno == errno.EBADF
    else:
        raise AssertionError('the interrupted open leaked a descriptor')
assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
assert f.remove_build_folder(path, lambda: shutil.rmtree(path) if path.exists() else None)
assert not path.exists() and not lock.exists() and not lease_path.exists()
print('free')
"""
        for stage in ("lock", "fstat", "lease", "mkdir", "publish", "removal", "folder_removed", "lease_removed", "lock_removed"):
            with self.subTest(stage=stage):
                result = subprocess.run([sys.executable, "-c", probe, stage], capture_output=True, text=True, timeout=4)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "free\n")

    def test_interrupt_before_stable_mkdir_keeps_existing_output(self) -> None:
        path = folders.create_build_folder(folders.StableOwnerFolder("existing", folders.ExplicitBuildParent(self.root)))
        _release_folder(path)
        _ = (path / "keep").write_text("existing output")
        with mock.patch("scripts.lib.build_folders._write_lease", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                _ = folders.create_build_folder(folders.StableOwnerFolder(path.name, folders.ExplicitBuildParent(self.root)))
        self.assertEqual((path / "keep").read_text(), "existing output")
        self.assertTrue(path.with_name(path.name + ".lock").is_file())
        self.assertTrue(path.with_name(path.name + ".lease.json").is_file())
        self.assertEqual(folders.build_folder_state(path), "free")

    @unittest.skipUnless(hasattr(os, "fork"), "fork is unavailable")
    def test_fork_during_allocation_inherits_the_kernel_lock(self) -> None:
        probe = """import fcntl, os, pathlib, signal
from unittest import mock
from scripts.lib import build_folders as f
root = pathlib.Path(os.environ['TMPDIR'])
path = root / 'fork-lock'
read, write = os.pipe()
flock = fcntl.flock
child = []
def fork_after_lock(fd, mode):
    result = flock(fd, mode)
    if not child and mode == fcntl.LOCK_SH | fcntl.LOCK_NB:
        pid = os.fork()
        if pid == 0:
            signal.alarm(3)
            assert os.read(read, 1) == b'x'
            assert path.is_dir() and f.build_folder_state(path) == 'held'
            os.close(fd)
            assert path.is_dir() and f.build_folder_state(path) == 'free'
            os._exit(0)
        child.append(pid)
    return result
with mock.patch.object(fcntl, 'flock', side_effect=fork_after_lock):
    assert f.create_build_folder(f.StableOwnerFolder(path.name, f.ExplicitBuildParent(root))) == path
getattr(f, '_release_build_folder')(path)
assert path.is_dir() and f.build_folder_state(path) == 'held'
os.write(write, b'x')
_, status = os.waitpid(child[0], 0)
assert os.waitstatus_to_exitcode(status) == 0, status
assert path.is_dir() and f.build_folder_state(path) == 'free'
print('inherited')
"""
        result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=6)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "inherited\n")

    @unittest.skipUnless(hasattr(os, "fork"), "fork is unavailable")
    def test_fork_while_the_main_thread_allocates_keeps_the_callers_signal_handlers(self) -> None:
        probe = """import fcntl, os, pathlib, signal, threading
from unittest import mock
from scripts.lib import build_folders as f
root = pathlib.Path(os.environ['TMPDIR'])
path = root / 'main-allocation'
reached, resume = threading.Event(), threading.Event()
read, write = os.pipe()
descriptors, status = [], []
flock, mkdir = fcntl.flock, os.mkdir
def record_lock(fd, mode):
    result = flock(fd, mode)
    if mode == fcntl.LOCK_SH | fcntl.LOCK_NB:
        descriptors.append(fd)
    return result
def pause_mkdir(name, mode=0o777, *, dir_fd=None):
    result = mkdir(name, mode, dir_fd=dir_fd)
    if name == path.name and dir_fd is not None:
        reached.set()
        assert resume.wait(4)
    return result
def fork_from_worker():
    assert reached.wait(2)
    child = os.fork()
    if child == 0:
        signal.alarm(3)
        assert signal.getsignal(signal.SIGINT) == signal.default_int_handler
        try:
            os.kill(os.getpid(), signal.SIGINT)
        except KeyboardInterrupt:
            pass
        else:
            os._exit(2)
        assert path.is_dir() and f.build_folder_state(path) == 'held'
        os.close(descriptors[0])
        assert os.read(read, 1) == b'x'
        assert path.is_dir() and f.build_folder_state(path) == 'free'
        os._exit(0)
    resume.set()
    _, code = os.waitpid(child, 0)
    status.append(os.waitstatus_to_exitcode(code))
worker = threading.Thread(target=fork_from_worker)
with mock.patch.object(fcntl, 'flock', side_effect=record_lock), mock.patch.object(os, 'mkdir', side_effect=pause_mkdir):
    worker.start()
    assert f.create_build_folder(f.StableOwnerFolder(path.name, f.ExplicitBuildParent(root))) == path
getattr(f, '_release_build_folder')(path)
os.write(write, b'x')
worker.join(4)
assert not worker.is_alive() and status == [0], status
assert path.is_dir() and f.build_folder_state(path) == 'free'
print('signals')
"""
        result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=6)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "signals\n")

    def test_maker_and_cleanup_retry_a_query_that_still_holds_its_lock(self) -> None:
        for operation in ("maker", "cleanup"):
            with self.subTest(operation=operation):
                path = self.root / operation
                path.with_name(path.name + ".lock").touch()
                if operation == "cleanup":
                    path.mkdir()
                    _ = path.with_name(path.name + ".lease.json").write_text("record")
                reached, release = threading.Event(), threading.Event()
                flock = fcntl.flock

                def pause_probe(descriptor: int, mode: int) -> None:
                    try:
                        flock(descriptor, mode)
                    except BlockingIOError:
                        release.set()
                        raise
                    if threading.current_thread().name.startswith("probe") and mode == fcntl.LOCK_EX | fcntl.LOCK_NB:
                        reached.set()
                        if not release.wait(3):
                            raise AssertionError("the maker did not meet the query")

                with ThreadPoolExecutor(max_workers=1, thread_name_prefix="probe") as workers:
                    with mock.patch("fcntl.flock", side_effect=pause_probe):
                        query = workers.submit(folders.build_folder_state, path)
                        try:
                            self.assertTrue(reached.wait(3))
                            if operation == "maker":
                                made = folders.create_build_folder(folders.StableOwnerFolder(path.name, folders.ExplicitBuildParent(self.root)))
                                self.assertEqual(made, path)
                                _release_folder(made)
                            else:
                                self.assertTrue(folders.remove_build_folder(path, lambda: shutil.rmtree(path)))
                                self.assertFalse(path.exists())
                                self.assertFalse(path.with_name(path.name + ".lock").exists())
                                self.assertFalse(path.with_name(path.name + ".lease.json").exists())
                        finally:
                            release.set()
                        self.assertEqual(query.result(timeout=3), "free")

    def test_hard_linked_lock_and_lease_keep_the_unrelated_file_intact(self) -> None:
        for suffix in (".lock", ".lease.json"):
            with self.subTest(suffix=suffix):
                parent = self.root / suffix.removeprefix(".")
                parent.mkdir()
                outside = parent / "unrelated"
                _ = outside.write_text("keep this file")
                os.link(outside, parent / ("owner" + suffix))
                with self.assertRaises(ValueError):
                    try:
                        _ = folders.create_build_folder(folders.StableOwnerFolder("owner", folders.ExplicitBuildParent(parent)))
                    finally:
                        self.assertEqual(outside.read_text(), "keep this file")

    def test_lease_failure_removes_only_a_new_allocation_and_releases_its_lock(self) -> None:
        for existing in (False, True):
            with self.subTest(existing=existing):
                path = self.root / ("existing" if existing else "new")
                if existing:
                    path.mkdir()
                    _ = (path / "keep").write_text("existing output")
                with mock.patch("scripts.lib.build_folders._write_lease", side_effect=OSError("lease failure")):
                    with self.assertRaisesRegex(OSError, "lease failure"):
                        _ = folders.create_build_folder(folders.StableOwnerFolder(path.name, folders.ExplicitBuildParent(self.root)))
                self.assertEqual(path.exists(), existing)
                self.assertEqual(folders.build_folder_state(path), "free")
                if existing:
                    self.assertEqual((path / "keep").read_text(), "existing output")

    def test_context_uses_one_parent_descriptor_when_the_parent_path_changes(self) -> None:
        parent = self.root / "parent"
        parent.mkdir()
        moved = self.root / "moved"
        opens: list[Path] = []
        original = cast(Callable[..., ContextManager[int]], getattr(folders, "_folder_parent"))

        @contextmanager
        def replace_parent(path: Path, root: Path, *, create: bool) -> Generator[int]:
            with original(path, root, create=create) as descriptor:
                opens.append(root)
                if root == parent:
                    _ = parent.rename(moved)
                    parent.mkdir()
                    _ = (parent / "keep").write_text("replacement")
                yield descriptor

        with mock.patch("scripts.lib.build_folders._folder_parent", side_effect=replace_parent):
            with folders.temporary_build_folder(folders.UniqueBuildFolder("proof-", folders.ExplicitBuildParent(parent))) as path:
                self.assertTrue((moved / path.name).is_dir())
                self.assertFalse(path.exists())
        self.assertEqual(opens, [parent])
        self.assertEqual(list(moved.iterdir()), [])
        self.assertEqual([entry.name for entry in parent.iterdir()], ["keep"])

    def test_unique_context_retries_held_and_free_collisions_without_deleting_output(self) -> None:
        for held in (False, True):
            with self.subTest(held=held):
                parent = self.root / str(held)
                parent.mkdir()
                existing = parent / "proof-aa"
                existing.mkdir()
                _ = (existing / "keep").write_text("existing output")
                lease = existing.with_name(existing.name + ".lease.json")
                _ = lease.write_text("existing lease")
                lock = existing.with_name(existing.name + ".lock")
                with lock.open("w") as descriptor:
                    if held:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    inode = lock.stat().st_ino
                    spec = folders.BuildFolderSpec(kind="gate", parent=parent, suffix_length=2, purpose="proof")
                    with mock.patch("secrets.choice", side_effect=list("aabb")):
                        with folders.temporary_build_folder(spec) as made:
                            self.assertEqual(made, parent / "proof-bb")
                    self.assertEqual(lock.stat().st_ino, inode)
                    self.assertEqual(lease.read_text(), "existing lease")
                    self.assertEqual((existing / "keep").read_text(), "existing output")
                self.assertFalse((parent / "proof-bb").exists())

    def test_explicit_work_with_zero_suffix_length_works_in_the_context(self) -> None:
        spec = folders.BuildFolderSpec(kind="temporary", parent=self.root, suffix_length=0,
                                       showrunner="builds", unit="queue", work="fixed", purpose="proof")
        direct = folders.create_build_folder(spec)
        _release_folder(direct)
        self.assertTrue(folders.remove_build_folder(direct, lambda: shutil.rmtree(direct)))
        with folders.temporary_build_folder(spec) as path:
            self.assertEqual(path, direct)
            self.assertEqual(folders.build_folder_state(path), "held")
        self.assertFalse(path.exists())
        self.assertFalse(path.with_name(path.name + ".lock").exists())
        self.assertFalse(path.with_name(path.name + ".lease.json").exists())

    def test_explicit_names_and_attribution_do_not_use_session_names(self) -> None:
        spec = folders.BuildFolderSpec(kind="temporary", parent=self.root, suffix_length=8,
                                       showrunner="builds", unit="queue", work="bf1", purpose="proof")
        with folders.temporary_build_folder(spec) as path:
            self.assertEqual(path, self.root / "builds-queue" / "bf1-proof")
            self.assertEqual(folders.build_folder_state(path), "held")
            lease = cast(dict[str, object], json.loads(path.with_name(path.name + ".lease.json").read_text()))
            self.assertEqual({key: lease[key] for key in ("showrunner", "unit", "work", "purpose")},
                             {"showrunner": "builds", "unit": "queue", "work": "bf1", "purpose": "proof"})
            self.assertEqual(lease["pid"], os.getpid())
            self.assertTrue(lease["start_time"])
            descriptor = folders.build_folder_lock_fd(path)
            self.assertTrue(os.get_inheritable(descriptor))
            inode = os.fstat(descriptor).st_ino
            path.rmdir()
            self.assertEqual(os.fstat(descriptor).st_ino, inode)
            self.assertEqual(folders.build_folder_state(path), "held")
        self.assertEqual(folders.build_folder_state(path), "free")

    def test_stable_folder_shares_between_processes_and_excludes_removal(self) -> None:
        path = folders.create_build_folder(folders.StableOwnerFolder("shared", folders.ExplicitBuildParent(self.root)))
        self.addCleanup(_release_folder, path)
        probe = """from pathlib import Path
import sys
from scripts.lib import build_folders as f
path = Path(sys.argv[1])
assert f.create_build_folder(f.StableOwnerFolder(path.name, f.ExplicitBuildParent(path.parent))) == path
assert f.build_folder_state(path) == 'held'
assert not f.remove_build_folder(path, lambda: (_ for _ in ()).throw(AssertionError('cleanup ran')))
print('shared')
"""
        result = subprocess.run([sys.executable, "-c", probe, str(path)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "shared\n")
        self.assertTrue(path.is_dir())

    def test_inherited_descriptor_holds_after_the_parent_releases_it(self) -> None:
        request = folders.UniqueBuildFolder("inherit-", folders.ExplicitBuildParent(self.root))
        path = folders.create_build_folder(request)
        descriptor = folders.build_folder_lock_fd(path)
        child = subprocess.Popen([sys.executable, "-c", "import sys; print('ready', flush=True); sys.stdin.read()"],
                                 pass_fds=(descriptor,), stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            assert child.stdout is not None
            self.assertEqual(child.stdout.readline(), "ready\n")
            _release_folder(path)
            self.assertEqual(folders.build_folder_state(path), "held")
            _ = child.communicate(input="end", timeout=10)
            self.assertEqual(child.returncode, 0)
            self.assertEqual(folders.build_folder_state(path), "free")
        finally:
            _release_folder(path)
            if child.poll() is None:
                child.kill()
            _ = child.communicate(timeout=10)

    def test_process_death_frees_the_lock_without_reading_the_lease(self) -> None:
        probe = """import signal
from scripts.lib import build_folders as f
path = f.create_build_folder(f.UniqueBuildFolder('death-'))
print(path, flush=True)
signal.pause()
"""
        process: subprocess.Popen[str] = subprocess.Popen([sys.executable, "-c", probe], stdout=subprocess.PIPE, text=True)
        try:
            assert process.stdout is not None
            path = Path(cast(str, process.stdout.readline()).strip())
            self.assertEqual(folders.build_folder_state(path), "held")
            _ = path.with_name(path.name + ".lease.json").write_text("invalid attribution")
            process.kill()
            _ = process.communicate(timeout=10)
            self.assertEqual(folders.build_folder_state(path), "free")
            self.assertTrue(path.is_dir())
        finally:
            if process.poll() is None:
                process.kill()
            _ = process.communicate(timeout=10)

    def test_cli_run_passes_the_lock_to_the_command_and_its_child(self) -> None:
        probe = """import fcntl, os, pathlib, subprocess, sys
fd = int(os.environ['BUILD_FOLDER_LOCK_FD'])
assert os.get_inheritable(fd)
path = pathlib.Path.cwd()
assert path.parts[-2:] == ('builds-queue', 'bf1-proof')
subprocess.run([sys.executable, '-c', 'import os,sys; os.fstat(int(sys.argv[1]))', str(fd)], pass_fds=(fd,), check=True)
with path.with_name(path.name + '.lock').open('a') as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(path)
    else:
        raise AssertionError('the command has no lock')
"""
        result = self.command("run", "--showrunner", "builds", "--unit", "queue", "--work", "bf1",
                              "--name", "proof", "--", sys.executable, "-c", probe)
        self.assertEqual(result.returncode, 0, result.stderr)
        path = Path(result.stdout.strip())
        self.assertFalse(path.exists())
        self.assertEqual(folders.build_folder_state(path), "free")

    def test_maker_generates_work_and_records_it_without_parsing_the_path(self) -> None:
        spec = folders.BuildFolderSpec(kind="temporary", parent=self.root, suffix_length=3,
                                       showrunner="builds", unit="queue", purpose="proof")
        with mock.patch("secrets.choice", side_effect=list("abc")), folders.temporary_build_folder(spec) as path:
            self.assertEqual(path, self.root / "builds-queue" / "abc-proof")
            lease = cast(dict[str, object], json.loads(path.with_name(path.name + ".lease.json").read_text()))
            self.assertEqual(lease["work"], "abc")

    def test_temporary_context_keeps_the_folder_while_a_child_holds_the_lock(self) -> None:
        child: subprocess.Popen[str] | None = None
        try:
            with folders.temporary_build_folder(folders.UniqueBuildFolder("child-")) as path:
                child = subprocess.Popen([sys.executable, "-c", "import sys; print('ready', flush=True); sys.stdin.read()"],
                                         pass_fds=(folders.build_folder_lock_fd(path),),
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
                assert child.stdout is not None
                self.assertEqual(child.stdout.readline(), "ready\n")
            self.assertTrue(path.is_dir())
            lock = path.with_name(path.name + ".lock")
            lease = path.with_name(path.name + ".lease.json")
            self.assertTrue(lock.is_file())
            self.assertTrue(lease.is_file())
            self.assertEqual(folders.build_folder_state(path), "held")
            self.assertFalse(folders.remove_build_folder(path, lambda: shutil.rmtree(path)))
            self.assertTrue(path.is_dir() and lock.is_file() and lease.is_file())
            _ = child.communicate(input="end", timeout=10)
            self.assertEqual(folders.build_folder_state(path), "free")
            self.assertTrue(folders.remove_build_folder(path, lambda: shutil.rmtree(path)))
            self.assertFalse(path.exists() or lock.exists() or lease.exists())
        finally:
            if child is not None:
                if child.poll() is None:
                    child.kill()
                _ = child.communicate(timeout=10)

    def test_lock_and_lease_symlinks_cannot_modify_another_file(self) -> None:
        outside = self.root / "unrelated"
        _ = outside.write_text("keep")
        for suffix in (".lock", ".lease.json"):
            with self.subTest(suffix=suffix):
                parent = self.root / suffix.removeprefix(".")
                parent.mkdir()
                (parent / ("owner" + suffix)).symlink_to(outside)
                with self.assertRaises(OSError):
                    _ = folders.create_build_folder(folders.StableOwnerFolder("owner", folders.ExplicitBuildParent(parent)))
                self.assertEqual(outside.read_text(), "keep")

    def test_import_does_not_load_cli_or_owner_modules(self) -> None:
        probe = """import runpy, sys
runpy.run_path(sys.argv[1])
heavy = {'argparse', 'scripts.lib.unit_runs', 'scripts.lib.copy_record',
         'scripts.lib.scratch', 'herdr'}
loaded = sorted(heavy.intersection(sys.modules))
assert not loaded, loaded
"""
        result = subprocess.run([sys.executable, "-I", "-c", probe, str(Path(folders.__file__))],
                                cwd=self.root, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_tmpfs_parent_uses_only_inherited_tmpfs_paths(self) -> None:
        for value, expected in (("/dev/shm/outer/module", Path("/dev/shm/outer/module")),
                                ("/dev/shm-other", Path("/dev/shm"))):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"TMPDIR": value}):
                self.assertEqual(folders.tmpfs_temporary_parent(), expected)

    def test_inherited_parent_changes_between_allocations(self) -> None:
        first = folders.create_build_folder(folders.UniqueBuildFolder("merge-gate-"))
        other = self.root / "inherited parent with spaces"
        other.mkdir()
        with mock.patch.dict(os.environ, {"TMPDIR": str(other)}):
            second = folders.create_build_folder(folders.UniqueBuildFolder("merge-gate-"))
        self.assertEqual(first.parent, self.root)
        self.assertEqual(second.parent, other)
        self.assertTrue(first.is_dir())
        self.assertTrue(second.is_dir())
        self.assertTrue(first.name.startswith("merge-gate-"))
        self.assertTrue(second.name.startswith("merge-gate-"))

    def test_explicit_parent_overrides_tmpdir_and_keeps_prefix_and_permissions(self) -> None:
        parent = self.root / "record parent with spaces"
        parent.mkdir()
        path = folders.create_build_folder(folders.UniqueBuildFolder(
            "bump-gate-", folders.ExplicitBuildParent(parent)))
        self.assertEqual(path.parent, parent)
        self.assertTrue(path.name.startswith("bump-gate-"))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        self.assertFalse((path / copy_record.RECORD).exists())

    def test_concurrent_unique_allocations_never_share_an_output_folder(self) -> None:
        def allocate(index: int) -> Path:
            path = folders.create_build_folder(folders.UniqueBuildFolder("promotion-index-"))
            _ = (path / "owner").write_text(str(index))
            return path

        with ThreadPoolExecutor(max_workers=8) as workers:
            jobs = [workers.submit(allocate, index) for index in range(32)]
            paths = [job.result(timeout=10) for job in jobs]
        self.assertEqual(len(set(paths)), 32)
        for index, path in enumerate(paths):
            self.assertEqual((path / "owner").read_text(), str(index))
            self.assertEqual(path.parent, self.root)

    def test_stable_owner_reuses_contents_and_existing_permissions(self) -> None:
        request = folders.StableOwnerFolder("session-123", folders.ExplicitBuildParent(self.root), mode=0o700)
        first = folders.create_build_folder(request)
        _ = (first / "live.sock.record").write_text("keep")
        first.chmod(0o750)
        with ThreadPoolExecutor(max_workers=4) as workers:
            jobs = [workers.submit(folders.create_build_folder, request) for _ in range(8)]
            paths = [job.result(timeout=10) for job in jobs]
        self.assertEqual(paths, [first] * 8)
        self.assertEqual((first / "live.sock.record").read_text(), "keep")
        self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o750)






    def test_stable_owner_file_collision_keeps_the_existing_file(self) -> None:
        occupied = self.root / "owner"
        _ = occupied.write_text("live owner record")
        with self.assertRaises(FileExistsError):
            _ = folders.create_build_folder(folders.StableOwnerFolder(
                occupied.name, folders.ExplicitBuildParent(self.root)))
        self.assertEqual(occupied.read_text(), "live owner record")

    def test_explicit_parent_refuses_missing_or_unwritable_storage_without_fallback(self) -> None:
        missing = self.root / "missing memory filesystem"
        with self.assertRaises(OSError):
            _ = folders.create_build_folder(folders.UniqueBuildFolder(
                "merge-gate-", folders.ExplicitBuildParent(missing)))
        self.assertFalse(missing.exists())
        self.root.chmod(0o500)
        try:
            with self.assertRaises(PermissionError):
                _ = folders.create_build_folder(folders.UniqueBuildFolder(
                    "merge-gate-", folders.ExplicitBuildParent(self.root)))
        finally:
            self.root.chmod(0o700)
        self.assertEqual(list(self.root.iterdir()), [])

    @unittest.skipUnless(Path("/dev/shm").is_dir() and os.access("/dev/shm", os.W_OK),
                         "the memory filesystem is unavailable")
    def test_explicit_parent_allocates_under_a_private_dev_shm_parent(self) -> None:
        with tempfile.TemporaryDirectory(dir=folders.tmpfs_temporary_parent()) as temporary:
            parent = Path(temporary)
            with folders.temporary_build_folder(folders.UniqueBuildFolder(
                    "merge-gate-", folders.ExplicitBuildParent(parent))) as name:
                path = Path(name)
                self.assertEqual(path.parent, parent)
                self.assertTrue(path.is_dir())
                self.assertEqual(path.stat().st_dev, parent.stat().st_dev)
            self.assertFalse(path.exists())

    def test_context_removes_build_output_after_completion_and_interruption(self) -> None:
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted):
                path = self.root
                try:
                    with folders.temporary_build_folder(folders.UniqueBuildFolder("queue-place-")) as name:
                        path = Path(name)
                        _ = (path / "module.log").write_text("output")
                        self.assertTrue(path.is_dir())
                        if interrupted:
                            raise KeyboardInterrupt("injected build interruption")
                except KeyboardInterrupt:
                    if not interrupted:
                        raise
                self.assertNotEqual(path, self.root)
                self.assertFalse(path.exists())


    def test_cancellation_during_allocation_or_removal_keeps_any_folder_recorded(self) -> None:
        def interrupted(number: int, frame: object) -> None:
            del frame
            raise KeyboardInterrupt(signal.Signals(number).name)

        for stage in ("allocation", "removal"):
            with self.subTest(stage=stage):
                paths: list[Path] = []
                allocate = folders.create_build_folder
                remove = shutil.rmtree
                cancel = threading.Event()
                sent = threading.Event()
                release = threading.Event()

                def send_from_thread() -> None:
                    if cancel.wait(10) and not release.is_set():
                        signal.pthread_kill(threading.get_ident(), signal.SIGTERM)
                        sent.set()
                        _ = release.wait(10)

                worker = threading.Thread(target=send_from_thread)
                worker.start()

                def allocate_then_cancel(request: folders.BuildFolderRequest | folders.BuildFolderSpec) -> Path:
                    path = allocate(request)
                    paths.append(path)
                    if stage == "allocation":
                        os.kill(os.getpid(), signal.SIGTERM)
                    return path

                def cancel_then_remove(path: str, *, dir_fd: int,
                                       onexc: Callable[[Callable[..., object], str, BaseException], object]) -> None:
                    if stage == "removal":
                        cancel.set()
                        self.assertTrue(sent.wait(10), "the worker did not send SIGTERM")
                    remove(path, dir_fd=dir_fd, onexc=onexc)

                previous = signal.signal(signal.SIGTERM, interrupted)
                try:
                    with (mock.patch.object(folders, "create_build_folder", side_effect=allocate_then_cancel),
                          mock.patch.object(shutil, "rmtree", side_effect=cancel_then_remove),
                          self.assertRaisesRegex(KeyboardInterrupt, "SIGTERM")):
                        with folders.temporary_build_folder(folders.UniqueBuildFolder("gate-proof-")):
                            pass
                    self.assertEqual(len(paths), 1)
                    path = paths[0]
                    lock = path.with_name(path.name + ".lock")
                    lease = path.with_name(path.name + ".lease.json")
                    if path.exists():
                        self.assertTrue(lock.is_file())
                        record = cast(dict[str, object], json.loads(lease.read_text()))
                        self.assertEqual(record["pid"], os.getpid())
                        self.assertEqual(record["purpose"], "gate-proof-")
                    self.assertEqual(folders.build_folder_state(path), "free")
                finally:
                    release.set()
                    cancel.set()
                    worker.join(timeout=10)
                    _ = signal.signal(signal.SIGTERM, previous)


    def test_authority_readers_follow_current_scratch_copy_and_unit_roots(self) -> None:
        for index in range(2):
            scratch_root = self.root / f"scratch-{index}"
            copies_root = self.root / f"copies-{index}"
            current_run = self.root / f"unit-{index}"
            former_run = self.root / f"delegate-{index}"
            with (mock.patch.dict(os.environ, {copy_record.ROOT_ENV: str(scratch_root),
                                               copy_record.COPIES_ENV: str(copies_root)}),
                  mock.patch.object(unit_runs, "run_roots", return_value=(current_run, former_run))):
                parents = (folders.ExplicitBuildParent(copy_record.root()),
                           folders.ExplicitBuildParent(copy_record.copies_root()), folders.UnitRunParent())
                for parent, expected in zip(parents, (scratch_root, copies_root, current_run), strict=True):
                    path = folders.create_build_folder(folders.StableOwnerFolder("owner", parent))
                    self.assertEqual(path, expected / "owner")
                    self.assertTrue(path.is_dir())
                self.assertFalse(former_run.exists())

    def test_context_removes_nonempty_read_only_subfolders(self) -> None:
        path = self.root
        try:
            with folders.temporary_build_folder(folders.UniqueBuildFolder("bump-gate-")) as name:
                path = Path(name)
                nested = path / "read-only-copy"
                nested.mkdir()
                _ = (nested / "output").write_text("compiled output")
                nested.chmod(0o500)
                self.assertTrue((nested / "output").is_file())
        finally:
            if path.exists() and path != self.root:
                for nested in path.iterdir():
                    if nested.is_dir():
                        nested.chmod(0o700)
        self.assertNotEqual(path, self.root)
        self.assertFalse(path.exists())

    def test_context_removes_nonempty_mode_zero_folders(self) -> None:
        for restrict_root in (False, True):
            with self.subTest(restrict_root=restrict_root):
                path = self.root
                nested = self.root
                try:
                    with folders.temporary_build_folder(folders.UniqueBuildFolder("bump-gate-")) as path:
                        nested = path / "private-output"
                        nested.mkdir()
                        _ = (nested / "output").write_text("compiled output")
                        nested.chmod(0)
                        if restrict_root:
                            path.chmod(0)
                finally:
                    if path != self.root and path.exists():
                        path.chmod(0o700)
                        if nested.exists():
                            nested.chmod(0o700)
                self.assertNotEqual(path, self.root)
                self.assertFalse(path.exists())

    def test_context_removes_dangling_symlinks_from_read_only_folders(self) -> None:
        path = self.root
        locked = self.root
        try:
            with folders.temporary_build_folder(folders.UniqueBuildFolder("bump-gate-")) as path:
                locked = path / "locked"
                locked.mkdir()
                (locked / "link").symlink_to("missing")
                locked.chmod(0o500)
        finally:
            if path != self.root and path.exists():
                locked.chmod(0o700)
        self.assertNotEqual(path, self.root)
        self.assertFalse(path.exists())

    def test_context_keeps_published_output_when_the_stage_moves(self) -> None:
        destination = self.root / "published shell inputs"
        with folders.temporary_build_folder(folders.UniqueBuildFolder(".shell-stage-")) as name:
            stage = Path(name)
            _ = (stage / "exports.sh").write_text("export FIXTURE=1\n")
            os.replace(stage, destination)
        self.assertFalse(stage.exists())
        self.assertEqual((destination / "exports.sh").read_text(), "export FIXTURE=1\n")

    def test_context_keeps_a_replacement_folder_after_the_stage_moves(self) -> None:
        published = self.root / "published"
        replacement = self.root / "unrelated"
        replacement.mkdir()
        _ = (replacement / "keep").write_text("unrelated output")
        with folders.temporary_build_folder(folders.UniqueBuildFolder("stage-")) as stage:
            _ = (stage / "output").write_text("published output")
            _ = stage.rename(published)
            _ = replacement.rename(stage)
        self.assertEqual((stage / "keep").read_text(), "unrelated output")
        self.assertEqual((published / "output").read_text(), "published output")

    def test_allocation_refuses_a_repository_parent_symlink(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        kinds: tuple[folders.BuildFolderKind, ...] = ("temporary", "standing")
        for kind in kinds:
            with self.subTest(kind=kind):
                parent = self.root / kind
                parent.mkdir()
                (parent / "repo").symlink_to(outside, target_is_directory=True)
                spec = folders.BuildFolderSpec(kind=kind, parent=parent, suffix_length=3,
                                               repository="repo", owner="unit", purpose="proof", suffix="abc")
                with self.assertRaises(OSError):
                    _ = folders.create_build_folder(spec)
                self.assertEqual(list(outside.iterdir()), [])

    def test_context_cleans_its_parent_when_the_parent_path_is_replaced(self) -> None:
        parent = self.root / "parent"
        parent.mkdir()
        moved = self.root / "moved"
        outside = self.root / "outside"
        outside.mkdir()
        request = folders.UniqueBuildFolder("stage-", folders.ExplicitBuildParent(parent))
        with folders.temporary_build_folder(request) as stage:
            _ = parent.rename(moved)
            parent.symlink_to(outside, target_is_directory=True)
            replacement = outside / stage.name
            replacement.mkdir()
            _ = (replacement / "keep").write_text("unrelated output")
        self.assertEqual((replacement / "keep").read_text(), "unrelated output")
        self.assertFalse((moved / stage.name).exists())

    def test_registered_task_uses_scratch_naming_record_and_task_cleanup(self) -> None:
        settings = conf.read("scratch")
        with mock.patch.object(scratch, "target", wraps=scratch.target) as naming:
            path = scratch.new("gate:folder-test", "checkout", False, scratch_settings=settings)
        naming.assert_called_once_with("gate:folder-test", "checkout", False, scratch_settings=settings)
        self.assertEqual(path.parent, copy_record.root())
        self.assertTrue(path.name.startswith("gate-folder-test-checkout-"))
        record = scratch.task_of(path)
        self.assertIsInstance(record, copy_record.Registered)
        assert isinstance(record, copy_record.Registered)
        self.assertEqual(record.task, "gate:folder-test")
        self.assertIsInstance(scratch.lifetime(path), scratch.Live)
        self.assertEqual(scratch.end("gate:folder-test"), [path])
        self.assertFalse(path.exists())

    def test_cli_preserves_spaces_existing_permissions_and_the_one_path_output(self) -> None:
        parent = self.root / "shell parent"
        parent.mkdir()
        unique = self.command("unique", "--prefix", "verify-tree.", "--parent", str(parent))
        self.assertEqual(unique.returncode, 0, unique.stderr)
        path = Path(unique.stdout.strip())
        self.assertEqual(path.parent, parent)
        self.assertTrue(path.is_dir())
        self.assertTrue(path.name.startswith("verify-tree."))
        self.assertEqual(unique.stdout, f"{path}\n")
        owner = parent / "run tmp"
        owner.mkdir(mode=0o700)
        stable = self.command("stable", "--name", "run tmp", "--parent", str(parent))
        self.assertEqual(stable.returncode, 0, stable.stderr)
        self.assertEqual(stable.stdout, f"{owner}\n")
        self.assertEqual(stat.S_IMODE(owner.stat().st_mode), 0o700)

    def test_cli_whole_path_and_run_temp_preserve_existing_output(self) -> None:
        whole = self.root / "inherited parent" / "run tmp"
        made = self.command("stable", "--path", str(whole))
        self.assertEqual(made.returncode, 0, made.stderr)
        self.assertEqual(made.stdout, f"{whole}\n")
        _ = (whole / "output").write_text("keep whole-path output")
        repeated = self.command("stable", "--path", str(whole))
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual((whole / "output").read_text(), "keep whole-path output")
        run = self.root / "unit run"
        named = self.command("run-temp-path", str(run))
        self.assertEqual(named.returncode, 0, named.stderr)
        allocated = self.command("stable", "--run-temp", str(run))
        self.assertEqual(allocated.returncode, 0, allocated.stderr)
        self.assertEqual(allocated.stdout, named.stdout)
        self.assertEqual(Path(allocated.stdout.strip()), unit_runs.run_temp_path(run))
        self.assertTrue(Path(allocated.stdout.strip()).is_dir())

    def test_cli_missing_parent_prints_no_path_and_creates_no_fallback(self) -> None:
        result = self.command("unique", "--prefix", "module-", "--parent", str(self.root / "missing"))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn(str(self.root / "missing"), result.stderr)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_cli_task_uses_the_existing_registry_and_task_end(self) -> None:
        result = subprocess.run([sys.executable, str(Path(scratch.__file__)), "new",
                                 "--task", "gate:shell-folder", "--name", "checkout"],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        path = Path(result.stdout.strip())
        self.assertEqual(path.parent, copy_record.root())
        record = scratch.task_of(path)
        self.assertIsInstance(record, copy_record.Registered)
        assert isinstance(record, copy_record.Registered)
        self.assertEqual(record.task, "gate:shell-folder")
        self.assertEqual(scratch.end("gate:shell-folder"), [path])
        self.assertFalse(path.exists())

    def test_names_cannot_escape_the_explicit_parent(self) -> None:
        for name in ("..", "../escape", "/absolute", "bad\x00name"):
            for request in (folders.UniqueBuildFolder(name, folders.ExplicitBuildParent(self.root)),
                            folders.StableOwnerFolder(name, folders.ExplicitBuildParent(self.root))):
                with self.subTest(name=name, request=type(request).__name__), self.assertRaises(ValueError):
                    _ = folders.create_build_folder(request)
        self.assertEqual(list(self.root.iterdir()), [])


    def test_approved_patterns_share_one_naming_function(self) -> None:
        cases: list[tuple[folders.BuildFolderKind, str]] = [
            ("temporary", "hana-key/unit-proof-abcdef"), ("gate", "proof-abcdef"),
            ("standing", "hana-key/unit-gate"), ("editable", "hana-key-unit/proof"),
            ("mirror", "hana-key/unit-s3"), ("target", "proof"), ("shell-cache", "hana-key"),
        ]
        for kind, expected in cases:
            with self.subTest(kind=kind):
                spec = folders.BuildFolderSpec(kind=kind, parent=self.root, suffix_length=6,
                                                repository="hana-key", owner="unit", purpose="proof", slot=3,
                                                suffix="abcdef" if kind in ("temporary", "gate") else "")
                self.assertEqual(folders.build_folder_path(spec), self.root / expected)
        with mock.patch.object(folders, "build_folder_path", wraps=folders.build_folder_path) as naming:
            with folders.temporary_build_folder(folders.UniqueBuildFolder("proof-")):
                pass
        naming.assert_called_once()

    def test_collision_retries_the_same_name_rule(self) -> None:
        spec = folders.BuildFolderSpec(kind="temporary", parent=self.root, suffix_length=2,
                                       repository="repo", owner="unit", purpose="proof")
        with mock.patch("secrets.choice", side_effect=list("aabb")):
            first = folders.create_build_folder(spec)
        with mock.patch("secrets.choice", side_effect=list("aabb")):
            second = folders.create_build_folder(spec)
        self.assertEqual(first.name, "unit-proof-aa")
        self.assertEqual(second.name, "unit-proof-bb")

    def test_nonvisible_scratch_target_uses_the_shared_name_rule(self) -> None:
        with (mock.patch.object(scratch, "validate_phase_task"),
              mock.patch.object(scratch, "build_folder_path", wraps=folders.build_folder_path) as naming):
            path = scratch.target("gate:proof", "tree", False)
        naming.assert_called_once()
        self.assertEqual(path.parent, copy_record.root())
        self.assertRegex(path.name, r"^gate-proof-tree-[a-z0-9]{6}$")

    def test_configuration_helper_supplies_missing_files_and_preserves_overrides(self) -> None:
        config = Path(os.environ["CONF_DIR"]) / "build_folders.conf"
        original = config.read_text()
        config.unlink()
        environment = config_environment(config.parent)
        self.assertEqual(environment, {"CONF_DIR": str(config.parent)})
        sources = Path(__file__).resolve().parents[2] / "config"
        for source in sources.glob("*.conf"):
            if source.name != "build_admission.conf":
                self.assertEqual((config.parent / source.name).read_bytes(), source.read_bytes())
        self.assertEqual(folders.FOLDER_PREFIXES["admit-test"], "admit-test-")
        with folders.temporary_build_folder(folders.UniqueBuildFolder("probe-", folders.SystemTemporaryParent())) as path:
            self.assertEqual(path.parent, Path("/tmp"))
            self.assertTrue(path.is_dir())
        self.assertFalse(path.exists())
        _ = config.write_text(original.replace("prefixes.admit-test=admit-test-", "prefixes.admit-test=private-"))
        _ = config_environment(config.parent)
        self.assertEqual(folders.FOLDER_PREFIXES["admit-test"], "private-")

    def test_repository_key_keeps_the_fixed_path_name(self) -> None:
        self.assertEqual(folders.repository_key(Path("/fixture/hana")), "hana-e4417bba")
        self.assertEqual(folders.repository_key_for("hana", "/fixture/hana"), "hana-e4417bba")
        self.assertEqual(folders.repository_key_for("hana space%", "/fixture/hana"), "hana space%-e4417bba")

    def test_equal_repository_basenames_have_distinct_keys(self) -> None:
        first = self.root / "first" / "hana"
        second = self.root / "second" / "hana"
        first.mkdir(parents=True)
        second.mkdir(parents=True)
        alias = self.root / "alias"
        alias.symlink_to(first, target_is_directory=True)
        self.assertNotEqual(folders.repository_key(first), folders.repository_key(second))
        self.assertEqual(folders.repository_key(first), folders.repository_key(alias))

    def test_private_config_without_folder_rules_uses_repository_without_git(self) -> None:
        config = Path(os.environ["CONF_DIR"]) / "build_folders.conf"
        original = config.read_text()
        config.unlink()
        with mock.patch("scripts.lib.conf.subprocess.run", side_effect=AssertionError("Git must not run")):
            self.assertEqual(folders.FOLDER_PREFIXES["verify-hold"], "verify-hold.{owner}.")
            path = folders.create_build_folder(folders.UniqueBuildFolder("private-"))
        self.assertTrue(path.is_dir())
        self.assertFalse(config.exists())
        _ = config.write_text(original.replace("prefixes.verify-hold=verify-hold.{owner}.",
                                               "prefixes.verify-hold=private.{owner}."))
        self.assertEqual(folders.FOLDER_PREFIXES["verify-hold"], "private.{owner}.")

    def test_folder_rules_keep_the_live_config_reader_without_a_private_override(self) -> None:
        settings = conf.read(Path(__file__).resolve().parents[2] / "config" / "build_folders.conf")
        with mock.patch.dict(os.environ):
            _ = os.environ.pop("CONF_DIR", None)
            with mock.patch.object(conf, "read", return_value=settings) as read:
                self.assertTrue(folders.FOLDER_PREFIXES["verify-hold"])
            read.assert_called_once_with("build_folders")

    def test_naming_reads_changed_configuration_at_use(self) -> None:
        config = Path(os.environ["CONF_DIR"]) / "build_folders.conf"
        _ = config.write_text(config.read_text().replace("patterns.temporary={repository}/{owner}-{purpose}-{suffix}",
                                                       "patterns.temporary={repository}/{purpose}-{owner}-{suffix}"))
        spec = folders.BuildFolderSpec(kind="temporary", parent=self.root, suffix_length=3,
                                       repository="repo", owner="unit", purpose="proof", suffix="xyz")
        self.assertEqual(folders.build_folder_path(spec), self.root / "repo" / "proof-unit-xyz")

    def test_run_removes_work_on_success_and_failure_and_records_owner(self) -> None:
        probe = """import json, os, pathlib, sys
path = pathlib.Path.cwd()
assert str(path) == os.environ['BUILD_FOLDER']
marker = json.loads((path / '.build-folder-owner.json').read_text())
assert marker['pid'] == os.getppid()
assert marker['start_time'] == pathlib.Path('/proc/%s/stat' % marker['pid']).read_text().rsplit(')',1)[1].split()[19]
assert marker['boot_id'] == pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
print(json.dumps({'path': str(path), 'owner': marker['owner']}))
sys.exit(int(sys.argv[1]))
"""
        for status in (0, 7):
            with self.subTest(status=status):
                result = self.command("run", "--owner", "unit", "--name", "proof", "--", sys.executable,
                                      "-c", probe, str(status))
                self.assertEqual(result.returncode, status, result.stderr)
                record = cast(dict[str, str], json.loads(result.stdout))
                self.assertEqual(record["owner"], "unit")
                self.assertFalse(Path(record["path"]).exists())

    def test_run_stops_and_reaps_an_escaped_descendant(self) -> None:
        probe = """import os, pathlib, subprocess, sys
child = subprocess.Popen([sys.executable, '-c', 'import os, signal; os.setsid(); print(os.getpid(), flush=True); signal.pause()'], stdout=subprocess.PIPE, text=True)
assert child.stdout is not None
print(str(pathlib.Path.cwd()), child.stdout.readline().strip(), flush=True)
os._exit(0)
"""
        result = self.command("run", "--owner", "unit", "--name", "proof", "--", sys.executable, "-c", probe)
        self.assertEqual(result.returncode, 0, result.stderr)
        path, child_pid = result.stdout.split()
        self.assertFalse(Path(path).exists())
        with self.assertRaises(ProcessLookupError):
            os.kill(int(child_pid), 0)

    def test_run_removes_work_after_each_handled_signal(self) -> None:
        probe = "import os, pathlib, signal; print(str(pathlib.Path.cwd()), os.getpid(), flush=True); signal.pause()"
        for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            with self.subTest(signal=number):
                process: subprocess.Popen[str] = subprocess.Popen([sys.executable, str(Path(folders.__file__)), "run", "--owner", "unit",
                                            "--name", "proof", "--", sys.executable, "-c", probe],
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    assert process.stdout is not None
                    path, child_pid = cast(str, process.stdout.readline()).split()
                    process.send_signal(number)
                    stdout, stderr = process.communicate(timeout=10)
                    self.assertEqual(process.returncode, 128 + number, stdout + stderr)
                    self.assertFalse(Path(path).exists())
                    with self.assertRaises(ProcessLookupError):
                        os.kill(int(child_pid), 0)
                finally:
                    if process.poll() is None:
                        process.kill()
                    _ = process.communicate(timeout=10)


if __name__ == "__main__":
    _ = unittest.main()
