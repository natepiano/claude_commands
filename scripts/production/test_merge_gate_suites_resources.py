"""Merge gate suites contracts."""

from __future__ import annotations
from scripts.build_queue import admission_records

from scripts.build_queue import queue_place, admission_protocol as wire

import unittest
from concurrent.futures import ThreadPoolExecutor
import io
import json
import fcntl
import os
import stat
from pathlib import Path
import resource
import select
import signal
import sys
import time
import threading
from typing import final
from contextlib import redirect_stdout
from unittest import mock
from scripts.production import merge_gate
from scripts.production.gate_modules import GateRefused
from scripts.production.merge_gate_test_support import SUITE_MODULE, gate_suites, wait_for_file
from scripts.production.merge_gate_suites_test_support import GateSuitesFixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


CPU_WORKLOAD = '''
import subprocess

def spin_cpu(seconds):
    started = time.process_time()
    while time.process_time() - started < seconds:
        pass
    return time.process_time() - started

class CpuUsageTest(unittest.TestCase):
    def test_measured_cpu_work(self):
        work = json.loads((state / "cpu-work.json").read_text())[suite + "/" + module_name]
        used = spin_cpu(work["leader"])
        if work["child"]:
            child_record = state / (module.stem + "-child-cpu")
            subprocess.run([sys.executable, str(module), str(work["child"]), str(child_record)], check=True)
            used += float(child_record.read_text())
        time.sleep(work["sleep"])
        measured = state / "cpu-measured" / suite / module_name
        measured.parent.mkdir(parents=True, exist_ok=True)
        measured.write_text(str(used))

if __name__ == "__main__":
    Path(sys.argv[2]).write_text(str(spin_cpu(float(sys.argv[1]))))
'''
from scripts.lib import cancellation, copy_record, scratch
from scripts.message.process_test_support import OwnedTestProcesses


@final
class MergeGateResourcesTests(GateSuitesFixture):
    @gate_suites("lib", "hooks")
    def test_memory_limit_stops_only_the_over_limit_module_beside_others(self) -> None:
        self.configure_gate("merge_gate_governor_gib=2")
        self.record_module_peaks({
            "lib/test_first.py": 100.0, "lib/test_second.py": 100.0, "hooks/test_first.py": 100.0,
        })
        output = io.StringIO()
        with mock.patch.dict(os.environ, self.environment), self.sampling_module_memory({
                "lib/test_first.py": (2560.0, 64.0),
                "lib/test_second.py": (2048.0,),
                "hooks/test_first.py": (32.0,),
        }) as started, redirect_stdout(output):
            run = merge_gate.run_suites(self.checkout, max_running_jobs=3)
        self.assertTrue(run.passed, output.getvalue())
        stopped = [job for job in started if job.killed_for]
        self.assertEqual([(job.suite, job.module) for job in stopped], [("lib", "test_first.py")])
        self.assertEqual(stopped[0].process.returncode, -signal.SIGTERM)
        self.assert_process_stopped(stopped[0].process.pid)
        events = self.module_events()
        initial = [event for event in events[:3] if event["event"] == "start"]
        self.assertEqual(len(initial), 3, events)
        self.assertEqual([event["event"] for event in events[-2:]], ["start", "finish"])
        self.assertEqual(events[-2]["module"], "test_first.py")
        peaks = merge_gate.read_measures(self.state / "runner/module_peak_mib.json")
        self.assertEqual(peaks, {"lib/test_first.py": 64.0, "lib/test_second.py": 2048.0, "hooks/test_first.py": 32.0})
        self.assertIn("peak memory 2.500 GiB exceeds the memory limit of 2 GiB", output.getvalue())
        messages = [call for call in self.calls() if call["tool"] == "send.py"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["args"][:2], ["--to", "sdlc"])
        text = messages[0]["args"][messages[0]["args"].index("--text") + 1]
        for detail in ("lib/test_first.py", "2.500 GiB", "limit 2 GiB", "merge-gate", "integration", "runs again alone"):
            self.assertIn(detail, text)

    def test_memory_stop_fails_even_when_the_module_exits_zero(self) -> None:
        for exit_status in (0, 5):
            with self.subTest(exit_status=exit_status):
                self.restore_fixture(("hooks",))
                module = self.checkout / "scripts/hooks/test_first.py"
                self.write(module, module.read_text().replace(
                    '        if (state / "sample-module-memory").exists():',
                    '        def exit_zero(signum, frame):\n'
                    + '            del signum, frame\n'
                    + '            print("Ran 0 tests in 0.000s", flush=True)\n'
                    + '            print("NO TESTS RAN", flush=True)\n'
                    + f'            os._exit({exit_status})\n'
                    + '        signal.signal(signal.SIGTERM, exit_zero)\n'
                    + '        if (state / "sample-module-memory").exists():', 1))
                self.configure_gate("merge_gate_governor_gib=4")
                output = io.StringIO()
                with mock.patch.dict(os.environ, self.environment), \
                        self.sampling_module_memory({"hooks/test_first.py": (5120.0,)}), redirect_stdout(output):
                    run = merge_gate.run_suites(self.checkout)
                self.assertFalse(run.passed, output.getvalue())
                self.assertEqual(len(run.executions), 2)
                for result in run.executions:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("peak memory 5.000 GiB", result.killed_for)
                self.assertIn("the gate fails", [call for call in self.calls() if call["tool"] == "send.py"][-1]["args"][-1])

    @gate_suites("hooks")
    def test_idle_gate_blocks_for_one_slot_and_reports_one_wait(self) -> None:
        for hold_during_wait in (False, True):
            with self.subTest(hold_during_wait=hold_during_wait):
                self.restore_fixture(("hooks",))
                self.supply_jobserver(b"")
                waiting = threading.Event()
                select_ready = select.select
                acquire = merge_gate.SharedCPUPool.acquire
                blocking: list[bool] = []
                held = False
                hold_waits: list[bool] = []

                def acquire_slot(pool: merge_gate.SharedCPUPool) -> merge_gate.SharedCPUSlot:
                    def observe_wait(readers: list[int], writers: list[int], errors: list[int],
                                     timeout: float | None = None) -> tuple[list[int], list[int], list[int]]:
                        nonlocal held
                        if pool.blocking in readers:
                            blocking.append(os.get_blocking(pool.blocking))
                            self.assertFalse(signal.pthread_sigmask(signal.SIG_BLOCK, set()).intersection(
                                {signal.SIGTERM, signal.SIGHUP, signal.SIGINT}))
                            held = hold_during_wait
                            waiting.set()
                        return select_ready(readers, writers, errors, timeout)

                    with mock.patch.object(select, "select", side_effect=observe_wait):
                        return acquire(pool)

                def release_hold() -> None:
                    nonlocal held
                    if held:
                        self.assertEqual(self.module_events(), [])
                        self.assertEqual(self.jobserver_tokens(), b"Q")
                        hold_waits.append(True)
                        held = False

                def return_slot() -> None:
                    self.assertTrue(waiting.wait(TEST_HANG_GUARD_SECS))
                    self.assertEqual(self.module_events(), [])
                    self.supply_jobserver(b"Q")

                with ThreadPoolExecutor(max_workers=1) as executor, \
                        mock.patch.object(merge_gate.SharedCPUPool, "acquire", acquire_slot), \
                        mock.patch.object(merge_gate.BuildHoldWait, "admits", side_effect=lambda: not held), \
                        mock.patch.object(merge_gate.BuildHoldWait, "wait_until_free", side_effect=release_hold):
                    released = executor.submit(return_slot)
                    result = self.run_gate("--tests-only")
                    _ = released.result(timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(blocking, [True])
                self.assertEqual(hold_waits, [True] if hold_during_wait else [])
                self.assertEqual(result.stdout.count("waits for a CPU slot"), 1)
                self.assertEqual(result.stdout.count("CPU slot wait ended after"), 1)
                self.assert_all_modules_started()
                self.assertEqual(self.jobserver_tokens(), b"Q")

    def test_stop_during_a_blocking_read_returns_every_owned_slot(self) -> None:
        for delivery in ("before_wait", "process", "sender_thread"):
            with self.subTest(delivery=delivery):
                self.supply_jobserver(b"A")
                waiting = threading.Event()
                select_ready = select.select

                def stop() -> None:
                    self.assertTrue(waiting.wait(TEST_HANG_GUARD_SECS))
                    if delivery == "process":
                        os.kill(os.getpid(), signal.SIGINT)
                    elif delivery == "sender_thread":
                        signal.pthread_kill(threading.get_ident(), signal.SIGINT)

                with ThreadPoolExecutor(max_workers=1) as executor:
                    stopped = executor.submit(stop)
                    with mock.patch.dict(os.environ, self.environment), \
                            self.assertRaises(KeyboardInterrupt):
                        with merge_gate.shared_cpu_pool() as pool:
                            assert isinstance(pool, merge_gate.SharedCPUPool)
                            self.assertIsInstance(pool.try_acquire(), merge_gate.SharedCPUSlot)
                            blocking_descriptor = pool.blocking

                            def observe_wait(readers: list[int], writers: list[int], errors: list[int],
                                             timeout: float | None = None) -> tuple[list[int], list[int], list[int]]:
                                if blocking_descriptor in readers:
                                    waiting.set()
                                    if delivery == "before_wait":
                                        os.kill(os.getpid(), signal.SIGINT)
                                return select_ready(readers, writers, errors, timeout)

                            with mock.patch.object(select, "select", side_effect=observe_wait):
                                _ = pool.acquire()
                    _ = stopped.result(timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(self.jobserver_tokens(), b"A")

    def test_stop_after_blocking_acquisition_returns_the_new_slot_too(self) -> None:
        for delivery in ("read", "return"):
            with self.subTest(delivery=delivery):
                self.supply_jobserver(b"A\0")
                read = os.read
                with mock.patch.dict(os.environ, self.environment), \
                        self.assertRaises(KeyboardInterrupt):
                    with merge_gate.shared_cpu_pool() as pool:
                        assert isinstance(pool, merge_gate.SharedCPUPool)
                        self.assertIsInstance(pool.try_acquire(), merge_gate.SharedCPUSlot)
                        token_descriptor = pool.descriptor

                        def interrupt_read(descriptor: int, count: int) -> bytes:
                            token = read(descriptor, count)
                            if descriptor == token_descriptor and delivery == "read":
                                os.kill(os.getpid(), signal.SIGINT)
                            return token

                        with mock.patch.object(os, "read", side_effect=interrupt_read):
                            _ = pool.acquire()
                        os.kill(os.getpid(), signal.SIGINT)
                self.assertEqual(self.jobserver_tokens(), b"A\0")

    def test_interrupt_after_reaping_preserves_status_and_cpu_and_allows_cleanup(self) -> None:
        jobs: list[merge_gate.RunningGateJob] = []
        for name, source in (("finished", "raise SystemExit(7)"),
                             ("running", "import signal; signal.pause()")):
            job = merge_gate.start_gate_job(
                "hooks", Path(f"test_{name}.py"), [sys.executable, "-c", source],
                self.checkout, self.environment, self.state / f"{name}.log")
            self.addCleanup(job.output.close)
            self.addCleanup(merge_gate.kill_job_group, job)
            jobs.append(job)
        finished, running = jobs
        _ = os.waitid(os.P_PID, finished.process.pid, os.WEXITED | os.WNOWAIT)
        wait4 = os.wait4

        def interrupt_after_wait(pid: int, options: int) -> tuple[int, int, resource.struct_rusage]:
            result = wait4(pid, options)
            os.kill(os.getpid(), signal.SIGINT)
            return result

        stopped = threading.Event()
        worker = threading.Thread(target=stopped.wait, daemon=True)
        cancellation.start_masked_thread(worker)
        try:
            with mock.patch.object(os, "wait4", side_effect=interrupt_after_wait):
                with self.assertRaises(KeyboardInterrupt):
                    merge_gate.reap_gate_job(finished)
        finally:
            stopped.set()
            worker.join(TEST_HANG_GUARD_SECS)
        self.assertEqual(finished.process.returncode, 7)
        self.assertGreater(finished.cpu_seconds, 0.0)
        for job in jobs:
            merge_gate.kill_job_group(job)
        self.assertEqual(finished.process.returncode, 7)
        self.assertEqual(running.process.returncode, -signal.SIGKILL)

    @gate_suites("lib", "hooks")
    def test_cpu_records_include_waited_children_distinguish_sleep_and_rank_jobs(self) -> None:
        nested = self.checkout / "scripts" / "hooks" / "tests" / "test_cpu.py"
        (self.checkout / "scripts" / "hooks" / "test_first.py").unlink()
        self.write(nested, SUITE_MODULE)
        workloads = {
            "lib/test_first.py": {"leader": 0.4, "child": 0.4, "sleep": 0.0},
            "lib/test_second.py": {"leader": 0.0, "child": 0.0, "sleep": 0.4},
            "hooks/tests/test_cpu.py": {"leader": 0.25, "child": 0.0, "sleep": 0.0},
        }
        self.write(self.state / "cpu-work.json", json.dumps(workloads))
        for key in workloads:
            path = self.checkout / "scripts" / key
            self.write(path, path.read_text() + CPU_WORKLOAD)
        cpu_path = self.state / "runner" / "module_cpu_seconds.json"
        merge_gate.save_measures(cpu_path, {"lib/test_first.py": 99.0, "unselected/test_old.py": 7.0})
        output = io.StringIO()
        with mock.patch.dict(os.environ, self.environment), redirect_stdout(output):
            run = merge_gate.run_suites(self.checkout, type_dirs=("scripts",), full=True)
        self.assertTrue(run.passed, output.getvalue())
        records = merge_gate.read_measures(cpu_path)
        results = {f"{job.suite}/{job.module}": job for job in run.jobs}
        self.assertEqual(set(results), {*workloads, "types/basedpyright"})
        self.assertEqual(records, {**{key: job.cpu_seconds for key, job in results.items()},
                                   "unselected/test_old.py": 7.0})
        for key, work in workloads.items():
            measured = float((self.state / "cpu-measured" / key).read_text())
            self.assertGreaterEqual(measured, work["leader"] + work["child"])
            self.assertGreaterEqual(results[key].cpu_seconds, measured - 0.02, key)
            self.assertLess(results[key].cpu_seconds, measured + 0.5, key)
        sleeper = results["lib/test_second.py"]
        self.assertLess(sleeper.cpu_seconds, 0.25)
        self.assertGreaterEqual(sleeper.seconds, workloads["lib/test_second.py"]["sleep"])
        self.assertGreater(results["types/basedpyright"].cpu_seconds, 0.0)
        ranked = sorted(results, key=lambda key: (-results[key].cpu_seconds, key))
        self.assertEqual(ranked[0], "lib/test_first.py")
        expected = ", ".join(f"{results[key].cpu_seconds:.2f} s {key}" for key in ranked[:3])
        lines = output.getvalue().splitlines()
        peak_line = next(index for index, line in enumerate(lines) if line.startswith("Largest module peak:"))
        self.assertEqual(lines[peak_line + 1],
                         f"Most CPU: {expected}; total {sum(job.cpu_seconds for job in run.jobs):.2f} s")

    def test_cpu_report_counts_both_attempts_but_the_record_keeps_the_rerun(self) -> None:
        path = self.checkout / "scripts" / "hooks" / "test_first.py"
        self.write(path, path.read_text() + CPU_WORKLOAD)
        self.write(self.state / "cpu-work.json", json.dumps({
            "hooks/test_first.py": {"leader": 0.1, "child": 0.0, "sleep": 0.0},
        }))
        self.write(self.state / "fail-first-hooks", "fail")
        attempts: list[merge_gate.GateJobResult] = []
        finish = merge_gate.finish_gate_job

        def record_attempt(job: merge_gate.RunningGateJob, status: int) -> merge_gate.GateJobResult:
            result = finish(job, status)
            attempts.append(result)
            return result

        output = io.StringIO()
        with mock.patch.dict(os.environ, self.environment), \
                mock.patch.object(merge_gate, "finish_gate_job", side_effect=record_attempt), redirect_stdout(output):
            run = merge_gate.run_suites(self.checkout)
        self.assertTrue(run.passed, output.getvalue())
        self.assertEqual([job.returncode for job in attempts], [1, 0])
        self.assertEqual(run.jobs, (attempts[-1],))
        self.assertEqual(merge_gate.read_measures(self.state / "runner" / "module_cpu_seconds.json"),
                         {"hooks/test_first.py": attempts[-1].cpu_seconds})
        total = sum(job.cpu_seconds for job in attempts)
        self.assertGreater(total, attempts[-1].cpu_seconds)
        self.assertIn(f"Most CPU: {total:.2f} s hooks/test_first.py; total {total:.2f} s", output.getvalue())
    def test_shared_pool_accepts_fifo_and_character_device_modes(self) -> None:
        self.supply_jobserver(b"Q")
        metadata = Path(self.environment["BUILDLOG_JOBSERVER_FIFO"]).stat()
        for kind in (stat.S_IFIFO, stat.S_IFCHR):
            mode = os.stat_result((kind | 0o600, *metadata[1:]))
            with self.subTest(kind=kind), mock.patch.dict(os.environ, self.environment), \
                    mock.patch.object(os, "fstat", return_value=mode), merge_gate.shared_cpu_pool() as pool:
                self.assertIsInstance(pool, merge_gate.SharedCPUPool)
                if not isinstance(pool, merge_gate.SharedCPUPool):
                    self.fail("the private pool was not opened")
                slot = pool.try_acquire()
                self.assertIsInstance(slot, merge_gate.SharedCPUSlot)
                self.assertEqual(self.jobserver_tokens(), b"")
            self.assertEqual(self.jobserver_tokens(), b"Q")

    def test_shared_pool_refuses_regular_files_without_consuming_them(self) -> None:
        path = Path(self.environment["BUILDLOG_JOBSERVER_FIFO"])
        path.unlink()
        self.write(path, "Q")
        with mock.patch.dict(os.environ, self.environment), self.assertRaises(GateRefused):
            with merge_gate.shared_cpu_pool():
                self.fail("a regular file must not provide CPU slots")
        self.assertEqual(path.read_text(), "Q")

    def test_empty_shared_pool_does_not_read_an_unready_descriptor(self) -> None:
        self.supply_jobserver(b"")
        metadata = Path(self.environment["BUILDLOG_JOBSERVER_FIFO"]).stat()
        mode = os.stat_result((stat.S_IFCHR | 0o600, *metadata[1:]))
        with mock.patch.dict(os.environ, self.environment), \
                mock.patch.object(os, "fstat", return_value=mode), merge_gate.shared_cpu_pool() as pool:
            if not isinstance(pool, merge_gate.SharedCPUPool):
                self.fail("the private pool was not opened")
            pool_descriptor = pool.descriptor
            read = os.read

            def read_other_descriptor(descriptor: int, count: int) -> bytes:
                self.assertNotEqual(descriptor, pool_descriptor, "read before the pool was ready")
                return read(descriptor, count)

            with mock.patch.object(os, "read", side_effect=read_other_descriptor):
                self.assertIsInstance(pool.try_acquire(), merge_gate.WaitingForCPU)

    @gate_suites("lib")
    def test_one_shared_slot_serializes_modules_and_is_returned_after_each(self) -> None:
        self.supply_jobserver(b"Q")
        start = merge_gate.start_gate_job
        available_at_start: list[bytes] = []

        def observed(suite: str, path: Path, argv: list[str], repo: Path,
                     environment: dict[str, str], log: Path, *,
                kind: merge_gate.GateJobKind = merge_gate.GateJobKind.TEST_MODULE) -> merge_gate.RunningGateJob:
            available_at_start.append(self.jobserver_tokens())
            return start(suite, path, argv, repo, environment, log, kind=kind)

        with mock.patch.object(merge_gate, "start_gate_job", side_effect=observed):
            result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(available_at_start, [b""] * (self.module_count + 1))
        self.assertEqual(self.jobserver_tokens(), b"Q")
        events = self.module_events()
        self.assertEqual([row["event"] for row in events], ["start", "finish"] * self.module_count)

    @gate_suites("hooks")
    def test_missing_shared_pool_runs_all_modules(self) -> None:
        Path(self.environment["BUILDLOG_JOBSERVER_FIFO"]).unlink()
        result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_all_modules_started()
        self.assert_gate_released()

    @gate_suites("hooks")
    def test_checkout_stays_busy_during_memory_and_slot_waits(self) -> None:
        busy = self.occupy_memory_queue()
        self.supply_jobserver(b"")
        waited: set[str] = set()
        admitted = False

        def assert_busy() -> None:
            with scratch.busy_path(self.checkout).open("r") as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def observed(result: admission_records.AdmissionWaitSnapshot) -> None:
            nonlocal admitted
            admitted = result.decision.state == "admit"
            if result.decision.state == "hold":
                assert_busy()
                waited.add("memory")
                with self.admission.loop_turn():
                    replies = self.admission.server.handle_request(wire.ReleaseRequest("release-busy", busy.reservation, 0))
                self.assertEqual(replies, [wire.Ok()])

        acquire = merge_gate.SharedCPUPool.acquire

        def free_slot(pool: merge_gate.SharedCPUPool) -> merge_gate.SharedCPUSlot:
            if admitted and "slot" not in waited:
                assert_busy()
                self.assertEqual(self.module_events(), [])
                waited.add("slot")
                self.supply_jobserver(b"Q")
            return acquire(pool)

        with self.observing_admission(observed), \
                mock.patch.object(merge_gate.SharedCPUPool, "acquire", free_slot), \
                mock.patch.object(copy_record, "copy_folder", return_value=self.checkout):
            result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(waited, {"memory", "slot"})
        self.assertEqual(self.jobserver_tokens(), b"Q")

    @gate_suites("lib")
    def test_governor_admits_two_overlapping_module_processes(self) -> None:
        self.record_module_peaks({"lib/test_first.py": 48.0, "lib/test_second.py": 48.0})
        self.write(self.state / "barrier", "two")
        self.configure_gate("merge_gate_governor_gib=0.26171875")
        result = self.run_gate("--tests-only", ignored_signals=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        active: set[int] = set()
        peak = 0
        events = self.module_events()
        for event in events:
            if event["event"] == "start":
                active.add(event["pid"])
            else:
                active.remove(event["pid"])
            peak = max(peak, len(active))
        self.assertGreaterEqual(peak, 2)
        self.assertEqual(active, set())
        self.assertEqual(len(events), 2 * self.module_count)
        self.assertIn("memory governor 0.261719 GiB", result.stdout)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
        self.assert_not_pushed()
        self.assertEqual(len([call for call in self.calls() if call["tool"] == "basedpyright"]), 1)
        self.assertFalse(any(call["tool"] == "ssh" for call in self.calls()))

    @gate_suites("lib", "hooks")
    def test_running_job_limit_bounds_overlap_without_dropping_modules(self) -> None:
        self.write(self.state / "concurrency-barrier", "held")
        started: list[merge_gate.RunningGateJob] = []
        start = merge_gate.start_gate_job
        sample = merge_gate.process_memory_mib

        def start_blocked(suite: str, path: Path, argv: list[str], repo: Path,
                          environment: dict[str, str], log: Path, *,
                kind: merge_gate.GateJobKind = merge_gate.GateJobKind.TEST_MODULE) -> merge_gate.RunningGateJob:
            job = start(suite, path, argv, repo, environment, log, kind=kind)
            started.append(job)
            wait_for_file(self.state / f"{job.process.pid}.started")
            return job

        def release_at_sample(roots: set[int]) -> dict[int, float]:
            # No job can finish during the start loop. Without the job limit,
            # memory admission would start more than two before this sample.
            for job in started:
                (self.state / f"{job.process.pid}.released").touch()
            return sample(roots)

        output = io.StringIO()
        with mock.patch.dict(os.environ, self.environment), \
                mock.patch.object(merge_gate, "start_gate_job", side_effect=start_blocked), \
                mock.patch.object(merge_gate, "process_memory_mib", side_effect=release_at_sample), \
                redirect_stdout(output):
            run = merge_gate.run_suites(self.checkout, max_running_jobs=2)
        self.assertTrue(run.passed, output.getvalue())
        expected = {(suite, "test_first.py") for suite in self.suites}
        expected.add(("lib", "test_second.py"))
        self.assertEqual({(job.suite, job.module) for job in run.jobs}, expected)
        self.assertEqual(len(run.jobs), self.module_count)
        active: set[int] = set()
        peak = 0
        events = self.module_events()
        for event in events:
            if event["event"] == "start":
                self.assertNotIn(event["pid"], active)
                active.add(event["pid"])
            else:
                active.remove(event["pid"])
            peak = max(peak, len(active))
            self.assertLessEqual(len(active), 2, events)
        self.assertEqual(peak, 2)
        self.assertEqual(active, set())
        self.assertEqual(len(events), 2 * self.module_count)
        self.assertRegex(output.getvalue(), r"(?m)^Gate tests: [\d.]+s; at most 2 modules at once; peak memory:")
        self.assertEqual(output.getvalue().count("at most 2 modules at once"), 1)

    @gate_suites("lib")
    def test_small_governor_starts_one_module_and_serializes_the_rest(self) -> None:
        # Each unrecorded job reserves 640 MiB. The governor must still start one job.
        self.configure_gate("merge_gate_governor_gib=0.125")
        result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        events = self.module_events()
        self.assertEqual(len(events), 2 * self.module_count)
        for start, finish in zip(events[::2], events[1::2], strict=True):
            self.assertEqual((start["event"], finish["event"]), ("start", "finish"))
            self.assertEqual(start["pid"], finish["pid"])
        self.assertIn("memory governor 0.125 GiB", result.stdout)

    def test_nonpositive_governor_refuses_without_running_modules(self) -> None:
        self.configure_gate("merge_gate_governor_gib=0")
        result = self.run_gate("--tests-only")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("merge_gate_governor_gib", result.stdout + result.stderr)
        self.assertEqual(self.module_events(), [])

    def test_a_module_still_running_at_the_time_limit_is_killed_and_the_gate_is_red(self) -> None:
        self.write(self.state / "hold-module", "hold")
        cpu_path = self.state / "runner" / "module_cpu_seconds.json"
        merge_gate.save_measures(cpu_path, {"hooks/test_first.py": 12.0})
        self.environment["BUILDLOG_SCOPE"] = "1"
        output = io.StringIO()
        monotonic = time.monotonic
        deadline = monotonic() + TEST_HANG_GUARD_SECS
        sampled: set[int] = set()
        killed: list[int] = []
        kill = merge_gate.kill_job_group

        def module_clock() -> float:
            if monotonic() >= deadline:
                raise TimeoutError("gate did not stop the held module")
            ready = self.state / "module-held"
            return monotonic() + (2000 if ready.exists() and int(ready.read_text()) in sampled else 0)

        def sample(roots: set[int]) -> dict[int, float]:
            sampled.update(roots)
            return dict.fromkeys(roots, 1.0)

        def kill_module(job: merge_gate.RunningGateJob) -> None:
            killed.append(job.process.pid)
            kill(job)

        with mock.patch.dict(os.environ, self.environment), \
                mock.patch.object(queue_place, "in_builds_slice", return_value=False), \
                mock.patch("scripts.production.merge_gate.time.monotonic", side_effect=module_clock), \
                mock.patch.object(merge_gate, "process_memory_mib", side_effect=sample), \
                mock.patch.object(merge_gate, "kill_job_group", side_effect=kill_module), redirect_stdout(output):
            result = merge_gate.run_suites(self.checkout)
        self.assertFalse(result.passed, output.getvalue())
        self.assertIn("hooks/test_first.py killed: still running after 1800 s, the module time limit", output.getvalue())
        self.assertRegex(output.getvalue(), r"(?m)^hooks: RED ")
        held = int((self.state / "module-held").read_text())
        self.assert_process_stopped(held)
        self.assertIn(held, sampled)
        self.assertEqual(set(killed), {held})
        scopes = [call for call in self.calls() if call["tool"] == "systemd-run" and call["args"][-1] != "true"]
        self.assertEqual([call["pid"] for call in scopes], [held])
        starts = [row for row in self.module_events() if row["event"] == "start"]
        self.assertEqual(len(starts), 1)
        self.assertTrue(cpu_path.is_file())
        self.assertEqual(merge_gate.read_measures(cpu_path), {"hooks/test_first.py": 12.0})
        self.assertGreater(result.jobs[0].cpu_seconds, 0.0)

    def test_module_time_limit_kills_and_reaps_a_detached_orphan(self) -> None:
        processes = OwnedTestProcesses()
        self.addCleanup(processes.finish)
        self.environment = processes.environment(self.environment)
        self.write(self.checkout / "scripts/hooks/test_first.py", '''import os
from pathlib import Path
import signal
import unittest

class HeldModule(unittest.TestCase):
    def test_detached_orphan(self):
        reader, writer = os.pipe()
        intermediate = os.fork()
        if intermediate == 0:
            os.close(reader)
            if os.fork() != 0:
                os._exit(0)
            os.setsid()
            os.write(writer, str(os.getpid()).encode())
            os.close(writer)
            while True:
                signal.pause()
        os.close(writer)
        pid = int(os.read(reader, 32))
        os.close(reader)
        os.waitpid(intermediate, 0)
        state = Path(os.environ["MERGE_GATE_TEST_STATE"])
        temporary = state / "detached-pid.tmp"
        temporary.write_text(str(pid))
        temporary.replace(state / "detached-pid")
        signal.pause()
''')
        output = io.StringIO()
        monotonic = time.monotonic
        deadline = monotonic() + TEST_HANG_GUARD_SECS
        handles: dict[int, int] = {}

        def module_clock() -> float:
            if monotonic() >= deadline:
                raise TimeoutError("The gate did not stop the held module")
            ready = self.state / "detached-pid"
            if not ready.exists():
                return monotonic()
            pid = int(ready.read_text())
            if pid not in handles:
                descriptor = os.pidfd_open(pid)
                handles[pid] = descriptor
                self.addCleanup(os.close, descriptor)
            return monotonic() + 2000

        with mock.patch.dict(os.environ, self.environment), \
                mock.patch.object(time, "monotonic", side_effect=module_clock), redirect_stdout(output):
            result = merge_gate.run_suites(self.checkout)
        self.assertFalse(result.passed, output.getvalue())
        self.assertIn("hooks/test_first.py killed: still running after 1800 s, the module time limit", output.getvalue())
        observer = select.poll()
        for descriptor in handles.values():
            observer.register(descriptor, select.POLLHUP)
        self.assertTrue(observer.poll(0), "The module time limit left the detached orphan alive or unreaped")

    def test_nonpositive_module_time_limit_refuses_without_running_modules(self) -> None:
        result = self.run_gate("--tests-only", "--module-time-limit-s", "0")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("module time limit must be positive", result.stdout + result.stderr)
        self.assertEqual(self.module_events(), [])

    @gate_suites("lib", "hooks")
    def test_runner_reports_load_at_its_own_start_and_end(self) -> None:
        output = io.StringIO()
        calls = 0

        def measured_load() -> float:
            nonlocal calls
            calls += 1
            if calls == 1:
                return 10.0
            events = self.state / "modules.jsonl"
            return 20.0 if events.exists() and len(events.read_text().splitlines()) == 2 * self.module_count else 60.0

        with mock.patch.dict(os.environ, self.environment), mock.patch.object(merge_gate, "one_minute_load", side_effect=measured_load), redirect_stdout(output):
            result = merge_gate.run_suites(self.checkout)
        self.assertTrue(result.passed, output.getvalue())
        self.assertIn("One-minute load: start=10.00 max=60.00 end=20.00", output.getvalue())


if __name__ == "__main__":
    _ = unittest.main()
