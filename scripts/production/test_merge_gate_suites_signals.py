"""Merge gate suites contracts."""

from __future__ import annotations

import unittest
from contextlib import ExitStack, redirect_stdout
import io
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
from typing import cast, final
from unittest import mock
from scripts.lib import build_folders
from scripts.lib.process_group import send_process_group
from scripts.production import merge_gate
from scripts.direct.wait_for_test_state import wait_until
from scripts.production.merge_gate_test_support import gate_suites
from scripts.production.merge_gate_suites_test_support import GateSuitesFixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


@final
class MergeGateSignalsTests(GateSuitesFixture):
    def test_recovery_kills_a_term_ignoring_member_after_its_leader_ends(self) -> None:
        self.supply_jobserver(b"Q")
        source = """import subprocess, sys
child = subprocess.Popen([sys.executable, '-c',
    'import os, signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); print(os.getpid(), flush=True); signal.pause()'],
    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
print(child.stdout.readline(), end='', flush=True)
sys.stdin.read(1)
"""
        leader = subprocess.Popen([sys.executable, "-c", source], env=self.environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True)
        member = 0
        try:
            output = leader.stdout
            assert output is not None
            member = int(cast(str, output.readline()))
            recovery = merge_gate.GateRecovery(self.state / "dead-gate.json", str(self.checkout),
                                               leader.pid, "ended-owner")
            recovery.add_group(leader.pid)
            stdout, stderr = leader.communicate(input="x", timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(leader.returncode, 0, stdout + stderr)
            folder = self.root / "abandoned-gate"
            folder.mkdir()
            self.write(folder / "module-output", "unfinished")
            recovery.folders.append(str(folder))
            _ = self.git(self.checkout, "merge", "--no-edit", self.checkpoint)
            recovery.rollback = self.base
            recovery.rollback_head = self.git(self.checkout, "rev-parse", "HEAD")
            recovery.rollback_source = self.checkpoint
            descriptor = os.open(self.environment["BUILDLOG_JOBSERVER_FIFO"], os.O_RDWR | os.O_NONBLOCK)
            try:
                with mock.patch.dict(os.environ, self.environment, clear=True):
                    recovery.tokens.append(recovery.token(descriptor, os.read(descriptor, 1)))
                    recovery.save()
                    recovery.recover()
            finally:
                os.close(descriptor)
            self.assert_process_stopped(member)
            self.assertEqual(self.jobserver_tokens(), b"Q")
            self.assertEqual(list(self.root.glob("abandoned-gate")), [])
            self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
            self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")
            self.assertEqual(list(self.state.glob("dead-gate.json")), [])
        finally:
            if leader.poll() is None:
                leader.kill()
            _ = leader.communicate(timeout=TEST_HANG_GUARD_SECS)
            if member:
                try:
                    send_process_group(leader.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

        for live_holder in (False, True):
            with self.subTest(live_holder=live_holder), mock.patch.dict(os.environ, self.environment, clear=True):
                with ExitStack() as holder:
                    if live_holder:
                        _ = holder.enter_context(merge_gate.gate_recovery(self.checkout))
                    directory = merge_gate.gate_run_directory(self.checkout)
                    directory.mkdir(parents=True, exist_ok=True)
                    recovery = merge_gate.GateRecovery(directory / "dead-owner.json", str(self.checkout),
                                                       leader.pid, "ended-owner")
                    folder = self.root / "abandoned-gate"
                    folder.mkdir()
                    self.write(folder / "module-output", "unfinished")
                    recovery.folders.append(str(folder))
                    _ = self.git(self.checkout, "merge", "--no-edit", self.checkpoint)
                    merged = self.git(self.checkout, "rev-parse", "HEAD")
                    recovery.rollback = self.base
                    recovery.rollback_head = merged
                    recovery.rollback_source = self.checkpoint
                    descriptor = os.open(self.environment["BUILDLOG_JOBSERVER_FIFO"], os.O_RDWR | os.O_NONBLOCK)
                    try:
                        recovery.tokens.append(recovery.token(descriptor, os.read(descriptor, 1)))
                        recovery.save()
                    finally:
                        os.close(descriptor)
                    if live_holder:
                        output = io.StringIO()
                        with redirect_stdout(output):
                            recovery.recover()
                        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), merged)
                        record = cast(dict[str, object], json.loads(recovery.path.read_text()))
                        self.assertEqual(record["rollback"], self.base)
                        self.assertEqual(record["groups"], {})
                        self.assertEqual(record["tokens"], [])
                        self.assertEqual(record["folders"], [])
                        self.assertEqual(len(output.getvalue().splitlines()), 1)
                        self.assertIn("rollback waits for the live gate", output.getvalue())
                    else:
                        with merge_gate.gate_recovery(self.unit):
                            self.assertEqual(self.jobserver_tokens(), b"Q")
                    self.assertEqual(self.jobserver_tokens(), b"Q")
                    self.assertFalse(folder.exists())
                with merge_gate.gate_recovery(self.checkout):
                    self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
                    self.assertFalse(recovery.path.exists())
                self.assertEqual(list(directory.glob("*.json")), [])

    def test_module_timeout_removes_fixture_and_nested_gate_folders(self) -> None:
        parent = self.root / "nested-temporary"
        parent.mkdir()
        self.environment["TMPDIR"] = str(parent)
        record = self.state / "nested-folders.json"
        module = self.checkout / "scripts" / "hooks" / "test_first.py"
        program = f'''import json
from pathlib import Path
import signal
import sys
from contextlib import contextmanager

with open(Path({str(Path(merge_gate.__file__).parents[2])!r}) / "scripts/lib/package_root.py") as package_root:
    exec(package_root.read(), {{"__file__": package_root.name}})
from scripts.lib import build_folders
from scripts.production import merge_gate
from scripts.production.merge_gate_test_support import MergeGateFixture

MergeGateFixture.setUpClass()
allocate = build_folders.temporary_build_folder

@contextmanager
def hold_nested_gate(request):
    with allocate(request) as folder:
        Path({str(record)!r}).write_text(json.dumps([
            str(MergeGateFixture.fixture_root.parent), str(folder)]))
        signal.pause()
        yield folder

build_folders.temporary_build_folder = hold_nested_gate
merge_gate.run_suites(Path.cwd(), full=True)
'''
        helper = self.bin / "nested-gate.py"
        self.write(helper, program)
        shim = self.bin.parent / "scripts/lib/py"
        self.write(module, "import os\n" + f"os.execv({str(shim)!r}, [{str(shim)!r}, {str(helper)!r}])\n")
        result = self.run_gate("--tests-only", "--module-time-limit-s", "10", own_process=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("killed: still running after 10 s, the module time limit", result.stdout)
        paths = cast(list[str], json.loads(record.read_text()))
        self.assertEqual(len(paths), 2)
        for value in paths:
            self.addCleanup(shutil.rmtree, Path(value), True)
        for value in paths:
            path = Path(value)
            self.assertTrue(path.is_relative_to(parent), str(path))
        self.assertEqual(list(parent.iterdir()), [])

    def test_cancellation_sends_term_to_all_jobs_before_reaping(self) -> None:
        jobs: list[merge_gate.RunningGateJob] = []
        for name in ("first", "second"):
            job = merge_gate.start_gate_job(
                "hooks", Path(f"test_{name}.py"),
                [sys.executable, "-c", "import signal; signal.pause()"],
                self.checkout, self.environment, self.state / f"{name}.log")
            self.addCleanup(job.output.close)
            self.addCleanup(merge_gate.kill_job_group, job)
            jobs.append(job)
        sent: list[int] = []
        reap = merge_gate.reap_gate_job

        def send_record(pid: int, number: int) -> None:
            if number == signal.SIGTERM:
                sent.append(pid)
            send_process_group(pid, number)

        def reap_after_all_terms(job: merge_gate.RunningGateJob, *, block: bool = False) -> None:
            self.assertEqual(sent, [item.process.pid for item in jobs])
            reap(job, block=block)

        with mock.patch("scripts.production.merge_gate.process_group.send_process_group", side_effect=send_record), \
                mock.patch.object(merge_gate, "reap_gate_job", side_effect=reap_after_all_terms):
            merge_gate.stop_gate_jobs(jobs)
        for job in jobs:
            self.assertIsNotNone(job.process.returncode)

    def assert_gate_folder_removed(self, ending: str) -> None:
        parent = self.root / "gate-temporary"
        parent.mkdir()
        self.environment["TMPDIR"] = str(parent)
        if ending == "failure":
            self.write(self.state / "red-hooks", "red")
        number = 0 if ending == "failure" else signal.Signals[ending].value
        preamble = f'''import os
from pathlib import Path
import sys
from scripts.lib import build_folders

def cleanup_event(event, arguments):
    if event != "shutil.rmtree" or arguments[1] is None:
        return
    if not Path(arguments[0]).name.startswith(build_folders.FOLDER_PREFIXES["merge-gate"]):
        return
    if {number}:
        os.kill(os.getpid(), {number})

sys.addaudithook(cleanup_event)
'''
        result = self.run_gate(own_process=True, preamble=preamble)
        if number:
            # The entry point converts the default SIGINT exception to status 1.
            expected = 1 if number == signal.SIGINT else -number
            self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
            [folder] = [entry for entry in parent.iterdir() if entry.is_dir()]
            lock = folder.with_name(folder.name + ".lock")
            lease = folder.with_name(folder.name + ".lease.json")
            self.assertEqual(set(parent.iterdir()), {folder, lock, lease})
            record = cast(dict[str, object], json.loads(lease.read_text()))
            self.assertIsInstance(record["pid"], int)
            self.assertEqual(build_folders.build_folder_state(folder), "free")
            recovered = self.run_gate("--tests-only", "--full", own_process=True)
            self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
            self.assertIn("merge undone", result.stderr + recovered.stdout)
        else:
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("merge undone", result.stderr)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
        self.assert_not_pushed()
        self.assertEqual(list(parent.iterdir()), [], result.stdout + result.stderr)

    def test_failed_gate_removes_its_folder(self) -> None:
        self.assert_gate_folder_removed("failure")

    def test_sigterm_during_folder_removal_leaves_a_recorded_folder(self) -> None:
        self.assert_gate_folder_removed("SIGTERM")

    def test_sighup_during_folder_removal_leaves_a_recorded_folder(self) -> None:
        self.assert_gate_folder_removed("SIGHUP")

    def test_sigint_during_folder_removal_leaves_a_recorded_folder(self) -> None:
        self.assert_gate_folder_removed("SIGINT")

    @gate_suites("hooks")
    def test_stop_after_launch_reaps_child_before_returning_capacity(self) -> None:
        self.supply_jobserver(b"Q")
        self.write(self.state / "hold-module", "hold")
        launched = self.state / "launched-job"
        preamble = f"""start = merge_gate.start_gate_job
def stop_after_start(*arguments, **options):
    job = start(*arguments, **options)
    if arguments[0] == "hooks":
        from scripts.direct.wait_for_test_state import wait_until
        wait_until((Path(os.environ["MERGE_GATE_TEST_STATE"]) / "module-held").exists,
                   Path(os.environ["MERGE_GATE_TEST_STATE"]), timeout={TEST_HANG_GUARD_SECS})
        Path({str(launched)!r}).write_text(str(job.process.pid))
        os.kill(os.getpid(), signal.SIGTERM)
    return job
merge_gate.start_gate_job = stop_after_start
"""
        result = self.run_gate("--tests-only", own_process=True, preamble=preamble)
        self.assertEqual(result.returncode, -signal.SIGTERM, result.stdout + result.stderr)
        self.assertEqual(len(launched.read_text().splitlines()), 1)
        (self.state / "hold-module").unlink()
        recovered = self.run_gate("--tests-only", "--full", own_process=True)
        self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
        self.assert_process_stopped(int(launched.read_text()))
        self.assertEqual(self.jobserver_tokens(), b"Q")
        self.assert_gate_released()

    @gate_suites("hooks", "lib")
    def test_stop_with_pending_modules_returns_owned_slot_and_reservation(self) -> None:
        self.supply_jobserver(b"S")
        self.write(self.state / "hold-module", "hold")
        process = subprocess.Popen(
            [*self.gate_command(), "--tests-only", "--repo", str(self.checkout)],
            env=self.environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        try:
            wait_until(lambda: (self.state / "module-held").exists() or process.poll() is not None,
                       self.state, timeout=TEST_HANG_GUARD_SECS, processes=(process,))
            self.assertTrue((self.state / "module-held").exists())
            self.assertEqual(self.jobserver_tokens(), b"")
            self.assertEqual(len(self.admission.server.reservations), 1)
            process.send_signal(signal.SIGTERM)
            output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(process.returncode, -signal.SIGTERM, output + errors)
            (self.state / "hold-module").unlink()
            recovered = self.run_gate("--tests-only", "--full", own_process=True)
            self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
            self.assertEqual(self.jobserver_tokens(), b"S")
            self.assert_gate_released()
            self.assert_process_stopped(int((self.state / "module-held").read_text()))
        finally:
            if process.poll() is None:
                process.kill()
            _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            held = self.state / "module-held"
            if held.exists():
                try:
                    send_process_group(int(held.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_finished_module_leaves_no_running_child(self) -> None:
        self.write(self.state / "leak-child", "leak")
        result = self.run_gate("--tests-only")
        child = int((self.state / "child-pid").read_text())
        try:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assert_process_stopped(child)
        finally:
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def test_termination_signals_roll_back_and_clean_module_groups(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with self.subTest(sig=sig):
                child = 0
                (self.state / "module-held").unlink(missing_ok=True)
                self.write(self.state / "hold-module", "hold")
                self.write(self.state / "leak-child", "leak")
                self.write(self.state / "detached-child", "detached")
                # Each signal gets its own pool, so a failed subtest leaves no token for the next.
                pool = self.root / f"jobserver-{sig.name}"
                os.mkfifo(pool)
                self.addCleanup(os.close, os.open(pool, os.O_RDWR | os.O_NONBLOCK))
                self.environment["BUILDLOG_JOBSERVER_FIFO"] = str(pool)
                self.supply_jobserver(b"Q")
                parent = self.root / f"interrupted-{sig.name}"
                parent.mkdir()
                self.environment["TMPDIR"] = str(parent)
                # The supported whole-machine path has no queue-owned handlers.
                preamble = """signal.signal(signal.SIGINT, signal.SIG_DFL)
def whole_gate(repo, type_dirs, module_time_limit_s, **options):
    with merge_gate.shared_cpu_pool() as pool:
        pool.try_acquire()
        return merge_gate.run_suites(repo, type_dirs, module_time_limit_s, machine=merge_gate.WholeMachine())
merge_gate.run_gate_suites = whole_gate
"""
                process = subprocess.Popen(
                    [*self.gate_command(preamble=preamble), "--repo", str(self.checkout), "--merge-branch", "integration",
                     "--unit-branch", "unit-phase", "--checkpoint", self.checkpoint, "--previous-checkpoint", self.base,
                     "--unit", "fixture", "--phase", "repair",
                     "--production", "quiet", "--type-dirs", "scripts"], env=self.environment, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    wait_until(lambda: (self.state / "module-held").exists() or process.poll() is not None,
                               self.state, timeout=TEST_HANG_GUARD_SECS, processes=(process,))
                    if not (self.state / "module-held").exists():
                        self.fail("fixture module never became ready")
                    child = int((self.state / "module-held").read_text())
                    leftover = int((self.state / "child-pid").read_text())
                    fields = Path(f"/proc/{leftover}/stat").read_text().rsplit(")", 1)[1].split()
                    self.assertEqual(fields[2:4], [str(leftover), str(leftover)])
                    self.assertEqual(self.jobserver_tokens(), b"")
                    process.send_signal(sig)
                    output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                    self.assertEqual(process.returncode, -sig, output + errors)
                    self.assert_process_stopped(child)
                    records = list((self.state / "runner/active-runs").glob("*/*.json"))
                    self.assertEqual(len(records), 1)
                    record = cast(dict[str, object], json.loads(records[0].read_text()))
                    self.assertEqual(record["pid"], process.pid)
                    self.assertEqual(record["rollback"], self.base)
                    folders = [Path(folder) for folder in cast(list[str], record["folders"])]
                    self.assertTrue(folders)
                    self.assertTrue(all(folder.is_dir() for folder in folders))
                    (self.state / "hold-module").unlink()
                    (self.state / "leak-child").unlink()
                    if sig == signal.SIGTERM:
                        merged = self.git(self.checkout, "rev-parse", "HEAD")
                        self.write(self.checkout / "later.txt", "an unrelated later commit")
                        later = self.commit(self.checkout, "Add an unrelated later commit")
                        refused = self.run_gate("--tests-only", "--full", own_process=True)
                        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
                        self.assertIn("differs from the recorded merge", refused.stderr)
                        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), later)
                        self.assertTrue(records[0].is_file())
                        _ = self.git(self.checkout, "reset", "--hard", merged)
                    recovered = self.run_gate("--tests-only", "--full", own_process=True)
                    self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
                    self.assertIn("merge undone", recovered.stdout)
                    self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
                    self.assert_process_stopped(child)
                    self.assert_process_stopped(leftover)
                    self.assertTrue(all(not folder.exists() for folder in folders))
                    self.assertEqual(list((self.state / "runner/active-runs").glob("*/*.json")), [])
                    self.assertEqual(list(parent.iterdir()), [])
                    self.assertEqual(self.jobserver_tokens(), b"Q")
                    self.assert_not_pushed()
                finally:
                    if process.poll() is None:
                        process.kill()
                    _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                    if child:
                        try:
                            send_process_group(child, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    _ = self.git(self.checkout, "reset", "--keep", self.base)

    def test_signal_during_merge_aborts_pending_merge_and_its_hook(self) -> None:
        ready = self.state / "merge-hook-ready"
        hook = self.checkout / ".git" / "hooks" / "prepare-commit-msg"
        # The pid is published by rename, so the test never reads a created but
        # still empty file.
        self.write(hook, f"#!{sys.executable}\nimport os, signal\nfrom pathlib import Path\n"
                   + f"Path({str(ready) + '.tmp'!r}).write_text(str(os.getpid()))\n"
                   + f"os.replace({str(ready) + '.tmp'!r}, {str(ready)!r})\nsignal.pause()\n")
        hook.chmod(0o755)
        process = subprocess.Popen(
            [*self.gate_command(), "--repo", str(self.checkout), "--merge-branch", "integration",
             "--unit-branch", "unit-phase", "--checkpoint", self.checkpoint, "--previous-checkpoint", self.base,
             "--unit", "fixture", "--phase", "repair",
             "--production", "quiet"],
            env=self.environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        hook_pid = 0
        try:
            wait_until(lambda: ready.exists() or process.poll() is not None,
                       self.state, timeout=TEST_HANG_GUARD_SECS, processes=(process,))
            if not ready.exists():
                self.fail("merge hook did not become ready")
            hook_pid = int(ready.read_text())
            process.send_signal(signal.SIGTERM)
            output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(process.returncode, -signal.SIGTERM, output + errors)
            hook.unlink()
            recovered = self.run_gate("--tests-only", "--full", own_process=True)
            self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
            self.assertIn("merge undone", recovered.stdout)
            self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
            self.assertFalse((self.checkout / ".git" / "MERGE_HEAD").exists())
            self.assert_process_stopped(hook_pid)
            self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")
            self.assert_not_pushed()
        finally:
            if process.poll() is None:
                process.kill()
            _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            if hook_pid:
                try:
                    os.kill(hook_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


if __name__ == "__main__":
    _ = unittest.main()
