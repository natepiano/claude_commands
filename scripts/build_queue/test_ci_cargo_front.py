"""CI cargo jobs share the admission queue and release their grants."""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import select
import shlex
import signal
import subprocess
import sys
from typing import TextIO, cast
import unittest

from scripts.build_queue import admission_protocol as wire
from scripts.build_queue import admission_records as records
from scripts.build_queue import ci_cargo_front
from scripts.build_queue.admission_test_support import AdmissionFixture, GIB, only_admitted
from scripts.build_queue.queue_fixture import write_memory_config, write_process


DENIED_HOME_CLIENT = """
import sys
from unittest import mock
from scripts.build_queue import admission_client as client, admission_protocol as wire
from scripts.build_queue import admission_records as records, step_estimator
from scripts.buildlog import store
from scripts.lib import conf

request = wire.parse_request(sys.stdin.readline())
output = client.WaitOutput(configuration=wire.PROTOCOL_ACQUISITION_TIMING)
def held(reply):
    output(reply)
    print('WAIT', flush=True)
with mock.patch.object(records, 'read_memory_config', side_effect=PermissionError('runner config')), \\
     mock.patch.object(step_estimator, 'step_estimate', side_effect=PermissionError('runner history')), \\
     mock.patch.object(conf, 'read', side_effect=PermissionError('runner config path')), \\
     mock.patch.object(store, 'locked_rows', side_effect=PermissionError('runner history path')):
    grant = client.service_admit(request, held)
    output.finished()
    print(wire.encode_reply(grant), end='', flush=True)
    if isinstance(grant, wire.Admitted):
        print(wire.encode_reply(client.release(grant.reservation, 137, grant.cleanup_timing)), end='', flush=True)
"""

FAKE_CARGO = """#!/usr/bin/env python3
import json
import os
import signal
import sys
import threading
finished = threading.Event()
signal.signal(signal.SIGUSR1, lambda number, frame: finished.set())
descendant = 0
if '--descendant' in sys.argv:
    ready_read, ready_write = os.pipe()
    descendant = os.fork()
    if descendant == 0:
        os.close(ready_read)
        os.setsid()
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        os.write(ready_write, b'R')
        os.close(ready_write)
        finished.wait(20)
        os._exit(0)
    os.close(ready_write)
    os.read(ready_read, 1)
    os.close(ready_read)
print(json.dumps({'pid': os.getpid(), 'argv': sys.argv[1:],
    'descendant': descendant,
    'progress': os.environ.get('CARGO_TERM_PROGRESS_WHEN'),
    'width': os.environ.get('CARGO_TERM_PROGRESS_WIDTH')}), flush=True)
if '--version' not in sys.argv:
    finished.wait(20)
raise SystemExit(int(os.environ.get('FAKE_CARGO_STATUS', '0')))
"""

DENIED_HOME_FRONT = """
import faulthandler
import runpy
import signal
from unittest import mock
from scripts.build_queue import admission_records as records, step_estimator
from scripts.buildlog import store
from scripts.lib import conf
faulthandler.register(signal.SIGUSR2)
with mock.patch.object(records, 'read_memory_config', side_effect=PermissionError('runner config')), \\
     mock.patch.object(step_estimator, 'step_estimate', side_effect=PermissionError('runner history')), \\
     mock.patch.object(conf, 'read', side_effect=PermissionError('runner config path')), \\
     mock.patch.object(store, 'locked_rows', side_effect=PermissionError('runner history path')):
    runpy.run_module('scripts.build_queue.ci_cargo_front', run_name='__main__')
"""


class CargoLifetimeTests(unittest.TestCase):
    def test_text_flags_skip_admission_before_the_program_arguments(self) -> None:
        for arguments, expected in (
            (("clippy", "--version"), False),
            (("build", "-V"), False),
            (("test", "--help"), False),
            (("nextest", "-h"), False),
            (("install", "--help"), False),
            (("install", "--version", "1.2", "foo"), True),
            (("run", "--", "--help"), True),
            (("test", "--", "--version"), True),
            (("clippy",), True),
            (("+stable", "--config", "build.jobs=2", "clippy", "--version"), False),
            (("--manifest-path", "Cargo.toml", "build"), True),
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(ci_cargo_front.building_call(arguments), expected)

    def test_packaged_wrapper_selects_the_next_cargo_and_admits_once(self) -> None:
        for use_shared, explicit in ((False, False), (True, False), (False, True)):
            with self.subTest(use_shared=use_shared, explicit=explicit), AdmissionFixture() as fixture, fixture.serving():
                front_dir = fixture.root / "front-bin"
                cargo_dir = fixture.root / "cargo-bin"
                devshell_dir = fixture.root / "devshell-bin"
                shim_dir = fixture.root / "shim-bin"
                shared = fixture.root / "admission-shared"
                job_temporary = fixture.root / "job-temporary"
                for folder in (front_dir, cargo_dir, devshell_dir, shim_dir, shared, job_temporary):
                    folder.mkdir()
                expected_shared: list[Path] = []
                prefix = f"build-admission-ci-{os.getuid()}-"
                if use_shared:
                    with subprocess.Popen([sys.executable, "-c", "pass"]) as finished:
                        self.assertEqual(finished.wait(timeout=3), 0)
                    live_start = records.process_start_time(os.getpid())
                    self.assertIsNotNone(live_start)
                    live_folder = shared / f"{prefix}{os.getpid()}-{live_start}-live"
                    for leftover in (shared / f"{prefix}{finished.pid}-1-dead", shared / f"{prefix}old", live_folder):
                        leftover.mkdir()
                        _ = (leftover / "cargo.cgroup").write_text("")
                    expected_shared = [live_folder]
                wrapper = front_dir / "cargo"
                cargo = cargo_dir / "cargo"
                compiler = devshell_dir / "cc"
                _ = compiler.write_text("#!/bin/sh\nexit 0\n")
                compiler.chmod(0o755)
                (shim_dir / "cargo").symlink_to(wrapper)
                script = Path(__file__).with_name("ci_cargo_front.py")
                shared_argument = " --shared-folder " + shlex.quote(str(shared)) if use_shared else ""
                cargo_argument = " --cargo " + shlex.quote(str(cargo)) if explicit else ""
                _ = wrapper.write_text("#!/bin/sh\nexec " + shlex.join((sys.executable, "-I", str(script)))
                    + ' --front "$0"' + shared_argument + cargo_argument + ' -- "$@"\n')
                wrapper.chmod(0o755)
                calls = fixture.root / "cargo-calls"
                _ = cargo.write_text(FAKE_CARGO.replace("descendant = 0", "with open(" + repr(str(calls))
                    + ", 'a') as output:\n    output.write('\\n'.join(os.environ[name] for name in"
                    + " ('PATH', 'TMPDIR', 'PYTHONPATH')) + '\\n')\ndescendant = 0"))
                cargo.chmod(0o755)
                environment = {**os.environ, "PYTHONPATH": "job-module-path", "TMPDIR": str(job_temporary),
                    "PATH": os.pathsep.join((str(front_dir), str(devshell_dir), str(shim_dir),
                                              str(cargo_dir), os.environ["PATH"]))}
                child = subprocess.Popen([str(wrapper), "build"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, env=environment, cwd=fixture.root)
                self.addCleanup(self.stop_front, child)
                data = self.started(child)
                self.supply_front_memory(fixture, child)
                with fixture.loop_turn():
                    record, = fixture.server.reservations.values()
                    self.assertEqual(Path(record.sidecar).parent.parent, shared if use_shared else job_temporary)
                    if use_shared:
                        self.assertTrue(Path(record.sidecar).parent.name.startswith(
                            f"{prefix}{child.pid}-{record.owner.start_time}-"))
                os.kill(int(str(data["pid"])), signal.SIGUSR1)
                output, errors = child.communicate(timeout=8)
                self.assertEqual(child.returncode, 0, output + errors)
                paths, temporary, module_path = calls.read_text().splitlines()
                self.assertNotIn(str(front_dir), paths.split(os.pathsep))
                self.assertNotIn(str(shim_dir), paths.split(os.pathsep))
                expected_folders = (cargo_dir, devshell_dir) if explicit else (devshell_dir, cargo_dir)
                self.assertEqual(paths.split(os.pathsep),
                    [*(str(folder) for folder in expected_folders), *os.environ["PATH"].split(os.pathsep)])
                self.assertEqual((temporary, module_path), (str(job_temporary), "job-module-path"))
                self.assertEqual(list(shared.iterdir()), expected_shared)
                self.assertEqual(list(job_temporary.iterdir()), [])
                self.assertEqual(len(fixture.history()), 1)
                with fixture.loop_turn():
                    self.assertFalse(fixture.server.reservations)

    def test_killing_the_front_also_kills_its_cargo_process(self) -> None:
        with AdmissionFixture() as fixture, fixture.serving():
            child = self.start_front(fixture, "build")
            data = self.started(child)
            cargo = int(str(data["pid"]))
            descriptor = os.pidfd_open(cargo)
            try:
                self.assertFalse(select.select([descriptor], [], [], 0)[0])
                child.kill()
                _ = child.communicate(timeout=3)
                self.assertTrue(select.select([descriptor], [], [], 3)[0])
                self.assertEqual(child.returncode, -signal.SIGKILL)
            finally:
                os.close(descriptor)
                try:
                    os.kill(cargo, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def start_front(self, fixture: AdmissionFixture, *arguments: str) -> subprocess.Popen[str]:
        cargo = fixture.root / "fake-cargo"
        _ = cargo.write_text(FAKE_CARGO)
        cargo.chmod(0o755)
        environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "lib/python_startup"),
            "HOME": str(fixture.root / "runner-home"), "PYTHONDONTWRITEBYTECODE": "1",
            "TMPDIR": str(fixture.root),
            "RUNNER_NAME": "hana-runner-a", "GITHUB_JOB": "compile"}
        child = subprocess.Popen([sys.executable, "-c", DENIED_HOME_FRONT,
            "--cargo", str(cargo), "--", *arguments], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=environment, cwd=fixture.root)
        self.addCleanup(self.stop_front, child)
        return child

    def stop_front(self, child: subprocess.Popen[str]) -> None:
        if child.poll() is None:
            child.terminate()
        try:
            _ = child.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            child.kill()
            _ = child.communicate(timeout=3)

    def started(self, child: subprocess.Popen[str]) -> dict[str, object]:
        stream = cast(TextIO, child.stdout)
        self.assertTrue(select.select([stream], [], [], 8)[0])
        return cast(dict[str, object], json.loads(stream.readline()))

    def supply_front_memory(self, fixture: AdmissionFixture, child: subprocess.Popen[str]) -> None:
        with fixture.loop_turn():
            record, = [record for record in fixture.server.reservations.values() if record.owner.pid == child.pid]
            write_process(fixture.proc.path, child.pid, GIB, start=record.owner.start_time)
            _ = fixture.run_pass()

    def test_fitting_cargo_calls_run_together_without_runner_home_reads(self) -> None:
        with AdmissionFixture() as fixture:
            write_memory_config(fixture.root / "build_admission.conf", **{"program_first_peak_gib.cargo": "4"})
            with fixture.serving():
                first = self.start_front(fixture, "build")
                first_data = self.started(first)
                self.supply_front_memory(fixture, first)
                fixture.allow_next_start()
                second = self.start_front(fixture, "+stable", "check")
                second_data = self.started(second)
                self.supply_front_memory(fixture, second)
                with fixture.loop_turn():
                    self.assertEqual(len(fixture.server.reservations), 2)
                self.assertIsNone(first.poll())
                self.assertIsNone(second.poll())
                for child, data in ((first, first_data), (second, second_data)):
                    self.assertEqual(data["progress"], "always")
                    self.assertEqual(data["width"], "80")
                    os.kill(int(str(data["pid"])), signal.SIGUSR1)
                    output, errors = child.communicate(timeout=8)
                    self.assertEqual(child.returncode, 0, output + errors)
                with fixture.loop_turn():
                    self.assertFalse(fixture.server.reservations)
            self.assertNotEqual(first_data["pid"], second_data["pid"])
            self.assertEqual(sorted(cast(int, row["status"]) for row in fixture.history()), [0, 0])

    def test_nonbuilding_cargo_passes_through_during_an_admission_outage(self) -> None:
        for arguments in (["--version"], ["clippy", "--version"]):
            with self.subTest(arguments=arguments), AdmissionFixture() as fixture:
                fixture.kill_socket_server()
                child = self.start_front(fixture, *arguments)
                output, errors = child.communicate(timeout=8)
                self.assertEqual(child.returncode, 0, errors)
                data = cast(dict[str, object], json.loads(output))
                self.assertEqual(data["argv"], arguments)
                self.assertIsNone(data["progress"])
                self.assertFalse(fixture.server.reservations)

    def test_cargo_that_does_not_fit_waits_until_the_first_cargo_releases(self) -> None:
        with AdmissionFixture() as fixture:
            write_memory_config(fixture.root / "build_admission.conf", **{"program_first_peak_gib.cargo": "4"})
            fixture.free(7 * GIB)
            with fixture.serving():
                first = self.start_front(fixture, "build")
                first_data = self.started(first)
                fixture.allow_next_start()
                second = self.start_front(fixture, "build")
                errors = cast(TextIO, second.stderr)
                self.assertTrue(select.select([errors], [], [], 8)[0])
                self.assertIn("memory", errors.readline())
                with fixture.loop_turn():
                    self.assertEqual(len(fixture.server.reservations), 1)
                    self.assertEqual(len(fixture.server.waiters), 1)
                os.kill(int(str(first_data["pid"])), signal.SIGUSR1)
                first_output, first_errors = first.communicate(timeout=8)
                self.assertEqual(first.returncode, 0, first_output + first_errors)
                fixture.allow_next_start()
                second_data = self.started(second)
                os.kill(int(str(second_data["pid"])), signal.SIGUSR1)
                second_output, second_errors = second.communicate(timeout=8)
                self.assertEqual(second.returncode, 0, second_output + second_errors)
                with fixture.loop_turn():
                    self.assertFalse(fixture.server.reservations)
                    self.assertFalse(fixture.server.waiters)

    def test_cancelled_cargo_releases_and_records_a_failed_peak(self) -> None:
        with AdmissionFixture() as fixture, fixture.serving():
            child = self.start_front(fixture, "build")
            _ = self.started(child)
            self.supply_front_memory(fixture, child)
            child.terminate()
            output, errors = child.communicate(timeout=8)
            self.assertEqual(child.returncode, 143, output + errors)
            with fixture.loop_turn():
                self.assertFalse(fixture.server.reservations)
            row, = fixture.history()
            self.assertEqual((row["status"], row["outcome"]), (143, "killed"))

    def test_cargo_release_waits_until_a_surviving_descendant_stops(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(program_sample_seconds=0.01, program_stop_seconds=0.2)
            with fixture.serving():
                child = self.start_front(fixture, "build", "--descendant")
                data = self.started(child)
                self.supply_front_memory(fixture, child)
                descendant = int(str(data["descendant"]))
                descriptor = os.pidfd_open(descendant)
                try:
                    self.assertFalse(select.select([descriptor], [], [], 0)[0])
                    with fixture.loop_turn():
                        self.assertEqual(len(fixture.server.reservations), 1)
                    os.kill(int(str(data["pid"])), signal.SIGUSR1)
                    try:
                        output, errors = child.communicate(timeout=8)
                    except subprocess.TimeoutExpired:
                        os.kill(child.pid, signal.SIGUSR2)
                        os.kill(descendant, signal.SIGKILL)
                        output, errors = child.communicate(timeout=8)
                        self.fail("the cargo front did not finish descendant cleanup: " + output + errors)
                    self.assertEqual(child.returncode, 0, output + errors)
                    self.assertTrue(select.select([descriptor], [], [], 0)[0])
                    with fixture.loop_turn():
                        self.assertFalse(fixture.server.reservations)
                finally:
                    os.close(descriptor)
                    try:
                        os.kill(descendant, signal.SIGKILL)
                    except ProcessLookupError:
                        pass


class RunnerHomeTests(unittest.TestCase):
    def test_service_estimate_wait_and_failed_release_need_no_client_config_or_history(self) -> None:
        with AdmissionFixture() as fixture:
            write_memory_config(fixture.root / "build_admission.conf", **{"program_first_peak_gib.cargo": "4"})
            _ = fixture.occupy_host()
            fixture.free(0)
            request = fixture.service_request()
            environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "lib/python_startup"),
                           "HOME": str(fixture.root / "runner-home"), "PYTHONDONTWRITEBYTECODE": "1"}
            with fixture.serving():
                child = subprocess.Popen([sys.executable, "-c", DENIED_HOME_CLIENT],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, env=environment, cwd=fixture.root)
                try:
                    if child.stdin is None or child.stdout is None:
                        self.fail("the runner client has no protocol pipes")
                    _ = child.stdin.write(wire.encode_request(request))
                    child.stdin.flush()
                    self.assertTrue(select.select([child.stdout], [], [], 5)[0])
                    self.assertEqual(child.stdout.readline(), "WAIT\n")
                    with fixture.loop_turn():
                        self.assertIn(request.episode, fixture.server.waiters)
                        fixture.free(60 * GIB)
                        fixture.allow_next_start()
                        _ = fixture.server.run_pass()
                    output, errors = child.communicate(timeout=8)
                finally:
                    if child.poll() is None:
                        child.kill()
                    _ = child.wait(timeout=3)
            self.assertEqual(child.returncode, 0, errors)
            replies = [wire.parse_reply(line) for line in output.splitlines() if line != "WAIT"]
            grant, released = replies
            self.assertIsInstance(grant, wire.Admitted)
            if isinstance(grant, wire.Admitted):
                self.assertEqual(grant.need, 4 * GIB)
            self.assertEqual(released, wire.Ok())
            self.assertIn("waiting for memory", errors)
            self.assertIn("starting", errors)
            self.assertNotIn(request.episode, fixture.server.reservations)
            failed, = fixture.history()
            self.assertEqual(failed["status"], 137)


class CiJobOrderTests(unittest.TestCase):
    def test_two_fitting_jobs_hold_reservations_at_the_same_time(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(24 * GIB)
            first, second = (fixture.request(need=8 * GIB) for _ in range(2))
            first_grant = only_admitted(fixture.server.handle_request(first))
            fixture.allow_next_start()
            second_grant = only_admitted(fixture.server.handle_request(second))
            self.assertNotEqual(first_grant.reservation, second_grant.reservation)
            self.assertEqual(set(fixture.server.reservations), {first.episode, second.episode})
            fixture.release(first_grant)
            self.assertEqual(set(fixture.server.reservations), {second.episode})
            fixture.release(second_grant)
            self.assertFalse(fixture.server.reservations)

    def test_jobs_that_do_not_fit_keep_arrival_order_until_each_release(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(14 * GIB)
            first, second, third = (fixture.request(need=8 * GIB) for _ in range(3))
            second = replace(second, since=first.since + 1)
            third = replace(third, since=first.since + 2)
            first_grant = only_admitted(fixture.server.handle_request(first))
            fixture.allow_next_start()
            second_wait, = fixture.server.handle_request(second)
            third_wait, = fixture.server.handle_request(third)
            self.assertIsInstance(second_wait, wire.Wait)
            self.assertIsInstance(third_wait, wire.Wait)
            self.assertEqual(fixture.queue_pids(), [second.owner.pid, third.owner.pid])
            fixture.release(first_grant)
            _ = fixture.run_pass()
            self.assertEqual(set(fixture.server.reservations), {second.episode})
            _ = fixture.run_pass()
            self.assertEqual(fixture.queue_pids(), [third.owner.pid])
            second_grant = only_admitted(fixture.server.handle_request(second))
            fixture.release(second_grant)
            fixture.allow_next_start()
            _ = fixture.run_pass()
            self.assertEqual(set(fixture.server.reservations), {third.episode})
            third_grant = only_admitted(fixture.server.handle_request(third))
            fixture.release(third_grant)
            self.assertFalse(fixture.server.reservations)
