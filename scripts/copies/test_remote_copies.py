"""Check staged copies and remote mirror ownership with real locks."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
import fcntl
import os
import select
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from threading import Event
from typing import final, override
import unittest
from unittest import mock

from scripts.copies import copies
from scripts.copies.copies_test_support import copies_environment, fake_remote_removal
from scripts.lib import conf, file_lock
from scripts.lib.py_floor_test_support import seed_interpreter_cache
from scripts.lib.atomic_file import write_text_atomic
from scripts.mac_test import floor_claims
from scripts.lint import tmp_sweep
from scripts.production.cli_fixture import ImmutableGitSeed
from scripts.production import staff
from scripts.production.staff_test_support import isolated_staff_file, fixture_member


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
        self.enterContext(mock.patch.dict(os.environ, copies_environment(self.root)))
        self.source = ImmutableGitSeed.create(self.root, "main", "Copy Tests", "copy@example.invalid",
                                             {"Cargo.toml": "[workspace]\n", "source.txt": "committed\n", "stable.txt": "stable\n"}).checkout
        self.mirror = self.root / "mirrors/repository"

    def remote(self, owner: str = "alpha", path: str = "mirror/repository") -> copies.RemoteMirror:
        return copies.RemoteMirror(owner, copies.SelectedRepository(self.source.name), "host", path, "holder-session")

    def test_a_remote_mirror_keeps_its_staff_owner_after_a_rename(self) -> None:
        self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        parent = fixture_member("showrunner", name="builds")
        unit = fixture_member("unit", name="alpha", parent=parent.id)
        mirror = self.remote(str(unit.id))
        with fake_remote_removal((mirror,)) as remove:
            _ = staff.rename(unit.id, "renamed")
            self.assertEqual(copies.owned_remote_mirrors(str(unit.id)), [mirror])
            copies.remove_trees(str(unit.id))
        remove.assert_called_once_with(mirror)
        self.assertEqual(copies.owned_remote_mirrors(str(unit.id)), [])

    def test_staged_source_keeps_only_index_content_without_source_changes(self) -> None:
        original = self.source / "source.txt"
        _ = original.write_text("staged\n")
        _ = copies.git(self.source, "add", "source.txt")
        _ = original.write_text("unstaged\n")
        _ = (self.source / "untracked.txt").write_text("untracked\n")
        index = self.source / ".git/index"
        before = index.read_bytes(), index.stat().st_mtime_ns, original.stat().st_mtime_ns
        with copies.standing("alpha", self.source, copies.StagedSource(copies.staged_tree(self.source))) as tree:
            self.assertEqual((tree / "source.txt").read_text(), "staged\n")
            self.assertFalse((tree / "untracked.txt").exists())
            stable = tree / "stable.txt"
            os.utime(stable, ns=(1_000_000_000, 1_000_000_000))
            target = tree / "target/retained"
            _ = target.write_text("artifact")
        with copies.standing("alpha", self.source, copies.StagedSource(copies.staged_tree(self.source))) as reused:
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
        with fake_remote_removal((mine, other, elsewhere)) as remove:
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

    def test_busy_mirror_stays_and_the_other_mirror_goes(self) -> None:
        busy, idle = self.remote(), self.remote("alpha", "mirror/idle")
        with fake_remote_removal((busy, idle)) as remove, copies.remote_lease(busy):
            with self.assertRaisesRegex(copies.CopyError, "mirror/repository.*holder-session"):
                copies.remove_trees("alpha")
        remove.assert_called_once_with(idle)
        self.assertEqual(copies.owned_remote_mirrors("alpha"), [busy])

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
        lock_inode = floor_claims.mirror_lock_path(self.mirror).stat().st_ino
        with copies.local_mirror_lease(self.mirror, "run-session"):
            with self.assertRaisesRegex(RuntimeError, "cancelled sweep"):
                with copies.mirror_sweep_leases(self.mirror.parent) as protected:
                    self.assertEqual(protected, frozenset({self.mirror}))
                    with copies.target_mirror_leases([str(self.mirror / "target")]) as linux_protected:
                        self.assertEqual(linux_protected, frozenset({self.mirror}))
                    with file_lock.exclusive(floor_claims.mirror_lock_path(idle), wait=False) as state:
                        self.assertIs(state, file_lock.LockResult.BUSY)
                    raise RuntimeError("cancelled sweep")
        with copies.local_mirror_lease(idle, "after-cancel"):
            pass
        self.assertEqual(floor_claims.mirror_lock_path(self.mirror).stat().st_ino, lock_inode)
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
            with file_lock.exclusive(floor_claims.mirror_lock_path(self.mirror), wait=False) as state:
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
            self.assertEqual(copies.mirror_run(args), 0)
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

    @contextmanager
    def open_file_process(self, path: Path) -> Generator[subprocess.Popen[str]]:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text("held\n")
        child = subprocess.Popen([
            sys.executable, "-c",
            "import signal,sys; held=open(sys.argv[1]); print('ready',flush=True); signal.pause()",
            str(path),
        ], cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            if child.stdout is None:
                self.fail("The child has no readiness stream")
            self.assertTrue(select.select([child.stdout], [], [], 10)[0])
            self.assertEqual(child.stdout.readline(), "ready\n")
            yield child
        finally:
            child.kill()
            _ = child.communicate(timeout=10)

    def test_open_file_below_mirror_keeps_the_mirror_busy(self) -> None:
        with self.open_file_process(self.mirror / "target/output"):
            self.assertTrue(copies.mirror_has_process(self.mirror, copies.scan_mirror_processes((self.mirror,))))

    def test_open_file_in_text_prefix_sibling_does_not_keep_the_mirror(self) -> None:
        self.mirror.mkdir(parents=True)
        with self.open_file_process(self.mirror.with_name(self.mirror.name + "-other") / "output"):
            self.assertFalse(copies.mirror_has_process(self.mirror, copies.scan_mirror_processes((self.mirror,))))

    def test_process_below_the_filesystem_root_keeps_a_root_mirror(self) -> None:
        uses = tmp_sweep.ProcessUsesKnown([("/var/tmp/output", os.getpid() + 1, "cwd")])
        self.assertTrue(copies.mirror_has_process(Path("/"), copies.MirrorProcessScan(uses, frozenset())))

    def test_unavailable_lsof_scan_keeps_the_mirror_busy(self) -> None:
        self.mirror.mkdir(parents=True)
        tools = self.root / "lsof-tools"
        tools.mkdir()
        lsof = tools / "lsof"
        _ = lsof.write_text("#!/bin/sh\nexit 1\n")
        lsof.chmod(0o755)
        with mock.patch.object(sys, "platform", "darwin"), mock.patch.dict(os.environ, {
                "PATH": str(tools) + os.pathsep + os.environ["PATH"]}):
            self.assertTrue(copies.mirror_has_process(self.mirror, copies.scan_mirror_processes((self.mirror,))))

    def test_unreadable_process_in_the_lease_group_keeps_the_mirror(self) -> None:
        self.mirror.mkdir(parents=True)
        with self.open_file_process(self.root / "outside/output") as child:
            copies.mark_mirror(self.mirror, "alpha", "repository", "group-session", process_group=os.getpgid(child.pid))
            cwd = Path("/proc") / str(child.pid) / "cwd"
            readlink = os.readlink

            def unreadable_readlink(path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                                    *, dir_fd: int | None = None) -> str:
                if os.fsdecode(path) == str(cwd):
                    raise PermissionError(str(path))
                return readlink(os.fsdecode(path), dir_fd=dir_fd)

            with mock.patch.object(os, "readlink", unreadable_readlink):
                with self.assertRaisesRegex(copies.CopyError, "busy.*group-session"):
                    with copies.local_mirror_lease(self.mirror, "remove", create=False):
                        self.fail("An unreadable process lost its lease group")

    def test_process_started_before_the_first_lease_keeps_the_mirror(self) -> None:
        self.mirror.mkdir(parents=True)
        flock = fcntl.flock

        @contextmanager
        def child_in_mirror() -> Generator[None]:
            child = subprocess.Popen([
                sys.executable, "-c",
                "import signal; print('ready',flush=True); signal.pause()",
            ], cwd=self.mirror, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                if child.stdout is None:
                    self.fail("The child has no readiness stream")
                self.assertTrue(select.select([child.stdout], [], [], 10)[0])
                self.assertEqual(child.stdout.readline(), "ready\n")
                yield
            finally:
                child.kill()
                _ = child.communicate(timeout=10)

        with ExitStack() as cleanup:
            started = False

            def start_before_lease(descriptor: int, operation: int) -> None:
                nonlocal started
                if not started and operation & fcntl.LOCK_EX:
                    started = True
                    cleanup.enter_context(child_in_mirror())
                flock(descriptor, operation)

            with mock.patch.object(fcntl, "flock", start_before_lease):
                with copies.mirror_lease_set({self.mirror}) as protected:
                    self.assertIn(self.mirror, protected)

    def test_lsof_exit_status_controls_scan_availability_and_mirror_protection(self) -> None:
        self.mirror.mkdir(parents=True)
        tools = self.root / "lsof-status-tools"
        tools.mkdir()
        lsof = tools / "lsof"
        for status in (0, 1, 2):
            _ = lsof.write_text("#!/bin/sh\nprintf 'p999999\\nfcwd\\nn/unrelated\\n'\nexit " + str(status) + "\n")
            lsof.chmod(0o755)
            with mock.patch.object(sys, "platform", "darwin"), mock.patch.dict(os.environ, {
                    "PATH": str(tools) + os.pathsep + os.environ["PATH"]}):
                with self.subTest(status=status, behavior="scan result"):
                    result = tmp_sweep.lsof_paths(str(self.mirror))
                    self.assertIsInstance(result, tmp_sweep.ProcessUsesUnavailable if status == 2 else tmp_sweep.ProcessUsesKnown)
                with self.subTest(status=status, behavior="mirror protection"):
                    self.assertEqual(copies.mirror_has_process(self.mirror, copies.scan_mirror_processes((self.mirror,))), status == 2)

    def test_many_mirrors_share_one_process_scan(self) -> None:
        idle = self.mirror.parent / "idle"
        idle.mkdir(parents=True)
        self.mirror.mkdir()
        with mock.patch.object(tmp_sweep, "process_paths", wraps=tmp_sweep.process_paths) as scan:
            with copies.mirror_lease_set({self.mirror, idle}) as protected:
                self.assertEqual(protected, frozenset())
            self.assertEqual(scan.call_count, 1)

    def probe_command(self) -> subprocess.CompletedProcess[str]:
        scripts = self.root / ".claude/scripts"
        scripts.parent.mkdir(exist_ok=True)
        scripts.symlink_to(Path(__file__).resolve().parents[1], target_is_directory=True)
        tools = self.root / "probe-tools"
        tools.mkdir()
        log = self.root / "probe-calls"
        _ = log.write_text("")
        python = tools / "python3"
        _ = python.write_text(f"#!{sys.executable}\n" + f"""import sys
from pathlib import Path
from scripts.copies import copies
from scripts.mac_test import floor_claims
log = Path({str(log)!r})
with log.open('a') as output:
    output.write('python\\n')
def trace(frame, event, argument):
    if event == 'call' and argument is None and frame.f_code.co_filename in (copies.__file__, floor_claims.__file__):
        if frame.f_code.co_name in ('mirror_local_path', 'mark_mirror'):
            with log.open('a') as output:
                output.write(frame.f_code.co_name + '\\n')
sys.setprofile(trace)
program_index = sys.argv.index("-c") + 1
program = sys.argv[program_index]
sys.argv = sys.argv[program_index:]
exec(compile(program, '<remote-probe>', 'exec'))
""")
        python.chmod(0o755)
        seed_interpreter_cache(self.root / "probe-cache", python)
        return subprocess.run(["/bin/sh", "-c", copies.mirror_probe_setup(str(self.mirror))],
                              env={**os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"],
                                   "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
                                   "XDG_CACHE_HOME": str(self.root / "probe-cache"), "CLAUDE_PYTHON": ""},
                              capture_output=True, text=True, timeout=10)

    def test_marked_probe_starts_no_remote_python(self) -> None:
        with copies.local_mirror_lease(self.mirror, "marked-session"):
            copies.mark_mirror(self.mirror, "alpha", "repository", "marked-session")
        result = self.probe_command()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "probe-calls").read_text(), "")

    def test_unmarked_probe_checks_the_local_path_before_marking(self) -> None:
        result = self.probe_command()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "probe-calls").read_text().splitlines(),
                         ["python", "mirror_local_path", "mark_mirror"])
        self.assertEqual(floor_claims.read_mirror_record(self.mirror / floor_claims.MIRROR_RECORD)["kind"], "remote-mirror")

    def test_allow_list_keeps_only_original_copy_makers(self) -> None:
        settings = conf.read(Path(__file__).resolve().parents[2] / "config/copies.conf").values()
        entries = {key.removeprefix("allow.") for key in settings if key.startswith("allow.")}
        self.assertEqual(entries, {"copies/copies.py", "production/add_unit.py", "shutdown/shutdown.py",
            "style/add_style.py", "worktree/make_worktree.sh",
            "bevy_migration_plan/bevy_migration_ensure_repo.sh"})

if __name__ == "__main__":
    _ = unittest.main()
