"""Merge gate suites contracts."""

from __future__ import annotations

import unittest
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
from typing import cast, final
from unittest import mock
from scripts.lib.process_group import send_process_group
from scripts.production import merge_gate
from scripts.direct.wait_for_test_state import wait_until
from scripts.production.merge_gate_test_support import gate_suites
from scripts.production.merge_gate_suites_test_support import GateSuitesFixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


@final
class MergeGateSignalsTests(GateSuitesFixture):
    def test_module_timeout_removes_fixture_and_nested_gate_folders(self) -> None:
        parent = self.root / "nested-temporary"
        parent.mkdir()
        self.environment["TMPDIR"] = str(parent)
        record = self.state / "nested-folders.json"
        module = self.checkout / "scripts" / "hooks" / "test_first.py"
        self.write(module, f'''import json
from pathlib import Path
import signal
import sys
from contextlib import contextmanager

sys.path.insert(0, {str(Path(merge_gate.__file__).parents[2])!r})
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
''')
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
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("merge undone", result.stderr)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
        self.assert_not_pushed()
        self.assertEqual(list(parent.iterdir()), [], result.stdout + result.stderr)

    def test_failed_gate_removes_its_folder(self) -> None:
        self.assert_gate_folder_removed("failure")

    def test_sigterm_during_folder_removal_leaves_no_folder(self) -> None:
        self.assert_gate_folder_removed("SIGTERM")

    def test_sighup_during_folder_removal_leaves_no_folder(self) -> None:
        self.assert_gate_folder_removed("SIGHUP")

    def test_sigint_during_folder_removal_leaves_no_folder(self) -> None:
        self.assert_gate_folder_removed("SIGINT")

    @gate_suites("hooks")
    def test_stop_after_launch_reaps_child_before_returning_capacity(self) -> None:
        self.supply_jobserver(b"Q")
        self.write(self.state / "hold-module", "hold")
        start = merge_gate.start_gate_job
        launched: list[merge_gate.RunningGateJob] = []

        def stop_after_start(suite: str, path: Path, argv: list[str], repo: Path,
                             environment: dict[str, str], log: Path, *,
                kind: merge_gate.GateJobKind = merge_gate.GateJobKind.TEST_MODULE) -> merge_gate.RunningGateJob:
            job = start(suite, path, argv, repo, environment, log, kind=kind)
            if suite == "hooks":
                launched.append(job)
                wait_until((self.state / "module-held").exists, self.state,
                           timeout=TEST_HANG_GUARD_SECS, processes=(job.process,))
                os.kill(os.getpid(), signal.SIGTERM)
            return job

        try:
            with mock.patch.object(merge_gate, "start_gate_job", side_effect=stop_after_start):
                result = self.run_gate("--tests-only")
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("interrupted by SIGTERM", result.stderr)
            self.assertEqual(len(launched), 1)
            self.assertIsNotNone(launched[0].process.poll(), "the runner left its launched module alive")
            self.assertEqual(self.jobserver_tokens(), b"Q")
            self.assert_gate_released()
        finally:
            for job in launched:
                if job.process.poll() is None:
                    send_process_group(job.process.pid, signal.SIGKILL)
                _ = job.process.wait(timeout=TEST_HANG_GUARD_SECS)
                job.output.close()

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
            self.assertEqual(process.returncode, 1, output + errors)
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
        for sig in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(sig=sig):
                child = 0
                (self.state / "module-held").unlink(missing_ok=True)
                self.write(self.state / "hold-module", "hold")
                process = subprocess.Popen(
                    [*self.gate_command(), "--repo", str(self.checkout), "--merge-branch", "integration",
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
                    process.send_signal(sig)
                    output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                    self.assertEqual(process.returncode, 1, output + errors)
                    self.assertIn("merge undone", errors)
                    self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
                    self.assertFalse(Path(f"/proc/{child}").exists())
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
            self.assertEqual(process.returncode, 1, output + errors)
            self.assertIn("merge undone", errors)
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
