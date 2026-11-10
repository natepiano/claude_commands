"""Check staged copies and remote mirror ownership with real locks."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from collections.abc import Generator
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
import fcntl
import io
import json
import os
import select
import shutil
import socket
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
from scripts.copies.copies_test_support import copies_environment, fake_remote_removal, publish_mirror
from scripts.copies.remote_test_support import local_remote_transport, warm_claim_servers
from scripts.lib import conf, file_lock
from scripts.lib.py_floor_test_support import seed_interpreter_cache
from scripts.lib.atomic_file import write_text_atomic
from scripts.mac_test import floor_claims
from scripts.lint import tmp_sweep
from scripts.production.cli_fixture import ImmutableGitSeed
from scripts.production import staff
from scripts.production.staff_test_support import isolated_staff_file, fixture_member, fixture_worker
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


def expected_remote_lease_columns(state: str, session: str, holder_pid: str) -> tuple[str, str, str]:
    """Keep Linux lease assertions and report unavailable when the real lock table is absent."""
    if not Path(file_lock.PROC_LOCKS).exists():
        return "unavailable", "-", "-"
    return state, session, holder_pid


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

    def list_rows(self) -> list[dict[str, str]]:
        output = io.StringIO()
        with redirect_stdout(output):
            copies.list_trees()
        lines = output.getvalue().splitlines()
        self.assertGreater(len(lines), 1, output.getvalue())
        columns = lines[0].split("\t")
        self.assertEqual(columns, ["number", "owner", "repository", "host", "path", "session",
                                   "commit", "state", "holder_pid", "target"])
        return [dict(zip(columns, line.split("\t"), strict=True)) for line in lines[1:]]

    def list_members(self) -> tuple[staff.StaffRow, staff.StaffRow]:
        self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        parent = fixture_member("showrunner", name="list-production")
        owner = fixture_member("unit", name="mirror-owner", parent=parent.id)
        holder = fixture_worker("live-holder", owner.id)
        return owner, holder

    def test_list_reports_remote_only_held_idle_and_missing_lease_records(self) -> None:
        owner, holder = self.list_members()
        for lease in ("held", "idle", "missing"):
            with self.subTest(lease=lease), ExitStack() as contexts:
                mirror = copies.RemoteMirror(str(owner.id), copies.SelectedRepository(self.source.name),
                                             "remote-host", "mirror/" + lease, holder.current_session)
                if lease == "held":
                    _ = contexts.enter_context(copies.remote_lease(mirror))
                else:
                    with copies.remote_lease(mirror):
                        pass
                    if lease == "missing":
                        copies.remote_lease_path(mirror).unlink()
                copies.register_remote(mirror)
                row = next(row for row in self.list_rows() if row["path"] == mirror.path)
                self.assertEqual((row["owner"], row["repository"], row["host"]),
                                 ("mirror-owner", self.source.name, "remote-host"))
                self.assertEqual((row["state"], row["session"], row["holder_pid"]),
                                 expected_remote_lease_columns(
                                     "held", "live-holder", str(os.getpid())) if lease == "held"
                                 else expected_remote_lease_columns("idle", "-", "-"))
                self.assertEqual((row["commit"], row["target"]), ("-", "-"))
                self.assertNotIn(holder.current_session, "\t".join(row.values()))
        checkout = self.root / "scratch/local/checkout"
        checkout.mkdir(parents=True)
        local = copies.RemoteMirror(str(owner.id), copies.SelectedRepository(self.source.name),
                                    "localhost", str(checkout), holder.current_session)
        copies.register_remote(local)
        row = next(row for row in self.list_rows() if row["path"] == str(checkout))
        self.assertEqual(row["host"], socket.gethostname())
        self.assertEqual(copies.read_remote_mirror(copies.remote_record_path(local)).host, "localhost")
        shutil.rmtree(checkout.parent)
        with copies.local_mirror_lease(checkout, "missing", create=False):
            self.assertFalse(checkout.parent.exists())
        with copies.mirror_sweep_leases(self.root / "mirrors"):
            self.assertFalse(checkout.parent.exists())
        self.assertFalse(copies.remote_record_path(local).exists())
        self.assertFalse(any(row["path"] == str(checkout) for row in self.list_rows()))

    def test_list_requires_matching_pid_before_naming_the_recorded_holder(self) -> None:
        owner, holder = self.list_members()
        for evidence, session in (("different-pid", "not recorded"), ("missing-pid", "not recorded"),
                                  ("missing-holder", "unavailable")):
            with self.subTest(evidence=evidence):
                stored_session = "pid-2147483000" if evidence == "missing-holder" else holder.current_session
                mirror = copies.RemoteMirror(str(owner.id), copies.SelectedRepository(self.source.name),
                                             "remote-host", "mirror/" + evidence, stored_session)
                with copies.remote_lease(mirror):
                    copies.register_remote(mirror)
                    record_path = copies.remote_record_path(mirror)
                    record = cast(dict[str, object], json.loads(record_path.read_text()))
                    if evidence == "different-pid":
                        record["holder_pid"] = os.getpid() + 1
                    elif evidence == "missing-pid":
                        del record["holder_pid"]
                    _ = record_path.write_text(json.dumps(record))
                    row = next(row for row in self.list_rows() if row["path"] == mirror.path)
                    self.assertEqual((row["state"], row["session"], row["holder_pid"]),
                                     expected_remote_lease_columns("held", session, str(os.getpid())))
                    self.assertNotIn(stored_session, "\t".join(row.values()))

    def test_list_keeps_owner_and_holder_names_separate(self) -> None:
        owner, holder = self.list_members()
        missing_owner = "00000000-0000-4000-8000-000000000001"
        for identity, expected_owner, expected_holder in (
            (str(owner.id), "renamed-owner", "live-holder"),
            ("toolchain-trial", "toolchain-trial", "live-holder"),
            (missing_owner, "unavailable", "live-holder"),
            ("staff-read-error", "unavailable", "unavailable"),
        ):
            with self.subTest(owner=identity), ExitStack() as contexts:
                mirror = copies.RemoteMirror(str(owner.id) if identity == "staff-read-error" else identity,
                    copies.SelectedRepository(self.source.name), "remote-host", "mirror/" + identity,
                    holder.current_session)
                _ = contexts.enter_context(copies.remote_lease(mirror))
                copies.register_remote(mirror)
                if identity == str(owner.id):
                    _ = staff.rename(owner.id, "renamed-owner")
                if identity == "staff-read-error":
                    _ = cast(object, contexts.enter_context(mock.patch.dict(os.environ, {"STAFF_FILE": str(self.root)})))
                before = copies.remote_record_path(mirror).read_bytes()
                row = next(row for row in self.list_rows() if row["path"] == mirror.path)
                self.assertEqual(row["owner"], expected_owner)
                self.assertEqual((row["state"], row["session"], row["holder_pid"]),
                                 expected_remote_lease_columns("held", expected_holder, str(os.getpid())))
                if identity != "toolchain-trial":
                    self.assertNotIn(mirror.owner, row["owner"])
                self.assertEqual(copies.remote_record_path(mirror).read_bytes(), before)

    def test_list_reports_unavailable_lease_evidence_without_naming_a_former_holder(self) -> None:
        owner, holder = self.list_members()
        read_locks = file_lock.held_flock_keys
        for evidence in ("unreadable-table", "macos-no-proc", "unreadable-lease"):
            with self.subTest(evidence=evidence), ExitStack() as contexts:
                mirrors = [copies.RemoteMirror(str(owner.id), copies.SelectedRepository(self.source.name),
                           "remote-host", f"mirror/{evidence}-{index}", holder.current_session) for index in range(2)]
                for mirror in mirrors:
                    copies.register_remote(mirror)
                    if evidence == "unreadable-lease":
                        lease = copies.remote_lease_path(mirror)
                        lease.symlink_to(lease.name)
                if evidence != "unreadable-lease":
                    lock_table = self.root if evidence == "unreadable-table" else self.root / "absent-proc-locks"
                    def fixture_locks() -> dict[str, int]:
                        return read_locks(str(lock_table))
                    _ = contexts.enter_context(mock.patch.object(file_lock, "held_flock_keys", side_effect=fixture_locks))
                    if evidence == "macos-no-proc":
                        _ = contexts.enter_context(mock.patch.object(sys, "platform", "darwin"))
                rows = self.list_rows()
                for mirror in mirrors:
                    row = next(row for row in rows if row["path"] == mirror.path)
                    self.assertEqual((row["owner"], row["state"], row["session"], row["holder_pid"]),
                                     ("mirror-owner", "unavailable", "-", "-"))

    def test_list_names_the_holder_registered_by_the_real_offload_process(self) -> None:
        from scripts.mac_test.offload_test_support import OffloadCommandFixture

        holder = self.list_members()[1]
        fixture = OffloadCommandFixture()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.fixture["hold_run"] = True
        fixture.sync_fixture()
        process = fixture.start_command(extra={"CLAUDE_CODE_SESSION_ID": holder.current_session,
                                               "CODEX_THREAD_ID": "", "UNIT_RUN_DIR": "",
                                               "UNIT_SESSION_DIR": "", "PLAN_DELEGATE_BOARD_DIR": "",
                                               "PLAN_DELEGATE_SESSION_DIR": ""})
        try:
            fixture.wait_for_path(fixture.stub_directory / "run-started")
            with mock.patch.dict(os.environ, fixture.environment):
                mirror, = copies.owned_remote_mirrors("verify")
                record = cast(dict[str, object], json.loads(copies.remote_record_path(mirror).read_text()))
                self.assertEqual(record["holder_pid"], process.pid)
                row, = self.list_rows()
                self.assertEqual(row["owner"], "verify")
                self.assertEqual((row["state"], row["session"], row["holder_pid"]),
                                 expected_remote_lease_columns("held", "live-holder", str(process.pid)))
            _ = (fixture.stub_directory / "finish-run").write_text("continue\n")
            output, _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(process.returncode, 0, output)
        finally:
            if process.poll() is None:
                process.kill()
                _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)

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
            target_root = (tree / "target").resolve()
            target = target_root / "retained"
            _ = target.write_text("artifact")
        with copies.standing("alpha", self.source, copies.StagedSource(copies.staged_tree(self.source))) as reused:
            self.assertNotEqual(reused, tree)
            self.assertFalse(tree.exists())
            self.assertEqual((reused / "target").resolve(), target_root)
            self.assertEqual((reused / "stable.txt").stat().st_mtime_ns, 1_000_000_000)
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
        self.assertIn("--compress", command)
        self.assertIn("-rlpc", command)
        exclude = self.root / "excludes"
        _ = exclude.write_text("/target/\n")
        output = self.root / "receiver"
        previous_mtime = 0
        for body, unchanged in (("before", False), ("edited", False), ("edited", True)):
            with self.subTest(body=body, unchanged=unchanged):
                source = self.source / "same-size.txt"
                _ = source.write_text(body)
                os.utime(source, (1_000_000_000, 1_000_000_000))
                answer = subprocess.run([*command[:-1], f"{output}/"],
                    capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(answer.returncode, 0, answer.stdout + answer.stderr)
                self.assertEqual((output / "same-size.txt").read_text(), body)
                destination_mtime = (output / "same-size.txt").stat().st_mtime_ns
                self.assertGreater(destination_mtime, source.stat().st_mtime_ns)
                if unchanged:
                    self.assertEqual(destination_mtime, previous_mtime)
                previous_mtime = destination_mtime

    def test_owner_removal_keeps_another_owner_and_repository(self) -> None:
        mine, other, elsewhere = self.remote(), self.remote("beta", "mirror/beta"), self.remote("alpha", "mirror/else")
        elsewhere = copies.RemoteMirror("alpha", copies.SelectedRepository("another"), "host", elsewhere.path, "another-session")
        with fake_remote_removal((mine, other, elsewhere)) as remove:
            copies.remove_trees("alpha", copies.SelectedRepository(self.source.name))
        remove.assert_called_once_with(mine)
        self.assertEqual(copies.owned_remote_mirrors("alpha"), [elsewhere])
        self.assertEqual(copies.owned_remote_mirrors("beta"), [other])

    def test_owner_removal_defers_only_connection_failures(self) -> None:
        cases: tuple[tuple[str, int, str, str], ...] = (
            ("connect", 255, "", "ssh: connect to host host port 22: Connection timed out"),
            ("dns", 255, "", "ssh: Could not resolve hostname host: Name or service not known"),
            ("timeout", 255, "", ""),
            ("refused", 2, "", "mirror is busy"),
            ("authentication", 255, "", "host: Permission denied (publickey)."),
            ("remote-refused", 0, "rc=2\n", "mirror is busy"),
            ("missing-status", 0, "", "remote status is missing"),
        )
        for cause, status, remote_output, detail in cases:
            with self.subTest(cause=cause), mock.patch.dict(os.environ, copies_environment(self.root / cause)):
                mirrors = [self.remote(path=f"mirror/{cause}-{index}") for index in range(2)]
                for mirror in mirrors:
                    copies.register_remote(mirror)
                output = io.StringIO()
                answer = subprocess.CompletedProcess(["ssh"], status, remote_output, detail)
                failure = subprocess.TimeoutExpired("ssh", 10) if cause == "timeout" else None
                with mock.patch.object(subprocess, "run", return_value=answer, side_effect=failure) as run, \
                        redirect_stdout(output):
                    for attempt in range(2):
                        if cause in ("refused", "authentication", "remote-refused", "missing-status"):
                            with self.assertRaises(copies.RemoteMirrorKept) as refused:
                                copies.remove_trees("alpha")
                            self.assertIn(detail, str(refused.exception))
                        else:
                            copies.remove_trees("alpha")
                            self.assertEqual(run.call_count, attempt + 1)
                for mirror in mirrors:
                    fields = cast(dict[str, object], json.loads(copies.remote_record_path(mirror).read_text()))
                    if cause in ("refused", "authentication", "remote-refused", "missing-status"):
                        self.assertNotIn("removal", fields)
                    else:
                        self.assertEqual(fields["removal"], "deferred")
                        self.assertIn(f"{mirror.host}:{mirror.path}", output.getvalue())
                        self.assertIn("next use of the host", output.getvalue())
                    copies.unregister_remote(mirror.host, mirror.path)

    def test_registration_retries_deferred_removal_without_removing_a_reused_mirror(self) -> None:
        for action in ("remove", "reuse", "refused", "remote-refused", "missing-status"):
            root = self.root / action
            root.mkdir()
            with self.subTest(action=action), local_remote_transport(root), warm_claim_servers(), ExitStack() as contexts:
                mirror = self.remote(path=str(root / "mirror"))
                self.assertEqual(publish_mirror(mirror), 0)
                payload = Path(mirror.path) / "saved-work"
                write_text_atomic(payload, "retained work\n")
                path = copies.remote_record_path(mirror)
                fields = cast(dict[str, object], json.loads(path.read_text()))
                fields["removal"] = "deferred"
                write_text_atomic(path, json.dumps(fields) + "\n")
                active = mirror if action == "reuse" else self.remote(path=str(root / "active"))
                if action == "refused":
                    contexts.enter_context(copies.local_mirror_lease(Path(mirror.path), "busy-worker"))
                if action in ("remote-refused", "missing-status"):
                    remote_output = "rc=2\n" if action == "remote-refused" else ""
                    _ = contexts.enter_context(mock.patch.object(subprocess, "run", return_value=
                        subprocess.CompletedProcess(["ssh"], 0, remote_output, "mirror is busy")))
                error = io.StringIO()
                with redirect_stderr(error):
                    copies.register_remote(active)
                if action == "remove":
                    self.assertEqual(copies.owned_remote_mirrors("alpha"), [active])
                    self.assertFalse(Path(mirror.path).exists())
                elif action == "reuse":
                    self.assertNotIn("removal", cast(dict[str, object], json.loads(path.read_text())))
                    self.assertEqual(payload.read_text(), "retained work\n")
                    self.assertEqual(error.getvalue(), "")
                else:
                    self.assertEqual(cast(dict[str, object], json.loads(path.read_text()))["removal"], "deferred")
                    self.assertEqual(payload.read_text(), "retained work\n")
                    self.assertEqual(len(error.getvalue().splitlines()), 1)
                    self.assertIn("anomaly:", error.getvalue())
                    self.assertIn(mirror.path, error.getvalue())

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
                    self.assertEqual(protected.busy, frozenset({self.mirror}))
                    self.assertEqual(protected.held, frozenset({idle}))
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
                self.assertFalse(protected.busy)
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

    def test_receiver_takes_lease_then_reads_claims_then_locks_record_and_keeps_metadata(self) -> None:
        args = copies.CopyArguments()
        args.path, args.action = str(self.mirror), "mirror-receive"
        args.owner, args.repository, args.session = "alpha", "repository", "receiver-session"
        args.command = [sys.executable, "-c", "pass"]
        claims_read = Event()
        real_write = write_text_atomic

        def claims(_timeout: float) -> floor_claims.ClaimSnapshot:
            with file_lock.exclusive(floor_claims.mirror_lock_path(self.mirror), wait=False) as state:
                self.assertIs(state, file_lock.LockResult.BUSY)
            claims_read.set()
            return floor_claims.ClaimSnapshot(frozenset())

        def record(path: Path, content: str) -> None:
            if path.name == ".copy-record.json":
                self.assertTrue(claims_read.is_set())
                lock = self.mirror.parent / ("." + self.mirror.name + ".copy-record.lock")
                with file_lock.exclusive(lock, wait=False) as state:
                    self.assertIs(state, file_lock.LockResult.BUSY)
            real_write(path, content)

        with mock.patch.object(floor_claims, "read_claims", side_effect=claims), \
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
    def open_file_process(self, path: Path, mode: str = "r") -> Generator[subprocess.Popen[str]]:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text("held\n")
        child = subprocess.Popen([
            sys.executable, "-c",
            "import signal,sys; held=open(sys.argv[1],sys.argv[2]); print('ready',flush=True); signal.pause()",
            str(path), mode,
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
        for mode in ("r", "w", "r+"):
            with self.subTest(mode=mode), self.open_file_process(self.mirror / "target/output", mode):
                self.assertEqual(copies.mirror_has_process(self.mirror,
                    copies.scan_mirror_processes((self.mirror,))), mode != "r")
        for how in (tmp_sweep.WORKS_IN, tmp_sweep.MAPS_FILE, tmp_sweep.WRITES_FILE, tmp_sweep.HOLDS_FILE):
            uses = tmp_sweep.ProcessUsesKnown([(str(self.mirror / "file"), os.getpid() + 1, how)])
            self.assertEqual(copies.mirror_has_process(self.mirror,
                copies.MirrorProcessScan(uses, frozenset())), how != tmp_sweep.HOLDS_FILE)

    def test_open_file_in_text_prefix_sibling_does_not_keep_the_mirror(self) -> None:
        self.mirror.mkdir(parents=True)
        with self.open_file_process(self.mirror.with_name(self.mirror.name + "-other") / "output"):
            self.assertFalse(copies.mirror_has_process(self.mirror, copies.scan_mirror_processes((self.mirror,))))

    def test_process_below_the_filesystem_root_keeps_a_root_mirror(self) -> None:
        uses = tmp_sweep.ProcessUsesKnown([("/var/tmp/output", os.getpid() + 1, tmp_sweep.WORKS_IN)])
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
        self.assertEqual(entries, {"copies/copies.py", "production/add_unit.py", "agents/shutdown.py",
            "style/add_style.py", "worktree/make_worktree.sh", "worktree/worktrees.py",
            "bevy_migration_plan/bevy_migration_ensure_repo.sh"})

if __name__ == "__main__":
    _ = unittest.main()
