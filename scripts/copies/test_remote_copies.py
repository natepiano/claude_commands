"""Check staged copies and remote mirror ownership with real locks."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from collections.abc import Generator
from contextlib import contextmanager
import os
import select
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from threading import Event
from typing import cast, final, override
import unittest
from unittest import mock

from scripts.copies import copies
from scripts.lib import file_lock
from scripts.lib.atomic_file import write_text_atomic
from scripts.mac_test import floor_claims
from scripts.production.cli_fixture import ImmutableGitSeed


@final
class RemoteCopiesTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.source = Path()
        self.mirror = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        config = self.root / "copies.conf"
        _ = config.write_text(f"root={self.root}/gates\nwait_seconds=0.01\ngit_timeout_seconds=10\nguard_timeout_seconds=10\n")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.root), "COPIES_CONFIG": str(config),
            "SCRATCH_ROOT": str(self.root / "scratch"),
            "SCRATCH_COPIES_ROOT": str(self.root / "copies"),
            "SCRATCH_HOLDS": str(self.root / "holds.json"),
        })))
        self.source = ImmutableGitSeed.create(self.root, "main", "Copy Tests", "copy@example.invalid",
                                             {"source.txt": "committed\n", "stable.txt": "stable\n"}).checkout
        self.mirror = self.root / "mirrors/repository"

    def remote(self, owner: str = "alpha", path: str = "mirror/repository") -> copies.RemoteMirror:
        return copies.RemoteMirror(owner, copies.SelectedRepository(self.source.name), "host", path, "holder-session")

    def test_staged_source_keeps_only_index_content_without_source_changes(self) -> None:
        original = self.source / "source.txt"
        _ = original.write_text("staged\n")
        _ = copies.git(self.source, "add", "source.txt")
        _ = original.write_text("unstaged\n")
        _ = (self.source / "untracked.txt").write_text("untracked\n")
        index = self.source / ".git/index"
        before = index.read_bytes(), index.stat().st_mtime_ns, original.stat().st_mtime_ns
        with copies.standing("alpha", self.source, copies.StagedSource()) as tree:
            self.assertEqual((tree / "source.txt").read_text(), "staged\n")
            self.assertFalse((tree / "untracked.txt").exists())
            stable = tree / "stable.txt"
            os.utime(stable, ns=(1_000_000_000, 1_000_000_000))
            target = tree / "target/retained"
            _ = target.write_text("artifact")
        with copies.standing("alpha", self.source, copies.StagedSource()) as reused:
            self.assertEqual(reused, tree)
            self.assertEqual(stable.stat().st_mtime_ns, 1_000_000_000)
            self.assertEqual(target.read_text(), "artifact")
        self.assertEqual((index.read_bytes(), index.stat().st_mtime_ns, original.stat().st_mtime_ns), before)

    def test_remote_rsync_keeps_lease_and_owner_metadata(self) -> None:
        mirror = self.remote()
        command = copies.remote_rsync(self.source, mirror, self.root / "excludes", [])
        receiver = next(part for part in command if part.startswith("--rsync-path="))
        self.assertIn("copies.py mirror-receive", receiver)
        self.assertIn("--owner alpha", receiver)
        self.assertIn("--repository " + self.source.name, receiver)
        self.assertIn("--session holder-session", receiver)
        self.assertIn("--delete", command)

    def test_owner_removal_keeps_another_owner_and_repository(self) -> None:
        mine, other, elsewhere = self.remote(), self.remote("beta", "mirror/beta"), self.remote("alpha", "mirror/else")
        elsewhere = copies.RemoteMirror("alpha", copies.SelectedRepository("another"), "host", elsewhere.path, "another-session")
        for item in (mine, other, elsewhere):
            copies.register_remote(item)
            with copies.remote_lease(item):
                pass
        def remove_record(item: copies.RemoteMirror) -> None:
            copies.remote_record_path(item).unlink()

        with mock.patch.object(copies, "remove_remote_mirror", side_effect=remove_record) as remove:
            copies.remove_trees("alpha", copies.SelectedRepository(self.source.name))
        remove.assert_called_once_with(mine)
        self.assertEqual(copies.owned_remote_mirrors("alpha"), [elsewhere])
        self.assertEqual(copies.owned_remote_mirrors("beta"), [other])

    def test_busy_owner_removal_names_holder_and_path(self) -> None:
        mirror = self.remote()
        copies.register_remote(mirror)
        with copies.remote_lease(mirror):
            with self.assertRaisesRegex(copies.CopyError, "mirror/repository.*holder-session"):
                copies.remove_trees("alpha")
        self.assertEqual(copies.owned_remote_mirrors("alpha"), [mirror])

    def test_cancelled_copy_releases_lease_and_keeps_metadata(self) -> None:
        mirror = self.remote()
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            copies.register_remote(mirror)
            with copies.remote_lease(mirror):
                raise RuntimeError("cancelled")
        self.assertTrue(copies.remote_record_path(mirror).exists())
        copies.register_remote(mirror)
        with copies.remote_lease(mirror):
            self.assertTrue(copies.remote_record_path(mirror).exists())
        program = (
            "from scripts.copies import copies; import signal; "
            "mirror=copies.RemoteMirror('alpha',copies.SelectedRepository('repository'),'host','mirror/repository','cancel-session'); "
            "lease=copies.remote_lease(mirror); lease.__enter__(); copies.register_remote(mirror); print('ready',flush=True); signal.pause()"
        )
        child = subprocess.Popen([sys.executable, "-c", program], stdout=subprocess.PIPE, text=True)
        try:
            self.assertIsNotNone(child.stdout)
            if child.stdout is None:
                self.fail("The child has no readiness stream")
            self.assertTrue(select.select([child.stdout], [], [], 10)[0])
            self.assertEqual(child.stdout.readline(), "ready\n")
            child.terminate()
            _ = child.communicate(timeout=10)
            self.assertEqual(child.returncode, -signal.SIGTERM)
            copies.register_remote(mirror)
            with copies.remote_lease(mirror):
                self.assertTrue(copies.remote_record_path(mirror).exists())
        finally:
            if child.poll() is None:
                child.kill()
                _ = child.communicate(timeout=10)

    def test_sweep_keeps_a_busy_mirror_and_releases_idle_locks_after_cancel(self) -> None:
        copies.probe_remote_folder(str(self.mirror))
        idle = self.mirror.parent / "idle"
        copies.probe_remote_folder(str(idle))
        lock_inode = copies.mirror_lock_path(self.mirror).stat().st_ino
        with copies.local_mirror_lease(self.mirror, "run-session"):
            with self.assertRaisesRegex(RuntimeError, "cancelled sweep"):
                with copies.mirror_sweep_leases(self.mirror.parent) as protected:
                    self.assertEqual(protected, frozenset({self.mirror}))
                    with copies.target_mirror_leases([str(self.mirror / "target")]) as linux_protected:
                        self.assertEqual(linux_protected, frozenset({self.mirror}))
                    with file_lock.exclusive(copies.mirror_lock_path(idle), wait=False) as state:
                        self.assertIs(state, file_lock.LockResult.BUSY)
                    raise RuntimeError("cancelled sweep")
        with copies.local_mirror_lease(idle, "after-cancel"):
            pass
        self.assertEqual(copies.mirror_lock_path(self.mirror).stat().st_ino, lock_inode)
        self.assertTrue((self.mirror / ".copy-record.json").exists())

    def test_live_child_keeps_the_remote_lease_after_owner_exit(self) -> None:
        copies.probe_remote_folder(str(self.mirror))
        ready = self.root / "ready"
        program = (
            "from pathlib import Path; import subprocess,sys; from scripts.copies import copies; "
            "tree=Path(sys.argv[1]); "
            "lease=copies.local_mirror_lease(tree,'surviving-session'); lease.__enter__(); "
            "child=subprocess.Popen([sys.executable,'-c','import signal; signal.pause()'],pass_fds=copies.COMMAND_LOCKS.get(),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
            "Path(sys.argv[2]).write_text(str(child.pid))"
        )
        answer = subprocess.run([sys.executable, "-c", program, str(self.mirror), str(ready)],
                                cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=10)
        self.assertEqual(answer.returncode, 0, answer.stderr)
        pid = int(ready.read_text())
        try:
            with self.assertRaisesRegex(copies.CopyError, "surviving-session"):
                with copies.local_mirror_lease(self.mirror, "remove"):
                    self.fail("A surviving child lost its lease")
            self.assertTrue((self.mirror / ".copy-record.json").exists())
        finally:
            os.kill(pid, signal.SIGKILL)

    def test_surviving_process_without_an_inherited_descriptor_keeps_metadata(self) -> None:
        copies.probe_remote_folder(str(self.mirror))
        child = subprocess.Popen([sys.executable, "-c", "import signal; signal.pause()"], cwd=self.mirror)
        try:
            with self.assertRaisesRegex(copies.CopyError, "mirror.*busy"):
                with copies.local_mirror_lease(self.mirror, "remove"):
                    self.fail("A surviving process permits removal")
            self.assertTrue((self.mirror / ".copy-record.json").exists())
        finally:
            child.kill()
            _ = child.wait(timeout=5)

    def test_concurrent_sweep_precedes_claim_and_record_locks(self) -> None:
        copies.probe_remote_folder(str(self.mirror))
        entered, release = Event(), Event()

        def sweep() -> None:
            with copies.mirror_sweep_leases(self.mirror.parent) as protected:
                self.assertFalse(protected)
                with file_lock.exclusive(self.root / "claims.lock"):
                    with file_lock.exclusive(self.root / "records.lock"):
                        entered.set()
                        self.assertTrue(release.wait(timeout=10))

        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(sweep)
            try:
                self.assertTrue(entered.wait(timeout=10))
                for action in ("admission", "owner removal"):
                    with self.subTest(action=action), self.assertRaises(copies.CopyError):
                        with copies.local_mirror_lease(self.mirror, action):
                            self.fail("A sweep holds this lease")
            finally:
                release.set()
            pending.result(timeout=10)
        with copies.local_mirror_lease(self.mirror, "admission"):
            pass

    def test_receiver_takes_lease_then_claim_then_record_and_keeps_metadata(self) -> None:
        args = copies.CopyArguments()
        args.path, args.action = str(self.mirror), "mirror-receive"
        args.owner, args.repository, args.session = "alpha", "repository", "receiver-session"
        args.command = [sys.executable, "-c", "pass"]
        claim_held = Event()
        real_write = write_text_atomic

        @contextmanager
        def claims(_timeout: float) -> Generator[floor_claims.LockedClaims]:
            with file_lock.exclusive(copies.mirror_lock_path(self.mirror), wait=False) as state:
                self.assertIs(state, file_lock.LockResult.BUSY)
            claim_held.set()
            try:
                yield floor_claims.LockedClaims(frozenset())
            finally:
                claim_held.clear()

        def record(path: Path, content: str) -> None:
            if path.name == ".copy-record.json":
                self.assertTrue(claim_held.is_set())
                lock = self.mirror.parent / ("." + self.mirror.name + ".copy-record.lock")
                with file_lock.exclusive(lock, wait=False) as state:
                    self.assertIs(state, file_lock.LockResult.BUSY)
            real_write(path, content)

        with mock.patch.object(floor_claims, "locked_claims", side_effect=claims), \
                mock.patch.object(copies, "write_text_atomic", side_effect=record):
            self.assertEqual(copies.mirror_action(args), 0)
        metadata = self.mirror / ".copy-record.json"
        self.assertIn('"owner": "alpha"', metadata.read_text())
        self.assertTrue((self.mirror / ".mac-test-mirror").exists())

    def test_remote_kind_covers_mac_linux_and_aws(self) -> None:
        from scripts.mac_test import offload
        from scripts.build_targets import target_config
        targets: tuple[tuple[str, target_config.Platform], ...] = (("mac", "macos"), ("linux", "linux"), ("aws", "linux"))
        for name, platform in targets:
            with self.subTest(target=name), mock.patch.object(offload, "configured_ssh_target",
                return_value=target_config.SshTarget(name, platform, self.root / "settings")):
                request = offload.OffloadRequest(self.source, "package", "call", offload.NO_RESULT_FILE, (),
                                                target=name, owner="alpha", holder_session="session")
                config = offload.OffloadConfig(True, offload.SshConnectionSettings("host", 1, 1), 1, 1, 1, "1", {})
                mirror = offload.request_mirror(config, request, offload.KnownRepository("repository"))
                self.assertEqual(mirror.owner, "alpha")
                self.assertEqual(mirror.repository, copies.SelectedRepository("repository"))
                self.assertEqual(mirror.session, "session")
                self.assertIn("copies.py mirror-use", copies.remote_use_command(mirror, "true"))

    def test_allow_list_keeps_only_original_copy_makers(self) -> None:
        settings = (Path(__file__).resolve().parents[2] / "config/copies.conf").read_text()
        entries = {line.split("=", 1)[0].removeprefix("allow.") for line in settings.splitlines() if line.startswith("allow.")}
        self.assertEqual(entries, {"copies/copies.py", "production/add_unit.py", "production/resume_unit.py",
            "style/add_style.py", "worktree/make_worktree.sh", "berth_fix/launch.sh",
            "bevy_migration_plan/bevy_migration_ensure_repo.sh"})

    def test_doc_checkout_uses_the_staged_copy_api(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "direct/verify.sh").read_text()
        checkout = source[source.index("doc_checkout() {"):source.index("doc_record() {")]
        self.assertIn('copies.py', checkout)
        self.assertIn('hold --repo', checkout)
        self.assertNotIn('read-tree', checkout)
        self.assertNotIn('checkout-index', checkout)


if __name__ == "__main__":
    _ = unittest.main()
